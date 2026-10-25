#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/core/settings.py —— 合并层的自有配置。

与两个引擎的 settings.json 是**两回事**：这份只属于融合面（portal），存
「合并模式」自己的三样东西：

  1. merged_keys  —— 允许走合并智能路由的 API key 列表（同一把 key 必须
                     同时在 wb 与 qd 的 API Key 里注册，两引擎各自校验）。
  2. routing      —— 智能路由偏好：开关 / 首选区（默认国内优先）/ 引擎顺序。
  3. panel 口令    —— portal 自己的面板口令（默认 admin，与引擎一致的口径）。

文件位置：<repo>/hub/settings.json（可用 HUB_SETTINGS 环境变量改）。
只用标准库。
"""
import hashlib
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(os.path.dirname(HERE), "settings.json")
_lock = threading.RLock()

REALMS = ("cn", "intl")
ENGINES = ("wb", "qd")

DEFAULTS = {
    "panel_password_default": True,
    "panel_password_hash": None,
    "merged_keys": [],
    "routing": {"enabled": True, "prefer_realm": "cn", "engine_prefer": "auto"},
}


def path():
    """配置文件位置。

    优先级：
      1. HUB_SETTINGS（显式指定，测试与多实例隔离都用它）
      2. HUB_DATA_ROOT 下的 hub-settings.json —— **数据根统一**：
         引擎的 accounts/usage 已经跟着数据根走了，融合层自己的配置没理由留在仓库里；
         否则同一个"数据根"其实只搬了半个应用（实测隔离实例里踩过这个不一致）。
      3. 仓库内 hub/settings.json（默认）
    """
    explicit = os.environ.get("HUB_SETTINGS")
    if explicit:
        return explicit
    try:
        from . import paths as _paths
        root = _paths.data_root()
    except Exception:
        root = None
    if root:
        return os.path.join(root, "hub-settings.json")
    return DEFAULT_PATH


#: 上一次加载失败的原因（缺失文件不算：那是首跑的正常状态）。
_LOAD_ERROR = {"path": None, "error": None}


def load_error():
    """{"path", "error"} —— 融合层配置文件**存在但读不动**时的原因；正常为 None。"""
    return dict(_LOAD_ERROR)


def load():
    """读配置；文件缺失/坏 JSON 时回默认（并自动补缺省键）。

    ⚠️ 但"坏 JSON"必须被**记住**：那个状态下 merged_keys 全空、所有合并 key 都会 401，
    而界面上只会显示"key 不对"。自检会把这条摊开（见 portal._selfcheck）。
    """
    with _lock:
        data = {}
        try:
            with open(path(), encoding="utf-8") as fh:
                data = json.load(fh)
            _LOAD_ERROR["path"] = None
            _LOAD_ERROR["error"] = None
        except FileNotFoundError:
            _LOAD_ERROR["path"] = None
            _LOAD_ERROR["error"] = None
            data = {}
        except Exception as exc:  # noqa: BLE001 —— 坏 JSON 不让门户挂，但要留痕
            _LOAD_ERROR["path"] = path()
            _LOAD_ERROR["error"] = "%s: %s" % (type(exc).__name__, exc)
            data = {}
        if not isinstance(data, dict):
            data = {}
        for key, value in DEFAULTS.items():
            data.setdefault(key, value)
        return data


def save(data):
    with _lock:
        tmp = path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path())


# ------------------------------------------------------------------ 面板口令
def panel_password_default():
    return bool(load().get("panel_password_default"))


def verify_panel_password(supplied):
    """面板口令校验：默认口令 admin；改过后按 sha256 比对。"""
    if not supplied:
        return False
    data = load()
    if data.get("panel_password_default"):
        return str(supplied) == "admin"
    stored = data.get("panel_password_hash")
    if not stored:
        return False
    digest = hashlib.sha256(str(supplied).encode("utf-8")).hexdigest()
    return digest == str(stored)


def set_panel_password(current, new):
    """改口令：校验当前口令后写入新口令的 sha256。返回 (ok, message)。"""
    if not verify_panel_password(current):
        return False, "current password does not match"
    new = str(new or "")
    if len(new) < 4:
        return False, "new password must be at least 4 characters"
    data = load()
    data["panel_password_default"] = False
    data["panel_password_hash"] = hashlib.sha256(new.encode("utf-8")).hexdigest()
    save(data)
    return True, ""


# ------------------------------------------------------------------ 合并密钥
def _clean_key_item(item):
    if not isinstance(item, dict):
        return None
    entry = dict(item)
    entry["id"] = str(entry.get("id") or "").strip()
    entry["name"] = str(entry.get("name") or "").strip()
    entry["key"] = str(entry.get("key") or "").strip()
    entry["enabled"] = entry.get("enabled", True) is not False
    return entry


def merged_keys(masked=False):
    out = []
    for item in load().get("merged_keys") or []:
        entry = _clean_key_item(item)
        if not entry or not entry["key"]:
            continue
        if masked and entry["key"]:
            key = entry["key"]
            entry["key"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else "…"
        out.append(entry)
    return out


def match_merged_key(supplied):
    """返回命中（且启用）的合并密钥条目，未命中返回 None。"""
    if not supplied:
        return None
    for entry in merged_keys():
        if entry.get("enabled") and entry["key"] == str(supplied):
            return entry
    return None


def set_merged_keys(items):
    """整体替换合并密钥列表。掩码条目（含 …）保留旧值不覆盖。"""
    if not isinstance(items, list):
        raise ValueError("merged_keys must be a list")
    previous = {entry.get("id"): entry for entry in merged_keys()}
    cleaned = []
    for item in items:
        entry = _clean_key_item(item)
        if not entry:
            continue
        if "…" in entry["key"] and entry["id"] in previous:
            entry["key"] = previous[entry["id"]]["key"]
        if not entry["key"]:
            continue
        if len(entry["key"]) < 4:
            raise ValueError("merged key must be at least 4 characters")
        if not entry["id"]:
            entry["id"] = "mkey-" + hashlib.sha256(
                entry["key"].encode("utf-8")).hexdigest()[:10]
        cleaned.append(entry)
    data = load()
    data["merged_keys"] = cleaned
    save(data)
    return cleaned


# ------------------------------------------------------------------ 路由偏好
def routing():
    data = load()
    cfg = data.get("routing")
    if not isinstance(cfg, dict):
        cfg = {}
    enabled = cfg.get("enabled", True) is not False
    prefer = str(cfg.get("prefer_realm") or "cn").strip().lower()
    if prefer not in REALMS:
        prefer = "cn"
    engine_prefer = str(cfg.get("engine_prefer") or "auto").strip().lower()
    if engine_prefer not in ("wb", "qd", "auto"):
        engine_prefer = "auto"
    return {"enabled": bool(enabled), "prefer_realm": prefer,
            "engine_prefer": engine_prefer}


def set_routing(patch):
    if not isinstance(patch, dict):
        raise ValueError("routing must be an object")
    current = routing()
    for key in ("enabled", "prefer_realm", "engine_prefer"):
        if key in patch:
            current[key] = patch[key]
    enabled = current["enabled"] is not False
    prefer = str(current["prefer_realm"] or "cn").strip().lower()
    if prefer not in REALMS:
        raise ValueError("prefer_realm must be cn or intl")
    engine_prefer = str(current["engine_prefer"] or "auto").strip().lower()
    if engine_prefer not in ("wb", "qd", "auto"):
        raise ValueError("engine_prefer must be wb, qd or auto")
    stored = {"enabled": bool(enabled), "prefer_realm": prefer,
              "engine_prefer": engine_prefer}
    data = load()
    data["routing"] = stored
    save(data)
    return stored
