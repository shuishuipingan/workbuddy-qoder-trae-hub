# -*- coding: utf-8 -*-
"""_test_hub_shapes.py —— 聚合端点的**键集合**必须是 ENGINES 决定的（第 150 轮）。

由来：第 147 轮 hub_usage 循环用了 ENGINES、返回却写死 {"wb":…, "qd":…}，
于是 /api/hub/usage 里没有 trae —— 界面上"网关调用量 1,210"的拆分只有两家，
**合计里有、分项里没有**。静态扫描（E1）能防写法，但防不住"运行时的键少了"。

做法：不启真引擎，直接把聚合器的取数函数换成假的，看**返回的键**。
这样 100% 确定、秒级、不占端口。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

# 空树（变异测试 X05 会这么跑）里 aggregator 不存在 —— 必须 SKIP 而不是 import 崩溃。
# 这是本仓的既有惯例：_test_aggregator.py 开头也先判断文件在不在。
if not os.path.exists(os.path.join(P.HUB, "core", "aggregator.py")):
    H.skip("H00 hub/core/aggregator.py 尚未落地 —— 键集合判据整体跳过",
           os.path.join(P.HUB, "core", "aggregator.py"))
    H.finish()

sys.path.insert(0, P.REPO)
try:
    from hub import ENGINES                                # noqa: E402
    from hub.core import aggregator                        # noqa: E402
except Exception as _exc:                                  # noqa: BLE001
    H.skip("H00 聚合器 import 失败（半成品树）—— 跳过", "%s: %s" % (type(_exc).__name__, _exc))
    H.finish()

WANT = set(ENGINES)


def fake_json(engine, ports, path_, params=None, **kw):
    """给每家一份最小可用的响应；形状不对的字段一律给 0/空，够算出键就行。"""
    if path_.endswith("/usage"):
        return {"requests": 1, "total_tokens": 10, "credit": 1}
    if path_.endswith("/usage/timeseries"):
        return {"series": [{"at": 1700000000, "requests": 1, "total_tokens": 10, "credit": 1}],
                "bucket_seconds": 3600}
    if path_.endswith("/usage/recent"):
        # 必须带账号字段：聚合按 "<engine>|<account>" 归并，没有就整行跳过（H3 第一次就栽在这）
        return {"rows": [{"at": 1700000000, "model": "m", "total_tokens": 5,
                          "account": "acct-1", "key_id": "k1", "status": 200}]}
    if path_.endswith("/health"):
        return {"ok": True, "version": "1.0.0", "service": engine + "-svc", "accounts": 1}
    if path_.endswith("/v1/models"):
        return {"data": [{"id": engine + "-model"}]}
    return {}


aggregator._json_or_none = fake_json          # 关键：不碰网络

# ---- H1 /api/hub/usage 的键 = ENGINES ∪ {merged} ----
try:
    out = aggregator.hub_usage({e: 1 for e in ENGINES}, None)
    keys = set(out or {})
    H.check("H1 /api/hub/usage 的键 = ENGINES ∪ {merged}", keys == WANT | {"merged"},
            "实际键 %s，期望 %s" % (sorted(keys), sorted(WANT | {"merged"})))
except Exception as exc:
    H.check("H1 /api/hub/usage 可调用", False, "%s: %s" % (type(exc).__name__, exc))

# ---- H2 timeseries 的 series / tokens 键 = ENGINES ----
try:
    ts = aggregator.hub_timeseries({e: 1 for e in ENGINES}, None, "7d")
    s_keys = set((ts or {}).get("series") or {})
    t_keys = set((ts or {}).get("tokens") or {})
    H.check("H2 /api/hub/timeseries 的 series 与 tokens 都覆盖全部引擎",
            s_keys == WANT and t_keys == WANT,
            "series=%s tokens=%s" % (sorted(s_keys), sorted(t_keys)))
except Exception as exc:
    H.check("H2 hub_timeseries 可调用", False, "%s: %s" % (type(exc).__name__, exc))

# ---- H3 账号健康按 "<engine>|<acct>" 归并，且每家都能出现 ----
try:
    ah = aggregator.hub_account_health({e: 1 for e in ENGINES}, limit=10)
    seen = {k.split("|")[0] for k in ((ah or {}).get("accounts") or {})}
    H.check("H3 /api/hub/account-health 的键带引擎前缀且覆盖全部引擎",
            seen == WANT, "出现过的引擎=%s" % sorted(seen))
except Exception as exc:
    H.check("H3 hub_account_health 可调用", False, "%s: %s" % (type(exc).__name__, exc))

# ---- H4 health 的 engines 字典覆盖全部引擎 ----
try:
    hh = aggregator.hub_health({e: 1 for e in ENGINES})
    keys = set(((hh or {}).get("engines") or {}))
    H.check("H4 /api/hub/health 的 engines 覆盖全部引擎", keys == WANT, "实际=%s" % sorted(keys))
except Exception as exc:
    H.check("H4 hub_health 可调用", False, "%s: %s" % (type(exc).__name__, exc))

# ---- H6 /api/hub/models 的并集覆盖全部引擎 ----
try:
    models = aggregator.hub_models({e: 1 for e in ENGINES})
    got = {m.get("engine") for m in (models or [])}
    H.check("H6 /api/hub/models 并集覆盖全部引擎且每条带 engine 标",
            got == WANT and all(isinstance(m.get("engine"), str) for m in models or []),
            "实际引擎=%s" % sorted(x for x in got if x))
except Exception as exc:
    H.check("H6 hub_models 可调用", False, "%s: %s" % (type(exc).__name__, exc))

# ---- H5 阳性对照：写死两家的实现必须被同一套断言判红 ----
def bad_usage(ports, realm=None):
    return {"wb": {}, "qd": {}, "merged": {}}

try:
    bad_keys = set(bad_usage({}, None))
    H.check("H5 阳性对照：返回写死两家时必须判红",
            not (bad_keys == WANT | {"merged"}),
            "对照组居然通过了 —— 断言无效")
except Exception as exc:
    H.check("H5 阳性对照可运行", False, str(exc))


# ---- H7：统一 Key 的计数与描述也必须覆盖全部引擎（第 194 轮）----
# 由来：keys.summary() 写死 merged/wb/qd -> /api/hub/keys 的 counts 里没有 trae；
#   describe() 同理，trae 的 key 会被说成「未登记的 key」。
try:
    from hub.core import keys as _keys
    _sum = _keys.summary()
    H.check("H7 keys.summary() 的键 = ENGINES ∪ {merged}",
            set(_sum) == set(ENGINES) | {"merged"}, "实际=%s" % sorted(_sum))
    import inspect as _inspect
    _src = _inspect.getsource(_keys.summary)
    H.check("H7b summary 不再写死引擎名（必须遍历 ENGINES）",
            "for engine in ENGINES" in _src and '"qd"' not in _src,
            _src.strip().splitlines()[0][:60] if _src else "")
    _dsrc = _inspect.getsource(_keys.describe)
    H.check("H8 describe 按注册表判断（没有写死 wb/qd 两支）",
            "kind in ENGINES" in _dsrc, "describe 仍是写死分支")
except Exception as exc:
    H.check("H7 统一 Key 计数可调用", False, "%s: %s" % (type(exc).__name__, exc))


# ---- H9：/api/hub/keys 的 rules 说明也必须覆盖全部引擎（第 194 轮）----
# 由来：counts 修好之后，同一个响应里的 rules 还写死 wb/qd —— 用户看到的仍是两家。
try:
    _portal_src = open(os.path.join(P.HUB, "core", "portal.py"), encoding="utf-8").read()
    _rules_block = _portal_src[_portal_src.index('"rules"'):][:600]
    H.check("H9 keys.rules 由 ENGINES 生成（不再写死 wb/qd）",
            "for e in ENGINES" in _rules_block and '"qd": "只走 Qoder' not in _rules_block,
            _rules_block.replace("\n", " ")[:80])
    H.check("H9b 反向对照：写死的样本会被判红",
            not ("for e in ENGINES" in '"rules": {"merged": 1, "wb": 2, "qd": 3}'),
            "检测器失效")
except Exception as exc:
    H.check("H9 keys.rules 可读", False, "%s: %s" % (type(exc).__name__, exc))

H.finish()
