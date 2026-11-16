#!/usr/bin/env bash
# =============================================================================
#  deploy/install.sh —— 把本项目注册为 systemd 服务
#
#  设计说明：
#    * systemd 单元含绝对路径/端口/数据目录，属于「部署产物」，因此仓库里只
#      保存模板 deploy/wqt-hub.service，安装时按本机实际情况渲染到
#      /etc/systemd/system/<name>.service。
#    * 幂等：可反复运行。已有单元会先备份为 *.bak.<时间戳> 再覆盖。
#    * 升级代码后只需 `systemctl restart wqt-hub`，不必重跑本脚本；
#      改了端口/数据目录才需要重跑。
#
#  用法：
#    sudo ./deploy/install.sh                   # 默认：127.0.0.1:18717，数据在 /data/opt/hub-data
#    sudo ./deploy/install.sh --portal-port 8817
#    sudo ./deploy/install.sh --data-root ./data
#    sudo ./deploy/install.sh --host 0.0.0.0     # 直接对外（不推荐，通常交给 OpenResty）
#    sudo ./deploy/install.sh --dry-run          # 只打印将写入的单元内容
#    sudo ./deploy/install.sh --no-start         # 只安装，不启动
#    sudo ./deploy/install.sh --uninstall        # 停用并移除单元（保留数据）
# =============================================================================
set -euo pipefail

SERVICE_NAME="wqt-hub"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # 项目根（本脚本位于 deploy/）
TEMPLATE="$HERE/deploy/wqt-hub.service"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

# ---- 默认值（与当前生产部署一致，避免重跑打乱现状）-------------------------
HOST="127.0.0.1"
WB_PORT=8811
QD_PORT=8812
TRAE_PORT=8792
PORTAL_PORT=18717
DATA_ROOT="/data/opt/hub-data"
TZ_NAME="Asia/Shanghai"

DRY_RUN=0
NO_START=0
UNINSTALL=0

usage() {
    sed -n '/^#  用法：/,/^# =====/p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host)        HOST="$2"; shift 2 ;;
        --wb-port)     WB_PORT="$2"; shift 2 ;;
        --qd-port)     QD_PORT="$2"; shift 2 ;;
        --trae-port)   TRAE_PORT="$2"; shift 2 ;;
        --portal-port) PORTAL_PORT="$2"; shift 2 ;;
        --data-root)   DATA_ROOT="$2"; shift 2 ;;
        --tz)          TZ_NAME="$2"; shift 2 ;;
        --dry-run)     DRY_RUN=1; shift ;;
        --no-start)    NO_START=1; shift ;;
        --uninstall)   UNINSTALL=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "未知参数: $1（--help 查看用法）" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = "0" ] || { echo "请用 root 运行（systemd 单元写入 /etc）" >&2; exit 1; }
[ -f "$TEMPLATE" ] || { echo "缺少模板: $TEMPLATE" >&2; exit 1; }

# ---- 卸载 -------------------------------------------------------------------
if [ "$UNINSTALL" = "1" ]; then
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
    echo "已移除 $SERVICE_NAME.service"
    echo "数据目录保留在: $DATA_ROOT （如需删除请自行 rm -rf）"
    exit 0
fi

# ---- Python 版本检查 --------------------------------------------------------
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "未找到 python3" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || { echo "需要 Python 3.9+，当前: $("$PY" -V 2>&1)" >&2; exit 1; }

# ---- 虚拟环境（项目零第三方依赖，venv 仅用于隔离解释器）---------------------
if [ -x "$HERE/.venv/bin/python" ]; then
    echo "[1/5] 复用已有 venv: $HERE/.venv ($("$HERE/.venv/bin/python" -V 2>&1))"
else
    echo "[1/5] 创建 venv: $HERE/.venv"
    "$PY" -m venv "$HERE/.venv"
fi

mkdir -p "$DATA_ROOT"
echo "[2/5] 数据目录: $DATA_ROOT"

# ---- 渲染单元 ---------------------------------------------------------------
HUB_NAME=$(sed -n 's/^HUB_NAME = "\(.*\)"$/\1/p' "$HERE/hub/__init__.py" | head -1)
[ -n "$HUB_NAME" ] || HUB_NAME="WorkBuddy x Qoder x Trae Hub"

render() {
    sed -e "s|@HUB_NAME@|$HUB_NAME|g" \
        -e "s|@PROJECT_DIR@|$HERE|g" \
        -e "s|@HUB_DATA_ROOT@|$DATA_ROOT|g" \
        -e "s|@TZ@|$TZ_NAME|g" \
        -e "s|@HOST@|$HOST|g" \
        -e "s|@WB_PORT@|$WB_PORT|g" \
        -e "s|@QD_PORT@|$QD_PORT|g" \
        -e "s|@TRAE_PORT@|$TRAE_PORT|g" \
        -e "s|@PORTAL_PORT@|$PORTAL_PORT|g" \
        "$TEMPLATE"
}

if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] 将写入 $UNIT:"
    echo "------------------------------------------------------------------"
    render
    echo "------------------------------------------------------------------"
    exit 0
fi

echo "[3/5] 写入 $UNIT"
if [ -f "$UNIT" ]; then
    BAK="${UNIT}.bak.$(date +%Y%m%d%H%M%S)"
    cp "$UNIT" "$BAK"
    echo "      已备份旧单元: $BAK"
fi
render > "$UNIT"

# ---- 端口占用预检（仅提示，不阻断）------------------------------------------
for p in "$WB_PORT" "$QD_PORT" "$TRAE_PORT" "$PORTAL_PORT"; do
    if ss -lntH "sport = :$p" 2>/dev/null | grep -q .; then
        echo "      注意: 端口 $p 已被占用（若正是本服务在跑，忽略即可）" >&2
    fi
done

# ---- 启用并启动 -------------------------------------------------------------
echo "[4/5] systemctl daemon-reload && enable"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

if [ "$NO_START" = "1" ]; then
    echo "[5/5] 已跳过启动（--no-start），稍后: systemctl start $SERVICE_NAME"
else
    # 先停再起：引擎端口是短连接服务，直接 restart 有可能撞上 TIME_WAIT
    # （表现为 "Address already in use"，引擎起不来、面板显示 down）。
    echo "[5/5] 重启服务使新配置生效"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    for _ in $(seq 1 15); do
        busy=$(ss -tan 2>/dev/null \
               | grep -cE ":($WB_PORT|$QD_PORT|$TRAE_PORT|$PORTAL_PORT)\b" || true)
        [ "$busy" = "0" ] && break
        sleep 2
    done
    systemctl start "$SERVICE_NAME"
    sleep 3
fi

systemctl --no-pager status "$SERVICE_NAME" | head -8 || true

cat <<EOF

------------------------------------------------------------------
安装完成
  服务名   : $SERVICE_NAME.service
  单元文件 : $UNIT（模板: deploy/wqt-hub.service）
  项目目录 : $HERE
  数据目录 : $DATA_ROOT
  门户地址 : http://$HOST:$PORTAL_PORT/
  API 地址 : http://$HOST:$PORTAL_PORT/api/v1
  引擎端口 : wb=$WB_PORT qd=$QD_PORT trae=$TRAE_PORT（仅绑定 $HOST）

  查看日志 : journalctl -u $SERVICE_NAME -f
  升级代码 : git pull && systemctl restart $SERVICE_NAME
  卸载服务 : sudo ./deploy/install.sh --uninstall
------------------------------------------------------------------
EOF
