# -*- coding: utf-8 -*-
"""hub/core/keys.py —— 统一 Key 层：一个地址认三种 key。

规则只有一句话：**你拿哪把 key 来，就走哪条路。**

  ┌ 合并 key（hub/settings.json · merged_keys）
  │    能调**所有**模型，走智能路由（国内优先 → 跨引擎回退）。
  ├ wb key（hub/engines/wb/accounts/settings.json · api_keys）
  │    只走 WorkBuddy；/v1/models 也只列 wb 的模型。
  └ qd key（hub/engines/qd/accounts/settings.json · api_keys）
       只走 Qoder；/v1/models 也只列 qd 的模型。

## 向上游换 key

两个引擎各自独立鉴权。合并 key 不在引擎的 key 表里时，直接转发会被引擎 401 ——
这正是「合并 key 还得在两个引擎里各登记一遍」的老做法，也是本次要消掉的东西。
现在：**合并模式由 hub 用该引擎自己的一把启用 key 去请求**，于是
「一把 key 调所有」不需要任何重复登记；而引擎自己的 key 则**原样转发**，
尊重它原本的 realm 绑定与 models 白名单。

## 缓存

按 (mtime, size) 失效：面板改完 key，下一个请求就生效；稳态下不读盘。
"""

import json
import os

from . import paths, settings
from hub import ENGINES, ENGINE_LABELS  # noqa: E402  引擎注册表（唯一真源）

#: 引擎 key 表所在（优先 accounts/settings.json，其次引擎目录下）
_CACHE = {}          # path -> {"sig": (mtime, size), "keys": [...]}


def engine_settings_path(engine):
    """引擎的 settings.json —— **必须跟随数据根**（HUB_DATA_ROOT）。

    实测（隔离实例）：原来这里硬编码 hub/engines/<e>/accounts，于是设了
    HUB_DATA_ROOT 之后，统一 Key 卡显示的还是**仓库里**的 key 表，
    与引擎实际生效的那份不是同一份 —— 又一个"看起来对、其实两套"。
    改用 paths.accounts_dir()（它认识 HUB_DATA_ROOT 与 --accounts-dir）。
    """
    base = paths.accounts_dir(engine)
    candidate = os.path.join(base, "settings.json")
    if os.path.isfile(candidate):
        return candidate
    # 兜底：老布局（引擎目录下直接放 settings.json）
    legacy = os.path.join(paths.engine_dir(engine), "settings.json")
    return legacy if os.path.isfile(legacy) else candidate


#: 读不动的 settings.json：{路径: 错误描述}。
#  为什么单独记：解析失败会让"这个引擎一把 key 都没有"，
#  而合并 key 的回落路径随即 401 —— 用户只看到 401，看不到"其实是配置文件坏了"。
#  自检（/api/hub/selfcheck）会把这条摊开。
_PARSE_ERRORS = {}


def parse_errors():
    """{path: message} —— 解析失败的引擎配置文件（供自检/排障）。"""
    return dict(_PARSE_ERRORS)


def _read_json(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        st = os.stat(path)
        doc = json.loads(raw.decode("utf-8", "replace"))
        _PARSE_ERRORS.pop(path, None)
        return doc, (st.st_mtime, st.st_size)
    except FileNotFoundError:
        _PARSE_ERRORS.pop(path, None)
        return None, None
    except Exception as exc:  # noqa: BLE001 —— 坏 JSON 不该让门户挂掉，但要**被记住**
        _PARSE_ERRORS[path] = "%s: %s" % (type(exc).__name__, exc)
        return None, None


def engine_keys(engine):
    """该引擎**启用中**的 key 条目：[{id, name, key, realm, models, enabled}]。"""
    path = engine_settings_path(engine)
    sig = None
    try:
        st = os.stat(path)
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return []
    cached = _CACHE.get(path)
    if cached and cached["sig"] == sig:
        return cached["keys"]
    doc, _ = _read_json(path)
    keys = []
    if isinstance(doc, dict):
        for item in doc.get("api_keys") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key or item.get("enabled") is False:
                continue
            keys.append({
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "key": key,
                "realm": str(item.get("realm") or ""),
                "models": item.get("models") or [],
                "engine": engine,
            })
    _CACHE[path] = {"sig": sig, "keys": keys}
    return keys


def merged_keys():
    return [entry for entry in settings.merged_keys() if entry.get("enabled")]


def classify(supplied):
    """这把 key 属于谁？

    返回 (kind, entry)：kind ∈ {"merged", "wb", "qd"}；不认识返回 (None, None)。
    """
    key = str(supplied or "").strip()
    if not key:
        return None, None
    for entry in merged_keys():
        if entry["key"] == key:
            entry = dict(entry, engine="merged")
            return "merged", entry
    for engine in ENGINES:
        for entry in engine_keys(engine):
            if entry["key"] == key:
                return engine, entry
    return None, None


def upstream_key(engine, realm=None):
    """给合并模式挑一把**该引擎认识**的 key。

    优先 realm 精确匹配，其次不绑区（通吃），最后任意启用的一把。
    返回 None 表示这个引擎一把可用 key 都没有（调用方应把它当作不可用候选）。
    """
    keys = engine_keys(engine)
    if not keys:
        return None
    if realm:
        for entry in keys:
            if entry["realm"] == realm:
                return entry["key"]
    for entry in keys:
        if not entry["realm"]:
            return entry["key"]
    return keys[0]["key"]


def summary():
    """面板/排障用：每类 key 各有多少把（第 194 轮修：原来写死 wb/qd，漏了 trae）。"""
    out = {"merged": len(merged_keys())}
    for engine in ENGINES:
        out[engine] = len(engine_keys(engine))
    return out


def describe(supplied):
    """一句话说明这把 key 会走哪条路（给 401 提示与排障用）。"""
    kind, entry = classify(supplied)
    if kind == "merged":
        return "合并 key → 智能路由（全部模型）"
    if kind in ENGINES:
        # 第 194 轮修：原来只写了 wb/qd 两支 —— trae 的 key 会掉到
        # 「未登记的 key」，401 提示与排障信息直接说错话。
        label = ENGINE_LABELS.get(kind, kind)
        who = entry.get("name") or entry.get("id") or ""
        return "%s key → 只走 %s（%s）" % (label, kind, who)
    return "未登记的 key"
