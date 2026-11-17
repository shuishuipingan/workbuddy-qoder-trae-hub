#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/core/router.py —— 合并模式智能路由。

一个合并 key 同时打国内/国际 × wb/qd 两引擎。路由矩阵由 portal 持有：

  * 快照（TTL 30s，单飞探测）：各引擎 × 两区域的模型清单（/v1/models?realm=X）
    + 总就绪账号数（/health）—— 本地回环探测，失败不致命（不参与候选）。
  * 候选序列 candidates_for(model)：
      区域优先：prefer_realm（默认 cn）先，另一区兜底；
      区域内引擎顺序：engine_prefer=auto 时按就绪账号多者先（并列 wb 先），
      显式 wb/qd 时按指定顺序。
  * 兜底语义：portal 逐个尝试候选，503/429/502/504（未流式输出前）换下一个。
  * 合并模型清单 merged_model_list()：同名模型合一条，带 engines 与 realm。

只用标准库。
"""
import http.client
import json
import threading
import time

# 引擎名单**只从注册表来**（第 100 轮）。
from hub import ENGINES  # noqa: E402
REALMS = ("cn", "intl")

#: 合并路由只作用于这些聊天端点（+ /v1/models 合并清单）。
#: 存**去首斜杠**的形式 —— portal 传来的 rest 是 /api/ 之后剥掉前缀的路径。
CHAT_PATHS = frozenset(p.strip("/") for p in (
    "/v1/chat/completions", "/chat/completions",
    "/v1/completions", "/completions",
    "/v1/messages", "/messages",
    "/v1/responses", "/responses",
))
#: 未开始流式输出前的可重试状态。
#: 401 单独处理：只有当 hub 注入的是**引擎自己的 key**（客户端那把没在各引擎登记）
#: 时，换引擎才可能有意义；如果发的是客户端那把 key，401 就是「key 错」，跨引擎
#: 重试只会把真实原因埋掉。判定见 should_retry()。
RETRYABLE_STATUS = frozenset((429, 502, 503, 504))
KEY_RETRY_STATUS = frozenset((401,))

SNAPSHOT_TTL = 30.0
#: 探测串行化：同一时刻只让一个线程真正访问引擎，其余等它出结果（最多等这么久，
#: 超时就用旧快照 —— 宁可短暂陈旧，也不要 N 个线程各探 6 次打出惊群）。
_REFRESH_LOCK = threading.Lock()
_REFRESH_WAIT = 15.0


def should_retry(status, key_injected=False):
    """这次失败值不值得换下一跳？

    key_injected=True 表示本次发给引擎的是 hub 拼的引擎自有 key（不是客户端那把），
    此时 401 说明「这台引擎的 key 不可用」，换引擎有救；否则 401 是客户端 key 问题，
    直接返回给客户端，别把错误信息洗成 503。
    """
    if status in RETRYABLE_STATUS:
        return True
    return status in KEY_RETRY_STATUS and bool(key_injected)


class RouteSnapshot(object):
    """路由快照：models[engine][realm]=set(ids)；ready[engine]=int|None。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.at = 0.0
        self.models = {}
        self.ready = {}
        self.errors = {}

    def is_fresh(self):
        with self.lock:
            return time.time() - self.at < SNAPSHOT_TTL


def _http(port, path, key=None, timeout=8.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + str(key)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, payload
    except OSError as exc:
        return 0, ("connection failed: %s" % exc).encode("utf-8", "replace")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _key_for(key, engine):
    """key 可以是**字符串**（两侧同一把）或**{engine: key} 字典**。

    字典是为了「合并 key 没在引擎里登记」的场景：hub 用各引擎自己的一把启用
    key 去抓清单，否则 /v1/models 会 401、快照空掉、候选退化成默认引擎。
    """
    if isinstance(key, dict):
        return key.get(engine)
    return key


def _probe(ports, key):
    """真正访问三台引擎，返回 (models, ready, errors)。"""
    models, ready, errors = {}, {}, {}
    for engine in ENGINES:
        port = (ports or {}).get(engine)
        if not port:
            continue
        models[engine], ready[engine], errors[engine] = {}, None, None
        status, body = _http(port, "/health")
        if status == 200:
            try:
                doc = json.loads(body.decode("utf-8", "replace"))
                ready[engine] = int(doc.get("accounts_ready") or 0)
            except Exception:
                ready[engine] = None
        else:
            errors[engine] = "health %s" % status
        for realm in REALMS:
            status, body = _http(port, "/v1/models?realm=" + realm,
                                 key=_key_for(key, engine))
            ids = set()
            if status == 200:
                try:
                    doc = json.loads(body.decode("utf-8", "replace"))
                    for item in doc.get("data") or []:
                        mid = str(item.get("id") or "").strip().lower()
                        if mid:
                            ids.add(mid)
                except Exception:
                    errors[engine] = "models parse error"
            else:
                errors[engine] = "models %s %s" % (realm, status)
            models[engine][realm] = ids
    return models, ready, errors


def refresh_snapshot(ports, key, snapshot, force=False):
    """按 TTL 重建快照；真正的探测由单飞锁串行化。

    并发时只有一个线程访问引擎（3 引擎 × (1 次 /health + 2 次 /v1/models)），
    其余线程等它写完直接复用；等锁超过 _REFRESH_WAIT 秒就带着旧快照返回 ——
    短暂陈旧好过把引擎打出惊群。
    """
    if not force and snapshot.is_fresh():
        return snapshot
    if not _REFRESH_LOCK.acquire(timeout=_REFRESH_WAIT):
        return snapshot
    try:
        # 等锁期间可能已有别的线程刷新过，二次确认避免重复探测
        if not force and snapshot.is_fresh():
            return snapshot
        models, ready, errors = _probe(ports, key)
        with snapshot.lock:
            snapshot.models = models
            snapshot.ready = ready
            snapshot.errors = errors
            snapshot.at = time.time()
        return snapshot
    finally:
        _REFRESH_LOCK.release()


def engine_holds(snapshot, engine, realm, model):
    return model in ((snapshot.models.get(engine) or {}).get(realm) or set())


def candidates_for(model, snapshot, config, requested=None):
    """(engine, realm) 候选序列。config 是 hub/core/settings.py routing() 的形状。

    requested：客户端**显式钉**的区域（?realm= / X-Realm）。钉了就把它当本次首选区，
    另一区仍然留在链尾兜底 —— 「显式优先，但不把路走死」。没钉就用 config 的
    prefer_realm。
    """
    model = str(model or "").strip().lower()
    if not model:
        return []
    engine_prefer = str((config or {}).get("engine_prefer") or "auto").strip().lower()
    if engine_prefer not in ("wb", "qd", "auto"):
        engine_prefer = "auto"
    requested = str(requested or "").strip().lower()
    if requested in REALMS:
        first = requested
    else:
        first = str((config or {}).get("prefer_realm") or "cn").strip().lower()
        if first not in REALMS:
            first = "cn"
    realms = [first] + [r for r in REALMS if r != first]
    out = []
    for realm in realms:
        holders = [e for e in ENGINES if engine_holds(snapshot, e, realm, model)]
        if engine_prefer == "auto":
            holders.sort(key=lambda e: (-(snapshot.ready.get(e) or 0),
                                        0 if e == "wb" else 1))
        else:
            holders.sort(key=lambda e: 0 if e == engine_prefer else 1)
        for engine in holders:
            out.append((engine, realm))
    return out


def merged_model_list(snapshot):
    """合并 /v1/models：同名模型合一条，带 engines（含区域）与 realm 并集。"""
    by_id = {}
    for engine in ENGINES:
        for realm in REALMS:
            for mid in sorted((snapshot.models.get(engine) or {}).get(realm) or set()):
                entry = by_id.setdefault(mid, {
                    "id": mid, "object": "model", "owned_by": "hub",
                    "engines": {}, "realm": [],
                })
                entry["engines"].setdefault(engine, [])
                if realm not in entry["engines"][engine]:
                    entry["engines"][engine].append(realm)
                if realm not in entry["realm"]:
                    entry["realm"].append(realm)
    out = []
    for mid in sorted(by_id):
        entry = by_id[mid]
        for engine in ENGINES:
            entry["engines"].setdefault(engine, [])
        realms = sorted(entry["realm"])
        entry["realm"] = realms
        entry["realm_exclusive"] = len(realms) == 1
        out.append(entry)
    return out


def extract_model(body):
    """从请求体 JSON 里拿模型名（chat/completions/responses/messages 都是 model 键）。"""
    if not body:
        return ""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return ""
    return str((doc.get("model") if isinstance(doc, dict) else "") or "").strip()
