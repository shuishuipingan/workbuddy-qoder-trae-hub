#!/usr/bin/env bash
# ===========================================================
#  hub - double-clickable launcher for macOS
#
#  Finder runs this file directly; it forwards to start-hub.sh
#  so there is only ONE place holding the logic.
#
#  First run may be blocked by Gatekeeper: right-click -> Open.
#  If the shell refuses to run it, once:
#      chmod +x start-hub.sh start-hub.command allow-firewall.command
# ===========================================================
cd "$(dirname "$0")" || exit 1
exec ./start-hub.sh "$@"
