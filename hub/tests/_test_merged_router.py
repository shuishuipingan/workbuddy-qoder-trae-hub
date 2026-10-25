# -*- coding: utf-8 -*-
"""_test_merged_router.py —— 合并模式智能路由：候选排序 / 同名合并 / 合并层设置。

纯单元：不碰引擎端口。覆盖：

  * router.candidates_for —— 区域优先（prefer_realm）+ 引擎排序（auto=就绪多者先、
    显式 wb/qd 固定序）+ 只保留「该引擎在该区清单里有这个模型」的组合；
  * router.merged_model_list —— 同名模型合一条、engines/realm 并集、realm_exclusive；
  * router.extract_model —— chat / responses / messages 三协议的 model 提取；
  * settings 层 —— merged_keys 掩码/匹配/替换（掩码条目保留旧值）、routing 校验、
    面板口令默认 admin 与改密校验。

口径与 _test_aggregator.py 一致：harness 的 check()，自报 SUMMARY 行。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

_ROUTER = os.path.join(P.HUB, "core", "router.py")
_SETTINGS = os.path.join(P.HUB, "core", "settings.py")
if not (os.path.exists(_ROUTER) and os.path.exists(_SETTINGS)):
    H.skip("R01 hub/core/router.py 或 settings.py 尚未落地 —— 合并路由判据整体跳过", _ROUTER)
    H.finish()

sys.path.insert(0, os.path.dirname(P.HUB))
from hub.core import router
from hub.core import settings


class Snapshot(object):
    def __init__(self, models=None, ready=None):
        self.models = models or {}
        self.ready = ready or {}
        self.errors = {}


def snap(models, ready):
    return Snapshot(models=models, ready=ready)


CFG_CN_AUTO = {"prefer_realm": "cn", "engine_prefer": "auto"}

# ---------- 候选排序 ----------
s = snap({"wb": {"cn": {"glm-5.3-flash"}, "intl": {"glm-5.3-flash"}},
          "qd": {"cn": {"glm-5.3-flash"}, "intl": set()}},
         {"wb": 27, "qd": 3})
H.eq("R02 cn 优先 + auto 按就绪多者先（wb27>qd3）+ intl 只剩 wb",
     router.candidates_for("glm-5.3-flash", s, CFG_CN_AUTO),
     [("wb", "cn"), ("qd", "cn"), ("wb", "intl")])

s = snap({"wb": {"cn": {"m"}, "intl": {"m"}}, "qd": {"cn": {"m"}, "intl": {"m"}}},
         {"wb": 5, "qd": 9})
H.eq("R03 prefer=intl 翻转区域顺序，区内 auto 仍按就绪排（qd9>wb5）",
     router.candidates_for("m", s, {"prefer_realm": "intl", "engine_prefer": "auto"}),
     [("qd", "intl"), ("wb", "intl"), ("qd", "cn"), ("wb", "cn")])

s = snap({"wb": {"cn": {"m"}}, "qd": {"cn": {"m"}}}, {"wb": 99, "qd": 1})
H.eq("R04 显式 engine_prefer=qd 压过就绪数",
     router.candidates_for("m", s, {"prefer_realm": "cn", "engine_prefer": "qd"}),
     [("qd", "cn"), ("wb", "cn")])

s = snap({"wb": {"cn": set(), "intl": {"m"}}, "qd": {"cn": {"m"}, "intl": set()}},
         {"wb": 5, "qd": 5})
H.eq("R05 只保留「该引擎在该区有此模型」的组合",
     router.candidates_for("m", s, CFG_CN_AUTO),
     [("qd", "cn"), ("wb", "intl")])

H.eq("R06 空模型名 -> 无候选", router.candidates_for("", snap({}, {}), CFG_CN_AUTO), [])

s = snap({"wb": {"cn": {"glm-5.3-flash"}}}, {"wb": 1})
H.eq("R07 模型名大小写不敏感（快照侧已小写）",
     router.candidates_for("GLM-5.3-Flash", s, CFG_CN_AUTO), [("wb", "cn")])

# ---------- 同名合并 ----------
s = snap({"wb": {"cn": {"m"}, "intl": set()}, "qd": {"cn": {"m"}, "intl": {"m"}}}, {})
by_id = {d["id"]: d for d in router.merged_model_list(s)}
H.check("R08 同名模型合一条", "m" in by_id and len(router.merged_model_list(s)) == 1,
        sorted(by_id))
# 第 100 轮起有三家：没有该模型的引擎给**空列表**（前端据此显示"这家没这个模型"），
# 而不是把它从 map 里删掉 —— 删掉的话"缺引擎"和"引擎在该区没有"就分不清了。
H.eq("R08b engines 并集（wb 只 cn、qd 双区、trae 无该模型→空列表）", by_id.get("m", {}).get("engines"),
     {"wb": ["cn"], "qd": ["cn", "intl"], "trae": []})
H.eq("R08c realm 并集排序", by_id.get("m", {}).get("realm"), ["cn", "intl"])
H.check("R08d 双区 -> 非独占", by_id.get("m", {}).get("realm_exclusive") is False)

s = snap({"wb": {"cn": set(), "intl": {"gpt-5.5"}}}, {})
one = router.merged_model_list(s)
H.check("R09 单区 -> realm_exclusive=True", one and one[0]["realm_exclusive"] is True
        and one[0]["realm"] == ["intl"], one)

s = snap({"wb": {"cn": {"b", "a"}}}, {})
H.eq("R10 合并清单按 id 排序", [d["id"] for d in router.merged_model_list(s)], ["a", "b"])

# ---------- 三协议 model 提取 ----------
for label, doc in (("chat", {"model": "glm-5.3-flash", "messages": []}),
                   ("responses", {"model": "glm-5.3-flash", "stream": True}),
                   ("messages", {"model": "glm-5.3-flash"})):
    H.eq("R11 extract_model(%s)" % label,
         router.extract_model(json.dumps(doc).encode("utf-8")), "glm-5.3-flash")
H.eq("R12 坏 body -> 空串", router.extract_model(b"not json"), "")
H.eq("R12b 空 body -> 空串", router.extract_model(b""), "")
H.eq("R12c 非对象 body -> 空串", router.extract_model(b"[]"), "")

# ---------- 合并层设置 ----------
_tmp = tempfile.mkdtemp(prefix="hub-settings-")
_saved_env = os.environ.get("HUB_SETTINGS")
os.environ["HUB_SETTINGS"] = os.path.join(_tmp, "settings.json")
try:
    H.eq("R13 routing 默认（开 / cn / auto）", settings.routing(),
         {"enabled": True, "prefer_realm": "cn", "engine_prefer": "auto"})
    H.eq("R13b merged_keys 默认空", settings.merged_keys(), [])
    H.check("R13c 默认口令 admin 可过", settings.panel_password_default()
            and settings.verify_panel_password("admin"))
    H.check("R13d 错口令不过", not settings.verify_panel_password("nope")
            and not settings.verify_panel_password(""))

    settings.set_routing({"prefer_realm": "intl", "engine_prefer": "qd", "enabled": False})
    H.eq("R14 routing 往返", settings.routing(),
         {"enabled": False, "prefer_realm": "intl", "engine_prefer": "qd"})
    for bad, why in (({"prefer_realm": "mars"}, "非法区"), ({"engine_prefer": "gpt"}, "非法引擎"),
                     ("cn", "非对象")):
        try:
            settings.set_routing(bad); ok = False
        except ValueError:
            ok = True
        H.check("R15 set_routing 拒绝%s" % why, ok)

    settings.set_merged_keys([{"id": "km0", "name": "hebing",
                               "key": "mrg-merge-key-001", "enabled": True}])
    masked = settings.merged_keys(masked=True)
    H.eq("R16 掩码 = 前6 + 省略号 + 后4", masked[0]["key"], "mrg-me\u2026-001")
    H.check("R16b 命中合并 key", settings.match_merged_key("mrg-merge-key-001") is not None)
    H.check("R16c 未命中/空 不过", settings.match_merged_key("wrong") is None
            and settings.match_merged_key("") is None)
    # 掩码条目保存时保留旧值（面板只回传掩码）
    settings.set_merged_keys([{"id": "km0", "name": "x", "key": "mrg-me\u2026-001", "enabled": True}])
    H.check("R17 掩码回写保留旧明文", settings.match_merged_key("mrg-merge-key-001") is not None)
    out = settings.set_merged_keys([{"key": "mrg-another-key", "enabled": True}])
    H.check("R17b 新条目自动补 id", out and out[0]["id"].startswith("mkey-"), out)
    for bad, why in (([{"key": "ab", "enabled": True}], "过短 key"), ("not a list", "非列表")):
        try:
            settings.set_merged_keys(bad); ok = False
        except ValueError:
            ok = True
        H.check("R18 set_merged_keys 拒绝%s" % why, ok)

    settings.set_merged_keys([{"id": "k", "key": "zzz-key-001", "enabled": False}])
    H.check("R19 停用的 key 不命中", settings.match_merged_key("zzz-key-001") is None)

    ok, msg = settings.set_panel_password("admin", "new-pass-123")
    H.check("R20 改密成功", ok, msg)
    H.check("R20b 改密后默认标志清除、新口令可过、旧口令不过",
            (not settings.panel_password_default())
            and settings.verify_panel_password("new-pass-123")
            and not settings.verify_panel_password("admin"))
    ok, _ = settings.set_panel_password("admin", "another")
    H.check("R21 当前口令错 -> 改密被拒", not ok)
    ok, _ = settings.set_panel_password("new-pass-123", "ab")
    H.check("R21b 新口令过短 -> 被拒", not ok)
finally:
    if _saved_env is None:
        os.environ.pop("HUB_SETTINGS", None)
    else:
        os.environ["HUB_SETTINGS"] = _saved_env

H.finish()
