# -*- coding: utf-8 -*-
"""_test_engine_scan.py —— 后端侧「只枚举两家」扫描（第 148 轮）。

由来：第 147 轮在 aggregator.hub_usage 里发现

    return {"wb": per.get("wb"), "qd": per.get("qd"), "merged": merged}

循环用的是 ENGINES，返回却写死两家 —— 前端拿到的 /api/hub/usage 里**没有 trae**，
于是"网关调用量 1,210"的拆分提示只有两家（合计里有它，分项里没有）。
这类问题是"总数对、分项少一家"，肉眼几乎发现不了。

本套件只扫 **hub 自己的代码**（hub/core、hub/server.py、hub/__init__.py、hub/tools）；
hub/engines/** 是逐字节搬入的上游原件，里面有大量单引擎逻辑，不能算违规。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

ROOT = P.REPO
SKIP_DIRS = ("engines", "tests", "__pycache__")

# 三种写法（与前端 W90/W91 对齐，但用 Python 的字面量风格）
PATTERNS = [
    # 只在同一行内匹配：跨行匹配会把几十行之后的内容吃进来，
    # 第 148 轮第一版就这样误报了 4 处（都是不相干的代码）。
    ("字典只写两键", re.compile(r"""[{]\s*["']wb["']\s*:[^\n}]{0,140}["']qd["']\s*:""")),
    ("字典只写两键(反序)", re.compile(r"""[{]\s*["']qd["']\s*:[^\n}]{0,140}["']wb["']\s*:""")),
    ("成对列表", re.compile(r"""\[\s*["']wb["']\s*,\s*["']qd["']""")),
    ("双比较", re.compile(r"""==\s*["']wb["'][\s\S]{0,60}==\s*["']qd["']""")),
]


def strip_comments(text):
    """去掉注释，但**保留行数**（整段删除会让行号错位 —— 第 148 轮踩过：
    报出来的行号指向完全不相干的代码）。"""
    out = []
    in_doc = False
    for line in text.splitlines():
        if line.count('"""') % 2 == 1:
            in_doc = not in_doc
            out.append("")
            continue
        if in_doc or line.lstrip().startswith("#"):
            out.append("")
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def scan_text(text):
    """逐行扫（不跨行）：跨行正则会把几十行之后的内容吃进来。"""
    code = strip_comments(text)
    hits = []
    for idx, line in enumerate(code.split("\n"), start=1):
        for name, rx in PATTERNS:
            if not rx.search(line):
                continue
            # 同一行里出现 trae 就不算"只枚举两家"
            if "trae" in line:
                continue
            hits.append((name, idx))
    return hits

def iter_files():
    targets = []
    for base, dirs, files in os.walk(os.path.join(ROOT, "hub")):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                targets.append(os.path.join(base, f))
    return sorted(targets)


def main():
    files = iter_files()
    H.check("E0 扫描范围 = hub 自有代码（不含 engines/tests）", len(files) >= 5,
            "只找到 %d 个文件" % len(files))

    bad = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        for name, line in scan_text(text):
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            bad.append("%s:%d %s" % (rel, line, name))

    H.check("E1 后端没有「只枚举 wb+qd」的写法", not bad,
            "；".join(bad[:5]) if bad else "")

    # 阳性对照：喂坏样本必须红，喂注册表写法必须绿
    probe_bad = 'return {"wb": a, "qd": b, "merged": c}\n'
    probe_bad2 = 'targets = ["wb", "qd"]\n'
    probe_ok = 'out = {e: per.get(e) for e in ENGINES}\nout["merged"] = merged\n'
    probe_comment = '# 以前写 ["wb", "qd"]，现在按 ENGINES\n'
    # ---- E3-E5：端口参数与反代前缀必须由注册表生成 ----
    # 这两处是"加第四家时最容易漏"的地方：漏了不会报错，只会让新引擎一路 404 / 未启动。
    server_src = ""
    portal_src = ""
    for p in files:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if rel.endswith("hub/server.py"):
            server_src = open(p, "r", encoding="utf-8").read()
        elif rel.endswith("hub/core/portal.py"):
            portal_src = open(p, "r", encoding="utf-8").read()

    H.check("E3 server.py 的引擎端口参数由注册表生成",
            '--%s-port" % _name' in server_src and "for _name in ENGINES" in server_src,
            "没看到 '--%s-port' % _name 或 for _name in ENGINES")

    H.check("E4 portal.py 的 /api/<engine>/ 前缀按注册表分发",
            "for _name in ENGINES" in portal_src and "in ENGINES" in portal_src,
            "门户侧看不到按 ENGINES 遍历的前缀分发")

    # 反面：不能在代码里手写某个引擎的端口参数（docstring 已剥）
    hard = []
    for p in files:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if not rel.endswith(".py"):
            continue
        try:
            body = strip_comments(open(p, "r", encoding="utf-8").read())
        except Exception:
            continue
        # 只抓**注册参数的代码**（add_argument("--wb-port", ...)）。
        # 光抓字符串会把帮助文案里的示例也算进来 —— 第 149 轮实测：
        # 第一次写完立刻在多处 "如 --trae-port 8803" 这类提示里误报。
        for m in re.finditer(r'add_argument\(\s*"--(wb|qd|trae)-(port|args)"', body):
            hard.append("%s:%d %s" % (rel, body[:m.start()].count("\n") + 1, m.group(0)))
    H.check("E5 代码里没有手写 --<engine>-port（必须 %s 生成）", not hard,
            "；".join(hard[:4]) if hard else "")

    # ---- E6：测试里不许硬编码引擎指纹（第 188 轮）----
    # 形状断言必须从 ENGINE_META 派生；写死 "qoder-proxy" 之类，加第四家必然漏。
    import glob as _glob
    literal = []
    for pat in (os.path.join(ROOT, "hub", "tests", "_test_*.py"),
                os.path.join(ROOT, "admin", "tests", "_test_*.js")):
        for path_ in _glob.glob(pat):
            try:
                with open(path_, "r", encoding="utf-8", errors="replace") as fh:
                    body = strip_comments(fh.read())
            except OSError:
                continue
            if re.search(r"[\"'](qoder-proxy|trae2api-hub)[\"']", body):
                literal.append(os.path.basename(path_))
    H.check("E6 测试里没有硬编码引擎 service 指纹（必须走 ENGINE_META）",
            not literal, "；".join(literal[:5]))
    H.check("E6b 阳性对照：硬编码样本会被抓到",
            bool(re.search(r"[\"'](qoder-proxy|trae2api-hub)[\"']",
                           'x = h.get("service") == "qoder-proxy"')),
            "检测器失效")
    H.check("E2 阳性对照：坏样本判红、注册表写法与注释判绿",
            bool(scan_text(probe_bad)) and bool(scan_text(probe_bad2))
            and not scan_text(probe_ok) and not scan_text(probe_comment),
            "probe 结果与预期不符")

    # E5 的正则同样要自证：手写参数注册必须被抓到，帮助文案里的示例不许误报。
    arg_rx = re.compile(r'add_argument\(\s*"--(wb|qd|trae)-(port|args)"')
    H.check("E5b 阳性对照：手写 add_argument 判红、帮助文案判绿",
            bool(arg_rx.search('ap.add_argument("--wb-port", type=int)'))
            and not arg_rx.search('print("如 --trae-port 8803")'),
            "arg 正则失效")

    H.finish()


main()
