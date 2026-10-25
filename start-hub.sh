#!/usr/bin/env bash
# ===========================================================
#  hub - unified launcher (macOS / Linux)
#
#  Usage:
#    ./start-hub.sh                      local, default ports
#    ./start-hub.sh lan                  LAN mode (listen on 0.0.0.0)
#    ./start-hub.sh 9001 9002            custom engine ports
#    ./start-hub.sh 9001 9002 9003       custom engine + portal ports
#    ./start-hub.sh 9001 9002 9003 lan   custom ports + LAN mode
#
#  One command starts everything: both engines (internal) and the
#  fusion portal - the ONLY address that matters (panel + /api/v1).
#  Defaults deliberately avoid 8789/8790 (standalone instances often
#  live there; a collision would fool the hub's health checks).
# ===========================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# ---- args: "lan" may sit anywhere; the rest are wb/qd/portal ports ----
LAN=""
WB_PORT="" QD_PORT="" HUB_PORT=""
for a in "$@"; do
  case "$a" in
    lan|LAN) LAN=1 ;;
    *)
      if [ -z "$WB_PORT" ]; then WB_PORT="$a"
      elif [ -z "$QD_PORT" ]; then QD_PORT="$a"
      elif [ -z "$HUB_PORT" ]; then HUB_PORT="$a"
      fi ;;
  esac
done
WB_PORT="${WB_PORT:-8811}"
QD_PORT="${QD_PORT:-8812}"
HUB_PORT="${HUB_PORT:-8817}"

cd "$HERE"

if [ ! -f "hub/server.py" ]; then
  echo "[ERROR] hub/server.py not found next to this script."
  echo "        expected: $HERE/hub/server.py"
  exit 1
fi

pick_python() {
  for c in "$HERE/python/bin/python3" "$HERE/python/bin/python" \
           /usr/bin/python3 "$(command -v python3 || true)" "$(command -v python || true)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

if ! PYEXE="$(pick_python)"; then
  echo "[ERROR] No usable Python found (need 3.9+)."
  echo "  macOS:  xcode-select --install   or   brew install python"
  echo "  Debian: sudo apt install python3"
  echo "  Or drop a portable runtime into ./python/ (python/bin/python3)."
  exit 1
fi

check_dash() {
  if [ ! -f "$1" ]; then
    echo "[WARN] $1 is MISSING - GET / on the $2 engine will answer 500."
    echo "       See README-HUB.md \"Known limitations\" for the one-line fix."
  fi
}

echo "==========================================================="
echo "  hub  (WorkBuddy x Qoder x Trae)"
echo
if [ -n "$LAN" ]; then
  echo "  portal : http://0.0.0.0:$HUB_PORT/    [LAN mode]"
  echo "  api    : http://0.0.0.0:$HUB_PORT/api/v1"
  echo "           other devices use this machine's LAN IP:$HUB_PORT"
else
  echo "  portal : http://127.0.0.1:$HUB_PORT/"
  echo "  api    : http://127.0.0.1:$HUB_PORT/api/v1"
fi
echo
echo "  Keep this window open. Press Ctrl+C to stop."
echo "==========================================================="
echo

check_dash "hub/engines/wb/dashboard.html" wb
check_dash "hub/engines/qd/dashboard.html" qd

EXTRA=""
if [ -n "$LAN" ]; then EXTRA="--host 0.0.0.0 --wb-args=--lan --qd-args=--lan"; fi

#  One command starts everything: both engines + the fusion portal.
#  shellcheck disable=SC2086
exec "$PYEXE" -m hub.server --wb-port "$WB_PORT" --qd-port "$QD_PORT" --portal-port "$HUB_PORT" $EXTRA
