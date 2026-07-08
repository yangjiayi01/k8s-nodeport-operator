#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
孤儿端口清理脚本（手动对账工具）。

用途：
    紧急情况下手动运行，扫描所有 PortAllocation CR，检查其 ownerDeployment 是否存在，
    不存在则删除对应 CR，释放端口。不依赖 Controller 运行。

使用方式：
    # 1. 集群外执行（使用 ~/.kube/config）
    python scripts/cleanup-orphan-ports.py

    # 2. Pod 内执行（in-cluster）
    kubectl -n port-manager exec -it <controller-pod> -- python /app/scripts/cleanup-orphan-ports.py

    # 3. 预演（dry-run，只打印不删除）
    python scripts/cleanup-orphan-ports.py --dry-run

依赖：
    pip install kubernetes
"""

import argparse
import sys
import logging

try:
    from kubernetes import client, config  # noqa: F401
except ImportError:
    print("缺少依赖：请先执行 `pip install kubernetes`", file=sys.stderr)
    sys.exit(1)

from kubernetes.client import ApiException

# CRD 资源定义（与 controller.py 保持一致）
CRD_GROUP = "portmanager.example.com"
CRD_VERSION = "v1"
CRD_PLURAL = "portallocations"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cleanup-orphan-ports")


def init_clients():
    """初始化 Kubernetes 客户端，优先 in-cluster，回退 kubeconfig。"""
    try:
        config.load_incluster_config()
        logger.info("使用 in-cluster 配置")
    except Exception:
        config.load_kubeconfig()
        logger.info("使用 kubeconfig 配置")

    return client.CustomObjectsApi(), client.AppsV1Api()


def list_all_port_allocations(custom_api):
    """列出所有 PortAllocation CR。"""
    try:
        result = custom_api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL
        )
    except ApiException as e:
        logger.error(f"列出 PortAllocation CR 失败: status={e.status}, body={e.body}")
        raise
    return result.get("items", [])


def list_all_deployments(apps_api):
    """列出所有命名空间下的 Deployment，返回 (namespace, name) 集合。"""
    try:
        all_deploys = apps_api.list_deployment_for_all_namespaces()
    except ApiException as e:
        logger.error(f"列出 Deployment 失败: status={e.status}, body={e.body}")
        raise
    return {(d.metadata.namespace, d.metadata.name) for d in all_deploys.items}


def cleanup(custom_api, apps_api, dry_run=False):
    """执行对账：删除 owner Deployment 不存在的 PortAllocation CR。"""
    crs = list_all_port_allocations(custom_api)
    logger.info(f"共发现 {len(crs)} 个 PortAllocation CR")

    deploy_keys = list_all_deployments(apps_api)
    logger.info(f"共发现 {len(deploy_keys)} 个 Deployment")

    orphan_count = 0
    deleted_count = 0

    for cr in crs:
        spec = cr.get("spec") or {}
        d_name = spec.get("ownerDeployment")
        d_ns = spec.get("ownerNamespace")
        cr_name = cr["metadata"]["name"]
        port = spec.get("port")

        if (d_ns, d_name) in deploy_keys:
            logger.debug(f"[OK] CR '{cr_name}' (端口 {port}) owner {d_ns}/{d_name} 存在")
            continue

        orphan_count += 1
        logger.warning(
            f"[ORPHAN] CR '{cr_name}' (端口 {port}) owner {d_ns}/{d_name} 已不存在"
        )

        if dry_run:
            logger.info(f"[DRY-RUN] 跳过删除 CR '{cr_name}'")
            continue

        try:
            custom_api.delete_cluster_custom_object(
                group=CRD_GROUP, version=CRD_VERSION, plural=CRD_PLURAL, name=cr_name
            )
            logger.info(f"[DELETED] 已删除孤儿 CR '{cr_name}'，端口 {port} 已释放")
            deleted_count += 1
        except ApiException as e:
            if e.status == 404:
                logger.debug(f"[SKIP] CR '{cr_name}' 已不存在（404），视为已删除")
                deleted_count += 1
            else:
                logger.error(
                    f"[FAIL] 删除 CR '{cr_name}' 失败: status={e.status}, body={e.body}"
                )

    logger.info(f"对账结束: 发现孤儿 {orphan_count} 个, 实际删除 {deleted_count} 个"
                + ("（dry-run 模式，未实际删除）" if dry_run else ""))


def main():
    parser = argparse.ArgumentParser(description="清理孤儿 PortAllocation CR")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印不删除"
    )
    args = parser.parse_args()

    custom_api, apps_api = init_clients()
    cleanup(custom_api, apps_api, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
