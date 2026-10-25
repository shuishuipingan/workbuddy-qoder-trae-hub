# -*- coding: utf-8 -*-
"""_test_token_refresh.py —— 缓存的 X-Panel-Token 失效后，必须**自动换一次再试**。

由来（第 59 轮）：aggregator.forget_token() 一直存在，但**没有任何调用点** ——
也就是说"改了面板密码 / 引擎重启"之后，缓存里的旧令牌会继续被用，整个面板一片 401，
而这条自愈能力从来没生效过。死函数比死代码更危险：它看起来像一道防线。

做法：起一个本地假服务（真 HTTP，不 mock urlopen）：
  · POST /panel/login  → 每次发一个新令牌 t1、t2 …
  · GET  /probe        → 只认最新那个；旧令牌回 401
然后让聚合器去请求 /probe：它必须自己发现 401、丢掉缓存、重新登录、再试一次并成功。
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

AGG = os.path.join(P.HUB, "core", "aggregator.py")
if not os.path.exists(AGG):
    H.skip("T01 hub/core/aggregator.py 尚未落地 —— 令牌刷新判据整体跳过", AGG)
    H.finish()

sys.path.insert(0, P.REPO)          # 包名是 hub → 加的是 hub/ 的**父目录**
from hub.core import aggregator as A  # noqa: E402

STATE = {"login": 0, "tokens": [], "sid": 1}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):      # 静音：测试输出里不要访问日志
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/panel/login"):
            STATE["login"] += 1
            STATE["sid"] += 1
            self._send(200, {"ok": True, "token": "t%d" % STATE["sid"]})
            return
        self._send(404, {"error": "no such route"})

    def do_GET(self):
        tok = self.headers.get("X-Panel-Token") or ""
        STATE["tokens"].append(tok)
        if self.path.startswith("/probe"):
            # 只认**最新**登录拿到的令牌；旧的一律 401（模拟密码被改 / 引擎重启）
            if tok == "t%d" % STATE["sid"]:
                self._send(200, {"ok": True, "seen": tok})
            else:
                self._send(401, {"error": "panel password required"})
            return
        self._send(404, {"error": "no such route"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()

try:
    A._TOKENS.clear()
    STATE["login"] = 0
    STATE["tokens"] = []
    STATE["sid"] = 1

    # 先登录一次，让缓存里存下 t2
    ports = {"wb": port}
    tok1 = A.ensure_token("wb", ports, password="admin")
    H.check("T01 首次登录取到令牌（假服务在跑）", bool(tok1), "token=%r" % (tok1,))

    # 服务端"换了密码"：把最新令牌推进一格，缓存里的 t2 于是失效
    STATE["sid"] += 1
    before = STATE["login"]

    status, _hdrs, raw = A._request("wb", ports, "/probe")
    tried = list(STATE["tokens"])
    H.check("T02 旧令牌 401 后自动重登并重试成功（不是把 401 直接抛给上层）",
            status == 200, "status=%s tried=%r" % (status, tried))
    H.check("T03 重试确实换过一次令牌（登录次数 +1，且两次请求带的令牌不同）",
            STATE["login"] == before + 1 and len(set(tried)) == 2,
            "login=%d before=%d tried=%r" % (STATE["login"], before, tried))
    H.check("T04 缓存里现在是最新令牌（下次不会再 401 一次）",
            (A._TOKENS.get("wb") or {}).get("token") == "t%d" % STATE["sid"],
            str(A._TOKENS.get("wb")))

    # 对照：不是 401 的状态码**不许**触发重试（避免把真错误吞成重试风暴）
    STATE["tokens"] = []
    login_before = STATE["login"]
    status2, _h2, _r2 = A._request("wb", ports, "/nope")
    H.check("T05 404 不触发重试（只有 401/403 才换令牌）",
            status2 == 404 and STATE["login"] == login_before,
            "status=%s login=%d->%d" % (status2, login_before, STATE["login"]))
finally:
    srv.shutdown()
    A._TOKENS.clear()

H.finish()
