"""route orchestration —— 用户可编辑的路由编排（跨引擎）。

在 router.candidates_for() 给出的「模型 → (引擎, 区域)」候选链之上再加一层规则：

    rules:  按 priority 升序匹配（模型通配 / 区域 / 引擎），命中即用规则给的链
    policy: strategy（first-available / round-robin）+ 会话粘性 + 最大回退次数

设计上刻意**不**改写模型名、不越权账号范围，干跑（preview）无副作用；
命中的规则若整条链都不可用，默认回落默认链（fail-open），除非规则显式
`stop_on_miss: true`（fail-closed）。
"""
import fnmatch
import json
import os
import threading
import time
import uuid

from hub.core import paths

_LOCK = threading.RLock()
STRATEGIES = ("first-available", "round-robin")
DEFAULT_POLICY = {
    "strategy": "first-available",
    "session_affinity": True,
    "max_fallbacks": 2,
}
_RR = {}          # (rule_id, model) -> 上次用的下标，round-robin 用


def _path():
    return os.path.join(paths.data_root() or paths.hub_dir(), "route-settings.json")


def _clean_list(value, field, limit=64):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("%s 必须是数组（最多 %d 项）" % (field, limit))
    out = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text[:120])
    return out


def normalize_rule(item, engines, realms):
    if not isinstance(item, dict):
        raise ValueError("规则必须是对象")
    rid = str(item.get("id") or uuid.uuid4().hex)[:40]
    name = str(item.get("name") or "")[:80]
    match = item.get("match") or {}
    action = item.get("action") or {}
    if not isinstance(match, dict) or not isinstance(action, dict):
        raise ValueError("match/action must be objects")
    if not isinstance(action.get("chain"), list):
        raise ValueError("action.chain must be an array")
    models = _clean_list(match.get("models"), "match.models")
    mrealms = [r for r in _clean_list(match.get("realms"), "match.realms") if r in realms]
    mengines = [e for e in _clean_list(match.get("engines"), "match.engines") if e in engines]
    chain = []
    for hop in (action.get("chain") or [])[:12]:
        if not isinstance(hop, dict):
            continue
        eng = str(hop.get("engine") or "").strip().lower()
        realm = str(hop.get("realm") or "").strip().lower()
        if eng not in engines:
            raise ValueError("chain 里的 engine=%s 不认识" % eng)
        if realm and realm not in realms:
            raise ValueError("chain 里的 realm=%s 不认识" % realm)
        chain.append({"engine": eng, "realm": realm or ""})
    if not chain:
        raise ValueError("规则 %s 的 action.chain 不能为空" % (name or rid))
    return {
        "id": rid,
        "name": name,
        "enabled": bool(item.get("enabled", True)),
        "priority": int(item.get("priority") or 100),
        "match": {"models": models, "realms": mrealms, "engines": mengines},
        "action": {"chain": chain, "stop_on_miss": bool(action.get("stop_on_miss", False))},
    }


def normalize_policy(value):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("policy must be an object")
    strategy = str(value.get("strategy") or "first-available")
    if strategy not in STRATEGIES:
        strategy = "first-available"
    try:
        fallbacks = max(0, min(8, int(value.get("max_fallbacks", 2))))
    except (TypeError, ValueError):
        fallbacks = 2
    return {
        "strategy": strategy,
        "session_affinity": bool(value.get("session_affinity", True)),
        "max_fallbacks": fallbacks,
    }


def load(engines, realms):
    """读配置；文件不存在/损坏都回落到默认（面板永远能打开）。"""
    with _LOCK:
        raw = {}
        try:
            with open(_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        rules, seen = [], set()
        for item in (raw.get("rules") or [])[:128]:
            try:
                rule = normalize_rule(item, engines, realms)
            except ValueError:
                continue
            if rule["id"] in seen:
                continue
            seen.add(rule["id"])
            rules.append(rule)
        rules.sort(key=lambda r: (r["priority"], r["name"]))
        return {"rules": rules, "policy": normalize_policy(raw.get("policy")),
                "revision": raw.get("revision") or 0}


def save(payload, engines, realms, expected_revision=None):
    """整表保存（乐观锁：revision 不符则拒绝）。"""
    with _LOCK:
        current = load(engines, realms)
        if expected_revision is not None and int(expected_revision) != int(current["revision"]):
            raise ValueError("配置已被别处修改（revision %s → %s），请刷新后重试"
                             % (expected_revision, current["revision"]))
        items = payload.get("rules")
        if not isinstance(items, list) or len(items) > 128:
            raise ValueError("rules must be an array of at most 128 items")
        rules = [normalize_rule(x, engines, realms) for x in items]
        ids = [r["id"] for r in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("规则 id 重复")
        rules.sort(key=lambda r: (r["priority"], r["name"]))
        doc = {"rules": rules, "policy": normalize_policy(payload.get("policy")),
               "revision": int(current["revision"]) + 1, "saved_at": int(time.time())}
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _path())
        return doc


def _model_hit(patterns, model):
    if not patterns:
        return True
    for pat in patterns:
        if fnmatch.fnmatchcase(model, pat.lower()):
            return True
    return False


def match_rule(rules, model, realm, engine):
    """返回命中的第一条规则（按 priority）。"""
    model = str(model or "").lower()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        match = rule.get("match") or {}
        if not _model_hit(match.get("models"), model):
            continue
        if match.get("realms") and realm and realm not in match["realms"]:
            continue
        if match.get("engines") and engine and engine not in match["engines"]:
            continue
        return rule
    return None


def reorder(candidates, model, realm, available, cfg, advance=True):
    """把默认候选链按编排规则重排。

    candidates: [(engine, realm)] 来自 router.candidates_for
    available:  set((engine, realm)) 当前真正可用的组合（快照）
    """
    if not candidates:
        return candidates, None
    rules = (cfg or {}).get("rules") or []
    policy = (cfg or {}).get("policy") or DEFAULT_POLICY
    first_engine = candidates[0][0]
    rule = match_rule(rules, model, realm, first_engine)
    if not rule:
        return candidates, None

    chain = []
    for hop in rule["action"]["chain"]:
        for eng, rl in candidates:
            if eng == hop["engine"] and (not hop["realm"] or rl == hop["realm"]):
                if (eng, rl) not in chain:
                    chain.append((eng, rl))
    if not chain:
        # 规则指向的组合当前都不在候选里 —— fail-open 用默认链，除非显式关闭
        if rule["action"].get("stop_on_miss"):
            return [], rule
        return candidates, rule

    rest = [c for c in candidates if c not in chain]
    merged = chain + rest
    if policy.get("strategy") == "round-robin" and len(merged) > 1:
        key = (rule["id"], str(model or ""))
        with _LOCK:
            idx = _RR.get(key, 0) % len(merged)
            if advance:
                _RR[key] = idx + 1
        merged = merged[idx:] + merged[:idx]
    limit = int(policy.get("max_fallbacks") or 0) + 1
    return merged[:max(1, limit)], rule


def preview(model, realm, engines, realms, snapshot_ready, snapshot_holds, candidates_for):
    """干跑：命中哪条规则、最终链长什么样、每一跳是否可用。无副作用。"""
    cfg = load(engines, realms)
    base = list(candidates_for(model, realm) or [])
    available = set((e, r) for (e, r) in base if snapshot_holds(e, r, model))
    ordered, rule = reorder(base, model, realm, available, cfg, advance=False)
    hops = []
    for eng, rl in ordered:
        hops.append({"engine": eng, "realm": rl,
                     "ready": int((snapshot_ready or {}).get(eng) or 0),
                     "holds_model": (eng, rl) in available})
    return {"model": model, "realm": realm, "matched_rule": (rule or {}).get("id"),
            "matched_name": (rule or {}).get("name"), "hops": hops,
            "policy": cfg["policy"], "rules_total": len(cfg["rules"])}