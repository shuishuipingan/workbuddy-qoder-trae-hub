# -*- coding: utf-8 -*-
"""把 capabilities.js 里的上游锚点按当前文件重算（行号 + 命中数）。

为什么需要它：上游 dashboard.html 一改版，锚点的 line/matches 就漂，
K1 会红一片，而每个锚点的手工核对只值 30 秒 —— 这类活应该由工具做。

用法：
  python hub/tools/refresh_anchors.py --check   # 只报告会改哪些
  python hub/tools/refresh_anchors.py           # 就地更新
"""
import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAP = os.path.join(ROOT, "admin", "tests", "_lib", "capabilities.js")

# { file: 'x', line: 12, marker: 'y', matches: 3 }
ENTRY = re.compile(r"\{\s*file:\s*'([^']+)',\s*line:\s*(\d+),\s*marker:\s*'((?:[^'\\]|\\.)+)',\s*matches:\s*(\d+)\s*\}")


def count_and_line(path, marker):
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return None, 0
    hits = [i + 1 for i, l in enumerate(lines) if marker in l]
    if not hits:
        return None, 0
    # matches 必须按**判据的语义**算：±60 行窗口内的命中数（K1 就是按窗口数的）
    first = hits[0]
    in_window = [h for h in hits if abs(h - first) <= 60]
    return first, len(in_window)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python hub/tools/refresh_anchors.py",
                                 description="按当前引擎文件重算 capabilities.js 的锚点行号与命中数")
    ap.add_argument("--check", action="store_true", help="只报告，不写盘")
    args = ap.parse_args(argv)

    src = io.open(CAP, encoding="utf-8").read()
    changes = []

    def sub(m):
        rel, line, marker, matches = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        target = os.path.join(ROOT, rel.replace("/", os.sep))
        first, n = count_and_line(target, marker)
        if first is None or n == 0:
            return m.group(0)          # 找不到就不动，让 K1 去报红
        if first != line or n != matches:
            changes.append((rel, marker[:24], line, first, matches, n))
        return "{ file: '%s', line: %d, marker: '%s', matches: %d }" % (rel, first, marker, n)

    out = ENTRY.sub(sub, src)
    for rel, mk, ol, nl, om, nm in changes:
        print("  %-28s %-22s line %d -> %d, matches %d -> %d" % (rel.split("/")[-1], mk, ol, nl, om, nm))
    print("共 %d 处需要更新" % len(changes))
    if changes and not args.check:
        io.open(CAP, "w", encoding="utf-8").write(out)
        print("已写回", CAP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
