#!/usr/bin/env bash
# hub - LAN mode launcher (macOS / Linux)
# Forwards to start-hub.sh with the "lan" keyword.
# Optional port overrides: ./start-hub-lan.sh 9001 9002 9003
cd "$(dirname "$0")" || exit 1
exec ./start-hub.sh "$@" lan
