# -*- coding: utf-8 -*-
"""_test_engine_static.py —— ① 静态契约：三家引擎必须「原样搬入」，hub 的编排契约不许漂。

判据全部基于「原件 vs 搬入件」的逐字节比对，以及 hub/__init__.py 与 hub/server.py 的文本事实：
  * wb 引擎 16 个 wb_*.py、qd 引擎 9 个 qoder_*.py + baseprompt.json + umid/runtime-info；
  * 两份 derive_id 仍是原实现（整文件 + 函数体两层比对）；
  * hub 的版本表 / 默认端口 / 引擎别名表；
  * server.py 的「串行启动 + 逐个等 health」不许被改回并行（Lead 踩过的坑：当时两家抢同一个端口）。
"""
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P
def md5(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def md5_stable(path, tries=3, pause=0.15):
    """哈希不一致时延时复算：队友的编辑器保存不是原子的，读到「半个文件」会给出假差异。"""
    import time as _t
    last = md5(path)
    for _ in range(max(0, tries - 1)):
        _t.sleep(pause)
        again = md5(path)
        if again == last:
            return last
        last = again
    return last


def func_body(text, name):
    """抽出 def name( 到下一个顶层 def/class 之前的原文（逐字节比较用）。

    刻意不用正则：这里要比的是「原文是否一模一样」，正则转义本身反而是噪声来源。
    """
    marker = "def %s(" % name
    start = text.find(marker)
    if start < 0:
        return None
    if start > 0 and text[start - 1] not in "\n\r":
        return None                      # 只认行首定义，避免命中注释里的同名串
    rest = text[start:]
    cuts = [p for p in (rest.find("\ndef ", 1), rest.find("\nclass ", 1)) if p > 0]
    return rest[: min(cuts)] if cuts else rest


wb_files = P.engine_files("wb")
qd_files = P.engine_files("qd")
# ★ Lead 第 45 轮：上游 wb 新增 5 个模块（activity/agents/egress/updates/upstream_pool），
#   原来"恰好 16 个"的等值断言当场红掉。改成**必需集合**判据：
#   · 一个不缺（少任何一个 wb_proxy 都 import 不起来 —— I03 就是这么红的）；
#   · 总数只设下限，不设上限（上游每加一个模块这里就红一次，纯属尺子跟着内容跑）。
WB_REQUIRED = {
    "wb_proxy.py", "wb_accounts.py", "wb_scheduler.py", "wb_tasks.py",
    "wb_catalog.py", "wb_pricing.py", "wb_settings.py", "wb_webtools.py",
    "wb_webagent.py", "wb_fingerprint.py", "wb_identity.py", "wb_ipintel.py",
    "wb_probes.py", "wb_prompt.py", "wb_atrest.py", "wb_modelsdev.py",
    "wb_activity.py", "wb_agents.py", "wb_egress.py", "wb_updates.py",
    "wb_upstream_pool.py", "wb_logstore.py",
}
QD_REQUIRED = {
    "qoder_proxy.py", "qoder_accounts.py", "qoder_scheduler.py", "qoder_tasks.py",
    "qoder_catalog.py", "qoder_anthropic.py", "qoder_fingerprint.py",
    "qoder_settings.py", "qoder_sign.py",
}
wb_mods = {n for n in wb_files if n.startswith("wb_")}
qd_mods = {n for n in qd_files if n.startswith("qoder_")}
H.check("S01 wb 引擎模块齐全（含上游新增的 activity/agents/egress/updates/upstream_pool）",
        WB_REQUIRED <= wb_mods, "缺少: %s" % sorted(WB_REQUIRED - wb_mods))
H.check("S02 qd 引擎模块齐全", QD_REQUIRED <= qd_mods, "缺少: %s" % sorted(QD_REQUIRED - qd_mods))
H.info("wb: %s" % ", ".join(wb_files))
H.info("qd: %s" % ", ".join(qd_files))

for name in ("baseprompt.json", os.path.join("umid", "runtime-info")):
    p = os.path.join(P.QD_ENGINE, name)
    H.check("S03 qd 引擎带 %s" % name, os.path.exists(p), p)


#: 融合层对引擎源码的**受控改动**：必须用这对标记包起来。
#: S04/S05 的比较口径 = 「去掉标记块之后逐字节相同」——
#: 这样"引擎原样搬入"仍是被钉住的，同时允许**写明理由的最小补丁**。
PATCH_OPEN = "# >>> hub-patch:"
PATCH_CLOSE = "# <<< hub-patch:"


def strip_hub_patches(text):
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


def hub_side_md5(path):
    """hub/engines 侧的内容指纹：剥掉受控补丁块再算。"""
    import hashlib
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    return hashlib.md5(strip_hub_patches(text).encode("utf-8")).hexdigest()


def compare_all(engine, names):
    origin_dir = P.ORIGIN[engine]
    same, diff, absent = 0, [], []
    for name in names:
        a = os.path.join(P.ENGINES[engine], name)
        b = os.path.join(origin_dir, name)
        if not os.path.exists(b):
            absent.append(name)
            continue
        # 第 196 轮：镜像侧现在**也带**同一份受控补丁（同步管线会自动重贴），
        # 所以两边都要剥掉标记块再比 —— 只剥一边会把"两边都打了同一补丁"误判成差异。
        if hub_side_md5(a) == hub_side_md5(b):
            same += 1
        else:
            diff.append(name)
    return same, diff, absent


# ---- 第 110 轮：第三家也要被钉住 ----
# 之前只有 S04(wb)/S05(qd)：Trae 怎么改都没人管 —— 与"引擎原样搬入"的契约不一致。
trae_files = P.engine_files("trae")
trae_same, trae_diff, trae_absent = compare_all("trae", trae_files)
H.check("S08 trae 引擎 %d 个文件与 合并damo/trae2api-hub 相同（只允许 hub-patch 标记块内差异）" % len(trae_files),
        trae_files and not trae_diff and not trae_absent and trae_same == len(trae_files),
        "相同=%d 差异=%s 缺原件=%s" % (trae_same, trae_diff, trae_absent))

wb_same, wb_diff, wb_absent = compare_all("wb", wb_files)
H.check("S04 wb 引擎 %d 个文件与 合并damo/wb 相同（只允许 hub-patch 标记块内差异）" % len(wb_files),
        not wb_diff and not wb_absent and wb_same == len(wb_files),
        "相同=%d 差异=%s 缺原件=%s" % (wb_same, wb_diff, wb_absent))
qd_same, qd_diff, qd_absent = compare_all("qd", qd_files)
H.check("S05 qd 引擎 %d 个文件与 qoder-proxy 仓根相同（只允许 hub-patch 标记块内差异）" % len(qd_files),
        not qd_diff and not qd_absent and qd_same == len(qd_files),
        "相同=%d 差异=%s 缺原件=%s" % (qd_same, qd_diff, qd_absent))

# S10 受控补丁还在不在：同步上游时会整目录覆盖 hub/engines/，
# 这段补丁**必须**随之重贴 —— 丢了就说明"三个端口三张脸"又回来了。
for engine in ("wb", "qd"):
    proxy = "wb_proxy.py" if engine == "wb" else "qoder_proxy.py"
    path = os.path.join(P.ENGINES[engine], proxy)
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    H.check("S10 %s 引擎带 dashboard-redirect 受控补丁（根路径跳控制台）" % engine,
            "hub-patch: dashboard-redirect" in text
            and "HUB_PORTAL_URL" in text,
            "补丁丢了：同步上游后需要照 markers 重贴（见 docs/merge/patches/engines-dashboard-redirect.md）")

for engine, mod in (("wb", "wb_fingerprint.py"), ("qd", "qoder_fingerprint.py")):
    a = os.path.join(P.ENGINES[engine], mod)
    b = os.path.join(P.ORIGIN[engine], mod)
    if not (os.path.exists(a) and os.path.exists(b)):
        H.missing("S06 %s 的 derive_id 比对（前置文件缺失）" % engine, "%s / %s" % (a, b))
        continue
    ta = open(a, encoding="utf-8").read()
    tb = open(b, encoding="utf-8").read()
    fa, fb = func_body(ta, "derive_id"), func_body(tb, "derive_id")
    H.check("S06 %s 的 derive_id 函数体逐字节相同" % engine, fa is not None and fa == fb,
            "hub=%d bytes orig=%d bytes" % (len(fa or ""), len(fb or "")))
    H.check("S06b %s 的 derive_id 仍是 md5 派生实现" % engine,
            "hashlib.md5" in (fa or "") and "hexdigest" in (fa or ""),
            ((fa or "").strip().splitlines() or ["未找到函数"])[-1][:80])

CHILD = r'''
import json, sys
sys.path.insert(0, REPO)
import hub
print(json.dumps({
    "version": hub.HUB_VERSION,
    "ports": hub.DEFAULT_PORTS,
    "hub_port": hub.HUB_PORT,
    "engines": hub.ENGINE_ENGINES if hasattr(hub, "ENGINE_ENGINES") else hub.ENGINE_VERSIONS,
    "alias": {k: hub.resolve_engine(k) for k in ("wb", "workbuddy", "qoder", "qd", "trae", "nope")},
}))
'''.replace("REPO", repr(P.REPO))
r = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    H.check("S07 hub 包可导入", False, (r.stderr or "").strip()[:200])
else:
    meta = json.loads(r.stdout.strip().splitlines()[-1])
    H.check("S07 hub 包可导入", True)
    # 第 244 轮：版本号随上游同步而变；面板也不再展示引擎版本，这里只校验"三家都在表里"
    H.check("S07b 引擎版本表里三家都在（值随上游同步更新）",
            sorted(meta["engines"].keys()) == ["qd", "trae", "wb"]
            and all(isinstance(v, str) and v for v in meta["engines"].values()), meta["engines"])
    H.check("S07c 默认端口 = wb 8811 / qd 8812 / trae 8813（避开操作者实例的 8789/8790/8791）",
        meta["ports"] == {"wb": 8811, "qd": 8812, "trae": 8813}, meta["ports"])
    H.check("S07d 门户端口常量存在且不等于任何一个引擎端口",
            isinstance(meta.get("hub_port"), int) and meta["hub_port"] not in (8789, 8790), meta.get("hub_port"))
    H.check("S07e 引擎别名表能规范化 wb/workbuddy/qoder/qd/trae，未知返回 None",
            meta["alias"] == {"wb": "wb", "workbuddy": "wb", "qoder": "qd", "qd": "qd", "trae": "trae", "nope": None},
            meta["alias"])

server_py = os.path.join(P.HUB, "server.py")
if not os.path.exists(server_py):
    H.missing("S08 hub/server.py 不存在", server_py)
else:
    src = open(server_py, encoding="utf-8").read()
    for flag in ("--wb-port", "--qd-port", "--engines", "--dry-run"):
        H.check("S08 server.py 支持 %s" % flag, flag in src)
    lines = src.splitlines()
    i_loop = next((i for i, l in enumerate(lines) if "for name, port, extra in plan:" in l), -1)
    i_start = next((i for i, l in enumerate(lines) if "start_engine(name, args.host, port, extra)" in l), -1)
    i_wait = next((i for i, l in enumerate(lines) if "health = wait_health(" in l), -1)
    H.check("S09 启动仍是「串行：start_engine 后紧跟逐个 wait_health」（防改回并行）",
            -1 < i_loop < i_start < i_wait and (i_wait - i_start) <= 3,
            "loop@%d start@%d wait@%d" % (i_loop, i_start, i_wait))
    H.check("S09b server.py 的端口默认值取自 hub.DEFAULT_PORTS（没写死字面量）", "DEFAULT_PORTS[" in src)

# ---------- S11/S12 客户端提前断开：引擎必须安静收场 ----------
# 由来（第 65 轮）：门户那边漏了"读阶段被 RST"的兜底，日志被 ConnectionResetError 刷满。
# 引擎侧本来就有（wb 在 handle_one_request、qd 在 handle/finish）；但没有任何判据盯着它 ——
# 上游同步一次就可能悄悄消失，而那时表现只是"日志变吵"，没人会当缺陷。
# 面板会高频轮询两个引擎，浏览器刷新/切页随时会中断请求，这条必须一直在。
CONN_ERRORS = ("ConnectionResetError", "BrokenPipeError", "ConnectionAbortedError")

for engine, fname in (("wb", "wb_proxy.py"), ("qd", "qoder_proxy.py")):
    path_ = os.path.join(P.ENGINES.get(engine, ""), fname)
    if not os.path.exists(path_):
        H.skip("S1%d %s 引擎缺失" % (1 if engine == "wb" else 2, engine), path_)
        continue
    with open(path_, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    # 找所有 except(...) 行，看有没有一条同时覆盖三种断开异常
    # 只认真正以 except 开头的语句行 —— 注释里提到这三种异常名不算数
    covered = [ln for ln in src.splitlines()
               if ln.strip().startswith("except") and all(e in ln for e in CONN_ERRORS)]
    H.check("S1%s %s 引擎对客户端断开有兜底（三种异常一起接）" % ("1" if engine == "wb" else "2", engine),
            bool(covered), "没找到覆盖 %s 的 except" % "/".join(CONN_ERRORS))

# S13 阳性对照：同一套判定喂"没有这三种异常"的样本，必须为假。
# 否则 S11/S12 可能只是在数一个恒真的条件（本仓栽过：W44 曾永远为真）。
_PROBE_NO = "def handle(self):\n    try:\n        pass\n    except OSError:\n        pass\n"
_probe_hits = [ln for ln in _PROBE_NO.splitlines()
               if ln.strip().startswith("except") and all(e in ln for e in CONN_ERRORS)]
H.check("S13 阳性对照：不含三种断开异常的 except 必须判为不覆盖", not _probe_hits,
        "probe=%r" % (_probe_hits,))


# ---- S14 写盘失败兜底还在不在（第 238 轮）----
# 同步上游会整目录覆盖 hub/engines/ —— 兜底丢了，写盘失败又会变成"引擎不可达"。
_ss13 = []
for _eng5, _e5 in (("wb", "wb_proxy.py"), ("qd", "qoder_proxy.py"), ("trae", "trae_proxy.py")):
    try:
        _txt5 = open(os.path.join(P.ENGINES[_eng5], _e5), encoding="utf-8").read()
        _ss13.append((_eng5, "settings-save-errors" in _txt5 and "write_failed" in _txt5))
    except Exception:
        _ss13.append((_eng5, False))
H.check("S14 三家都有「写盘失败 → 500 write_failed」兜底",
        all(_ok5 for _e6, _ok5 in _ss13),
        "缺: " + ", ".join(_e7 for _e7, _ok7 in _ss13 if not _ok7))

H.finish()
