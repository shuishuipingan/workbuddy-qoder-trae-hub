# 单镜像同时跑「三家引擎 + 门户」——与本地 bat 启动的是同一个进程模型。
# admin/static 是构建产物，已随仓库提供，所以镜像里不需要 Node。
#
# 端口与数据根必须写成 ENV，并被 hub/tests/_test_docker_contract.py 校验：
#   ENV 数值 = CMD 数值 = EXPOSE = compose 映射 = 数据卷根。
# trae 在容器里用 8792（与上游镜像历史口径一致，避免与宿主机 trae 默认端口撞车）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HUB_HOST=0.0.0.0 \
    HUB_PORT=8817 \
    WB_PORT=8811 \
    QD_PORT=8812 \
    TRAE_PORT=8792 \
    HUB_DATA_ROOT=/app/data \
    TZ=Asia/Shanghai

WORKDIR /app
COPY . /app

# 运行时数据（账号/用量/日志/设置）落在卷上，升级镜像不丢
RUN mkdir -p "$HUB_DATA_ROOT" /app/logs /app/state \
    && useradd -m -u 10001 hub \
    && chown -R hub:hub /app
USER hub

EXPOSE 8811 8812 8792 8817

# 探针表写成 (端口变量, 路径) 的字面量，契约测试按这个形状解析
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD python -c "import os,sys,urllib.request as u;probe=[('WB_PORT','/health'), ('QD_PORT','/health'), ('TRAE_PORT','/health'), ('HUB_PORT','/api/hub/health')];base='http://127.0.0.1:';ok=all(u.urlopen(base+os.environ[k]+p,timeout=4).status==200 for k,p in probe);sys.exit(0 if ok else 1)"

CMD python -m hub.server --host 0.0.0.0 \
     --wb-port 8811 --qd-port 8812 --trae-port 8792 \
     --portal-port 8817
