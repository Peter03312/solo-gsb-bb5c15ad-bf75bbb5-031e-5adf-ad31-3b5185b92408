#!/usr/bin/env sh
# 容器启动初始化：校验依赖可导入后启动服务
set -e

echo "[entrypoint] starting quota simulation service on 0.0.0.0:8000 ..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
