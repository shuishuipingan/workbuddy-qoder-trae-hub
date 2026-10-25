# -*- coding: utf-8 -*-
"""_test_merged_fallback.py —— 合并层「回退到 qoder」的决定性集成证明。

老板原话：合并层同名模型默认国内优先，国内的额度/账号用完了要能**退到 qoder**。
候选链是 wb:cn -> qd:cn -> wb:intl -> qd:intl；这一套专门钉住「wb 耗尽 -> qd 接管」
这一环，用两个 stub 引擎（不碰任何真实账号）：

  * stub-wb：/health 报很多就绪账号（于是 auto 排序让 wb 排第一）、/v1/models
    两区都列出目标模型（于是 wb 进入候选）、/v1/chat/completions **永远 503**
    （"no usable account"，模拟国内额度/账号耗尽）；
  * stub-qd：/health 就绪较少、/v1/models 也列出目标模型、/v1/chat/completions
    **200** 且回一个带标记的 body。

判据：合并 key 打 /api/v1/chat/completions -> 最终 200、X-Hub-Route 落在 qd:cn、
X-Hub-Route-Attempts 里能看到 wb:cn 先试了 503。这正是"退回 qoder"的证据。

口径与 _test_portal.py 一致：harness 的 check()，自报 SUMMARY 行；stub 引擎与
portal 都跑在本进程的守护线程上，结束时 shutdown + server_close，不留孤儿。
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

_PORTAL = os.path.join(P.HUB, "core", "portal.py")
if not os.path.exists(_PORTAL):
    H.skip("F01 hub/core/portal.py 尚未落地 —— 合并回退集成判据整体跳过", _PORTAL)
    H.finish()

sys.path.insert(0, os.path.dirname(P.HUB))
from hub.core import portal, settings, keys

MODEL = "glm-5.3-flash"
MERGED_KEY = "mrg-fallback-test-001"


class _Stub(BaseHTTPRequestHandler):
    """一个可编程的假引擎：behavior 决定 chat 的返回。"""
    server_version = "stub-engine"
    behavior = "ok"          # "ok" -> 200；"exhaust" -> 503 no usable account
    marker = "stub"
    ready = 1

    def log_message(self, *a):
        pass

    def _send(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"ok": True, "accounts_ready": self.ready,
                                    "realm": "cn", "version": "stub"})
        if path in ("/v1/models", "/models"):
            return self._send(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": self.marker}]})
        return self._send(404, {"error": "stub: no such path " + path})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        path = self.path.split("?")[0]
        if path.endswith("/chat/completions"):
            if self.behavior == "exhaust":
                return self._send(503, {"error": {
                    "message": "no usable account for realm 'cn': all are disabled, "
                               "cooling down, expired or parked by the daily token limit",
                    "type": "server_error", "code": 503}})
            from urllib.parse import urlparse, parse_qs
            realms = parse_qs(urlparse(self.path).query).get("realm", [])
            return self._send(200, {"id": "stub-" + self.marker, "object": "chat.completion",
                                    "model": MODEL, "choices": [{"index": 0, "message": {
                                        "role": "assistant", "content": "hi from " + self.marker},
                                        "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                              "total_tokens": 2},
                                    "realm_seen": realms},
                              extra=[("X-WB-Realm", realms[0] if realms else "cn"),
                                     ("X-Qoder-Realm", realms[0] if realms else "cn")])
        return self._send(404, {"error": "stub: no such path " + path})


def _make_handler(behavior, marker, ready):
    return type("Handler", (_Stub,), {"behavior": behavior, "marker": marker, "ready": ready})


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = dict(headers or {})
        if data:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, dict(resp.getheaders()), payload.decode("utf-8", "replace")
    finally:
        conn.close()


def _route_log_path():
    """与 portal._record_route / _route_stats 同一口径：admin_dir 的**上一级** + logs/route.jsonl。"""
    return os.path.join(os.path.dirname(_tmp), "logs", "route.jsonl")


def _route_rows():
    p = _route_log_path()
    if not os.path.isfile(p):
        return []
    rows = []
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return rows


_tmp = tempfile.mkdtemp(prefix="hub-fallback-")
_saved_env = os.environ.get("HUB_SETTINGS")
os.environ["HUB_SETTINGS"] = os.path.join(_tmp, "settings.json")
servers = []
try:
    settings.save({
        "panel_password_default": True,
        "merged_keys": [{"id": "kmf", "name": "fallback", "key": MERGED_KEY, "enabled": True}],
        "routing": {"enabled": True, "prefer_realm": "cn", "engine_prefer": "auto"},
    })
    wb = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler("exhaust", "wb", 50))
    qd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler("ok", "qd", 3))
    servers += [wb, qd]
    for srv in (wb, qd):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    prtl = portal.PortalServer(
        ("127.0.0.1", 0), portal.PortalHandler,
        ports={"wb": wb.server_address[1], "qd": qd.server_address[1]},
        admin_dir=_tmp, default_engine="wb", proxy_timeout=15, quiet=True)
    servers.append(prtl)
    threading.Thread(target=prtl.serve_forever, daemon=True).start()
    port = prtl.server_address[1]
    time.sleep(0.25)

    # ---------- F02 wb 耗尽 -> 退回 qd ----------
    # 先记基线条数：route.jsonl 在共享的临时目录下，可能残留别的运行的记录
    _rows_before = len(_route_rows())
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer " + MERGED_KEY})
    route = headers.get("X-Hub-Route") or headers.get("x-hub-route")
    attempts = headers.get("X-Hub-Route-Attempts") or headers.get("x-hub-route-attempts")
    chain = json.loads(attempts) if attempts else []
    H.eq("F02 wb 国内耗尽 -> 最终 200（qd 接管）", status, 200)
    H.eq("F02b X-Hub-Route 落在 qd:cn", route, "qd:cn")
    H.check("F02c body 来自 qd stub", "hi from qd" in body, body[:160])
    H.check("F02d 尝试链含 wb:cn 的 503（先撞了国内 wb）",
            any(a.get("engine") == "wb" and a.get("status") == 503 for a in chain), chain)
    # ---------- F08 回退必须被**记下来**，否则面板上的「回退」永远是 0 ----------
    _new_rows = _route_rows()[_rows_before:]
    _fallback_rows = [r for r in _new_rows if int(r.get("attempts") or 1) > 1]
    H.check("F08 回退那次请求在 route.jsonl 里留下了 attempts>1 的记录",
            any(r.get("engine") == "qd" for r in _fallback_rows),
            "new=%r" % (_new_rows[-3:],))

    # ---------- F09 统计端点自己也要算对（面板「今天的实际落点」直接读它） ----------
    _st, _hd, _bd = _request(port, "GET", "/api/hub/route-stats",
                             headers={"X-Panel-Token": "admin"})
    _stats = json.loads(_bd) if _st == 200 else {}
    _today = (_stats.get("today") or {})
    H.check("F09 /api/hub/route-stats 把这次回退算进 fallbacks 与按引擎分布",
            _st == 200 and int(_today.get("fallbacks") or 0) >= 1
            and int((_today.get("by_engine") or {}).get("qd") or 0) >= 1,
            "status=%s today=%r" % (_st, _today))

    H.check("F02e 尝试链含 qd:cn 的 200（回退成功）",
            any(a.get("engine") == "qd" and a.get("status") == 200 for a in chain), chain)

    # ---------- F03 合并模型清单 ----------
    status, headers, body = _request(port, "GET", "/api/v1/models", None,
                                     {"Authorization": "Bearer " + MERGED_KEY})
    doc = json.loads(body) if status == 200 else {}
    H.eq("F03 合并 /v1/models 200", status, 200)
    H.check("F03b merged 标记", doc.get("merged") is True, doc.get("counts"))
    H.check("F03c 目标模型在合并清单里（同名合一条）",
            MODEL in {d["id"] for d in doc.get("data", [])},
            sorted(d["id"] for d in doc.get("data", [])))

    # ---------- F04 非合并 key 不偷偷换引擎 ----------
    # ★ 契约升级（本轮 · 统一 Key 层）：三种 key 都能用同一个地址 ——
    #   合并 key（全量路由）/ wb key（只走 wb）/ qd key（只走 qd）。
    #   因此「一个谁都对不上的 key」不再原样透传给默认引擎（那会把 401 伪装成
    #   引擎的业务错误，也是「假合并」的一种），改由网关**当场 401** 并说明三类 key。
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer some-other-key"})
    route = headers.get("X-Hub-Route") or headers.get("x-hub-route")
    H.check("F04 非合并 key 不带 X-Hub-Route（不走智能路由）", route is None, route)
    H.eq("F04b 未登记的 key 由网关当场 401（不到引擎）", status, 401)
    _doc = json.loads(body) if body else {}
    H.check("F04c 401 里说明三类 key 与提示（便于自查）",
            isinstance(_doc.get("error"), dict)
            and _doc["error"].get("code") == "invalid_api_key"
            and "merged" in (_doc["error"].get("known_keys") or {}),
            _doc.get("error"))
    # 引擎自己的 key 则照常放行（这就是「一个地址」的另一半）
    wb_key = (keys.engine_keys("wb") or [{}])[0].get("key")
    if wb_key:
        status, headers, body = _request(
            port, "POST", "/api/v1/chat/completions",
            {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
            {"Authorization": "Bearer " + wb_key})
        H.eq("F04d wb 引擎自己的 key 走同一个地址 -> 落到 wb（stub 503 如实回）", status, 503)

    # ---------- F06 客户端显式钉区：hub 尊重它作为首选，且权威覆盖（不拼重复参数）----------
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions?realm=intl",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer " + MERGED_KEY})
    route = headers.get("X-Hub-Route") or headers.get("x-hub-route")
    attempts = headers.get("X-Hub-Route-Attempts") or headers.get("x-hub-route-attempts")
    chain = json.loads(attempts) if attempts else []
    doc = json.loads(body) if status == 200 else {}
    H.eq("F06 钉 intl -> 200", status, 200)
    # wb stub 永远 503：钉 intl 后链 = wb:intl(503) -> qd:intl(200)。
    # 关键是「首选区被尊重成 intl」+「引擎只收到一个 realm 参数」。
    H.eq("F06b 最终落 qd:intl（首选 intl 被尊重；wb 耗尽后同区回退 qd）", route, "qd:intl")
    H.check("F06c 尝试链第一个就是 wb:intl（不是 cn）",
            chain and chain[0] == {"engine": "wb", "realm": "intl", "status": 503}, chain)
    H.eq("F06d 引擎只收到一个 realm=intl（hub 权威覆盖，没拼成 intl&cn）",
         doc.get("realm_seen"), ["intl"])

    # X-Realm 头同样要被尊重并剥掉（wb 里头优先于 query）
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer " + MERGED_KEY, "X-Realm": "intl"})
    route = headers.get("X-Hub-Route") or headers.get("x-hub-route")
    doc = json.loads(body) if status == 200 else {}
    H.eq("F07 X-Realm 头钉 intl -> 最终 qd:intl（同上：wb stub 恒 503）", route, "qd:intl")
    H.eq("F07b 引擎只收到一个 realm=intl（头被剥、query 权威）",
         doc.get("realm_seen"), ["intl"])

    # ---------- F05 关掉智能路由 -> 合并 key 也回默认引擎 ----------
    settings.set_routing({"enabled": False})
    prtl.route_snapshot.at = 0.0          # 让快照失效，确保读到新配置
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer " + MERGED_KEY})
    route = headers.get("X-Hub-Route") or headers.get("x-hub-route")
    H.check("F05 routing.enabled=False -> 合并 key 也不智能路由（无 X-Hub-Route）",
            route is None, route)
    H.eq("F05b 落默认引擎 wb -> 503", status, 503)
    settings.set_routing({"enabled": True})
finally:
    for srv in servers:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    if _saved_env is None:
        os.environ.pop("HUB_SETTINGS", None)
    else:
        os.environ["HUB_SETTINGS"] = _saved_env

H.finish()
