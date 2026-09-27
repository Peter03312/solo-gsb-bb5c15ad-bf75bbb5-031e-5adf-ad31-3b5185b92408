FROM python:3.11-slim

# 不写 .pyc、日志直出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码与启动脚本（依赖与初始化全部在镜像内完成）
COPY app ./app
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 构建期冒烟校验：确保所有路由模块（含 /debt-diff）可正常导入
RUN python -c "from app.main import app; assert any(getattr(r, 'path', None) == '/debt-diff' for r in app.routes)"

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]
