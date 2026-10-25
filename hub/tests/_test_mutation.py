# -*- coding: utf-8 -*-
"""hub/tests/_test_mutation.py —— hub 侧的**变异测试**：注入违规，断言对应判据会红。

姊妹篇：admin/tests/_test_mutation.js（那边证的是前端扫描类判据）。
这里证的是 hub 侧最容易"永远为真"的三条：

  M4  S10 受控补丁在不在        —— 删掉 hub-patch 标记块，S10 必须红
  M5  S07c 默认端口              —— 把 DEFAULT_PORTS 改回 8789/8790，S07c 必须红
  M6  S04 引擎源码只允许补丁差异 —— 往 wb_proxy.py 加一行普通代码，S04 必须红

做法：把 hub/ 整树复制到临时目录（套件的路径全由 __file__ 推导，复制即隔离），
在副本里注入，然后用 HUB_TESTS_SIBLING 指回**真实**的上游原件目录，跑副本里的套件。
这样红的原因只能是"引擎被改了"，不会是"找不到原件"。

全程只碰临时副本；真实产物零改动。
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)                       # hub/
REPO = os.path.dirname(HUB)                       # 仓库根
SIBLING = os.path.dirname(REPO)                   # 合并damo/（上游原件在这）

sys.path.insert(0, HERE)
from _lib import harness as H                      # noqa: E402

SUITE = "_test_engine_static.py"

# 前置：引擎与 hub 包必须在。空树（X05 的 fixture）里这些是缺的 ——
# 那种情况下本套件应当 [SKIP]，而不是抛异常（"不许崩"是那个 meta 判据的底线）。
_NEED = [
    os.path.join(HUB, "__init__.py"),
    os.path.join(HUB, "engines", "wb", "wb_proxy.py"),
    os.path.join(HUB, "engines", "qd", "qoder_proxy.py"),
]
_missing = [p for p in _NEED if not os.path.exists(p)]
if _missing:
    H.skip("M 变异测试（前置缺失）", "缺 %d 个文件，例如 %s" % (len(_missing), os.path.basename(_missing[0])))
    H.finish()


def make_copy():
    tmp = tempfile.mkdtemp(prefix="hub-mut-")
    dst = os.path.join(tmp, "hub")
    shutil.copytree(HUB, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp, dst


def run_suite(root, hub_copy, suite=None):
    """跑**副本里**的套件；cwd=root，SIBLING 指回真实上游。"""
    suite = suite or SUITE
    env = dict(os.environ)
    env["HUB_TESTS_SIBLING"] = SIBLING
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, os.path.join(hub_copy, "tests", suite)],
        cwd=root, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


_SHARED = {"tmp": None, "hub": None}


def _shared_copy():
    """整个套件**只复制一次** hub 树。

    第一版每例复制一份（3 次 copytree，含 engines 与 umid 快照），实测 13s；
    改成"一次复制 + 每例备份/还原被改的文件"后只剩一次 I/O，慢机器上差别更明显。
    """
    if _SHARED["tmp"] is None:
        _SHARED["tmp"], _SHARED["hub"] = make_copy()
    return _SHARED["tmp"], _SHARED["hub"]


def mutate(label, inject, expect_id, suite=None):
    tmp, hub_copy = _shared_copy()
    backup = {}
    try:
        try:
            note = inject(hub_copy, backup)
        except Exception as exc:  # noqa: BLE001
            H.check(label, False, "注入失败: %r" % (exc,))
            return
        code, out = run_suite(tmp, hub_copy, suite)
        failed = [ln for ln in out.splitlines() if "[FAIL]" in ln]
        hit = any(expect_id in ln for ln in failed)
        H.check(label, code != 0 and hit,
                "exit=%s 命中=%s 失败行=%s" % (code, hit, " | ".join(failed[:2]) or "(无)"))
        if note:
            H.info("%s · %s" % (label.split()[0], note))
    finally:
        # 还原被改的文件（下一次变异在干净副本上做）—— backup 存的是**原始文本**
        for rel, original in backup.items():
            with open(os.path.join(hub_copy, rel), "w", encoding="utf-8") as fh:
                fh.write(original)


def _read(hub_copy, rel):
    with open(os.path.join(hub_copy, rel), encoding="utf-8") as fh:
        return fh.read()


def _write(hub_copy, rel, text):
    with open(os.path.join(hub_copy, rel), "w", encoding="utf-8") as fh:
        fh.write(text)


def _drop_patch_block(hub_copy, backup):
    rel = os.path.join("engines", "wb", "wb_proxy.py")
    text = _read(hub_copy, rel)
    backup[rel] = text
    out, inside = [], False
    for line in text.splitlines(True):
        s = line.strip()
        if s.startswith("# >>> hub-patch:"):
            inside = True
            continue
        if s.startswith("# <<< hub-patch:"):
            inside = False
            continue
        if not inside:
            out.append(line)
    _write(hub_copy, rel, "".join(out))
    return "已删除 hub-patch 标记块"


def _restore_old_ports(hub_copy, backup):
    rel = "__init__.py"
    text = _read(hub_copy, rel)
    backup[rel] = text
    # 第 100 轮：端口从 ENGINE_META 派生，DEFAULT_PORTS 那行已经不存在了。
    # 改注册表里的 wb 端口，效果等价（S07c 仍然必须红）。
    if '"port": 8811' not in text:
        raise RuntimeError("注册表里找不到 wb 的 8811 端口，M5 需同步")
    _write(hub_copy, rel, text.replace('"port": 8811', '"port": 8789', 1))
    return "已把 wb 默认端口改回 8789"


def _add_plain_line(hub_copy, backup):
    rel = os.path.join("engines", "wb", "wb_proxy.py")
    text = _read(hub_copy, rel)
    backup[rel] = text
    _write(hub_copy, rel, text + "\n# 变异测试注入的一行普通改动（不在受控补丁块内）\nMUTATION_MARK = 1\n")
    return "已在 wb_proxy.py 末尾追加一行非补丁代码"


def _break_server(hub_copy, backup):
    rel = "server.py"
    text = _read(hub_copy, rel)
    backup[rel] = text
    _write(hub_copy, rel, text + "\ndef _broken(:\n")
    return "已给 hub/server.py 注入语法错误"


try:
    mutate("M4 删掉 hub-patch 标记块 → S10 变红（受控补丁那条真的在看）",
           _drop_patch_block, "S10 wb 引擎带 dashboard-redirect")
    mutate("M5 把 wb 默认端口改回 8789 → S07c 变红（默认端口那条真的在看）",
           _restore_old_ports, "S07c 默认端口")
    mutate("M6 往 wb_proxy.py 加一行普通代码 → S04 变红（源码完整性那条真的在看）",
           _add_plain_line, "S04 wb 引擎")
    # M8：server.py 一坏，门户套件必须**报红**，而不是"探测不到 → 当没落地 → 整片跳过"。
    #    这条守的正是本轮修掉的那个宽捕获。
    mutate("M8 弄坏 server.py → 门户套件报 P01b 红（不再静默跳过）",
           _break_server, "P01b 探测 --portal-port 失败", suite="_test_portal.py")
finally:
    if _SHARED["tmp"]:
        shutil.rmtree(_SHARED["tmp"], ignore_errors=True)

H.finish()
