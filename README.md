# kubernetes-inplace-pod-resize-validator

验证 Pod 原地 resize、cgroup 和回滚状态的纯本地模拟器。

**本项目不连接 Kubernetes**——不访问 API server、不读取节点 cgroup、
不需要 kubeconfig。所有输入（Pod 请求、现有限制、目标限制、节点余量、
容器 cgroup、QoS、探针、重启策略、更新回执）都以普通 Python 数据结构
传入，模块据此计算"实际"资源状态。

## 背景

原地 resize 时，API server 可能已经在 Pod status 里显示新的 request，
但节点上各容器的 cgroup 限制是逐个更新的。本模块模拟 kubelet 侧的
判定逻辑，并强制三条安全规则：

1. **所有容器成功前，Pod 不算完成**：只有每个容器的 cgroup 更新都
   成功，才返回 `Completed`。
2. **需要重启的缩容必须明确要求**：缩容且容器 resize 策略为
   `RestartContainer` 时，未显式设置 `allow_restart=True` 一律阻塞。
3. **部分失败回滚到统一点**：任一容器失败时，已更新的容器全部回滚
   到原始限制，Pod 不会停留在混合状态。

## 输出字段

`validate_resize(...)` 返回 dict：

- `status` / `pod_completed`：`Completed` / `RolledBack` / `Blocked`
- `actual_limits`：各容器**实际生效**的限制（失败时为旧值）
- `failed_containers`：cgroup 更新失败的容器列表
- `qos`：按实际限制重新计算的 QoS（Guaranteed/Burstable/BestEffort）
- `rollback`：`rolled_back`、`rollback_point`、`fields`、`containers`
- `reason`：不可继续的原因（余量不足、未批准的重启缩容、探针未就绪等）

重复的更新回执按 `receipt_id` 去重，只应用一次（幂等）。

## 运行测试

```bash
python -m unittest
```

测试覆盖：部分成功回滚、节点余量不足、重启要求、重复更新幂等，
以及 QoS 重算、探针门控和输入校验。

## 兼容性

Python 3.8.2+，仅标准库。
