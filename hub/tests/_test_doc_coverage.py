# -*- coding: utf-8 -*-
"""_test_doc_coverage.py -- 工具与套件的名字必须出现在文档里（第 184 轮）。

由来：D 系列（第 77-82 轮）的文档插入用的是 t.replace(锚点, ...)，
锚点没匹配上时它**静默返回原文** —— 于是"写了"和"没写"看起来一模一样，
整整六轮的东西一个字都没进 docs。等发现时只能靠人眼。

这条判据把"文档有没有提"变成可判定的：hub/tools 下的每个脚本、
两侧的每个套件，名字都必须出现在 docs/merge/*.md 或 README-HUB.md 里。
新加一个套件却忘了写文档 → 直接红。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

ROOT = P.REPO
DOC_DIR = os.path.join(ROOT, "docs", "merge")
EXTRA_DOCS = [os.path.join(ROOT, "README-HUB.md")]


def doc_text():
    parts = []
    if os.path.isdir(DOC_DIR):
        for name in sorted(os.listdir(DOC_DIR)):
            if name.endswith(".md"):
                try:
                    with open(os.path.join(DOC_DIR, name), encoding="utf-8", errors="replace") as fh:
                        parts.append(fh.read())
                except OSError:
                    pass
    for extra in EXTRA_DOCS:
        try:
            with open(extra, encoding="utf-8", errors="replace") as fh:
                parts.append(fh.read())
        except OSError:
            pass
    return "\n".join(parts)


def names(folder, prefix, suffix):
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.startswith(prefix) and n.endswith(suffix))


def missing_from(text, items):
    return [n for n in items if n not in text]


def main():
    text = doc_text()
    H.check("C0 文档可读（docs/merge/*.md + README-HUB.md）", len(text) > 5000,
            "只读到 %d 字符" % len(text))

    tools = [n for n in names(os.path.join(ROOT, "hub", "tools"), "", ".py") if n != "__init__.py"]
    hub_tests = names(os.path.join(ROOT, "hub", "tests"), "_test_", ".py")
    # 第 12 轮：面板切到 React 工程，前端判据是 admin/app/src 下的 *.test.tsx；
    #   旧套件（admin/tests/*.js）整目录归档在 docs/backup/admin-legacy-20261021/tests。
    admin_tests = []
    _admin_src = os.path.join(ROOT, "admin", "app", "src")
    for _dp, _dn, _fs in os.walk(_admin_src):
        admin_tests.extend(f for f in _fs if f.endswith(".test.tsx"))

    H.check("C1 工具与套件数量合理", len(tools) >= 5 and len(hub_tests) >= 10 and len(admin_tests) >= 8,
            "tools=%d hub=%d admin=%d" % (len(tools), len(hub_tests), len(admin_tests)))

    mt = missing_from(text, tools)
    H.check("C2 hub/tools 下每个脚本都在文档里", not mt,
            "缺：" + ", ".join(mt[:6]) if mt else "")

    mh = missing_from(text, hub_tests)
    H.check("C3 每个 hub 套件都在文档里", not mh,
            "缺：" + ", ".join(mh[:6]) if mh else "")

    ma = missing_from(text, admin_tests)
    H.check("C4 每个 admin 套件都在文档里", not ma,
            "缺：" + ", ".join(ma[:6]) if ma else "")

    # 阳性对照：一个编造的文件名必须被判"缺"
    bogus = missing_from(text, ["definitely_not_a_real_suite.py"])
    H.check("C5 阳性对照：编造的名字必须判缺", bogus == ["definitely_not_a_real_suite.py"],
            "检测器失效：%s" % bogus)

    H.finish()


main()
