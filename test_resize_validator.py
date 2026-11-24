import unittest

from resize_validator import (
    QOS_BEST_EFFORT,
    QOS_BURSTABLE,
    QOS_GUARANTEED,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_ROLLED_BACK,
    ResizeInputError,
    compute_qos,
    validate_resize,
)


def base_kwargs():
    """A healthy two-container grow resize with everything in place."""
    return dict(
        pod_request={"cpu": 600, "memory": 768},
        current_limits={
            "app": {"cpu": 200, "memory": 256},
            "sidecar": {"cpu": 100, "memory": 128},
        },
        target_limits={
            "app": {"cpu": 400, "memory": 512},
            "sidecar": {"cpu": 200, "memory": 256},
        },
        node_headroom={"cpu": 1000, "memory": 1024},
        containers=[
            {"name": "app", "cgroup_writable": True},
            {"name": "sidecar", "cgroup_writable": True},
        ],
        qos=QOS_BURSTABLE,
        probes={"startup_passed": True, "ready": True},
        restart_policy={
            "pod": "Always",
            "resize": {
                "app": {"cpu": "NotRequired", "memory": "NotRequired"},
                "sidecar": {"cpu": "NotRequired", "memory": "NotRequired"},
            },
        },
        update_receipts=[
            {"receipt_id": "r1", "container": "app",
             "success": True, "error": None},
            {"receipt_id": "r2", "container": "sidecar",
             "success": True, "error": None},
        ],
    )


class TestAllContainersSucceed(unittest.TestCase):
    def test_pod_completed_with_target_limits(self):
        result = validate_resize(**base_kwargs())
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertTrue(result["pod_completed"])
        self.assertEqual(result["actual_limits"]["app"],
                         {"cpu": 400, "memory": 512})
        self.assertEqual(result["actual_limits"]["sidecar"],
                         {"cpu": 200, "memory": 256})
        self.assertEqual(result["failed_containers"], [])
        self.assertFalse(result["rollback"]["rolled_back"])
        self.assertIsNone(result["reason"])

    def test_qos_recomputed_from_actual_limits(self):
        kwargs = base_kwargs()
        # requests (600/768) == sum of target limits -> Guaranteed
        result = validate_resize(**kwargs)
        self.assertEqual(result["qos"], QOS_GUARANTEED)


class TestPartialSuccessRollsBack(unittest.TestCase):
    def test_one_container_fails_everything_rolls_back(self):
        kwargs = base_kwargs()
        kwargs["update_receipts"] = [
            {"receipt_id": "r1", "container": "app",
             "success": True, "error": None},
            {"receipt_id": "r2", "container": "sidecar",
             "success": False, "error": "device or resource busy"},
        ]
        result = validate_resize(**kwargs)
        # The Pod must NOT be reported complete.
        self.assertEqual(result["status"], STATUS_ROLLED_BACK)
        self.assertFalse(result["pod_completed"])
        # Actual limits stay at the unified rollback point (old values),
        # even though the Pod status already shows the new requests and
        # the "app" cgroup update succeeded before the failure.
        self.assertEqual(result["actual_limits"]["app"],
                         {"cpu": 200, "memory": 256})
        self.assertEqual(result["actual_limits"]["sidecar"],
                         {"cpu": 100, "memory": 128})
        self.assertEqual(result["failed_containers"], ["sidecar"])
        rollback = result["rollback"]
        self.assertTrue(rollback["rolled_back"])
        self.assertEqual(rollback["rollback_point"], "original_limits")
        self.assertEqual(rollback["containers"], ["app"])
        self.assertIn("actual_limits", rollback["fields"])
        self.assertIn("sidecar", result["reason"])

    def test_unwritable_cgroup_counts_as_failure(self):
        kwargs = base_kwargs()
        kwargs["containers"][1]["cgroup_writable"] = False
        result = validate_resize(**kwargs)
        self.assertEqual(result["status"], STATUS_ROLLED_BACK)
        self.assertEqual(result["failed_containers"], ["sidecar"])
        self.assertTrue(result["rollback"]["rolled_back"])

    def test_missing_receipt_counts_as_failure(self):
        kwargs = base_kwargs()
        kwargs["update_receipts"] = [
            {"receipt_id": "r1", "container": "app",
             "success": True, "error": None},
        ]
        result = validate_resize(**kwargs)
        self.assertEqual(result["status"], STATUS_ROLLED_BACK)
        self.assertEqual(result["failed_containers"], ["sidecar"])


class TestInsufficientHeadroom(unittest.TestCase):
    def test_growth_beyond_headroom_is_blocked(self):
        kwargs = base_kwargs()
        kwargs["node_headroom"] = {"cpu": 100, "memory": 1024}
        result = validate_resize(**kwargs)
        self.assertEqual(result["status"], STATUS_BLOCKED)
        self.assertFalse(result["pod_completed"])
        self.assertIn("insufficient node headroom", result["reason"])
        # Nothing was applied, so nothing to roll back.
        self.assertFalse(result["rollback"]["rolled_back"])
        self.assertEqual(result["actual_limits"]["app"],
                         {"cpu": 200, "memory": 256})

    def test_shrink_does_not_consume_headroom(self):
        kwargs = base_kwargs()
        kwargs["target_limits"] = {
            "app": {"cpu": 100, "memory": 128},
            "sidecar": {"cpu": 50, "memory": 64},
        }
        kwargs["node_headroom"] = {"cpu": 0, "memory": 0}
        result = validate_resize(**kwargs)
        self.assertEqual(result["status"], STATUS_COMPLETED)


class TestRestartRequiredShrink(unittest.TestCase):
    def shrink_kwargs(self, allow_restart=False):
        kwargs = base_kwargs()
        kwargs["target_limits"] = {
            "app": {"cpu": 100, "memory": 128},
            "sidecar": {"cpu": 200, "memory": 256},
        }
        kwargs["restart_policy"]["resize"]["app"]["memory"] = \
            "RestartContainer"
        kwargs["allow_restart"] = allow_restart
        return kwargs

    def test_restart_shrink_requires_explicit_request(self):
        result = validate_resize(**self.shrink_kwargs(allow_restart=False))
        self.assertEqual(result["status"], STATUS_BLOCKED)
        self.assertFalse(result["pod_completed"])
        self.assertEqual(result["failed_containers"], ["app"])
        self.assertIn("requires container restart", result["reason"])
        self.assertIn("not explicitly requested", result["reason"])
        # Limits untouched.
        self.assertEqual(result["actual_limits"]["app"],
                         {"cpu": 200, "memory": 256})

    def test_restart_shrink_proceeds_when_explicitly_allowed(self):
        result = validate_resize(**self.shrink_kwargs(allow_restart=True))
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(result["actual_limits"]["app"],
                         {"cpu": 100, "memory": 128})


class TestDuplicateUpdates(unittest.TestCase):
    def test_duplicate_receipt_applied_once(self):
        kwargs = base_kwargs()
        kwargs["update_receipts"] = [
            {"receipt_id": "r1", "container": "app",
             "success": True, "error": None},
            {"receipt_id": "r1", "container": "app",
             "success": True, "error": None},  # duplicate delivery
            {"receipt_id": "r2", "container": "sidecar",
             "success": True, "error": None},
        ]
        result = validate_resize(**kwargs)
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertTrue(result["pod_completed"])
        self.assertEqual(result["duplicate_receipts_ignored"], ["r1"])
        # Applied exactly once: the target value, not doubled.
        self.assertEqual(result["actual_limits"]["app"],
                         {"cpu": 400, "memory": 512})

    def test_duplicate_failure_receipt_does_not_double_count(self):
        kwargs = base_kwargs()
        kwargs["update_receipts"] = [
            {"receipt_id": "r1", "container": "app",
             "success": True, "error": None},
            {"receipt_id": "r2", "container": "sidecar",
             "success": False, "error": "write failed"},
            {"receipt_id": "r2", "container": "sidecar",
             "success": False, "error": "write failed"},
        ]
        result = validate_resize(**kwargs)
        self.assertEqual(result["status"], STATUS_ROLLED_BACK)
        self.assertEqual(result["failed_containers"], ["sidecar"])

    def test_repeated_resize_is_idempotent(self):
        # Re-running the same completed resize yields the same state.
        kwargs = base_kwargs()
        first = validate_resize(**kwargs)
        second = validate_resize(**kwargs)
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(first["actual_limits"], second["actual_limits"])


class TestProbeGating(unittest.TestCase):
    def test_failing_readiness_defers_resize(self):
        kwargs = base_kwargs()
        kwargs["probes"]["ready"] = False
        result = validate_resize(**kwargs)
        self.assertEqual(result["status"], STATUS_BLOCKED)
        self.assertIn("readiness", result["reason"])


class TestQoSComputation(unittest.TestCase):
    def test_guaranteed(self):
        qos = compute_qos({"cpu": 300, "memory": 384},
                          {"a": {"cpu": 200, "memory": 256},
                           "b": {"cpu": 100, "memory": 128}})
        self.assertEqual(qos, QOS_GUARANTEED)

    def test_burstable(self):
        qos = compute_qos({"cpu": 300, "memory": 384},
                          {"a": {"cpu": 200, "memory": 256},
                           "b": {"cpu": 50, "memory": 128}})
        self.assertEqual(qos, QOS_BURSTABLE)

    def test_best_effort(self):
        qos = compute_qos({"cpu": 0, "memory": 0}, {"a": {}})
        self.assertEqual(qos, QOS_BEST_EFFORT)


class TestInputValidation(unittest.TestCase):
    def test_mismatched_containers_rejected(self):
        kwargs = base_kwargs()
        del kwargs["target_limits"]["sidecar"]
        with self.assertRaises(ResizeInputError):
            validate_resize(**kwargs)

    def test_negative_quantity_rejected(self):
        kwargs = base_kwargs()
        kwargs["pod_request"]["cpu"] = -1
        with self.assertRaises(ResizeInputError):
            validate_resize(**kwargs)

    def test_missing_cgroup_handle_rejected(self):
        kwargs = base_kwargs()
        kwargs["containers"] = [{"name": "app", "cgroup_writable": True}]
        with self.assertRaises(ResizeInputError):
            validate_resize(**kwargs)


if __name__ == "__main__":
    unittest.main()
