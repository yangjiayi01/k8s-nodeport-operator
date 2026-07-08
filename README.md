# k8s-port-allocator

基于 [kopf](https://github.com/nolar/kopf) 的 Kubernetes Operator，自动为带有特定 annotation 的 Deployment 分配**集群内全局唯一**的双端口（`hostPort` + `containerPort`），并在 Deployment 删除时自动释放端口。

## 项目简介

Operator 通过监听带有 `portmanager.example.com/managed: "true"` 注解的 Deployment，从预定义端口池（默认 32000~33000）中随机选取 2 个未占用端口，注入到目标容器的 `ports` 与 `env` 中，并以 `PortAllocation` CR（集群级资源）持久化分配记录，保证集群内端口全局唯一。

### 架构图

```
                         ┌───────────────────────────────────────────────┐
                         │           k8s-port-allocator Operator         │
                         │   (kopf controller, namespace=port-manager)    │
                         │                                               │
   用户创建 Deployment     │   on_create/on_resume                        │
   (带 managed 注解) ───► │     ├─ 幂等检查 (allocated-ports 注解)         │
                         │     ├─ allocate_ports (端口池随机选取)         │
                         │     ├─ 创建 PortAllocation CR (409 重试)       │
                         │     └─ patch Deployment (注入 ports/env/注解)  │
                         │                                               │
   用户删除 Deployment ──►│   on_delete                                   │
                         │     └─ 删除关联 PortAllocation CR (释放端口)    │
                         │                                               │
   定时对账 RECONCILE ───►│   timer (每 300s)                             │
                         │     ├─ 清理孤儿 PortAllocation CR              │
                         │     └─ 补建缺失的 PortAllocation CR            │
                         └───────────────┬───────────────────────────────┘
                                         │
              ┌──────────────────────────┴──────────────────────────┐
              ▼                                                        ▼
   ┌─────────────────────┐                              ┌──────────────────────┐
   │   Deployment (apps) │                              │ PortAllocation CR    │
   │  - 注入 hostPort    │ ◄────── 全局唯一性校验 ────► │ (集群级 scope)       │
   │  - 注入 env         │                              │ spec.port / status   │
   │  - annotation 记录  │                              └──────────────────────┘
   └─────────────────────┘
```

## 前置条件

| 项目 | 版本要求 |
| --- | --- |
| Kubernetes | ≥ 1.22（需支持 `apiextensions.k8s.io/v1` CRD 与 `subresources.status`） |
| kubectl | 与集群版本匹配 |
| Docker | ≥ 20.10（构建镜像用） |
| Python（本地调试可选） | ≥ 3.11 |

## 快速部署

### 1. 部署 CRD / RBAC / Controller

```bash
# 在项目根目录执行
kubectl apply -k config/
```

### 2. 构建 Controller 镜像

```bash
cd k8s-port-allocator
docker build -t k8s-port-allocator:latest .

# 若推送到私有仓库：
# docker tag k8s-port-allocator:latest <registry>/k8s-port-allocator:latest
# docker push <registry>/k8s-port-allocator:latest
# 然后修改 config/controller-deployment.yaml 中的 image 字段后重新 apply
```

### 3. 验证 Controller 运行

```bash
kubectl -n port-manager get pods
kubectl -n port-manager logs -l app=port-allocator -f
```

## 用户使用方式

只需在 Deployment 上添加一个 annotation 即可触发端口分配：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  annotations:
    portmanager.example.com/managed: "true"   # 关键注解
spec:
  template:
    spec:
      containers:
        - name: business          # 目标容器名（可用 CONTAINER_NAME 配置）
          image: nginx:1.25
          ports: []               # 留空，Operator 自动注入
          env: []                 # 留空，Operator 自动注入
```

Operator 会自动注入：

```yaml
ports:
  - name: biz-port-0
    containerPort: 32145
    hostPort: 32145
    protocol: TCP
  - name: biz-port-1
    containerPort: 32890
    hostPort: 32890
    protocol: TCP
env:
  - name: BIZ_PORT_0
    value: "32145"
  - name: BIZ_PORT_1
    value: "32890"
```

并写入 annotation：

```yaml
metadata:
  annotations:
    portmanager.example.com/allocated-ports: "32145,32890"
```

完整示例见 [examples/example-deployment.yaml](examples/example-deployment.yaml)。

## 配置说明

所有配置通过 Controller Deployment 的环境变量传入：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT_RANGE_START` | `32000` | 端口池起始 |
| `PORT_RANGE_END` | `33000` | 端口池结束 |
| `MANAGED_ANNOTATION` | `portmanager.example.com/managed` | 触发管理的 annotation key |
| `PORTS_ANNOTATION` | `portmanager.example.com/allocated-ports` | 记录已分配端口的 annotation key |
| `CONTAINER_NAME` | `business` | 端口注入目标容器名（找不到则取第一个容器） |
| `NUM_PORTS` | `2` | 每个 Deployment 分配的端口数量 |
| `RECONCILE_INTERVAL` | `300` | 定时对账间隔（秒） |
| `KOPF_LOG_LEVEL` | `INFO` | kopf 日志级别 |

## 验证方法

### 查看分配的端口

```bash
# 列出所有 PortAllocation CR
kubectl get portallocations

# 查看 Deployment 注入的端口注解
kubectl get deploy example-app -o jsonpath='{.metadata.annotations}'

# 查看容器实际注入的端口
kubectl get deploy example-app -o jsonpath='{.spec.template.spec.containers[0].ports}'
```

### 验证端口释放

```bash
# 删除测试 Deployment
kubectl delete -f examples/example-deployment.yaml

# PortAllocation CR 应自动清空
kubectl get portallocations   # 应为空
```

### 查看日志

```bash
kubectl -n port-manager logs -l app=port-allocator -f
```

## 故障排查指南

### Controller 未启动

```bash
kubectl -n port-manager describe deploy port-allocator-controller
kubectl -n port-manager logs -l app=port-allocator
```

常见原因：

- **镜像拉取失败**：本地镜像未构建或未推送，检查 `image` 字段。
- **RBAC 权限不足**：检查 `kubectl auth can-i --list -n port-manager`。
- **CRD 未创建**：确认 `kubectl get crd portallocations.portmanager.example.com` 存在。

### 端口未被分配

1. 确认 Deployment 注解：`portmanager.example.com/managed: "true"`（注意是字符串 `"true"`）。
2. 确认目标容器名与 `CONTAINER_NAME` 一致，否则 Operator 回退到第一个容器。
3. 查看 Controller 日志中是否有报错。

### 端口冲突 / 分配失败

- 日志出现 `端口池可用端口不足`：扩大 `PORT_RANGE_END - PORT_RANGE_START` 范围。
- 日志出现 `409 Conflict`：属正常竞态重试，Operator 会自动选择其他端口。
- `hostPort` 被节点上其他进程占用：需检查节点端口使用情况并清理。

### 孤儿端口清理

若 Controller 异常退出导致 PortAllocation CR 残留，可手动执行对账：

```bash
# 预演
kubectl -n port-manager exec -it <controller-pod> -- \
  python scripts/cleanup-orphan-ports.py --dry-run

# 实际清理
kubectl -n port-manager exec -it <controller-pod> -- \
  python scripts/cleanup-orphan-ports.py
```

或在本地（拥有 kubeconfig 的机器上）直接执行：

```bash
pip install kubernetes
python scripts/cleanup-orphan-ports.py --dry-run
```

## 注意事项

### hostPort 限制

- `hostPort` 直接占用节点宿主机端口，**不同节点间可复用同一端口**，但同一节点内同端口不可重复。
- 本 Operator 保证**集群内全局唯一**，比 `hostPort` 的本机唯一性更严格，避免节点迁移时冲突。
- 使用 `hostPort` 的 Pod 受节点端口范围限制，可能与 NodePort/其他系统服务冲突，请合理规划端口池范围。

### 端口池大小规划

- 默认端口池 32000~33000（共 1001 个端口），每个 Deployment 占用 2 个端口，理论可服务约 500 个 Deployment。
- 端口池耗尽时 Operator 抛出 `PermanentError` 并停止重试，需扩容端口范围后手动重新触发。
- 建议预留 20% 余量以应对并发分配。

### 跨作用域 ownerReference

`PortAllocation` 为集群级资源，`Deployment` 为命名空间级资源。Kubernetes GC 对跨作用域 `ownerReference` 支持有限，因此 Operator 在 `on_delete` 中**显式清理**对应 CR，同时通过定时对账兜底，不依赖 GC。

### 幂等性

- Controller 重启后 `on_resume` 会重放事件，通过 `allocated-ports` 注解判断是否已分配，避免重复分配。
- Deployment patch 使用 strategic merge patch，保留用户已有的 `ports`/`env` 配置。

## 目录结构

```
k8s-port-allocator/
├── README.md
├── Dockerfile
├── requirements.txt
├── controller.py              # Operator 核心逻辑
├── config/
│   ├── crd.yaml               # PortAllocation CRD 定义
│   ├── rbac.yaml              # ServiceAccount + ClusterRole + ClusterRoleBinding
│   ├── namespace.yaml         # Namespace 定义
│   ├── controller-deployment.yaml  # Controller 自身的 Deployment
│   └── kustomization.yaml     # Kustomize 聚合配置
├── examples/
│   └── example-deployment.yaml  # 用户使用示例
└── scripts/
    └── cleanup-orphan-ports.py  # 孤儿端口清理脚本
```

## 部署验证命令

```bash
# 1. 部署 Operator
kubectl apply -k config/

# 2. 验证 Controller 运行
kubectl -n port-manager get pods
kubectl -n port-manager logs -l app=port-allocator -f

# 3. 创建测试 Deployment
kubectl apply -f examples/example-deployment.yaml

# 4. 查看分配的端口
kubectl get portallocations
kubectl get deploy example-app -o jsonpath='{.metadata.annotations}'

# 5. 删除测试，验证端口释放
kubectl delete -f examples/example-deployment.yaml
kubectl get portallocations   # 应该为空
```
