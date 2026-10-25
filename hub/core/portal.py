#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/core/portal.py —— 融合面：一个端口上同时端出 admin 静态站 + 两引擎的接口。

它自己不实现任何业务，只做三件事：

  1. 静态：GET / 与 /admin/* 从 admin/ 目录直吐（正确 MIME、no-store、目录穿越防护）；
  2. 聚合：/api/hub/{health,engines,usage,models} 走 hub/core/aggregator.py；
  3. 反代：/api/<engine>/<path> 打到对应引擎，**并额外提供 /api/<name> 这一条
     「无引擎前缀」路径**——它落到默认引擎，可用 ?engine=qd 覆盖。
     这一条是兼容关键：现有 admin 前端调的是 /api/health、/api/usage…，
     有了它，前端一行都不用改就能跑在融合面上。

鉴权口径（与引擎一致，**不做任何 Cookie 域处理**）：
  * 客户端带来的 X-Panel-Token / Cookie / Authorization 一律原样透传给引擎；
  * 回包的状态码与响应头（含 Set-Cookie，重复头逐条保留）原样回传；
  * portal 自己不校验任何凭据 —— 谁有权限，由引擎回答（401 就是 401）。

用法：

    # 1) 先起两个引擎（同进程双端口）
    python -m hub.server --wb-port 8801 --qd-port 8802
    # 2) 再起融合面
    python -m hub.core.portal --port 8791 --wb-port 8801 --qd-port 8802
    # 3) 打开 http://127.0.0.1:8791/  （admin 静态站）
    #    curl http://127.0.0.1:8791/api/hub/health
    #    curl http://127.0.0.1:8791/api/wb/health      # 显式打到 wb
    #    curl http://127.0.0.1:8791/api/qd/health      # 显式打到 qd
    #    curl http://127.0.0.1:8791/api/health         # 默认引擎
    #    curl 'http://127.0.0.1:8791/api/health?engine=qd'

只用标准库。
"""

import argparse
import gzip
import hashlib
import threading
import http.client
import json
import os
import re
import socket
import subprocess
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from hub import ENGINES, ENGINE_LABELS  # noqa: E402  引擎注册表（唯一真源）
from hub.core import paths  # noqa: E402

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from hub import HUB_VERSION, HUB_NAME, DEFAULT_PORTS, HUB_PORT, resolve_engine
    from hub.core import aggregator, router, settings, updater
else:
    from .. import HUB_VERSION, HUB_NAME, DEFAULT_PORTS, HUB_PORT, resolve_engine
    from . import aggregator, router, settings, keys, paths, updater


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_ADMIN_DIR = os.path.join(REPO_ROOT, "admin")


# 重复请求检测：同一份 (model + messages) 在几秒内到两次，最常见是客户端重发
# （超时误判 / 双发），也可能是网关自己重试。记一条带指纹的日志，
# 让"到底谁发了两遍"有据可查 —— 不用靠猜。
_RECENT_REQUESTS = {}
_RECENT_LOCK = threading.Lock()
_DUP_WINDOW = 5.0


def _request_fingerprint(body):
    try:
        doc = json.loads(body.decode("utf-8", "replace")) if body else {}
    except Exception:
        return ""
    if not isinstance(doc, dict):
        return ""
    head = "%s|%s" % (doc.get("model"), len(doc.get("messages") or []))
    try:
        blob = json.dumps(doc.get("messages"), sort_keys=True, ensure_ascii=False)
    except Exception:
        blob = repr(doc.get("messages"))
    return hashlib.sha1((head + blob).encode("utf-8", "replace")).hexdigest()[:16]


def _note_duplicate(fingerprint):
    """返回 (是否 5 秒内重复, 距上次的秒数)。"""
    if not fingerprint:
        return False, 0.0
    now = time.time()
    with _RECENT_LOCK:
        last = _RECENT_REQUESTS.get(fingerprint)
        _RECENT_REQUESTS[fingerprint] = now
        if len(_RECENT_REQUESTS) > 512:
            for k in [k for k, v in list(_RECENT_REQUESTS.items()) if now - v > 60]:
                _RECENT_REQUESTS.pop(k, None)
    if last and now - last <= _DUP_WINDOW:
        return True, now - last
    return False, 0.0

API_PREFIX = "/api"
HUB_PREFIX = API_PREFIX + "/hub"

#: 显式 MIME 表。.js 必须是 text/javascript —— 模块脚本对 MIME 敏感。
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}

#: 逐跳头（RFC 7230 6.1）：两侧都不转发。
HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
))
#: 请求侧额外丢掉：Host 由 http.client 重算；Content-Length 由它按实发字节重算。
#: 注意 **不丢** Accept-Encoding —— 原样转发 + 原样回传 Content-Encoding，
#: 才能真正做到「回包原样透传」。
DROP_REQUEST_HEADERS = HOP_BY_HOP | frozenset(("host", "content-length"))
#: 响应侧额外丢掉：长度我们自己按收到的字节写。
DROP_RESPONSE_HEADERS = HOP_BY_HOP | frozenset(("content-length",))


# --------------------------------------------------------------------------- 小工具
def safe_join(root, url_path):
    """把 URL 路径钉在 root 内；越界/非法返回 None。

    三道闸（与 hub/dev 的 serve.py、wb_atrest._safe_path 同一套思路）：
    逐段丢弃 ".." → realpath → 要求结果仍在 root 内。
    """
    root_real = os.path.realpath(root)
    rel = urllib.parse.unquote(str(url_path or "/").split("?", 1)[0].split("#", 1)[0])
    rel = rel.replace("\\", "/")
    parts = [seg for seg in rel.split("/") if seg and seg not in (".", "..")]
    if not parts:
        parts = ["index.html"]
    candidate = os.path.realpath(os.path.join(root_real, *parts))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        return None
    return candidate


def guess_mime(path):
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def lower_path(rest):
    return str(rest or "").strip().strip("/").lower()


def port_owner_hint(port):
    if os.name == "nt":
        return 'netstat -ano | findstr :%d     然后再 tasklist /FI "PID eq <末列PID>"' % port
    if sys.platform == "darwin":
        return "lsof -nP -iTCP:%d -sTCP:LISTEN" % port
    return "ss -ltnp 'sport = :%d'   或   lsof -nP -iTCP:%d -sTCP:LISTEN" % (port, port)


# --------------------------------------------------------------------------- 处理器

#: 只允许探测这几家的授权页（避免变成任意 URL 的探测器）。
_FRAME_CHECK_HOSTS = ("workbuddy.ai", "workbuddy.cn", "qoder.com", "qoder.com.cn",
                      "trae.ai", "trae.com.cn", "trae.cn")


def _frame_check(url):
    """返回 {"frameable": bool, "reason": str}。探测失败一律当"不能内嵌"。"""
    import urllib.parse as _up
    import urllib.request as _ur
    try:
        parsed = _up.urlparse(str(url or ""))
    except Exception:
        return {"frameable": False, "reason": "bad url"}
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return {"frameable": False, "reason": "bad url"}
    if not any(host == h or host.endswith("." + h) for h in _FRAME_CHECK_HOSTS):
        return {"frameable": False, "reason": "host not allowed"}
    try:
        req = _ur.Request(url, method="GET", headers={"User-Agent": "hub-frame-check/1"})
        with _ur.urlopen(req, timeout=6) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except Exception as exc:
        return {"frameable": False, "reason": "probe failed: %s" % str(exc)[:80]}
    xfo = (headers.get("x-frame-options") or "").lower()
    if "deny" in xfo or "sameorigin" in xfo:
        return {"frameable": False, "reason": "x-frame-options: %s" % xfo}
    csp = headers.get("content-security-policy") or ""
    if "frame-ancestors" in csp and "'none'" in csp:
        return {"frameable": False, "reason": "csp frame-ancestors 'none'"}
    return {"frameable": True, "reason": ""}

class PortalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hub-portal/" + HUB_VERSION  # 值在类外补丁（见文件末），避免引用顺序问题

    #: 高频轮询的 GET：2xx 时不写日志（与两个引擎同一口径）。
    #  为什么要有：面板「请求流水」每 5 秒刷新一次，加上健康探针与静态资源，
    #  一个开着面板的窗口一小时能刷掉上千行 —— 真正的 4xx/5xx 就被埋了。
    #  非 GET、4xx/5xx 一律照常记录。
    QUIET_PATH_PREFIXES = (
        "/health", "/favicon.ico",
        "/api/hub/health", "/api/hub/engines", "/api/hub/keys",
        "/api/hub/accounts", "/api/hub/route-stats", "/api/hub/logs",
        "/api/hub/usage", "/api/hub/rewards", "/api/hub/scheduler",
        "/api/hub/timeseries", "/api/hub/account-health",
        "/css/", "/js/", "/fonts/", "/img/",
    )

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        try:
            status = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 200
            if status < 400 and getattr(self, "command", "GET") == "GET":
                req_path = (getattr(self, "path", None) or (args[0] if args else "")).split("?")[0]
                if any(req_path == p or req_path.startswith(p) for p in self.QUIET_PATH_PREFIXES):
                    return
        except Exception:  # noqa: BLE001 —— 日志过滤不许影响请求本身
            pass
        sys.stderr.write("[portal] %s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- 连接健壮性
    def handle_one_request(self):
        """客户端提前断开时**安静收场**，不要把堆栈打到日志里。

        两个方向都要接住（第 64–65 轮实测各出现过一次）：
          · 读：客户端发完请求就 RST → rfile.readline() 抛 ConnectionResetError(10054)
          · 写：服务端回包时对方已关 → wfile.write 抛 ConnectionAbortedError(10053)
        这两件事都不是服务端错误；长跑的服务里它们会把真正的问题淹掉。
        """
        try:
            super().handle_one_request()
        except (ConnectionError, OSError):
            self.close_connection = True

    # ---------------------------------------------------------------- 响应助手
    def _send(self, status, body, content_type, extra_headers=None, head_only=False,
              cache_control=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # 第 12 轮：面板切到构建产物后，首屏是 400KB 级别的 JS —— 本地也该走 gzip。
        #   只压文本类且客户端明确接受；二进制（字体/图片）不碰。
        encoding = None
        accept = (self.headers.get("Accept-Encoding") or "").lower() if self.headers else ""
        is_text = any(t in content_type for t in ("text/", "javascript", "json", "svg", "xml"))
        if is_text and len(body) >= 1024 and "gzip" in accept:
            try:
                body = gzip.compress(body, 6)
                encoding = "gzip"
            except Exception:  # noqa: BLE001 —— 压不了就原样发
                encoding = None
        # 标记：这个请求已经写过响应了。
        # _merged_request 用 _json 写完返回 None，调用处只看返回值会以为没处理，
        # 于是再转发一次 —— 一个请求两份响应，keep-alive 上后续响应整体错位。
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control or "no-store, no-cache, must-revalidate")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        if not head_only and self.command != "HEAD":
            # 客户端提前断开（关页面、超时、切网络）不是服务端错误 —— 不该刷一整屏堆栈。
            # 实测：测试套件与浏览器取消请求时，socketserver 会打 ConnectionAbortedError(10053)。
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                self.close_connection = True

    def _json(self, status, obj, head_only=False):
        self._send(status, json.dumps(obj, ensure_ascii=False), MIME_TYPES[".json"],
                   head_only=head_only)

    def _read_body(self):
        """读请求体：支持 Content-Length 与 **chunked**。

        第 33 轮：只认 Content-Length 时，用 chunked 上传的客户端（Node 的
        fetch 流式 body、部分 SDK、curl -H "Transfer-Encoding: chunked"）会
        被读成空 body，引擎于是回 400 invalid JSON body —— 但客户端看到的是
        "本轮运行失败"，完全查不到原因。
        """
        # 同一请求会被读两次：_merged_request 先读（判断要不要智能路由），
        # 没进合并分支时 _proxy 还会再读一次，而 rfile 已经空了 ——
        # 引擎收到空 body 就回 400 invalid JSON body（实测：DSH 客户端三轮全挂）。
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            self._body_cache = self._decode_content_encoding(self._read_chunked_body())
            return self._body_cache
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._body_cache = b""
            return self._body_cache
        self._body_cache = self._decode_content_encoding(self.rfile.read(length))
        return self._body_cache

    def _decode_content_encoding(self, raw):
        """在门户把压缩的请求体解开，再交给引擎。"""
        enc = (self.headers.get("Content-Encoding") or "").strip().lower()
        if not raw or not enc:
            return raw
        try:
            if "gzip" in enc:
                import gzip as _gz
                return _gz.decompress(raw)
            if "deflate" in enc:
                import zlib as _zl
                try:
                    return _zl.decompress(raw)
                except Exception:
                    return _zl.decompress(raw, -_zl.MAX_WBITS)
        except Exception:
            return raw
        return raw

    def _read_chunked_body(self, limit=64 * 1024 * 1024):
        """解码 HTTP/1.1 chunked 请求体（带总长度上限，防打爆内存）。"""
        out = bytearray()
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                break
            if size == 0:
                # 吃掉 trailer（以空行为界）
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            chunk = self.rfile.read(size)
            out.extend(chunk)
            self.rfile.read(2)  # 每块后面的 CRLF
            if len(out) > limit:
                break
        return bytes(out)

    def _split(self):
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path or "/", parsed.query or ""

    # ---------------------------------------------------------------- 方法入口
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    # ---------------------------------------------------------------- 路由
    def _dispatch(self, method):
        # ★ 每个请求都要清空请求体缓存！
        #   handler 实例是**按连接**复用的（HTTP/1.1 keep-alive），一个实例会依次
        #   处理同一条连接上的多个请求。缓存不清，第二个请求就会拿到上一个请求的
        #   body —— 表现为「另一个会话的内容串进来了」。实测：两个工作区共用连接池
        #   时，第二个窗口的思维链会变成第一个窗口的。
        self._body_cache = None
        self._responded = False
        self._stream_committed = False
        path, query = self._split()

        # 1) 聚合端点（默认只读；唯一例外是 POST /api/hub/settings/save）
        if path == HUB_PREFIX or path.startswith(HUB_PREFIX + "/"):
            if method not in ("GET", "HEAD"):
                # 聚合端点默认只读；允许的两个写入口是白名单（不是"前缀匹配"）：
                #   /settings/save 存合并层配置 · /try 就地试打（真的往自己的 /api/v1 发一条）
                allowed_post = (HUB_PREFIX + "/settings/save", HUB_PREFIX + "/try")
                if not (method == "POST" and path in allowed_post):
                    return self._json(405, {"error": "hub endpoints are read-only "
                                                    "(GET/HEAD; POST /settings/save or /try)",
                                            "path": path})
            return self._handle_hub(path[len(HUB_PREFIX):] or "/", query,
                                    head_only=(method == "HEAD"), method=method)

        # 2) 反代：/api/<engine>/<path> 与 /api/<name>（默认引擎）
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            return self._handle_api(path, query, method)

        # 2b) OpenAI 兼容别名：/v1/* → /api/v1/*
        #     所有 OpenAI 客户端（openai-python / Cherry Studio / LobeChat …）默认把
        #     base_url 写成 http://host:port/v1，请求就是 /v1/models、/v1/chat/completions。
        #     以前只认 /api/v1，于是它们全部 404 —— 表现成"网关没起来"。
        if path == "/v1" or path.startswith("/v1/"):
            return self._handle_api(API_PREFIX + path, query, method)

        # 3) 静态：/ 与 /admin/*
        if method in ("GET", "HEAD"):
            return self._handle_static(path, head_only=(method == "HEAD"))
        return self._json(405, {"error": "static assets are GET/HEAD only", "path": path})

    # ---- 1) 聚合
    def _handle_hub(self, rest, query, head_only=False, method="GET"):
        rest = rest.strip("/")
        ports = self.server.ports
        params = urllib.parse.parse_qs(query or "")
        realm = (params.get("realm") or [None])[0] or None

        if rest in ("health", ""):
            return self._json(200, aggregator.hub_health(ports), head_only=head_only)
        if rest == "engines":
            return self._json(200, {"version": HUB_VERSION,
                                    "default_engine": self.server.default_engine,
                                    "engines": aggregator.engine_summary(ports)},
                              head_only=head_only)
        if rest == "usage":
            q3 = urllib.parse.parse_qs(query or "")
            return self._json(200, aggregator.hub_usage(
                ports, realm=realm,
                range_=(q3.get("range", [None]) or [None])[0],
                since=(q3.get("since", [None]) or [None])[0],
                until=(q3.get("until", [None]) or [None])[0]),
                head_only=head_only)
        if rest == "account-health":
            # 按账号的"最近失败"（第 114 轮）：账号页据此打标，不必翻请求流水。
            q3 = urllib.parse.parse_qs(query or "")
            try:
                lim = int((q3.get("limit", ["300"]) or ["300"])[0])
            except (TypeError, ValueError):
                lim = 300
            return self._json(200, aggregator.hub_account_health(ports, limit=lim),
                              head_only=head_only)
        if rest == "timeseries":
            # 三家时序合并（第 105 轮）：前端一个请求就够 —— 浏览器只有合并令牌，
            # 逐引擎打会 401（引擎只认自己的 <token>::<engine> 槽）。
            # 本文件只导入了 urllib.parse（没有裸 parse_qs）—— 用全名，别再造一次 NameError
            q2 = urllib.parse.parse_qs(query or "")
            rng = (q2.get("range", ["week"]) or ["week"])[0]
            since = (q2.get("since", [None]) or [None])[0]
            until = (q2.get("until", [None]) or [None])[0]
            bucket = (q2.get("bucket", [None]) or [None])[0]
            return self._json(200, aggregator.hub_timeseries(ports, realm=realm, range_=rng, since=since, until=until, bucket=bucket),
                              head_only=head_only)
        if rest == "models":
            models = aggregator.hub_models(ports)
            by_engine = {}
            for item in models:
                key = item.get("engine") or "?"
                by_engine[key] = by_engine.get(key, 0) + 1
            return self._json(200, {"object": "list", "data": models,
                                    "counts": by_engine, "total": len(models)},
                              head_only=head_only)
        if rest == "frame-check":
            # 授权页能否被面板内嵌？跨域 iframe 只有对方允许才画得出来
            # （X-Frame-Options / CSP frame-ancestors）。这里用 HEAD 先问一次，
            # 免得面板里先给用户看一块空白。
            target = (params.get("url") or [""])[0]
            return self._json(200, _frame_check(target), head_only=False)
        if rest == "rewards":
            # 福利 / 签到：两边 /tasks 的原始体 + 归一字段；引擎不可达不抛，走 errors
            return self._json(200, aggregator.hub_rewards(ports), head_only=head_only)
        # ---- 合并态三档语义（决策书 §十二）需要的三条 ----
        if rest == "accounts":
            # 两引擎账号**【并集】**，每条带 engine 标 —— 不标来源的合并会让用户
            # 看到「12 个账号」却不知道是 8+4 还是别的组合（§12.3 第 1 条）。
            return self._json(200, aggregator.hub_accounts(ports), head_only=head_only)
        if rest == "scheduler":
            # 调度器天然每引擎一个，**不合并成一份**，只并排给（§12.2）。
            return self._json(200, aggregator.hub_scheduler(ports), head_only=head_only)
        if rest == "logs":
            try:
                limit = int((params.get("limit") or ["100"])[0])
            except (TypeError, ValueError):
                limit = 100
            return self._json(200, aggregator.hub_logs(ports, limit=limit),
                              head_only=head_only)
        if rest == "route-stats":
            return self._json(200, self._route_stats(), head_only=head_only)
        if rest == "route-log":
            # 第 155 轮：路由决策的**逐条**记录（route-stats 是聚合）。
            # 面板据此显示"这次请求试了几家、最后落在哪"—— 回退不再是看不见的事。
            try:
                limit = int((params.get("limit") or ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            return self._json(200, self._route_log(limit), head_only=head_only)
        if rest == "realdata":
            # 最近一次 hub/tools/verify_realdata.py 的结果（三个数字：合并 / wb / qd 各看到多少模型）
            return self._json(200, self._realdata(), head_only=head_only)
        if rest == "keys/reveal":
            # 取回合并 Key 的**明文**：给面板的「复制」用。
            # 与 settings/save 同一套授权 —— 必须带面板口令（X-Panel-Token 里放口令本体），
            # 引擎令牌不够：那是引擎的，不是融合层的。
            supplied = (self.headers.get("X-Panel-Token") or "").strip()
            if not settings.verify_panel_password(supplied):
                return self._json(401, {"error": "panel password required"})
            wanted = (params.get("id") or [""])[0]
            for entry in settings.merged_keys(masked=False):
                if str(entry.get("id") or "") == wanted:
                    return self._json(200, {"id": wanted, "key": entry.get("key") or ""})
            return self._json(404, {"error": "no such key"})
        if rest == "update":
            force = "refresh=1" in (query or "") or "force=1" in (query or "")
            return self._json(200, updater.check(force=force), head_only=head_only)
        if rest == "update/apply":
            result = updater.update_to_latest(force_check=True)
            if result.get("ok") and result.get("updated"):
                result["restart_hint"] = "restart the gateway to load the new code"
            return self._json(200 if result.get("ok") else 500, result)
        if rest == "update/restart":
            return self._json(200, self._spawn_restart())

        if rest == "by-model":
            q4 = urllib.parse.parse_qs(query or "")
            return self._json(200, aggregator.hub_by_model(
                ports, realm=realm,
                range_=(q4.get("range", [None]) or [None])[0],
                since=(q4.get("since", [None]) or [None])[0],
                until=(q4.get("until", [None]) or [None])[0]), head_only=head_only)
        if rest == "keys":
            return self._json(200, self._hub_keys(), head_only=head_only)
        if rest == "selfcheck":
            return self._json(200, self._selfcheck(), head_only=head_only)
        if rest == "try":
            if method != "POST":
                return self._json(405, {"error": "use POST /api/hub/try"}, head_only=head_only)
            return self._json(200, self._handle_try(), head_only=head_only)
        if rest in ("settings", "settings/save"):
            # 合并层的自有配置（合并密钥掩码 + 路由偏好）。读是开放的（本机工具，
            # 掩码后无秘密），写要面板口令 —— 见 POST /api/hub/settings/save。
            if method == "POST" or rest == "settings/save":
                return self._handle_hub_settings_save()
            return self._json(200, {
                "routing": settings.routing(),
                "merged_keys": settings.merged_keys(masked=True),
                "panel_password_default": settings.panel_password_default(),
            }, head_only=head_only)
        return self._json(404, {
            "error": "no such hub endpoint: /api/hub/%s" % rest,
            "available": ["/api/hub/health", "/api/hub/engines",
                          "/api/hub/usage", "/api/hub/models", "/api/hub/rewards",
                          "/api/hub/accounts", "/api/hub/scheduler", "/api/hub/logs",
        "/api/hub/timeseries", "/api/hub/account-health",
                          "/api/hub/route-stats", "/api/hub/route-log",
                          "/api/hub/keys", "/api/hub/by-model", "/api/hub/selfcheck", "/api/hub/try",
                          "/api/hub/settings"],
        })

    def _handle_try(self):
        """**就地试打**：拿指定类别的一把 key，真的往自己的 /api/v1 发一条最小请求。

        为什么值得有：自检只证明"配置齐了"，不证明"请求真的通"。这里走的就是用户
        平时走的那条路（同一个端口、同一套 key 路由），所以返回里的
        status / 路由落点 / 耗时 是**真实结果**，不是推断。

        代价说明：会真实消耗一点点额度（max_tokens=16，提示词固定为 ping）。
        """
        try:
            payload = json.loads((self._read_body() or b"{}").decode("utf-8", "replace") or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        kind = str(payload.get("kind") or "merged").strip().lower()
        model = str(payload.get("model") or "").strip()
        prompt = str(payload.get("prompt") or "ping").strip() or "ping"
        if not model:
            return {"ok": False, "error": "model required"}

        if kind == "merged":
            pool = [item.get("key") for item in keys.merged_keys() if item.get("key")]
        else:
            pool = [item.get("key") for item in keys.engine_keys(kind) if item.get("key")]
        if not pool:
            return {"ok": False, "error": "no enabled %s key registered" % kind}
        key = pool[0]

        port = self.server.server_address[1]
        url = "http://127.0.0.1:%d/api/v1/chat/completions" % int(port)
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        })
        started = time.time()
        status, raw, route, headers = None, "", None, {}
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                status = resp.status
                headers = dict(resp.headers or {})
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers or {})
            raw = exc.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "kind": kind, "model": model,
                    "ms": int((time.time() - started) * 1000),
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        ms = int((time.time() - started) * 1000)
        route = headers.get("X-Hub-Route") or headers.get("x-hub-route")
        content, usage = "", {}
        try:
            doc = json.loads(raw)
            choice = (doc.get("choices") or [{}])[0] or {}
            content = ((choice.get("message") or {}).get("content") or "")[:200]
            usage = doc.get("usage") or {}
        except Exception:
            content = raw[:200]
        return {"ok": 200 <= int(status or 0) < 300, "status": status, "ms": ms,
                "route": route, "kind": kind, "model": model,
                "content": content, "usage": usage,
                "error": "" if 200 <= int(status or 0) < 300 else raw[:240]}

    def _selfcheck(self):
        """**系统自检**：把「这套东西现在是不是一个能用的产品」拆成可判定的几条。

        全部是**本地**判定（引擎端口、配置、目录），不打外网 —— 所以它既能当面板按钮，
        也能被排障时随手调一下。逐条给 ok/detail，总体 ok = 全过。

        为什么值得有：融合层把"两个引擎 + 三类 key + 数据根"缝在一起，
        出问题时用户看到的往往只是"某个数字不对"。这里直接回答"哪一环没接上"。
        """
        ports = self.server.ports or {}
        checks = []

        def add(cid, label, ok, detail=""):
            checks.append({"id": cid, "label": label, "ok": bool(ok), "detail": detail})

        health = {}
        try:
            health = aggregator.engine_health(ports) or {}
        except Exception:
            health = {}
        for engine in ENGINES:
            info = health.get(engine)
            if ports.get(engine):
                ok = isinstance(info, dict) and info.get("ok") is True
                add("engine-" + engine, "「%s」引擎在线" % engine, ok,
                    "" if ok else ("未应答 /health" if info is None else str(info)[:120]))
            else:
                add("engine-" + engine, "「%s」引擎已配置" % engine, False, "没有为它分配端口")

        merged = keys.merged_keys()
        add("keys-merged", "登记了合并 key（一把调所有）", len(merged) >= 1,
            "共 %d 把" % len(merged))
        for engine in ENGINES:
            n = len(keys.engine_keys(engine))
            add("keys-" + engine, "「%s」有可用 API Key" % engine, n >= 1, "共 %d 把" % n)

        up_ok = {}
        for engine in ENGINES:
            k = keys.upstream_key(engine)
            up_ok[engine] = bool(k)
            add("upstream-" + engine, "合并模式能取到「%s」的凭据" % engine, bool(k),
                "" if k else "该引擎一把启用的 key 都没有")

        cfg = settings.routing() or {}
        add("routing-on", "智能路由已开启", cfg.get("enabled") is not False, "")
        prefer = str(cfg.get("prefer_realm") or "")
        add("routing-prefer", "首选区是 cn / intl 之一", prefer in ("cn", "intl"), "当前=%s" % (prefer or "未设"))

        try:
            root = paths.data_root()
            add("data-root", "数据根可解析", True,
                root or "未设（各引擎用自己目录内）")
            for engine in ENGINES:
                if not ports.get(engine):
                    continue
                folder = paths.accounts_dir(engine)
                add("data-" + engine, "「%s」账号目录存在" % engine, os.path.isdir(folder), folder)
        except Exception as exc:
            add("data-root", "数据根可解析", False, repr(exc)[:120])

        # 配置文件"存在但读不动"：这个状态下合并 key 会全空、所有调用 401，
        # 而界面上只会显示"key 不对"。必须在这里摊开（第 46 轮）。
        try:
            le = settings.load_error() or {}
        except Exception:
            le = {}
        add("settings-json", "融合层配置文件可解析", not le.get("error"),
            ("%s：%s" % (le.get("path") or "", le.get("error") or "")) if le.get("error") else "")

        try:
            perr = keys.parse_errors() or {}
        except Exception:
            perr = {}
        for engine in ENGINES:
            if not ports.get(engine):
                continue
            try:
                sp = keys.engine_settings_path(engine)
            except Exception:
                sp = ""
            msg = perr.get(sp) if sp else None
            add("engine-json-" + engine, "「%s」的 settings.json 可解析" % engine, not msg, msg or "")

        try:
            from hub import ENGINE_VERSIONS as _EV
        except Exception:
            try:
                from .. import ENGINE_VERSIONS as _EV
            except Exception:
                _EV = {}
        for engine in ENGINES:
            add("version-" + engine, "「%s」版本已登记" % engine, bool((_EV or {}).get(engine)),
                str((_EV or {}).get(engine) or ""))

        ok_all = all(c["ok"] for c in checks)
        return {"ok": ok_all, "version": HUB_VERSION, "checks": checks,
                "passed": sum(1 for c in checks if c["ok"]), "total": len(checks)}

    def _hub_keys(self):
        """统一 Key 总览：三类 key 各多少把、分别走哪条路（**只回掩码**）。

        这是「一个地址吃所有 key」的可视化入口：面板据此说明
        「你拿哪把 key 来，就走哪条路」，不用再翻两个引擎各自的面板。
        """
        def mask(value):
            value = str(value or "")
            return (value[:6] + "…" + value[-4:]) if len(value) > 12 else "…"

        merged = [{"id": e.get("id"), "name": e.get("name"),
                   "key": mask(e.get("key")), "enabled": e.get("enabled", True)}
                  for e in keys.merged_keys()]
        out = {"ok": True, "counts": keys.summary(), "merged": merged,
               # 第 194 轮修：rules 也写死了 wb/qd —— 加一家就少一条说明。
               # 现在按注册表生成，新引擎只需在 ENGINE_META 填一行。
               "rules": dict([("merged", "全部模型 · 智能路由（国内优先 → 跨引擎回退）")]
                             + [(e, "只走 %s（模型清单也只列 %s）" % (ENGINE_LABELS.get(e, e), e))
                                for e in ENGINES])}
        for engine in ENGINES:
            out[engine] = [{"id": e.get("id"), "name": e.get("name"),
                            "key": mask(e.get("key")), "realm": e.get("realm") or "",
                            "models": len(e.get("models") or [])}
                           for e in keys.engine_keys(engine)]
        return out

    def _realdata(self):
        """「本机实测」三个数字：合并 key 看到多少、wb 多少、qd 多少。

        **优先现算**（用网关自己的路由快照，与 /v1/models 同一套口径）：
          · merged = merged_model_list(snapshot) 的条数（同名合一条）
          · wb/qd  = 该引擎在**所有区域**下的模型 id 合计
        这与 hub/tools/verify_realdata.py 量的是同一件事（后者走一遍真实 HTTP），
        区别只是这里不额外起实例 —— 面板上点一下就能拿到，不用去终端。

        同时把工具上次跑出来的报告（logs/realdata-report.json）一起带上：
        它包含"什么时候用真实数据副本实测过"，给数字一个可信的时间戳。
        """
        out = {"available": False}
        try:
            base = os.path.dirname(self.server.admin_dir) if self.server.admin_dir else os.getcwd()
            path_ = os.path.join(base, "logs", "realdata-report.json")
            if os.path.isfile(path_):
                with open(path_, encoding="utf-8", errors="replace") as fh:
                    doc = json.load(fh)
                if isinstance(doc, dict):
                    out = dict(doc)
                    out["available"] = True
                    out["report"] = True
        except Exception:
            out = {"available": False}

        try:
            upstream = {e: (keys.upstream_key(e) or "") for e in ENGINES}
            snapshot = router.refresh_snapshot(self.server.ports, upstream,
                                               self.server.route_snapshot)
            counts = {"merged": len(router.merged_model_list(snapshot))}
            for engine in ENGINES:
                realms = (snapshot.models or {}).get(engine) or {}
                counts[engine] = sum(len(ids) for ids in realms.values())
            out["live"] = counts
            out["available"] = True
            out["source"] = "live"
            out["ok"] = counts["merged"] >= max([counts.get(e, 0) for e in ENGINES] or [0])
        except Exception as exc:  # noqa: BLE001 —— 现算失败不影响返回旧报告
            out.setdefault("liveError", "%s: %s" % (type(exc).__name__, exc))
        return out

    def _route_log_path(self):
        """路由日志的位置（第 224 轮）。

        原来写死 <repo>/logs/route.jsonl —— 于是：
          · 两个实例共用一个文件，互相污染；
          · 设了 HUB_DATA_ROOT 的"干净实例"仍会读到仓库里的历史流量，
            面板一路显示旧数字（实测空数据根下总览仍报 106/159）。
        现在优先跟随数据根；没设才退回仓库里的 logs/（保持单实例默认行为）。
        """
        try:
            root = paths.data_root()
        except Exception:
            root = None
        if root:
            return os.path.join(root, "logs", "route.jsonl")
        base = os.path.dirname(self.server.admin_dir) if self.server.admin_dir else os.getcwd()
        return os.path.join(base, "logs", "route.jsonl")

    def _route_log(self, limit=50):
        """读路由日志的尾部 N 条（最新的排前面）。读不到给空表 + 原因。"""
        route_log = self._route_log_path()
        rows = []
        try:
            with open(route_log, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except OSError as exc:
            return {"rows": [], "total": 0, "path": route_log, "error": str(exc)}
        tail = rows[-max(1, int(limit)):]
        tail.reverse()
        return {"rows": tail, "total": len(rows), "path": route_log, "error": None}
    def _route_stats(self):
        """读 logs/route.jsonl 聚合路由统计。

        面板回答：今天/累计多少请求走了 wb vs qd、cn vs intl、多少次发生回退
        （attempts > 1 = 至少换过一次引擎/区域才成功）。
        文件不存在或读不到 → 空数据（面板显示「暂无」而不是报错）。
        """
        try:
            route_log = self._route_log_path()
            if not os.path.isfile(route_log):
                return {"total": 0, "today": {}}
            today_key = time.strftime("%Y-%m-%d")
            rows = []
            with open(route_log, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except (ValueError, TypeError):
                        continue
        except Exception:
            return {"total": 0, "today": {}}

        def agg(items):
            if not items:
                return {"requests": 0, "fallbacks": 0, "by_engine": {},
                        "by_realm": {}, "by_route": {}, "errors": 0}
            out = {"requests": len(items), "fallbacks": 0,
                   "by_engine": {}, "by_realm": {}, "by_route": {}, "errors": 0}
            for r in items:
                eng = str(r.get("engine") or "?")
                realm = str(r.get("realm") or "auto")
                route = eng + ":" + realm
                out["by_engine"][eng] = out["by_engine"].get(eng, 0) + 1
                out["by_realm"][realm] = out["by_realm"].get(realm, 0) + 1
                out["by_route"][route] = out["by_route"].get(route, 0) + 1
                if (r.get("attempts") or 1) > 1:
                    out["fallbacks"] += 1
                if (r.get("status") or 0) >= 400:
                    out["errors"] += 1
            return out

        today_rows = [r for r in rows if str(r.get("iso") or "")[:10] == today_key]
        return {
            "total": len(rows),
            "today": agg(today_rows),
            "all_time": agg(rows),
        }

    def _handle_hub_settings_save(self):
        """POST /api/hub/settings/save —— 合并密钥 + 路由偏好 + hub 面板口令。

        载荷：
          routing           部分/整体路由偏好（enabled / prefer_realm / engine_prefer）
          merged_keys       整体替换合并密钥列表（掩码条目保留旧值）
          panel_password    {current, new} 修改 hub 面板口令
        """
        token = (self.headers.get("X-Panel-Token") or "").strip()
        if not settings.verify_panel_password(token):
            return self._json(401, {"error": "hub panel password required",
                                    "hint": "X-Panel-Token: admin（默认口令）"})
        payload = self._read_body()
        try:
            doc = json.loads(payload.decode("utf-8", "replace")) if payload else {}
        except Exception:
            return self._json(400, {"error": "invalid JSON body"})
        if not isinstance(doc, dict):
            return self._json(400, {"error": "body must be an object"})
        reply = {}
        try:
            if "routing" in doc:
                reply["routing"] = settings.set_routing(doc.get("routing"))
            if "merged_keys" in doc:
                reply["merged_keys_saved"] = len(
                    settings.set_merged_keys(doc.get("merged_keys")))
            if "panel_password" in doc:
                pw = doc.get("panel_password") or {}
                current = str(pw.get("current") or "")
                new = str(pw.get("new") or "")
                ok, message = settings.set_panel_password(current, new)
                if not ok:
                    return self._json(400, {"error": message})
                reply["panel_password"] = "changed"
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        reply["ok"] = True
        return self._json(200, reply)

    # ---- 2) 反代
    def _handle_api(self, path, query, method):
        rest = path[len(API_PREFIX):].lstrip("/")
        params = urllib.parse.parse_qs(query or "", keep_blank_values=True)

        engine = None
        # ★ 引擎前缀由注册表驱动（第 100 轮）：/api/trae/... 现在和 /api/wb/... 一样。
        #   原来这里只写了 wb/qd 两条 elif，加 Trae 后它的所有调试路由都 404。
        _prefixed = None
        for _name in ENGINES:
            if rest == _name or rest.startswith(_name + "/"):
                _prefixed = _name
                break
        if _prefixed:
            engine, rest = _prefixed, rest[len(_prefixed):].lstrip("/")
        else:
            # 无引擎前缀：先看合并模式（合并 key + 智能路由），否则回默认引擎，
            # ?engine= 可覆盖（这是「现有 admin 一行不改」的关键）。
            raw = (params.pop("engine", [None]) or [None])[0]
            explicit = resolve_engine(raw)
            if explicit is None:
                merged = self._merged_request(method, rest, params, query)
                if merged is not None or getattr(self, "_responded", False):
                    return merged
                # ★ 统一 key 层：引擎自己的 key 也用**同一个地址** ——
                #   按 key 的归属把请求送进对应引擎，不必写 /api/wb、/api/qd 前缀。
                #   （合并 key 在上面那条早退里已经走智能路由了。）
                kind, _entry = keys.classify(self._presented_key())
                if kind in ENGINES:
                    engine = kind
                elif kind is None and self._reject_unknown_key(rest):
                    # 只拦「三种 key 都不认识」这一种。合并 key 在**关闭智能路由**
                    # 时也要能落默认引擎（F05b 就是这条），否则关掉路由就把自己锁死。
                    return self._unknown_key_response()
            engine = engine or explicit or self.server.default_engine

        if not rest:
            return self._json(400, {
                "error": "missing engine path, e.g. /api/%s/health" % engine,
                "engines": list(self.server.ports.keys()),
            })

        # 用剥掉 engine 之后的查询串转发（engine 是 portal 的控制参数，不该漏给引擎）
        forwarded = urllib.parse.urlencode(
            [(k, v) for k, values in params.items() for v in values])
        return self._proxy(engine, "/" + rest, forwarded, method)

    def _presented_key(self):
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    #: 需要鉴权的推理路径（剥掉 / 后比较）。面板接口不在此列。
    INFERENCE_PATHS = frozenset(router.CHAT_PATHS) | frozenset(("v1/models", "models"))

    def _reject_unknown_key(self, rest):
        """这条推理请求带了 key，但三种 key 都不认 → 由 hub 明确拒绝。

        为什么不在 hub 拦所有请求：面板接口走 X-Panel-Token（不是 api key），
        无 key 请求也应由引擎按自己的开关决定要不要放行。只拦「明显在调模型、
        又拿了一把谁都对不上的 key」这一种 —— 那是最容易让人以为是「坏了」的情况。
        """
        if not self._presented_key():
            return False
        return rest.lower().strip("/") in self.INFERENCE_PATHS

    def _unknown_key_response(self):
        counts = keys.summary()
        presented = self._presented_key() or ""
        # 最常见的两种「明明有 key 却说无效」：复制到了面板上的掩码、或复制时带了空白。
        # 真 Key 一定是 ASCII；出现非 ASCII 基本就是复制到了面板上的 "mrg-me…-001"。
        # （HTTP 头按 latin-1 解码，所以这里不能直接比对 U+2026 那个省略号。）
        try:
            _ascii_only = presented.isascii()
        except Exception:
            _ascii_only = True
        if not _ascii_only:
            message = ("这不是一把真实的 Key（含非 ASCII 字符，通常是从面板复制到了掩码 mrg-me…-001）。"
                       "请在「设置 → 统一 Key」里点行尾的复制图标取回明文。")
        elif "..." in presented:
            message = ("这是面板上的掩码（形如 mrg-me...-001），不是真实 Key。"
                       "请在「设置 → 统一 Key」里点行尾的复制图标取回明文。")
        elif presented != presented.strip():
            message = "Key 前后带了空格/换行，请重新复制（网关已按去空格比对，仍不匹配说明不是这把）。"
        else:
            message = "unknown api key for this gateway"
        return self._json(401, {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "hint": ("三种 key 都能用同一个地址：合并 key → 全部模型（智能路由）；"
                         "wb key → 只走 WorkBuddy；qd key → 只走 Qoder。"
                         "在面板「设置 → 统一 Key」里查看与新增。"),
                "known_keys": counts,
            }
        })

    def _merged_request(self, method, rest, params, query):
        """合并模式判定：路由开 + 合并 key + 聊天/模型路径 → 智能路由。

        返回 None = 不进入合并模式（走既有默认引擎路径）。
        其余返回值是已经**发送完成**的响应（_handle_api 直接 return）。
        """
        cfg = settings.routing()
        if not cfg.get("enabled"):
            return None
        key = self._presented_key()
        entry = settings.match_merged_key(key) if key else None
        if not entry:
            return None
        lower = rest.lower().strip("/")
        chat = lower in router.CHAT_PATHS
        if not chat and lower not in ("v1/models", "models"):
            # 非聊天端点：合并 key 也只走默认引擎（各引擎自校验）。
            return None
        return self._handle_merged(method, rest, params, cfg, key, entry)

    def _handle_merged(self, method, rest, params, cfg, key, entry=None):
        """合并智能路由：候选 (engine, realm) 逐个尝试，未出流前可换。

        候选由 router.candidates_for() 给出（区域优先 + 引擎就绪排序）；每个
        引擎拿到 hub 权威拼的 ?realm=（客户端 realm 参数与 X-Realm 头都被剥掉，
        避免出现 realm=intl&realm=cn 这种重复参数）。

        ★ 双层回退是**有意**的：hub 钉一个区给引擎，引擎自己那层 auto-realm 仍
          可能在本区内耗尽时再退到另一区并成功服务（例如 hub 钉 intl、wb 无 intl
          账号但 cn 有 → wb 内部退 cn 出 200）。这比"引擎硬钉 intl 直接 503"对用户
          更友好（请求照样成功）。代价是 hub 请求的区 ≠ 引擎实际服务的区 ——
          所以 X-Hub-Route 以**引擎回包里的 X-WB-Realm/X-Qoder-Realm 为准**（真实
          服务区），引擎没报才回落到 hub 请求的区。

        401/429/502/503/504 且还有候选时换下一个，尝试链写进 X-Hub-Route-Attempts。
        """
        body = self._read_body() if method in ("POST", "PUT", "PATCH") else b""
        model = router.extract_model(body)
        # 同一份请求 5 秒内来两次 → 记一条带指纹的日志（客户端重发 vs 网关重试，
        # 这条日志是唯一可靠的判据）。
        _fp = _request_fingerprint(body)
        _dup, _gap = _note_duplicate(_fp)
        if _dup:
            sys.stderr.write(
                "[portal] duplicate request fp=%s model=%s gap=%.1fs (client sent twice?)\n"
                % (_fp, model, _gap))
            sys.stderr.flush()

        # ★ 合并 Key 的作用域（对齐 Agent2API 的 per-key 限制思路）：
        #   entry["models"]  / entry["engines"] 为空 = 不限制（旧行为）。
        #   为什么要它：所有调用方共用一把 key，一旦泄露就是全量损失；
        #   加上作用域后每把 key 只能碰它该碰的那部分。
        scope_models = [str(m).strip().lower() for m in (entry or {}).get("models") or [] if str(m).strip()]
        scope_engines = [str(e).strip().lower() for e in (entry or {}).get("engines") or [] if str(e).strip()]
        if scope_models and model:
            if str(model).strip().lower() not in scope_models:
                return self._json(403, {
                    "error": {
                        "message": "this merged key is not allowed to use model %r" % model,
                        "type": "invalid_request_error",
                        "code": "model_not_allowed",
                        "allowed_models": scope_models,
                        "hint": "在面板「统一 Key」里去掉/调整这把 key 的模型限制。",
                    }
                })
        # ★ 统一 key 层：合并 key 不必在各引擎里登记 —— 抓清单与发请求都用
        #   该引擎自己的一把启用 key（keys.upstream_key）；引擎没 key 才退回客户端那把。
        upstream = {e: (keys.upstream_key(e) or key) for e in ENGINES}
        snapshot = router.refresh_snapshot(self.server.ports, upstream,
                                           self.server.route_snapshot)
        if lower_path(rest) in ("v1/models", "models"):
            data = router.merged_model_list(snapshot)
            counts = {}
            for engine, realms in (snapshot.models or {}).items():
                counts[engine] = sum(len(ids) for ids in (realms or {}).values())
            return self._json(200, {"object": "list", "data": data,
                                    "merged": True, "counts": counts,
                                    "route": cfg})
        # 客户端显式钉的区（?realm= 或 X-Realm 头）：尊重它 —— 作为本次首选区，
        # 但另一区仍留在链尾兜底（「显式优先，不把路走死」）。
        requested = (params.get("realm") or [None])[0] or self.headers.get("X-Realm")
        candidates = router.candidates_for(model, snapshot, cfg, requested)
        if scope_engines:
            _all = candidates
            candidates = [c for c in candidates if str(c[0]).lower() in scope_engines]
            if not candidates:
                return self._json(503, {
                    "error": {
                        "message": "this merged key is limited to engines %s, none of them can serve %r"
                                   % (scope_engines, model),
                        "type": "server_error",
                        "code": "engine_not_allowed",
                        "allowed_engines": scope_engines,
                    }
                })
        if not candidates:
            # 快照里没有这个模型（新模型/清单未取全）：退回默认引擎、区域让引擎
            # 自己定（引擎内的自动路由仍会兜底），至少别比旧行为差。
            candidates = [(self.server.default_engine, None)]
        # ★ realm 由 hub **权威覆盖**（每个候选带自己的 ?realm=）：
        #   ① 客户端的 realm 参数必须剥掉 —— 否则拼出 realm=intl&realm=cn，
        #      引擎取第一个值，hub 的 X-Hub-Route 头就和实际行为对不上（假合并）；
        #   ② X-Realm 头同理要剥（wb 的 _request_realm 里头优先于 query）。
        forwarded = urllib.parse.urlencode(
            [(k, v) for k, values in params.items() if k != "realm" for v in values])
        attempts = []
        last = None
        last_engine = None
        # ★ 流式：合并 key 也要边收边发。先拿**第一跳**直接透传（遇到可重试
        #   状态就撤回，交给下面的重试循环），这样长回答不会整段憋到最后。
        if method == "POST" and b'"stream"' in body and b'true' in body and candidates:
            _eng0, _realm0 = candidates[0]
            _q0 = forwarded
            if _realm0:
                _q0 = (_q0 + "&" if _q0 else "") + "realm=" + _realm0
            if self._proxy_stream(_eng0, "/" + rest, _q0, method, body,
                                  upstream.get(_eng0), accept_retryable=False):
                # 响应已经在 _proxy_stream 里发完了，这里只结束处理
                return None
        # ★ 已经给客户端写过字节（流式半途失败）时**不能再重发**：
        #   否则客户端会收到「第一跳的部分流 + 第二跳的完整流」——
        #   表现就是同一个回答来两遍、工具调用被执行两次。
        if getattr(self, "_stream_committed", False):
            sys.stderr.write("[portal] merged: stream already committed, skipping retry\n")
            sys.stderr.flush()
            return None
        for engine, realm in candidates:
            query_extra = forwarded
            if realm:
                query_extra = (query_extra + "&" if query_extra else "") + "realm=" + realm
            status, payload, pairs = self._proxy_once(
                engine, "/" + rest, query_extra, method, body=body,
                strip_headers={"x-realm"}, upstream_key=upstream.get(engine))
            attempts.append({"engine": engine, "realm": realm or "",
                             "status": status})
            last = (status, payload, pairs)
            last_engine = engine
            if status not in router.RETRYABLE_STATUS:
                break
        # 真实服务区：引擎回包里的 X-WB-Realm / X-Qoder-Realm 优先（它可能因引擎
        # 自身的 auto-realm 回退而与 hub 请求的区不同）；引擎没报就用 hub 请求的区。
        served_realm = ""
        for name, value in (last[2] or []):
            if name.lower() in ("x-wb-realm", "x-qoder-realm") and value:
                served_realm = value
                break
        reported = served_realm or attempts[-1]["realm"] or "auto"
        self._record_route(last_engine, reported, attempts, last[0], model)
        extra = [("X-Hub-Route", "%s:%s" % (last_engine, reported)),
                 ("X-Hub-Route-Attempts", json.dumps(attempts, ensure_ascii=False))]
        return self._relay(last[0], last[1], last[2], last_engine,
                           extra_headers=extra)

    def _proxy_once(self, engine, upstream_path, query, method, body=None,
                    strip_headers=None, upstream_key=None):
        """一次引擎往返：返回 (status, payload:bytes, pairs:list)。不发送、不抛。

        strip_headers：额外要剥掉的请求头（小写集合）—— 合并路由用它剥 X-Realm，
        让 hub 拼的 ?realm= 成为**唯一**的区域来源。
        """
        port = self.server.ports.get(engine)
        if port is None:
            return 503, json.dumps({
                "error": "engine %r is not configured on this portal" % engine,
                "configured": {k: v for k, v in self.server.ports.items()},
            }, ensure_ascii=False).encode("utf-8"), []
        target = upstream_path + (("?" + query) if query else "")
        headers = {}
        for name, value in self.headers.items():
            if name.lower() in DROP_REQUEST_HEADERS:
                continue
            if strip_headers and name.lower() in strip_headers:
                continue
            headers[name] = value
        if upstream_key:
            # 合并模式：用**该引擎自己的一把 key** 换掉客户端那把（客户端用的是
            # 合并 key，引擎不认识它）。x-api-key 一起换，免得引擎优先读它。
            headers["Authorization"] = "Bearer " + str(upstream_key)
            headers["x-api-key"] = str(upstream_key)
        headers.setdefault("X-Forwarded-For", self.client_address[0])
        headers.setdefault("X-Forwarded-Host", self.headers.get("Host") or "")
        headers.setdefault("X-Forwarded-Proto", "http")
        headers.setdefault("X-Hub-Engine", engine)
        timeout = getattr(self.server, "proxy_timeout", 120)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(method, target, body=body if body else None, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, payload, resp.getheaders()
        except socket.timeout:
            return 504, json.dumps({
                "error": "engine %s timed out after %ds" % (engine, timeout),
                "kind": "upstream_timeout", "path": target,
            }, ensure_ascii=False).encode("utf-8"), []
        except (ConnectionRefusedError, socket.gaierror, OSError) as exc:
            return 502, json.dumps({
                "error": "cannot reach engine %s on 127.0.0.1:%d - %s"
                         % (engine, port, exc),
                "kind": "engine_unreachable",
                "hint": "先起引擎：python -m hub.server --wb-port 8801 --qd-port 8802",
            }, ensure_ascii=False).encode("utf-8"), []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _record_route(self, engine, realm, attempts, status, model):
        """把一次合并路由决策追加到 logs/route.jsonl —— 一行一条。

        面板回答「今天多少走 wb/qd、cn/intl、多少次回退」就靠它。
        写盘失败静默（可观测是加速件，不能反过来拖垮请求路径）。
        """
        try:
            route_log = self._route_log_path()
            os.makedirs(os.path.dirname(route_log), exist_ok=True)
            row = {"at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "engine": engine, "realm": realm,
                   "attempts": len(attempts), "status": status,
                   # 第 156 轮：把**完整尝试链**落盘（哪几家、各是什么状态）。
                   # 原来只存了个数 —— 界面上只能说"回退了 6 次"，说不出"试过谁、
                   # 各自挂在哪"，排障时还得去翻引擎自己的日志。
                   "chain": [{"engine": a.get("engine"), "realm": a.get("realm") or "",
                              "status": a.get("status")} for a in (attempts or [])],
                   "model": str(model or "")[:60]}
            with open(route_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _relay(self, status, payload, pairs, engine, extra_headers=None):
        """把引擎回包原样透传（逐条下发，重复头不合并）。"""
        relay = [(name, value) for name, value in pairs
                 if name.lower() not in DROP_RESPONSE_HEADERS]
        relay.append(("X-Hub-Engine", engine))
        for name, value in (extra_headers or []):
            relay.append((name, value))
        ctype = None
        for name, value in pairs:
            if name.lower() == "content-type":
                ctype = value
                break
        self._send(status, payload, ctype or MIME_TYPES[".json"], extra_headers=relay)

    def _proxy(self, engine, upstream_path, query, method):
        body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""
        # 合并 key 只在门户里有效；引擎认的是它自己的 key。转发前换掉，
        # 这样「一把合并 key 调所有」不必再在两个引擎里各登记一遍。
        kind, _entry = keys.classify(self._presented_key())
        upstream = keys.upstream_key(engine) if kind == "merged" else None
        # ★ 流式透传：客户端要 stream 时不要"收完整包再由门户转发" ——
        #   那样一次 28s 的回答在整个生成期间一个字都不下发，客户端很容易
        #   判成连接错误（实测出现过 TRANSPORT）。这里边收边发。
        if method == "POST" and b'"stream"' in body and b'true' in body:
            if self._proxy_stream(engine, upstream_path, query, method, body, upstream):
                return
        status, payload, pairs = self._proxy_once(
            engine, upstream_path, query, method, body=body, upstream_key=upstream)
        self._relay(status, payload, pairs, engine)

    def _proxy_stream(self, engine, upstream_path, query, method, body, upstream_key,
                      accept_retryable=True):
        """边收边发（SSE）。返回 True 表示已经完整应答，调用方不要再写。

        只在「上游真的给了 text/event-stream」时才走这条；否则返回 False，
        回落到原来的一次性转发（保证非流式请求的行为不变）。
        """
        port = self.server.ports.get(engine)
        if port is None:
            return False
        target = upstream_path + (("?" + query) if query else "")
        headers = {}
        for name, value in self.headers.items():
            if name.lower() in DROP_REQUEST_HEADERS:
                continue
            headers[name] = value
        if upstream_key:
            headers["Authorization"] = "Bearer " + str(upstream_key)
            headers["x-api-key"] = str(upstream_key)
        headers["Accept-Encoding"] = "identity"
        timeout = getattr(self.server, "proxy_timeout", 120)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(method, target, body=body or b"", headers=headers)
            resp = conn.getresponse()
            # 合并且要跨引擎重试时：可重试的错误不能在这条连接上就发出去，
            # 否则客户端只会看到第一跳的 429/503，重试逻辑等于被废掉。
            if not accept_retryable and resp.status in router.RETRYABLE_STATUS:
                return False
            ctype = str(resp.getheader("Content-Type") or "")
            if "text/event-stream" not in ctype.lower():
                payload = resp.read()
                pairs = resp.getheaders()
                self._relay(resp.status, payload, pairs, engine)
                self._stream_committed = True
                return True
            self.send_response(resp.status)
            for name, value in resp.getheaders():
                if name.lower() in DROP_RESPONSE_HEADERS:
                    continue
                self.send_header(name, value)
            self.send_header("X-Hub-Engine", engine)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            # 一旦给客户端写出过字节，就不能再重发整个请求（否则客户端收到两遍）
            self._stream_committed = True
            _stream_usage = {"in": 0, "out": 0, "total": 0, "seen": False}
            _tail = b""
            while True:
                chunk = resp.read(2048)
                if not chunk:
                    break
                self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
                self.wfile.flush()
                # 顺手解析 SSE 里的 usage：引擎日志漏记时，这里还有一份
                _tail = (_tail + chunk)[-8192:]
                if b'"usage"' in _tail:
                    try:
                        _m = re.search(rb'"prompt_tokens"\s*:\s*(\d+)', _tail)
                        _c = re.search(rb'"completion_tokens"\s*:\s*(\d+)', _tail)
                        _t = re.search(rb'"total_tokens"\s*:\s*(\d+)', _tail)
                        if _t:
                            _stream_usage["in"] = int(_m.group(1)) if _m else 0
                            _stream_usage["out"] = int(_c.group(1)) if _c else 0
                            _stream_usage["total"] = int(_t.group(1))
                            _stream_usage["seen"] = True
                    except Exception:
                        pass
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            if _stream_usage["seen"]:
                sys.stderr.write("[portal] stream done engine=%s tokens=%d (in=%d out=%d)\n"
                                 % (engine, _stream_usage["total"],
                                    _stream_usage["in"], _stream_usage["out"]))
            else:
                sys.stderr.write("[portal] stream done engine=%s tokens=? (no usage chunk: aborted or upstream omitted)\n" % engine)
            sys.stderr.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return True  # 客户端先走了：没什么可写的了
        except Exception as exc:
            if getattr(self, "_stream_committed", False):
                # 已经给客户端发过字节：必须把 chunked 流收尾，
                # 否则客户端会一直等最后那个 0 长度块（表现为请求卡住）。
                try:
                    err = json.dumps({"error": {
                        "message": "upstream stream aborted: %s" % exc,
                        "type": "server_error"}}, ensure_ascii=False)
                    payload = ("data: %s\n\n" % err).encode("utf-8")
                    self.wfile.write(("%x\r\n" % len(payload)).encode() + payload + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return True
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- 3) 静态
    def _handle_static(self, path, head_only=False):
        """**任何走到这里的 GET/HEAD 都当静态**（/api/* 已在 _dispatch 里被前面两条截走）。

        为什么不做前缀白名单：页面从 / 端出去时，index.html 引用的是
        ./css/app.css、./js/app.js 这类**相对**路径，浏览器把它解析成
        /css/app.css、/js/app.js —— 只认 /admin/* 的写法会让页面加载不出自己的
        资源（实测：GET / 200 但 /js/app.js 404，浏览器里就是个没样式没脚本的空壳）。
        /admin/ 前缀仍然接受（显式引用与书签），两条路径指向**同一个静态根**，
        安全性由 safe_join 的三道闸统一保证，不靠白名单。
        """
        rel = path or "/"
        if rel == "/admin":
            rel = "/"
        elif rel.startswith("/admin/"):
            rel = rel[len("/admin"):]
        target = safe_join(self.server.admin_dir, rel)
        if target is None:
            return self._send(403, "forbidden: path escapes admin root\n", MIME_TYPES[".txt"])
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return self._send(404, "404 not found: %s\n\n静态根: %s\n"
                              % (rel, self.server.admin_dir), MIME_TYPES[".txt"],
                              head_only=head_only)
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._send(500, "read failed: %s\n" % exc, MIME_TYPES[".txt"])
        # Vite 产物名里带内容哈希：可以放心长缓存；index.html 仍然每次校验。
        cache_cc = None
        if "/static/" in target.replace("\\", "/") or os.path.basename(target).startswith("index-") or "-" in os.path.basename(target):
            cache_cc = "public, max-age=31536000, immutable" if guess_mime(target) != MIME_TYPES.get(".html") else None
        self._send(200, body, guess_mime(target), head_only=head_only, cache_control=cache_cc)


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False          # 端口被占就报错，不偷偷接管别人的监听

    def __init__(self, addr, handler, ports, admin_dir, default_engine="wb",
                 proxy_timeout=120, quiet=False):
        self.ports = ports
        self.admin_dir = admin_dir
        self.default_engine = default_engine
        self.proxy_timeout = proxy_timeout
        self.quiet = quiet
        self.route_snapshot = router.RouteSnapshot()
        super().__init__(addr, handler)


# --------------------------------------------------------------------------- 入口
EPILOG = r"""示例：
  # 1) 起引擎（同进程双端口）
  python -m hub.server --wb-port 8801 --qd-port 8802
  # 2) 起融合面
  python -m hub.core.portal --port 8791 --wb-port 8801 --qd-port 8802

路由：
  GET  /  /admin/*          admin 静态站（no-store）
  GET  /api/hub/health      两引擎聚合健康（engine_health / hub_health）
  GET  /api/hub/engines     每个引擎的 id/port/health
  GET  /api/hub/usage       两边 /usage 合并（merged: requests/total_tokens/credit）
GET  /api/hub/timeseries  三家时序合并（labels + series{wb,qd,trae} + sum_tokens/sum_credit）
GET  /api/hub/account-health  按账号的最近失败（账号页打标用）
  GET  /api/hub/models      两边 /v1/models 并集，每条带 engine 标
  GET  /api/hub/rewards     两边的福利/签到（/tasks 原样体 + 归一字段；不可达不抛）
  ANY  /api/wb/<path>       反代到 wb 引擎（X-Panel-Token/Cookie 原样透传）
  ANY  /api/qd/<path>       反代到 qd 引擎
  ANY  /api/<name>          默认引擎；可加 ?engine=qd 覆盖（现有 admin 兼容入口）

验收速查（PowerShell）：
  curl.exe -s http://127.0.0.1:8791/api/hub/health
  curl.exe -s http://127.0.0.1:8791/api/wb/health      # 响应含 realm，不含 service
  curl.exe -s http://127.0.0.1:8791/api/qd/health      # 响应含 service/version（用来区分）
  curl.exe -s http://127.0.0.1:8791/api/health         # 走默认引擎
"""


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m hub.core.portal",
        description="融合面：一个端口端出 admin 静态站 + 两引擎接口（聚合 + 反代）",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=HUB_PORT, help="监听端口，默认 %d" % HUB_PORT)
    # 引擎端口参数**按注册表生成**（第 100 轮）：加 Trae 时这里没有手写 --trae-port。
    # 之前是写死的 --wb-port/--qd-port，于是门户只认两个引擎 —— trae 的 /health 永远 None。
    for _name in ENGINES:
        ap.add_argument("--%s-port" % _name, type=int, default=DEFAULT_PORTS[_name],
                        help="%s 引擎端口，默认 %d" % (ENGINE_LABELS.get(_name, _name), DEFAULT_PORTS[_name]))
    ap.add_argument("--admin-dir", default=None, help="静态根，默认 <repo>/admin")
    ap.add_argument("--default-engine", default="wb",
                    help="无引擎前缀的 /api/<name> 落到哪个引擎（wb|qd），默认 wb")
    ap.add_argument("--timeout", type=int, default=120, help="反代读写超时秒数，默认 120")
    ap.add_argument("--quiet", action="store_true", help="不打印每次请求日志")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    default_engine = resolve_engine(args.default_engine)
    if not default_engine:
        sys.stderr.write("[portal] --default-engine 只能是 wb 或 qd，收到 %r\n" % args.default_engine)
        return 2
    admin_dir = os.path.abspath(args.admin_dir or DEFAULT_ADMIN_DIR)
    # 端口字典按注册表收集（第 100 轮）：漏掉某一家，它的 /health 就永远是 None。
    ports = {}
    for _name in ENGINES:
        _p = getattr(args, "%s_port" % _name, 0)
        if _p:
            ports[_name] = int(_p)

    try:
        server = PortalServer((args.host, args.port), PortalHandler, ports=ports,
                              admin_dir=admin_dir, default_engine=default_engine,
                              proxy_timeout=args.timeout, quiet=args.quiet)
    except OSError as exc:
        sys.stderr.write("\n[portal] 无法监听 %s:%d —— %s\n" % (args.host, args.port, exc))
        sys.stderr.write("        端口可能已被占用。查占用者：\n          %s\n\n"
                         % port_owner_hint(args.port))
        return 1

    host_shown = args.host if args.host != "0.0.0.0" else "0.0.0.0"
    print("")
    # 融合模式（hub/server.py 拉起）不再重复打横幅 —— 横幅只该出现一次。
    _embedded = os.environ.get("HUB_EMBEDDED", "").strip().lower() in ("1", "true", "yes")
    if not _embedded:
        print("  %s v%s · 融合面" % (HUB_NAME, HUB_VERSION))
        print("  " + "-" * 62)
        print("  唯一入口 : http://%s:%d/" % (host_shown, args.port))
        print("  API     : http://%s:%d/api/v1   (OpenAI 兼容 · 合并 Key 智能路由)" % (host_shown, args.port))
        if args.host == "0.0.0.0":
            print("            (局域网设备把 0.0.0.0 换成本机 IP)")
        print("  " + "-" * 62)
        print("  Ctrl+C 停止")
    print("")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[portal] 已停止")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    return 0


PortalHandler.server_version = "hub-portal/" + HUB_VERSION

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
