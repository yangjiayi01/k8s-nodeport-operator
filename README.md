<div align="center">

# k8s-port-allocator

**A Kubernetes Operator that automatically allocates cluster-wide unique `hostPort`s for your Deployments.**

[![Kubernetes](https://img.shields.io/badge/kubernetes-%E2%89%A5%201.22-blue?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Python](https://img.shields.io/badge/python-3.11-blue?logo=python&logoColor=white)](https://www.python.org/)
[![kopf](https://img.shields.io/badge/kopf-1.37%2B-orange?logo=python&logoColor=white)](https://github.com/nolar/kopf)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-4%2F4%20passed-brightgreen)](TEST_REPORT.md)
[![Platform](https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64%20%7C%20linux%2Farmv7-lightgrey)](#跨平台镜像)

[Quick Start](#quick-start) · [Documentation](#目录) · [Examples](examples/) · [Test Report](TEST_REPORT.md)

</div>

---

## 目录

- [简介](#简介)
- [功能特性](#功能特性)
- [架构](#架构)
- [Quick Start](#quick-start)
- [使用方式](#使用方式)
- [配置项](#配置项)
- [验证方法](#验证方法)
- [故障排查](#故障排查)
- [注意事项](#注意事项)
- [跨平台镜像](#跨平台镜像)
- [项目结构](#项目结构)
- [开发](#开发)
- [测试报告](#测试报告)
- [贡献](#贡献)
- [许可证](#许可证)
- [致谢](#致谢)

---

## 简介

**k8s-port-allocator** 是一个基于 [kopf](https://github.com/nolar/kopf) 框架开发的 Kubernetes Operator，用于自动为带有特定 annotation 的 Deployment 分配**集群内全局唯一**的双端口（`hostPort` + `containerPort`），并在 Deployment 删除时自动释放端口。

### 解决什么问题？

在 Kubernetes 中，使用 `hostPort` 暴露服务时面临两个痛点：

1. **端口冲突**：多个 Deployment 手动分配 `hostPort` 极易冲突，导致 Pod 启动失败。
2. **端口泄漏**：Deployment 删除后 `hostPort` 未释放，新 Deployment 无法复用。

本 Operator 通过维护一个中心化的端口池（`PortAllocation` CRD），自动完成端口的分配、注入、释放，保证集群内端口全局唯一且可追溯。

---

## 功能特性

- ✨ **零侵入接入**：用户只需给 Deployment 打一个 annotation，Operator 自动完成端口分配与注入
- 🔒 **集群内全局唯一**：通过集群级 `PortAllocation` CR 持久化分配记录，端口绝无重复
- 🔁 **全生命周期管理**：自动响应 Deployment 的创建/删除/重启事件
- 🛡️ **幂等性保证**：Controller 重启不会重复分配端口，annotation 是幂等性锚点
- 🧹 **孤儿端口清理**：定时对账（timer handler）自动清理残留的孤儿 CR
- ⚡ **竞态处理**：CR 创建冲突（409）自动重试选择其他端口
- 🎯 **Strategic Merge Patch**：注入 ports/env 时保留用户已有配置
- 📊 **可观测**：完整的中文日志输出，每个操作步骤都可追踪
- 🐳 **跨平台镜像**：支持 linux/amd64、linux/arm64、linux/arm/v7

---

## 架构

```
                         ┌───────────────────────────────────────────────┐
                         │           k8s-port-allocator Operator         │
                         │   (kopf controller, namespace=port-manager)   │
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

### 核心组件

| 组件 | 作用 |
| --- | --- |
| `controller.py` | Operator 核心逻辑，kopf handlers 实现 |
| `PortAllocation` CRD | 集群级 CRD，持久化端口分配记录 |
| `ClusterRole` + `SA` | RBAC 权限模型，最小权限原则 |
| `port-allocator-controller` | 部署在 `port-manager` namespace 的 Deployment |

---

## Quick Start

### 前置条件

| 项 | 版本 | 说明 |
| --- | --- | --- |
| Kubernetes | ≥ 1.22 | 需支持 `apiextensions.k8s.io/v1` CRD 与 `subresources.status` |
| kubectl | 与集群版本匹配 | 部署与验证用 |
| Docker | ≥ 20.10 | 构建 Controller 镜像 |
| Python | ≥ 3.11 | 仅本地调试时需要 |

### 三步部署

```bash
# 1️⃣ 克隆仓库
git clone https://github.com/yangjiayi01/k8s-nodeport-opertaort.git
cd k8s-nodeport-opertaort

# 2️⃣ 构建 Controller 镜像
docker build -t k8s-port-allocator:latest .

# 3️⃣ 一键部署 Operator 到集群
kubectl apply -k config/
```

### 验证部署

```bash
# Controller Pod 应为 Running
kubectl -n port-manager get pods

# CRD 应已注册
kubectl get crd portallocations.portmanager.example.com
```

> 📌 **镜像说明**：若集群需要从私有仓库拉取镜像，请修改 [config/controller-deployment.yaml](config/controller-deployment.yaml) 中的 `image` 字段后重新 `kubectl apply`。

---

## 使用方式

### 1. 给 Deployment 打上 managed annotation

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  annotations:
    portmanager.example.com/managed: "true"   # 关键注解，触发端口分配
spec:
  template:
    spec:
      containers:
        - name: business          # 目标容器名（可用 CONTAINER_NAME 配置）
          image: nginx:1.25
          ports: []               # 留空，Operator 自动注入
          env: []                 # 留空，Operator 自动注入
```

### 2. Operator 自动注入

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

metadata:
  annotations:
    portmanager.example.com/allocated-ports: "32145,32890"
```

### 3. 查看 / 删除

```bash
# 查看所有端口分配
kubectl get portallocations

# 删除 Deployment 后端口自动释放
kubectl delete deploy my-app
kubectl get portallocations   # 应为空
```

完整示例见 [examples/example-deployment.yaml](examples/example-deployment.yaml)。

---

## 配置项

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

修改方式：编辑 [config/controller-deployment.yaml](config/controller-deployment.yaml) 中 `env` 段后 `kubectl apply -k config/`。

---

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

---

## 故障排查

<details>
<summary><b>Controller 未启动</b></summary>

```bash
kubectl -n port-manager describe deploy port-allocator-controller
kubectl -n port-manager logs -l app=port-allocator
```

常见原因：

- **镜像拉取失败**：本地镜像未构建或未推送，检查 `image` 字段。
- **RBAC 权限不足**：检查 `kubectl auth can-i --list -n port-manager`。
- **CRD 未创建**：确认 `kubectl get crd portallocations.portmanager.example.com` 存在。

</details>

<details>
<summary><b>端口未被分配</b></summary>

1. 确认 Deployment 注解：`portmanager.example.com/managed: "true"`（注意是字符串 `"true"`）。
2. 确认目标容器名与 `CONTAINER_NAME` 一致，否则 Operator 回退到第一个容器。
3. 查看 Controller 日志中是否有报错。

</details>

<details>
<summary><b>端口冲突 / 分配失败</b></summary>

- 日志出现 `端口池可用端口不足`：扩大 `PORT_RANGE_END - PORT_RANGE_START` 范围。
- 日志出现 `409 Conflict`：属正常竞态重试，Operator 会自动选择其他端口。
- `hostPort` 被节点上其他进程占用：需检查节点端口使用情况并清理。

</details>

<details>
<summary><b>孤儿端口清理</b></summary>

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

</details>

---

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

### Pod 重启端口稳定性

端口分配在 **Deployment 级别**（非 Pod 级别）。只要 Deployment 对象不删除，端口就稳定不变：

| 场景 | 端口是否变化 |
| --- | --- |
| Pod crash 自动重启 | ❌ 不变 |
| `kubectl delete pod` 重建 | ❌ 不变 |
| 副本数 1→0→1 | ❌ 不变 |
| 副本数 1→N（多副本） | ❌ 不变（共享同组端口） |
| Deployment 滚动更新 | ❌ 不变 |
| Controller Pod 重启 | ❌ 不变 |
| **Deployment 删除后重建** | ✅ **重新分配** |

> ⚠️ **多副本陷阱**：同一 Deployment 的多副本共享同组 `hostPort`，若调度到同一节点会冲突启动失败。多副本场景需配合 `podAntiAffinity` 让副本调度到不同节点。

---

## 跨平台镜像

支持通过 `docker buildx` 构建多架构镜像：

| 架构 | 构建命令 |
| --- | --- |
| linux/amd64 | `docker buildx build --platform linux/amd64 -t k8s-port-allocator:latest --load .` |
| linux/arm64 | `docker buildx build --platform linux/arm64 -t k8s-port-allocator:arm64 --load .` |
| linux/arm/v7 | `docker buildx build --platform linux/arm/v7 -t k8s-port-allocator:armv7 --load .` |

镜像大小约 **59 MB**（python:3.11-slim + 纯 Python wheel 依赖）。

---

## 项目结构

```
k8s-port-allocator/
├── README.md                          # 本文档
├── Dockerfile                         # 镜像构建脚本
├── requirements.txt                   # Python 依赖
├── controller.py                      # Operator 核心逻辑（kopf handlers）
├── config/
│   ├── crd.yaml                       # PortAllocation CRD 定义（集群级）
│   ├── rbac.yaml                      # ServiceAccount + ClusterRole + Binding
│   ├── namespace.yaml                 # Namespace 定义
│   ├── controller-deployment.yaml    # Controller 自身的 Deployment
│   └── kustomization.yaml             # Kustomize 聚合配置
├── examples/
│   └── example-deployment.yaml        # 用户使用示例
└── scripts/
    └── cleanup-orphan-ports.py        # 孤儿端口清理脚本（手动执行）
```

---

## 开发

### 本地运行（不构建镜像）

```bash
pip install -r requirements.txt
kopf run --standalone --liveness=http://0.0.0.0:8080/healthz controller.py
```

> 需要 `~/.kube/config` 指向目标集群。

### 重新构建镜像

```bash
docker build -t k8s-port-allocator:latest .
kubectl rollout restart -n port-manager deploy/port-allocator-controller
```

### 部署 K8s 1.26 集群兼容性

本项目与 K8s 1.22+ 完全兼容，包括 1.26。代码无需改动，仅在严格对齐 `kubernetes` Python client 版本时可选：

```diff
# requirements.txt（可选，client/server 版本严格对齐时）
- kubernetes>=29.0
+ kubernetes>=26.0,<27.0
```

---

## 测试报告

完整测试报告见 [TEST_REPORT.md](TEST_REPORT.md)。

| # | 测试项 | 结果 | 关键验证点 |
| --- | --- | --- | --- |
| 1 | 端口分配 | ✅ PASS | 自动分配 2 个端口，CR + annotation + ports + env 全部正确注入 |
| 2 | 端口释放 | ✅ PASS | Deployment 删除后 2 个 CR 自动清理 |
| 3 | 幂等性 | ✅ PASS | Controller 重启后端口完全一致 |
| 4 | 孤儿清理 | ✅ PASS | 对账机制识别并清理 owner 不存在的 CR |

测试环境：k3s v1.30.0+k3s1 单节点容器集群。

---

## 贡献

欢迎贡献！请按以下流程：

1. 🍴 Fork 本仓库
2. 🌿 创建特性分支：`git checkout -b feature/your-feature`
3. 💾 提交变更：`git commit -m "feat: add your feature"`
4. 📤 推送分支：`git push origin feature/your-feature`
5. 🔀 发起 Pull Request

### 贡献规范

- 遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范提交
- 代码需有中文注释，关键逻辑需有 docstring
- 新增功能需附带测试用例
- 修改 CRD 字段时需同步更新 [config/crd.yaml](config/crd.yaml) 与 [controller.py](controller.py)

---

## 许可证

本项目基于 [MIT License](LICENSE) 开源。

 Copyright (c) 2026 yangjiayi01

---

## 致谢

- [kopf](https://github.com/nolar/kopf) — Kubernetes Operator Python Framework
- [kubernetes-client/python](https://github.com/kubernetes-client/python) — Official Python client for Kubernetes
- [Rancher k3s](https://k3s.io/) — 轻量级 Kubernetes 发行版，用于本项目测试

---

<div align="center">

**如果这个项目对你有帮助，欢迎 ⭐ Star 支持！**

</div>
