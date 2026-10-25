# -*- coding: utf-8 -*-
"""_test_aggregator.py —— ④ 聚合器单元：引擎不可达时必须「返回 None / 部分结果」，不许抛。

为什么用反射而不是写死函数名：聚合器的 API 由团子（task-11）定，契约书只给了行为要求。
判据 = 对每个公开函数喂一个**确定连不上**的目标，看它抛不抛：
  * 抛 TypeError / AttributeError(.get/.items/.keys) → 是我的形状猜错了（记 SKIP，不冤枉他）；
  * 抛别的（URLError / ConnectionRefused / OSError / 超时…）→ **判红**，因为「引擎不可达」
    是融合层的一等状态，必须变成返回值；
  * 不抛 → 再看返回值形态，把实际形状打出来（None / dict / list / str）。

参数形状的猜测规则（第一候选定裁决，其余候选只记 INFO）：
  ports/engines/urls 这类复数名 → 先试 {"wb": p, "qd": p}（hub 的端口就是按引擎配的字典）；
  port 单数 → 先试 int；url/base/host → 先试 "http://127.0.0.1:p"；engine/name → 先试 "wb"。
  实测教训（2026-10-09）：hub_health/engine_summary 收的是**字典**，先按 int 喂会得到
  AttributeError: int has no attribute get —— 那是判据猜错，不是产品缺陷，所以规则要能自我纠偏。
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

AGG = os.path.join(P.HUB, "core", "aggregator.py")
CORE_DIR = os.path.join(P.HUB, "core")

core_files = sorted(n for n in (os.listdir(CORE_DIR) if os.path.isdir(CORE_DIR) else []) if n.endswith(".py"))
H.info("hub/core/ 现有: %s" % (", ".join(core_files) if core_files else "(空)"))

if not os.path.exists(AGG):
    H.skip("A01 hub/core/aggregator.py 尚未落地（团子 task-11）——聚合器判据整体跳过", AGG)
    H.finish()

closed_port = H.free_port()          # 故意不监听：连它就是「引擎不可达」

CHILD = r'''
import inspect, json, sys
sys.path.insert(0, REPO)
P = int(sys.argv[1])
out = {"import_error": None, "funcs": []}
try:
    from hub.core import aggregator
except Exception as exc:
    out["import_error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out))
    raise SystemExit(0)

PLURAL = ("ports", "engines", "urls", "endpoints", "targets")
SINGULAR = ("port",)
URLISH = ("url", "base", "host", "endpoint", "address")
ENGINEISH = ("engine", "name", "which")
MISMATCH = ("has no attribute \x27get\x27", "has no attribute \x27items\x27", "has no attribute \x27keys\x27",
            "not subscriptable", "unhashable")


def candidates(pname):
    n = pname.lower()
    if any(n == k or n.endswith(k) for k in PLURAL):
        return [[{"wb": P, "qd": P}], [P], ["http://127.0.0.1:%d" % P]]
    if any(n == k for k in SINGULAR):
        return [[P], [{"wb": P, "qd": P}]]
    if any(k in n for k in URLISH):
        return [["http://127.0.0.1:%d" % P]]
    if any(k in n for k in ENGINEISH):
        return [["wb"]]
    return []


def is_mismatch(exc):
    text = str(exc)
    return isinstance(exc, TypeError) or any(m in text for m in MISMATCH)


for name, fn in inspect.getmembers(aggregator, inspect.isfunction):
    if name.startswith("_"):
        continue
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        continue
    params = [p for p in sig.parameters.values()
              if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty]
    entry = {"name": name, "signature": str(sig), "called_with": None, "ok": None,
             "error": None, "return_kind": None, "alternates": []}
    if len(params) > 1:
        entry["called_with"] = "(跳过：%d 个必填参数)" % len(params)
        out["funcs"].append(entry)
        continue
    cands = [[]] if not params else candidates(params[0].name)
    if not cands:
        entry["called_with"] = "(跳过：参数名 %s 猜不出语义)" % params[0].name
        out["funcs"].append(entry)
        continue
    primary = cands[0]
    entry["called_with"] = repr(primary)
    for i, args in enumerate(cands):
        try:
            value = fn(*args)
            if i == 0:
                entry["ok"] = True
                entry["return_kind"] = type(value).__name__
                entry["empty_ish"] = value is None or (hasattr(value, "__len__") and len(value) == 0)
                entry["sample"] = repr(value)[:120]
            else:
                entry["alternates"].append("候选%d(%r) -> ok（%s）" % (i, args, type(value).__name__))
            break
        except Exception as exc:
            if i == 0:
                if is_mismatch(exc):
                    entry["ok"] = None
                    entry["error"] = "形状不匹配（判据猜错）：%s: %s" % (type(exc).__name__, exc)
                else:
                    entry["ok"] = False
                    entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            else:
                entry["alternates"].append("候选%d(%r) -> %s: %s" % (i, args, type(exc).__name__, exc))
    out["funcs"].append(entry)
print(json.dumps(out, ensure_ascii=False))
'''.replace("REPO", repr(P.REPO))

# 用「写文件再读」而不是 capture_output=True：避免 subprocess 的 _readerthread 在退出时刷噪声
import tempfile
probe_log = os.path.join(tempfile.mkdtemp(prefix="hub-agg-"), "probe.log")
proc = H.popen([sys.executable, "-c", CHILD, str(closed_port)], cwd=P.REPO, log_path=probe_log)
rc = proc.wait(timeout=180)
with open(probe_log, encoding="utf-8", errors="replace") as fh:
    probe_out = fh.read()
if rc != 0:
    H.check("A02 能导入 hub.core.aggregator", False, probe_out.strip()[-300:])
    H.finish()
data = json.loads(probe_out.strip().splitlines()[-1])
H.check("A02 能导入 hub.core.aggregator（导入不启动任何服务）", data["import_error"] is None, data.get("import_error"))

funcs = data["funcs"]
H.check("A03 聚合器暴露了公开函数（接口面可判定）", len(funcs) > 0, "公开函数: %s" % [f["name"] for f in funcs])
H.info("不可达目标: http://127.0.0.1:%d（确认没人监听）" % closed_port)

raised = [f for f in funcs if f["ok"] is False]
misjudged = [f for f in funcs if f["ok"] is None and f["error"]]
called = [f for f in funcs if f["ok"] is True]
for f in called:
    H.check("A04 %s%s 在引擎不可达时不抛，返回 %s：%s" % (
        f["name"], f["signature"].replace(" ", ""), f["return_kind"], (f.get("sample") or "")[:60]),
        True, "")
    for alt in f.get("alternates", []):
        H.info("  · %s" % alt)
for f in misjudged:
    H.skip("A05 %s 的形状猜不出（不判红）" % f["name"], f["error"])
for f in funcs:
    if f["ok"] is None and not f["error"]:
        H.info("跳过 %s：%s" % (f["name"], f["called_with"]))

H.check("A06 没有任何公开函数在引擎不可达时抛「真异常」（核心判据）", not raised,
        "; ".join("%s -> %s" % (f["name"], f["error"]) for f in raised[:4]))
H.finish()
