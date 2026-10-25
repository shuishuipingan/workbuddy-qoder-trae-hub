# -*- coding: utf-8 -*-
"""_test_static_names.py —— 门禁：融合层代码里不能有「运行时才炸的未定义名」。

为什么要有这条：第 38 轮给合并路由加"流已提交就不重试"的保护时，顺手写了
`log("...", level="WARN")` —— 而 portal.py 里**根本没有 log 这个函数**。
语法检查过得去、单测也没覆盖那条分支，于是线上真跑到那一刻才炸：
客户端已经拿到 200 和半个流，连接被 NameError 打断。

这条用标准库 symtable 做一次保守检查：凡是在函数里当**全局名**读、
模块顶层又从未绑定（赋值/导入/def/class）、也不是内置名的，一律报出来。
只查融合层自己的代码（hub/），vendored 引擎是上游代码不在此列。
"""
import builtins
import os
import symtable
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

ROOTS = ("hub/core", "hub/tools", "hub")
SKIP_DIRS = {"engines", "__pycache__", "tests", "static"}
BUILTINS = set(dir(builtins)) | {
    '__file__', '__name__', '__doc__', '__package__', '__spec__', '__loader__',
    '__builtins__', '__annotations__', '__dict__', '__path__',
}


def module_bindings(table):
    names = set()
    for sym in table.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            names.add(sym.get_name())
    return names


def global_reads(table, bound, out):
    for sym in table.get_symbols():
        name = sym.get_name()
        if sym.is_global() and not sym.is_assigned():
            if name not in bound and name not in BUILTINS:
                out.add(name)
    for child in table.get_children():
        global_reads(child, bound, out)


def scan(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        source = fh.read()
    table = symtable.symtable(source, path, "exec")
    bound = module_bindings(table)
    bad = set()
    global_reads(table, bound, bad)
    return sorted(bad)


targets = []
seen = set()
for root in ROOTS:
    base = os.path.join(P.HUB, os.path.relpath(root, "hub")) if root != "hub" else P.HUB
    if not os.path.isdir(base):
        continue
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.abspath(os.path.join(dirpath, name))
            if full in seen:
                continue
            seen.add(full)
            targets.append(full)

offenders = []
errors = []
for path in sorted(targets):
    try:
        bad = scan(path)
    except SyntaxError as exc:
        errors.append("%s: syntax error %s" % (os.path.basename(path), exc))
        continue
    if bad:
        offenders.append("%s: %s" % (os.path.relpath(path, P.REPO), ", ".join(bad)))

H.check("N01 融合层没有语法错误", not errors, "; ".join(errors[:3]))
H.check("N02 函数里读到的全局名都在模块里定义过（防 NameError）",
        not offenders,
        "\n".join(offenders[:6]) or "offenders=%d" % len(offenders))
H.check("N03 至少扫到了核心文件", len(targets) >= 8, "files=%d" % len(targets))
H.finish()
