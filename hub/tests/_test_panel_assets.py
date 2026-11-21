# -*- coding: utf-8 -*-
"""_test_panel_assets.py —— 面板入口与静态资源必须对得上（issue #2 回归）。

由来：发布 v1.1.0-rc.1 时同步脚本漏掉了 admin/index.html，仓库里的入口还停在
上一次构建的 hash（index-uYHCE4hp.js），而 static/ 已经换成新的（index-BDXy7Gvq.js）——
用户 clone 下来一打开就是两条 404，面板白屏。

这条测试不看构建能不能过，只看**盘上这一份**入口引用的每个 ./static/* 是否真的存在。
它跑在 hub 套件里，所以任何"只同步一半"的发布都会在闸门上红。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

ROOT = P.REPO
INDEX = os.path.join(ROOT, "admin", "index.html")
STATIC = os.path.join(ROOT, "admin", "static")


def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def main():
    html = read(INDEX)
    H.check("P01 admin/index.html 存在且非空", bool(html.strip()), INDEX)

    refs = re.findall(r"(?:src|href)=" + chr(34) + r"([^" + chr(34) + r"]+)" + chr(34), html)
    assets = [r for r in refs if r.startswith("./static/") or r.startswith("static/")]
    H.check("P02 入口至少引用一个 ./static/ 资源", len(assets) >= 1, str(refs[:6]))

    missing = []
    for rel in assets:
        name = rel.split("/")[-1]
        if not os.path.exists(os.path.join(STATIC, name)):
            missing.append(name)
    H.check("P03 入口引用的每个静态资源都真实存在（防 404）", not missing,
            "缺失：%s" % missing if missing else "")

    # 反向：static 里的入口 chunk 必须在 html 里被引用（否则是没清干净的旧构建）
    html_names = set(r.split("/")[-1] for r in assets)
    orphans = [n for n in (os.listdir(STATIC) if os.path.isdir(STATIC) else [])
               if n.startswith("index-") and n not in html_names]
    H.check("P04 static 里没有孤立的旧入口 chunk", not orphans, "孤立：%s" % orphans if orphans else "")

    H.finish()


if __name__ == "__main__":
    main()
