#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
k8s-port-allocator Operator 核心控制器。

功能概览：
    1. 监听带有特定 annotation 的 Deployment（portmanager.example.com/managed: "true"）。
    2. 在创建/恢复时为每个 Deployment 分配 NUM_PORTS 个集群内全局唯一的端口
       （hostPort == containerPort），并将分配信息写入 PortAllocation CR。
    3. 在 Deployment 删除时自动释放对应端口（删除 PortAllocation CR）。
    4. 通过定时任务对账：清理孤儿端口、补建缺失的 PortAllocation CR。

设计要点：
    * 幂等：on_resume 重放事件不会重复分配端口，已有 allocated-ports 注解则跳过。
    * 竞态：创建 PortAllocation CR 遇 409 Conflict 时自动重新选择端口。
    * 网络/API 异常用 kopf.TemporaryError 触发自动重试；端口耗尽用 kopf.PermanentError。
"""

import os
import random
import time
import logging
from datetime import datetime, timezone
from typing import List, Set, Dict, Any, Optional

import kopf
import kubernetes
from kubernetes import client, config
from kubernetes.client import ApiException

# =============================================================================
# 配置项：全部通过环境变量读取，提供合理默认值
# =============================================================================

PORT_RANGE_START = int(os.environ.get("PORT_RANGE_START", "32000"))
PORT_RANGE_END = int(os.environ.get("PORT_RANGE_END", "33000"))
MANAGED_ANNOTATION = os.environ.get(
    "MANAGED_ANNOTATION", "portmanager.example.com/managed"
)
PORTS_ANNOTATION = os.environ.get(
    "PORTS_ANNOTATION", "portmanager.example.com/allocated-ports"
)
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "business")
NUM_PORTS = int(os.environ.get("NUM_PORTS", "2"))
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "300"))

# CRD 资源定义常量
CRD_GROUP = "portmanager.example.com"
CRD_VERSION = "v1"
CRD_PLURAL = "portallocations"
CRD_KIND = "PortAllocation"

# 对账去抖：用于 timer 多次触发时只让一次真正执行全量扫描
_LAST_RECONCILE_TS: float = 0.0


# =============================================================================
# Kubernetes 客户端初始化
# =============================================================================

def _build_clients() -> tuple:
    """初始化并返回 (custom_api, apps_api, core_api) 三个客户端对象。

    优先使用集群内配置，失败则回退到 kubeconfig（本地调试用）。
    """
    try:
        config.load_incluster_config()
    except kubernetes.config.ConfigException:
        # 本地开发场景：从 ~/.kube/config 读取
        config.load_kubeconfig()

    custom_api = client.CustomObjectsApi()
    apps_api = client.AppsV1Api()
    core_api = client.CoreV1Api()
    return custom_api, apps_api, core_api


# 模块级全局客户端（kopf 启动时模块加载，in-cluster 配置此时已可用）
# 用惰性初始化包装，避免在 import 阶段就连接集群失败导致模块不可导入
_CUSTOM_API: Optional[client.CustomObjectsApi] = None
_APPS_API: Optional[client.AppsV1Api] = None
_CORE_API: Optional[client.CoreV1Api] = None


def custom_api() -> client.CustomObjectsApi:
    global _CUSTOM_API
    if _CUSTOM_API is None:
        _CUSTOM_API, _APPS_API_, _CORE_API_ = _build_clients()
    return _CUSTOM_API


def apps_api() -> client.AppsV1Api:
    global _APPS_API
    if _APPS_API is None:
        _CUSTOM_API_, _APPS_API, _CORE_API_ = _build_clients()
    return _APPS_API


def core_api() -> client.CoreV1Api:
    global _CORE_API
    if _CORE_API is None:
        _CUSTOM_API_, _APPS_API_, _CORE_API = _build_clients()
    return _CORE_API


# =============================================================================
# 端口分配核心逻辑
# =============================================================================

def get_allocated_ports(api: client.CustomObjectsApi) -> Set[int]:
    """列出所有 PortAllocation CR，返回 status.phase == "Allocated" 的端口号集合。

    用于计算端口池中的已占用端口，保证集群内全局唯一。
    """
    allocated: Set[int] = set()
    try:
        # 集群级别资源，使用 list_cluster_custom_object
        result = api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL
        )
    except ApiException as e:
        # API 临时故障交给 kopf 重试
        raise kopf.TemporaryError(
            f"列出 PortAllocation CR 失败: status={e.status}, body={e.body}", delay=10
        )

    for item in result.get("items", []):
        status = item.get("status") or {}
        phase = status.get("phase")
        port = (item.get("spec") or {}).get("port")
        if phase == "Allocated" and port is not None:
            allocated.add(int(port))
    return allocated


def allocate_ports(api: client.CustomObjectsApi, count: int) -> List[int]:
    """从可用端口池中随机挑选 count 个端口（排序返回）。

    若可用端口不足则抛出 kopf.PermanentError（端口池耗尽，重试无意义）。
    """
    allocated = get_allocated_ports(api)
    full_pool = set(range(PORT_RANGE_START, PORT_RANGE_END + 1))
    available = list(full_pool - allocated)

    if len(available) < count:
        raise kopf.PermanentError(
            f"端口池可用端口不足: 需要 {count} 个，实际可用 {len(available)} 个。"
            f"请扩容端口范围 [PORT_RANGE_START..PORT_RANGE_END]。"
        )

    chosen = random.sample(available, count)
    chosen.sort()
    return chosen


def create_port_allocation_cr(
    api: client.CustomObjectsApi,
    port: int,
    deployment_name: str,
    namespace: str,
    deployment_uid: str,
    container_index: int,
) -> bool:
    """创建单个 PortAllocation CR。

    成功返回 True；若因同名 CR 已存在返回 409 Conflict，说明该端口已被抢占，
    返回 False 由调用方重新选择其他端口；其他异常抛出 kopf.TemporaryError。
    """
    cr_name = f"port-{port}"
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": CRD_KIND,
        "metadata": {
            "name": cr_name,
            # 标签便于查询与筛选
            "labels": {
                "app.kubernetes.io/managed-by": "port-allocator",
                f"{CRD_GROUP}/owner-deployment": deployment_name,
                f"{CRD_GROUP}/owner-namespace": namespace,
            },
        },
        "spec": {
            "port": port,
            "ownerDeployment": deployment_name,
            "ownerNamespace": namespace,
            "containerIndex": container_index,
        },
    }

    # 跨作用域 ownerReference（PortAllocation 为集群级，Deployment 为命名空间级）
    # K8s GC 对跨作用域引用支持有限，因此 on_delete 中也会显式清理。
    if deployment_uid:
        body["metadata"]["ownerReferences"] = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": deployment_name,
                "uid": deployment_uid,
                "controller": True,
                "blockOwnerDeletion": True,
            }
        ]

    try:
        api.create_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL, body=body
        )
        return True
    except ApiException as e:
        if e.status == 409:
            # 同名 CR 已存在，端口被抢占，调用方需重试
            return False
        # 其余状态码（403/500 等）视为临时错误
        raise kopf.TemporaryError(
            f"创建 PortAllocation CR '{cr_name}' 失败: status={e.status}, body={e.body}",
            delay=10,
        )


def allocate_and_create_ports(
    api: client.CustomObjectsApi,
    count: int,
    deployment_name: str,
    namespace: str,
    deployment_uid: str,
    logger: logging.Logger,
) -> List[int]:
    """分配端口并创建对应 PortAllocation CR，处理 409 竞态重试。

    返回最终成功分配并落库的端口列表（升序）。
    """
    result_ports: List[int] = []
    max_attempts = 20  # 总重试次数上限，避免极端竞态下死循环

    for attempt in range(1, max_attempts + 1):
        remaining = count - len(result_ports)
        if remaining <= 0:
            break

        # 每轮重新计算可用池（排除本轮已成功分配的端口，避免重复选取）
        allocated = get_allocated_ports(api)
        full_pool = set(range(PORT_RANGE_START, PORT_RANGE_END + 1))
        available = list(full_pool - allocated - set(result_ports))

        if len(available) < remaining:
            # 端口池真的不足，永久错误
            raise kopf.PermanentError(
                f"端口池可用端口不足: 还需 {remaining} 个，实际可用 {len(available)} 个。"
            )

        candidates = sorted(random.sample(available, remaining))
        logger.info(
            f"端口分配第 {attempt} 次尝试: 候选端口 {candidates} (剩余需求 {remaining})"
        )

        for port in candidates:
            ok = create_port_allocation_cr(
                api=api,
                port=port,
                deployment_name=deployment_name,
                namespace=namespace,
                deployment_uid=deployment_uid,
                container_index=candidates.index(port),
            )
            if ok:
                result_ports.append(port)
                logger.info(f"端口 {port} 已成功创建 PortAllocation CR 并分配给 "
                            f"{namespace}/{deployment_name}")
            else:
                logger.warning(
                    f"端口 {port} 创建 PortAllocation CR 时发生 409 Conflict，"
                    f"该端口已被其他进程抢占，将重新选择其他端口"
                )

    if len(result_ports) < count:
        # 重试用尽仍未分配齐，交给 kopf 临时错误稍后重试整个 handler
        raise kopf.TemporaryError(
            f"经过 {max_attempts} 次重试仍未能为 {namespace}/{deployment_name} "
            f"分配足额端口（已分配 {len(result_ports)}/{count}）",
            delay=30,
        )

    result_ports.sort()
    return result_ports


# =============================================================================
# Deployment 补丁：注入 containerPort / hostPort / 环境变量 / 注解
# =============================================================================

def _find_target_container(containers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """根据 CONTAINER_NAME 定位目标容器；找不到则回退到 containers[0]。"""
    for c in containers:
        if c.get("name") == CONTAINER_NAME:
            return c
    if containers:
        return containers[0]
    return None


def patch_deployment_with_ports(
    apps: client.AppsV1Api,
    deployment: Dict[str, Any],
    ports: List[int],
    logger: logging.Logger,
) -> None:
    """使用 strategic merge patch 将端口与环境变量注入目标 Deployment。

    实现策略：读取现有 Deployment，定位目标容器，构造合并后的 ports/env 数组，
    然后用 strategic-merge-patch 更新，避免覆盖用户已有配置。
    """
    name = deployment["metadata"]["name"]
    namespace = deployment["metadata"]["namespace"]
    spec = deployment.get("spec", {})
    pod_spec = spec.get("template", {}).get("spec", {})
    containers = pod_spec.get("containers", [])

    target = _find_target_container(containers)
    if target is None:
        raise kopf.TemporaryError(
            f"{namespace}/{name} 未找到任何容器，无法注入端口", delay=15
        )

    container_name = target.get("name")

    # 1) 构造合并后的 ports 数组（保留用户已有 ports）
    existing_ports = list(target.get("ports") or [])
    # 收集已存在的 containerPort，避免重复注入
    existing_container_ports = {p.get("containerPort") for p in existing_ports}
    for idx, port in enumerate(ports):
        if port in existing_container_ports:
            # 该端口已被注入过，跳过（幂等保护）
            continue
        existing_ports.append(
            {
                "name": f"biz-port-{idx}",
                "containerPort": port,
                "hostPort": port,
                "protocol": "TCP",
            }
        )

    # 2) 构造合并后的 env 数组（保留用户已有 env）
    existing_env = list(target.get("env") or [])
    existing_env_names = {e.get("name") for e in existing_env}
    for idx, port in enumerate(ports):
        env_name = f"BIZ_PORT_{idx}"
        if env_name in existing_env_names:
            # 已存在同名环境变量，更新其值（幂等）
            for e in existing_env:
                if e.get("name") == env_name:
                    e["value"] = str(port)
            continue
        existing_env.append({"name": env_name, "value": str(port)})

    # 3) 构造 strategic merge patch（以容器 name 为合并键）
    # 注解一并写入 metadata.annotations
    patch_body = {
        "metadata": {
            "annotations": {
                PORTS_ANNOTATION: ",".join(str(p) for p in ports),
            }
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container_name,
                            "ports": existing_ports,
                            "env": existing_env,
                        }
                    ]
                }
            }
        },
    }

    logger.info(
        f"正在 patch Deployment {namespace}/{name}: 注入端口 {ports} 到容器 '{container_name}'"
    )

    try:
        # patch_namespaced_deployment 默认使用 strategic-merge-patch+json
        apps.patch_namespaced_deployment(
            name=name,
            namespace=namespace,
            body=patch_body,
        )
    except ApiException as e:
        raise kopf.TemporaryError(
            f"patch Deployment {namespace}/{name} 失败: status={e.status}, body={e.body}",
            delay=15,
        )


def parse_allocated_ports_from_annotation(value: Optional[str]) -> List[int]:
    """从 allocated-ports 注解中解析端口列表（容错处理空值/非法值）。"""
    if not value:
        return []
    ports: List[int] = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit():
            ports.append(int(part))
    return ports


# =============================================================================
# PortAllocation CR 生命周期辅助函数
# =============================================================================

def list_port_allocation_crs(
    api: client.CustomObjectsApi, owner_deployment: str = None, owner_namespace: str = None
) -> List[Dict[str, Any]]:
    """列出 PortAllocation CR，可按 owner 过滤。"""
    try:
        result = api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL
        )
    except ApiException as e:
        raise kopf.TemporaryError(
            f"列出 PortAllocation CR 失败: status={e.status}, body={e.body}", delay=10
        )

    items = result.get("items", [])
    if owner_deployment:
        items = [
            i for i in items
            if (i.get("spec") or {}).get("ownerDeployment") == owner_deployment
            and (owner_namespace is None
                 or (i.get("spec") or {}).get("ownerNamespace") == owner_namespace)
        ]
    return items


def update_port_allocation_status(
    api: client.CustomObjectsApi, cr_name: str, phase: str, logger: logging.Logger
) -> None:
    """更新 PortAllocation CR 的 status.phase 字段。"""
    now = datetime.now(timezone.utc).isoformat()
    body = {
        "status": {
            "phase": phase,
            "allocatedAt": now if phase == "Allocated" else None,
        }
    }
    try:
        # 集群级资源使用 patch_cluster_custom_object_status（更新 status 子资源）
        # CRD 开启了 subresources.status，必须通过 status 子资源 patch status 字段
        api.patch_cluster_custom_object_status(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL,
            name=cr_name,
            body=body,
        )
    except ApiException as e:
        logger.warning(f"更新 PortAllocation CR '{cr_name}' status 为 {phase} 失败: "
                       f"status={e.status}, body={e.body}")


def delete_port_allocation_cr(
    api: client.CustomObjectsApi, cr_name: str, logger: logging.Logger
) -> None:
    """删除单个 PortAllocation CR（释放端口）。"""
    try:
        api.delete_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL, name=cr_name
        )
        logger.info(f"已删除 PortAllocation CR '{cr_name}'，端口已释放")
    except ApiException as e:
        if e.status == 404:
            # 已删除，幂等忽略
            logger.debug(f"PortAllocation CR '{cr_name}' 不存在（404），跳过删除")
        else:
            raise kopf.TemporaryError(
                f"删除 PortAllocation CR '{cr_name}' 失败: status={e.status}, body={e.body}",
                delay=10,
            )


# =============================================================================
# Kopf Handlers
# =============================================================================

@kopf.on.create("apps", "v1", "deployments",
                annotations={MANAGED_ANNOTATION: "true"})
@kopf.on.resume("apps", "v1", "deployments",
                annotations={MANAGED_ANNOTATION: "true"})
def manage_deployment(
    spec, name, namespace, uid, annotations, logger, **kwargs
) -> Dict[str, Any]:
    """Deployment 创建 / Controller 重启恢复时的处理逻辑。

    幂等：若 Deployment 已带 allocated-ports 注解，则跳过分配。
    """
    logger.info(f"捕获到受管 Deployment 事件: {namespace}/{name} (uid={uid})")

    # 幂等检查：注解中已有端口则不再分配
    existing_ports_str = annotations.get(PORTS_ANNOTATION)
    existing_ports = parse_allocated_ports_from_annotation(existing_ports_str)
    if existing_ports:
        logger.info(
            f"{namespace}/{name} 已分配端口 {existing_ports}（注解已存在），跳过分配"
        )
        # 确保对应的 PortAllocation CR 存在（兜底重建缺失的 CR）
        _ensure_crs_for_existing_ports(
            api=custom_api(),
            deployment_name=name,
            namespace=namespace,
            deployment_uid=uid,
            ports=existing_ports,
            logger=logger,
        )
        return {"allocatedPorts": existing_ports, "phase": "Allocated"}

    # 读取当前 Deployment 完整对象，便于后续 patch
    apps = apps_api()
    try:
        deploy = apps.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as e:
        raise kopf.TemporaryError(
            f"读取 Deployment {namespace}/{name} 失败: status={e.status}, body={e.body}",
            delay=15,
        )

    # 序列化为 dict 便于 _find_target_container 处理
    deploy_dict = client.ApiClient().sanitize_for_serialization(deploy)

    # 分配端口并创建 CR（内置 409 竞态重试）
    ports = allocate_and_create_ports(
        api=custom_api(),
        count=NUM_PORTS,
        deployment_name=name,
        namespace=namespace,
        deployment_uid=uid,
        logger=logger,
    )

    # 更新每个 CR 的 status 为 Allocated
    for idx, port in enumerate(ports):
        update_port_allocation_status(
            api=custom_api(), cr_name=f"port-{port}", phase="Allocated", logger=logger
        )

    # 注入端口到 Deployment
    patch_deployment_with_ports(
        apps=apps, deployment=deploy_dict, ports=ports, logger=logger
    )

    logger.info(f"{namespace}/{name} 端口分配完成: {ports}")
    return {"allocatedPorts": ports, "phase": "Allocated"}


def _ensure_crs_for_existing_ports(
    api: client.CustomObjectsApi,
    deployment_name: str,
    namespace: str,
    deployment_uid: str,
    ports: List[int],
    logger: logging.Logger,
) -> None:
    """兜底逻辑：确保每个已分配端口都有对应的 PortAllocation CR。"""
    existing_crs = list_port_allocation_crs(
        api, owner_deployment=deployment_name, owner_namespace=namespace
    )
    existing_ports_in_cr = {(c.get("spec") or {}).get("port") for c in existing_crs}

    for idx, port in enumerate(ports):
        if port not in existing_ports_in_cr:
            logger.warning(
                f"{namespace}/{deployment_name} 注解中的端口 {port} 缺少对应 PortAllocation CR，正在补建"
            )
            ok = create_port_allocation_cr(
                api=api,
                port=port,
                deployment_name=deployment_name,
                namespace=namespace,
                deployment_uid=deployment_uid,
                container_index=idx,
            )
            if ok:
                update_port_allocation_status(
                    api=api, cr_name=f"port-{port}", phase="Allocated", logger=logger
                )
            else:
                logger.warning(
                    f"补建 PortAllocation CR 'port-{port}' 时遇到 409，端口可能被其他资源占用"
                )


@kopf.on.delete("apps", "v1", "deployments",
                annotations={MANAGED_ANNOTATION: "true"})
def release_deployment(name, namespace, uid, logger, **kwargs) -> Dict[str, Any]:
    """Deployment 被删除时的清理逻辑：删除其拥有的所有 PortAllocation CR。"""
    logger.info(f"捕获到受管 Deployment 删除事件: {namespace}/{name} (uid={uid})")

    api = custom_api()
    crs = list_port_allocation_crs(
        api, owner_deployment=name, owner_namespace=namespace
    )

    if not crs:
        logger.info(f"{namespace}/{name} 没有关联的 PortAllocation CR，无需清理")
        return {"released": 0}

    logger.info(f"{namespace}/{name} 找到 {len(crs)} 个待释放的 PortAllocation CR")
    for cr in crs:
        cr_name = cr["metadata"]["name"]
        port = (cr.get("spec") or {}).get("port")
        # 先将状态置为 Released 再删除，便于审计
        update_port_allocation_status(
            api=api, cr_name=cr_name, phase="Released", logger=logger
        )
        delete_port_allocation_cr(api=api, cr_name=cr_name, logger=logger)
        logger.info(f"{namespace}/{name} 已释放端口 {port}")

    return {"released": len(crs)}


# =============================================================================
# 定时对账任务：清理孤儿端口、补建缺失 CR
# =============================================================================

@kopf.timer(CRD_GROUP, CRD_VERSION, CRD_PLURAL,
            interval=RECONCILE_INTERVAL)
def reconcile_orphan_ports(logger, **kwargs) -> None:
    """定期对账 handler。

    由于 kopf timer 是按对象触发的（每个 PortAllocation CR 触发一次），
    这里通过全局时间戳去抖，确保一个 RECONCILE_INTERVAL 内只执行一次全量扫描。

    做两件事：
      1. 清理孤儿：PortAllocation CR 的 ownerDeployment 已不存在的，删除 CR。
      2. 补建缺失：带 managed annotation 的 Deployment 注解中端口缺少 CR 的，补建。
    """
    global _LAST_RECONCILE_TS
    now = time.time()
    # 留出 10 秒余量，防止边界抖动导致跳过
    if now - _LAST_RECONCILE_TS < max(RECONCILE_INTERVAL - 10, 1):
        return
    _LAST_RECONCILE_TS = now

    logger.info("====== 开始周期对账 ======")
    try:
        _reconcile_orphan_crs(logger)
        _reconcile_missing_crs(logger)
    except kopf.TemporaryError:
        raise
    except Exception as e:
        # 兜底捕获，避免 timer handler 异常导致 kopf 异常退出
        logger.exception(f"对账过程中发生未预期异常: {e}")
    logger.info("====== 周期对账结束 ======")


def _reconcile_orphan_crs(logger: logging.Logger) -> None:
    """扫描所有 PortAllocation CR，删除 owner Deployment 已不存在的孤儿 CR。"""
    api = custom_api()
    apps = apps_api()
    crs = list_port_allocation_crs(api)

    # 一次性列出所有 Deployment（apps/v1）做存在性检查，减少 API 调用
    deploy_keys: Set[tuple] = set()
    try:
        all_deploys = apps.list_deployment_for_all_namespaces()
        for d in all_deploys.items:
            deploy_keys.add((d.metadata.namespace, d.metadata.name))
    except ApiException as e:
        raise kopf.TemporaryError(
            f"列出所有 Deployment 失败: status={e.status}, body={e.body}", delay=20
        )

    orphan_count = 0
    for cr in crs:
        spec = cr.get("spec") or {}
        d_name = spec.get("ownerDeployment")
        d_ns = spec.get("ownerNamespace")
        cr_name = cr["metadata"]["name"]
        port = spec.get("port")

        if (d_ns, d_name) in deploy_keys:
            continue  # owner 仍在，正常

        logger.warning(
            f"发现孤儿 PortAllocation CR '{cr_name}' (端口 {port})："
            f"owner Deployment {d_ns}/{d_name} 已不存在，准备清理"
        )
        update_port_allocation_status(
            api=api, cr_name=cr_name, phase="Released", logger=logger
        )
        delete_port_allocation_cr(api=api, cr_name=cr_name, logger=logger)
        orphan_count += 1

    if orphan_count:
        logger.info(f"对账完成：共清理 {orphan_count} 个孤儿 PortAllocation CR")
    else:
        logger.info("对账完成：未发现孤儿 PortAllocation CR")


def _reconcile_missing_crs(logger: logging.Logger) -> None:
    """扫描带 managed annotation 的 Deployment，补建缺失的 PortAllocation CR。"""
    api = custom_api()
    apps = apps_api()

    try:
        all_deploys = apps.list_deployment_for_all_namespaces()
    except ApiException as e:
        raise kopf.TemporaryError(
            f"列出所有 Deployment 失败: status={e.status}, body={e.body}", delay=20
        )

    fixed_count = 0
    for d in all_deploys.items:
        annotations = d.metadata.annotations or {}
        if annotations.get(MANAGED_ANNOTATION) != "true":
            continue

        ns = d.metadata.namespace
        name = d.metadata.name
        uid = d.metadata.uid
        ports_str = annotations.get(PORTS_ANNOTATION)
        ports = parse_allocated_ports_from_annotation(ports_str)

        if not ports:
            # 注解中没有端口信息，可能是分配流程被中断，跳过交给 create handler 处理
            logger.warning(
                f"{ns}/{name} 带有 managed 注解但缺少 allocated-ports 注解，"
                f"可能端口分配流程未完成，将在下次事件中处理"
            )
            continue

        existing_crs = list_port_allocation_crs(
            api, owner_deployment=name, owner_namespace=ns
        )
        existing_ports = {(c.get("spec") or {}).get("port") for c in existing_crs}

        missing = [p for p in ports if p not in existing_ports]
        if missing:
            logger.warning(
                f"{ns}/{name} 注解中的端口 {missing} 缺少对应 PortAllocation CR，开始补建"
            )
        for idx, port in enumerate(ports):
            if port in existing_ports:
                continue
            ok = create_port_allocation_cr(
                api=api,
                port=port,
                deployment_name=name,
                namespace=ns,
                deployment_uid=uid,
                container_index=idx,
            )
            if ok:
                update_port_allocation_status(
                    api=api, cr_name=f"port-{port}", phase="Allocated", logger=logger
                )
                fixed_count += 1
            else:
                logger.warning(
                    f"补建 PortAllocation CR 'port-{port}' 失败（409），端口可能已被占用"
                )

    if fixed_count:
        logger.info(f"对账完成：共补建 {fixed_count} 个缺失的 PortAllocation CR")


# =============================================================================
# 启动入口（容器内由 kopf 命令直接调用本模块，无需 __main__）
# =============================================================================

# 配置 kopf 默认日志级别（可通过 KOPF_LOG_LEVEL 环境变量覆盖）
logging.getLogger("kopf").setLevel(os.environ.get("KOPF_LOG_LEVEL", "INFO"))
