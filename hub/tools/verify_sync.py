# -*- coding: utf-8 -*-
"""hub/tools/verify_sync.py —— 三家引擎「和上游一致吗」的体检（第 110 轮）。

为什么需要它：S04/S05/S08 是**测试套件**里的判据，跑一次要起一整套；
而"我同步完上游了吗"是随时会问的一句话。这个工具只做一件事：

    把 hub/engines/<e>/ 与上游原件逐字节比对，剥掉 hub-patch 块再比。

口径与静态判据完全一致（同一对标记、同一种剥离），所以两者不会各说各话。

用法：
    python hub/tools/verify_sync.py              # 体检三家
    python hub/tools/verify_sync.py --engine trae
    python hub/tools/verify_sync.py --json

退出码：0 = 全部一致；1 = 有漂移（列出文件）。
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # hub/tools
HUB = os.path.dirname(HERE)                                # hub/
REPO = os.path.dirname(HUB)                                # 融合仓根
SIBLING = os.path.dirname(REPO)                            # 合并damo/

PATCH_OPEN = "# >>> hub-patch:"
PATCH_CLOSE = "# <<< hub-patch:"

#: 引擎 → (vendored 目录, 上游原件目录, 入口文件名)
ENGINES = {
    "wb": (os.path.join(HUB, "engines", "wb"), os.path.join(SIBLING, "wb"), "wb_proxy.py"),
    "qd": (os.path.join(HUB, "engines", "qd"), REPO, "qoder_proxy.py"),
    "trae": (os.path.join(HUB, "engines", "trae"), os.path.join(SIBLING, "trae2api-hub"), "trae_proxy.py"),
}

#: 除了 *.py，还要比对的非源码文件（各引擎自带的数据/前端）
EXTRA = {
    "wb": ("dashboard.html", "pricing/pricing.json", "baseprompt.json"),
    "qd": ("dashboard.html", "baseprompt.json"),
    "trae": ("trae_catalog_cn.json", "trae_catalog_intl.json", "dashboard.html", "pricing/trae_pricing.json"),
}


def strip_hub_patches(text):
    """剥掉受控补丁块 —— 与 _test_engine_static.py 同一口径。"""
    out, inside = [], False
    for line in text.splitlines(True):
        s = line.strip()
        if s.startswith(PATCH_OPEN):
            inside = True
            continue
        if s.startswith(PATCH_CLOSE):
            inside = False
            continue
        if not inside:
            out.append(line)
    return "".join(out)


def digest(path, strip):
    with open(path, "rb") as fh:
        raw = fh.read()
    if not strip:
        return hashlib.md5(raw).hexdigest()
    text = raw.decode("utf-8", "replace")
    return hashlib.md5(strip_hub_patches(text).encode("utf-8")).hexdigest()


def compare(engine):
    vendor, origin, entry = ENGINES[engine]
    report = {"engine": engine, "vendor": vendor, "origin": origin,
              "same": [], "diff": [], "absent": [], "missing_entry": False}
    if not os.path.isdir(origin):
        report["origin_missing"] = True
        return report
    if not os.path.exists(os.path.join(vendor, entry)):
        report["missing_entry"] = True
        return report

    names = sorted(n for n in os.listdir(vendor) if n.endswith(".py"))
    for rel in list(names) + list(EXTRA.get(engine, ())):
        a = os.path.join(vendor, rel.replace("/", os.sep))
        b = os.path.join(origin, rel.replace("/", os.sep))
        if not os.path.exists(a):
            continue                      # 引擎多出来的文件不算漂移（vendored 可以更全）
        if not os.path.exists(b):
            report["absent"].append(rel)
            continue
        strip = rel.endswith((".py", ".html"))
        if digest(a, strip) == digest(b, strip):
            report["same"].append(rel)
        else:
            report["diff"].append(rel)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="三家引擎与上游的一致性体检")
    ap.add_argument("--engine", action="append", choices=sorted(ENGINES),
                    help="只查某一家（可重复）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    wanted = args.engine or sorted(ENGINES)
    reports = [compare(e) for e in wanted]
    ok = all(not r["diff"] and not r["absent"] and not r.get("origin_missing")
             and not r.get("missing_entry") for r in reports)

    if args.json:
        print(json.dumps({"ok": ok, "engines": reports}, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    for r in reports:
        name = r["engine"]
        if r.get("origin_missing"):
            print("[%s] 找不到上游原件：%s" % (name, r["origin"]))
            continue
        if r.get("missing_entry"):
            print("[%s] vendored 目录里没有入口文件" % name)
            continue
        print("[%s] 相同 %d 个" % (name, len(r["same"])))
        for rel in r["diff"]:
            print("     ✗ 内容不同：%s" % rel)
        for rel in r["absent"]:
            print("     ? 上游没有这个文件：%s" % rel)
    print("")
    print("RESULT: " + ("三家与上游一致（仅 hub-patch 块内允许差异）" if ok else "存在漂移，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
