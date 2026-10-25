# -*- coding: utf-8 -*-
"""_test_unified_keys.py —— 统一 Key 层（hub/core/keys.py）的行为判据。

锁住三件必须一直成立的事：

  U0x  分类：一把 key 属于 合并 / wb / qd / 不认识（停用的不算）
  U1x  上游挑 key：优先 realm 匹配 → 通吃（不绑区）→ 任意启用
  U2x  数据根：engine_keys() 必须跟随 HUB_DATA_ROOT —— 隔离实例里踩过：
       硬编码 hub/engines/<e>/accounts 会让面板显示"另一套" key 表，
       与引擎实际生效的那份不是同一个文件。
  U3x  summary / 缓存按 (mtime,size) 失效（改完 key 立刻生效）

不碰真实配置：全程 tempfile + HUB_SETTINGS / HUB_DATA_ROOT 指向临时目录。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

# 依赖缺失时**跳过而不是崩**（元套件 X05 会在"空 hub 树"里跑全量：
#   那棵树只有 tests/ + __init__.py，没有 core/keys.py —— 不设这道闸，
#   这里会抛 ImportError，把"别人文件没落地"变成一条假红 + Traceback）。
_KEYS_MODULE = os.path.join(P.HUB, "core", "keys.py")
if not os.path.exists(_KEYS_MODULE):
    H.skip("hub/core/keys.py 尚未落地 —— 统一 Key 判据整体跳过", _KEYS_MODULE)
    H.finish()
WORK = tempfile.mkdtemp(prefix="hub-keys-")
SETTINGS = os.path.join(WORK, "hub-settings.json")
DATA = os.path.join(WORK, "data")
os.environ["HUB_SETTINGS"] = SETTINGS
os.environ["HUB_DATA_ROOT"] = DATA


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


write_json(SETTINGS, {
    "panel_password_default": True,
    "merged_keys": [
        {"id": "m1", "name": "merged", "key": "mk-merged-1", "enabled": True},
        {"id": "m2", "name": "off", "key": "mk-disabled", "enabled": False},
    ],
    "routing": {"enabled": True, "prefer_realm": "cn", "engine_prefer": "auto"},
})
write_json(os.path.join(DATA, "wb", "accounts", "settings.json"), {
    "api_keys": [
        {"id": "w1", "name": "wb-cn", "key": "wbk-cn", "realm": "cn", "enabled": True},
        {"id": "w2", "name": "wb-any", "key": "wbk-any", "realm": "", "enabled": True},
        {"id": "w3", "name": "wb-off", "key": "wbk-off", "realm": "cn", "enabled": False},
    ],
})
write_json(os.path.join(DATA, "qd", "accounts", "settings.json"), {
    "api_keys": [
        {"id": "q1", "name": "qd-intl", "key": "qdk-intl", "realm": "intl", "enabled": True},
    ],
})
# 第 151 轮：三家都要有 key 表，否则"引擎 key 只走那一家"这条只验过两家。
write_json(os.path.join(DATA, "trae", "accounts", "settings.json"), {
    "api_keys": [
        {"id": "t1", "name": "trae-cn", "key": "trk-cn", "realm": "cn", "enabled": True},
        {"id": "t2", "name": "trae-off", "key": "trk-off", "realm": "cn", "enabled": False},
    ],
})

sys.path.insert(0, os.path.dirname(P.HUB))
from hub.core import keys  # noqa: E402  （必须在设置 env 之后导入，见文件头）

H.eq("U01 合并 key 分类为 merged", keys.classify("mk-merged-1")[0], "merged")
H.eq("U02 wb 的 key 分类为 wb", keys.classify("wbk-cn")[0], "wb")
H.eq("U03 qd 的 key 分类为 qd", keys.classify("qdk-intl")[0], "qd")
H.check("U04 未登记的 key 分类为 None", keys.classify("nope")[0] is None)
H.check("U05 停用的合并 key 不认（enabled=False）", keys.classify("mk-disabled")[0] is None)
# 第 151 轮：三档鉴权的第三家也要钉住 —— 只验两家等于没验"引擎 key 只走那一家"。
H.eq("U03b trae 的 key 分类为 trae", keys.classify("trk-cn")[0], "trae")
H.check("U05b 停用的 trae key 不认", keys.classify("trk-off")[0] is None)
H.eq("U14 只有一个 key 的引擎（trae）直接用它", keys.upstream_key("trae"), "trk-cn")

H.eq("U10 上游 key 优先 realm 精确匹配", keys.upstream_key("wb", "cn"), "wbk-cn")
H.eq("U11 目标区没有时退「通吃」，不拿别的区的 key 顶", keys.upstream_key("wb", "intl"), "wbk-any")
H.eq("U12 不指定区时优先通吃", keys.upstream_key("wb"), "wbk-any")
H.eq("U13 只有一个 key 的引擎直接用它", keys.upstream_key("qd"), "qdk-intl")

H.check("U20 engine_keys 跟随 HUB_DATA_ROOT（读到临时目录那份）",
        [k["name"] for k in keys.engine_keys("wb")] == ["wb-cn", "wb-any"],
        [k["name"] for k in keys.engine_keys("wb")])
H.check("U21 停用的引擎 key 被排除",
        "wb-off" not in [k["name"] for k in keys.engine_keys("wb")])
H.check("U22 settings 路径落在数据根内",
        keys.engine_settings_path("wb").startswith(DATA), keys.engine_settings_path("wb"))

# 第 194 轮：期望值必须**由注册表推导** —— 原来写死 {merged,wb,qd}，
# 结果 trae 一加进来这条就红（红得对：它守的正是"计数覆盖哪些引擎"）。
# 夹具里只塞了 wb/qd 的 key，其余引擎自然是 0。
# 夹具里每家塞了几把**启用**的 key（停用的不算）—— 新加引擎默认 0，
# 若哪天夹具给它塞了 key 却忘了更新这里，U30 会红，提醒你两处对齐。
_SEEDED = {"wb": 2, "qd": 1, "trae": 1}
_EXPECT = {"merged": 1}
for _e in keys.ENGINES:
    _EXPECT[_e] = _SEEDED.get(_e, 0)
H.eq("U30 summary 计数正确（键覆盖 ENGINES 且数值正确）", keys.summary(), _EXPECT)

write_json(os.path.join(DATA, "qd", "accounts", "settings.json"), {
    "api_keys": [
        {"id": "q1", "name": "qd-intl", "key": "qdk-intl", "realm": "intl", "enabled": True},
        {"id": "q2", "name": "qd-cn", "key": "qdk-cn", "realm": "cn", "enabled": True},
    ],
})
H.eq("U31 改完 key 表后下一次调用就生效（按 mtime/size 失效）",
     len(keys.engine_keys("qd")), 2)
H.eq("U32 新增的 cn key 立刻参与 realm 匹配", keys.upstream_key("qd", "cn"), "qdk-cn")

# ---------- U4x 数据根统一：融合层自己的配置也跟随 HUB_DATA_ROOT ----------
from hub.core import settings as hub_settings  # noqa: E402

_saved_settings = os.environ.pop("HUB_SETTINGS", None)
try:
    H.check("U40 未设 HUB_SETTINGS 时，hub 配置跟随 HUB_DATA_ROOT（半个应用搬走不算数据根）",
            hub_settings.path().startswith(DATA), hub_settings.path())
finally:
    if _saved_settings is not None:
        os.environ["HUB_SETTINGS"] = _saved_settings

H.check("U41 显式 HUB_SETTINGS 优先级最高（测试与多实例隔离靠它）",
        hub_settings.path() == SETTINGS, hub_settings.path())

# ---------- U5x 坏掉的 settings.json 必须被**记住**（不是静默当成"没有 key"） ----------
# 由来（第 46 轮）：keys._read_json 原来是 except Exception: return None, None ——
#   配置文件坏了 = 这个引擎"一把 key 都没有" = 合并 key 回落时 401，
#   而用户只看到 401，看不到"其实是 JSON 坏了"。现在要求：不崩，但必须登记。
import hub.core.keys as _keys  # noqa: E402

bad_dir = os.path.join(DATA, "wb", "accounts")
os.makedirs(bad_dir, exist_ok=True)
bad_path = os.path.join(bad_dir, "settings.json")
with open(bad_path, "w", encoding="utf-8") as fh:
    fh.write("{ this is not json")

empty = _keys.engine_keys("wb")
errs = _keys.parse_errors()
H.check("U50 坏 JSON：不抛异常、返回空表（门户不挂）", empty == [], empty)
H.check("U51 坏 JSON：必须登记进 parse_errors()（否则合并 key 静默 401）",
        bool(errs.get(bad_path)), errs)

with open(bad_path, "w", encoding="utf-8") as fh:
    fh.write('{"api_keys": []}')
_keys.engine_keys("wb")
H.check("U52 文件修好后登记被清掉（不会永远红）",
        bad_path not in _keys.parse_errors(), _keys.parse_errors())

# ---------- U6x 融合层配置坏了同样要留痕 ----------
from hub.core import settings as _hs  # noqa: E402
_bad_hs = os.path.join(WORK, "bad-hub-settings.json")
with open(_bad_hs, "w", encoding="utf-8") as fh:
    fh.write("{ not json either")
_saved = os.environ.get("HUB_SETTINGS")
os.environ["HUB_SETTINGS"] = _bad_hs
_loaded = _hs.load()
_err = _hs.load_error()
if _saved is None:
    os.environ.pop("HUB_SETTINGS", None)
else:
    os.environ["HUB_SETTINGS"] = _saved
H.check("U60 融合层配置坏 JSON：回默认（不崩）", isinstance(_loaded, dict) and "routing" in _loaded, list(_loaded)[:4])
H.check("U61 且 load_error() 报出路径与原因（自检据此报红）",
        _err.get("path") == _bad_hs and "JSONDecodeError" in (_err.get("error") or ""), _err)

H.finish()
