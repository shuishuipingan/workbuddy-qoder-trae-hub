#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_app_tests.py —— 跑 admin/app（React 面板）的 vitest，供 release_check 调用。

2026 第 12 轮起，后台前端从"零构建原生面板"切到 Vite + React 工程；
旧判据（docs/backup/admin-legacy-20261021/tests）归档，新判据在 admin/app/src/**/*.test.tsx。

本脚本只做一件事：在 admin/app 里跑 `npx vitest run`，把退出码原样返回，
并把末行摘要打出来（release_check 取末行显示）。
"""
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "admin", "app")


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print("usage: python hub/tools/run_app_tests.py")
        print(__doc__ or "在 admin/app 里跑 npx vitest run")
        return 0
    if not os.path.isdir(os.path.join(APP, "node_modules")):
        print("admin/app/node_modules 不存在 —— 先跑 `pnpm install`")
        return 1
    cmd = ["npx", "vitest", "run"]
    proc = subprocess.run(cmd, cwd=APP, shell=(os.name == "nt"),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding="utf-8", errors="replace", timeout=900)
    out = proc.stdout or ""
    tail = [l.strip() for l in out.splitlines() if l.strip()]
    keep = [l for l in tail if "Test Files" in l or "Tests " in l]
    for line in keep[-2:]:
        print(line)
    if proc.returncode != 0:
        print("".join(out.splitlines(True)[-30:]))
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
