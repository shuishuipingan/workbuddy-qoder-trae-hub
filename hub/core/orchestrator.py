"""route orchestration —— 用户可编辑的路由编排（跨引擎）。

在 router.candidates_for() 给出的「模型 → (引擎, 区域)」候选链之上再加三件事：

    rules:   按 priority 升序匹配（模型通配 / 区域 / 候选引擎集合），命中即用规则给的链
    policy:  strategy（first-available / round-robin）+ 最大回退次数 + 亲和 TTL
    状态:    会话亲和（session → (engine, realm)，带 TTL 与容量上限）
             轮询下标（有界，LRU 淘汰）

设计约束（与 qoder2api 的 routing 对齐）：
  * 决策只有一个入口 decide()，preview 与真实转发走**同一条**路径 —— 面板看到
    的链就是实际会走的链；
  * available 参与剪枝：当前没有就绪账号的 (engine, realm) 不会被排到链首；
  * 亲和只影响**顺序**，不改变合法性：绑定的那一跳若已不在候选里（模型不支持 /
    账号耗尽），自动失效并回落到默认链，不做 fail-closed；
  * 不改写模型名、不越权账号范围；干跑（preview）无副作用。
"""
import fnmatch
import json
import os
import threading
import time
import uuid
from collections import OrderedDict

from hub.core import paths

_LOCK = threading.RLock()
STRATEGIES = ("first-available", "round-robin")
DEFAULT_POLICY = {
    "strategy": "first-available",
    "session_affinity": True,
    "max_fallbacks": 2,
    "affinity_ttl": 1800,
}

#: 轮询下标：key -> 下次起始下标。有界 + LRU，避免长跑进程缓慢涨内存。
_RR = OrderedDict()
_RR_CAP = 1024

#: 会话亲和：session_key -> (engine, realm, expires_at)。有界 + LRU。
_AFFINITY = OrderedDict()
_AFFINITY_CAP = 4096
AFFINITY_TTL_MIN = 60
AFFINITY_TTL_MAX = 86400


def _path():
    return os.path.join(paths.hub_dir(), "route-settings.json")


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
    strategy = str(value.get("strategy") or "first-available")
    if strategy not in STRATEGIES:
        strategy = "first-available"
    try:
        fallbacks = max(0, min(8, int(value.get("max_fallbacks", 2))))
    except (TypeError, ValueError):
        fallbacks = 2
    try:
        ttl = int(value.get("affinity_ttl", DEFAULT_POLICY["affinity_ttl"]))
    except (TypeError, ValueError):
        ttl = DEFAULT_POLICY["affinity_ttl"]
    ttl = max(AFFINITY_TTL_MIN, min(AFFINITY_TTL_MAX, ttl))
    return {
        "strategy": strategy,
        "session_affinity": bool(value.get("session_affinity", True)),
        "max_fallbacks": fallbacks,
        "affinity_ttl": ttl,
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
        rules = [normalize_rule(x, engines, realms) for x in (payload.get("rules") or [])]
        ids = [r["id"] for r in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("规则 id 重复")
        rules.sort(key=lambda r: (r["priority"], r["name"]))
        doc = {"rules": rules, "policy": normalize_policy(payload.get("policy")),
               "revision": int(current["revision"]) + 1, "saved_at": int(time.time())}
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _path())
        return doc


# ---------------------------------------------------------------------------
# 会话亲和状态
# ---------------------------------------------------------------------------
def normalize_session_key(value):
    """把客户端给的会话标识收敛成短 key；空值返回空串（= 不做亲和）。

    去掉来源前缀（h: 头 / b: 请求体 / d: 派生指纹）后再做 key：面板预览里手填的
    "s1" 必须能和真实请求用 X-Session-Id: s1 绑定的那条对上，否则预览永远显示
    「无亲和」，等于骗人。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    for prefix in ("h:", "b:", "d:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip()[:200]


def bind_session(session_key, engine, realm, ttl=None):
    """记录「这个会话上次落在哪」。ttl<=0 表示不记（亲和被关闭）。"""
    key = normalize_session_key(session_key)
    if not key or not engine:
        return
    try:
        ttl = int(ttl if ttl is not None else DEFAULT_POLICY["affinity_ttl"])
    except (TypeError, ValueError):
        ttl = DEFAULT_POLICY["affinity_ttl"]
    if ttl <= 0:
        return
    with _LOCK:
        _AFFINITY[key] = (engine, realm or "", time.time() + ttl)
        _AFFINITY.move_to_end(key)
        while len(_AFFINITY) > _AFFINITY_CAP:
            _AFFINITY.popitem(last=False)


def affinity_for(session_key):
    """返回 ((engine, realm) | None)。过期即删（惰性回收）。"""
    key = normalize_session_key(session_key)
    if not key:
        return None
    now = time.time()
    with _LOCK:
        row = _AFFINITY.get(key)
        if not row:
            return None
        engine, realm, expires = row
        if expires <= now:
            _AFFINITY.pop(key, None)
            return None
        _AFFINITY.move_to_end(key)
        return (engine, realm)


def clear_affinity(session_key=None):
    with _LOCK:
        if session_key is None:
            _AFFINITY.clear()
        else:
            _AFFINITY.pop(normalize_session_key(session_key), None)


def affinity_stats():
    with _LOCK:
        now = time.time()
        live = sum(1 for _e, _r, exp in _AFFINITY.values() if exp > now)
        return {"entries": len(_AFFINITY), "live": live, "cap": _AFFINITY_CAP}


# ---------------------------------------------------------------------------
# 规则匹配 / 决策
# ---------------------------------------------------------------------------
def _model_hit(patterns, model):
    if not patterns:
        return True
    for pat in patterns:
        if fnmatch.fnmatchcase(model, pat.lower()):
            return True
    return False


def match_rule(rules, model, realm, engines):
    """返回命中的第一条规则（按 priority）。

    engines 是**候选链上出现的全部引擎**（不是只有第一个）—— 规则写
    match.engines=[wb] 时，只要候选里有 wb 就算命中，否则这个维度等于废字段。
    """
    model = str(model or "").lower()
    pool = [str(e).strip().lower() for e in (engines or [])]
    for rule in rules:
        if not rule.get("enabled"):
            continue
        match = rule.get("match") or {}
        if not _model_hit(match.get("models"), model):
            continue
        if match.get("realms") and realm and realm not in match["realms"]:
            continue
        wanted = match.get("engines") or []
        if wanted and not (set(wanted) & set(pool)):
            continue
        return rule
    return None


def _rr_start(key, size):
    """轮询起点：有界 LRU 计数。"""
    if size <= 1:
        return 0
    with _LOCK:
        cursor = _RR.get(key, 0)
        _RR[key] = cursor + 1
        _RR.move_to_end(key)
        while len(_RR) > _RR_CAP:
            _RR.popitem(last=False)
        return cursor % size


def decide(candidates, model, realm, available, cfg, session_key=None):
    """唯一的决策入口：preview 与真实转发都走这里。

    candidates: [(engine, realm)]，来自 router.candidates_for
    available:  set((engine, realm)) 当前**真的**可用（模型在该引擎 + 有就绪账号）
    返回 dict: hops / rule / affinity / pruned，不改任何外部状态。
    """
    candidates = list(candidates or [])
    if not candidates:
        return {"hops": [], "rule": None, "affinity": None, "pruned": 0}
    rules = (cfg or {}).get("rules") or []
    policy = normalize_policy((cfg or {}).get("policy") or DEFAULT_POLICY)
    engines = [c[0] for c in candidates]
    rule = match_rule(rules, model, realm, engines)

    base = candidates
    if rule:
        chain = []
        for hop in rule["action"]["chain"]:
            for eng, rl in candidates:
                if eng == hop["engine"] and (not hop["realm"] or rl == hop["realm"]):
                    if (eng, rl) not in chain:
                        chain.append((eng, rl))
        if chain:
            base = chain + [c for c in candidates if c not in chain]
        elif rule["action"].get("stop_on_miss"):
            # 规则指向的组合当前都不在候选里 + 显式 fail-closed
            return {"hops": [], "rule": rule, "affinity": None, "pruned": 0}

    # ① 剪枝：当前没有就绪账号的组合不排到链首（全不可用时 fail-open 保留原链）
    pruned = 0
    if available:
        usable = [c for c in base if c in available]
        if usable:
            pruned = len(base) - len(usable)
            tail = [c for c in base if c not in usable]
            base = usable + tail
        elif rule and rule["action"].get("stop_on_miss"):
            return {"hops": [], "rule": rule, "affinity": None, "pruned": 0}

    # ② 会话亲和：只调顺序，不改合法性
    bound = affinity_for(session_key) if policy.get("session_affinity") else None
    if bound and bound in base:
        base = [bound] + [c for c in base if c != bound]

    hops = list(base)
    # ③ 策略
    if policy["strategy"] == "round-robin" and len(hops) > 1 and not (bound and hops[0] == bound):
        key = ((rule or {}).get("id") or "default", str(model or ""), str(realm or ""))
        start = _rr_start(key, len(hops))
        hops = hops[start:] + hops[:start]
        if bound and bound in hops:      # 轮询也优先保住亲和那一跳
            hops = [bound] + [c for c in hops if c != bound]

    limit = int(policy.get("max_fallbacks") or 0) + 1
    hops = hops[:max(1, limit)]
    return {"hops": hops, "rule": rule, "affinity": bound, "pruned": pruned}


def reorder(candidates, model, realm, available, cfg, session_key=None):
    """兼容旧调用：返回 (hops, rule)。"""
    out = decide(candidates, model, realm, available, cfg, session_key=session_key)
    return out["hops"], out["rule"]


def rebind(session_key, hops, cfg=None):
    """请求成功后把「实际服务的那一跳」写回亲和表。"""
    if not hops:
        return None
    policy = normalize_policy(((cfg or {}).get("policy") or DEFAULT_POLICY))
    if not policy.get("session_affinity"):
        return None
    engine, realm = hops[0]
    bind_session(session_key, engine, realm, ttl=policy.get("affinity_ttl"))
    return (engine, realm)


def preview(model, realm, engines, realms, snapshot_ready, snapshot_holds,
            candidates_for, session_key=None, available=None):
    """干跑：命中哪条规则、最终链长什么样、每一跳是否可用。无副作用。

    与真实路径共用 decide()，因此面板看到的顺序就是请求会走的顺序。
    """
    cfg = load(engines, realms)
    base = list(candidates_for(model, realm) or [])
    holds = set((e, r) for (e, r) in base if snapshot_holds(e, r, model))
    ready_ok = set((e, r) for (e, r) in holds
                   if int((snapshot_ready or {}).get(e) or 0) > 0)
    if available is None:
        available = ready_ok
    out = decide(base, model, realm, available, cfg, session_key=session_key)
    hops = []
    for eng, rl in out["hops"]:
        hops.append({"engine": eng, "realm": rl,
                     "ready": int((snapshot_ready or {}).get(eng) or 0),
                     "holds_model": (eng, rl) in holds,
                     "usable": (eng, rl) in available})
    return {"model": model, "realm": realm,
            "matched_rule": (out["rule"] or {}).get("id"),
            "matched_name": (out["rule"] or {}).get("name"),
            "hops": hops,
            "affinity": {"engine": out["affinity"][0], "realm": out["affinity"][1]}
                        if out["affinity"] else None,
            "pruned": out["pruned"],
            "policy": cfg["policy"], "rules_total": len(cfg["rules"])}
