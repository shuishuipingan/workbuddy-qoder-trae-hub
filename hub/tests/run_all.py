# -*- coding: utf-8 -*-
"""hub/tests/run_all.py —— 融合层的验收编排器（零依赖 · 无浏览器 · 无外网）。

    python hub/tests/run_all.py                # 全部套件
    python hub/tests/run_all.py static         # 只跑名字含 static 的
    python hub/tests/run_all.py --strict       # skip / 缺失也算红（CI 与最终验收用）

口径与 admin/tests/run_all.py、tests/run_all.py（qoder）、wb/tests/run_all.py 一致：
  · 套件名规范 _test_*.py（只扫本目录顶层，不递归）；每套件独立日志，失败打 tail 25 行；
  · 套件自报 `SUMMARY: TOTAL N checks, P passed, F failed, S skipped`，汇总时一并统计；
  · 无匹配套件返回 2；有红返回 1；全绿返回 0；
  · 中文输出一律锁 UTF-8（Windows 控制台默认 cp936，会把判据刷成乱码）。

与前两套的差异：hub 的门槛包含「别人文件没落地时不许崩」，所以 skip 是常态之一 ——
默认 skip 不算红（但会显著打印），`--strict` 把它升级成红。
"""
import os
import re
import subprocess
import sys
import tempfile
import datetime   # MEASURED 行用（奶盖的「测量必须带版本与时刻」）

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
TAIL_LINES = 25
# 单套件上限：门户套件现在要起进程 + 30 条判据（含 /v1 别名、chunked/gzip body、
# keep-alive 对齐），在 300s 上偶发超时（实测过一次，单独跑却是 30/30 全过）。
# 抬到 420s：只是放宽天花板，跑得快的套件不受影响。
TIMEOUT = float(os.environ.get("HUB_TESTS_TIMEOUT") or 420)


def suites(pattern=""):
    names = sorted(n for n in os.listdir(HERE)
                   if n.startswith("_test_") and n.endswith(".py"))
    return [n for n in names if pattern in n]


def parse_summary(text):
    """从套件日志里取 TOTAL / passed / failed / skipped / missing。"""
    m = None
    for hit in re.finditer(r"SUMMARY: TOTAL (\d+) checks, (\d+) passed, (\d+) failed, (\d+) skipped(?: \((\d+) missing\))?", text):
        m = hit
    if not m:
        return None
    return {"total": int(m.group(1)), "passed": int(m.group(2)), "failed": int(m.group(3)),
            "skipped": int(m.group(4)), "missing": int(m.group(5) or 0)}


def build_ref():
    """被测产物指纹（奶盖 2026-10-09 的规则：测量结论必须能自证「测的是哪一版、什么时刻」）。

    两条真实案例都来自缺这个：① 等 3s 断言 svg=0（其实是还没画完）；
    ② 清了缓存却没断言版本，读到旧 DOM 导致结论失效。
    """
    import hashlib
    import time
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    targets = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "hub")):
        for n in files:
            if n.endswith(".py"):
                targets.append(os.path.join(dirpath, n))
    idx = os.path.join(root, "admin", "index.html")
    if os.path.exists(idx):
        targets.append(idx)
    targets.sort()
    h = hashlib.md5()
    recent = []
    now = time.time()
    for f in targets:
        with open(f, "rb") as fh:
            h.update(os.path.relpath(f, root).encode("utf-8") + fh.read())
        age = now - os.path.getmtime(f)
        if age < 5:
            recent.append((os.path.basename(f), age))
    return h.hexdigest()[:12], len(targets), recent


def tail(text, n=TAIL_LINES):
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def run_one(name, logdir):
    log = os.path.join(logdir, name + ".log")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        with open(log, "wb") as fh:
            rc = subprocess.call([sys.executable, os.path.join(HERE, name)],
                                 stdout=fh, stderr=subprocess.STDOUT, env=env, cwd=HERE,
                                 timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("\nTIMEOUT: 套件超过 %.0f 秒未退出（多半是没清掉的进程/连接）\n" % TIMEOUT)
        return name, 124, log
    return name, rc, log


def main(argv):
    args = list(argv[1:])
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    strict = "--strict" in args
    args = [a for a in args if not a.startswith("--")]
    pattern = args[0] if args else ""
    selected = suites(pattern)
    if not selected:
        print("  no suite matches %r in %s" % (pattern, HERE))
        return 2

    logdir = tempfile.mkdtemp(prefix="hub-suites-")
    results = [run_one(n, logdir) for n in selected]

    failed, skipped_suites, unknown = [], [], []
    grand = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "missing": 0}
    for name, rc, log in results:
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        stat = parse_summary(text)
        if stat is None:
            unknown.append(name)
            print("  %-28s %-12s checks=?" % (name, "PASS" if rc == 0 else "FAIL(rc=%d)" % rc))
        else:
            for k in grand:
                grand[k] += stat[k]
            if stat["skipped"] or stat["missing"]:
                skipped_suites.append(name)
            print("  %-28s %-12s checks=%-4d skipped=%d%s" % (
                name, "PASS" if rc == 0 else "FAIL(rc=%d)" % rc, stat["total"], stat["skipped"],
                ("  missing=%d" % stat["missing"]) if stat["missing"] else ""))
        if rc != 0:
            failed.append((name, log))
    for name, log in failed:
        print("")
        print("---- %s tail (%d lines) ----" % (name, TAIL_LINES))
        with open(log, encoding="utf-8", errors="replace") as fh:
            for line in tail(fh.read()):
                print("    " + line)
    print("")
    _bh, _bn, _brecent = build_ref()
    print("MEASURED: %s · BUILD %s (%d 个被测文件)" % (datetime.datetime.now().astimezone().isoformat(timespec="seconds"), _bh, _bn))
    if _brecent:
        print("[WARN] 有 %d 个被测文件在测量前 5 秒内被改写: %s —— 可能对应中间态，复跑一次再引用数字" % (len(_brecent), ", ".join("%s(%.1fs前)" % r for r in _brecent[:5])))
    print("SUMMARY: %d suites, %d passed, %d failed, %d skipped, TOTAL %d checks%s  (logs: %s)" % (
        len(results), len(results) - len(failed), len(failed), grand["skipped"], grand["total"],
        ("" if not unknown else "  (+%d suites without SUMMARY: %s)" % (len(unknown), ", ".join(unknown))),
        logdir))
    if skipped_suites and not strict:
        print("  注意：%s 里有 skip/缺失项；要把它当红跑，用 python hub/tests/run_all.py --strict" % ", ".join(skipped_suites))
    if strict and skipped_suites:
        print("  [strict] skip/缺失项被当成红：%s" % ", ".join(skipped_suites))
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
