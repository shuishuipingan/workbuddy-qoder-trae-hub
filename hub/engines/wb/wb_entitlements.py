# -*- coding: utf-8 -*-
"""Realm x model entitlement marks (issue #505).

The model catalogue only describes what the upstream offers; it cannot say
whether a free account is allowed to call a given model. The only trustworthy
signal is the upstream answer itself: code 11158, "model ... is not allowed
for user type ...". This module keeps that signal in a small sidecar next to
the account files, so a restart does not forget it.

The mark is one-way: only "blocked" is recorded. A successful request removes
the mark instead of writing a positive "available" claim, because one account
succeeding does not prove the whole free tier can use the model. Codes 11102
(model retired) and 11134 (provider unavailable) are deliberately not stored
here - they are model/service state, not account entitlement.

Marks expire after TTL seconds; a stale label is worse than no label.
"""
import json
import os
import threading
import time

#: A mark older than this is dropped on read. The panel says "recheck".
TTL = 24 * 3600

_lock = threading.RLock()
_cache = {"key": None, "data": {}}


def path(accounts_dir):
    return os.path.join(accounts_dir or ".", "model_entitlements.json")


def _load(accounts_dir):
    target = path(accounts_dir)
    try:
        key = (target, os.path.getmtime(target), os.path.getsize(target))
    except OSError:
        return {}
    with _lock:
        if _cache["key"] == key:
            return {realm: dict(models)
                    for realm, models in _cache["data"].items()}
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for realm, models in data.items():
        if not isinstance(models, dict):
            continue
        clean = {}
        for model, entry in models.items():
            if isinstance(entry, dict) and entry.get("code"):
                clean[str(model)] = dict(entry)
        if clean:
            out[str(realm)] = clean
    with _lock:
        _cache.update({"key": key, "data": out})
    return {realm: dict(models) for realm, models in out.items()}


def _save(accounts_dir, data):
    target = path(accounts_dir)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, target)
    try:
        key = (target, os.path.getmtime(target), os.path.getsize(target))
    except OSError:
        key = None
    with _lock:
        _cache.update({"key": key, "data": data})


def _fresh(entry, now):
    try:
        checked = float(entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        return False
    return checked > 0 and now - checked < TTL


def mark(accounts_dir, realm, model, code=11158, user_type="", source="runtime",
         now=None):
    """Record that model was refused for realm's free tier."""
    realm = str(realm or "").strip()
    model = str(model or "").strip()
    if not realm or not model:
        return None
    now = time.time() if now is None else float(now)
    entry = {
        "code": int(code),
        "userType": str(user_type or ""),
        "checked_at": now,
        "source": str(source or "runtime"),
    }
    with _lock:
        data = _load(accounts_dir)
        data.setdefault(realm, {})[model] = entry
        _save(accounts_dir, data)
    return entry


def clear(accounts_dir, realm, model):
    """Drop the mark for (realm, model); no-op when nothing is stored."""
    realm = str(realm or "").strip()
    model = str(model or "").strip()
    if not realm or not model:
        return False
    with _lock:
        data = _load(accounts_dir)
        models = data.get(realm) or {}
        if model not in models:
            return False
        models.pop(model, None)
        if models:
            data[realm] = models
        else:
            data.pop(realm, None)
        _save(accounts_dir, data)
    return True


def snapshot(accounts_dir, realm=None, now=None):
    """{realm: {model: entry}} with expired marks filtered out."""
    now = time.time() if now is None else float(now)
    with _lock:
        data = _load(accounts_dir)
    out = {}
    for name, models in data.items():
        if realm is not None and name != realm:
            continue
        fresh = {model: entry for model, entry in models.items()
                 if _fresh(entry, now)}
        if fresh:
            out[name] = fresh
    return out


def entitlement_for(accounts_dir, realm, model, now=None):
    """The mark for one model, or None."""
    realm = str(realm or "").strip()
    model = str(model or "").strip()
    if not realm or not model:
        return None
    return (snapshot(accounts_dir, realm, now).get(realm) or {}).get(model)


def user_type_from_detail(detail):
    """Pull personal_free_user out of the upstream 11158 body."""
    try:
        payload = json.loads(detail or "")
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("userType", "user_type"):
        value = payload.get(key)
        if value:
            return str(value)
    msg = str(payload.get("msg") or payload.get("message") or "")
    marker = "user type ["
    at = msg.find(marker)
    if at >= 0:
        end = msg.find("]", at + len(marker))
        if end > at:
            return msg[at + len(marker):end].strip()
    return ""
