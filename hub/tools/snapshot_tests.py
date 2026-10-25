#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snapshot_tests.py —— 给测试目录打一个 zip 快照（第 221 轮）。

为什么需要：这个仓库**没有 git**。2026-10-20 我一次写文件的参数写错，把
admin/tests/_test_merged_writes.js（168 条判据）覆盖成了 93 字节 ——
最后靠 Windows 卷影副本才捞回来。系统快照不是每个用户都有，也不该是唯一保险。

做法：把 admin/tests 与 hub/tests 打进 docs/backup/tests-<时间>.zip，
只保留最近 N 份（默认 5）。docs/ 在 .dockerignore 里，不会进镜像。

用法：
  python hub/tools/snapshot_tests.py            # 打一份
  python hub/tools/snapshot_tests.py --keep 3   # 只留 3 份
  python hub/tools/snapshot_tests.py --list     # 看现有快照
"""
import argparse
import os
import sys
import time
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "docs", "backup")
TREES = (("admin", "tests"), ("hub", "tests"))


def snapshots():
    if not os.path.isdir(OUT_DIR):
        return []
    names = [n for n in os.listdir(OUT_DIR) if n.startswith("tests-") and n.endswith(".zip")]
    return sorted(names)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python hub/tools/snapshot_tests.py",
                                 description="把两侧测试目录打成 zip 快照（无 git 时的保险）")
    ap.add_argument("--keep", type=int, default=5, help="保留最近几份（默认 5）")
    ap.add_argument("--list", action="store_true", help="只列出已有快照")
    args = ap.parse_args(argv)

    if args.list:
        for n in snapshots():
            p = os.path.join(OUT_DIR, n)
            print("%-34s %8.1f KB" % (n, os.path.getsize(p) / 1024.0))
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUT_DIR, "tests-%s.zip" % stamp)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, sub in TREES:
            root = os.path.join(ROOT, base, sub)
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, files in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for f in files:
                    if not f.endswith((".js", ".py")):
                        continue
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, ROOT).replace("\\", "/")
                    zf.write(full, rel)
                    n += 1
    size = os.path.getsize(out) / 1024.0
    print("已快照 %d 个文件 → %s（%.1f KB）" % (n, os.path.relpath(out, ROOT), size))

    olds = snapshots()[:-args.keep] if args.keep > 0 else []
    for name in olds:
        try:
            os.remove(os.path.join(OUT_DIR, name))
            print("  清掉旧快照:", name)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
