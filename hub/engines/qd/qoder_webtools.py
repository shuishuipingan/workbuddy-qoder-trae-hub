"""本地 web 工具：让网关真正执行 web_search / web_fetch。

为什么需要：Codex 等客户端会声明内置工具（type=web_search / web_fetch），
Qoder 上游只认 function 类型，这类声明此前被直接丢弃（#63）——客户端以为有
搜索能力，实际永远调不到。开启本模块后，网关把它们改写成自己的同名 function，
模型调用时由网关本地执行，再把结果回灌给模型继续生成。

安全边界（这是唯一会由**模型**驱动出网的地方）：
  · 只允许 http/https，禁止带凭据的 URL；
  · 目标域名解析后必须是公网地址（私网/环回/链路本地一律拒绝）；
  · 重定向逐跳复检，重定向到内网直接失败；
  · 响应体与正文都有字节上限，超时固定；
  · 引擎链轮换 + TTL 缓存 + single-flight + 冷却，避免被限流。
"""
import ipaddress
import http.client
import html
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

WEB_SEARCH_NAME = "web_search"
WEB_FETCH_NAME = "web_fetch"
SEARCH_DECL_TYPES = ("web_search", "web_search_preview",
                     "web_search_preview_2025_03_11")
FETCH_DECL_TYPES = ("web_fetch",)

MAX_RESULTS = 5
MAX_FETCH_CHARS = 20000
MAX_BODY_BYTES = 4 * 1024 * 1024
HTTP_TIMEOUT = 15
CACHE_TTL = 600.0
BLOCK_COOLDOWN = 600.0
ERROR_COOLDOWN = 60.0
MIN_INTERVAL = 0.5

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ENGINES = ("duckduckgo", "bing", "wikipedia")

_LOCK = threading.Lock()
_CACHE = {}
_INFLIGHT = {}
_ENGINE_STATE = {}     # engine -> {"until": ts, "blocked": bool}
_LAST_AT = [0.0]


def _log(msg, level="INFO"):
    try:
        print("[webtools] %s %s" % (level, msg), flush=True)
    except Exception:
        pass


def _throttle():
    """引擎之间保持最小间隔，避免连续请求被同一家封。"""
    with _LOCK:
        wait = MIN_INTERVAL - (time.time() - _LAST_AT[0])
        if wait > 0:
            time.sleep(min(wait, 3.0))
        _LAST_AT[0] = time.time()


def _cache_get(key):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        if hit:
            _CACHE.pop(key, None)
    return None


def _cache_put(key, value):
    with _LOCK:
        if len(_CACHE) > 256:
            _CACHE.clear()
        _CACHE[key] = (time.time(), value)


def _engine_ok(name):
    st = _ENGINE_STATE.get(name) or {}
    return time.time() >= (st.get("until") or 0)


def _engine_penalise(name, blocked=False):
    _ENGINE_STATE[name] = {"until": time.time() +
                           (BLOCK_COOLDOWN if blocked else ERROR_COOLDOWN),
                           "blocked": blocked}
    _log("%s engine cooling down for %ss"
         % (name, int(BLOCK_COOLDOWN if blocked else ERROR_COOLDOWN)),
         level="WARN")


# ---------------------------------------------------------------- SSRF 守卫
# issue #131-6：三条绕过路径（DNS 重绑定 / 代理模式跳过检查 / 非公网段分类不全）
# 一次性收口：
#   1) 用 ipaddress 判「全球单播」，不再手写字符串前缀；
#   2) 解析一次并把**解析结果钉死**到实际连接（不再 check 一次、connect 再解析一次）；
#   3) 不再信任环境代理：本地网页工具一律直连钉好的 IP（要代理请显式给
#      QD_WEB_TOOLS_PROXY，且同样受地址校验约束）。
_MAX_REDIRECTS = 5


def _ip_is_public(ip):
    """是否**全球可路由单播**地址（issue #131-6）。"""
    try:
        addr = ipaddress.ip_address(str(ip or "").strip().strip("[]"))
    except Exception:
        return False
    # is_global 已排除 私网/环回/链路本地/保留/基准测试网段/CGNAT（100.64/10）；
    # 组播与未指定地址在部分版本里 is_global 仍为 True，显式再挡一次。
    return bool(addr.is_global and not addr.is_multicast
                and not addr.is_unspecified and not addr.is_reserved)


def _resolve_public_ip(host):
    """解析主机名并返回一个可连接的全球单播 IP；任何一条记录非公网就整体拒绝。

    issue #131-6：**所有**解析结果都必须通过校验（不只是取第一条），否则
    「第一条公网、第二条内网」的多记录域名仍可被利用。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return None, "host does not resolve"
    ips = []
    for info in infos:
        addr = info[4][0]
        if not _ip_is_public(addr):
            return None, "host resolves to a non-public address"
        ips.append(addr)
    if not ips:
        return None, "host does not resolve"
    return ips[0], ""


def check_url(url):
    """返回 (ok, reason)。模型给的 URL 必须过这一关才会被请求。"""
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except Exception:
        return False, "invalid url"
    if parsed.scheme not in ("http", "https"):
        return False, "only http/https allowed"
    if parsed.username or parsed.password:
        return False, "credentials in url are not allowed"
    host = parsed.hostname or ""
    if not host:
        return False, "missing host"
    if host in ("localhost", "localhost.localdomain"):
        return False, "localhost is not allowed"
    try:
        literal_ip = str(ipaddress.ip_address(host))
    except Exception:
        literal_ip = ""
    if literal_ip:
        if not _ip_is_public(literal_ip):
            return False, "private/loopback address is not allowed"
        return True, ""
    # 域名：**无论是否配置代理**都要解析并校验（代理不再豁免检查，issue #131-6）
    _ip, reason = _resolve_public_ip(host)
    if not _ip:
        return False, reason
    return True, ""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连到**已校验**的 IP，但按原域名发 Host（issue #131-6：防 DNS 重绑定）。"""

    def __init__(self, host, port, pinned_ip, timeout=HTTP_TIMEOUT):
        http.client.HTTPConnection.__init__(self, host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """同上；TLS 的 SNI / 证书校验仍按原域名（用 server_hostname）。"""

    def __init__(self, host, port, pinned_ip, timeout=HTTP_TIMEOUT,
                 context=None):
        http.client.HTTPSConnection.__init__(self, host, port, timeout=timeout,
                                             context=context)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             self.timeout)
        self.sock = self._context.wrap_socket(self.sock,
                                              server_hostname=self.host)


class _PinnedResponse(object):
    """把 http.client 的响应包装成 urllib 那样可 with/read 的对象。"""

    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers
        self._body = body
        self.url = url

    def read(self, n=None):
        if n is None:
            return self._body
        return self._body[:n]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _pinned_open(url, data=None, headers=None, max_bytes=MAX_BODY_BYTES):
    """带钉死 IP 的 GET/POST；逐跳跟随重定向并对每一跳重新校验。

    返回 _PinnedResponse。任何一跳不合格都直接抛 ValueError（调用方转成错误）。
    """
    current = str(url or "").strip()
    for _hop in range(_MAX_REDIRECTS + 1):
        ok, reason = check_url(current)
        if not ok:
            raise ValueError("blocked: %s" % reason)
        parsed = urllib.parse.urlparse(current)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            literal_ip = str(ipaddress.ip_address(host))
        except Exception:
            literal_ip = ""
        pinned, why = (literal_ip, "") if literal_ip else _resolve_public_ip(host)
        if not pinned:
            raise ValueError("blocked: %s" % (why or "unresolved host"))
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        send = dict({"User-Agent": _UA,
                     "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                     "Host": parsed.netloc}, **(headers or {}))
        if parsed.scheme == "https":
            conn = _PinnedHTTPSConnection(host, port, pinned)
        else:
            conn = _PinnedHTTPConnection(host, port, pinned)
        try:
            conn.request("POST" if data else "GET", path,
                         body=data, headers=send)
            resp = conn.getresponse()
            body = resp.read(max_bytes)
            status = resp.status
            hdrs = resp.headers
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if status in (301, 302, 303, 307, 308):
            nxt = hdrs.get("Location") or ""
            if not nxt:
                raise ValueError("redirect without location")
            current = urllib.parse.urljoin(current, nxt)
            data = None if status in (301, 302, 303) else data
            continue
        return _PinnedResponse(status, hdrs, body, current)
    raise ValueError("too many redirects")


def _open(url, data=None, headers=None):
    """兼容旧调用点：钉死 IP + 逐跳校验的新实现（issue #131-6）。"""
    return _pinned_open(url, data=data, headers=headers)


# ---------------------------------------------------------------- 搜索引擎
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text):
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", str(text or "")))).strip()


def _search_duckduckgo(query, n):
    body = urllib.parse.urlencode({"q": query}).encode("utf-8")
    with _open("https://html.duckduckgo.com/html/", data=body) as resp:
        page = resp.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    out = []
    for m in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            page, re.S):
        href, title = m.group(1), _clean(m.group(2))
        if "duckduckgo.com/l/" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            href = (qs.get("uddg") or [href])[0]
        if not href.startswith("http"):
            continue
        out.append({"title": title, "url": href, "snippet": ""})
        if len(out) >= n:
            break
    snippets = [_clean(x) for x in re.findall(
        r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', page, re.S)]
    for i, snip in enumerate(snippets[:len(out)]):
        out[i]["snippet"] = snip
    return out


def _search_bing(query, n):
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
    with _open(url) as resp:
        page = resp.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    out = []
    for block in re.findall(r'<li class="b_algo"(.*?)</li>', page, re.S):
        m = re.search(r'<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href, title = m.group(1), _clean(m.group(2))
        if not href.startswith("http"):
            continue
        snip = ""
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        if sm:
            snip = _clean(sm.group(1))
        out.append({"title": title, "url": href, "snippet": snip})
        if len(out) >= n:
            break
    return out


def _search_wikipedia(query, n):
    qs = urllib.parse.urlencode({"action": "query", "list": "search",
                                 "format": "json", "srlimit": n,
                                 "srsearch": query})
    with _open("https://zh.wikipedia.org/w/api.php?" + qs) as resp:
        data = json.loads(resp.read(MAX_BODY_BYTES).decode("utf-8", "replace"))
    out = []
    for item in ((data.get("query") or {}).get("search") or [])[:n]:
        title = item.get("title") or ""
        out.append({"title": title,
                    "url": "https://zh.wikipedia.org/wiki/" +
                           urllib.parse.quote(title.replace(" ", "_")),
                    "snippet": _clean(item.get("snippet") or "")})
    return out


_ENGINE_FN = {"duckduckgo": _search_duckduckgo,
              "bing": _search_bing,
              "wikipedia": _search_wikipedia}


def search(query, limit=MAX_RESULTS):
    """在引擎链上轮换搜索；返回 {"ok", "engine", "results", "error"}。"""
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "empty query", "results": []}
    key = ("search", query, int(limit))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    errors = []
    for engine in ENGINES:
        if not _engine_ok(engine):
            errors.append("%s: cooling down" % engine)
            continue
        try:
            _throttle()
            results = _ENGINE_FN[engine](query, limit) or []
            if results:
                out = {"ok": True, "engine": engine, "results": results,
                       "error": ""}
                _cache_put(key, out)
                return out
            errors.append("%s: no results" % engine)
        except urllib.error.HTTPError as exc:
            _engine_penalise(engine, blocked=exc.code in (403, 429))
            errors.append("%s: HTTP %s" % (engine, exc.code))
        except Exception as exc:
            _engine_penalise(engine)
            errors.append("%s: %s" % (engine, str(exc)[:60]))
    return {"ok": False, "results": [], "error": "; ".join(errors[:3])}


# ---------------------------------------------------------------- 抓取
def fetch(url):
    """抓取网页正文（已过 SSRF 守卫）；返回 {"ok","url","title","text","error"}。"""
    ok, reason = check_url(url)
    if not ok:
        return {"ok": False, "url": url, "error": reason}
    key = ("fetch", url)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        _throttle()
        with _open(url) as resp:
            raw = resp.read(MAX_BODY_BYTES)
            ctype = (resp.headers.get("Content-Type") or "").lower()
        text = raw.decode("utf-8", "replace")
        if "html" in ctype or "<html" in text[:2000].lower():
            title_m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
            title = _clean(title_m.group(1)) if title_m else ""
            body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ",
                          text)
            body = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", body)
            body = _clean(body)
        else:
            title = ""
            body = text
        body = body[:MAX_FETCH_CHARS]
        out = {"ok": True, "url": url, "title": title, "text": body,
               "error": ""}
        _cache_put(key, out)
        return out
    except urllib.error.HTTPError as exc:
        return {"ok": False, "url": url, "error": "HTTP %s" % exc.code}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)[:120]}


# ---------------------------------------------------------------- 工具层
def tool_declarations():
    """两个 function 声明（模型看到的就是这两个可调用工具）。"""
    return [
        {"type": "function", "function": {
            "name": WEB_SEARCH_NAME,
            "description": ("Search the public web. Use for fresh facts, docs, "
                            "news or anything you are unsure about."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "search query"}},
                "required": ["query"]}}},
        {"type": "function", "function": {
            "name": WEB_FETCH_NAME,
            "description": ("Fetch one URL and return its readable text. "
                            "Only http/https public addresses are allowed."),
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "absolute url"}},
                "required": ["url"]}}},
    ]


def run_tool(name, arguments):
    """执行一次工具调用，返回给模型的文本结果。"""
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except Exception:
            args = {}
    args = args if isinstance(args, dict) else {}
    if name == WEB_SEARCH_NAME:
        res = search(args.get("query") or "")
        if not res.get("ok"):
            return json.dumps({"error": res.get("error") or "search failed"},
                              ensure_ascii=False)
        lines = []
        for i, item in enumerate(res["results"], 1):
            lines.append("%d. %s\n   %s\n   %s"
                         % (i, item.get("title") or "", item.get("url") or "",
                            item.get("snippet") or ""))
        return "Search results (%s):\n" % res.get("engine") + "\n".join(lines)
    if name == WEB_FETCH_NAME:
        res = fetch(args.get("url") or "")
        if not res.get("ok"):
            return json.dumps({"error": res.get("error") or "fetch failed"},
                              ensure_ascii=False)
        return "Fetched %s\nTitle: %s\n\n%s" % (res["url"], res.get("title"),
                                                 res.get("text") or "")
    return json.dumps({"error": "unknown tool %s" % name}, ensure_ascii=False)