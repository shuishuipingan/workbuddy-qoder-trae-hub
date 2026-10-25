"""Model tiers: which models only paid (PRO) accounts may use.

The upstream `/v3/config` response carries a `modelTiers[]` array next to the
model list. The gateway used to keep only `agents[].models` and `models[]`, so
a free account was still offered PRO-only models and the upstream answered
11158. This module keeps that tier map so the scheduler and `/v1/models` can
honour it.

Only entries with a `requiredUserType` / `tier` are tiers. `enabled: false`
entries are skipped, and a badge that says "订阅优先" (subscription priority)
without `accessMode: exclusive` is not a tier gate: free accounts were measured
calling those models successfully, so treating them as exclusive would refuse
working requests (the same rule as the Go gateway's ADR-0049).
"""
import threading
import time

#: User-type spellings the upstream uses for an unpaid account.
FREE_TYPES = frozenset((
    "free",
    "personal_free_user",
    "personal-free-user",
))

_lock = threading.Lock()
#: realm -> {model id: "free" | "standard"}
_tiers = {}
#: realm -> epoch seconds of the last successful ingest (the catalogue refresh
#: the map came from). The panel shows it next to the label so a stale map is
#: visible instead of silently trusted.
_ingested_at = {}


def _priority_only(entry):
    """True when the entry is only a scheduling preference, not a gate."""
    if str(entry.get("accessMode") or "").strip().lower() == "exclusive":
        return False
    badge = entry.get("badge")
    labels = []
    if isinstance(badge, dict):
        labels = [badge.get("label"), badge.get("labelEn")]
    elif isinstance(badge, str):
        labels = [badge]
    for label in labels:
        text = str(label or "").strip()
        if not text:
            continue
        if "订阅优先" in text:
            return True
        if text.lower() in ("priority", "subscription priority"):
            return True
    return False


def _tier_of(raw):
    """Normalize an upstream user type; unknown non-empty values mean paid."""
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if value in FREE_TYPES:
        return "free"
    return "standard"


def ingest(realm, payload):
    """Record the tier map carried by one /v3/config payload.

    A payload without `modelTiers` leaves the previous map untouched: an
    install whose upstream omits the array must not silently lose the tiers it
    learned earlier.
    """
    if not isinstance(payload, dict):
        return False
    root = payload
    data = payload.get("data")
    if isinstance(data, dict):
        root = data
    entries = root.get("modelTiers")
    if not isinstance(entries, list):
        return False
    out = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        if _priority_only(entry):
            continue
        tier = _tier_of(entry.get("requiredUserType") or entry.get("tier"))
        if tier is None:
            continue
        ids = entry.get("modelIds")
        if not isinstance(ids, list):
            continue
        for mid in ids:
            model = str(mid or "").strip()
            if model:
                out[model] = tier
    with _lock:
        _tiers[str(realm or "")] = out
        _ingested_at[str(realm or "")] = time.time()
    return True


def snapshot(realm=None):
    """A copy of the tier map for one realm, or the merged map."""
    with _lock:
        if realm:
            return dict(_tiers.get(realm) or {})
        merged = {}
        for mapping in _tiers.values():
            merged.update(mapping)
        return merged


def requires_pro(model, realm=None):
    """True when the model is gated to paid accounts.

    Unknown models and an empty map return False, so an install that never
    received `modelTiers` keeps its exact previous behaviour.
    """
    name = str(model or "").strip()
    if not name:
        return False
    with _lock:
        if realm and realm in _tiers:
            return _tiers[realm].get(name) == "standard"
        return any(mapping.get(name) == "standard"
                   for mapping in _tiers.values())


def ingested_at(realm=None):
    """When the realm's tier map was last refreshed, or None if never.

    `/v1/models` carries this next to the label: the map is in-memory only and
    is rebuilt by every catalogue refresh, so "when" is the only honest way to
    say how current the claim is.
    """
    with _lock:
        if realm:
            return _ingested_at.get(str(realm))
        stamps = list(_ingested_at.values())
    return max(stamps) if stamps else None


def reset():
    """Drop every recorded tier map (tests only)."""
    with _lock:
        _tiers.clear()
        _ingested_at.clear()
