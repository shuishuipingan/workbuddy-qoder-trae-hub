#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""release_check.py -- 一条命令跑完发布前的全部验收（第 241 轮）。

跑什么、为什么：
  1. admin 套件      前端契约（渲染/交互/文案/主题/CSS 一致性）
  2. hub 套件        后端契约（形状/降级/鉴权/引擎原样搬入/文档覆盖）
  3. verify_sync     三家引擎与上游逐字节一致（只允许 hub-patch 块内差异）
  4. verify_persistence  写入 -> 重启 -> 数据还在
  5. verify_writefail    写不进去时说人话、引擎不倒

判据只有一条：全部通过才给 PASS。任何一项非零退出即 FAIL。

用法：
  python hub/tools/release_check.py
  python hub/tools/release_check.py --skip-slow    # 跳过两个起实例的检查
"""
import argparse
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable

STEPS = [
    # 第 12 轮：面板已切到 React 工程，前端判据改跑 admin/app 的 vitest（旧套件归档在 docs/backup/admin-legacy-20261021/tests）
    ("前端套件", ["hub/tools/run_app_tests.py"], 900, False),
    ("hub 套件", ["hub/tests/run_all.py", "--strict"], 900, False),
    ("与上游一致", ["hub/tools/verify_sync.py"], 300, False),
    ("重启不丢数据", ["hub/tools/verify_persistence.py"], 420, True),
    ("写失败可诊断", ["hub/tools/verify_writefail.py"], 420, True),
]


def run_one(name, argv, timeout, slow):
    started = time.time()
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run([PY] + argv, cwd=ROOT, env=env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace")
        out = proc.stdout or ""
        code = proc.returncode
    except subprocess.TimeoutExpired:
        out, code = "超时（%d 秒）" % timeout, 124
    dur = time.time() - started
    tail = ""
    for line in reversed(out.splitlines()):
        if line.strip():
            tail = line.strip()[:120]
            break
    return {"name": name, "code": code, "tail": tail, "seconds": dur, "slow": slow, "out": out}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python hub/tools/release_check.py")
    ap.add_argument("--skip-slow", action="store_true", help="跳过会起实例的两项")
    ap.add_argument("--verbose", action="store_true", help="失败时打印完整输出")
    args = ap.parse_args(argv)

    rows, failed = [], []
    for name, argvx, timeout, slow in STEPS:
        if args.skip_slow and slow:
            rows.append({"name": name, "code": None, "tail": "已跳过", "seconds": 0.0, "slow": slow, "out": ""})
            continue
        print("  ... %s" % name, flush=True)
        r = run_one(name, argvx, timeout, slow)
        rows.append(r)
        if r["code"] != 0:
            failed.append(r)

    print("")
    print("  %-16s %-8s %8s  %s" % ("检查", "结果", "耗时", "末行"))
    for r in rows:
        if r["code"] is None:
            verdict = "SKIP"
        elif r["code"] == 0:
            verdict = "PASS"
        else:
            verdict = "FAIL"
        print("  %-16s %-8s %7.1fs  %s" % (r["name"], verdict, r["seconds"], r["tail"]))

    if failed and args.verbose:
        for r in failed:
            print("")
            print("---- %s 输出 ----" % r["name"])
            print("\n".join(r["out"].splitlines()[-40:]))

    print("")
    if failed:
        print("RESULT: RED  (%d 项未通过: %s)" % (len(failed), ", ".join(x["name"] for x in failed)))
        return 1
    print("RESULT: GREEN  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
