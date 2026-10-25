# -*- coding: utf-8 -*-
"""hub/tests/_lib/paths.py —— 全部路径基于 __file__ 推导（照 tests/README 第 1 条纪律：不用 cwd）。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))        # hub/tests/_lib
TESTS = os.path.dirname(HERE)                            # hub/tests
HUB = os.path.dirname(TESTS)                             # hub/
REPO = os.path.dirname(HUB)                              # qoder-proxy/
# SIBLING（上游原件所在目录）允许被覆盖：给「变异测试」用 ——
# 把 hub/ 复制到临时目录后，原件仍在真实位置，这样 S04/S05 才会因为"引擎被改"而红，
# 而不是因为"找不到原件"而红（后者是假红，证明不了判据在起作用）。
SIBLING = os.environ.get("HUB_TESTS_SIBLING") or os.path.dirname(REPO)   # 合并damo/

WB_ENGINE = os.path.join(HUB, "engines", "wb")
QD_ENGINE = os.path.join(HUB, "engines", "qd")
TRAE_ENGINE = os.path.join(HUB, "engines", "trae")          # 第 110 轮：第三家
ENGINES = {"wb": WB_ENGINE, "qd": QD_ENGINE, "trae": TRAE_ENGINE}
ENTRY = {"wb": "wb_proxy.py", "qd": "qoder_proxy.py", "trae": "trae_proxy.py"}

# 两个引擎的「原件」：搬入前的来源路径（静态契约要逐字节比对）
ORIGIN = {
    "wb": os.path.join(SIBLING, "wb"),                    # 合并damo/wb
    "qd": REPO,                                           # qoder-proxy 仓根
    "trae": os.path.join(SIBLING, "trae2api-hub"),        # 合并damo/trae2api-hub
}
ADMIN_INDEX = os.path.join(REPO, "admin", "index.html")


def engine_files(engine):
    """引擎目录下的源文件（*.py），按名字排序。"""
    d = ENGINES[engine]
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d) if n.endswith(".py"))


def exists(path):
    return os.path.exists(path)
