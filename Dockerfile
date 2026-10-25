# 单镜像同时跑「三家引擎 + 门户」——与本地 bat 启动的是同一个进程模型。
# admin/static 是构建产物，已随仓库提供，所以镜像里不需要 Node。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HUB_HOST=0.0.0.0 \
    HUB_PORT=8817

WORKDIR /app
COPY . /app

# 运行时数据（账号/用量/日志/设置）挂到卷上，升级镜像不丢
RUN mkdir -p /app/data /app/logs /app/state \
    && useradd -m -u 10001 hub \
    && chown -R hub:hub /app
USER hub

EXPOSE 8817
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8817/api/hub/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "hub.server", "--host", "0.0.0.0", "--wb-port", "8811", "--qd-port", "8812", "--trae-port", "8813", "--portal-port", "8817"]
