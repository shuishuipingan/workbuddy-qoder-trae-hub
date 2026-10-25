# -*- coding: utf-8 -*-
"""_test_dead_symbols.py —— hub/core 里不许有「定义了却没人调用」的公开函数。

由来（第 59 轮）：aggregator.forget_token() 存在了很久，却**没有任何调用点**。
它不是无用的代码，而是**看起来已经装了的一道防线**：注释写着「换密码、引擎重启后调用」，
读代码的人会以为这件事已经被处理；实际上缓存里的旧令牌永远不会被丢掉，
用户改了面板密码就只会看到一整片 401。

范围与口径：
  · 只看 hub/core/*.py 里 **模块级、不以 _ 开头**的函数（私有工具函数允许只在本地用）；
  · 引用统计覆盖 hub/ 下所有 .py（含 tests、tools），**排除 engines/**（那是上游原件的副本）；
  · 出现次数 <= 1（只有定义本身）即判为死符号。

已知边界（写清楚，免得判据被误解）：一个只被测试引用的函数会被算作「有引用」。
本判据不区分「产品代码引用」与「测试引用」—— 宁可漏报，不要因为测试写法差异误红。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(")


def py_files(root):
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("engines", "__pycache__", ".git")]
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.join(base, name))
    return out


def definitions(sources):
    """[(rel, name)] —— 只收模块级、非下划线开头的 def。"""
    found = []
    for rel, src in sources:
        for line in src.splitlines():
            m = DEF_RE.match(line)
            if m:
                found.append((rel, m.group(1)))
    return found


def dead_names(defs, sources):
    """出现次数 <= 1（只有定义）的公开函数。"""
    out = []
    for rel, name in defs:
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        uses = 0
        for _rel2, src in sources:
            uses += len(pattern.findall(src))
        if uses <= 1:
            out.append("%s -> %s" % (rel, name))
    return out


core = os.path.join(P.HUB, "core")
if not os.path.isdir(core):
    H.skip("D00 hub/core 尚未落地 —— 死符号判据整体跳过", core)
    H.finish()

sources = []
for f in py_files(P.HUB):
    rel = os.path.relpath(f, P.REPO).replace(os.sep, "/")
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            sources.append((rel, fh.read()))
    except OSError:
        continue

core_sources = [(rel, src) for rel, src in sources if rel.startswith("hub/core/")]
defs = definitions(core_sources)

H.check("D01 扫到了 hub/core 的真实源码（防路径写错导致真空通过）",
        len(core_sources) >= 3 and len(defs) >= 10,
        "文件=%d 函数=%d" % (len(core_sources), len(defs)))

dead = dead_names(defs, sources)
H.check("D02 hub/core 没有「定义了却没人调用」的公开函数（%d 个函数）" % len(defs),
        not dead, " | ".join(dead[:6]))

# ---- D03 阳性对照：同一套检测喂合成样本，必须只报「没人调用」的那一个 ----
PROBE_A = "def used():\n    return 1\n\n\ndef never_called():\n    return 2\n"
PROBE_B = "from x.a import used\n\nused()\n"
probe = [("x/a.py", PROBE_A), ("x/b.py", PROBE_B)]
probe_dead = dead_names(definitions(probe), probe)
H.check("D03 阳性对照：合成样本里只报 never_called",
        len(probe_dead) == 1 and "never_called" in probe_dead[0],
        "probe=%r" % (probe_dead,))

# ---- D04 模块级常量同样不许「定义了没人读」 ----
# 由来（第 61 轮）：aggregator.REWARD_FIELDS 的注释写着「视图可以照这个列表断言存在性」，
# 却没有任何地方读它 —— 规格写在纸上，代码里另抄一份字面量。同一类病。
CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*=")


def const_names(sources):
    found = []
    for rel, src in sources:
        for line in src.splitlines():
            m = CONST_RE.match(line)
            if m:
                found.append((rel, m.group(1)))
    return found


consts = const_names(core_sources)
dead_consts = dead_names(consts, sources)
H.check("D04 hub/core 的模块级常量都被读过（%d 个）" % len(consts),
        not dead_consts, " | ".join(dead_consts[:6]))

PROBE_C = "MAX_ROWS = 100" + chr(10) + "UNUSED_LIMIT = 5" + chr(10)
PROBE_D = "print(MAX_ROWS)" + chr(10)
probe_c = [("x/c.py", PROBE_C), ("x/d.py", PROBE_D)]
probe_c_dead = dead_names(const_names(probe_c), probe_c)
H.check("D05 阳性对照：合成样本里只报 UNUSED_LIMIT",
        len(probe_c_dead) == 1 and "UNUSED_LIMIT" in probe_c_dead[0],
        "probe=%r" % (probe_c_dead,))

H.finish()
