# Dockerfile for k8s-port-allocator
# 基于 python:3.11-slim 精简镜像
# ARG 可在多平台构建时切换基础镜像源（默认官方源，国内构建可用代理源）
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

# 设置时区与编码环境变量，避免中文日志输出乱码
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8

# 创建工作目录
WORKDIR /app

# 先单独拷贝依赖文件，利用 Docker 层缓存加速构建
COPY requirements.txt /app/requirements.txt

# 安装依赖；slim 镜像无 gcc，纯 wheel 安装即可
RUN pip install --no-cache-dir -r /app/requirements.txt

# 拷贝 Operator 核心逻辑
COPY controller.py /app/controller.py

# kopf 以 standalone 模式运行，并暴露 liveness 探针端口 8080/healthz
ENTRYPOINT ["kopf", "run", "--standalone", "--liveness=http://0.0.0.0:8080/healthz", "controller.py"]
