"""In-place Pod resize validator (pure simulation, no Kubernetes connection).

Simulates the kubelet-side flow of the Kubernetes in-place pod vertical
scaling feature: the API server may already show the new requests in the
Pod status while the actual cgroup limits on the node are only updated
container by container. This module computes the *actual* resource state
from the declared inputs and enforces three safety rules:

1. The Pod as a whole is only reported complete when *every* container's
   cgroup update succeeded.
2. A downsize (shrink) whose container resize policy requires a restart
   must be explicitly requested, otherwise the resize cannot continue.
3. On partial failure every already-updated container is rolled back to
   one unified point (the original limits), so the Pod never stays in a
   mixed state.

Python 3.8.2 compatible, standard library only.
"""

RESOURCES = ("cpu", "memory")

RESTART_NOT_REQUIRED = "NotRequired"
RESTART_CONTAINER = "RestartContainer"

STATUS_COMPLETED = "Completed"
STATUS_ROLLED_BACK = "RolledBack"
STATUS_BLOCKED = "Blocked"

QOS_GUARANTEED = "Guaranteed"
QOS_BURSTABLE = "Burstable"
QOS_BEST_EFFORT = "BestEffort"


class ResizeInputError(ValueError):
    """Raised when the resize request itself is malformed."""


def _validate_resource_map(value, field):
    if not isinstance(value, dict):
        raise ResizeInputError("%s must be a dict of resource quantities" % field)
    for resource in RESOURCES:
        quantity = value.get(resource, 0)
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
            raise ResizeInputError(
                "%s.%s must be a numeric quantity" % (field, resource))
        if quantity < 0:
            raise ResizeInputError(
                "%s.%s must not be negative" % (field, resource))


def compute_qos(pod_request, limits_by_container):
    """Derive the QoS class from requests and (actual) limits."""
    has_any = False
    for resource in RESOURCES:
        requested = pod_request.get(resource, 0)
        if requested <= 0:
            continue
        has_any = True
        total_limit = sum(
            limits.get(resource, 0) for limits in limits_by_container.values())
        if total_limit != requested:
            return QOS_BURSTABLE
    if not has_any:
        return QOS_BEST_EFFORT
    return QOS_GUARANTEED


def _check_headroom(current_limits, target_limits, node_headroom):
    """Return (ok, shortage). Only growth consumes node headroom."""
    shortage = {}
    for resource in RESOURCES:
        current_total = sum(l.get(resource, 0) for l in current_limits.values())
        target_total = sum(l.get(resource, 0) for l in target_limits.values())
        needed = max(0, target_total - current_total)
        available = node_headroom.get(resource, 0)
        if needed > available:
            shortage[resource] = {"needed": needed, "available": available}
    return (not shortage), shortage


def _find_restart_required_shrinks(current_limits, target_limits, resize_policy):
    """Containers whose shrink requires a restart per their resize policy."""
    blocked = []
    for name, target in target_limits.items():
        current = current_limits.get(name, {})
        policy = resize_policy.get(name, {})
        for resource in RESOURCES:
            if target.get(resource, 0) < current.get(resource, 0):
                resource_policy = policy.get(resource, RESTART_NOT_REQUIRED)
                if resource_policy == RESTART_CONTAINER:
                    blocked.append({
                        "container": name,
                        "resource": resource,
                        "current": current.get(resource, 0),
                        "target": target.get(resource, 0),
                    })
    return blocked


def _empty_rollback():
    return {
        "rolled_back": False,
        "rollback_point": None,
        "fields": [],
        "containers": [],
    }


def validate_resize(pod_request, current_limits, target_limits, node_headroom,
                    containers, qos, probes, restart_policy, update_receipts,
                    allow_restart=False):
    """Compute the actual resource state of an in-place Pod resize.

    Parameters
    ----------
    pod_request: dict
        The new requests already reflected in the Pod status, e.g.
        {"cpu": 500, "memory": 512}.
    current_limits: dict
        Per-container limits currently enforced by the cgroups:
        {"app": {"cpu": 250, "memory": 256}, ...}.
    target_limits: dict
        Per-container limits the resize wants to reach (same shape).
    node_headroom: dict
        Allocatable headroom confirmed by the scheduler:
        {"cpu": ..., "memory": ...}.
    containers: list of dict
        Container cgroup handles, each {"name": str, "cgroup_writable": bool}.
    qos: str
        QoS class assigned to the Pod before the resize.
    probes: dict
        Probe gating, e.g. {"startup_passed": True, "ready": True}.
    restart_policy: dict
        {"pod": "Always", "resize": {container: {resource: policy}}} where
        policy is "NotRequired" or "RestartContainer".
    update_receipts: list of dict
        Per-container cgroup update receipts, each
        {"receipt_id": str, "container": str, "success": bool,
         "error": str|None}. Duplicated receipt ids are applied once.
    allow_restart: bool
        Explicit approval for restart-requiring shrinks.

    Returns
    -------
    dict with keys: status, pod_completed, actual_limits, failed_containers,
    qos, rollback, reason.
    """
    _validate_resource_map(pod_request, "pod_request")
    _validate_resource_map(node_headroom, "node_headroom")
    for field, mapping in (("current_limits", current_limits),
                           ("target_limits", target_limits)):
        if not isinstance(mapping, dict) or not mapping:
            raise ResizeInputError("%s must be a non-empty container map" % field)
        for name, limits in mapping.items():
            _validate_resource_map(limits, "%s[%s]" % (field, name))

    if set(current_limits) != set(target_limits):
        raise ResizeInputError(
            "current_limits and target_limits must cover the same containers")

    container_names = set(current_limits)
    cgroup_by_name = {}
    for entry in containers or []:
        cgroup_by_name[entry.get("name")] = entry
    missing_cgroups = sorted(container_names - set(cgroup_by_name))
    if missing_cgroups:
        raise ResizeInputError(
            "missing cgroup handles for containers: %s" % ", ".join(missing_cgroups))

    resize_policy = (restart_policy or {}).get("resize", {})
    probes = probes or {}

    result = {
        "status": STATUS_BLOCKED,
        "pod_completed": False,
        "actual_limits": {name: dict(limits)
                          for name, limits in current_limits.items()},
        "failed_containers": [],
        "qos": qos,
        "rollback": _empty_rollback(),
        "reason": None,
    }

    # Gate 1: probes must allow touching the cgroups at all.
    if probes.get("startup_passed") is False:
        result["reason"] = "startup probe has not passed; cgroup updates are unsafe"
        return result
    if probes.get("ready") is False:
        result["reason"] = "readiness probe failing; resize deferred"
        return result

    # Gate 2: restart-requiring shrinks need an explicit request.
    restart_blocked = _find_restart_required_shrinks(
        current_limits, target_limits, resize_policy)
    if restart_blocked and not allow_restart:
        result["failed_containers"] = sorted(
            {item["container"] for item in restart_blocked})
        result["reason"] = (
            "shrink requires container restart but was not explicitly "
            "requested: %s" % "; ".join(
                "%s/%s %s->%s" % (i["container"], i["resource"],
                                  i["current"], i["target"])
                for i in restart_blocked))
        return result

    # Gate 3: the scheduler-confirmed headroom must cover the growth.
    headroom_ok, shortage = _check_headroom(
        current_limits, target_limits, node_headroom)
    if not headroom_ok:
        result["reason"] = (
            "insufficient node headroom: %s" % "; ".join(
                "%s needs %s, only %s available"
                % (res, s["needed"], s["available"])
                for res, s in sorted(shortage.items())))
        return result

    # Apply phase: deduplicate receipts by id (idempotent re-delivery),
    # then apply each container's cgroup update exactly once.
    seen_receipts = set()
    receipts_by_container = {}
    duplicate_receipts = []
    for receipt in update_receipts or []:
        receipt_id = receipt.get("receipt_id")
        if receipt_id in seen_receipts:
            duplicate_receipts.append(receipt_id)
            continue
        seen_receipts.add(receipt_id)
        receipts_by_container.setdefault(
            receipt.get("container"), []).append(receipt)

    applied = {}
    failed = {}
    for name in sorted(container_names):
        cgroup = cgroup_by_name.get(name, {})
        if not cgroup.get("cgroup_writable", True):
            failed[name] = "cgroup not writable"
            continue
        receipts = receipts_by_container.get(name, [])
        if not receipts:
            failed[name] = "no cgroup update receipt"
            continue
        last = receipts[-1]
        if last.get("success"):
            applied[name] = dict(target_limits[name])
        else:
            failed[name] = last.get("error") or "cgroup update rejected"

    if failed:
        # Rule 3: roll every applied container back to the unified point
        # (the original limits); the Pod keeps its old, consistent state.
        result["status"] = STATUS_ROLLED_BACK
        result["actual_limits"] = {name: dict(limits)
                                   for name, limits in current_limits.items()}
        result["failed_containers"] = sorted(failed)
        result["rollback"] = {
            "rolled_back": True,
            "rollback_point": "original_limits",
            "fields": ["actual_limits", "cgroup_cpu", "cgroup_memory"],
            "containers": sorted(applied),
            "errors": failed,
        }
        result["reason"] = (
            "partial cgroup update failure in %s; rolled back %d applied "
            "container(s) to original limits"
            % (", ".join(sorted(failed)), len(applied)))
        result["qos"] = compute_qos(pod_request, result["actual_limits"])
        return result

    # Rule 1: only now, with every container applied, is the Pod complete.
    result["status"] = STATUS_COMPLETED
    result["pod_completed"] = True
    result["actual_limits"] = applied
    result["qos"] = compute_qos(pod_request, applied)
    result["reason"] = None
    if duplicate_receipts:
        result["duplicate_receipts_ignored"] = sorted(duplicate_receipts)
    return result
