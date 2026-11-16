#!/usr/bin/env python3
"""Qoder -> OpenAI-compatible reverse proxy (CN + Intl dual realm).

把 Qoder 国内版 (qoder.com.cn) 与国际版 (qoder.com) 的原生服务封装为标准
OpenAI 兼容接口。上游链路：

    客户端 OpenAI 请求
      -> build_qoder_body()      (官方 baseprompt 模板 + 会话压平)
      -> qoder_encode()          (Qoder 自定义 Base64 变体)
      -> COSY 签名               (RSA 包裹 AES 会话密钥 + MD5 请求签名)
      -> POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation
      -> SSE 信封解包            ({"headers","body","statusCodeValue"} 嵌套帧)
      -> 标准 OpenAI SSE / chat.completion 回给客户端

暴露：
    GET  /v1/models
    POST /v1/chat/completions     (stream=true / false)
    POST /v1/responses            (Responses API 双向转换)
    GET  /health
    以及看板全套管理接口 (accounts / usage / tasks / scheduler / settings / logs)

核心链路仅使用 Python 标准库；可选 Pillow 提供图片预处理（官方镜像内置）。

    python qoder_proxy.py                    # bind 127.0.0.1:8790
    python qoder_proxy.py --port 9000
    python qoder_proxy.py --api-key sk-local  # require a bearer token
"""
import argparse
import copy
import atexit
import hashlib
from collections import deque, OrderedDict
import re
import json
import os
MAX_PAYLOAD_BYTES = int(os.environ.get("QD_MAX_PAYLOAD_BYTES", 50 * 1024 * 1024))  # 50MB limit
import ipaddress
import socket
import signal
import ssl
import sys
import threading
import fnmatch
import time
import urllib.error
import urllib.parse
import urllib.request
import random
import uuid

import qoder_accounts
import qoder_activity
import qoder_agents
import qoder_anthropic

# issue #116：把宿主日志注入 anthropic 模块（它自己只 import 标准库）
import qoder_catalog
import qoder_keepalive
import qoder_images
import qoder_cache
import qoder_routing
import qoder_admin
import qoder_logpaths
import qoder_pricing
import qoder_settings
import qoder_session_cache
import qoder_sign
import qoder_updater
import qoder_webtools
from qoder_sign import qoder_encode, SESSIONS
from qoder_accounts import (get_realm_config, gateway_candidates,
                            local_vm_status)
from pathlib import Path

VERSION = "1.9.10"

CURRENT_REALM = os.environ.get("QD_PROXY_DEFAULT_REALM", "cn")


def is_model_banned(model):
    """P1-4：True = 该模型在本机被全局封禁，不送去上游。

    配置源：settings 的 banned_models（fnmatch pattern 列表，默认空 = 不封禁）。
    读配置失败一律 fail-open（不误封）。与 wb 的差异：配置在 settings 而非代码
    常量——本仓已有面板/设置机制。
    """
    name = str(model or "").strip().lower()
    if not name:
        return False
    try:
        patterns = qoder_settings.banned_models(ACCOUNTS_DIR)
    except Exception:
        return False
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def banned_model_message(model):
    """封禁模型的本机拒绝文案（完全不碰上游）。"""
    return ("模型 %s 已被本机网关封禁（settings 的 banned_models 配置）。"
            "如需使用，请在设置页或 settings.json 里调整 banned_models。"
            % model)


def key_model_message(entry, model):
    """每 Key 模型限制的拒绝文案（与全局封禁同风格）。"""
    name = (entry or {}).get("name") or "未命名"
    allowed = "、".join((entry or {}).get("models") or []) or "-"
    asked = str(model or "").strip() or "(未指定模型)"
    return ("API Key「%s」的模型限制不允许 %s。该 Key 目前允许：%s。"
            "请在设置页修改该 Key 的模型限制，或改用允许该模型的 Key。"
            % (name, asked, allowed))


# 198.18.0.0/15 (RFC 2544 benchmarking) 与 fdfe:dcba:9876::/48 被 Clash/mihomo
# 等本地代理用作 fake-IP DNS 段：开启透明代理的机器上所有公网域名都会解析到
# 这些网段。命中它说明 DNS 已被本机代理接管、真实 IP 不可见，此时跳过解析级
# 校验（名称级校验已完成）。
_FAKEIP_NETS = [
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
]
# 与 qoder_accounts 同款：macOS 的 getaddrinfo 会同时返回 IPv4 与
# IPv4-translated IPv6（::ffff:0:c612:2d），需要按内嵌 IPv4 识别 fake-IP。
_FAKEIP_V4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")


def _is_fake_ip(ip):
    candidates = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        elif ip in _FAKEIP_V4_TRANSLATED:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return any(addr in net for addr in candidates for net in _FAKEIP_NETS)


def _host_boundary_violation(ip, allow_local):
    """True when the resolved/IP address must be refused."""
    if ip.is_loopback:
        return not allow_local
    if (ip.is_private or ip.is_reserved or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified):
        return True
    return False


def validate_public_http_url(url, allow_local=False):
    """SSRF 防护：仅允许 http/https，且 host 不得指向本机/私有/保留网段。

    与 qoder_accounts.validate_public_http_url 同逻辑、独立实现防同错：
    本文件的 urlopen 调用点必须经过本文件的边界校验（签名链路上的 URL
    在 sess.headers() 之后再校验一次，确保进入 Request 的字符串是洁净的）。
    allow_local 仅供显式面向本机网关的开发/验证脚本使用。
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().strip("[]").lower()
    if not host:
        raise ValueError("URL host is required")
    # 企业 VPC 专属域通常解析到公司内网；只放行 QD_CN_VPC_ENDPOINT
    # 推导出的固定主机，普通公网 URL 的校验逻辑保持不变。
    if qoder_accounts.is_configured_vpc_host(host):
        return url
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        if not allow_local:
            raise ValueError("requests to localhost are not allowed")
        return url
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _host_boundary_violation(literal, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % literal)
        return url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("cannot resolve URL host %r: %s" % (host, exc))
    addrs = []
    for info in infos:
        addr = str(info[4][0]).strip("[]")
        try:
            addrs.append(ipaddress.ip_address(addr))
        except ValueError:
            raise ValueError("URL host resolved to a non-IP address: %r" % addr)
    if addrs and all(_is_fake_ip(ip) for ip in addrs):
        return url  # fake-IP DNS：真实 IP 不可见，名称级校验已通过
    for ip in addrs:
        if _host_boundary_violation(ip, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % ip)
    return url


# 双区自动路由（#26 步1/步2）：默认开启。**显式指定永远优先** —— 本开关只作用于
# 「用户没指定区域」时的兜底路径（?realm= / Key 绑定 / X-Realm 任一存在时都不会走到这里）。
AUTO_REALM_ENABLED = os.environ.get("QD_AUTO_REALM", "1").lower() not in (
    "0", "false", "no", "off")


def detect_model_realm(model_id):
    """模型 -> 出口区域（**纯数据判断，不查账号可用性**）。

    优先级：区域独占硬表 > 清单驱动自动路由（步1）> 全局开关。
    清单驱动只在「该模型只在一个区域存在」时换区；双区都有的模型维持当前区
    （判不出就不猜 —— 双区同名模型物理上无法靠名字区分，见评估 §2）。
    账号可用性不在这里判（那会污染 row_matches_realm 等纯统计路径），
    由 fallback_realm_by_accounts() 在选号前单独做（步2）。
    """
    owner = exclusive_realm(model_id)
    if owner:
        return owner
    if not model_id:
        return CURRENT_REALM
    if AUTO_REALM_ENABLED:
        realm, reason = qoder_catalog.auto_realm_for_model(model_id, CURRENT_REALM)
        if realm:
            log("auto realm switch: %s (model=%r, current=%s -> %s)"
                % (reason, model_id, CURRENT_REALM, realm), tag="realm")
            return realm
    return CURRENT_REALM


def auto_realm_reason(model, realm):
    """本次自动换区的**确定性**原因（给面板显示结论，不让它自己推断）。

      model_missing  当前区的官方清单里没有这个模型（独占表与清单驱动都归此类）
      no_account     模型本身在当前区有（或无法判断），是账号可用性把我们推过去的
    用同一份数据源复算（catalog 的 auto_realm_for_model），与 detect_model_realm
    同源，不做二次猜测。
    """
    try:
        hint, _why = qoder_catalog.auto_realm_for_model(model, CURRENT_REALM)
        if hint == realm:
            return "model_missing"
    except Exception:
        pass
    return "no_account"


def fallback_realm_by_accounts(realm, model):
    """步2：当前区**一个可用账号都没有**时回退另一区（判不出就维持现状）。

    只在未显式指定区域时被调用（open_upstream 内）。判据三段：
      当前区 count_ready > 0        -> 不动
      当前区 == 0 且 另一区 > 0     -> 换区（打日志）
      两区都是 0 / 异常 / 无池      -> 不动（交给既有错误路径，不猜）
    """
    if not AUTO_REALM_ENABLED or POOL is None:
        return realm
    if realm != CURRENT_REALM:
        # **模型归属优先于账号可用性**：realm 已被模型规则（独占表/清单驱动）推走，
        # 说明当前区根本没有这个模型；此时再按账号把请求换回去，只会打到
        # 「没有这个模型」的上游。停在目标区、由选号给出「无可用账号」的明确错误
        # 才是正确行为。
        return realm
    try:
        if POOL.count_ready(realm, model=model or None) > 0:
            return realm
        other = "intl" if realm == "cn" else "cn"
        if POOL.count_ready(other, model=model or None) > 0:
            log("auto realm switch (no ready account): realm %s has no usable "
                "account for %r -> %s" % (realm, model or "?", other), tag="realm")
            return other
    except Exception as exc:
        log("auto realm fallback skipped: %s" % exc, level="WARN")
    return realm


# 目前双区模型清单不同（源自官方 catalog 快照差集），独占表给区域特供模型。
INTL_EXCLUSIVE_PREFIXES = getattr(qoder_catalog, "INTL_EXCLUSIVE_PREFIXES", ())
CN_EXCLUSIVE_PREFIXES = getattr(qoder_catalog, "CN_EXCLUSIVE_PREFIXES", ())
INTL_EXCLUSIVE = getattr(qoder_catalog, "INTL_EXCLUSIVE", set())
CN_EXCLUSIVE = getattr(qoder_catalog, "CN_EXCLUSIVE", set())


def exclusive_realm(model_id):
    """"intl"/"cn" when only that exit serves the model, else "".

    先把人类可读别名解析成上游 key 再判独占（glm-5.2 -> gm51model 是国内独占）。
    """
    if not model_id:
        return ""
    m = str(model_id).lower()
    resolved = qoder_catalog.resolve_upstream_key(model_id).lower()
    for cand in (resolved, m):
        if cand in INTL_EXCLUSIVE or cand.startswith(INTL_EXCLUSIVE_PREFIXES):
            return "intl"
        if cand in CN_EXCLUSIVE or cand.startswith(CN_EXCLUSIVE_PREFIXES):
            return "cn"
    return ""


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def install_console_close_handler():
    """Release the port when the console window is closed by the user.

    Windows does not kill child processes when a console window closes, so
    the proxy (started by the .bat as a child of cmd.exe) would survive and
    keep the port bound - the next launch then wrongly reports "another
    proxy is already running". Registering an event-driven handler for
    CTRL_CLOSE_EVENT is the reliable signal. Harmless without a console.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os._exit(0)
            return False

        handler = PHANDLER_ROUTINE(_handler)   # keep the callback referenced
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler
    except Exception:
        return None


# Launcher names differ per platform; the messages below are read by whoever
# is staring at the terminal, so point at the file that actually exists there.
IS_WINDOWS = os.name == "nt"
START_SCRIPT = "start-qoder-proxy.bat" if IS_WINDOWS \
    else "start-qoder-proxy.sh"
FIREWALL_SCRIPT = "allow-firewall.bat" if IS_WINDOWS else "allow-firewall.sh"


def port_owner_hint(port):
    """One-liner(s) the user can paste to see which process holds a port."""
    cmds = ["netstat -ano | findstr :%d" % port] if IS_WINDOWS else \
        ["ss -ltnp | grep :%d" % port,
         "lsof -iTCP:%d -sTCP:LISTEN" % port]
    return cmds


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """端口独占绑定（Windows 双开防护）。

    POSIX 的 SO_REUSEADDR 只允许复用 TIME_WAIT，重启瞬间不会误伤，保持开启；
    Windows 的 SO_REUSEADDR 连「正在被监听的端口」也允许抢绑 —— 升级重启时
    新旧两个实例都能 bind 成功，连接被静默分流，旧实例继续应答 /health，新版本
    永远不生效（1.9.7 实测：看板一直提示「有新版本」）。Windows 改用
    SO_EXCLUSIVEADDRUSE，抢绑直接 WSAEADDRINUSE。
    """
    allow_reuse_address = not IS_WINDOWS

    if IS_WINDOWS:  # pragma: no cover - 平台分支
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


def serve_http_server(server):
    """Handle PID 1 SIGTERM without calling shutdown on the serving thread.

    SystemExit unwinds serve_forever into server_close and normal atexit cleanup.
    HTTPServer.shutdown() must not be used in this handler: it waits for the same
    thread's serve_forever loop and deadlocks. Repeated TERM during close is ignored.
    """
    previous = None
    installed = False
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise SystemExit(0)

    try:
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, terminate)
            installed = True
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("bye")
    finally:
        stopping = True
        try:
            server.server_close()
        finally:
            if installed:
                signal.signal(signal.SIGTERM, previous)


CHAT_PATH = ("/algo/api/v2/service/pro/sse/agent_chat_generation"
             "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1")
MODELS_PATH = "/algo/api/v2/model/list?Encode=1"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

# 官方 baseprompt 模板（与桌面端一致的请求体骨架）
BASEPROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "baseprompt.json")
try:
    with open(BASEPROMPT_PATH, encoding="utf-8") as _bp_fh:
        BASEPROMPT = json.load(_bp_fh)
except Exception:
    BASEPROMPT = {}

NOISE_KEYS = ("extra_fields", "refusal", "reasoning_content")

# DeepSeek 上游 model key（官方目录：dmodel=DeepSeek-V4-Pro / dfmodel=DeepSeek-Flash）。
# 这族模型的多轮一致性与 reasoning_content 强相关，见 is_deepseek_model。
DEEPSEEK_MODEL_KEYS = ("dmodel", "dfmodel")


class BodyTooLarge(Exception):
    """Raised when a request body exceeds the configured cap."""

    def __init__(self, length):
        super(BodyTooLarge, self).__init__(length)
        self.length = length


class BadJSON(Exception):
    """Raised when a request body is present but not a JSON object."""


class UpstreamStatus(Exception):
    """Raised when the upstream SSE envelope carries a non-200 status."""

    def __init__(self, status, detail=""):
        super(UpstreamStatus, self).__init__("upstream status %s" % status)
        self.status = status
        self.detail = str(detail or "")


# CORS is only needed by browser-based chat clients that call the OpenAI-style
# API from another origin. Management routes serve the dashboard (same-origin)
# and get no ACAO header - that keeps a stray LAN page from reading them.
CORS_PATH_PREFIXES = ("/v1", "/chat", "/completions", "/models", "/responses")
MANAGEMENT_PATH_PREFIXES = ("/v1/usage", "/usage", "/accounts", "/settings",
                            "/tasks", "/scheduler", "/panel", "/logs")


def cors_origin_allowed(path):
    """True when the OpenAI-style API path should advertise CORS."""
    path = (path or "").split("?")[0]
    if path.startswith(MANAGEMENT_PATH_PREFIXES):
        return False
    return path.startswith(CORS_PATH_PREFIXES)


_lock = threading.Lock()
_login_lock = threading.Lock()
_login_attempts = {}  # ip -> list of timestamp


def _prune_login_attempts(now=None, window=60):
    """Drop stale per-IP entries so the dict cannot grow without bound.

    Caller must hold _login_lock.
    """
    now = now or time.time()
    for ip in list(_login_attempts.keys()):
        recent = [t for t in _login_attempts[ip] if now - t < window]
        if recent:
            _login_attempts[ip] = recent
        else:
            del _login_attempts[ip]


_models_cache = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}

# Usage accounting: every upstream response carries a usage block, and the
# proxy also records one JSONL line per request.
LOG_DIR = qoder_logpaths.resolve_log_root(os.path.dirname(os.path.abspath(__file__)))
USAGE_DIR = qoder_logpaths.resolve_usage_dir(os.path.dirname(os.path.abspath(__file__)))
USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
USAGE_SUMMARY = os.path.join(USAGE_DIR, "usage-summary.json")
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "dashboard.html")
# Locale packs are plain JSON files dropped into i18n/: the folder listing *is* the
# registry, so adding a language never touches code and never needs a restart.
I18N_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")
# The route is unauthenticated (the login gate itself needs translating), so the
# requested name is matched against this allowlist before it ever reaches the
# filesystem. Anchored at both ends and .json-only: no separator, no "..", no NUL.
I18N_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}\.json$")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                "cached_tokens", "total_tokens", "credit",
                # issue #22：折扣时段对账要用的原价积分（数值，参与聚合）。
                # billable 是布尔值，**不做聚合**、只写进 JSONL 行（见 _extract_usage）。
                "original_credits")

# Web-panel access control. The panel is gated by its own password (default
# "admin"), independent of the /v1 API key. Sessions are signed tokens whose key
# lives in accounts/settings.json, so a restart no longer forces a re-login.
PANEL = qoder_settings.PanelSessions(lambda: ACCOUNTS_DIR)
API_KEY_FILE_SET = False


def configured_keys():
    """Panel-managed API keys, always read fresh so panel edits apply at once."""
    try:
        return qoder_settings.api_keys(ACCOUNTS_DIR)
    except Exception as exc:
        log("could not read api keys: %s" % exc)
        return []


def auth_required():
    """Whether /v1 calls must present a key at all."""
    if qoder_settings.auth_disabled(ACCOUNTS_DIR):
        return False
    if any(entry.get("enabled") for entry in configured_keys()):
        return True
    return bool(API_KEY)


def identify_key(supplied):
    """Return the key entry a caller used, or None when nothing matches.

    Once the panel has at least one key, those keys are the only accepted
    credentials - otherwise a launcher key left in a .bat file would silently
    keep working after the panel was locked down.
    """
    extra = () if configured_keys() else (API_KEY,)
    return qoder_settings.match_api_key(ACCOUNTS_DIR, supplied, extra_keys=extra)


def _empty_stats():
    return {"requests": 0, "errors": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "credit": 0.0, "original_credits": 0.0,
            "cost_usd": 0.0, "unpriced": 0,
            "started": time.time(),
            "by_model": {},
            "ttft_ms_sum": 0, "ttft_samples": 0,
            "gen_ms_sum": 0, "gen_samples": 0,
            "wall_ms_sum": 0, "wall_samples": 0}


_usage = _empty_stats()


_BILLED_WARN_AT = {}


def _extract_usage(usage):
    """Normalize the upstream usage block into the fields we track."""
    if not usage:
        return {}
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": details.get("reasoning_tokens") or 0,
        "cached_tokens": usage.get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or details.get("cached_tokens") or prompt_details.get("cached_tokens") or 0,
        # issue #129 后续：区分「真的没命中」与「上游没回报缓存字段」；
        # 后者在面板显示成 `?`，且不计入「全价请求」。
        "cached_reported": bool(
            "cached_tokens" in usage or "prompt_cache_hit_tokens" in usage
            or "cached_tokens" in details or "cached_tokens" in prompt_details),
        "total_tokens": usage.get("total_tokens") or 0,
        # issue #22：上游给的是**复数 credits** —— 旧实现只读单数 credit，
        # 恒为 None -> 所有 usage 记录成 0（历史数据无法回填，原始值未落盘）。
        # 保留单数兜底以兼容其它区域/版本的字段差异。
        "credit": usage.get("credits") or usage.get("credit") or 0,
        "original_credits": usage.get("original_credits") or 0,
        # billable 只记录不聚合（布尔求和无意义）：进 JSONL 行，不进
        # USAGE_FIELDS 的累加与 by_model。
        "billable": bool(usage.get("billable")),
    }


def realm_scope(value):
    """面板 realm 取值的归一：显式 "all"（大小写不敏感）= 全部区域 → None（无过滤）。

    只有字面 "all" 被当作通配：None / "" 本来就表示"无过滤"（各路由会先回落到
    CURRENT_REALM 再传进来），"cn" / "intl" / 其它未知值**一律原样保留既有语义**
    （按字面比较；未知值自然过滤出空集 —— 不在这里替面板猜意图）。
    """
    v = str(value if value is not None else "").strip()
    return None if v.lower() in ("", "all") else v


def row_matches_realm(row, realm):
    realm = realm_scope(realm)
    if not realm:
        return True
    r = row.get("realm")
    if r:
        return r == realm
    acct_uid = row.get("account")
    if acct_uid and POOL:
        acc = POOL.get(acct_uid)
        if acc:
            return acc.realm == realm
    model = row.get("model")
    if model:
        return detect_model_realm(model) == realm
    return realm == "cn"


# issue #129 后续：积分三段折算系数（每千 token 的积分）。默认值来自报告者的实测
# 拟合（244 条 DeepSeek-Flash 实扣 + 110 条 Qwen3.8-Flash 名义，残差 <1%）；
# 可用 QD_CREDIT_COEF="非缓存,缓存,输出" 覆盖成自己算出来的系数。
_CREDIT_COEF_DEFAULT = (1.069, 0.086, 4.277)


def credit_coefficients():
    """(非缓存输入, 缓存输入, 输出) 每千 token 积分系数。"""
    raw = str(os.environ.get("QD_CREDIT_COEF") or "").strip()
    if raw:
        try:
            parts = [float(x) for x in raw.replace(",", " ").split()]
            if len(parts) == 3 and all(x >= 0 for x in parts):
                return tuple(parts)
        except Exception:
            pass
    return _CREDIT_COEF_DEFAULT


def split_credit(prompt_tokens, cached_tokens, completion_tokens):
    """按三段折算积分：非缓存输入 / 缓存输入 / 输出（issue #129 后续）。"""
    a, b, c = credit_coefficients()
    try:
        p = max(0, int(prompt_tokens or 0))
        cached = max(0, int(cached_tokens or 0))
        out = max(0, int(completion_tokens or 0))
    except (TypeError, ValueError):
        return {"input": 0.0, "cached": 0.0, "output": 0.0}
    cached = min(cached, p)
    fresh = max(0, p - cached)
    return {"input": round(fresh * a / 1000.0, 4),
            "cached": round(cached * b / 1000.0, 4),
            "output": round(out * c / 1000.0, 4)}


def record_usage(model, usage, stream=None, elapsed_ms=None, ttft_ms=None,
                 gen_ms=None, fp=None, account=None, effort=None,
                 effort_requested=None, key_id=None, realm_reason=None,
                 session_trace=None):
    """Accumulate stats, append a JSONL row, and persist the summary.

    P1-3（推理档位审计 + key 归属）：后三个参数都是**可选**的，旧调用路径不传
    就一个键都不写 —— 行格式与改动前逐字节一致。
      effort            实际下发给上游的档位（None = 本次没有下发该参数）
      effort_requested  客户端请求的原始档位（归一化之前）
      key_id            API key 的**标识**（panel id / legacy / launcher），不涉明文
    请求档位与实际档位同时落盘，才能看出归一化有没有把它们改掉/丢掉。
    """
    fields = _extract_usage(usage)
    if not fields:
        return None
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "stream": bool(stream),
        "elapsed_ms": elapsed_ms,
        "ttft_ms": ttft_ms,
        "gen_ms": gen_ms,
    }
    row.update(fields)
    if fp:
        row.update(fp)
    if session_trace:
        # Never serialize tenant identifiers, client conversation IDs or prompts.
        for field in ("session_affinity", "upstream_session_id", "route_rule"):
            if session_trace.get(field):
                row[field] = session_trace[field]
        for field in ("output_content_chars", "output_reasoning_chars", "output_has_tool_call",
                      "output_finish_reason", "output_code_without_tools"):
            if field in session_trace:
                row[field] = session_trace[field]
    if account:
        row["account"] = account
    # P1-3：推理档位审计（请求档位 vs 实际下发档位）与 API key 归属。
    # 三者都是可选参数：不传则一个键都不写，旧行格式不变。
    if effort_requested:
        row["reasoning_effort_requested"] = str(effort_requested)
    if effort:
        row["reasoning_effort"] = str(effort)
    elif effort_requested:
        # 请求了档位但归一化后不下发（模型无档位表 / 被丢弃）：显式记空串，
        # 便于区分「客户端没请求」与「请求了但没生效」两种情况。
        row["reasoning_effort"] = ""
    if key_id:
        # 只记 key 的**标识**（panel 分配的 id / legacy / launcher），绝无明文 key。
        row["key_id"] = str(key_id)
    if realm_reason is not None:
        # #26：本次是否**自动换区**及原因（持久化，面板不依赖响应头即可读到）。
        #   "model_missing" 当前区官方清单里没有这个模型（独占表 / 清单驱动）
        #   "no_account"    模型本区有（或无法判断），是账号可用性把它推走的
        #   ""（空串）      本次**没有换区**（正常路径）
        #   键不存在        该行产生于本字段上线前（旧行）—— 与「没换区」明确区分
        row["realm_reason"] = str(realm_reason)
    acc = POOL.get(account) if (account and POOL) else None
    row["realm"] = acc.realm if acc else CURRENT_REALM
    if gen_ms and gen_ms > 0:
        row["tokens_per_sec"] = round(
            fields["completion_tokens"] / (gen_ms / 1000.0), 2)
    if fields["prompt_tokens"] > 0:
        row["cache_hit_pct"] = round(
            fields["cached_tokens"] * 100.0 / fields["prompt_tokens"], 1)
    with _lock:
        _usage["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                _usage[k] += fields[k]
        if ttft_ms is not None:
            _usage["ttft_ms_sum"] += ttft_ms
            _usage["ttft_samples"] += 1
        if gen_ms is not None:
            _usage["gen_ms_sum"] += gen_ms
            _usage["gen_samples"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
        per = _usage["by_model"].setdefault(
            model, {"requests": 0, **{k: 0 for k in USAGE_FIELDS}})
        per["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                per[k] += fields[k]
        summary = json.loads(json.dumps(_usage))
    _persist_usage(row, summary, "usage persist failed")
    # issue #43：把「这个号刚被用过」告诉余额刷新器（**只打标，不联网**）。
    # 打标失败绝不能影响记账/响应，所以整段吞异常。
    try:
        if account and CREDITS_USAGE_REFRESHER is not None:
            CREDITS_USAGE_REFRESHER.mark_used(account)
    except Exception:
        pass
    try:
        t_tokens = fields.get("total_tokens", 0)
        dur = " %dms" % elapsed_ms if elapsed_ms is not None else ""
        acc_tag = " acct=%s" % account[:8] if account else ""
        speed_tag = " %st/s" % row.get("tokens_per_sec", 0) \
            if row.get("tokens_per_sec") else ""
        log("chat done: model=%s%s%s tokens=%d (in=%d out=%d)%s"
            % (model, acc_tag, dur, t_tokens, fields.get("prompt_tokens", 0),
               fields.get("completion_tokens", 0), speed_tag), tag="chat")
    except Exception:
        pass
    if session_trace and session_trace.get("output_code_without_tools"):
        log("response observation: model=%s realm=%s code/text output without tool call "
            "(content_chars=%s reasoning_chars=%s finish=%s); no tool was dispatched" %
            (model, row.get("realm", "?"), session_trace.get("output_content_chars", 0),
             session_trace.get("output_reasoning_chars", 0), session_trace.get("output_finish_reason", "?")),
            level="WARN", tag="chat")
    return row


def _persist_usage(row, summary, fail_label):
    """Append one JSONL row and atomically rewrite the summary.

    The temp file carries a unique suffix: two threads writing the same
    "<summary>.tmp" race, and the loser's os.replace() fails with ENOENT
    because the winner already renamed the file away.
    """
    try:
        usage_dir = Path(USAGE_DIR).resolve()
        usage_dir.mkdir(parents=True, exist_ok=True)
        log_path = usage_dir / os.path.basename(USAGE_LOG)
        summary_path = usage_dir / os.path.basename(USAGE_SUMMARY)
        if not (log_path.is_relative_to(usage_dir)
                and summary_path.is_relative_to(usage_dir)):
            raise ValueError("usage path escapes base directory")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp_summary = usage_dir / (
            os.path.basename(USAGE_SUMMARY)
            + ".%d.%d.tmp" % (os.getpid(), threading.get_ident()))
        if not tmp_summary.is_relative_to(usage_dir):
            raise ValueError("usage path escapes base directory")
        try:
            tmp_summary.write_text(json.dumps(summary, ensure_ascii=False,
                                              indent=2), encoding="utf-8")
            os.replace(tmp_summary, summary_path)
        except Exception:
            try:
                os.unlink(tmp_summary)
            except Exception:
                pass
            raise
    except Exception as exc:
        log("%s: %s" % (fail_label, exc))


def record_error(model, status, message, elapsed_ms=None,
                 stream=None, account=None, key_id=None):
    """Count a failed request and append it to the log so errors are visible.

    后三个参数与 record_usage 同构，全是**可选**：不传就一个键都不写，行格式
    与改动前逐字节一致（老调用路径与历史日志不受影响）。
      stream   是否流式请求
      account  出错的账号 uid（拿得到才传：open_upstream 失败时选号未成功，
               调用点手里没有 account，传 None 即可——不能为了填字段去猜）
      key_id   API key 标识，同 record_usage（不涉明文）
    失败行带上账号/Key，才能看出「是哪个号在失败、是不是某个 Key 的客户端在
    打」；此前失败行这三项恒为空，面板只能显示成「历史行未记录 Key」。
    """
    try:
        # issue #101：从 400 文本里学输出上限（下一次请求直接 clamp）
        learn_max_output_limit(message, model)
    except Exception:
        pass
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "error": True,
        "status": status,
        # 保留更完整的上游错误（provider_error 的内层 details 常在 200+ 字节）
        "message": str(message)[:400],
        "elapsed_ms": elapsed_ms,
    }
    if stream is not None:
        row["stream"] = bool(stream)
    if account:
        row["account"] = account
        try:
            actual = POOL.get(account) if POOL else None
            if actual and getattr(actual, "realm", "") in ("cn", "intl"):
                row["realm"] = actual.realm
        except Exception:
            pass
    if key_id:
        row["key_id"] = key_id
    with _lock:
        _usage["errors"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
        summary = json.loads(json.dumps(_usage))
    _persist_usage(row, summary, "error persist failed")
    dur = " %dms" % elapsed_ms if elapsed_ms is not None else ""
    log("request error: model=%s%s status=%s msg=%s"
        % (model, dur, status, str(message)[:360]),
        level="ERROR", tag="chat")
    return row


def _pct(values, q):
    """Nearest-rank percentile (no interpolation) - good enough for latency."""
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((q / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]


# ---------------------------------------------------------------------------
# 时间窗口：?range=day|week|month|all|custom&since=&until=
# ---------------------------------------------------------------------------
# 语义（与面板选择器、wb 参考实现一致）：day/week/month 是**锚到本地零点**的日历
# 窗口（week 从周一起算、month 从 1 号起算），custom 取面板传来的两个 epoch（任一端
# 可缺省，反了自动交换），all / 空 / 未知值 = 不设窗口（历史全量）。返回 (since, until)，
# None 表示该侧无界；两侧都 None 时下游**完全不做过滤**（_row_in_window 直接返回 True，
# 连 at 都不读），因此不传 range 的输出与改动前逐字节一致。
def _epoch_or_none(value):
    """面板传来的 epoch 秒；负值/不可解析一律丢弃（不做钳制，宁可不设界）。"""
    if value in (None, "", False):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _local_day_start(ts):
    """任意时间戳 -> 本地自然日 0 点（epoch 秒）。"""
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, 0, 0, -1)))


def _local_midnight(days_back=0):
    return _local_day_start(time.time() - days_back * 86400)


def range_window(value, since=None, until=None):
    v = str(value or "").strip().lower()
    if v in ("today", "day", "1d"):
        return _local_midnight(), None
    if v in ("week", "w"):
        return _local_midnight(days_back=time.localtime().tm_wday), None
    if v in ("month", "m"):
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)), None
    if v == "custom":
        lo, hi = _epoch_or_none(since), _epoch_or_none(until)
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        return lo, hi
    return None, None


def range_query(query):
    """从 parse_qs 的查询串里取 (range, since, until)；缺省一律 None。"""
    def first(name):
        values = query.get(name) or [None]
        return values[0] if values else None
    return first("range"), first("since"), first("until")


def _row_in_window(row, since, until):
    """窗口过滤：两侧都 None 时不做任何读取（老路径零开销、零差异）。"""
    if since is None and until is None:
        return True
    at = row.get("at")
    if not isinstance(at, (int, float)):
        at = 0
    if since is not None and at < since:
        return False
    if until is not None and at > until:
        return False
    return True


_perf_cache = {}
_perf_lock = threading.Lock()


def perf_stats(sample=5000, realm=None, ttl=10, range=None, since=None, until=None):
    """Cached wrapper: parsing thousands of rows is CPU-heavy, and the
    dashboard polls this endpoint every few seconds."""
    r = realm or CURRENT_REALM
    lo, hi = range_window(range, since, until)
    try:
        # 缓存键带上**解析后的边界**（不是 range 标签）：week 与 month 相互重叠，
        # 一个标签无法描述两个窗口，否则会拿一个窗口的数据冒充另一个。
        key = (int(sample), r, lo, hi)
    except Exception:
        key = (5000, r, lo, hi)
    now = time.time()
    with _perf_lock:
        hit = _perf_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _perf_stats_uncached(sample, realm, since=lo, until=hi)
    with _perf_lock:
        _perf_cache[key] = (time.time(), data)
    return data


def _perf_stats_uncached(sample=5000, realm=None, since=None, until=None):
    """Latency percentiles + derived rates, computed from the JSONL log.

    since/until 为窗口边界（None=该侧无界）；两者都 None 时行为与改动前一致。
    """
    ttfts, gens, walls, rates, hits, tok_rates = [], [], [], [], [], []
    total = ok = err = 0
    m_buckets = {}
    # 只读日志末尾 sample 行：readlines() 会把整个日志读成字符串列表。
    rows = [raw.decode("utf-8", "replace") for raw in _tail_lines(USAGE_LOG, sample)]
    for line in rows:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if realm and not row_matches_realm(r, realm):
            continue
        if not _row_in_window(r, since, until):
            continue
        total += 1
        if r.get("error"):
            err += 1
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        ok += 1
        if r.get("ttft_ms") is not None:
            ttfts.append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            gens.append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            walls.append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            tok_rates.append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            hits.append(r["cache_hit_pct"])
        m_id = r.get("model") or "unknown"
        mb = m_buckets.setdefault(m_id, {"total": 0, "ok": 0, "err": 0,
                                         "ttfts": [], "gens": [], "walls": [],
                                         "tok_rates": [], "hits": []})
        mb["total"] += 1
        if r.get("error"):
            mb["err"] += 1
        else:
            mb["ok"] += 1
        if r.get("ttft_ms") is not None:
            mb["ttfts"].append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            mb["gens"].append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            mb["walls"].append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            mb["tok_rates"].append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            mb["hits"].append(r["cache_hit_pct"])

    def block(vals):
        if not vals:
            return None
        return {
            "avg": round(sum(vals) / len(vals), 1),
            "p50": _pct(vals, 50),
            "p90": _pct(vals, 90),
            "p99": _pct(vals, 99),
            "max": max(vals),
            "samples": len(vals),
        }

    return {
        "sampled": total,
        "success": ok,
        "errors": err,
        "inner_errors": inner_error_snapshot(),
        "success_rate_pct": round(ok * 100.0 / total, 1) if total else None,
        "ttft_ms": block(ttfts),
        "generation_ms": block(gens),
        "wall_ms": block(walls),
        "tokens_per_sec": block(tok_rates),
        "cache_hit_pct": block(hits),
        "by_model": {
            mid: {
                "requests": mb["total"],
                "errors": mb["err"],
                "success_rate_pct": round(mb["ok"] * 100.0 / mb["total"], 1)
                if mb["total"] else None,
                "ttft_ms": block(mb["ttfts"]),
                "generation_ms": block(mb["gens"]),
                "wall_ms": block(mb["walls"]),
                "tokens_per_sec": block(mb["tok_rates"]),
                "cache_hit_pct": block(mb["hits"]),
            } for mid, mb in m_buckets.items()
        },
    }


_snap_cache = {}
_snap_lock = threading.Lock()


def usage_snapshot(realm=None, ttl=10):
    """Cached wrapper: the dashboard polls this every few seconds."""
    r = realm or CURRENT_REALM
    now = time.time()
    with _snap_lock:
        hit = _snap_cache.get(r)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _usage_snapshot_uncached(r)
    with _snap_lock:
        _snap_cache[r] = (time.time(), data)
    return data


_PRICING_CACHE = {"at": 0.0, "data": {}}


def _PRICING_OVERRIDES():
    """usage/pricing-overrides.json，30 秒缓存（改完不用重启）。"""
    now = time.time()
    if now - _PRICING_CACHE["at"] > 30:
        _PRICING_CACHE["data"] = qoder_pricing.load_overrides(USAGE_DIR)
        _PRICING_CACHE["at"] = now
    return _PRICING_CACHE["data"]


def _usage_snapshot_uncached(realm=None):
    r = realm or CURRENT_REALM
    rep = POOL.representative(realm=r) if POOL else current_account()
    snap = _empty_stats()
    snap["started"] = _usage.get("started", time.time())
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if r and not row_matches_realm(row, r):
                    continue
                if row.get("error"):
                    snap["errors"] += 1
                else:
                    snap["requests"] += 1
                    for k in USAGE_FIELDS:
                        if k in row:
                            snap[k] += (row[k] or 0)
                    m = row.get("model") or "unknown"
                    per = snap["by_model"].setdefault(
                        m, {"requests": 0, "accounts": {},
                            **{k: 0 for k in USAGE_FIELDS}})
                    per["requests"] += 1
                    for k in USAGE_FIELDS:
                        if k in row:
                            per[k] += (row[k] or 0)
                    # 费用估算（参考 OpenRouter 价，读时折算；未知模型不计）
                    _c = qoder_pricing.cost_of(
                        row.get("model"), row.get("prompt_tokens") or 0,
                        row.get("cached_tokens") or 0,
                        row.get("completion_tokens") or 0, _PRICING_OVERRIDES())
                    if _c is not None:
                        snap["cost_usd"] = snap.get("cost_usd", 0.0) + _c
                        per["cost_usd"] = per.get("cost_usd", 0.0) + _c
                    else:
                        snap["unpriced"] = snap.get("unpriced", 0) + 1
                    acct_id = row.get("account")
                    if acct_id:
                        per.setdefault("accounts", {})
                        per["accounts"][acct_id] = per["accounts"].get(acct_id, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage snapshot read failed: %s" % exc)
    snap["since"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(snap.get("started", time.time())))
    snap["log_file"] = USAGE_LOG
    snap["realm"] = r
    snap["accounts_map"] = {a.uid: {"nickname": a.nickname, "realm": a.realm}
                            for a in POOL.accounts} if POOL else {}
    snap["account"] = {
        "uid": (rep.uid if rep else ""),
        "domain": (rep.domain if rep else ""),
        "issuer": ("qoder" if rep else ""),
        "credential_file": (os.path.basename(rep.path) if rep and rep.path else ""),
        "expires_at": (rep.expires_at if rep else 0),
        "accounts": (len(POOL.accounts) if POOL else 0),
        "accounts_ready": (POOL.count_ready() if POOL else 0),
    }
    return snap


def _tail_lines(path, max_lines, chunk=256 * 1024):
    """Return up to the last `max_lines` non-empty lines, oldest first.

    The usage log passes 20MB within a day. Scanning it end to end on every
    dashboard poll was the dominant cost behind slow /usage/* responses.
    """
    lines = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(lines) < max_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    if not raw.strip():
                        continue
                    lines.append(raw)
                    if len(lines) >= max_lines:
                        break
            if len(lines) < max_lines and buf.strip():
                lines.append(buf)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log("tail read failed: %s" % exc)
        return []
    lines.reverse()
    return lines


def count_usage_rows(realm=None):
    """Reuse recent-page totals while the log fingerprint stays unchanged."""
    scope = realm_scope(realm)
    stamp = _usage_log_fingerprint()
    realms = tuple((a.uid, a.realm) for a in (POOL.accounts if POOL else []))
    key = ("count", scope, CURRENT_REALM, realms, stamp)
    return _USAGE_REPORT_CACHE.get(
        key, lambda: _count_usage_rows_uncached(scope), ttl=5,
        valid=lambda: _usage_log_fingerprint() == stamp)


def _count_usage_rows_uncached(realm=None):
    """Cheap row count - substring match instead of a full JSON parse."""
    realm = realm_scope(realm)      # realm=all 走无 needles 分支，与逐行过滤一致
    needles = ()
    if realm:
        needles = ('"realm": "%s"' % realm, '"realm":"%s"' % realm)
    n = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if not needles:
                    n += 1
                    continue
                if any(x in line for x in needles):
                    n += 1
                    continue
                if '"realm"' in line:
                    continue
                try:
                    if row_matches_realm(json.loads(line), realm):
                        n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return n


def recent_usage(limit=100, realm=None, page=1):
    """Paginated rows from the tail of the log (page 1 is latest)."""
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    total = count_usage_rows(realm)
    total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
    page = min(page, total_pages)
    target_count = page * limit
    matching = []
    chunk = 256 * 1024
    try:
        with open(USAGE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(matching) < target_count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    st = raw.strip()
                    if not st:
                        continue
                    try:
                        item = json.loads(st.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if realm and not row_matches_realm(item, realm):
                        continue
                    matching.append(item)
                    if len(matching) >= target_count:
                        break
            if len(matching) < target_count and buf.strip():
                try:
                    item = json.loads(buf.strip().decode("utf-8", "replace"))
                    if not realm or row_matches_realm(item, realm):
                        matching.append(item)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("recent_usage read failed: %s" % exc)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_rows = matching[start_idx:end_idx]
    # issue #129 后续：每行带上「积分三段折算」（非缓存输入 / 缓存输入 / 输出），
    # 让面板单行就能看出成本落在哪一段；'billable=false' 的行是名义成本，
    # 前端会换样式标注，避免被当成真实扣费。
    for _row in page_rows:
        if not isinstance(_row, dict) or _row.get("error"):
            continue
        try:
            _row["credit_split"] = split_credit(_row.get("prompt_tokens"),
                                                _row.get("cached_tokens"),
                                                _row.get("completion_tokens"))
        except Exception:
            pass
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "rows": page_rows,
    }


POOL = None
# issue #43：使用驱动的余额刷新器（main() 里创建；单测/CLI 场景保持 None）
CREDITS_USAGE_REFRESHER = None
SCHEDULER = None
ACCOUNTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts")
REALM_STATE_FILE = os.path.join(ACCOUNTS_DIR, "active_realm.json")


def load_persisted_realm():
    global CURRENT_REALM
    if os.path.isfile(REALM_STATE_FILE):
        try:
            with open(REALM_STATE_FILE, "r", encoding="utf-8") as fh:
                d = json.load(fh)
                r = d.get("realm")
                if r in ("intl", "cn"):
                    CURRENT_REALM = r
                    return CURRENT_REALM
        except Exception as e:
            log("could not load active realm: %s" % e)
    return CURRENT_REALM


def save_persisted_realm(realm):
    global CURRENT_REALM
    if realm in ("intl", "cn"):
        CURRENT_REALM = realm
        try:
            state_dir = Path(ACCOUNTS_DIR).resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            state_file = state_dir / os.path.basename(REALM_STATE_FILE)
            if not state_file.is_relative_to(state_dir):
                raise ValueError("realm state path escapes base directory")
            state_file.write_text(
                json.dumps({"realm": realm, "updated_at": time.time(),
                            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")},
                           indent=2),
                encoding="utf-8")
            log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            log("failed to persist active realm: %s" % exc)
    return CURRENT_REALM


API_KEY = None
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


def _refreshable_accounts(accounts):
    """从账号集合里挑出**可以安全打上游**的号（#25 遗留其二）。

    判据与 pick/count_ready 的准入一致：enabled 且有凭证。禁用号 / 坏凭证号
    打上游不但没有意义，还可能被上游回一个**空账本**，把本地账本再零化一次
    （本仓那个 enabled=False 的 intl 号正是这个形态）。

    返回 (targets, skipped)：skipped 是 [{"uid", "reason"}]，调用方**必须**把它
    放进响应里 —— 否则「少刷新了几个号」会变成静默行为，排查时看不见。
    """
    targets, skipped = [], []
    for a in accounts or []:
        if a is None:
            continue
        if not a.enabled:
            skipped.append({"uid": a.uid, "reason": "disabled"})
            continue
        if not a.access_token:
            skipped.append({"uid": a.uid, "reason": "no_credential"})
            continue
        targets.append(a)
    return targets, skipped


def account_views(realm=None):
    """List view of every account, including a live readiness flag."""
    if not POOL:
        return []
    return POOL.list_public(realm=realm)


_CREDITS_SUMMARY_NOTE = ("registered values only; freshness depends on "
                         "check-in and consumption write-back")

# 按**解析后的窗口边界**分键：(since, until) -> (at, data)。week/month 相互重叠，
# 一个"范围标签"键会让一个窗口的数据冒充另一个（见 range_window 的注释）。
_byacct_cache = {}
_byacct_lock = threading.Lock()
# P1-3 收尾：按 key 聚合用**同形状**的缓存（同 key 口径：解析后的窗口边界）
_bykey_cache = {}
_bykey_lock = threading.Lock()


def credits_summary():
    """issue #21：只读积分汇总（**绝不触发上游查询**）。

    数据源是 accounts/*.json 里**已登记**的 credits（fetch_credits 写入 +
    每次消耗回写）；同宿主机轮询方（API Pool 面板）用它替代裸读目录文件。
    与 /accounts/credits 的区别：那个会逐号强刷上游，只适合手动点击 ——
    本函数一次网络调用都不发。

    防御：POOL 为 None / 列表为空 / 账号没有 credits（新号还没登过）/
    credits 结构残缺 —— 都只影响该行取值（None）与它是否计入汇总，
    绝不抛异常、绝不 500。
    """
    by_realm = {}
    rows = []
    total_remain = 0
    seen_orgs = set()        # 已计入过共享池的组织（跨账号去重，issue #67）
    unlimited_orgs = set()   # cap=-1 的组织共享池
    accounts = list(POOL.accounts) if POOL else []
    for a in accounts:
        cred = getattr(a, "credits", None)
        if not isinstance(cred, dict):
            cred = {}
        realm = getattr(a, "realm", "") or ""
        remain = cred.get("remain")
        shared = cred.get("shared") if isinstance(cred.get("shared"), dict) else None
        # issue #67：共享池是**组织级**资源，同一组织下每个账号的 quota 响应都会
        # 回显同一份；逐账号累加会把 1,000,000 乘成 4,000,000。这里按
        # organization_id 去重，没有组织标识的（个人号）才按账号算。
        org = str(getattr(a, "organization_id", "") or "")
        org_key = ("org", org) if org else ("uid", getattr(a, "uid", "") or "")
        unlimited = bool(shared and shared.get("unlimited"))
        plan_remain = remain if isinstance(remain, (int, float)) \
            and not isinstance(remain, bool) else 0
        shared_counted = 0
        if unlimited:
            unlimited_orgs.add(org_key)
        elif shared and shared.get("available", True):
            if org_key not in seen_orgs:
                seen_orgs.add(org_key)
                shared_counted = int(shared.get("remain") or 0)
        # 行内仍保留账号视角的 effective（套餐+共享），另给 counted 便于核对汇总
        eff = qoder_accounts.effective_remain(cred)
        rows.append({
            "uid": getattr(a, "uid", "") or "",
            "nickname": getattr(a, "nickname", "") or "",
            "realm": realm,
            "credits_remain": remain,
            "credits_remain_effective": eff,
            "credits_counted": int(plan_remain) + shared_counted,
            "credits_org": org,
            "credits_shared_unlimited": unlimited,
            "credits_shared_remain": (shared or {}).get("remain"),
            "credits_used": cred.get("used"),
            "credits_size": cred.get("size"),
            # 登记时间：credits.updated_at（fetch_credits 写入），不是文件 mtime。
            "registered_at": cred.get("updated_at"),
            # issue #141：额度构成（套餐内 / 资源包 / SOTA / 共享池）与取数端点，
            # 面板据此把「这些数字是怎么来的」摊开。
            "composition": cred.get("composition") or [],
            # 面板账号表用：四类小计（共享池单独标注，不参与个人合计）
            "composition_sums": {
                k: sum(int(b.get("remain") or 0)
                       for b in (cred.get("composition") or [])
                       if b.get("kind") == k)
                for k in ("plan", "pack", "sota", "shared")
            },
            "source": cred.get("source") or "",
            "expires_at": cred.get("expires_at"),
        })
        total = int(plan_remain) + shared_counted
        by_realm[realm] = by_realm.get(realm, 0) + total
        total_remain += total
    return {
        "by_realm": by_realm,
        "totals": {"remain": total_remain, "accounts": len(rows),
                   "unlimited_orgs": len(unlimited_orgs),
                   "shared_deduped": True},
        "accounts": rows,
        "note": _CREDITS_SUMMARY_NOTE,
    }


def usage_by_account(ttl=10, range=None, since=None, until=None):
    """Cached wrapper: full aggregation over the whole log is expensive."""
    lo, hi = range_window(range, since, until)
    now = time.time()
    key = (lo, hi)
    with _byacct_lock:
        hit = _byacct_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _usage_by_account_uncached(since=lo, until=hi)
    with _byacct_lock:
        _byacct_cache[key] = (time.time(), data)
    return data


def _usage_by_account_uncached(since=None, until=None):
    """Aggregate the JSONL log per account id（可选窗口过滤）。"""
    buckets = {}
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("error"):
                    continue
                if not _row_in_window(row, since, until):
                    continue
                key = row.get("account") or "(unattributed)"
                bucket = buckets.setdefault(key, {
                    "account": key, "requests": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "reasoning_tokens": 0,
                    "cached_tokens": 0, "total_tokens": 0, "models": {},
                })
                bucket["requests"] += 1
                for field in ("prompt_tokens", "completion_tokens",
                              "reasoning_tokens", "cached_tokens",
                              "total_tokens"):
                    bucket[field] += row.get(field) or 0
                model = row.get("model") or "?"
                bucket["models"][model] = bucket["models"].get(model, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage_by_account failed: %s" % exc)
    out = sorted(buckets.values(), key=lambda b: -b["total_tokens"])
    for item in out:
        item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
    return out


def usage_by_key(ttl=10, range=None, since=None, until=None):
    """Cached wrapper：与 usage_by_account 同缓存 key、同 TTL、同窗口解析。"""
    lo, hi = range_window(range, since, until)
    now = time.time()
    key = (lo, hi)
    with _bykey_lock:
        hit = _bykey_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _usage_by_key_uncached(since=lo, until=hi)
    with _bykey_lock:
        _bykey_cache[key] = (time.time(), data)
    return data


def _usage_by_key_uncached(since=None, until=None):
    """Aggregate the JSONL log per API key id（可选窗口过滤）。

    P1-3 收尾：与 _usage_by_account_uncached **逐行同判**——同一套
    _row_in_window 过滤、同一套「error 行跳过」规则、同样的字段与排序，
    只把分桶键从 account 换成 key_id。因此两个视图在同一 range 下：
        sum(by_key.total_tokens) == sum(by_account.total_tokens)
    （这条不变量可直接当作「同源」的回归断言。）

    注意：
      - 缺 key_id 的行（P1-3 字段上线前的历史行、未带 key 的面板会话）归入
        "(no-key)" 桶，**不静默丢弃**——丢弃会让「按 key 求和」小于总量；
      - 只读本地 usage.jsonl，**不触发任何上游调用**；
      - credit / original_credits 是 USAGE_FIELDS 的既有成员（by-account 目前
        只累 token），这里一并累上：同名数字字段的算法与 by-account 完全一致，
        属**字段超集**，不会让两个视图的数字对不上。
    """
    buckets = {}
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("error"):
                    continue
                if not _row_in_window(row, since, until):
                    continue
                key = row.get("key_id") or "(no-key)"
                bucket = buckets.setdefault(key, {
                    "key_id": key, "requests": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "reasoning_tokens": 0,
                    "cached_tokens": 0, "total_tokens": 0,
                    "credit": 0.0, "original_credits": 0.0, "models": {},
                })
                bucket["requests"] += 1
                for field in ("prompt_tokens", "completion_tokens",
                              "reasoning_tokens", "cached_tokens",
                              "total_tokens"):
                    bucket[field] += row.get(field) or 0
                bucket["credit"] += row.get("credit") or 0
                bucket["original_credits"] += row.get("original_credits") or 0
                model = row.get("model") or "?"
                bucket["models"][model] = bucket["models"].get(model, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage_by_key failed: %s" % exc)
    out = sorted(buckets.values(), key=lambda b: -b["total_tokens"])
    for item in out:
        item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
        item["credit"] = round(item["credit"], 6)
        item["original_credits"] = round(item["original_credits"], 6)
    return out


_series_cache = {}
_series_lock = threading.Lock()


def usage_timeseries(realm=None, range=None, since=None, until=None,
                     bucket_seconds=None, ttl=10):
    """分桶的 token/积分时序（面板时序图）。

    桶宽随窗口自适应：minute（span <= 6h）/ hour（<= 14d）/ day（其余），
    显式 bucket_seconds 覆盖（下限 60 秒）。计数口径与 KPI/模型表一致：
    非 error 行计入 requests 与各 token 字段，error 行计入 errors；credit 对
    所有行累加（与 _usage_snapshot_uncached 的 USAGE_FIELDS 口径相同）。
    最近 50 条 credit > 0 的请求随行返回，面板无需再打一个端点。

    缓存按**解析后的边界 + 桶宽**分键：week/month 相互重叠，一个"范围标签"
    无法描述两个窗口；桶宽不同即载荷不同（自动桶宽会随 span 翻转）。
    """
    r = realm or CURRENT_REALM
    lo, hi = range_window(range, since, until)
    pinned_lo, pinned_hi = lo, hi
    # 「显式给了窗口参数」与「完全没给」必须分开：
    #   · 没给（老面板/裸调用）→ 图表默认看最近 24 小时（改动前的既有行为）；
    #   · 给了 range=all（或 custom 但只给了一端）→ 该侧是**真的无界**，
    #     不能悄悄缩成 24 小时，否则 KPI(range=all=全量) 与序列(近 24h) 不自洽。
    explicit = not (range is None and since is None and until is None)
    if hi is None:
        hi = time.time()
    if lo is None:
        lo = 0 if explicit else hi - 86400
    span = max(1.0, hi - lo)
    if bucket_seconds:
        step = max(60, int(bucket_seconds))
    elif span <= 6 * 3600:
        step = 60
    elif span <= 14 * 86400:
        step = 3600
    else:
        step = 86400
    now = time.time()
    edge = "unbounded" if explicit else "auto"
    key = (r or "all", pinned_lo if pinned_lo is not None else edge,
           pinned_hi if pinned_hi is not None else edge, step)
    with _series_lock:
        hit = _series_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _usage_timeseries_uncached(r, lo, hi, step)
    with _series_lock:
        _series_cache[key] = (time.time(), data)
    return data


def _usage_timeseries_uncached(realm, lo, hi, step):
    """One uncached pass over the log, folding rows into fixed-width buckets."""
    buckets = {}
    credits = []
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if realm and not row_matches_realm(row, realm):
                    continue
                at = row.get("at")
                at = at if isinstance(at, (int, float)) else 0
                if at < lo or at > hi:
                    continue
                key = int((at - lo) // step)
                bucket = buckets.setdefault(key, {
                    "at": lo + key * step, "requests": 0, "errors": 0,
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "reasoning_tokens": 0, "cached_tokens": 0,
                    "total_tokens": 0, "credit": 0.0,
                })
                if row.get("error"):
                    bucket["errors"] += 1
                else:
                    bucket["requests"] += 1
                    for field in ("prompt_tokens", "completion_tokens",
                                  "reasoning_tokens", "cached_tokens",
                                  "total_tokens"):
                        bucket[field] += (row.get(field) or 0)
                credit = row.get("credit") or 0
                bucket["credit"] += credit
                if credit > 0:
                    credits.append({
                        "at": at, "iso": row.get("iso") or "",
                        "model": row.get("model") or "",
                        "account": row.get("account") or "",
                        "credit": credit,
                        "total_tokens": row.get("total_tokens") or 0,
                    })
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage timeseries read failed: %s" % exc)
    credits.sort(key=lambda item: item.get("at") or 0, reverse=True)
    return {
        "ok": True, "realm": realm or "all", "bucket_seconds": step,
        "since": lo, "until": hi,
        "series": [buckets[key] for key in sorted(buckets)],
        "credits": credits[:50],
    }


# ---------------------------------------------------------------------------
# 7 天 / 30 天 Token 用量图（日期 × 分组 两个维度）
# ---------------------------------------------------------------------------
# 与 /usage/timeseries 的关系：那边只有「日期」一个维度（分桶总量），这里多
# 一个分组维度（模型 / 区域 / Key / 账号），供堆叠柱状图用。两者口径一致
# （error 行计入 errors、token 字段与 credit 的累加方式相同）。
# 性能：日志是单进程顺序 append 的，所以从尾部倒读、遇到早于窗口的行即停——
# 30 天窗口不必从文件头扫起（日志实测可长到几十 MB/天）。
DAILY_MAX_DAYS = 90
DAILY_GROUPS = ("model", "realm", "key", "account")
_daily_cache = OrderedDict()
_daily_lock = threading.Lock()


def _iter_usage_rows_since(since, chunk=256 * 1024):
    """逆序产出 usage 行，遇到早于 since 的行即停止（生成器，内存 O(1)）。

    只在倒读终止时依赖时间有序性：单进程 append 保证同一时刻附近的行总在
    相邻块内，所以按 at 截断不会漏掉窗口内的行。
    """
    try:
        fh = open(USAGE_LOG, "rb")
    except FileNotFoundError:
        return
    try:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buf = b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            parts = buf.split(b"\n")
            buf = parts[0]
            for raw in reversed(parts[1:]):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                at = row.get("at")
                if not isinstance(at, (int, float)) or at < since:
                    return
                yield row
        raw = buf.strip()
        if raw:
            try:
                row = json.loads(raw)
            except Exception:
                return
            at = row.get("at")
            if isinstance(at, (int, float)) and at >= since:
                yield row
    except Exception as exc:
        log("daily usage tail read failed: %s" % exc)
    finally:
        fh.close()


def _daily_group_id(row, group):
    """一行归入哪个分组。缺失键不静默丢弃（与 by-key 视图同口径）。"""
    if group == "model":
        return row.get("model") or "(unknown)"
    if group == "realm":
        return row.get("realm") or "(unknown)"
    if group == "key":
        return row.get("key_id") or "(no-key)"
    return row.get("account") or "(unattributed)"


def usage_daily(days=7, group="model", realm=None, ttl=10):
    """Cached wrapper：面板轮询用，同一窗口+分组 10 秒内不重复扫日志。"""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(DAILY_MAX_DAYS, days))
    group = str(group or "model").strip().lower()
    if group not in DAILY_GROUPS:
        group = "model"
    since = _local_day_start(time.time())
    for _ in range(days - 1):
        # 逐个「减一天再取本地零点」而不是 once 减 (days-1)*86400：
        # 有夏令时的时区上按秒回推会得到 01:00 而不是 00:00，随后桶键与行的
        # 本地零点全 miss → 30 天窗口的前半段被静默丢掉（7 天档碰不到）。
        since = _local_day_start(since - 86400)
    key = (realm_scope(realm) or "all", days, group, since)
    now = time.time()
    with _daily_lock:
        hit = _daily_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            # LRU recency must not extend the data freshness deadline.
            _daily_cache.move_to_end(key)
            return hit[1]
    data = _usage_daily_uncached(days, group, realm, since)
    with _daily_lock:
        if len(_daily_cache) >= 32 and key not in _daily_cache:
            # 只淘汰**最旧的单个**条目。原先这里是 clear()——清空会把其它
            # 仍在被轮询的 (realm, days, group) 组合一起作废，下一轮全部重算。
            _daily_cache.popitem(last=False)
        _daily_cache[key] = (time.time(), data)
    return data


def _iter_usage_rows_forward(since, chunk=256 * 1024):
    """顺序读整个日志，只产出 at >= since 的行（倒读失明时的兜底用）。

    record_usage 是顺序 append 的，所以正常情况永远走不到这里；只有当日志
    被外部重排、或系统时钟大幅回拨导致倒读第一行就早于 since 时才需要它。
    """
    try:
        with open(USAGE_LOG, "rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                at = row.get("at")
                if isinstance(at, (int, float)) and at >= since:
                    yield row
    except FileNotFoundError:
        return
    except Exception as exc:
        log("daily usage forward read failed: %s" % exc)


def _usage_log_size():
    try:
        return os.path.getsize(USAGE_LOG)
    except OSError:
        return 0


def _usage_log_head_ts(probe_bytes=4096):
    """读 usage 日志**第一条**记录的 at（只读开头几 KB）。

    用来区分「窗口内本来就没数据」与「倒读被乱序日志阻死」——倒读对这两种
    情况都产出 0 行，正确处置却相反（前者不该扫，后者必须扫）。正常日志是
    单进程顺序 append 的，头行 at 最小；头行落在窗口内却一行都倒读不到，
    只能是文件被重排过。
    """
    try:
        with open(USAGE_LOG, "rb") as fh:
            chunk = fh.read(probe_bytes)
    except OSError:
        return None
    for raw in chunk.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            at = json.loads(raw).get("at")
        except Exception:
            return None
        return at if isinstance(at, (int, float)) else None
    return None


def _usage_daily_uncached(days, group, realm, since):
    until = since + days * 86400
    series = []
    index = {}
    for i in range(days):
        # 同 since 的理由：索引用**重算出来的本地零点**，与 fold() 里的
        # _local_day_start(at) 同一口径；直接 since + i*86400 在 DST 时区会
        # 错位一小时，index.get() 全 miss。
        start = since
        for _ in range(i):
            # +1 天再 +12h：只加 86400 在秋季回拨那天会落在「次日 23:00」
            # （再取零点就退回去了），加半天余量能保证跨到下一自然日。
            start = _local_day_start(start + 86400 + 43200)
        lt = time.localtime(start)
        item = {
            "at": start,
            "day": time.strftime("%Y-%m-%d", lt),
            "label": time.strftime("%m-%d", lt),
            "requests": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "credit": 0.0,
            "groups": {},
        }
        series.append(item)
        index[start] = item
    groups = {}
    totals = {"requests": 0, "errors": 0, "total_tokens": 0, "credit": 0.0}
    day_cache = {}
    seen = [0]

    def fold(row):
        if realm and not row_matches_realm(row, realm):
            return
        at = row.get("at") or 0
        hour = int(at // 3600)
        day_start = day_cache.get(hour)
        if day_start is None:
            day_start = _local_day_start(at)
            day_cache[hour] = day_start
        item = index.get(day_start)
        if item is None:
            return
        is_err = bool(row.get("error"))
        if is_err:
            item["errors"] += 1
        else:
            item["requests"] += 1
        tokens = row.get("total_tokens") or 0
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                      "cached_tokens", "total_tokens"):
            item[field] += (row.get(field) or 0)
        credit = row.get("credit") or 0
        item["credit"] += credit
        gid = _daily_group_id(row, group)
        item["groups"][gid] = item["groups"].get(gid, 0) + tokens
        agg = groups.get(gid)
        if agg is None:
            agg = groups[gid] = {"id": gid, "requests": 0, "errors": 0,
                                 "total_tokens": 0, "credit": 0.0}
        if is_err:
            agg["errors"] += 1
        else:
            agg["requests"] += 1
        agg["total_tokens"] += tokens
        agg["credit"] += credit
        totals["total_tokens"] += tokens
        totals["credit"] += credit
        if is_err:
            totals["errors"] += 1
        else:
            totals["requests"] += 1

    bad_rows = [0]

    def fold_safe(row):
        # usage.jsonl 是历史追加的，_extract_usage 不做类型收敛（上游给什么写
        # 什么），一行 "total_tokens": "123" 就足以让 += 抛 TypeError。裸调会让
        # 整个端点永久 500（脏行通常在尾部，倒读天天先撞它）；而同口径的
        # _usage_timeseries_uncached 是 except 住降级的——这里对齐成「跳坏行」。
        try:
            fold(row)
        except Exception:
            bad_rows[0] += 1

    for row in _iter_usage_rows_since(since):
        seen[0] += 1
        fold_safe(row)
    if not seen[0]:
        # 倒读一行都没产出：要么「窗口内本来就无数据」（新装、隔很久没请求），
        # 要么「倒读被乱序日志/时钟回拨阻死」。两者在倒读侧都是 0 行，正确处置
        # 却完全相反。用**文件头第一条 at** 区分：正常日志从早到晚递增，头行
        # 早于窗口起点就是合法空窗口，不必扫；头行落在窗口内却一行都读不到，
        # 只能是文件被重排过，值得付一次顺序全扫。
        # （曾用「文件小于 8MB 才兜底」的体积门限，那恰好只在不需要兜底的小
        #   日志上生效，在大日志上留下静默空图。）
        head = _usage_log_head_ts()
        if isinstance(head, (int, float)) and head >= since:
            log("daily usage: backward read went blind, falling back to full scan",
                level="WARN", tag="usage")
            for row in _iter_usage_rows_forward(since):
                fold_safe(row)
    if bad_rows[0]:
        log("daily usage: skipped %d inconsistent rows" % bad_rows[0],
            level="WARN", tag="usage")
    ordered = sorted(groups.values(), key=lambda g: -(g["total_tokens"] or 0))
    return {
        "ok": True,
        "realm": realm or "all",
        "days": days,
        "group_by": group,
        "bucket_seconds": 86400,
        "since": since,
        "until": until,
        "series": series,
        "groups": ordered,
        "totals": totals,
    }



# ---------------------------------------------------------------------------
# 统计概览（新版数据看板）：一次扫描产出 KPI / Top 榜 / 热力图 / 缓存 / 24h 曲线
# ---------------------------------------------------------------------------
# compute_usage_stats 的形参名是 range（与 /usage/* 的查询参数同名），
# 会遮蔽内建 range —— 在模块层先留一个别名，函数内一律用它。
_RNG = range

_PROVIDER_RULES = (
    ("claude", "Anthropic"), ("anthropic", "Anthropic"), ("haiku", "Anthropic"),
    ("sonnet", "Anthropic"), ("opus", "Anthropic"),
    ("gpt", "OpenAI"), ("openai", "OpenAI"), ("o1", "OpenAI"), ("o3", "OpenAI"),
    ("o4", "OpenAI"), ("davinci", "OpenAI"),
    ("gemini", "Google"), ("gemma", "Google"), ("palm", "Google"),
    ("qwen", "通义千问"), ("qwq", "通义千问"), ("tongyi", "通义千问"),
    ("deepseek", "DeepSeek"), ("dfmodel", "DeepSeek"), ("df-", "DeepSeek"),
    ("glm", "智谱 GLM"), ("chatglm", "智谱 GLM"),
    ("kimi", "月之暗面"), ("moonshot", "月之暗面"),
    ("llama", "Meta"), ("mistral", "Mistral"), ("mixtral", "Mistral"),
    ("grok", "xAI"), ("doubao", "豆包"), ("ernie", "文心一言"), ("spark", "讯飞星火"),
    ("hunyuan", "腾讯混元"), ("minimax", "MiniMax"), ("abab", "MiniMax"),
)


def provider_of(model):
    """模型 -> 上游厂商（面板「提供商」维度用；未知归「其他」）。"""
    m = str(model or "").lower()
    for key, name in _PROVIDER_RULES:
        if key in m:
            return name
    return "其他"


def _day_key(ts):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except Exception:
        return ""


# issue #129 后续：多少 prompt token 以上、且缓存为 0，算「全价请求」
FULL_PRICE_PROMPT_MIN = int(os.environ.get("QD_FULL_PRICE_PROMPT_MIN") or 100000)


def compute_usage_stats(range=None, since=None, until=None,
                        heatmap_days=365, top_n=5):
    """统计概览页所需的全部数据（单次扫描 usage.jsonl）。

    窗口语义与 /usage/analytics 一致（range_window）：today/week/month/custom/空=全部。
    热力图固定看最近 heatmap_days 个本地自然日，与窗口无关；窗口只影响 KPI/Top/趋势。
    """
    now = time.time()
    lo, hi = range_window(range, since, until)
    today_start = _local_midnight()
    heat_lo = _local_midnight(days_back=max(1, int(heatmap_days)) - 1)

    tot = {"requests": 0, "errors": 0, "tokens": 0,
           "prompt": 0, "completion": 0, "cached": 0}
    # issue #129 后续：报告者按小时拟合出的最大成本项是「缓存为 0 的超长 prompt」
    # （一天里 9 次这类请求就占了 30% 积分）。这里把这类请求单独计数并统计积分占比，
    # 让用户能直接看到「换前缀 / 新开会话」的代价。
    full_price = {"requests": 0, "prompt": 0, "credit": 0.0}
    total_credit = 0.0
    models = {}       # model -> {"tokens","requests"}
    accounts = {}     # account -> {"tokens","requests"}
    providers = {}    # provider -> {"tokens","requests"}
    heat = {}         # "YYYY-MM-DD" -> tokens
    act_days = set()  # 窗口内活跃日（本地日期）
    all_days = set()  # 全年活跃日（连续性计算用）
    cache_win = {"10m": [0, 0], "1h": [0, 0], "24h": [0, 0], "7d": [0, 0]}
    hour_cached = [0] * 24
    hour_prompt = [0] * 24
    hour_tokens = [0] * 24

    def _bump(bucket, key, tokens, reqs=1):
        cell = bucket.get(key)
        if cell is None:
            cell = bucket[key] = {"tokens": 0, "requests": 0}
        cell["tokens"] += tokens
        cell["requests"] += reqs

    if os.path.exists(USAGE_LOG):
        try:
            with open(USAGE_LOG, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    at = r.get("at") or 0
                    try:
                        at = float(at)
                    except (TypeError, ValueError):
                        at = 0.0
                    prompt = int(r.get("prompt_tokens") or 0)
                    cached = int(r.get("cached_tokens") or 0)
                    tokens = int(r.get("total_tokens") or 0)
                    is_err = bool(r.get("error"))
                    dkey = _day_key(at)
                    # 热力图与活跃日：全年范围（不按窗口过滤）
                    if at >= heat_lo and dkey:
                        if not is_err and tokens:
                            heat[dkey] = heat.get(dkey, 0) + tokens
                        all_days.add(dkey)
                    # 缓存三窗口：按「现在」回溯（与窗口无关）
                    if not is_err and prompt > 0:
                        for _k, _secs in (("10m", 600), ("1h", 3600),
                                          ("24h", 86400), ("7d", 604800)):
                            if at >= now - _secs:
                                cache_win[_k][0] += cached
                                cache_win[_k][1] += prompt
                    # 24 小时曲线：按本地整点
                    if at >= now - 86400 and not is_err:
                        hour = time.localtime(at).tm_hour
                        hour_cached[hour] += cached
                        hour_prompt[hour] += prompt
                        hour_tokens[hour] += tokens
                    if not _row_in_window(r, lo, hi):
                        continue
                    if dkey:
                        act_days.add(dkey)
                    if is_err:
                        tot["errors"] += 1
                        continue
                    tot["requests"] += 1
                    tot["tokens"] += tokens
                    tot["prompt"] += prompt
                    tot["completion"] += int(r.get("completion_tokens") or 0)
                    tot["cached"] += cached
                    _credit = r.get("credit") or 0
                    try:
                        _credit = float(_credit)
                    except (TypeError, ValueError):
                        _credit = 0.0
                    total_credit += _credit
                    # 缓存字段**缺失**的行（老模型不回报）不算「全价」——按全价估会偏高。
                    _cached_known = r.get("cached_reported")
                    if _cached_known is None:
                        _cached_known = True     # 历史行没有该字段：按旧口径计入
                    if (prompt >= FULL_PRICE_PROMPT_MIN and cached <= 0
                            and _cached_known):
                        full_price["requests"] += 1
                        full_price["prompt"] += prompt
                        full_price["credit"] += _credit
                    m = r.get("model") or "(unknown)"
                    _bump(models, m, tokens)
                    _bump(accounts, r.get("account") or "(unattributed)", tokens)
                    _bump(providers, provider_of(m), tokens)
        except Exception as exc:
            log("compute_usage_stats failed: %s" % exc, level="WARN", tag="usage")

    _nick = {}
    try:
        if POOL:
            for _a in POOL.accounts:
                _nick[_a.uid] = _a.nickname or _a.uid[:8]
    except Exception:
        _nick = {}

    def _rank(bucket, limit=None):
        rows = sorted(bucket.items(), key=lambda kv: kv[1]["tokens"], reverse=True)
        if limit:
            rows = rows[:limit]
        total = sum(v["tokens"] for _, v in rows) or 1
        return [{"name": _nick.get(k) or k, "key": k, "tokens": v["tokens"], "requests": v["requests"],
                 "share": round(v["tokens"] * 100.0 / total, 2)}
                for k, v in rows]

    # 连续天数：从今天（或昨天）往回数全年活跃日
    streak = 0
    if all_days:
        probe = time.localtime()
        # 今天没有请求时从昨天开始数（今天还没过完，不该中断）
        if _day_key(now) not in all_days:
            probe = time.localtime(now - 86400)
        while True:
            d = time.strftime("%Y-%m-%d", probe)
            if d not in all_days:
                break
            streak += 1
            probe = time.localtime(time.mktime(probe[:3] + (0, 0, 0, 0, 0, -1)) - 86400)

    top_models = _rank(models)
    hours = []
    for i in _RNG(23, -1, -1):
        ts = now - i * 3600
        h = time.localtime(ts).tm_hour
        prompt = hour_prompt[h]
        cached = hour_cached[h]
        hours.append({
            "ts": int(ts),
            "label": time.strftime("%H:00", time.localtime(ts)),
            "pct": (round(cached * 100.0 / prompt, 2) if prompt else None),
            "tokens": hour_tokens[h],
        })

    cache_rows = []
    for _k, _label in (("10m", "近 10 分钟"), ("1h", "近 1 小时"),
                       ("24h", "近 24 小时"), ("7d", "近 7 天")):
        hit, inp = cache_win[_k]
        cache_rows.append({"key": _k, "label": _label, "hit": hit, "input": inp,
                           "pct": (round(hit * 100.0 / inp, 1) if inp else None)})

    heat_days = []
    start = time.localtime(heat_lo)
    cursor = time.mktime((start.tm_year, start.tm_mon, start.tm_mday,
                          0, 0, 0, 0, 0, -1))
    while cursor <= now:
        d = _day_key(cursor)
        heat_days.append({"date": d, "tokens": heat.get(d, 0)})
        cursor += 86400

    return {
        "window": {"from": lo, "to": hi, "range": str(range or "all")},
        "kpi": {
            "requests": tot["requests"],
            "success": tot["requests"],
            "errors": tot["errors"],
            "success_pct": (round(tot["requests"] * 100.0 /
                                  (tot["requests"] + tot["errors"]), 2)
                            if (tot["requests"] + tot["errors"]) else None),
            "tokens": tot["tokens"],
            "prompt": tot["prompt"],
            "completion": tot["completion"],
            "cached": tot["cached"],
            "active_days": len(act_days),
            "streak_days": streak,
            "top_model": (top_models[0] if top_models else None),
        },
        "top_accounts": _rank(accounts, top_n),
        "top_providers": _rank(providers, top_n),
        "models": _rank(models, 10),
        "providers": _rank(providers, 10),
        "cache": {"windows": cache_rows},
        "full_price": {
            "requests": full_price["requests"],
            "prompt": full_price["prompt"],
            "credit": round(full_price["credit"], 2),
            "credit_share": (round(full_price["credit"] * 100.0 / total_credit, 1)
                             if total_credit > 0 else None),
            "threshold": FULL_PRICE_PROMPT_MIN,
        },
        "hourly": {"points": hours},
        "heatmap": {"days": heat_days,
                     "max": max([d["tokens"] for d in heat_days] or [0])},
    }

def compute_usage_analytics(range=None, since=None, until=None):
    """Detailed analytics for Token, Cache, and Reasoning metrics page.

    range/since/until 为可选时间窗口，语义见 range_window()。**不传时是 legacy
    模式：不新增任何键、不过滤任何行**，输出与改动前逐字节一致（老面板不受影响）；
    传了才追加顶层 window 边界、summary.window 桶与每账号/每模型的 window 桶。
    """
    now = time.localtime()
    today_ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    lo, hi = range_window(range, since, until)
    legacy = (not str(range or "").strip() and since is None and until is None)

    def new_stat():
        return {
            "requests": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
            "ttft_sum": 0.0, "ttft_n": 0,
            "speed_sum": 0.0, "speed_n": 0,
            "elapsed_sum": 0.0, "elapsed_n": 0,
        }

    all_summary = new_stat()
    today_summary = new_stat()
    # 窗口桶：legacy 模式下不进入输出，但循环结构保持统一（避免复制一份过滤逻辑）
    window_summary = new_stat()
    win_acct = {}       # uid -> {"stat": stat, "models": {model: {...}}}
    win_model = {}      # model id -> stat
    acct_map = {}
    model_map = {}
    if os.path.exists(USAGE_LOG):
        try:
            with open(USAGE_LOG, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    is_err = bool(r.get("error"))
                    at = r.get("at", 0)
                    is_today = (at >= today_ts)
                    in_window = _row_in_window(r, lo, hi)
                    acct_uid = r.get("account") or "(unattributed)"
                    m_id = r.get("model") or "(unknown)"

                    def feed(stat_obj, is_error):
                        if is_error:
                            stat_obj["errors"] += 1
                        else:
                            stat_obj["requests"] += 1
                            stat_obj["prompt_tokens"] += (r.get("prompt_tokens") or 0)
                            stat_obj["completion_tokens"] += (r.get("completion_tokens") or 0)
                            stat_obj["reasoning_tokens"] += (r.get("reasoning_tokens") or 0)
                            stat_obj["cached_tokens"] += (r.get("cached_tokens") or 0)
                            stat_obj["total_tokens"] += (r.get("total_tokens") or 0)
                            if r.get("ttft_ms"):
                                stat_obj["ttft_sum"] += r["ttft_ms"]
                                stat_obj["ttft_n"] += 1
                            if r.get("tokens_per_sec"):
                                stat_obj["speed_sum"] += r["tokens_per_sec"]
                                stat_obj["speed_n"] += 1
                            if r.get("elapsed_ms"):
                                stat_obj["elapsed_sum"] += r["elapsed_ms"]
                                stat_obj["elapsed_n"] += 1

                    feed(all_summary, is_err)
                    if is_today:
                        feed(today_summary, is_err)
                    if in_window:
                        feed(window_summary, is_err)
                    if acct_uid not in acct_map:
                        acct_map[acct_uid] = {
                            "uid": acct_uid,
                            "nickname": acct_uid,
                            "realm": r.get("realm", ""),
                            "domain": "",
                            "today": new_stat(),
                            "all_time": new_stat(),
                            "today_models": {},
                            "all_models": {},
                        }
                    feed(acct_map[acct_uid]["all_time"], is_err)
                    if is_today:
                        feed(acct_map[acct_uid]["today"], is_err)
                    if in_window:
                        slot = win_acct.setdefault(
                            acct_uid, {"stat": new_stat(), "models": {}})
                        feed(slot["stat"], is_err)
                        if not is_err:
                            wm = slot["models"].setdefault(
                                m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                            wm["requests"] += 1
                            wm["tokens"] += (r.get("total_tokens") or 0)
                            wm["reasoning"] += (r.get("reasoning_tokens") or 0)
                    if not is_err:
                        tm = acct_map[acct_uid]["all_models"].setdefault(
                            m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                        tm["requests"] += 1
                        tm["tokens"] += (r.get("total_tokens") or 0)
                        tm["reasoning"] += (r.get("reasoning_tokens") or 0)
                        if is_today:
                            tdm = acct_map[acct_uid]["today_models"].setdefault(
                                m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                            tdm["requests"] += 1
                            tdm["tokens"] += (r.get("total_tokens") or 0)
                            tdm["reasoning"] += (r.get("reasoning_tokens") or 0)
                    if m_id not in model_map:
                        model_map[m_id] = {"model": m_id, "today": new_stat(),
                                           "all_time": new_stat()}
                    feed(model_map[m_id]["all_time"], is_err)
                    if is_today:
                        feed(model_map[m_id]["today"], is_err)
                    if in_window:
                        feed(win_model.setdefault(m_id, new_stat()), is_err)
        except Exception as exc:
            log("compute_usage_analytics failed: %s" % exc)
    if POOL:
        for a in POOL.accounts:
            if a.uid in acct_map:
                acct_map[a.uid]["nickname"] = a.nickname
                acct_map[a.uid]["realm"] = a.realm
                acct_map[a.uid]["domain"] = a.domain
                acct_map[a.uid]["credits"] = getattr(a, "credits", None) or {}
            else:
                acct_map[a.uid] = {
                    "uid": a.uid,
                    "nickname": a.nickname,
                    "realm": a.realm,
                    "domain": a.domain,
                    "credits": getattr(a, "credits", None) or {},
                    "today": new_stat(),
                    "all_time": new_stat(),
                    "today_models": {},
                    "all_models": {},
                }

    def finalize(stat_obj):
        p = stat_obj["prompt_tokens"]
        c = stat_obj["cached_tokens"]
        out = stat_obj["completion_tokens"]
        reas = stat_obj["reasoning_tokens"]
        stat_obj["cache_hit_pct"] = round((c / p * 100), 1) if p > 0 else 0.0
        stat_obj["reasoning_ratio"] = round((reas / out * 100), 1) if out > 0 else 0.0
        stat_obj["ttft_ms_avg"] = round(stat_obj["ttft_sum"] / stat_obj["ttft_n"]) \
            if stat_obj["ttft_n"] > 0 else 0
        stat_obj["speed_avg"] = round(stat_obj["speed_sum"] / stat_obj["speed_n"], 1) \
            if stat_obj["speed_n"] > 0 else 0.0
        stat_obj["elapsed_ms_avg"] = round(stat_obj["elapsed_sum"] / stat_obj["elapsed_n"]) \
            if stat_obj["elapsed_n"] > 0 else 0
        return stat_obj

    finalize(all_summary)
    finalize(today_summary)
    for a in acct_map.values():
        finalize(a["today"])
        finalize(a["all_time"])
    for m in model_map.values():
        finalize(m["today"])
        finalize(m["all_time"])
    if not legacy:
        finalize(window_summary)
        for slot in win_acct.values():
            finalize(slot["stat"])
        for stat in win_model.values():
            finalize(stat)
    accts_list = sorted(acct_map.values(),
                        key=lambda a: (-a["today"]["total_tokens"],
                                       -a["all_time"]["total_tokens"]))
    models_list = sorted(model_map.values(),
                         key=lambda m: (-m["today"]["total_tokens"],
                                        -m["all_time"]["total_tokens"]))
    result = {
        "today_ts": today_ts,
        "summary": {"today": today_summary, "all_time": all_summary},
        "accounts": accts_list,
        "models": models_list,
    }
    if not legacy:
        # 只在实际传了窗口时追加这三个桶；legacy 输出逐字节不变（老面板零影响）。
        # 排序仍沿用 today/all_time（面板可自行按 window 重排），避免改变既有顺序。
        result["window"] = {"since": lo, "until": hi}
        result["summary"]["window"] = window_summary
        for a in accts_list:
            slot = win_acct.get(a["uid"])
            a["window"] = slot["stat"] if slot else finalize(new_stat())
            a["window_models"] = slot["models"] if slot else {}
        for m in models_list:
            m["window"] = win_model.get(m["model"]) or finalize(new_stat())
    return result


_USAGE_REPORT_CACHE = qoder_cache.JsonResultCache()


def _usage_log_fingerprint():
    """Detect append, truncation, replacement and file-path changes without scanning."""
    path = os.path.abspath(USAGE_LOG)
    try:
        st = os.stat(path)
        return (path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError:
        return (path, None)


def cached_usage_report(kind, ttl=10, **params):
    """Bound dashboard scans; invalidate immediately when the source changes.

    Raw compute_* functions remain available for exact diagnostics and tests.
    Expiry never slides on hits. Concurrent identical polls share one scan.
    """
    builder = {"stats": compute_usage_stats, "analytics": compute_usage_analytics}[kind]
    fingerprint = _usage_log_fingerprint()
    # Account names/credit snapshots are part of analytics, not part of JSONL.
    metadata = []
    for account in (POOL.accounts if POOL else []):
        row = [account.uid, account.nickname, account.realm]
        if kind == "analytics":
            row.extend([account.domain, getattr(account, "credits", None)])
        metadata.append(row)
    meta = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    day = time.strftime("%Y-%m-%d", time.localtime())
    key = (kind, tuple(sorted(params.items())), fingerprint, day,
           os.environ.get("TZ", ""), time.tzname, meta, FULL_PRICE_PROMPT_MIN)
    return _USAGE_REPORT_CACHE.get(
        key, lambda: builder(**params), ttl=ttl,
        valid=lambda: _usage_log_fingerprint() == fingerprint)


def free_model_names(realm=None):
    """目录里 is_free 的模型名集合（含上游 key 与别名）。

    与护栏阈值**同批**推给池（R3）：free 名单为空集时 credit 守卫会拦住
    全部模型，所以只要启用了 daily_credit_limit，名单就必须一起推。
    只读本地目录快照，不联网；失败只记 WARN 并返回已收集部分。
    """
    names = set()
    try:
        for mid, meta in fetch_models(realm=realm or CURRENT_REALM):
            meta = meta if isinstance(meta, dict) else {}
            if not meta.get("is_free"):
                continue
            vals = [mid, meta.get("key"), meta.get("alias"),
                    meta.get("display_name")]
            extra = meta.get("aliases")
            if isinstance(extra, (list, tuple, set)):
                vals.extend(extra)
            for cand in vals:
                if isinstance(cand, str) and cand.strip():
                    names.add(cand.strip())
    except Exception as exc:
        log("free model list probe failed: %s" % exc, level="WARN")
    return names


def gateway_models_for_agents(realm=None):
    """面板「智能体配置」页的模型预填清单（与 /v1/models 同源裁剪）。"""
    try:
        entries = ([(mid, meta, realm) for mid, meta in fetch_models(realm=realm)]
                   if realm else fetch_models_merged())
    except Exception:
        entries = []
    out = []
    for row in entries if isinstance(entries, list) else []:
        # fetch_models* 返回的是 (mid, meta, realm) 元组，不是 dict；
        # 与 /v1/models 同样经 model_entry 展开成带 id / context_length 的字典。
        try:
            e = model_entry(row[0], row[1], realm=row[2])
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        mid = str(e.get("id") or "").strip()
        if not mid:
            continue
        item = {"id": mid}
        try:
            if e.get("context_length"):
                item["context_window"] = int(e["context_length"])
        except (TypeError, ValueError):
            pass
        try:
            if e.get("max_output_tokens"):
                item["max_output"] = int(e["max_output_tokens"])
        except (TypeError, ValueError):
            pass
        out.append(item)
    return {"models": out[:80]}


def runtime_settings_view():
    """Current panel-visible settings (never returns the password or the key)."""
    key = API_KEY or ""
    if len(key) > 8:
        masked = key[:4] + "*" * 6 + key[-4:]
    else:
        masked = "*" * len(key)
    keys = []
    for entry in configured_keys():
        raw = entry.get("key") or ""
        keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "enabled": entry.get("enabled", True) is not False,
            "masked": (raw[:4] + "*" * 6 + raw[-4:]) if len(raw) > 8
            else "*" * len(raw),
            "source": entry.get("source") or "panel",
            "created_at": entry.get("created_at") or "",
            # issue #131-1：白名单与备注必须回显 —— 面板「读 → 改 → 存」是一条
            # 全量覆盖链，视图里没有的字段会被前端当成空值写回去（等于放大授权）。
            "models": list(entry.get("models") or []),
            "note": entry.get("note") or "",
            "account_ids": list(entry.get("account_ids") or []),
            "account_groups": list(entry.get("account_groups") or []),
        })
    return {
        "panel_password_is_default":
            qoder_settings.panel_password_is_default(ACCOUNTS_DIR),
        "api_key_set": bool(key),
        "api_key_set_by_panel": API_KEY_FILE_SET,
        "api_key_masked": masked,
        "auth_required": auth_required(),
        "api_keys": keys,
        # limits 与保存分支同源：limits_data 返回**完整形状**（每个守卫都补齐
        # global/intl/cn，未设时全为 0/None），面板读到即可安全回显、再保存不会
        # 把已有阈值覆盖成默认值。
        "limits": qoder_settings.limits_data(ACCOUNTS_DIR),
        # 本地 web 工具开关（默认关）：这是唯一由模型驱动出网的能力
        "web_tools": web_tools_enabled(),
        # issue #135：工具协议残片收尾策略（面板可切换，立即生效）
        "tool_echo_mode": tool_echo_mode(),
        "tool_echo_mode_env": (os.environ.get("QD_TOOL_ECHO_MODE") or "").strip().lower(),
        # issue #146：Anthropic thinking block 输出策略
        "anthropic_thinking": anthropic_thinking_mode(),
        "anthropic_thinking_env": (os.environ.get("QD_ANTHROPIC_THINKING") or "").strip().lower(),
        # issue #106：各模型上下文档（面板回显 + 客户端可查）
        "model_context": _model_context_snapshot(),
        "web_tools_rounds": WEB_TOOLS_MAX_ROUNDS,
        "accounts_dir": ACCOUNTS_DIR,
        "usage_dir": USAGE_DIR,
        "log_dir": LOG_DIR,
        "settings_file": qoder_settings.settings_path(ACCOUNTS_DIR),
        "version": VERSION,
    }


def current_account():
    """Account used for display purposes (health / usage summaries)."""
    return POOL.representative() if POOL else None


# ---------------------------------------------------------------------------
# 前缀会话亲和（同一对话固定同一账号，命中上游按账号的 prompt 缓存）
# ---------------------------------------------------------------------------
AFFINITY_BY_PREFIX = os.environ.get("QD_AFFINITY_BY_PREFIX", "1").lower() not in (
    "0", "false", "no", "off")
AFFINITY_DEBUG = os.environ.get("QD_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")
# P1-6 会话亲和长度上限：对话超过该条数就不再绑定账号（长对话的请求体只会越来越大，
# 钉在同一账号等于让下一轮同样超长）。0 = 关闭上限，**默认关闭**，行为与改动前一致。
# 阈值不照搬参考项目：那张断连率表来自其单一实例的 333 个请求、上游也不同，本仓无
# 自有曲线，因此先给机制、等实测再定值。非数字取值按 0 处理（不在 import 期抛异常）。
try:
    AFFINITY_MAX_MSGS = int(os.environ.get("QD_AFFINITY_MAX_MSGS", "0") or 0)
except (TypeError, ValueError):
    AFFINITY_MAX_MSGS = 0


_PREFIX_SESSION_CACHE = None
_PREFIX_SESSION_CACHE_CONFIG = None
_PREFIX_SESSION_CACHE_LOCK = threading.Lock()


def prefix_session_cache():
    """Select an opt-in memory/Redis session map; no backend means disabled."""
    global _PREFIX_SESSION_CACHE, _PREFIX_SESSION_CACHE_CONFIG
    url = (os.environ.get("QD_SESSION_REDIS_URL") or "").strip()
    mode = (os.environ.get("QD_SESSION_CACHE") or
            ("redis" if url else "off")).strip().lower()
    if mode == "off":
        return None
    if mode not in ("memory", "redis"):
        raise ValueError("QD_SESSION_CACHE must be off, memory or redis")
    ttl = (os.environ.get("QD_SESSION_CACHE_TTL")
           or os.environ.get("QD_SESSION_REDIS_TTL") or 3600)
    timeout = os.environ.get("QD_SESSION_REDIS_TIMEOUT") or 0.4
    max_entries = os.environ.get("QD_SESSION_CACHE_MAX_ENTRIES") or 4096
    config = (mode, url, ttl, timeout, max_entries)
    if _PREFIX_SESSION_CACHE is not None and config == _PREFIX_SESSION_CACHE_CONFIG:
        return _PREFIX_SESSION_CACHE
    with _PREFIX_SESSION_CACHE_LOCK:
        if _PREFIX_SESSION_CACHE is None or config != _PREFIX_SESSION_CACHE_CONFIG:
            secret = qoder_settings.panel_session_secret(ACCOUNTS_DIR)
            if mode == "memory":
                cache = qoder_session_cache.MemorySessionCache(
                    ttl=ttl, max_entries=max_entries, hash_secret=secret)
            else:
                cache = qoder_session_cache.PrefixSessionCache(
                    url, ttl=ttl, timeout=timeout, hash_secret=secret)
            _PREFIX_SESSION_CACHE = cache
            _PREFIX_SESSION_CACHE_CONFIG = config
    return _PREFIX_SESSION_CACHE


def assign_prefix_session(body, account, model_key, client_session_key=None,
                          usage_ctx=None, fallback_session_id=None):
    """Choose a cache-affine ID only within the selected upstream account."""
    # Always reset: a preceding failed account may already have acquired an ID.
    # issue #128：没有前缀缓存命中时用**会话级** id 兜底，保证同一对话的
    # session_id 稳定（上游用量归组用），而不是每请求一个新 uuid。
    body["session_id"] = fallback_session_id or str(uuid.uuid4())
    if usage_ctx is not None:
        usage_ctx["session_affinity"] = "random"
    try:
        cache = prefix_session_cache()
        if cache is not None:
            matched = cache.resolve(
                body.get("messages"), account.uid, account.realm, model_key,
                client_session_key=client_session_key,
                tenant_scope=(usage_ctx or {}).get("tenant_scope", ""),
                context={"tools": body.get("tools"),
                         "parameters": body.get("parameters"),
                         "agent_id": body.get("agent_id")})
            if matched:
                body["session_id"] = matched
                if usage_ctx is not None:
                    usage_ctx["session_affinity"] = cache.backend + "-prefix"
    except Exception as exc:
        # Optional optimization: cache failure must not break inference or
        # log a connection URL, password, message, or raw exception details.
        log("prefix session cache unavailable (%s); random session used"
            % type(exc).__name__, level="WARN", tag="chat")
    if usage_ctx is not None:
        usage_ctx["upstream_session_id"] = body["session_id"]


def derive_affinity_key(messages):
    """Derive a stable affinity key from a conversation's stable prefix.

    前两条消息（system + 首轮 user）在整段对话生命周期内不变，因此同一对话
    每一轮都落到同一上游账号 —— 正是 prompt 缓存需要的；不同对话首轮不同，
    依旧分散到各账号，负载均衡不受影响。
    """
    if not AFFINITY_BY_PREFIX:
        return None
    try:
        msgs = messages or []
        if not msgs:
            return None
        # P1-6：超长对话放弃亲和（== 上限仍绑定，> 上限释放）
        if AFFINITY_MAX_MSGS > 0 and len(msgs) > AFFINITY_MAX_MSGS:
            if AFFINITY_DEBUG:
                log("affinity: skip %d msgs (> %d), letting the pool rotate"
                    % (len(msgs), AFFINITY_MAX_MSGS))
            return None
        # issue #136：只用首条 user 消息做亲和键。system 里常带每轮变化的运行时
        # 上下文，一旦把它算进去，长会话的亲和键会逐轮变化 -> 账号轮换 ->
        # 上游缓存整段丢失（面板上就是 98% ↔ 0% 来回跳）。
        head = [m for m in msgs
                if isinstance(m, dict) and str(m.get("role")) == "user"][:1]
        if not head:
            head = msgs[:1]
        blob = json.dumps(head, ensure_ascii=False,
                          sort_keys=True).encode("utf-8")
        return "pfx-" + hashlib.sha256(blob).hexdigest()[:16]
    except Exception:
        return None


def _anthropic_sys_src(payload):
    """Anthropic 入口的 system 来源标记（issue #55，写进 usage 行）。"""
    has_top = bool(str((payload or {}).get("system") or "").strip())
    n_msg = qoder_anthropic.count_msg_system(payload)
    mode = qoder_anthropic.msg_system_mode()
    if mode == "merge" and n_msg:
        return "top+msg" if has_top else "msg"
    if mode == "inline" and n_msg:
        return "top+inline" if has_top else "inline"
    return "top" if has_top else "none"


def prompt_fingerprint(messages, sys_src=None, n_msg_system=None):
    """Privacy-safe fingerprint of the outgoing prompt.

    Cache hits need a byte-identical prefix, so these hashes answer "is my
    prefix stable / is my conversation continuous?" without storing any text.
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False,
                              sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        # issue #55：把 system 的来源与条数写进指纹——「顶层 system 稳定、
        # messages 内 system 每轮都在变」这类问题不必再翻代码就能定位。
        if sys_src is not None:
            out["sys_src"] = str(sys_src)
        if n_msg_system is not None:
            out["n_msg_system"] = int(n_msg_system)
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}


LOG_BUFFER = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0


def add_log_entry(msg, level=None, tag=None):
    global _LOG_COUNTER
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t_short = time.strftime("%H:%M:%S")
    msg_str = str(msg).rstrip()
    if not level:
        lower = msg_str.lower()
        if any(k in lower for k in ("error", "exception", "failed", "traceback",
                                    "errno", "fatal", "token_expire", "12153")):
            level = "ERROR"
        elif any(k in lower for k in ("warn", "warning", "retry", "timeout")):
            level = "WARN"
        else:
            level = "INFO"
    if not tag:
        lower = msg_str.lower()
        if "chat:" in lower or "chat done" in lower or "/v1/chat" in lower \
                or "/chat/completions" in lower or "responses" in lower:
            tag = "chat"
        elif "scheduler" in lower or "调度器" in lower:
            tag = "scheduler"
        elif "task" in lower or "任务" in lower or "签到" in lower \
                or "checkin" in lower or "福利" in lower:
            tag = "tasks"
        elif "account" in lower or "账号" in lower or "pool" in lower \
                or "imported" in lower:
            tag = "accounts"
        elif "model" in lower or "catalog" in lower or "模型" in lower:
            tag = "catalog"
        elif "auth" in lower or "token" in lower or "oauth" in lower:
            tag = "auth"
        elif "settings" in lower or "设置" in lower:
            tag = "settings"
        else:
            tag = "system"
    with _LOG_LOCK:
        _LOG_COUNTER += 1
        entry = {
            "id": _LOG_COUNTER,
            "ts": ts,
            "time": t_short,
            "level": level,
            "tag": tag,
            "msg": msg_str,
        }
        LOG_BUFFER.append(entry)
    return entry


def log(msg, level=None, tag=None):
    sys.stderr.write("[qd-proxy] %s %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()
    add_log_entry(msg, level=level, tag=tag)



# issue #116：把宿主日志注入 anthropic 模块（它只 import 标准库，无法反向 import 本模块）
qoder_anthropic._bind_log(log)

def get_logs(limit=200, level="", tag="", search="", since_id=0):
    with _LOG_LOCK:
        items = list(LOG_BUFFER)
    if since_id > 0:
        items = [x for x in items if x["id"] > since_id]
    if level:
        items = [x for x in items if x["level"] == level.upper()]
    if tag:
        items = [x for x in items if x["tag"].lower() == tag.lower()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x["msg"].lower() or s in x["tag"].lower()]
    total = len(items)
    if limit and limit > 0 and since_id == 0:
        items = items[-limit:]
    max_id = items[-1]["id"] if items else since_id
    return {"total": total, "logs": items, "max_id": max_id}


def clear_logs():
    with _LOG_LOCK:
        LOG_BUFFER.clear()


# ---------------------------------------------------------------------------
# 模型目录（动态 COSY model/list + 静态兜底）
# ---------------------------------------------------------------------------
NON_CHAT_MODELS = {"lite"}
NON_CHAT_PREFIXES = ("codewise-", "completion-")
NON_CHAT_SUFFIXES = ("-image-alpha", "-image-alpha-edit", "-taco-completion")


def is_chat_model(mid):
    if not mid:
        return False
    if mid in NON_CHAT_MODELS:
        return False
    if mid.startswith(NON_CHAT_PREFIXES):
        return False
    if mid.endswith(NON_CHAT_SUFFIXES):
        return False
    return True


CN_UI_ORDER = [m["key"] for m in qoder_catalog.STATIC_CN_MODELS]
INTL_UI_ORDER = [m["key"] for m in qoder_catalog.STATIC_INTL_MODELS]


def merge_catalog(primary, realm=None):
    """静态目录打底（补元数据），按官方 UI 顺序输出。

    **清单以 primary（动态接口或本机官方目录）为准**：桌面版此刻显示什么
    这里就显示什么（如动态返回 15 条就不额外塞静态独有的 2 条）；
    primary 为空时回退静态快照全量。enable/strategies 等状态不做任何裁剪。
    """
    r = realm or CURRENT_REALM
    merged = {}
    source_static = getattr(qoder_catalog, "STATIC_CN_MODELS" if r == "cn"
                            else "STATIC_INTL_MODELS", qoder_catalog.STATIC_MODELS)
    for item in source_static:
        mid = item.get("key") or item.get("id")
        if mid and is_chat_model(mid):
            merged[mid] = dict(item)
    primary = list(primary or [])
    primary_keys = None
    for mid, meta in primary:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            # None 值不覆盖：动态接口对部分条目返回 null（如 context_config/
            # thinking_config/description），静默 null 会把静态快照里的真实
            # 值抹掉——只用有值字段做增量覆盖。
            base.update({k: v for k, v in meta.items() if v is not None})
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    if primary:
        primary_keys = {mid for mid, _ in primary if is_chat_model(mid)}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    out = []
    seen = set()

    def wanted(mid):
        if not is_chat_model(mid):
            return False
        if primary_keys is not None and mid not in primary_keys:
            return False      # 静态独有、官方此刻未列出的条目不外塞
        return True

    for mid in order:
        if mid in merged and wanted(mid):
            out.append((mid, merged[mid]))
            seen.add(mid)
    for mid, meta in merged.items():
        if mid not in seen and wanted(mid):
            out.append((mid, meta))
    return out


LOCAL_CATALOG_VERSIONS = ("catalog-v7", "catalog-v6")


def local_catalog_files(home):
    """本机目录缓存文件候选：新版 v7 优先、老客户端 v6 兜底（issue #74 同款降级）。

    官方 1.1.81 起写 catalog-v7（格式与 v6 同为 QMC v1，只是场景更多）；仍存
    在只有 v6 的老安装，因此两个文件名都找，同一账号目录下 v7 优先。
    返回 [(path, uid)]，uid 是解密密钥派生用的子目录名。
    """
    out = []
    try:
        with open(os.path.join(home, "default"), encoding="utf-8") as fh:
            uid = str(json.load(fh).get("uid") or "")
    except Exception:
        # issue #74：容器 / 未装官方客户端时 ~/.qoder/.models/default 天然
        # 不存在，这是**正常降级**（最终回落到内嵌快照），不该刷 WARN。
        uid = ""
    try:
        subs = [uid] if uid and os.path.isdir(os.path.join(home, uid)) else \
            sorted(d for d in os.listdir(home) if os.path.isdir(os.path.join(home, d)))
    except Exception:
        return out
    for sub in subs:
        for fname in LOCAL_CATALOG_VERSIONS:
            path = os.path.join(home, sub, fname)
            if os.path.isfile(path):
                out.append((path, sub))
    return out


def read_local_models(realm=None):
    """读取本机官方客户端的模型目录缓存（QMC 解密），无网络也能跟官方对齐。

    路径：~/.qoder/.models/<uid>/catalog-v7（1.1.81+，v6 兜底）
    返回 [(key, meta)]；任何失败返回 []（回退静态快照）。
    """
    r = realm or CURRENT_REALM
    home = os.path.join(os.path.expanduser("~"),
                        get_realm_config(r)["home_dir"], ".models")
    try:
        from qoder_sign import qmc_decrypt
        for cat, sub in local_catalog_files(home):
            try:
                with open(cat, "rb") as fh:
                    blob = fh.read().decode("ascii", "replace")
                plain = json.loads(qmc_decrypt(blob, sub).decode("utf-8"))
            except Exception:
                continue
            chat = qoder_catalog.dialogue_models(plain)
            out = []
            for m in chat:
                if not isinstance(m, dict) or not m.get("key"):
                    continue
                # 官方本地目录条目全字段原样
                row = dict(m)
                row["id"] = m["key"]
                row.setdefault("name", m.get("display_name") or m["key"])
                row.setdefault("display_name", m.get("display_name") or m["key"])
                out.append((m["key"], row))
            if out:
                log("model catalog read from local client cache: %d models (%s)"
                    % (len(out), r))
                return out
    except Exception as exc:
        log("local model catalog read skipped: %s" % exc, level="DEBUG")
    return []


_MODELS_DISK_CACHE_TTL = 7 * 24 * 3600      # 7 天（动态失败时保底，issue #58）


def _models_cache_path(realm):
    """动态模型清单的磁盘缓存路径（issue #58）。"""
    return os.path.join(ACCOUNTS_DIR, "models-cache-%s.json" % (realm or "cn"))


def _save_models_cache(realm, live):
    """把**动态发现**的模型清单落盘（容器重启/网络抖动后仍能看到新模型）。"""
    try:
        rows = [{"key": mid, "meta": meta} for mid, meta in live]
        blob = {"at": time.time(), "realm": realm or "cn", "models": rows}
        path = _models_cache_path(realm)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log("models cache write failed: %s" % exc, level="WARN")


def _load_models_cache(realm):
    """读磁盘缓存；过期或损坏返回 []（issue #58）。"""
    try:
        path = _models_cache_path(realm)
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        if time.time() - float(blob.get("at") or 0) > _MODELS_DISK_CACHE_TTL:
            return []
        out = []
        for row in blob.get("models") or []:
            mid = row.get("key")
            if mid:
                out.append((mid, row.get("meta") or {}))
        return out
    except Exception:
        return []


def _register_live_names(entries):
    """把清单条目的 display_name -> key 注册进解析器（官方改显示名窗口期）。

    动态接口 / 本机目录缓存 / 落盘缓存里 display_name 永远比冻结快照新鲜；
    注册后 resolve_upstream_key 才认得 /v1/models 正在广告的新名字
    （如 kmodel_latest 改显示名 Kimi-K3 -> Kimi-K3.1-Pro，详见
    qoder_catalog.register_live_display_names 的注释）。失败静默：清单
    拉不出来时冻结别名表仍在，行为退回改动前。
    """
    try:
        pairs = []
        for mid, meta in entries or []:
            name = str((meta or {}).get("display_name")
                       or (meta or {}).get("name") or "").strip()
            if name and mid:
                pairs.append((name, mid))
        if pairs:
            qoder_catalog.register_live_display_names(pairs)
    except Exception:
        pass


_LIVE_NAMES_SYNC_AT = {}
_LIVE_NAMES_LOCK = threading.Lock()


def _ensure_live_names(realm=None):
    """确保动态清单的显示名已注册进解析器（每区域 300s 至多一次，失败静默）。

    聊天主链路的兜底：客户端可以不看 /v1/models 直接发模型名（配了固定模型
    的脚本/智能体），此时 fetch_models 可能一次都没跑过，解析器里没有动态
    显示名——第一次解析失败时在这里补注册。**只读离线源**（allow_network=
    False）：请求路径里出网拉 model/list 会多一次出站调用并触发 POOL.pick
    轮换账号（破坏 #86 钉死账号语义）；在线清单由启动预热线程与 /v1/models
    等常规路径负责刷新。
    """
    r = realm if realm in ("cn", "intl") else CURRENT_REALM
    with _LIVE_NAMES_LOCK:
        if time.time() - _LIVE_NAMES_SYNC_AT.get(r, 0.0) < 300:
            return
        _LIVE_NAMES_SYNC_AT[r] = time.time()
    try:
        _register_live_names(fetch_models(realm=r, allow_network=False))
    except Exception:
        # 失败不占节流窗口：离线源只是本地文件探测，让下一条请求能立即重试，
        # 冷启动竞态（预热线程还没跑完就来了第一条请求）不会白等 300s。
        with _LIVE_NAMES_LOCK:
            _LIVE_NAMES_SYNC_AT[r] = 0.0


def resolve_model_key(model, realm=None):
    """open_upstream 用的模型名解析：冻结表优先，未命中时同步动态名再试一次。

    返回原样字符串 = 真未知（BYOK 自定义模型等，透传语义保持不变）；
    官方改了显示名的情况（快照未跟上）由第二次解析兜住。
    """
    key = qoder_catalog.resolve_upstream_key(model, realm=realm)
    raw = str(model or "").strip()
    if key and key == raw and not qoder_catalog.is_known_upstream_key(key, realm):
        _ensure_live_names(realm)
        key = qoder_catalog.resolve_upstream_key(model, realm=realm)
    return key


# 离线合并结果的短 TTL memo（MAJOR：请求路径每请求一次磁盘 IO 的节流）。
# 离线模式不写 _models_cache（会挡住在线刷新），但没有自己的 memo 时，
# 降级环境（无账号 / 上游持续失败 / 容器重启后）每条请求都会重读
# models-cache JSON 甚至重解密本机目录缓存。
_OFFLINE_MODELS_TTL = 60.0
_offline_models_cache = {}      # realm -> (at, entries)，受 _lock 保护


def _offline_models_memo_get(r):
    hit = _offline_models_cache.get(r)
    if hit and time.time() - hit[0] < _OFFLINE_MODELS_TTL:
        return hit[1]
    return None


def fetch_models(realm=None, allow_network=True):
    """合并三级来源的模型清单（[(key, meta)]，官方 UI 顺序）。

    allow_network=False 供**请求路径**（resolve_model_key 的惰性同步、
    catalog_meta_for 的元数据查询）使用：只读离线源（内存缓存 / issue #58
    落盘缓存 / 本机官方目录缓存），不出网、不碰账号池（#86），且结果有
    60s memo——磁盘 IO 每分钟至多一次。请求路径里调 read_dynamic_models
    会多一次 model/list 出站调用（打乱 failover 序列）并触发 POOL.pick。
    """
    r = realm or CURRENT_REALM
    with _lock:
        c = _models_cache.get(r) or {"at": 0.0, "data": None}
        if c["data"] and time.time() - c["at"] < 300:
            return c["data"]
        if not allow_network:
            memo = _offline_models_memo_get(r)
            if memo is not None:
                return memo
    # 1) 动态接口（需要账号） 2) 本机官方目录缓存 3) 内嵌静态快照
    live = []
    if allow_network:
        live = read_dynamic_models(realm=r)
        if live:
            _save_models_cache(r, live)      # issue #58：成功的动态清单落盘
    if not live:
        # 过期的内存缓存也好过没有：display_name -> key 的映射几乎不会变旧
        # （官方改显示名的窗口期里，旧映射最多让新名字解析不到，不会解析错）
        with _lock:
            live = (_models_cache.get(r) or {}).get("data") or []
    if not live:
        live = _load_models_cache(r)     # 动态拿不到时先用上次成功的结果
    if not live:
        live = read_local_models(realm=r)
    entries = merge_catalog(live or None, realm=r)
    # 广告的名字（/v1/models 的 id）与解析器认知同源：清单一到手就注册显示名
    _register_live_names(entries)
    if allow_network:
        with _lock:
            _models_cache[r] = {"at": time.time(), "data": entries}
            # 在线刷新出了新数据：旧 memo 作废，离线消费者立即看到新显示名
            _offline_models_cache.pop(r, None)
    else:
        # 只进 memo（短 TTL），不写 _models_cache：把离线合并结果标成
        # 「新鲜」会挡住 /v1/models 等在线路径 300s 内的正常网络刷新。
        with _lock:
            _offline_models_cache[r] = (time.time(), entries)
    return entries


def fetch_models_merged():
    """#26 步4：两区并集，逐条附区域标注。返回 [(mid, meta, realm)]。

    同一上游 key 在两区都有的**只保留一条**（当前区优先），避免客户端看到
    重复 id；顺序为「当前区在前 + 各自清单顺序」。
    """
    out = []
    seen = set()
    cur = CURRENT_REALM if CURRENT_REALM in ("cn", "intl") else "cn"
    for r in (cur, "intl" if cur == "cn" else "cn"):
        for mid, meta in fetch_models(realm=r):
            if mid in seen:
                continue
            seen.add(mid)
            out.append((mid, meta, r))
    return out


def read_dynamic_models(realm=None):
    """COSY 签名拉取 /algo/api/v2/model/list（chat + app-only 对话模型）。失败返回 []。

    注意：签名的 body 是 qoder_encode("{}")，请求必须**带同款 body**发出
    （服务端校验签名与 body 一致，裸 GET 会 403）。

    主机按官方的候选顺序尝试（国际版 api1 → api2 → api3）：切换主机不影响
    签名（签名只覆盖 path），单个域名故障不再导致模型清单整体拉取失败。
    """
    r = realm or CURRENT_REALM
    account = POOL.pick(realm=r) if POOL else None
    if account is None:
        return []
    # 签名以 qoder_encode("{}") 作为 body 参与 MD5；请求同样携带该 body。
    sign_body = qoder_encode(b"{}")
    payload = None
    for host in gateway_candidates(r, qoder_accounts.account_realm_config(account)):
        raw_url = host + MODELS_PATH
        try:
            sess = SESSIONS.get(account)
            headers = sess.headers(sign_body, raw_url, model_key="", sse=False,
                                   accept="application/json")
            url = validate_public_http_url(raw_url)
            req = urllib.request.Request(url, data=sign_body.encode("utf-8"),
                                         method="GET", headers=headers)
            opener = (qoder_accounts._opener_for_request().open
                      if qoder_accounts.account_realm_config(account).get("vpc_instance")
                      else urllib.request.urlopen)
            with opener(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            log("model discovery failed on %s: %s" % (host, exc))
    if payload is None:
        return []
    chat = qoder_catalog.dialogue_models(payload)
    out = []
    for m in chat:
        if not isinstance(m, dict):
            continue
        mid = m.get("key")
        if not mid:
            continue
        # 官方原始条目**全字段原样**透传（enable/strategies/is_editable/
        # minimal_version/... 一律不裁剪），仅补 id/name 兼容键。
        row = dict(m)
        row["id"] = mid
        row.setdefault("name", m.get("display_name") or mid)
        row.setdefault("display_name", m.get("display_name") or mid)
        out.append((mid, row))
    if out:
        log("model discovery ok: %d chat models from %s" % (len(out), r))
    return out


def _parse_hhmm(value):
    """'22:00' -> 1320 分钟数；解析失败返回 None。"""
    try:
        parts = str(value).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


def off_peak_active_now(window_start, window_end, tz=None, now=None):
    """判断当前时刻是否处于官方低谷（Off-Peak）时段。

    官方 promotion.window 为 22:00-08:00（跨午夜），文案标注 UTC+8；
    官方时区 Asia/Shanghai / Asia/Singapore 均为 UTC+8，因此按固定 +8
    计算（无 tzdata 依赖），未知时区也按官方标注的 UTC+8 兜底。
    返回 True（窗口内）/ False（窗口外）/ None（无有效窗口）。
    """
    s = _parse_hhmm(window_start)
    e = _parse_hhmm(window_end)
    if s is None or e is None:
        return None
    if now is None:
        now = time.time()
    import datetime
    offset_hours = 8
    if isinstance(tz, str):
        if tz.upper().startswith("UTC"):
            m = re.search(r"UTC([+-]\d{1,2})", tz.upper())
            if m:
                offset_hours = int(m.group(1))
        # Asia/Shanghai / Asia/Singapore -> +8（默认即 8）
    utc = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    local = utc + datetime.timedelta(hours=offset_hours)
    cur = local.hour * 60 + local.minute
    if s == e:
        return True
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e      # 跨午夜窗口（22:00-08:00）


def model_entry(mid, meta, realm=None):
    """Build a rich /v1/models entry.

    - `id` = **官方模型名 display_name**（如 `Qwen3.8-Max`）——客户端唯一需要
      填的值，与官方桌面版选择器显示一致；`upstream_key` 保留缩写 key，
      `aliases` 列出所有可接受形式（key / 「key (Name)」/ 人类别名）。
    - `description` = 官方桌面版介绍文案（dynamic-text zh.detail）。
    - `name_local` = 官方本地化名（zh.label 与 display_name 不同时，如
      Ultimate -> 极致、旧文案 Kimi-K2.7-Code）。
    - 上下文窗口来自官方 context_config；思考档位来自 thinking_config。
    - 峰谷价来自 promotion（peak -> valley + 时段窗口与官方错峰文案）。
    - `enabled=false` 时给出 `disabled_reason`（官方原文：需要升级或购买
      千问官方套餐开放）与上游 `disabled_message_key`。
    - **最大输出**：官方（catalog / 动态接口原始响应）均无此字段，因此
      仅当来源真实携带时才输出，绝不编造。
    """
    meta = meta or {}
    display_name = meta.get("name") or meta.get("display_name") or mid
    item = {
        "id": display_name,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "qoder",
        "upstream_key": mid,
        "enabled": meta.get("enable", True) is not False,
    }
    item["name"] = display_name
    if realm:
        # #26 步4：该条来自哪个区域（默认单区视图也带，便于面板/客户端标注）
        item["realm"] = realm
        try:
            _span = qoder_catalog.key_realm_span(mid)
            if len(_span) == 1:
                # 只在**一个**区域存在（如仅 intl 的 Sonus）—— 面板据此打「仅国际版」角标；
                # 双区都有的不给该字段（避免把「都能用」误标成独占）。
                item["realm_exclusive"] = _span[0]
        except Exception:
            pass
    # 描述：来源自带动态/本地 description，否则取官方桌面版文案
    desc = meta.get("description") or ""
    if not desc:
        try:
            desc = qoder_catalog.official_description(mid)
        except Exception:
            desc = ""
    if desc:
        item["description"] = desc
    # 官方本地化显示名（与 display_name 不同时透出）
    try:
        local = qoder_catalog.official_local_name(mid)
    except Exception:
        local = ""
    if local:
        item["name_local"] = local
    # 所有可接受的填写形式
    aliases = [mid, "%s (%s)" % (mid, display_name)] if display_name != mid else [mid]
    for ak, av in qoder_catalog.MODEL_ALIASES.items():
        if av == mid and ak not in aliases:
            aliases.append(ak)
    item["aliases"] = aliases
    # 禁用态：官方原因文案（未开通千问套餐时官方桌面版所示）
    if not item["enabled"]:
        item["disabled_reason"] = getattr(
            qoder_catalog, "OFFICIAL_DISABLED_REASON",
            "需要升级或购买千问官方套餐开放")
        strategies = meta.get("strategies")
        if isinstance(strategies, list):
            for st in strategies:
                if isinstance(st, dict) and st.get("disabled_message_key"):
                    item["disabled_message_key"] = st["disabled_message_key"]
                    break
            item["strategies"] = strategies
    vision = bool(meta.get("is_vl") or meta.get("supportsImages")) \
        and not meta.get("disabledMultimodal")
    tools = bool(meta.get("supportsToolCall", True))
    thinking_cfg = meta.get("thinking_config") if isinstance(
        meta.get("thinking_config"), dict) else {}
    thinks = bool(meta.get("is_reasoning") or meta.get("supportsReasoning")
                  or isinstance(thinking_cfg.get("enabled"), dict))
    inputs = ["text"] + (["image"] if vision else [])
    item["capabilities"] = {"vision": vision, "tool_calls": tools,
                            "reasoning": thinks}
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {"vision": vision, "functionCall": tools,
                         "function_call": tools, "reasoning": thinks}
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    item["architecture"] = {
        "input_modalities": inputs,
        "output_modalities": ["text"],
        "modality": "+".join(inputs) + "->text",
    }
    # ---- limits：官方 context_config 多窗口 + 默认输入上限 ----
    max_in = meta.get("maxInputTokens") or meta.get("max_input_tokens")
    ctx_cfg = meta.get("context_config") if isinstance(
        meta.get("context_config"), dict) else {}
    windows, labels, default_label = [], [], ""
    for label, conf in ctx_cfg.items():
        if not isinstance(conf, dict):
            continue
        tok = conf.get("token_count")
        if tok:
            labels.append(str(label))
            windows.append(tok)
            if conf.get("is_default"):
                default_label = str(label)
    if not default_label and labels:
        default_label = labels[0]
    if max_in:
        item["context_length"] = max_in
        item["max_input_tokens"] = max_in
    if windows:
        item["context_windows"] = windows
        item["context_window_labels"] = labels
        item["context_window_default"] = default_label
    # 最大输出：官方（catalog/动态接口）均无此字段——不输出、不展示（用户
    # 已确认不再测量该值）。
    max_out = meta.get("maxOutputTokens") or meta.get("max_output_tokens")
    if not max_out:
        try:
            max_out = max_output_limit(mid, realm)
        except Exception:
            max_out = None
    if max_out:
        item["max_output_tokens"] = max_out
        item["max_completion_tokens"] = max_out
    # ---- 思考档位：官方 thinking_config ----
    enabled_think = thinking_cfg.get("enabled")
    if isinstance(enabled_think, dict):
        order = ["low", "medium", "high", "xhigh", "max"]
        eff_map = enabled_think.get("efforts") or {}
        efforts = [e for e in order if e in eff_map]
        if efforts:
            item["reasoning_efforts"] = efforts
        for e in efforts:
            if isinstance(eff_map.get(e), dict) and eff_map[e].get("is_default"):
                item["reasoning_default_effort"] = e
                break
    if "disabled" in thinking_cfg:
        item["reasoning_can_disable"] = True
    # ---- 兼容旧 catalog 形态 ----
    effort_fixed = (meta.get("reasoning") or {}).get("effort")
    if effort_fixed:
        item["reasoning_fixed_effort"] = effort_fixed
    efforts_old = (meta.get("reasoning") or {}).get("supportedEfforts")
    if efforts_old and "reasoning_efforts" not in item:
        item["reasoning_efforts"] = efforts_old
    if (meta.get("reasoning") or {}).get("defaultEffort") \
            and "reasoning_default_effort" not in item:
        item["reasoning_default_effort"] = meta["reasoning"]["defaultEffort"]
    # ---- 计费：峰谷价（官方 promotion） ----
    price_now = meta.get("price_factor")
    promo = meta.get("promotion") if isinstance(meta.get("promotion"), dict) else {}
    if price_now is not None:
        item["price_factor"] = price_now
    peak = None
    if promo.get("before_promotion_price_factor") is not None:
        peak = promo.get("before_promotion_price_factor")
    elif meta.get("original_price_factor") is not None:
        peak = meta.get("original_price_factor")
    if peak is not None:
        item["price_factor_peak"] = peak
    # 低谷价 = 峰值 × 折扣（官方 promotion 的定义，与快照抓取时刻无关）。
    # 旧实现直接把"当前 price_factor"当成低谷价：快照若在非低谷时段抓取，
    # 报出的低谷价就会等于峰价（看板/客户端据此算错费用）。
    discount = promo.get("discount_factor")
    valley = None
    try:
        if peak is not None and discount and 0 < float(discount) <= 1:
            valley = round(float(peak) * float(discount), 4)
    except (TypeError, ValueError):
        valley = None
    if valley is None and price_now is not None:
        valley = price_now
    if valley is not None:
        item["price_factor_valley"] = valley
    # 只要官方给了 promotion 就下发峰谷元数据（不限于"此刻正在打折"）：
    # 高峰期抓取的快照里 promo.active=False，旧实现会整块跳过，客户端于是
    # 既看不到低谷窗口、也看不到峰价。active 如实透传官方值，"现在是否处于
    # 低谷"另由 off_peak_active_now（按本地时间复算）给出。
    if promo:
        badge = promo.get("badge") or {}
        desc_p = promo.get("description") or {}
        link = promo.get("link_url") or {}
        item["off_peak"] = {
            "active": bool(promo.get("active")),
            "window_start": promo.get("window_start"),
            "window_end": promo.get("window_end"),
            "timezone": promo.get("timezone"),
            "discount_factor": promo.get("discount_factor"),
            "badge": badge.get("zh") or badge.get("en") or "",
            "badge_en": badge.get("en") or "",
            "description": desc_p.get("zh") or desc_p.get("en") or "",
            "description_en": desc_p.get("en") or "",
            "link": link.get("zh") or link.get("en") or "",
        }
        item["off_peak_window"] = "%s-%s" % (promo.get("window_start"),
                                             promo.get("window_end"))
        item["promotion"] = promo
        # 当前是否正处于低谷时段（供看板做视觉高亮；前端亦会按本地时间复算）
        active = off_peak_active_now(promo.get("window_start"),
                                     promo.get("window_end"),
                                     tz=promo.get("timezone"))
        item["off_peak_active_now"] = bool(active)
    if meta.get("original_price_factor") is not None:
        item["original_price_factor"] = meta.get("original_price_factor")
    if meta.get("is_free") is not None:
        item["is_free"] = bool(meta.get("is_free"))
    if meta.get("is_new"):
        item["is_new"] = True
    if meta.get("icon"):
        item["icon"] = meta.get("icon")
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    return item


# ---------------------------------------------------------------------------
# 请求清洗与归一化
# ---------------------------------------------------------------------------
def strip_data_prefix(line):
    line = line.strip()
    if not line or line.startswith(":"):
        return ""
    while line.startswith("data:"):
        line = line[5:].strip()
    if not line or line.startswith(":"):
        return ""
    return line


def normalize_usage_for_client(usage):
    """把上游的多套缓存字段归一写进 OpenAI 标准位（issue #112）。

    上游按模型族换字段：DeepSeek 用 `prompt_cache_hit_tokens`、Qwen 用
    `completion_tokens_details.cached_tokens`，都不发标准位
    `prompt_tokens_details.cached_tokens` —— 按标准协议解析的客户端恒读 0。
    这里复用 _extract_usage 的优先级把数字搬过去，**原字段保留**（不删上游惯例，
    避免已经适配了上游形态的客户端反而读不到）。
    """
    if not isinstance(usage, dict):
        return usage
    det = usage.get("completion_tokens_details") or {}
    pdet = usage.get("prompt_tokens_details")
    pdet = dict(pdet) if isinstance(pdet, dict) else {}
    val = None
    for cand in (usage.get("cached_tokens"),
                 usage.get("prompt_cache_hit_tokens"),
                 det.get("cached_tokens") if isinstance(det, dict) else None,
                 pdet.get("cached_tokens")):
        try:
            n = int(cand or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            val = n
            break
    if val is None:
        if "prompt_tokens_details" in usage and pdet:
            return usage          # 上游已经给了标准位，别动它
        return usage
    if pdet.get("cached_tokens") == val and usage.get("prompt_tokens_details"):
        return usage
    pdet["cached_tokens"] = val
    usage["prompt_tokens_details"] = pdet
    return usage

def clean_chunk(raw):
    """Drop the empty noise fields the gateway pads deltas with."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(obj, dict):
        return raw
    changed = False
    # issue #112：流尾 usage 帧也要按 OpenAI 标准位给 cached_tokens
    if isinstance(obj.get("usage"), dict):
        before = json.dumps(obj["usage"], sort_keys=True)
        obj["usage"] = normalize_usage_for_client(obj["usage"])
        if json.dumps(obj["usage"], sort_keys=True) != before:
            changed = True
    for choice in obj.get("choices") or []:
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        fc = delta.get("function_call")
        if fc is not None:
            fc_empty = (not fc) if not isinstance(fc, dict) else (not fc.get("name"))
            if fc_empty:
                delta.pop("function_call", None)
                changed = True
        if isinstance(delta.get("tool_calls"), list) and not delta["tool_calls"]:
            delta.pop("tool_calls")
            changed = True
        for key in NOISE_KEYS:
            if key in delta and not delta.get(key):
                delta.pop(key)
                changed = True
        if not delta and not choice.get("finish_reason"):
            return ""
    return json.dumps(obj, ensure_ascii=False) if changed else raw


def _strip_empty_fc(obj):
    """递归剔除空 function_call 占位（Responses/chat 通用）。"""
    changed = False
    if isinstance(obj, dict):
        fc = obj.get("function_call")
        if isinstance(fc, dict) and not fc.get("name"):
            obj.pop("function_call", None)
            changed = True
        tc = obj.get("tool_calls")
        if isinstance(tc, list) and not tc:
            obj.pop("tool_calls")
            changed = True
        for v in list(obj.values()):
            if _strip_empty_fc(v):
                changed = True
    elif isinstance(obj, list):
        for v in obj:
            if _strip_empty_fc(v):
                changed = True
    return changed


def clean_responses_frame(frame):
    """清洗 Responses SSE 帧（bytes）。只改写 data: 行，event: 行原样保留。"""
    if not frame:
        return frame
    try:
        text = frame.decode("utf-8")
    except Exception:
        return frame
    out, changed = [], False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("data:"):
            payload = st[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if _strip_empty_fc(obj):
                        line = "data: " + json.dumps(obj, ensure_ascii=False)
                        changed = True
                except Exception:
                    pass
        out.append(line)
    return ("\n".join(out) + "\n\n").encode("utf-8") if changed else frame


def normalize_roles(messages):
    """Map role names the upstream rejects onto ones it accepts.

    Qoder 只认识 system / user / assistant（+ 模板自带）。OpenAI 新的
    "developer" 角色等价于 "system"。
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        if m.get("role") == "developer":
            item = dict(m)
            item["role"] = "system"
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 出站提示词脱敏（防御性保留：过滤常见客户端指纹串）
# ---------------------------------------------------------------------------
SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code, Anthropic's official CLI for Claude",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
)
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
)
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\r\n]*;?\s*")
SANITIZE_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\r\n]*;?\s*")


def has_fingerprint(text):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    # The only remaining match starts with ASCII x-/X-. Cheap C substring scans
    # avoid the case-insensitive regex over every clean token of long histories.
    return ("x-" in text or "X-" in text) and bool(SANITIZE_BARE_HDR_RE.search(text))


def sanitize_text(text):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text):
        return text
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = ""
        while prev != text:
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    text = SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()


def sanitize_content(content):
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" \
                    and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"])
                out.append(p)
            else:
                out.append(part)
        return out
    return content


def sanitize_tool_calls(tool_calls):
    if not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            item = dict(tc)
            fn = item.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn = dict(fn)
                fn["arguments"] = sanitize_text(fn["arguments"])
                item["function"] = fn
            out.append(item)
        else:
            out.append(tc)
    return out


def sanitize_messages(messages):
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            item = dict(m)
            if "content" in item:
                item["content"] = sanitize_content(item["content"])
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = sanitize_text(item["reasoning_content"])
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out


# 思考档位的官方词表顺序（用于把客户端档位映射到模型支持档位）
EFFORT_RANK = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4,
               "xhigh": 5, "max": 6}
_EFFORT_OFF = ("none", "off", "disabled", "disable", "false", "0", "no")


def supported_efforts(meta):
    """该模型官方支持的思考档位（取自目录 thinking_config.enabled.efforts）。

    空列表表示该模型只有"开/关"没有档位（如 cn 的 qmodel）。
    """
    tc = (meta or {}).get("thinking_config") or {}
    enabled = tc.get("enabled") if isinstance(tc.get("enabled"), dict) else {}
    efforts = enabled.get("efforts") if isinstance(enabled.get("efforts"), dict) else {}
    return [e for e in EFFORT_RANK if e in efforts]


def catalog_meta_for(model_key, realm=None):
    """按上游 key 取该模型在官方目录里的元数据（找不到返回 {}）。

    Anthropic 入口在调用 qoder_anthropic.messages_to_chat 时需要一个 meta，
    否则 effort_from_anthropic 取不到 supported 档位表 -> 思考档位被静默丢弃。

    优先动态清单（display_name / thinking_config / 上下文窗口都是官方**此刻**
    的口径；快照可能还停在旧显示名，如 kmodel_latest 的 Kimi-K3 ->
    Kimi-K3.1-Pro 改名），静态快照兜底。fetch_models 自带 300s 缓存与
    「动态 > 落盘 > 本机 > 快照」降级，离线/无账号时退回快照，行为不变。
    """
    r = realm if realm in ("cn", "intl") else CURRENT_REALM
    try:
        # 请求路径也会走到这里（build_qoder_body / Anthropic 档位表）——离线取
        # （allow_network=False）：在线刷新由启动预热线程与 /v1/models 负责，
        # 请求内不做额外出站调用、不碰账号池（#86 钉死账号语义）。
        for mid, meta in fetch_models(realm=r, allow_network=False):
            if mid == model_key and meta:
                out = dict(meta)
                out.setdefault("key", model_key)
                return out
    except Exception:
        pass
    for m in qoder_catalog.models_for_realm(r):
        if m.get("key") == model_key:
            return m
    for m in qoder_catalog.STATIC_INTL_MODELS + qoder_catalog.STATIC_CN_MODELS:
        if m.get("key") == model_key:
            return m
    return {}


def default_effort(meta):
    """该模型的默认思考档位（is_default 标注项），无则取支持表中间档。"""
    tc = (meta or {}).get("thinking_config") or {}
    enabled = tc.get("enabled") if isinstance(tc.get("enabled"), dict) else {}
    efforts = enabled.get("efforts") if isinstance(enabled.get("efforts"), dict) else {}
    for e, cfg in efforts.items():
        if isinstance(cfg, dict) and cfg.get("is_default"):
            return e
    sup = supported_efforts(meta)
    return sup[len(sup) // 2] if sup else ""


def normalize_reasoning_effort(effort, meta):
    """把请求里的思考档位归一化到该模型**官方支持**的档位集合。

    为什么必须做：上游对不支持的档位**不报错、直接忽略**（回落到模型默认档）。
    实测：dfmodel 官方只支持 low/high/max（默认 max），传 medium/xhigh 时输出
    与默认档一致——客户端以为"思考档位没生效"；cn 的 qmodel 只有开/关，传任何
    档位都被忽略（只有 none 能关掉思考）。

    规则：
      - 关闭类取值（none/off/disabled/...）统一映射为 "none"（官方通用关闭值）；
      - 模型有档位表：命中则原样透传；未命中取"最近的合法档位"（同距时偏向
        模型默认档），并给出说明性 note；
      - 模型有 thinking_config 但无档位表（仅开/关）：除 none 外不下发该参数
        （避免给上游发它不认识的档位）；
      - 模型没有 thinking_config（如路由器 `auto`）：**原样透传**，不做猜测。

    返回 (value|None, note)：value=None 表示不下发 reasoning_effort。
    """
    e = str(effort or "").strip().lower()
    if not e:
        return None, ""
    if e in _EFFORT_OFF:
        return "none", ""
    tc = (meta or {}).get("thinking_config")
    if not isinstance(tc, dict):
        # 目录里没有该模型的思考配置（路由器/未知模型）：不猜测，原样透传
        return e, ""
    sup = supported_efforts(meta)
    if not sup:
        return None, "dropped (model has no effort levels; use none to disable)"
    if e in sup:
        return e, ""
    if e not in EFFORT_RANK:
        return default_effort(meta) or sup[0], \
            "unknown level -> %s" % (default_effort(meta) or sup[0])
    d = default_effort(meta)
    want = EFFORT_RANK[e]

    def key(cand):
        return (abs(EFFORT_RANK[cand] - want),
                abs(EFFORT_RANK[cand] - EFFORT_RANK.get(d, want)))

    best = sorted(sup, key=key)[0]
    return best, "unsupported level %s -> %s" % (e, best)


def requested_effort(payload):
    """请求侧的**原始**思考档位（归一化之前），供 usage 记账与请求体构造共用。

    取值顺序与 build_qoder_body 下发给上游时完全一致（同源，避免两处漂移）：
    reasoning_effort -> reasoning.effort -> thinking.effort / thinking.level。
    仅做提取，不做任何归一化或猜测；空值返回 None。
    """
    if not isinstance(payload, dict):
        return None
    effort = payload.get("reasoning_effort")
    if not effort and isinstance(payload.get("reasoning"), dict):
        effort = (payload.get("reasoning") or {}).get("effort")
    if not effort and isinstance(payload.get("thinking"), dict):
        # 兼容 thinking.effort / thinking_level 风格客户端
        effort = (payload.get("thinking") or {}).get("effort")             or (payload.get("thinking") or {}).get("level")
    return effort or None


# ---------------------------------------------------------------------------
# 一键更新（面板按钮 -> 下载 release -> 校验 -> 备份 -> 替换 -> 自动重启）
# ---------------------------------------------------------------------------
_LAUNCH_ARGV = list(sys.argv)          # 更新后按原参数重启
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
UPDATE_DISABLED = str(os.environ.get("QD_UPDATE_DISABLE") or "").strip().lower() \
    in ("1", "on", "true", "yes", "enable", "enabled")
_UPDATE_LOCK = threading.Lock()


def _update_allow_remote():
    return str(os.environ.get("QD_UPDATE_ALLOW_REMOTE") or "").strip().lower() \
        in ("1", "on", "true", "yes", "enable", "enabled")


def _is_loopback(host):
    host = str(host or "")
    return host in ("::1", "localhost") or host.startswith("127.") \
        or host.startswith("::ffff:127.")


def update_plan_info():
    """一键更新的预检信息（不下载、不改文件）。"""
    cur = VERSION
    latest = ""
    err = ""
    lookup_failed = False
    try:
        latest = qoder_updater.latest_release().get("version") or ""
    except Exception as exc:
        err = str(exc)[:160]
        # GitHub 匿名限流 60 次/小时/IP：本函数每次都实时打 API，403 被吞掉后
        # 曾被渲染成「当前已是最新版本」（2026-11-11 看板自相矛盾）。
        # 查不到就回退到 /update/check 的缓存；仍拿不到才标记 lookup_failed。
        try:
            cached = check_for_update()
        except Exception:
            cached = None
        if isinstance(cached, dict):
            latest = str(cached.get("latest") or "").strip().lstrip("vV")
            err = err or str(cached.get("error") or "")
        if not latest:
            lookup_failed = True
    has = bool(latest) and version_tuple(latest) > version_tuple(cur)
    return {
        "ok": True,
        "current": cur,
        "latest": latest,
        "has_update": has,
        "lookup_failed": lookup_failed,
        "container": qoder_updater._in_container(),
        "disabled": UPDATE_DISABLED,
        "can_apply": bool(has) and not UPDATE_DISABLED,
        "state": qoder_updater.state(),
        "error": err,
    }


def apply_update_now(tag=None):
    """执行更新（阻塞）；成功后调用方负责触发重启。"""
    with _UPDATE_LOCK:
        return qoder_updater.apply_update(APP_ROOT, tag=tag)

# 进程级启动时间：/health 回给外部（issue #89：判断「跑的是不是新版本」）
STARTED_AT = time.time()


def _pid_alive(pid):
    """PID 是否还活着（跨平台；权限不足视为活着）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
    except Exception:
        return False


def classify_existing_instance(existing, pid_alive=None):
    """判断 /health 的应答者属于哪种情况（issue #89）。

    返回 "foreign" / "healthy" / "stale" / "none"：
      · foreign  —— 同端口上是别的服务（service 字段不对或没有 accounts）
      · healthy  —— 真的是一个活着的 qd-proxy
      · stale    —— 应答来自**正在退出**的旧实例（pid 已不存在），不该拦启动
      · none     —— 没人应答
    """
    if not isinstance(existing, dict):
        return "none"
    if existing.get("service") not in (None, "qoder-proxy") \
            or "accounts" not in existing:
        return "foreign"
    alive = pid_alive or _pid_alive
    pid = existing.get("pid")
    if pid and not alive(pid):
        return "stale"
    return "healthy"


# ---------------------------------------------------------------------------
# 项目新版本检测（对比 GitHub 最新 release；结果缓存 6 小时）
# ---------------------------------------------------------------------------
UPDATE_CHECK_URL = ("https://api.github.com/repos/shuishuipingan/"
                    "qoder2api-hub/releases/latest")
UPDATE_CHECK_TTL = 6 * 3600
_update_cache = {"at": 0.0, "data": None}


def version_tuple(value):
    """'v1.2.3' -> (1, 2, 3)；容忍前缀与不足位数。"""
    out = []
    for chunk in str(value or "").lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits or 0))
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def check_for_update(force=False):
    """检查项目是否有新版本（对比当前 VERSION 与 GitHub 最新 release）。

    返回 {ok, current, latest, has_update, url, published_at, name, checked_at, error}
    网络不可用/被墙时 ok=False + error，看板据此显示"检查失败"而不是误报。
    """
    now = time.time()
    cached = _update_cache.get("data")
    if cached and not force and now - _update_cache.get("at", 0) < UPDATE_CHECK_TTL:
        return cached
    info = {"ok": False, "current": VERSION, "latest": "", "has_update": False,
            "url": "", "published_at": "", "name": "", "checked_at": int(now),
            "error": ""}
    try:
        url = validate_public_http_url(UPDATE_CHECK_URL)
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "qoder-proxy-update-check",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "").strip()
        info.update({
            "ok": True,
            "latest": tag,
            "url": str(data.get("html_url") or ""),
            "published_at": str(data.get("published_at") or ""),
            "name": str(data.get("name") or ""),
            "has_update": bool(tag) and version_tuple(tag) > version_tuple(VERSION),
        })
    except Exception as exc:
        info["error"] = str(exc)[:160]
    _update_cache.update({"at": now, "data": info})
    return info


def is_deepseek_model(model="", model_key=""):
    """判断这次请求最终打到的是不是一个 DeepSeek 上游模型。

    必须同时看**上游 key**（dmodel / dfmodel）和客户端写的名字：
    客户端完全可能直接写 `dfmodel`/`dmodel`（仓库文档里就把 DeepSeek-Flash
    标为「内部 key：dfmodel」），而展示名是 `DeepSeek-Flash`。此前只按名字
    前缀 "deepseek" 判断，走 key 的请求拿不到 reasoning_content 兼容处理，
    多轮会话会被上游拒绝——表现为"偶发失败、重试有时能过"。
    """
    key = str(model_key or "").strip().lower()
    if key in DEEPSEEK_MODEL_KEYS:
        return True
    low = str(model or "").strip().lower()
    if not low:
        return False
    if low in DEEPSEEK_MODEL_KEYS or low.startswith("deepseek"):
        return True
    # 展示 id「dfmodel (DeepSeek-Flash)」/ 别名 deepseek-v4-flash 等
    return "deepseek" in low


def is_kimi_model(model="", model_key=""):
    """Resolve both provider keys and client-facing aliases (issue #153)."""
    keys = ("kmodel", "kmodel_latest")
    for candidate in (model_key, model):
        low = str(candidate or "").strip().lower()
        if low in keys or re.search(r"(?:^|[^a-z0-9])kimi(?:$|[^a-z0-9])", low):
            return True
    return False


# Stable request-prefix instruction, not a per-turn reminder: do not change
# history/tool schemas or claim upstream strict-schema support we did not verify.
KIMI_TOOL_PROTOCOL_PROMPT = """[Tool protocol requirements]
When tools are available, use the native tool_calls channel for actual tool calls.
Historical tool-call text and tool-result messages are records, not output templates.
Do not repeat textual tool-call markers, XML/DSML tags, JSON call envelopes, or
Markdown fences as a substitute for a native tool call. Ordinary answers remain
ordinary text; the presence of tools does not require calling one.
For each tool call, arguments must encode one complete valid JSON object matching
that tool's declared parameters, required fields and value types. Use double-quoted
JSON keys and strings; escape inner quotes, backslashes, newlines, tabs and control
characters. Preserve code/file/command content exactly through JSON escaping.
Do not use Python literals, comments, trailing commas, NaN/Infinity, or expressions
as JSON values, or a JSON-encoded string instead of the arguments object.
Before emitting the call, check the full JSON syntax and schema. For long file writes
or edits, prefer smaller complete calls when the tool schema and task permit; never
truncate a JSON string/object. Do not claim execution until a tool result arrives.
[/Tool protocol requirements]"""


def _apply_kimi_tool_protocol(body, payload, model_key):
    if not is_kimi_model(payload.get("model"), model_key) or not body.get("tools"):
        return
    choice = payload.get("tool_choice")
    if choice == "none" or (isinstance(choice, dict) and choice.get("type") == "none"):
        return
    messages = body["messages"]
    system = next((m for m in messages if m.get("role") == "system"), None)
    if system is None:
        messages.insert(0, {"role": "system", "content": KIMI_TOOL_PROTOCOL_PROMPT})
    elif KIMI_TOOL_PROTOCOL_PROMPT not in str(system.get("content") or ""):
        system["content"] = str(system.get("content") or "") + "\n\n" + KIMI_TOOL_PROTOCOL_PROMPT


def backfill_reasoning_content(messages, model, model_key=""):
    """DeepSeek 多轮一致性：assistant 历史补 reasoning_content。

    触发条件只看"上游是不是 DeepSeek"，与客户端用 key 还是展示名无关。
    """
    if not is_deepseek_model(model, model_key):
        return messages
    has_trace = False
    for m in messages:
        if isinstance(m, dict):
            if m.get("reasoning") or "reasoning_content" in m:
                has_trace = True
                break
    if not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            if "reasoning_content" not in item:
                if item.get("reasoning"):
                    item["reasoning_content"] = str(item["reasoning"])
                else:
                    item["reasoning_content"] = ""
            out.append(item)
        else:
            out.append(m)
    return out


# P2-2：tool_choice="none" 时是否连 tools 一起删（历史行为）。
# 默认**保留** tools 并把 "none" 原样写回 —— 工具能力声明与「本轮不许调用」是两件事，
# 且上游实测对 tools+none 返回纯文本、不报 400。若某天上游不再遵守 none（参考项目
# wb 的上游就有此现象：保留 tools 后仍回 tool_calls），设 QD_TOOLCHOICE_NONE_DROPS_TOOLS=1
# 可一键回到「连 tools 一起删」的旧行为，无需改代码发版。
TOOLCHOICE_NONE_DROPS_TOOLS = os.environ.get(
    "QD_TOOLCHOICE_NONE_DROPS_TOOLS", "0").lower() in ("1", "true", "yes", "on")


def normalize_tool_choice(obj):
    """把 OpenAI tool_choice 归一成上游可接受的形态（避免 400）。"""
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            if TOOLCHOICE_NONE_DROPS_TOOLS:
                obj.pop("tool_choice", None)
                obj.pop("tools", None)
            else:
                # 保留 tools 声明，只把「本轮不许调用」写在 tool_choice 上（上游只认字符串）
                obj["tool_choice"] = "none"
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            if TOOLCHOICE_NONE_DROPS_TOOLS:
                obj.pop("tool_choice", None)
                obj.pop("tools", None)
            else:
                # 对象形式降级为字符串 "none"（与 auto/required 的既有降级风格一致）
                obj["tool_choice"] = "none"
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)


def normalize_tools(obj):
    """把顶层 name 型工具定义包成 Chat Completions function schema。"""
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        return
    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        _kind2 = t.get("type")
        if _kind2 not in (None, "function"):
            # issue #63：namespace / web_search 等类型 Qoder 无法表达，
            # 透传会导致上游 400（unknown variant）。丢弃并告警。
            _log_unsupported_tool(_kind2, "chat-tools")
            continue
        if "name" in t and "function" not in t and t.get("type") == "function":
            fn = {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {},
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            norm.append({"type": "function", "function": fn})
        else:
            norm.append(t)
    obj["tools"] = norm


def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# DeepSeek DSML 工具调用回退解析
# ---------------------------------------------------------------------------
TAG_START = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>",
                           re.DOTALL)
DSML_INVOKE_RE = re.compile(
    TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>",
    re.DOTALL)
DSML_PARAM_RE = re.compile(
    TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>",
    re.DOTALL)


def parse_dsml_tool_calls(text):
    if not text or "DSML" not in text:
        return None, text
    match = DSML_CALLS_RE.search(text)
    if not match:
        # issue #51：截图里出现过「没有 calls 包裹、只有 invoke/parameter」的形态
        # （以及 calls 闭合标签被截断）——直接在全文里找 invoke 块。
        if not DSML_INVOKE_RE.search(text):
            return None, text
        calls_block = text
    else:
        calls_block = match.group(1)
    tool_calls = []
    for inv_match in DSML_INVOKE_RE.finditer(calls_block):
        func_name = inv_match.group(1)
        params_block = inv_match.group(2)
        params = {}
        for p_match in DSML_PARAM_RE.finditer(params_block):
            p_name = p_match.group(1)
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        tool_calls.append({
            "id": _new_id("call_"),
            "name": func_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        })
    if match is not None:
        clean = (text[:match.start()].strip() + " "
                 + text[match.end():].strip()).strip()
    else:
        # 无 calls 包裹（issue #51）：从第一个 invoke 之前的标签起点裁起
        _starts = [m.start() for m in DSML_INVOKE_RE.finditer(text)]
        _start = min(_starts) if _starts else 0
        _lt = text.rfind("<", 0, _start)
        clean = text[:(_lt if _lt >= 0 else _start)].strip()
    return tool_calls, clean


# ---------------------------------------------------------------------------
# 历史工具调用文本形态的「回读」（issue #8）
# ---------------------------------------------------------------------------
# flatten_messages() 会把 assistant 历史里的 tool_calls 序列化成
#   marker（LEAK_MARKER，见下）+ 一个 JSON 数组 [{name, arguments}, ...]
# 给上游模型当上下文。长会话里模型偶发**照格式复述**成正文（没有结构化
# tool_calls），客户端就会把这段 JSON 当正文显示、本轮工具调用不执行。
# 这里补齐回读：在严格守卫下还原成结构化 tool_calls 并从正文移除。
LEAK_MARKER = "[assistant 请求调用工具]"
# issue #36 follow-up: the same upstream converter also emits the marker
# with ANGLE brackets (<assistant 请求调用工具>). Normalize it to the square-bracket
# form at every entry so there is exactly one code path for detection.
LEAK_MARKER_ALT = "<assistant 请求调用工具>"
# issue #60 follow-up：模型复述时还会把括号写成 `[<assistant 请求调用工具]`、
# 或丢掉尾括号 `[<assistant 请求调用工具`。这里用一条正则统一识别所有变体，
# 避免每出现一种就补一个常量。
_LEAK_MARKER_RE = re.compile(
    r"[\[<(]*\s*assistant\s*请求调用工具\s*[\]>)]?")

_MARKER_CORE = "assistant请求调用工具"
# 只匹配「开头」部分（前导括号 + assistant + 文案），不含尾括号。
_MARKER_OPEN_RE = re.compile(r"^[\[<(]*\s*assistant\s*请求调用工具")
_MARKER_OPEN_CANON = "[assistant 请求调用工具"


def _marker_key(text):
    """去掉空白后的 marker 主体（用于前缀判定）。"""
    return (text or "").replace(" ", "").replace("\u3000", "")


def _marker_prefix_ok(text):
    """text 是否是某个 marker 变体的**前半截**（跨帧/截断用）。"""
    raw = (text or "").lstrip()
    body = raw.lstrip("[<")
    key = _marker_key(body)
    if not key:
        return raw.strip() in ("", "[", "<", "[<")
    return _MARKER_CORE.startswith(key)


def _last_marker_span(text):
    """返回最后一个 marker 变体的 (start, end)；没有则 (-1, -1)。"""
    last = (-1, -1)
    for m in _LEAK_MARKER_RE.finditer(text or ""):
        last = (m.start(), m.end())
    return last





def _normalize_leak_marker(text):
    """Rewrite a leading angle-bracket marker variant into the
    square-bracket form (also through a ``` fence).

    Only a LEADING hit is rewritten; the variant appearing mid-prose is
    untouched (that is not the start of an echo block).
    """
    if not text:
        return text
    t = text.lstrip()
    lead = text[:len(text) - len(t)]
    # issue #60 follow-up：只把**开头**归一成规范形态，尾括号（或缺失）
    # 原样保留——否则给本来没写 ] 的片段补一个，跨帧拼接后会多出一个字符。
    m = _LEAK_MARKER_RE.match(t)
    if m:
        # 只归一**已存在**的括号：原本没写尾括号就不补（否则跨帧拼接多一个字符）；
        # 写了 > / ) / ] 则统一成 ]，保持「一个 marker 只有一个闭括号」。
        _close = "]" if m.group(0).rstrip()[-1:] in ("]", ">", ")") else ""
        return lead + _MARKER_OPEN_CANON + _close + t[m.end():]
    if t.startswith("```"):
        rest = t[3:]
        low = rest.lstrip().lower()
        if low.startswith("json"):
            after = rest.lstrip()[4:].lstrip()
        else:
            after = rest.lstrip()
        m2 = _LEAK_MARKER_RE.match(after)
        if m2:
            return (lead + t[:len(t) - len(after)]
                    + LEAK_MARKER + after[m2.end():])
    return text


# issue #36：上游把 DSML 工具调用转成「marker + JSON 数组」后，偶尔还会在
# 数组之后漏出原始 DSML 闭合标签（</｜DSML｜parameter> / </｜DSML｜invoke>
# / </｜DSML｜calls>，全角或半角竖线）。这些残片落在 hold 尾部会让
# _json_array_prefix_ok 在 "Extra data" 处证伪 -> 整段被当正文补发，工具调用
# 不执行且内部协议文本泄漏给用户。剥离只作用于尾部（内部出现则不属于此形态）。
_DSML_CLOSE_TAG_RE = re.compile(r"</[^>]*DSML[^>]*>\s*$")


def _strip_dsml_close_tail(text):
    """反复剥掉正文尾部连续的 DSML 闭合标签 + 空白；返回剥离后的文本。"""
    prev = None
    while prev != text:
        prev = text
        text = _DSML_CLOSE_TAG_RE.sub("", text)
    return text


def _dsml_tail_holdable(tail):
    """Extra-data 尾巴是否是「DSML 闭合标签（完整 / 帧边界截断的半截）」。

    先剥掉所有尾部完整标签，剩余若空（全是标签）或以 </ 开头（标签
    进行中）→ 值得暂留。宽松只影响延迟：流末 parse 入口的完整剥离是
    最终兜底（issue #36 后续）。
    """
    s = (tail or "").strip()
    if not s:
        return True
    prev = None
    while prev != s:
        prev = s
        s = _DSML_CLOSE_TAG_RE.sub("", s).strip()
    if not s:
        return True
    # 半截标签进行中：已见 "</" 或恰好切在首字符 "<"（1 字符/帧的真实边界）。
    # 长度护栏：真实 DSML 标签 <= ~25 字符，80 已留足余量；更长的大概率是
    # 散文，不再压住（fail-open 语义不破坏）。
    if s.startswith("</"):
        return len(s) <= 80
    return s == "<"


# issue #37：回声块**外层数组合法、内层 arguments 非法**（未转义换行/引号、
# 截断、尾部多余文本、非法转义）。这种块能确定是工具协议回声（marker + 数组 +
# 已声明的工具名），只是参数坏了——绝不能当正文放出去（协议泄漏），也不能
# 伪造一个可执行调用（会写坏文件）。统一替换为一句明确的失败说明。
BAD_ARGS_NOTICE = ("（上游返回的工具调用参数不是合法 JSON，网关已丢弃该调用，"
                   "未执行任何工具；请重试。）")

# issue #37（fail-closed）：错误详情是**固定串**——它会写进日志与客户端响应，
# 绝不能把实际参数、路径或正文带出去。
BAD_ARGS_ERROR_DETAIL = ("upstream tool-call echo contains invalid JSON "
                         "arguments; no tools were emitted")


# issue #49 方向 C 的节流状态：一分钟最多一条 WARN，其余只计数——
# 形态3（54 组空壳）一次会话就能刷出几十条，不节流会把日志淹掉。
_LEAK_WARN_STATE = {"last": 0.0, "suppressed": 0, "total": 0}
_LEAK_WARN_INTERVAL = 60.0


def _log_unrecovered_leak(text, where="chat"):
    """识别到「像工具协议回声、但四条回读路径都没认」时记一条 WARN（issue #49 方向 C）。

    为什么必须可见：失败路径是 fail-open（原样透传），于是**协议失败**被转换成
    **语义失真**——模型说「已清理」，工具其实没执行，客户端看到的是正常结束。
    日志至少让发生率可观测，而不是只能靠用户去查磁盘。
    节流：一分钟一条，其余计入 suppressed（下一条日志里带出来）。
    """
    try:
        t = text or ""
        marks = t.count(LEAK_MARKER) + t.count(LEAK_MARKER_ALT)
        dsml = t.count("DSML")
        if not marks and not dsml:
            return
        st = _LEAK_WARN_STATE
        st["total"] += 1
        now = time.time()
        if now - st["last"] < _LEAK_WARN_INTERVAL:
            st["suppressed"] += 1
            return
        sup = st["suppressed"]
        st["last"] = now
        st["suppressed"] = 0
        log("unrecovered tool-protocol leak passed through (%d chars, "
            "markers=%d, dsml=%d) at %s — issue #49 (total=%d, suppressed=%d)"
            % (len(t), marks, dsml, where, st["total"], sup),
            level="WARN", tag="chat")
    except Exception:
        pass


EMPTY_ECHO_ERROR_DETAIL = (
    "upstream returned only a truncated tool-call echo (no usable call); "
    "the turn was failed on purpose so the client retries instead of "
    "silently treating the task as finished (issue #72)"
)


def _echo_names_declared(text, allowed_names):
    """回声里出现的工具名**全部**在本次声明集合里（issue #72 收紧判据）。

    只按「声明了 tools」就 fail-closed 太宽：模型复述**未声明**工具时我们本来就
    恢复不了（#27），把它判成失败会误伤一批正常回合。
    """
    if not allowed_names:
        return False
    names = re.findall(r'"name"\s*:\s*"([^"]{1,120})"', str(text or ""))
    if not names:
        return False
    return all(n in allowed_names for n in names)


def _marker_only_echo(text):
    """整段除了泄漏标记之外什么都没有（issue #72 的退化形态）。"""
    if not str(text or "").strip():
        return False
    t = _normalize_leak_marker(str(text or "").strip())
    rest = t
    while rest.startswith(LEAK_MARKER):
        rest = rest[len(LEAK_MARKER):].lstrip()
    rest = _strip_dsml_close_tail(rest).strip()
    return rest == ""


def _leak_residue_present(text):
    """这段文本是不是「剥不干净的协议残片」——不是散文里提到标记，是真的漏了。

    issue #49 的三条「认不出就当正文补发」分支都是 fail-open：残片进到客户端
    的 content 里，coding agent 会把标记与半截参数当成工具协议去解析，结果是
    整轮任务静默中断（2026-10-30 实测 4828 字符残片打断一次、31 字符一次）。

    判据必须**双向精确**：
      · 没有泄漏标记 -> 不是残片（普通正文/示例代码）；
      · 有标记但只是散文在讨论它（没有标签、没有 JSON 键）-> 不是残片，
        否则「解释这个 bug 的那轮回复」会被自己打断，客户端无限重试。
    """
    t = str(text or "")
    if not t or not (LEAK_MARKER in t or LEAK_MARKER_ALT in t):
        return False
    if "DSML" in t and "<" in t:                       # DSML 标签残片
        return True
    if "</" in t:                                      # 闭标签尾巴
        return True
    if re.search(r'"(?:arguments|name)"\s*:', t):      # 半截调用参数
        return True
    return "<tool_call" in t or '{"' in t


_LEAK_STRUCT_RE = re.compile(
    r"name\s*=\s*[\"']"                 # DSML invoke/parameter 属性
    r"|<tool_call"                      # 伪标签
    r"|\"(?:arguments|name)\"\s*:"      # JSON 调用字段
    r"|\{\""                            # 参数对象开头
)


_LEAK_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z_][\w:.-]*\s*/?\s*>")

# DSML 令牌：`<|DSML|calls>` / `</｜DSML｜｜ parameter>` / `<｜DSML｜invoke>` 都算
_DSML_TOKEN_RE = re.compile(r"<[^<>]{0,4}DSML|DSML[^<>]{0,4}>|｜DSML｜")


def _dsml_residue_present(text):
    """有 DSML 协议令牌或别的协议结构 —— **不要求出现 LEAK_MARKER**。

    `_sse_recover_dsml` 只有在上游流里出现过 DSML 令牌时才会被调用，所以进到它的
    尾部 else 分支时，DSML 本身已经确认存在。2026-10-30 17:58 实测漏了一条
    `markers=0, dsml=3` 的 58 字符残片：只带 DSML 令牌、没有 LEAK_MARKER，
    用 `_leak_residue_present` 判就漏过去了。
    """
    t = str(text or "")
    if not t or "DSML" not in t:
        return False
    if _DSML_TOKEN_RE.search(t):
        return True
    return bool(_LEAK_STRUCT_RE.search(t) or _LEAK_TAG_RE.search(t))


_FENCE_RE = re.compile(r"```.*?(?:```|$)|`[^`\n]{0,200}`", re.S)


def _outside_fences(text):
    """剥掉代码块/行内代码后的正文。

    非流式侧拿到的是全文，用户/模型**在代码块里讲解本协议**是正常内容
    （例如「DSML 格式长这样：```<|DSML|calls>…```」）。判定残片前先剥掉围栏，
    否则会把一段教学示例判成泄漏，白白 502 一次。
    """
    return _FENCE_RE.sub("", str(text or ""))


def _residue_text_only(text):
    """回读成功后剩下的文本里还带协议残片 -> 该丢（不影响已恢复的调用）。"""
    t = str(text or "")
    if not t or _leak_garbage_only(t):
        return False
    return bool(_leak_residue_present(t) or _dsml_residue_present(t))


def _leak_garbage_only(text):
    """只有泄漏标记和空壳标签，**没有任何可恢复结构**（issue #49 形态 3）。

    形态 3 = `[assistant 请求调用工具]` + `<terminal></terminal>` 刷 54 遍：
    既没有 JSON 也没有 invoke 属性，回读必然失败。此前它是 fail-open 补发，
    结果是客户端正文被标记与空标签塞满，coding agent 解析工具协议时直接
    断在半路（2026-10-30 实测 31 字符残片打断一次）。

    判据必须窄：**文本里得有真正的 HTML 式空标签**才算这种垃圾。
    散文里提到标记（"关于 [assistant 请求调用工具] 这个标记…"）没有标签，
    必须原样透传 —— 既有 #37/#60 的逐字节守恒测试就盯着这条。
    """
    t = str(text or "")
    if not t or not (LEAK_MARKER in t or LEAK_MARKER_ALT in t):
        return False
    if _LEAK_STRUCT_RE.search(t):
        return False
    return _LEAK_TAG_RE.search(t) is not None


def _echo_names_all_undeclared(text, allowed_names):
    """回声里出现的工具名**全部不在**本次声明集合里（issue #27 的 fail-open 边界）。

    模型复述一个没声明的工具时，我们本来就没有可恢复的调用可言，把这种残片判成
    失败会误伤一批正常回合（既有 _test_issue37 / _test_qoder 都盯着这条）。
    """
    if not allowed_names:
        return False
    t = str(text or "")
    names = re.findall(r'"name"\s*:\s*"([^"]{1,120})"', t)
    names += re.findall(r'name\s*=\s*["\']([^"\']{1,120})["\']', t)
    if not names:
        return False
    return all(n not in allowed_names for n in names)


# issue #135：工具协议回声的收尾策略。
#   strict  （默认）= fail-closed：流式响应以 response.failed / error 帧收尾，
#                     让客户端知道这一轮没有完成（correctness 优先）。
#   lenient        = 遇到 InvalidToolArguments 时**按正常结束收尾**（保留已产生的
#                     正文），用于上游爱吐协议残片的模型（实测 Qwen3.8-Flash），
#                     避免 Codex 这类客户端在收到 failed 后直接静默结束对话。
TOOL_ECHO_MODE = (os.environ.get("QD_TOOL_ECHO_MODE") or "strict").strip().lower()
if TOOL_ECHO_MODE not in ("strict", "lenient"):
    TOOL_ECHO_MODE = "strict"


class InvalidToolArguments(UpstreamStatus):
    """已确认是工具协议回声，但内层 arguments 不是合法 JSON（issue #37）。

    fail-closed：不泄漏协议文本、不伪造可执行调用，也**不伪装成成功结束**。
    抛到上层后由各协议的错误出口转成明确失败：
      · chat 流式   -> data: {"error": ...} + [DONE]（不再发正常 finish）
      · Responses   -> response.failed（不再发 response.completed）
      · Anthropic   -> event: error（不再发 message_stop）
      · 非流式      -> HTTP 502 + 标准错误信封
    重试与账号冷却入口都显式排除本异常（见 should_retry_envelope /
    _handle_envelope_account_cooldown）：它不是账号问题，也不该自动重发收费请求。
    """

    def __init__(self):
        super(InvalidToolArguments, self).__init__(502, BAD_ARGS_ERROR_DETAIL)


class EmptyToolEcho(InvalidToolArguments):
    """模型本轮只吐了工具协议回声、没有可用调用（issue #72）。

    继承 InvalidToolArguments：四条协议出口（chat 流式 / Responses / Anthropic /
    非流式 502）已经统一按 fail-closed 处理，重试与账号冷却入口也显式排除。
    区别只是错误文案——告诉客户端「这是残缺回声，请重试」，而不是让它误判完成。
    """

    def __init__(self, chars=0, kind="marker_only"):
        self.kind = kind
        self.integrity_failure = kind in ("missing_tool_name", "missing_tool_call",
                                          "truncated_arguments", "reasoning_only", "empty_tool_turn")
        UpstreamStatus.__init__(
            self, 502, "%s [%d chars, %s]" % (EMPTY_ECHO_ERROR_DETAIL,
                                             int(chars), kind))


_JSON_FENCE = chr(96) * 3


def _strip_json_fence(text):
    """剥掉**首/尾**代码围栏残余（头部 ```[json]`、尾部 ```）。

    为什么需要：流式里 `_find_marker_split` 会把开围栏切进散文，hold 只剩
    「marker + 数组 + 换行 + 闭围栏」；旧实现只在「整串被围栏包住」时才剥，
    于是这种形状被判成非回声 -> 当正文补发（issue #37 fail-closed 的漏检）。
    只动首尾，不碰中段。
    """
    t = (text or "").strip()
    if t.startswith(_JSON_FENCE):
        inner = t[len(_JSON_FENCE):]
        if inner[:4].lower() == "json":
            inner = inner[4:]
        t = inner.lstrip()
    if t.endswith(_JSON_FENCE) and len(t) > len(_JSON_FENCE):
        t = t[:-len(_JSON_FENCE)].rstrip()
    return t


def _echo_with_bad_args(text, allowed_names=None):
    """这段正文是否是「工具协议回声 + 内层参数非法」？（issue #37）

    判定链（全部满足才算）：
      1) 整段是 marker（方/尖括号）+ JSON 数组（可带围栏、可带 DSML 尾标签）；
      2) 外层数组能解析、非空，每项是 dict 且有非空字符串 name；
      3) 声明了 tools 时，所有 name 都在声明集合里；
      4) 至少一个 arguments 是**非空字符串但解析失败**。
    普通散文 / 讨论 marker / 合法 JSON 示例都不会命中。
    """
    if not text:
        return False
    t = _strip_json_fence(_normalize_leak_marker((text or "").strip()))
    # 围栏与 DSML 尾标签可能交替出现（围栏 -> DSML、DSML -> 围栏），
    # 剥两轮直到稳定；每轮都只动**首尾**，中段正文不受影响。
    prev = None
    while prev != t:
        prev = t
        t = _strip_json_fence(_strip_dsml_close_tail(t).strip()).strip()
    # issue #49：允许「散文 + 重复 marker」——取最后一个 marker，跳过连续重复。
    _mpos = t.rfind(LEAK_MARKER)
    if _mpos < 0:
        return False
    t = t[_mpos:]
    while t.startswith(LEAK_MARKER):
        t = t[len(LEAK_MARKER):].lstrip()
    t = _strip_dsml_close_tail(t)
    if t.endswith(_JSON_FENCE) and len(t) > len(_JSON_FENCE):
        t = _strip_dsml_close_tail(t[:-len(_JSON_FENCE)].rstrip())
    rest = t.strip()
    if not (rest.startswith("[") and rest.endswith("]")):
        return False
    try:
        arr = json.loads(rest)
    except Exception:
        return False
    if not isinstance(arr, list) or not arr:
        return False
    bad = 0
    for item in arr:
        if not isinstance(item, dict):
            return False
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return False
        if allowed_names and name.strip() not in allowed_names:
            return False
        if "arguments" not in item:
            return False
        args = item.get("arguments")
        if isinstance(args, (dict, list)):
            continue
        if not isinstance(args, str):
            return False
        if not args.strip():
            continue
        try:
            json.loads(args)
        except Exception:
            # 能修好就不算「坏参数」——交给 parse_leaked_tool_calls 修复后还原，
            # 否则这一轮会被 fail-closed 整轮丢掉（issue: DSH 工具参数 502）。
            if _repair_args_json(args) is None:
                bad += 1
    return bad > 0


# issue #11：tool 角色消息被序列化进 content 时的开标记（写入侧见
# flatten_messages）。网关只写开标记、不写闭标记；模型常自造一个
# 「[工具结果结束]」闭标记，甚至夹带自造的 <system_warning> 残片。
# 更根本的修法是不把 tool 结果序列化进 content（issue #11 作者建议），
# 那属于协议层重构（需要 tool_call_id 配对与结构化 message 透传），
# 本轮不做，只在回读侧兜住已被放大到正文里的回声块。
TOOL_RESULT_MARKER = "[工具结果"
TOOL_RESULT_CLOSE = "[工具结果结束]"

# issue #16-B1：跨帧补全窗口 = 最长标记长度 - 1（LEAK_MARKER 18 字符 -> 17）。
# 只有帧/缓冲**末尾**的这 17 个字符内匹配到某个标记的真前缀时才暂留；
# 下一帧一旦证伪立即补发（fail-open），不会无限压住正常文本。
_MARKER_HOLD_WINDOW = max(len(LEAK_MARKER), len(LEAK_MARKER_ALT),
                           len(TOOL_RESULT_MARKER)) - 1

# issue #16 兜底：hold 缓冲上限。正常文本长期"看起来仍是候选"时（模型写超长
# JSON/正则示例），hold 会持续增长 -> 内存与首字延迟风险。超限即 fail-open
# 放行并记一条 WARN：宁可漏一次，也不无限压住客户端。
_HOLD_MAX_CHARS = 32768            # 32KB（真实回声块远小于此；见档案 §13）
_HOLD_MAX_FRAMES = 200             # 连续空转帧上限（帧不增长才算一帧）


_ECHO_LOOSE_RE = re.compile(
    r'^\[\s*\{\s*"name"\s*:\s*"([^"]{1,200})"\s*,\s*"arguments"\s*:\s*"(.*)"\s*\}\s*\]$',
    re.DOTALL)


def _repair_json_quotes(text):
    """把「内层未转义的双引号」补上转义（issue #60）。

    模型会写出 `"arguments": "{"filename":"a.png"}"` —— 内部引号没转义，
    连外层数组都不是合法 JSON。这里先把已转义的占位，再把裸引号转义，复原。
    """
    ph = "\x00"
    s = (text or "").replace('\\"', ph)
    s = s.replace('"', '\\"')
    return s.replace(ph, '\\"')


def _repair_args_json(raw):
    """尽量把模型写坏的 arguments 修成合法 JSON（issue: DSH 工具参数 502）。

    模型高频写法：内层双引号未转义（`"command":"echo "hi" && ls"`）、尾随逗号。
    依次尝试：严格 -> 转义内层裸引号 -> 去尾随逗号 -> 两者叠加。
    返回修复后的字符串；都修不好返回 None（调用方继续按失败处理，不伪造调用）。
    """
    text = str(raw or "")
    if not text.strip():
        return ""
    cands = [text]
    try:
        cands.append(_repair_json_quotes(text))
    except Exception:
        pass
    # 更强的一档：按 JSON 状态机只转义「未转义的内层引号」（_escape_unescaped_quotes，
    # 与 <tool_call> 解析同源）。模型写 `"command":"echo "hi" && ls"` 时这一步能救回来。
    try:
        _esc = _escape_unescaped_quotes(text)
        if _esc not in cands:
            cands.append(_esc)
    except Exception:
        pass
    for base in list(cands):
        cands.append(re.sub(r",\s*([}\]])", r"\1", base))
    for cand in cands:
        try:
            json.loads(cand)
            return cand
        except Exception:
            continue
    return None


def _salvage_echo_array(rest):
    """宽松重建「[{"name":…,"arguments":"<未转义 JSON>"}]」（issue #60）。

    严格 json.loads 失败时才调用；只处理**单调用**形态（模型复述几乎都是单调用），
    重建后再交给调用方做同一套严格校验（name 命中声明、arguments 合法）。
    """
    m = _ECHO_LOOSE_RE.match((rest or "").strip())
    if not m:
        return None
    name, raw_args = m.group(1), m.group(2)
    for cand in (raw_args, _repair_json_quotes(raw_args)):
        try:
            json.loads(cand)
            return [{"name": name, "arguments": cand}]
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 伪 XML 工具调用：`<tool_call> Name {json} </tool_call>`（issue #137）
# ---------------------------------------------------------------------------
# 上游（尤其自定义供应商 / 经转换层中转的模型）会吐这种非标准形态：
#   [assistant 请求调用工具] <tool_call> Bash {"command":"ls -la"} </tool_call>
# 它既不是 OpenAI 结构化 tool_calls、也不是 DSML、也不是网关自己的
# marker+JSON 数组 —— 三个既有解析器全部认不出，整段被判定成「无法还原的
# 协议残片」，流式中途 fail-closed，客户端看到的就是整轮静默中断。
TOOL_CALL_OPEN = "<tool_call"
_CALL_TAG_OPEN_RE = re.compile(r"<\s*tool_call\s*>", re.IGNORECASE)
_CALL_TAG_OPEN_ATTR_RE = re.compile(
    r"<\s*tool_call\b([^>]*)>", re.IGNORECASE)
_CALL_TAG_CLOSE_RE = re.compile(r"<\s*/\s*tool_call\s*>", re.IGNORECASE)
_CALL_TAG_ATTR_NAME_RE = re.compile(
    r"""\b(?:name|tool|tool_name|function)\s*=\s*(?:"([^"]+)"|'([^']+)')""",
    re.IGNORECASE)
_CALL_TAG_NAME_RE = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_.:\-]*)\s*(?=[({\[]|$)")
# 形态 D：名字后可跟逗号/冒号/等号分隔符（`Bash, {...}` / `Bash: {...}`）
_CALL_TAG_LEAD_RE = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_.:\-]*)\s*(?=[,:=]|[({\[]|$)")


def _escape_unescaped_quotes(s):
    """把 JSON 字符串值里**未转义**的双引号补上转义。

    模型写 `<tool_call>Bash {"command":"echo "hi" && ls"}</tool_call>` 时，内层
    引号没有转义，整个对象都不是合法 JSON。判据：字符串内遇到的 `"`，只有其后
    第一个非空白字符是 `, } ] :` 或文本结束才算闭合引号，否则是内层字面引号。
    已经写成 `\"` 的保持原样。
    """
    out, in_str, i, n = [], False, 0, len(s)
    while i < n:
        c = s[i]
        if not in_str:
            out.append(c)
            if c == '"':
                in_str = True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(s[i:i + 2])      # 已成对的转义序列整体搬运
            i += 2
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ",}]:":
                out.append(c)
                in_str = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _loads_lenient(raw):
    """尽量把工具参数文本解析成 JSON；全部尝试失败返回 None。

    依次尝试：严格解析 -> 去尾随逗号 -> 转义内层裸引号 -> 两者叠加。
    与 _salvage_echo_array 同一定位：模型复述的参数常有未转义引号。
    """
    t = str(raw or "").strip()
    if not t:
        return {}
    no_trailing = re.sub(r",\s*([}\]])", r"\1", t)
    escaped = _escape_unescaped_quotes(t)
    cands = [t]
    if no_trailing != t:
        cands.append(no_trailing)
    if escaped != t:
        cands.append(escaped)
        cands.append(re.sub(r",\s*([}\]])", r"\1", escaped))
    for cand in cands:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _make_tool_call(name, args):
    """组装成 OpenAI 结构化 tool_call；参数不是合法 JSON 时返回 None。"""
    name = str(name or "").strip().strip("\"'`")
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_.:\-]*$", name):
        return None
    if isinstance(args, (dict, list)):
        argstr = json.dumps(args, ensure_ascii=False)
    elif isinstance(args, str):
        argstr = args.strip() or "{}"
    elif args is None:
        argstr = "{}"
    else:
        argstr = json.dumps(args, ensure_ascii=False)
    try:
        json.loads(argstr)
    except Exception:
        return None
    return {"id": _new_id("call_"), "type": "function",
            "function": {"name": name, "arguments": argstr}}


def _parse_one_call_tag(body, attr_name=None):
    """单个标签块 -> tool_call：

    形态 A：`{"name": "Bash", "arguments": {...}}`（JSON 对象）
    形态 B：`Bash {"command": "ls"}` / `Bash({...})` / 只有工具名
    形态 C：`<tool_call name="Bash">{...}</tool_call>`（名字在属性里）
    形态 D：`Bash, {"command":"ls"}` / `Bash: {...}`（名字后用逗号/冒号分隔）
    """
    b = _strip_json_fence((body or "").strip())
    if attr_name:
        # 形态 C：属性给了工具名，正文就是参数本身
        b = b.lstrip("=:").strip()
        if not b:
            return _make_tool_call(attr_name, {})
        args = _loads_lenient(b)
        if args is None:
            return None
        return _make_tool_call(attr_name, args)
    if not b:
        return None
    if b.startswith("{"):
        obj = _loads_lenient(b)
        if not isinstance(obj, dict):
            return None
        name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
        if not (isinstance(name, str) and name.strip()):
            return None        # 只有一个裸参数对象，没有工具名 -> 无法还原
        for key in ("arguments", "input", "args", "parameters"):
            if key in obj:
                return _make_tool_call(name, obj.get(key))
        return _make_tool_call(name, {})
    # 形态 D 的探测必须在 _CALL_TAG_NAME_RE 之前：后者的前瞻要求「名后紧跟
    # ( { [ 或结尾」，`Bash, {…}` / `Bash: {…}` 会整个 match 失败。
    m = _CALL_TAG_LEAD_RE.match(b)
    if not m:
        return None
    # 字符类含 `:` 是为了命名空间工具（`ns:tool`）；但 `Read: {…}` 里的冒号
    # 是分隔符，会被贪心吃进名字。只剥**尾**冒号，命名空间名不受影响。
    name = m.group(1).rstrip(":")
    rest = b[m.end():].lstrip(",:=").strip()
    if rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1].strip()
    if not rest:
        return _make_tool_call(name, {})
    args = _loads_lenient(rest)
    if args is None:
        return None
    return _make_tool_call(name, args)


def parse_tool_call_tags(text, allowed_names=None):
    """把 `<tool_call> Name {json} </tool_call>` 还原成结构化 tool_calls。

    严格守卫（避免把「讲解这个格式」的普通回复误判成一次调用）：
      1) 至少一个标签块能解析出工具名 + 合法 JSON 参数；
      2) 本次请求声明了 tools 时，解析出的工具名必须全部命中声明集合；
      3) 最后一个标签块之后只能剩空白 / 围栏残片 —— 真实泄漏永远在消息末尾，
         讲解示例后面通常还有说明文字。

    返回 (calls|None, clean_text)；clean_text 是剥掉标签块与网关 marker 后
    的剩余正文（未识别时原样返回 text）。
    """
    t = str(text or "")
    if not t or TOOL_CALL_OPEN not in t.lower():
        return None, text
    spans, calls, pos = [], [], 0
    while True:
        m = _CALL_TAG_OPEN_ATTR_RE.search(t, pos)
        if not m:
            break
        attr_name = None
        for am in _CALL_TAG_ATTR_NAME_RE.finditer(m.group(1) or ""):
            attr_name = am.group(1) or am.group(2)
            break
        close = _CALL_TAG_CLOSE_RE.search(t, m.end())
        nxt = _CALL_TAG_OPEN_ATTR_RE.search(t, m.end())
        if nxt and (close is None or nxt.start() < close.start()):
            # 缺闭标签（流被截断）：拿下一个开标签前的文本当参数
            body, end = t[m.end():nxt.start()], nxt.start()
        elif close is not None:
            body, end = t[m.end():close.start()], close.end()
        else:
            body, end = t[m.end():], len(t)
        call = _parse_one_call_tag(body, attr_name)
        if call is not None:
            calls.append(call)
            spans.append((m.start(), end))
        pos = max(end, m.end())
    if not calls:
        return None, text
    # 未声明 tools 的会话里 `<tool_call>` 几乎都是在讨论这个格式（教程、提问、
    # 粘贴示例）—— 不还原，交回正文，避免把聊天内容变成客户端无法执行的调用。
    if not allowed_names:
        return None, text
    if not all(c["function"]["name"] in allowed_names for c in calls):
        return None, text
    # 有没有网关 marker 决定了判定宽严：
    #   · 有 marker  -> 模型在复述网关写进历史的协议（真泄漏）；
    #   · 无 marker  -> 只是正文里出现了标签，必须「整段就是一个块」才算，
    #                   否则讲解示例（标签前后都有散文）会被误判成一次真实调用。
    has_marker = bool(_LEAK_MARKER_RE.search(t))
    lead = _LEAK_MARKER_RE.sub("", t[:spans[0][0]]).strip().strip("`").strip()
    if not has_marker and lead:
        return None, text
    # 块之后的残留：允许 marker 变体、围栏、以及**不含协议结构的短散文**
    # （模型常写「以上是本次操作」之类收尾）。带标签/JSON 键的残留仍判失败，
    # 那是真的没剥干净，宁可交回正文也不伪造调用。
    tail_txt = _LEAK_MARKER_RE.sub("", t[spans[-1][1]:]).strip().strip("`").strip()
    if tail_txt and (_LEAK_STRUCT_RE.search(tail_txt)
                     or _CALL_TAG_OPEN_RE.search(tail_txt)):
        return None, text
    if not has_marker and tail_txt:
        return None, text
    clean, last = [], 0
    for start, end in spans:
        clean.append(t[last:start])
        last = end
    clean.append(t[last:])
    out = _LEAK_MARKER_RE.sub("", "".join(clean))
    out = re.sub(r"<\s*assistant\s*请求调用工具\s*>", "", out)
    return calls, out.strip()


def parse_leaked_tool_calls(text, allowed_names=None):
    """把模型复述的「marker + JSON 数组」还原成结构化 tool_calls。

    严格守卫（避免把"讨论该标记的普通回复"误判为工具调用）：
      1) 整段正文 strip 后必须**恰好**是 marker + JSON 数组（可带 ``` 围栏）；
      2) 数组非空，每项 name 为非空字符串、arguments 为合法 JSON（字符串或对象）；
      3) 本次请求声明了 tools 时，所有 name 必须命中声明集合。

    返回 (tool_calls|None, clean_text)：识别成功时 clean_text 为空串
    （整段都是该块）；未识别时原样返回 text。
    """
    t = (text or "").strip()
    # issue #36 follow-up: normalize the angle-bracket marker variant.
    t = _normalize_leak_marker(t)
    # issue #36：整段解析前先剥尾部 DSML 闭合标签残片。
    t = _strip_dsml_close_tail(t)
    # 整段被 ``` 围栏包裹时先剥壳（模型复述时常带围栏）
    if t.startswith("```") and t.endswith("```") and len(t) > 6:
        inner = t[3:]
        if inner[:4].lower() == "json":
            inner = inner[4:]
        t = inner[:-3].strip()
    # issue #49：模型在长会话里会写成「散文 + marker + marker + 数组 + DSML 尾」。
    # 取**最后一个** marker 起算（前面散文作为 clean 返回，由调用方补发），
    # 再剥掉连续重复的 marker 与尾部围栏——这些都不放松后面的严格校验。
    # issue #51：截图里出现「闭标签 + 数组」（无 marker）与「marker + 杂字 + 数组」。
    # 先剥掉前导的 DSML 闭合标签，再决定起点。
    t = re.sub(r"^(?:\s*</[^>]*DSML[^>]*>)+", "", t).strip()
    # issue #60 follow-up：marker 可能写成 [<assistant …] / 尾括号缺失，
    # 用正则取**最后一个**变体，避免每出现一种括号组合就漏一次。
    _mpos, _mend = _last_marker_span(t)
    if _mpos < 0:
        # 无 marker：允许「整段就是 [{name, arguments}] 数组」（严格校验仍在下面）
        if t.startswith("["):
            t = LEAK_MARKER + chr(10) + t     # 复用同一套解析路径
            _mpos, _mend = 0, len(LEAK_MARKER)
        else:
            return None, text
    # 前缀里残留的任何 marker 变体都不能当正文发给客户端。
    prefix = _LEAK_MARKER_RE.sub("", t[:_mpos]).rstrip()
    t = t[_mend:]
    while True:
        _m = _LEAK_MARKER_RE.match(t.lstrip())
        if not _m:
            break
        t = t.lstrip()[_m.end():]
    t = t.lstrip()
    t = _strip_dsml_close_tail(t)
    if t.endswith(_JSON_FENCE) and len(t) > len(_JSON_FENCE):
        t = _strip_dsml_close_tail(t[:-len(_JSON_FENCE)].rstrip())
    rest = t.strip()
    if not (rest.startswith("[") and rest.endswith("]")):
        # issue #51 形态C：marker 后跟杂字（如 ">）、且数组的开头 [ 丢了。
        # 取第一个 { 到最后一个 } 重新包成数组——后续严格校验不变。
        _i = rest.find("{")
        _j = rest.rfind("}")
        if (not rest.startswith("[")) and 0 <= _i < _j:
            _cand = "[" + rest[_i:_j + 1] + "]"
            try:
                json.loads(_cand)
                rest = _cand
            except Exception:
                return None, text
        else:
            return None, text
    try:
        arr = json.loads(rest)
    except Exception:
        # issue #60：内层引号未转义时外层也非法——按单调用形态抢救一次
        arr = _salvage_echo_array(rest)
        if arr is None:
            return None, text
    if not isinstance(arr, list) or not arr:
        return None, text
    calls = []
    for item in arr:
        if not isinstance(item, dict):
            return None, text
        name = item.get("name")
        args = item.get("arguments")
        if not isinstance(name, str) or not name.strip():
            return None, text
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        if not isinstance(args, str):
            return None, text
        if args.strip():
            try:
                json.loads(args)
            except Exception:
                _fixed = _repair_args_json(args)
                if _fixed is None:
                    return None, text
                args = _fixed
                try:
                    RECOVERY_STATS["args_repaired"] =                         RECOVERY_STATS.get("args_repaired", 0) + 1
                except Exception:
                    pass
        calls.append({
            "id": _new_id("call_"),
            "type": "function",
            "function": {"name": name.strip(), "arguments": args},
        })
    if allowed_names:
        if not all(c["function"]["name"] in allowed_names for c in calls):
            return None, text
    return calls, prefix


def _leak_body_offset(text):
    """把「marker 之前的围栏前缀 / marker 之后的正文」解析出来。

    返回 (state, body)：state='pre'（还处在围栏/前缀区，未到 marker）、
    state='body'（已越过 marker，body 为其后文本）、state='no'（显然不是）。
    流式暂存与判真共用同一套前缀规则（含 ``` 与 ```json 围栏）。
    """
    t = _normalize_leak_marker((text or "")).lstrip()
    if not t or LEAK_MARKER.startswith(t):
        return "pre", None
    _m = _LEAK_MARKER_RE.match(t)
    if _m:
        # issue #49：模型会连写两个 marker（数组在第二个之后）——跳过重复的，
        # 否则 hold 会被判成「body 不是数组」而在流式中途证伪、整块补发。
        b = t[_m.end():].lstrip()
        while True:
            _m2 = _LEAK_MARKER_RE.match(b)
            if not _m2:
                break
            b = b[_m2.end():].lstrip()
        # 重复 marker 还没打完（跨帧）——仍算候选，内容留空等下一帧。
        if b and _marker_prefix_ok(b):
            return "body", ""
        return "body", b
    # issue #60 follow-up：任意括号组合的**真前缀**（跨帧进行中）同样算 pre——
    # 1 字符/帧时 '[' 或 '<' 单独到达，不能当散文放出（否则 marker 开头丢失）。
    if _marker_prefix_ok(t):
        return "pre", None
    if not t.startswith("`"):
        return "no", None
    if len(t) < 3:
        return ("pre", None) if "```".startswith(t) else ("no", None)
    if not t.startswith("```"):
        return "no", None
    rest = t[3:]
    low = rest.lstrip().lower()
    if low.startswith("json"):
        after = rest.lstrip()[4:].lstrip()
    elif LEAK_MARKER.startswith(low) or low.startswith(LEAK_MARKER):
        after = rest.lstrip()          # 无语言标记的 ``` 围栏
    elif "json".startswith(low):
        return "pre", None             # 语言标记还没打完（j/js/json）
    else:
        return "no", None
    if LEAK_MARKER.startswith(after):
        return "pre", None
    if after.startswith(LEAK_MARKER):
        return "body", after[len(LEAK_MARKER):]
    return "no", None


def _leak_prefix_hold(text):
    """文本仍可能是泄漏块的前缀？（流式 hold-back 用）"""
    return _leak_body_offset(text)[0] != "no"


def _tool_names_from_payload(payload):
    """取客户端本次请求声明的工具名集合（回读守卫用）。"""
    names = set()
    for t in (payload or {}).get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name") or ""
        if name:
            names.add(str(name))
    return names or None


def _json_array_prefix_ok(text):
    """text 是否仍可能扩展成合法 JSON 数组字面量（流式暂存判定用）。"""
    s = (text or "").strip()
    if not s:
        return True
    if not s.startswith("["):
        return False
    try:
        json.loads(s)
        return True
    except Exception as exc:
        msg = str(exc)
        # 未闭合字符串的报错位置在字符串开头，但仍可继续扩展
        if msg.startswith("Unterminated string"):
            return True
        pos = getattr(exc, "pos", None)
        if pos is None:
            return False
        if msg.startswith("Invalid \\uXXXX escape"):
            # issue #16-B3：截断落在未写完的转义里（"...\\u6" / "...\\u63a2"）。
            # json.loads 的报错位置在**反斜杠**处而非末尾，旧的
            # `pos >= len(s) - 1` 兜底会判 False -> 不吞 -> 泄漏。
            # 「未写完」（hex 不足）与「彻底非法」（如 \\uZZZZ）在异常消息层面
            # 无法区分，一律宽松 hold —— 流末仍会走吞掉/补发判定（parse_leaked /
            # _leaked_partial_droppable），宽松只影响延迟、不改变最终结论。
            # 实测（Python 3.13）：pos 指向 'u' 本身（如 s[pos-1] == "\\"）；
            # 为稳妥两种位置都检查。
            return (s[max(0, pos - 1):pos + 1] == "\\u"
                    or s[pos:pos + 2] == "\\u")
        # issue #60: 内层 arguments 的引号未转义时，json 会在数组中途报
        # "Expecting , delimiter" / "Expecting property name"。这不是截断，
        # 而是要靠流末 salvage 重建——继续 hold，别中途证伪漏成正文。
        if ("arguments" in s and (
                msg.startswith("Expecting ',' delimiter")
                or msg.startswith("Expecting property name"))):
            return True
        if msg.startswith("Extra data"):
            # 数组已完整，尾随的只能是（可能还没打完的）``` 收尾围栏
            tail = s[pos:].strip()
            # issue #36 follow-up: the tail may also be upstream DSML
            # close tags (complete, or cut mid-tag by a frame boundary
            # before the closing >). Hold those too - the stream-end
            # parse strips them; leniency only delays, never changes
            # the final verdict.
            if (not tail) or "```".startswith(tail):
                return True
            # issue #37 follow-up：闭围栏**之后**还可能跟 DSML 尾标签。小分块时
            # 会出现「``` + 换行 + 半截 </…」——旧逻辑直接喂给
            # _dsml_tail_holdable，它看到开头是反引号就判 False，于是候选被
            # 中途证伪、整块当正文补发（真·泄漏）。先剥掉完整闭围栏再看剩余。
            if tail.startswith(_JSON_FENCE):
                tail = tail[len(_JSON_FENCE):].strip()
                if not tail:
                    return True
            return _dsml_tail_holdable(tail)
        # 语法错误点落在末尾（未闭合括号等）→ 还允许继续扩展
        return pos >= len(s) - 1


def _find_marker_split(text, allow_prefix=True):
    """把正文切成 (立即发出, 暂存候选)：在首个完整标记处切开（issue #16-B1）。

    - 命中完整标记 -> (标记之前的散文, 标记及其后)；
    - allow_prefix=True 且**末尾**是某标记的真前缀（长度 < 标记）时，把这段
      前缀留给下一帧（跨帧补全窗口 _MARKER_HOLD_WINDOW）；
    - 无命中 -> (text, "")。

    背景：服务端 delta 切分不可控，"散文 + marker" 可能落在同一帧，而
    _leak_prefix_hold 只认「整帧以标记开头」-> 整帧被透传 -> 泄漏。
    """
    t = _normalize_leak_marker(text or "")
    if not t:
        return "", ""
    best = -1
    # issue #60 follow-up：任意括号变体（[<assistant …] / 尾括号缺失）都要能切开，
    # 否则「散文 + marker」同帧到达时整块被当正文发出去。
    _m = _LEAK_MARKER_RE.search(t)
    if _m:
        best = _m.start()
    i = t.find(TOOL_RESULT_MARKER)
    if i != -1 and (best == -1 or i < best):
        best = i
    if best != -1:
        return t[:best], t[best:]
    if not allow_prefix:
        return t, ""
    tail = t[-_MARKER_HOLD_WINDOW:] if len(t) > _MARKER_HOLD_WINDOW else t
    for n in range(len(tail), 0, -1):
        seg = tail[-n:]
        if _marker_prefix_ok(seg) or TOOL_RESULT_MARKER.startswith(seg):
            return t[:-n], t[-n:]
    return t, ""


def _marker_starts_at(text):
    """text 是否以某标记开头，或整段就是它的真前缀（跨帧补全中）。"""
    t = text or ""
    if not t:
        return True
    # issue #60 follow-up：括号变体（[<assistant …] / 缺尾括号）同样算命中；
    # 单靠 startswith(常量) 会让大块开头的变体直接漏出去。
    if _LEAK_MARKER_RE.match(t) or _marker_prefix_ok(t):
        return True
    for mk in (LEAK_MARKER, LEAK_MARKER_ALT, TOOL_RESULT_MARKER):
        if t.startswith(mk):
            return True
        if len(t) < len(mk) and mk.startswith(t):
            return True
    return False


def _tool_tag_candidate(text):
    """这段文本是否仍是「`<tool_call> Name {json} </tool_call>`」候选（issue #137）。

    只对**块形态本身**判定，不管前面的 marker：
      · 标签还没打完（"<" "<tool_ca" …）-> 仍是候选，等下一帧；
      · 标签已开、闭标签未到 -> 仍是候选；
      · 闭标签已到 -> 之后只能剩空白/围栏/marker，出现别的字即证伪
        （"例如 <tool_call>…</tool_call> 就是这样" 这类讲解不会被吞）。
    """
    t = (text or "").lstrip()
    if not t:
        return True
    low = t.lower()
    if TOOL_CALL_OPEN.startswith(low):
        return True
    if not low.startswith(TOOL_CALL_OPEN):
        return False
    # "<tool_callXXX" 只有紧跟分隔符才算标签：`<tool_calling>` 是普通词。
    nxt = low[len(TOOL_CALL_OPEN):len(TOOL_CALL_OPEN) + 1]
    if nxt and nxt not in (">", " ", "\t", "\n", "\r", "/"):
        return False
    # 开标签可以带属性（`<tool_call name="Bash">`）；只要 `>` 还没到就继续等。
    if ">" not in t:
        return True
    if not _CALL_TAG_OPEN_ATTR_RE.match(t):
        return False
    close = _CALL_TAG_CLOSE_RE.search(t)
    if close is None:
        return True
    rest = _LEAK_MARKER_RE.sub("", t[close.end():]).strip().strip("`").strip()
    if not rest:
        return True
    # 块后已出现正文：只要不含协议结构就仍是候选（收尾散文很常见）
    return _LEAK_STRUCT_RE.search(rest) is None


def _still_leak_candidate(text):
    """整段文本目前是否仍是「marker + JSON 数组」候选？（流式暂存判定用）"""
    state, body = _leak_body_offset(text)
    if state == "pre":
        return True
    if state == "no":
        return False
    rest = (body or "").lstrip()
    if not rest:
        return True
    # issue #137：伪 XML 工具调用（`<tool_call>Name {json}</tool_call>`）不是
    # JSON 数组，但同属「marker + 协议块」，必须继续 hold 到流末再判定；
    # 否则第二帧就被证伪、整块当正文补发（或中途 fail-closed）。
    if _tool_tag_candidate(rest):
        return True
    # issue #51：marker 之后可能先跟一小段杂字（">、=、: 等）再进数组，
    # 且数组开头的 [ 可能丢了（直接 {"name":...}）。两种都保持候选，
    # 由流末 parse（含补括号启发式）决定成败。
    # issue #60 follow-up：marker 变体可能多带一个 ]（[assistant …]]），也算杂字。
    _trimmed = rest.lstrip(chr(34) + chr(39) + ">=: ]").lstrip()
    if not _trimmed:
        return True          # 目前全是杂字（还没进数组）：继续等
    if _trimmed[0] in "[{":
        rest = _trimmed
        if rest.startswith("{"):
            return True
    if rest.endswith("```"):
        rest = rest[:-3]
    return _json_array_prefix_ok(rest)


def _leaked_partial_droppable(text, allowed_names=None):
    """issue #9：正文是网关自己写入历史的工具调用回声、但被截断（JSON 不完整/
    非法）时，应吞掉整段而不是透传给终端用户。三个条件同时成立才吞：
      1) strip 后以 LEAK_MARKER 开头（真实散文回复几乎不会这样开头）；
      2) 本次请求声明了 tools（allowed_names 非空）——与恢复路径同前置条件；
      3) marker 之后（剥掉围栏后）仍是 JSON 数组字面量的前缀
         （复用现成的 _json_array_prefix_ok）——即"本该是数组，只是没写完"。
    返回 True = 应吞掉正文（输出空 content）。

    完整且合法的 JSON 数组不算：那种情况下恢复失败另有原因（工具名未声明 /
    arguments 非法），保持既有 fail-open 透传语义（见 _test_qoder.py 的 [27]
    断言"未声明工具名 -> 不吞正文"），只有"数组本身没写完 / 非法"才吞。
    """
    if not text or not allowed_names:
        return False
    t = text.strip()
    if not t:
        return False
    if t == LEAK_MARKER:
        # 只有 marker、连数组都没开始写：判据 3 的退化情形，同样是回声。
        return True
    state, body = _leak_body_offset(t)
    if state != "body":
        return False
    rest = (body or "").strip()
    if rest.endswith("```"):
        rest = rest[:-3].strip()
    if not _json_array_prefix_ok(rest):
        return False
    try:
        json.loads(rest)
        return False
    except Exception:
        return True


def _tool_echo_head(text):
    """切分「[工具结果<name>]」开标记行 → (body, ok)；ok=False 表示不是合法开标记。"""
    t = (text or "").strip()
    if not t.startswith(TOOL_RESULT_MARKER):
        return None, False
    rest = t[len(TOOL_RESULT_MARKER):]
    close = rest.find("]")
    if close == -1 or "\n" in rest[:close]:
        return None, False          # 标记行未闭合 / 换行后才有 ]：非本网关形态
    return rest[close + 1:], True


def _tool_echo_droppable(text, allowed_names=None):
    """issue #11：正文是「[工具结果...]」回声块时吞掉整段（与 issue #9 同形态）。

    三个条件同时成立才吞：
      1) strip 后是合法开标记行（TOOL_RESULT_MARKER + 可选 name + ]）；
      2) 本次请求声明了 tools（allowed_names 非空）——与既有回读同一前置；
      3) 形态自洽：出现模型自造的闭标记 TOOL_RESULT_CLOSE，或标记之后（忽略空白）
         以 JSON/标签起始字符开头（含被截断、永不闭合的块）。

    为什么吞掉而不是还原成 tool 消息：还原需要把回声与历史上的 tool_call_id
    配对（网关现在压平进 content 时并未保留 call_id），本轮不引入该复杂度，
    只做「不把内部协议文本交给终端用户」。

    不吞（fail-open）：不以标记开头；标记后跟自然语言散文；未声明 tools。
    """
    if not text or not allowed_names:
        return False
    body, ok = _tool_echo_head(text)
    if not ok:
        return False
    if TOOL_RESULT_CLOSE in body:
        return True
    rest = body.lstrip()
    if not rest:
        return True                 # 只有开标记：退化形态（与 #9 的纯 marker 一致）
    return rest[0] in "{[\"<"


def _tool_echo_prefix_hold(text):
    """流式 hold-back：这段缓冲仍可能是 issue #11 的回声块吗？（宽松版判据）"""
    t = (text or "").lstrip()
    if not t:
        return True
    if TOOL_RESULT_MARKER.startswith(t):
        return True                 # 标记本身还没打完
    body, ok = _tool_echo_head(t)
    if not ok:
        return False                # 不是标记形态 -> 证伪，立即放行
    if TOOL_RESULT_CLOSE in body:
        return True
    rest = body.lstrip()
    if not rest:
        return True
    return rest[0] in "{[\"<"


def _echo_hold_candidate(text):
    """统一暂存判定：仍可能是 #8 数组回声或 #11 工具结果回声？"""
    return _still_leak_candidate(text) or _tool_echo_prefix_hold(text)


def _carry_delta(delta):
    """取一帧里「除 content 外的增量」（tool_calls / reasoning_content）。

    issue #124：合成正文帧时只带 content，会把**同一帧里的** tool_calls 起点
    （带 function.name）整个丢掉 —— 下游只收到后续参数帧，index 上永远没有
    name，ai-sdk 系客户端收尾时抛 "tool call delta is missing id or name"。
    正文要被改写时，这两类增量必须原样搬运到合成帧里。
    """
    if not isinstance(delta, dict):
        return {}
    out = {}
    tcs = delta.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        out["tool_calls"] = tcs
    rc = delta.get("reasoning_content")
    if isinstance(rc, str) and rc:
        out["reasoning_content"] = rc
    return out


def _content_chunk(meta, piece, delta=None, finish_reason=None):
    """正文帧 = content + 该帧原有的 tool_calls/reasoning 增量（issue #124）。"""
    body = {"content": piece}
    body.update(_carry_delta(delta))
    return _leak_chunk_frame(meta, body, finish_reason)



def _leak_chunk_frame(meta, delta, finish_reason=None):
    """按流式 chat.completion.chunk 形态合成一帧（沿用上游的 id/model）。"""
    payload = {
        "id": meta.get("id") or "chatcmpl-qoder",
        "object": "chat.completion.chunk",
        "created": meta.get("created") or int(time.time()),
        "model": meta.get("model") or "",
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish_reason}],
    }
    return ("data: " + json.dumps(payload, ensure_ascii=False)
            + "\n\n").encode("utf-8")


def _frame_without_content(payload):
    """复制一帧并去掉 delta.content（保留 tool_calls/reasoning/收尾信息）。"""
    out = dict(payload)
    choices = []
    for ch in payload.get("choices") or []:
        if not isinstance(ch, dict):
            choices.append(ch)
            continue
        ch2 = dict(ch)
        delta = ch2.get("delta")
        if isinstance(delta, dict) and "content" in delta:
            d2 = dict(delta)
            d2.pop("content", None)
            ch2["delta"] = d2
        choices.append(ch2)
    out["choices"] = choices
    return out


def _sse_normalize_tool_call_indices(frames):
    states = {}
    for raw in frames:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        data = strip_data_prefix(text)
        try:
            payload = json.loads(data) if data and data != "[DONE]" else None
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            yield raw
            continue
        changed = False
        for choice_position, choice in enumerate(payload["choices"]):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            calls = delta.get("tool_calls") if isinstance(delta, dict) else None
            if not isinstance(calls, list):
                continue
            choice_index = choice.get("index")
            if not isinstance(choice_index, int) or isinstance(choice_index, bool):
                choice_index = choice_position
            state = states.setdefault(choice_index, {"ids": {}, "active": {}, "slots": {}})
            for call_position, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                wire_index = call.get("index")
                if not isinstance(wire_index, int) or isinstance(wire_index, bool) or wire_index < 0:
                    wire_index = call_position
                call_id = call.get("id")
                call_id = call_id if isinstance(call_id, str) and call_id.strip() else ""
                slot = state["ids"].get(call_id) if call_id else None
                if slot is None:
                    slot = state["active"].get(wire_index)
                    if slot is not None and call_id and slot["id"] and slot["id"] != call_id:
                        slot = None
                if slot is None:
                    normalized_index = wire_index
                    if normalized_index in state["slots"]:
                        normalized_index = max(state["slots"], default=-1) + 1
                    slot = {"index": normalized_index, "id": call_id}
                    state["slots"][normalized_index] = slot
                if call_id:
                    slot["id"] = call_id
                    state["ids"][call_id] = slot
                state["active"][wire_index] = slot
                if call.get("index") != slot["index"] or isinstance(call.get("index"), bool):
                    call["index"] = slot["index"]
                    changed = True
        if changed:
            yield ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
        else:
            yield raw


def _sse_fill_tool_call_ids(frames):
    """流式出口保证：tool_call 增量的 id 必须非空（issue #47）。

    上游偶尔给出**缺 id**（甚至缺 function）的增量帧，网关此前原样透传；
    下游客户端会把 id="" 落盘，而它的会话加载器要求非空字符串 —— 一条空 id
    （校验发生在「从 seq 0 顺序回放整份会话」的状态机里，不是跳过坏行）
    就让整份会话报废。三层客户端都把 "" 一路放行，**只有网关出口能拦**。

    规则：
      - 上游给了 id -> 原样保留（不改写、不重新造号），并让同 index 的后续
        分帧沿用同一个 id（下游靠 id 把 tool_call 与 tool_result 配对）；
      - 缺 id -> 补 _new_id("call_")，**按 index 记忆**（分帧到达时换号会断链）；
      - 非 tool_call 帧（[DONE] / 纯正文 / 非 JSON）**字节级原样透传**。
    放在出口而不是各产出点：流式侧有多个来源（上游原生增量 / #8 泄漏回读 /
    DSML 回放），逐个补必然漏，这是唯一合流点。
    """
    ids = {}          # key -> 已分配的 id
    named = set()     # 该 key 是否已经「用掉」过一次 function.name（新调用的起点）

    def _key_for(tc, pos, n_calls):
        """tool_call 的**稳定**记忆键（issue #47 补充）。

        1) 有 int index -> 用它（协议正解）；
        2) 缺 index -> 用**帧内位置**：多调用同帧各得不同键（并行不再撞号），
           单调用恒为 0（与 index=0 归一到同一键，避免 None/0 两套记忆）；
        3) index 是不可哈希/畸形值（list/dict/str）-> 同样按位置，
           **绝不让它进 dict key**（否则 TypeError 会一路冒到 socket）。
        """
        idx = tc.get("index")
        if isinstance(idx, int) and not isinstance(idx, bool):
            return ("i", idx)
        return ("i", pos if n_calls > 1 else 0)

    for raw in frames:
        text = (raw.decode("utf-8", "replace")
                if isinstance(raw, bytes) else raw)
        data = strip_data_prefix(text)
        if not data or data == "[DONE]":
            yield raw
            continue
        try:
            payload = json.loads(data)
        except Exception:
            yield raw
            continue
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list):
            yield raw
            continue
        touched = False
        try:
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta")
                if not isinstance(delta, dict):
                    continue
                calls = delta.get("tool_calls")
                if not isinstance(calls, list):
                    continue
                for pos, tc in enumerate(calls):
                    if not isinstance(tc, dict):
                        continue
                    key = _key_for(tc, pos, len(calls))
                    cur = tc.get("id")
                    fn = tc.get("function")
                    name = str((fn or {}).get("name") or "").strip() \
                        if isinstance(fn, dict) else ""
                    if isinstance(cur, str) and cur.strip():
                        # 上游给了 id：原样保留；**覆盖式**记忆让后到的真 id
                        # 收敛（旧实现 setdefault 会让后续帧回落到旧补值）。
                        ids[key] = cur
                        if name:
                            named.add(key)
                        continue
                    # 同一 key 第二次带 name = 无 index 场景下的**另一个**调用
                    # （串行两次调用都落在位置 0），必须换号，否则配对断裂。
                    if name and key in named:
                        ids[key] = _new_id("call_")
                    new_id = ids.get(key) or _new_id("call_")
                    ids[key] = new_id
                    if name:
                        named.add(key)
                    tc["id"] = new_id
                    touched = True
        except Exception:
            # 兜底：任何意外都不得让异常冒到 socket（fail-open，与其余分支一致）
            yield raw
            continue
        if touched:
            yield ("data: " + json.dumps(payload, ensure_ascii=False)
                   + "\n\n").encode("utf-8")
        else:
            yield raw


def _recover_leaked_tool_calls_inner(frames, allowed_names=None):
    """流式回读（issue #8）：把模型逐块吐出的「marker + JSON 数组」正文恢复
    成结构化 tool_calls 增量帧；其余内容原样透传（fail-open）。

    与 parse_leaked_tool_calls 同一套严格守卫，另加：
      - 只有当已输出的正文仍是该块的「前缀/候选」时才暂存（期间不发正文）；
      - 候选被证伪（出现别的字 / JSON 非法）→ 立刻把暂存整段补发为正文；
      - 收尾帧暂存到流末，识别成功时改写 finish_reason=tool_calls；
      - 上游中断时暂存直接丢弃（正文未发，交给上游重试/错误帧处理）。
    """
    hold = None          # 正在暂存的候选正文（None = 未暂存）
    hold_frames = 0      # 连续未增长帧数（空转保护，issue #16/#36 后续）
    finished = None      # 暂存的收尾帧：(payload, 原 finish_reason)
    tail_usage = []      # hold 期间到达的 usage 垫底帧（流末补发）
    recovered = False
    meta = {"id": None, "model": None, "created": None}

    for raw in frames:
        text = (raw.decode("utf-8", "replace")
                if isinstance(raw, bytes) else raw)
        data = strip_data_prefix(text)
        payload = None
        if data and data != "[DONE]":
            try:
                payload = json.loads(data)
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            yield raw
            continue
        if payload.get("id"):
            meta["id"] = payload["id"]
        if payload.get("model"):
            meta["model"] = payload["model"]
        if payload.get("created"):
            meta["created"] = payload["created"]
        choices = payload.get("choices") or []
        choice = (choices[0] if choices and isinstance(choices[0], dict)
                  else {})
        delta = choice.get("delta") or {}
        piece_raw = delta.get("content")
        piece = piece_raw if isinstance(piece_raw, str) else ""
        fin = choice.get("finish_reason")
        if piece_raw is not None and not isinstance(piece_raw, str):
            # 非字符串 content（罕见）：不参与暂存判断，原样透传
            if hold is not None:
                yield _content_chunk(meta, hold)   # 随后 yield raw 已携带增量
                hold = None
                hold_frames = 0
            yield raw
            continue
        if hold is not None:
            if not choices and isinstance(payload.get("usage"), dict):
                # 回读暂存期间到达的 usage 垫底帧：没有正文增量、不影响候选
                # 判定，此前会掉进下面的 continue 被静默吞掉 —— 触发回读的
                # 轮次调用方整轮收不到 usage（只剩客户端自估的输入 tokens）。
                # 缓存到流末、跟在收尾帧之后发（保持 usage 垫底的标准顺序）。
                tail_usage.append(payload)
                continue
            hold += piece
            # issue #36 后续：帧上限只防**空转**（连续空帧不增长），有增长的
            # 合法回声块（每帧 1 字符也会推进长度）交给 32KB 字符上限兜底——
            # 否则 201 字符的合法块在 1 字符/帧下会在 200 帧处被误伤 fail-open。
            if piece:
                hold_frames = 0
            else:
                hold_frames += 1
            # issue #36：尾部 DSML 闭合标签是上游协议残片，剥掉再判候选；
            # 只有剥掉后仍是候选时才采纳（讨论 DSML 的普通散文不被误伤）。
            _stripped = _strip_dsml_close_tail(hold)
            if _stripped != hold and _still_leak_candidate(_stripped):
                hold = _stripped
            if _echo_hold_candidate(hold):
                # issue #16 兜底：超限即 fail-open 放行（内存 + 首字延迟）。
                if len(hold) > _HOLD_MAX_CHARS \
                        or hold_frames > _HOLD_MAX_FRAMES:
                    # issue #112：超限放行前先补一次 #72 判定 —— marker-only 的
                    # 空壳块走到这里会被当正文发出去，绕过流末的 fail-closed
                    # （实测日志：released oversized leak hold (21 chars /
                    # 201 frames) — fail-open）。
                    if allowed_names and _marker_only_echo(hold):
                        raise EmptyToolEcho(len(hold), "marker_only_released")
                    log("released oversized leak hold (%d chars / %d frames) "
                        "— fail-open" % (len(hold), hold_frames),
                        level="WARN", tag="chat")
                    yield _content_chunk(meta, hold, None if fin else delta)
                    hold = None
                    hold_frames = 0
                    if fin:
                        finished = (_frame_without_content(payload), fin)
                    continue
                if fin:
                    # 该帧 content 已进入 hold（可能被吞或稍后补发），收尾帧必须
                    # 去掉 content，否则同一段内容会被发两次（issue #16）。
                    finished = (_frame_without_content(payload), fin)
                elif (delta.get("tool_calls")
                      or delta.get("reasoning_content")
                      or delta.get("role")):
                    other = _frame_without_content(payload)
                    yield ("data: "
                           + json.dumps(other, ensure_ascii=False)
                           + "\n\n").encode("utf-8")
                continue
            # issue #37 fail-closed：块虽已「证伪」（后续还有别的字/围栏/DSML
            # 标签），但内容足以认定是**协议回声 + 内层参数非法**时，绝不能
            # 当正文补发——直接升级为协议错误（提前判定，不等流末）。
            if _echo_with_bad_args(hold, allowed_names):
                raise InvalidToolArguments()
            # issue #60 follow-up：散文 + marker 落在同一个大块时，hold 以散文开头
            # 会直接证伪——整块（含协议正文）被当正文补发。这里先把散文前缀放行，
            # 只保留 marker 起的部分继续判候选。
            _last_m = None
            for _mm in _LEAK_MARKER_RE.finditer(hold or ""):
                _last_m = _mm
            if _last_m is not None and _last_m.start() > 0:
                _tail60 = hold[_last_m.start():]
                if _still_leak_candidate(_tail60):
                    _prose60 = hold[:_last_m.start()]
                    if _prose60.strip():
                        yield _content_chunk(meta, _prose60, None if fin else delta)
                    hold = _tail60
                    if fin:
                        finished = (_frame_without_content(payload), fin)
                    continue
            # issue #49 方向 C：候选被证伪 -> 整段当正文补发，这里同样留痕
            # （形态3「marker + <terminal></terminal> 空壳」就走这条：没有 JSON
            # 可恢复，但必须让发生率可见，而不是静默失真）。
            _log_unrecovered_leak(hold, "stream-falsified")
            if _leak_garbage_only(hold):
                # issue #49 形态3：只有标记 + 空壳标签，补发出去只会污染客户端正文
                log("dropped bare tool-protocol residue (%d chars) — issue #49 form3"
                    % len(hold), level="WARN", tag="chat")
            elif (allowed_names
                    and _leak_residue_present(hold)
                    and not parse_dsml_tool_calls(hold)[0]
                    and not _echo_names_all_undeclared(hold, allowed_names)):
                # issue #120 后续：可被 DSML 解析器还原的残片要留给下游
                # _sse_recover_dsml（它才是 DSML 回读者），这里只拦真正还原不了的。
                # 注意：这条分支是**流中途**判定，绝不能把 `_dsml_residue_present`
                # 也算进来 —— 分块投递时 hold 可能只是 DSML 调用的前半截
                # （_test_issue50_51_52 的 B 形态 n=8 就是这样），中途判死会把
                # 本来能恢复的整轮打断；DSML 残片的 fail-closed 留给流末分支。
                log("unrecovered leak residue in tools session (%d chars) — "
                    "failing the turn closed (issue #49)" % len(hold),
                    level="WARN", tag="chat")
                raise EmptyToolEcho(len(hold), "unrecovered_falsified")
            else:
                # 证伪：暂存整段补发为普通正文
                yield _content_chunk(meta, hold, None if fin else delta)
            hold = None
            hold_frames = 0
            if fin:
                    # 该帧 content 已进入 hold（可能被吞或稍后补发），收尾帧必须
                    # 去掉 content，否则同一段内容会被发两次（issue #16）。
                finished = (_frame_without_content(payload), fin)
            continue
        if fin and not piece:
            finished = (payload, fin)
            continue
        if piece:
            # issue #16-B1：散文与 marker 落在同一帧时，_leak_prefix_hold
            # 对整帧判 False（它要求 lstrip 后以标记开头）-> 整帧透传 -> 泄漏。
            # 这里先按标记切分：标记之前的散文**立即发出**（不拖首字延迟），
            # 标记及其后进既有 hold 流程；帧尾的真前缀也在这里被暂留。
            _emit16, _hold16 = _find_marker_split(piece)
            if _hold16:
                if _emit16:
                    yield _content_chunk(meta, _emit16, None if fin else delta)
                hold = _hold16
                hold_frames = 1
                if fin:
                    # 该帧 content 已进入 hold（可能被吞或稍后补发），收尾帧必须
                    # 去掉 content，否则同一段内容会被发两次（issue #16）。
                    finished = (_frame_without_content(payload), fin)
                continue
            if _leak_prefix_hold(piece) or _tool_echo_prefix_hold(piece):
                hold = piece
                hold_frames = 1
                if fin:
                    # 该帧 content 已进入 hold（可能被吞或稍后补发），收尾帧必须
                    # 去掉 content，否则同一段内容会被发两次（issue #16）。
                    finished = (_frame_without_content(payload), fin)
                continue
            # issue #124：同帧里可能还有 tool_calls 起点（带 name）——
            # 只发 content 会把起点整帧吞掉，下游 index 永远缺 name。
            yield _content_chunk(meta, piece, delta, fin)
            continue
        if (finished is not None and hold is None and not choices
                and isinstance(payload.get("usage"), dict)):
            # 上游流尾顺序是 [finish] -> [usage]（OpenAI 标准也是 usage 垫底）。
            # 收尾帧被扣到这里说明回读判定早已结束（hold 为空、不可能再改写
            # finish_reason），继续扣它只会把 usage 顶到 finish 前面 —— 按
            # 「末帧 usage」解析的客户端（OpenAI Chat 格式）就此读不到
            # 输出/缓存 tokens。这里先放行收尾帧再透传 usage，恢复标准顺序。
            _fp, _ffin = finished
            finished = None
            yield ("data: " + json.dumps(_fp, ensure_ascii=False)
                   + "\n\n").encode("utf-8")
        yield raw

    if hold is not None:
        calls, clean = parse_leaked_tool_calls(hold, allowed_names)
        if not calls and TOOL_CALL_OPEN in (hold or "").lower():
            # issue #137：伪 XML 形态（`[marker] <tool_call>Name {json}</tool_call>`）
            calls, clean = parse_tool_call_tags(hold, allowed_names)
            if calls:
                RECOVERY_STATS["tool_tag"] = \
                    RECOVERY_STATS.get("tool_tag", 0) + 1
        if calls:
            recovered = True
            RECOVERY_STATS["leak"] = RECOVERY_STATS.get("leak", 0) + 1
            for i, call in enumerate(calls):
                yield _leak_chunk_frame(meta, {"tool_calls": [{
                    "index": i,
                    "id": call.get("id"),
                    "type": call.get("type") or "function",
                    "function": call.get("function") or {},
                }]})
            if clean and _residue_text_only(clean):
                # issue #120 后续：同上（marker 回声回读成功后的剩余文本）
                log("dropped residue in post-recovery text (%d chars) — issue #49"
                    % len(clean), level="WARN", tag="chat")
            elif clean:
                yield _leak_chunk_frame(meta, {"content": clean})
        elif _echo_with_bad_args(hold, allowed_names):
            # issue #37（fail-closed）**必须先于 #9/#11 判定**：坏参数块的外层
            # 数组是**完整合法**的，只是后面跟着 DSML/围栏残片——这会让
            # _leaked_partial_droppable 误判成"截断回声"并静默吞掉（正常 stop），
            # 既不是失败语义也不是恢复。判定互斥：真截断的数组 parse 不过，
            # 走不到这里（仍由 #9 吞掉）。
            # issue #37（fail-closed）：能确定是工具协议回声（marker + 合法外层
            # 数组 + 已声明工具名），只是内层 arguments 坏了——不泄漏协议、不
            # 伪造可执行调用，也**不以普通 stop 结束**，改为协议级失败。
            log("tool-call echo has invalid JSON arguments (%d chars) — "
                "failing the turn closed (issue #37)" % len(hold),
                level="WARN", tag="chat")
            raise InvalidToolArguments()
        elif _leaked_partial_droppable(hold, allowed_names):
            # issue #72：**不要把「残缺工具回声」当成功结束**。此前吞掉后按
            # stop 收尾，coding agent 会认为任务做完了（静默中断，比报错更难查）。
            # 客户端声明了 tools 且本轮没有一个可用调用 -> fail-closed，让客户端重试。
            _kind = "marker_only" if _marker_only_echo(hold) else "partial_array"
            log("dropped truncated leaked tool-call echo (%d chars, %s, "
                "tools_declared=%d) — failing the turn closed (issue #72)"
                % (len(hold), _kind, len(allowed_names)),
                level="WARN", tag="chat")
            raise EmptyToolEcho(len(hold), _kind)
        elif _tool_echo_droppable(hold, allowed_names):
            # issue #11：模型把 [工具结果...] 回声块当正文吐出（自造闭标记或
            # 未闭合的截断块）——吞掉，不还原（需 tool_call_id 配对）。
            log("dropped leaked tool-result echo (%d chars) — issue #11"
                % len(hold), level="WARN", tag="chat")
        elif _marker_only_echo(hold):
            # 只有标记、没有任何内容：无论如何都不该透传给终端用户
            # （未声明 tools 时无法确认是回声，但「一个孤零零的标记」没有信息量）。
            log("dropped bare tool-call marker (%d chars) — issue #72" % len(hold),
                level="WARN", tag="chat")
        else:
            # issue #49 方向 C：认不出但**看起来就是协议回声**的，必须留痕。
            _log_unrecovered_leak(hold, "stream")
            if _leak_garbage_only(hold):
                log("dropped bare tool-protocol residue (%d chars) — issue #49 form3"
                    % len(hold), level="WARN", tag="chat")
            elif (allowed_names
                    and (_leak_residue_present(hold) or _dsml_residue_present(hold))
                    and not parse_dsml_tool_calls(hold)[0]
                    and not _echo_names_all_undeclared(hold, allowed_names)):
                log("unrecovered leak residue in tools session (%d chars) — "
                    "failing the turn closed (issue #49)" % len(hold),
                    level="WARN", tag="chat")
                raise EmptyToolEcho(len(hold), "unrecovered_leak")
            else:
                yield _content_chunk(meta, hold)   # 流末补发：不再重复增量
    if finished is not None:
        payload, fin = finished
        if recovered:
            payload = json.loads(json.dumps(payload))
            for ch in payload.get("choices") or []:
                if isinstance(ch, dict) and ch.get("finish_reason"):
                    ch["finish_reason"] = "tool_calls"
        yield ("data: " + json.dumps(payload, ensure_ascii=False)
               + "\n\n").encode("utf-8")
    elif recovered:
        yield _leak_chunk_frame(meta, {}, "tool_calls")
    for _up in tail_usage:
        yield ("data: " + json.dumps(_up, ensure_ascii=False)
               + "\n\n").encode("utf-8")


def _dsml_tail_hold(text):
    """返回「疑似 DSML 标签开头」的尾巴（跨帧暂留用，issue #51）。"""
    t = text or ""
    i = t.rfind("<")
    if i < 0 or len(t) - i > 24:
        return ""
    tail = t[i:]
    if ">" in tail:               # 已经是完整标签：不算前缀，别暂留
        return ""
    norm = tail.replace(chr(0xFF5C), "|").upper()
    body = norm[1:]
    if body.startswith("/"):          # 闭标签 </... 也要暂留
        body = body[1:]
    if not body:
        return tail                   # "<" / "</"
    if not body.startswith("|"):
        return ""
    rest = body[1:]
    if not rest:
        return tail
    if "DSML".startswith(rest) or rest.startswith("DSML"):
        return tail
    return ""


def _chunk_with_content(payload, content):
    """Copy only the modified first-choice/delta path; retain other metadata."""
    out = dict(payload)
    choices = list(payload.get("choices") or [])
    first = dict(choices[0])
    delta = dict(first.get("delta") or {})
    delta["content"] = content
    first["delta"] = delta
    choices[0] = first
    out["choices"] = choices
    return out


def _sse_recover_dsml(frames, allowed_names=None):
    """把流式正文里的 DeepSeek DSML 工具调用还原成结构化 tool_calls（issue #51）。

    chat / Anthropic 两条流式路径此前**完全没有** DSML 回读（只有 Responses 有），
    模型直接吐原生 DSML 时整段协议漏成正文。
    策略：平时只暂留「像标签开头」的几个字符；一旦正文出现 DSML 就转入缓冲
    直到流末统一解析——解析出调用则补发散文 + tool_calls + finish=tool_calls；
    解析不出则原样补发（fail-open）并记 WARN。非 DSML 流逐字节不变。
    """
    buf = ""
    tail = ""
    finish = None
    tail_usage = []      # DSML 缓冲期间到达的 usage 垫底帧（流末补发）
    meta = {"id": None, "model": None, "created": None}
    recovered = False

    for raw in frames:
        text = (raw.decode("utf-8", "replace")
                if isinstance(raw, bytes) else raw)
        data = strip_data_prefix(text)
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except Exception:
            yield raw
            continue
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            if (isinstance(payload, dict)
                    and isinstance(payload.get("usage"), dict)):
                if buf or tail:
                    # DSML 缓冲期间到达的 usage 垫底帧：流末可能还要改写
                    # finish_reason，缓存到收尾帧之后补发（不吞、不抢序）。
                    tail_usage.append(payload)
                    continue
                if finish is not None:
                    # usage 垫底帧到达且无缓冲可改写 finish_reason：先放行
                    # 被扣的收尾帧再透传 usage，保持 [finish] -> [usage] ->
                    # [DONE] 标准顺序（同 _recover_leaked_tool_calls_inner）。
                    _fp, _ffin = finish
                    finish = None
                    yield ("data: " + json.dumps(_fp, ensure_ascii=False)
                           + "\n\n").encode("utf-8")
            yield raw
            continue
        if not meta["id"] and payload.get("id"):
            meta["id"] = payload.get("id")
        if not meta["model"] and payload.get("model"):
            meta["model"] = payload.get("model")
        if not meta["created"] and payload.get("created"):
            meta["created"] = payload.get("created")
        ch = choices[0] if isinstance(choices[0], dict) else {}
        delta = ch.get("delta") or {}
        piece = delta.get("content")
        fin = ch.get("finish_reason")
        if not isinstance(piece, str) or not piece:
            if fin:
                finish = (payload, fin)
            else:
                yield raw
            continue
        piece = tail + piece
        tail = ""
        if not buf:
            hold = _dsml_tail_hold(piece)
            body = piece[:len(piece) - len(hold)] if hold else piece
            if "DSML" in body:
                buf = body
                tail = hold
                continue
            if body:
                payload2 = _chunk_with_content(payload, body)
                yield ("data: " + json.dumps(payload2, ensure_ascii=False)
                       + "\n\n").encode("utf-8")
            tail = hold
            continue
        buf += piece

    if buf:
        calls, clean = parse_dsml_tool_calls(buf)
        if not calls:
            # issue #51：闭标签 + 数组 / marker + 杂字 + 数组 也走同一条恢复链
            _lc, _lclean = parse_leaked_tool_calls(buf, allowed_names)
            if _lc:
                calls = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                          "arguments": (c.get("function") or {}).get("arguments")}
                         for c in _lc]
                clean = _lclean
        if calls:
            recovered = True
            RECOVERY_STATS["dsml"] = RECOVERY_STATS.get("dsml", 0) + 1
            if clean and _residue_text_only(clean):
                # issue #120 后续：调用已恢复，但剥离后的剩余文本仍带协议残片
                # （嵌套/重复块导致剥离不干净）—— 丢掉残片，别再喂给客户端。
                log("dropped residue in post-recovery text (%d chars) — issue #49"
                    % len(clean), level="WARN", tag="chat")
            elif clean:
                yield _leak_chunk_frame(meta, {"content": clean})
            for i, call in enumerate(calls):
                yield _leak_chunk_frame(meta, {"tool_calls": [{
                    "index": i,
                    "id": call.get("id") or _new_id("call_"),
                    "type": "function",
                    "function": {"name": call.get("name") or "",
                                 "arguments": call.get("arguments") or "{}"},
                }]})
        else:
            _log_unrecovered_leak(buf, "dsml-stream")
            if _leak_garbage_only(buf):
                log("dropped bare tool-protocol residue (%d chars) — issue #49 form3"
                    % len(buf), level="WARN", tag="chat")
            elif (allowed_names
                    and (_leak_residue_present(buf) or _dsml_residue_present(buf))
                    and not _echo_names_all_undeclared(buf, allowed_names)):
                log("unrecovered DSML leak residue in tools session (%d chars) "
                    "— failing the turn closed (issue #49)" % len(buf),
                    level="WARN", tag="chat")
                raise EmptyToolEcho(len(buf), "unrecovered_dsml")
            else:
                yield _leak_chunk_frame(meta, {"content": buf})
    if tail:
        if _residue_text_only(tail):
            # issue #120 后续（Ultimate 审计 5232 行）：tail 是流末 holdback 的
            # 尾巴，原先无条件当正文补发 —— 截在 `<|DSML|inv` 中间、或第二个
            # DSML 块的开头都会从这条漏出去。含残片就丢。
            log("dropped residue tail (%d chars) — issue #49" % len(tail),
                level="WARN", tag="chat")
        else:
            yield _leak_chunk_frame(meta, {"content": tail})
    if finish is not None:
        payload, fin = finish
        if recovered:
            payload = json.loads(json.dumps(payload))
            for ch2 in payload.get("choices") or []:
                if isinstance(ch2, dict) and ch2.get("finish_reason"):
                    ch2["finish_reason"] = "tool_calls"
        yield ("data: " + json.dumps(payload, ensure_ascii=False)
               + "\n\n").encode("utf-8")
    elif recovered:
        yield _leak_chunk_frame(meta, {}, "tool_calls")
    for _up in tail_usage:
        yield ("data: " + json.dumps(_up, ensure_ascii=False)
               + "\n\n").encode("utf-8")


def _sse_guard_truncated_tool_args(frames):
    """Validate accumulated native arguments before forwarding terminal success.

    Content/reasoning deltas are never interchanged; completed JSON calls retain
    streaming latency. An explicit finish does not make malformed JSON valid.
    """
    calls = {}
    def validate():
        for slot in calls.values():
            args = "".join(slot["args"]).strip()
            if not args:
                continue  # No-argument tools are valid; existing adapters use {}.
            try:
                json.loads(args)
            except (TypeError, ValueError):
                raise EmptyToolEcho(len(args), "truncated_arguments")
    for raw in frames:
        terminal = False
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            data = strip_data_prefix(text)
            if data == "[DONE]":
                terminal = True
            elif data:
                payload = json.loads(data)
                for choice in payload.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    terminal = terminal or bool(choice.get("finish_reason"))
                    for tc in ((choice.get("delta") or {}).get("tool_calls") or []):
                        if not isinstance(tc, dict):
                            continue
                        key = (choice.get("index", 0), tc.get("index", 0))
                        slot = calls.setdefault(key, {"args": []})
                        args = (tc.get("function") or {}).get("arguments")
                        if isinstance(args, str):
                            slot["args"].append(args)
        except (ValueError, TypeError, AttributeError):
            pass
        if terminal:
            validate()
        yield raw
    validate()


def _finish_only_frame(payload, finish_reason):
    """复制一帧、清空 delta、只改 finish_reason（issue #124 降级用）。"""
    out = dict(payload or {})
    choices = []
    for ch in (payload or {}).get("choices") or []:
        if isinstance(ch, dict):
            ch2 = dict(ch)
            ch2["delta"] = {}
            ch2["finish_reason"] = finish_reason
            choices.append(ch2)
        else:
            choices.append(ch)
    if not choices:
        choices = [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
    out["choices"] = choices
    return ("data: " + json.dumps(out, ensure_ascii=False) + "\n\n").encode("utf-8")


def _sse_guard_nameless_tool_calls(frames):
    """issue #124：没拿到 name 的 tool_call 起点不许直接发给下游。

    上游（或回读路径）可能先给「index + arguments」、name 稍后才到，甚至永不到。
    OpenAI 流式语义里一个 index 的首帧必须同时带 id 与 name；ai-sdk 这类客户端
    会把它挂进 pendingTools，收尾仍不完整就抛
    "OpenAI Chat tool call delta is missing id or name"，整轮作废。

    与 Anthropic 侧的 #116 同构：
      · name 未到 -> 扣住 arguments，不发这一条；
      · name 到 -> 立刻发「id + name + 攒下的 arguments」一次；
      · 流结束仍无名 -> 明确协议失败，绝不降级为正常 stop。
    """
    pending = {}

    for raw in frames:
        text = (raw.decode("utf-8", "replace")
                if isinstance(raw, bytes) else raw)
        data = strip_data_prefix(text)
        payload = None
        if data and data != "[DONE]":
            try:
                payload = json.loads(data)
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            yield raw
            continue
        choices = payload.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        fin = choice.get("finish_reason")
        tcs = delta.get("tool_calls")
        if not isinstance(tcs, list) or not tcs:
            if fin and any(not s["opened"] for s in pending.values()):
                raise EmptyToolEcho(0, "missing_tool_name")
            else:
                yield raw
            continue

        forwarded = []
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index")
            idx = idx if isinstance(idx, int) else 0
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or "")
            args = fn.get("arguments") or ""
            slot = pending.setdefault(idx, {"id": "", "type": tc.get("type"),
                                            "name": "", "args": [], "opened": False})
            if tc.get("id"):
                slot["id"] = str(tc["id"])
            if name:
                slot["name"] = name
            if not slot["name"]:
                slot["args"].append(args)        # 扣住，等 name
                continue
            if not slot["opened"]:
                slot["opened"] = True
                forwarded.append({
                    "index": idx,
                    "id": slot["id"] or _new_id("call_"),
                    "type": slot["type"] or "function",
                    "function": {"name": slot["name"],
                                 "arguments": "".join(slot["args"]) + args},
                })
                slot["args"] = []
            else:
                forwarded.append({
                    "index": idx,
                    "id": slot["id"] or "",
                    "type": slot["type"] or "function",
                    "function": {"arguments": args},
                })

        if fin and any(not slot["opened"] for slot in pending.values()):
            raise EmptyToolEcho(0, "missing_tool_name")
        body = {k: v for k, v in delta.items() if k != "tool_calls"}
        if forwarded:
            body["tool_calls"] = forwarded
            frame = dict(payload)
            frame["choices"] = [{"index": choice.get("index", 0) or 0,
                                 "delta": body, "finish_reason": fin}]
            yield ("data: " + json.dumps(frame, ensure_ascii=False)
                   + "\n\n").encode("utf-8")
        elif body:
            # 这一帧还有正文/思考：只把无名 tool_calls 剥掉，其余照发
            frame = dict(payload)
            frame["choices"] = [{"index": choice.get("index", 0) or 0,
                                 "delta": body, "finish_reason": fin}]
            yield ("data: " + json.dumps(frame, ensure_ascii=False)
                   + "\n\n").encode("utf-8")
        elif fin and any(not s["opened"] for s in pending.values()):
            raise EmptyToolEcho(0, "missing_tool_name")

    dropped = [i for i, s in pending.items() if not s["opened"]]
    if dropped:
        RECOVERY_STATS["nameless_drop"] = RECOVERY_STATS.get("nameless_drop", 0) + 1
        log("dropped %d tool_call(s) without name at stream end - issue #124"
            % len(dropped), level="WARN", tag="chat")
        raise EmptyToolEcho(0, "missing_tool_name")



def _tool_tag_tail_hold(text):
    """返回「疑似 `<tool_call>` 开头但还没打完」的尾巴（跨帧暂留，issue #137）。"""
    t = text or ""
    i = t.rfind("<")
    if i < 0 or len(t) - i > len(TOOL_CALL_OPEN) + 8:
        return ""
    tail = t[i:]
    if ">" in tail:
        return ""                    # 标签已完整：交由正文缓冲判定
    if TOOL_CALL_OPEN.startswith(tail.lower()):
        return tail                  # "<" / "<tool_c" / "<tool_call" …
    return ""


def _sse_recover_tool_tags(frames, allowed_names=None):
    """把流式正文里的 `<tool_call> Name {json} </tool_call>` 还原为 tool_calls。

    issue #137：模型（尤其经自定义供应商中转的模型）会把工具调用写成伪 XML。
    这条链路此前只认结构化 tool_calls / DSML / 网关 marker+JSON 数组，
    `<tool_call>` 形态要么当正文漏给客户端，要么被判成「无法还原的协议残片」
    而 fail-closed —— 客户端看到的就是整轮静默中断。

    策略与 _sse_recover_dsml 一致：平时只暂留「像标签开头」的几个字符；一旦
    正文出现 `<tool_call` 就转入缓冲直到流末统一解析。解析出调用则补发散文 +
    tool_calls + finish=tool_calls；解析不出则原样补发（fail-open）。
    未声明 tools 的会话一律不缓冲（`<tool_call>` 只会是在讨论这个格式）。
    """
    if not allowed_names:
        for raw in frames:
            yield raw
        return
    buf = ""
    tail = ""
    finish = None
    tail_usage = []
    meta = {"id": None, "model": None, "created": None}
    recovered = False
    # 已放行的正文：块若落在未闭合的 ``` 里，那是文档示例（教程/提问里贴的
    # 格式），**不能**转成真实调用 —— 误报会真的执行命令。
    # 必须累计整段再算奇偶：围栏标记会跨帧拆开（"``" + "`"），逐帧计数会漏。
    emitted_text = []

    for raw in frames:
        text = (raw.decode("utf-8", "replace")
                if isinstance(raw, bytes) else raw)
        data = strip_data_prefix(text)
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except Exception:
            yield raw
            continue
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            if (isinstance(payload, dict)
                    and isinstance(payload.get("usage"), dict)):
                if buf or tail:
                    # 缓冲期间的 usage 垫底帧：缓存到收尾帧之后补发
                    tail_usage.append(payload)
                    continue
                if finish is not None:
                    _fp, _ffin = finish
                    finish = None
                    yield ("data: " + json.dumps(_fp, ensure_ascii=False)
                           + "\n\n").encode("utf-8")
            yield raw
            continue
        if not meta["id"] and payload.get("id"):
            meta["id"] = payload.get("id")
        if not meta["model"] and payload.get("model"):
            meta["model"] = payload.get("model")
        if not meta["created"] and payload.get("created"):
            meta["created"] = payload.get("created")
        ch = choices[0] if isinstance(choices[0], dict) else {}
        delta = ch.get("delta") or {}
        piece = delta.get("content")
        fin = ch.get("finish_reason")
        if not isinstance(piece, str) or not piece:
            if fin:
                finish = (payload, fin)
            else:
                yield raw
            continue
        piece = tail + piece
        tail = ""
        if not buf:
            hold = _tool_tag_tail_hold(piece)
            body = piece[:len(piece) - len(hold)] if hold else piece
            if TOOL_CALL_OPEN in body.lower():
                # 围栏判定必须发生在**进入缓冲之前**：正文已经走完的 ``` 若为
                # 奇数，说明这个标签落在未闭合的代码块里 = 文档示例（教程、提问
                # 里贴的格式），绝不能转成真实调用 —— 误报会真的执行命令。
                if ("".join(emitted_text) + body).count("```") % 2 == 1:
                    payload2 = _chunk_with_content(payload, body)
                    emitted_text.append(body)
                    yield ("data: " + json.dumps(payload2, ensure_ascii=False)
                           + "\n\n").encode("utf-8")
                    tail = hold
                    continue
                buf = body
                tail = hold
                continue
            if body:
                payload2 = _chunk_with_content(payload, body)
                emitted_text.append(body)
                yield ("data: " + json.dumps(payload2, ensure_ascii=False)
                       + "\n\n").encode("utf-8")
            tail = hold
            continue
        buf += piece

    if buf:
        calls, clean = parse_tool_call_tags(buf, allowed_names)
        if calls:
            recovered = True
            RECOVERY_STATS["tool_tag"] = RECOVERY_STATS.get("tool_tag", 0) + 1
            if clean and _residue_text_only(clean):
                log("dropped residue in post-recovery text (%d chars) — issue #49"
                    % len(clean), level="WARN", tag="chat")
            elif clean:
                yield _leak_chunk_frame(meta, {"content": clean})
            for i, call in enumerate(calls):
                yield _leak_chunk_frame(meta, {"tool_calls": [{
                    "index": i,
                    "id": call.get("id") or _new_id("call_"),
                    "type": "function",
                    "function": call.get("function") or {},
                }]})
        else:
            # fail-open：不是可执行的调用（讲解示例 / 参数坏了 / 名字没声明）
            yield _leak_chunk_frame(meta, {"content": buf})
    if tail:
        yield _leak_chunk_frame(meta, {"content": tail})
    if finish is not None:
        payload, fin = finish
        if recovered:
            payload = json.loads(json.dumps(payload))
            for ch2 in payload.get("choices") or []:
                if isinstance(ch2, dict) and ch2.get("finish_reason"):
                    ch2["finish_reason"] = "tool_calls"
        yield ("data: " + json.dumps(payload, ensure_ascii=False)
               + "\n\n").encode("utf-8")
    elif recovered:
        yield _leak_chunk_frame(meta, {}, "tool_calls")
    for _up in tail_usage:
        yield ("data: " + json.dumps(_up, ensure_ascii=False)
               + "\n\n").encode("utf-8")


# 模型偶发把工具调用写成正文（真实 Claude 会话）。这不是可执行
# 协议：不能猜测执行命令，也不能返回正常 stop 让客户端误判任务完成。
_NAMED_TOOL_ECHO_RE = re.compile(
    r"(?m)^[ \t]*\[([A-Za-z_][A-Za-z0-9_.:\-]*)[ \t]+工具调用\]\s*(?=[{\[])"
)


def _raise_unstructured_tool_echo(text, allowed_names):
    if not allowed_names:
        return
    for match in _NAMED_TOOL_ECHO_RE.finditer(_outside_fences(text)):
        if match.group(1) in allowed_names:
            log("unstructured named tool-call echo — failing the turn closed",
                level="WARN", tag="chat")
            raise EmptyToolEcho(len(text), "unstructured_tool_call")


def _sse_guard_unstructured_tool_echo(frames, allowed_names=None):
    """正文里的伪工具调用必须在发送成功收尾前报错；正文仍即时输出。"""
    if not allowed_names:
        yield from frames
        return
    content, deferred = [], []
    has_calls = False
    for raw in frames:
        finish = False
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            payload = json.loads(strip_data_prefix(text))
            for choice in payload.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str):
                    content.append(piece)
                has_calls = has_calls or bool(delta.get("tool_calls"))
                finish = finish or bool(choice.get("finish_reason"))
        except (ValueError, TypeError, AttributeError):
            pass
        if finish or deferred:
            # 连同 usage / [DONE] 保持原顺序；失败时绝不先发成功的 finish。
            deferred.append(raw)
        else:
            yield raw
    if not has_calls:
        _raise_unstructured_tool_echo("".join(content), allowed_names)
    yield from deferred


def _sse_guard_empty_tool_turn(frames, allowed_names=None, audit=None):
    """No visible answer and no executable call is not successful completion.

    Only tool-enabled requests opt in. Ordinary code examples are valid visible
    answers and are never interpreted or executed as tools.
    """
    if not allowed_names:
        yield from frames
        return
    visible, has_call, reasoning_chars, finish, seen = False, False, 0, None, False
    content_chars = 0
    content_prefix = ""
    def validate():
        if isinstance(audit, dict):
            audit.update(output_content_chars=content_chars, output_reasoning_chars=reasoning_chars,
                         output_has_tool_call=bool(has_call), output_finish_reason=finish or "eof")
            audit["output_code_without_tools"] = bool(not has_call and content_prefix.lstrip().startswith(chr(96)*3))
        if seen and not visible and not has_call and finish not in ("length", "content_filter"):
            raise EmptyToolEcho(0, "reasoning_only" if reasoning_chars else "empty_tool_turn")
        if finish in ("tool_calls", "function_call") and not has_call:
            raise EmptyToolEcho(0, "missing_tool_call")
    for raw in frames:
        terminal = False
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            data = strip_data_prefix(text)
            if data == "[DONE]":
                terminal = True
            elif data:
                payload = json.loads(data)
                for choice in payload.get("choices") or []:
                    seen = True
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str):
                        content_chars += len(content)
                        if len(content_prefix) < 80:
                            content_prefix = (content_prefix + content)[:80]
                    visible = visible or (isinstance(content, str) and bool(content.strip()))
                    if isinstance(content, list):
                        visible = visible or any(isinstance(v, dict) and bool(str(v.get("text") or "").strip()) for v in content)
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str):
                        reasoning_chars += len(reasoning)
                    has_call = has_call or any(bool((tc.get("function") or {}).get("name")) for tc in delta.get("tool_calls") or [] if isinstance(tc, dict))
                    has_call = has_call or bool((delta.get("function_call") or {}).get("name"))
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                        terminal = True
        except (ValueError, TypeError, AttributeError):
            pass
        if terminal:
            validate()
        yield raw
    validate()


def recover_leaked_tool_calls(frames, allowed_names=None, audit=None):
    """流式出口：回读（issue #8 / #51 / #137）+ **tool_call id 保证**（issue #47）
    + 截断参数守卫（issue #112）。

    保持同名：三条调用点（chat 主链路 / Anthropic / Responses）都不用改。
    各阶段都是生成器，顺序即数据流方向；用具名中间变量而非层层嵌套 ——
    出口是包装器这一事实要能被静态断言钉住（见 _test_stream_tool_ids 第 7 节）。
    """
    frames = _sse_normalize_tool_call_indices(frames)
    leaked = _recover_leaked_tool_calls_inner(frames, allowed_names)
    dsml = _sse_recover_dsml(leaked, allowed_names)
    tagged = _sse_recover_tool_tags(dsml, allowed_names)
    with_ids = _sse_fill_tool_call_ids(tagged)
    named = _sse_guard_nameless_tool_calls(with_ids)
    checked = _sse_guard_truncated_tool_args(named)
    guarded = _sse_guard_unstructured_tool_echo(checked, allowed_names)
    for frame in _sse_guard_empty_tool_turn(guarded, allowed_names, audit=audit):
        yield frame


# ---------------------------------------------------------------------------
# Qoder 数据面：请求体构造
# ---------------------------------------------------------------------------
def _flatten_content_text(content):
    """把 OpenAI content（str 或 parts 列表）压成纯文本；图片另行收集。"""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if isinstance(content, dict):
        # 单 part 形态（部分客户端把单个 part 对象直接当 content 而非列表）：
        # 当单元素列表处理，绝不 str() —— 否则图像 dict 被字符串化成 Python
        # 字面量文本，模型收到的是一段 {'type': 'image_url', ...}，
        # 图像从未真正到达模型（issue #33 的反馈根因）。
        content = [content]
    elif not isinstance(content, list):
        return str(content), []
    texts, images = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("text", "input_text", "output_text", "summary_text"):
            texts.append(piece.get("text") or "")
        elif ptype in ("image_url", "image", "input_image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = "data:%s;base64,%s" % (mime, piece["data"])
            if url:
                images.append(url)
    return "\n".join(t for t in texts if t), images


# ---------------------------------------------------------------------------
# 写入侧暴露面削减（评估报告路线 3：历史工具结果首尾保留 + 中间省略）
# ---------------------------------------------------------------------------
# 动机（issue #8/#11）：长会话里工具结果被全文回灌、且每轮全量重放，模型反复
# 看到同一套「信封 + 长正文」样本 -> 复述概率升高，token 成本也随轮数线性增长。
# 这里只削减**历史**（当前轮的 tool 结果必须完整保留）：信封前缀
# （TOOL_RESULT_MARKER + name + "]\n"）逐字节不变，只动正文。
TOOL_RESULT_KEEP_DEFAULT = 2000        # 单侧保留字符数（默认值，可由环境覆盖）
_TOOL_RESULT_SHRINK_MARGIN = 200       # 削后至少要省这么多字符，才值得加省略标记


def _tool_result_keep_chars():
    """单侧保留字符数；返回 0 表示**完全关闭**削减（回退全文回灌）。

    环境变量 QD_TOOL_RESULT_KEEP：正整数 = 单侧保留字符数；
    0 / off / none / false / no / disable[d] = 关闭；非法值 = 用默认值。
    每次调用都读环境变量，便于运行期调整与离线测试。
    """
    raw = (os.environ.get("QD_TOOL_RESULT_KEEP") or "").strip().lower()
    if not raw:
        return TOOL_RESULT_KEEP_DEFAULT
    if raw in ("0", "off", "none", "false", "no", "disable", "disabled"):
        return 0
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return TOOL_RESULT_KEEP_DEFAULT
    return val if val > 0 else 0


def _is_complete_json(text):
    s = (text or "").strip()
    if not s or s[0] not in "{[":
        return False
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def _shrink_tool_result(text, keep):
    """历史工具结果：首尾各保留 keep 字符，中间换成明确的省略标记。

    为什么首尾保留：工具输出的信息密度两端最高——头部是命令回显/首个错误/
    文件起始，尾部是错误堆栈结尾、退出状态、diff 末尾；中间通常是重复的滚动
    日志。按换行边界对齐，避免切出半行。
    完整 JSON 额外宽限到 2*keep：结构化数据被截断即失去可解析性。
    返回原文本表示「不值得削或不能削」。
    """
    if not isinstance(text, str) or keep <= 0:
        return text
    if len(text) <= keep * 2 + _TOOL_RESULT_SHRINK_MARGIN:
        return text                       # 本来就不长：原样保留
    if _is_complete_json(text) and len(text) <= keep * 4:
        return text                       # 完整 JSON 给双倍预算，保住可解析性
    head = text[:keep]
    nl = head.rfind("\n")
    if nl > keep // 2:                    # 尽量切在行边界（且别把头部砍太多）
        head = head[:nl]
    tail = text[-keep:]
    nl2 = tail.find("\n")
    if nl2 != -1 and nl2 < keep // 2:
        tail = tail[nl2 + 1:]
    # issue #48：省略标记必须是**固定串**——带字数会让「同一条工具结果」
    # 在不同轮次产生不同字节（内容一旦变化/重试，数字就变），直接打断上游
    # 前缀缓存。字数信息换不来什么，去掉。
    marker = ("\n[[qoder-proxy: 已省略中间内容；"
              "需要完整内容请重新执行该命令]]\n")
    return head + marker + tail


def _tool_result_scope():
    """削减范围策略（issue #48）：返回 ("tail", N) / ("all", 0) / ("turn", 0)。

    为什么需要它：旧规则 `_current_turn_start` 会随请求形态**滑动**——
    连续工具调用时 `_cur_start=1`（全不削），用户插一句话后 `_cur_start` 跳到
    末尾（**每条**历史工具结果全部改写成省略版）。同一条历史消息在不同轮次被
    序列化成不同字节，上游前缀缓存从第一条被削处开始整段失效（实测命中率
    从 95-99% 掉到 8%）。

    新默认 `tail`：只保护**最后 N 条**工具结果（默认 1），更早的一律削——
    同一结果一旦被削，字节里不再有任何随轮次变化的成分（标记已改固定串），
    因此只有「从最后一条滑出保护窗」的那一条会变，缓存中断点被限制在一处。

    环境变量 QD_TOOL_RESULT_SCOPE：
      tail（默认） 保护最后 QD_TOOL_RESULT_KEEP_TAIL 条（默认 1）
      all          全部削减（缓存最稳，但当轮长输出也会被截）
      turn         旧行为（当前轮全保，形态相关——仅兼容用）
    """
    scope = str(os.environ.get("QD_TOOL_RESULT_SCOPE") or "tail").strip().lower()
    if scope in ("all", "stable", "everything"):
        return "all", 0
    if scope in ("turn", "history", "legacy", "current"):
        return "turn", 0
    try:
        n = int(os.environ.get("QD_TOOL_RESULT_KEEP_TAIL") or 1)
    except Exception:
        n = 1
    return "tail", max(0, n)


def _current_turn_start(messages):
    """返回「当前轮」起始下标（索引 >= 它的 tool 结果不削减）。

    **仅 QD_TOOL_RESULT_SCOPE=turn 时使用**（issue #48：该规则随请求形态滑动，
    会让历史工具结果在不同轮次序列化成不同字节 -> 前缀缓存每轮失效）。

    判定：从末尾往前找最后一条 user/system/developer 消息——它开启当前回合，
    其后的一切（assistant 工具调用 + tool 结果）都属于当前轮。找不到则返回 0
    （全部视为当前轮：宁可不削，也不误削模型正在用的结果）。
    """
    msgs = messages or []
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, dict) and m.get("role") in ("user", "system",
                                                    "developer"):
            return i + 1
    return 0


# ---------------------------------------------------------------------------
# 结构化工具历史（task-32：tool 消息 / assistant.tool_calls 原样透传）
# ---------------------------------------------------------------------------
# 依据 task-31 实测（.team/tiedan.md §17）：legacy 通道在 Qwen 全系 / GLM 上
# 接受结构化历史并被正确消费（含 3 轮链、结构化+旧文本信封混合、并行调用、
# id 不匹配）；DeepSeek / Kimi 在 assistant.content=null 时被拒，**content=""
# 即通过**。因此本实现恒用空字符串，绝不发 null。
# issue #49 后续：DeepSeek 由报告者在真实长会话上端到端验证通过（v1.5.4，
# Anthropic 入口，1,283 次工具调用 0 降级；同环境开启前基线 0.30%）。
# 结构化直传后模型看不到 LEAK_MARKER 文本格式，也就无从复述——这是治本项。
# _tool_ids_ok 守卫仍在：客户端 id 不齐备时自动回退文本化，不会踩坑。
# issue #153：Kimi 仍走文本化会在长工具链复述双层 JSON 信封；加入同一结构化
# 决策（content 恒为空字符串）并在有工具的请求中添加稳定的 JSON/原生调用约束。
# 这是生成端预防，不能保证模型不再犯错，现有出站参数守卫仍保持不变。
# issue #60 follow-up：DeepSeek 的**上游 key 是 `dfmodel`**（显示名 DeepSeek-V4-Flash），
# 只写 "deepseek" 匹配不到 -> auto 一直是文本化，模型照抄伪协议。两个都保留。
STRUCTURED_TOOL_HISTORY_WHITELIST = ("qwen", "glm", "gm4", "gm5",
                                   "deepseek", "dfmodel")
_STRUCTURED_LOG_ONCE = {"done": False}


def _structured_realms():
    """auto 模式允许结构化历史的出口，默认 cn + intl。

    国际区 GLM-5.3 已实测接受配对的 assistant.tool_calls / role:tool，
    继续文本化会让模型照抄历史信封并静默结束。provider 白名单与 id 守卫
    仍由 structured_tool_history_enabled 控制；显式设为 cn 可回退旧范围。
    """
    raw = os.environ.get("QD_STRUCTURED_TOOL_HISTORY_REALMS")
    if raw is None or not raw.strip():
        return {"cn", "intl"}
    out = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return out or {"cn", "intl"}


def _structured_mode():
    """QD_STRUCTURED_TOOL_HISTORY 三态：'on' / 'off' / 'auto'（默认）。"""
    raw = (os.environ.get("QD_STRUCTURED_TOOL_HISTORY") or "").strip().lower()
    if not raw or raw == "auto":
        return "auto"
    if raw in ("1", "on", "true", "yes", "enable", "enabled", "force"):
        return "on"
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return "off"
    return "auto"


def _tool_ids_ok(messages):
    """结构化前提：每条 tool 有 tool_call_id，每个 assistant.tool_calls 条目有 id。"""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            if not str(m.get("tool_call_id") or "").strip():
                return False
        elif role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"] or []:
                if not isinstance(tc, dict) \
                        or not str(tc.get("id") or "").strip():
                    return False
    return True


def structured_tool_history_enabled(model="", model_key="", realm="",
                                    messages=None):
    """本次请求是否走结构化工具历史。

    - off          -> False（一键回退文本化）
    - on           -> True（显式强制，包括白名单之外的模型）
    - auto（默认） -> 「允许的出口 + provider 白名单 + id 齐备」为真：
        默认允许 CN / INTL；QD_STRUCTURED_TOOL_HISTORY_REALMS 可缩小范围。
    任何模式下 id 不齐备都回退文本化（fail-safe：宁可少用，不发畸形请求）。
    """
    if not _tool_ids_ok(messages):
        return False
    mode = _structured_mode()
    if mode == "off":
        return False
    if mode == "on":
        return True
    if (realm or "").strip().lower() not in _structured_realms():
        return False
    for cand in (str(model_key or ""), str(model or "")):
        low = cand.strip().lower()
        if low and (low.startswith(STRUCTURED_TOOL_HISTORY_WHITELIST)
                    or is_kimi_model(cand)):
            if not _STRUCTURED_LOG_ONCE["done"]:
                _STRUCTURED_LOG_ONCE["done"] = True
                log("structured tool history: ON (model=%s key=%s realm=%s)"
                    % (model, model_key, realm), tag="chat")
            return True
    return False


def flatten_messages(messages, keep_reasoning=False, structured=False):
    """把客户端会话压平成 Qoder 上游可接受的 {role, content} 序列。

    返回 (system_text, flat_msgs, images)：
      - 首个 system/developer 消息抽为 system_text（模板 system 的替换源）
      - user/assistant 原位保留（content 压平为字符串）
      - tool 结果：structured=True 时保留 {role:"tool", tool_call_id, content}
        （content 恒为空字符串而非 null——task-31 实测 null 会让上游转换层丢掉
        配对的 assistant 消息，DeepSeek/Kimi 直接 provider_error）；
        否则降级为 user 文本（上游历史上不认识 tool 角色）
      - assistant 的 tool_calls：structured=True 时原样透传
        （id/type/function，arguments 统一为 JSON 字符串）；
        否则序列化进 content（LEAK_MARKER + JSON 数组）
      - assistant 的 reasoning_content 仅在 keep_reasoning=True（DeepSeek 族）
        时保留：这族模型的多轮一致性与该字段绑定，其它模型不带（避免上游
        因未知字段拒答）。此前 flatten 无条件丢弃该字段，使
        backfill_reasoning_content 的兼容处理在真实请求路径上完全失效。
    """
    system_text = None
    flat, images = [], []
    # 暴露面削减只作用于历史（当前轮的 tool 结果必须完整保留）。
    _keep = _tool_result_keep_chars()
    # issue #48：削减范围必须**与请求形态无关**。旧规则按「当前轮起点」滑动，
    # 用户每发一条消息就把全部历史工具结果重削一遍 -> 同一条消息在不同轮次
    # 序列化成不同字节 -> 上游前缀缓存整段失效（实测命中率 95%+ -> 8%）。
    # 新默认 tail：只保护最后 N 条工具结果，更早的一律削（且削后字节恒定）。
    _scope, _tail_n = _tool_result_scope()
    _protect = set()
    if _keep:
        if _scope == "turn":
            _protect = set(range(_current_turn_start(messages) or 0,
                                 len(messages or [])))
        elif _scope == "tail" and _tail_n > 0:
            _tool_idx = [i for i, m in enumerate(messages or [])
                         if isinstance(m, dict) and m.get("role") == "tool"]
            _protect = set(_tool_idx[-_tail_n:])
    for _idx, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text, imgs = _flatten_content_text(m.get("content"))
        images.extend(imgs)
        text = sanitize_text(text) if isinstance(text, str) else text
        if role in ("system", "developer"):
            if system_text is None and text:
                system_text = text
            continue
        if role == "tool":
            name = ""
            tc = m.get("name") or ""
            if tc:
                name = " (%s)" % tc
            body = text or ""
            if _keep and _idx not in _protect:
                body = _shrink_tool_result(body, _keep)
            if structured:
                # 结构化直传：保留 tool 角色与配对 id；content 用空字符串而非
                # null（task-31：null 会让上游丢掉配对的 assistant 消息）。
                flat.append({"role": "tool",
                             "tool_call_id": str(m.get("tool_call_id") or ""),
                             "content": body})
                continue
            flat.append({"role": "user",
                         # 前缀逐字节不变（TOOL_RESULT_MARKER + name + "]\n"）；
                         # 削减只发生在正文部分。
                         "content": TOOL_RESULT_MARKER + name + "]\n" + body})
            continue
        if role == "assistant" and m.get("tool_calls"):
            if structured:
                # 结构化直传：保 id/type/function；content 恒为空字符串。
                # arguments 统一成 JSON 字符串（OpenAI 规范形态；dict 会被
                # 客户端/上游两侧的转换层按字符串处理，字符串最稳）。
                calls = []
                for tc in m["tool_calls"]:
                    tc = tc if isinstance(tc, dict) else {}
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, (dict, list)):
                        args = json.dumps(args, ensure_ascii=False)
                    elif not isinstance(args, str):
                        args = "" if args is None else str(args)
                    calls.append({"id": str(tc.get("id") or ""),
                                  "type": tc.get("type") or "function",
                                  "function": {"name": fn.get("name") or "",
                                               "arguments": args}})
                item = {"role": "assistant", "content": text or "",
                        "tool_calls": calls}
                if keep_reasoning and m.get("reasoning_content") is not None:
                    item["reasoning_content"] = m.get("reasoning_content") or ""
                flat.append(item)
                continue
            calls = []
            for tc in m["tool_calls"]:
                fn = (tc or {}).get("function") or {}
                calls.append({"name": fn.get("name") or "",
                              "arguments": fn.get("arguments") or ""})
            if calls:
                text = (text or "") + "\n\n" + LEAK_MARKER + "\n" + \
                    json.dumps(calls, ensure_ascii=False)
            item = {"role": "assistant", "content": text or ""}
            if keep_reasoning and m.get("reasoning_content") is not None:
                item["reasoning_content"] = m.get("reasoning_content") or ""
            flat.append(item)
            continue
        if role in ("user", "assistant"):
            if imgs:
                # 上游只读消息 content 里的结构化 image_url parts，**不读**
                # 顶层 image_urls / chat_context.imageUrls（实测：image_urls
                # 填了模型仍回「我没有收到图片」，改 parts 后正确识别颜色）。
                # 带图消息保留 parts 形态；顶层 image_urls 仍照填（兼容无害）。
                _parts = _flatten_content(m.get("content"))
                item = {"role": role,
                        "content": _parts if isinstance(_parts, list)
                        else (text or "")}
            else:
                item = {"role": role, "content": text or ""}
            if role == "assistant" and keep_reasoning \
                    and m.get("reasoning_content") is not None:
                item["reasoning_content"] = m.get("reasoning_content") or ""
            flat.append(item)
        elif role:  # 未知角色一律降级为 user，不静默丢弃
            flat.append({"role": "user", "content": text or ""})
    return system_text, flat, images


# issue #59：出站体积前置限制。阿里云 SLB 对单个 POST body 有 30MB 硬限制，
# 超过会直接 RST（本地表现为 Broken pipe），而 Broken pipe 会被当成瞬态故障
# 反复重试、最终把整个账号池打到熔断。留 1MB 余量。
MAX_UPSTREAM_BODY_BYTES = int(float(os.environ.get("QD_MAX_BODY_MB") or 29)
                               * 1024 * 1024)


def _keep_toplevel_images():
    """是否保留顶层冗余图片字段（默认否，issue #59）。"""
    return str(os.environ.get("QD_KEEP_TOPLEVEL_IMAGES") or "").strip().lower()         in ("1", "on", "true", "yes")


def _default_test_model(realm=None):
    """账号「测试」的默认模型：优先**免费**模型（issue #134）。

    面板测试的定位是「凭证 + 通道是否活着」。此前固定 auto，零额度账号必然
    403（code 112），看起来像凭证坏了。免费模型的成本是 0，最适合探活；
    找不到免费模型时退回 auto（保持旧行为，不猜）。
    """
    r = realm if realm in ("cn", "intl") else CURRENT_REALM
    try:
        models = [m for m in qoder_catalog.models_for_realm(r)
                  if isinstance(m, dict) and m.get("enable") is not False]
    except Exception:
        models = []

    def _name(m):
        try:
            return str(qoder_catalog.display_id(m) or m.get("key") or "")
        except Exception:
            return str(m.get("key") or "")

    # 三层优先级：真正 0 价（price_factor == 0）> 标了 is_free > auto。
    # 注意 is_free 的语义是「含免费权益」，不代表 0 价（Qwen3.8-Max 是 0.2x），
    # 所以不能用它当第一档（issue #134 里报告者也提到要挑免费模型）。
    for pick in (lambda m: m.get("price_factor") == 0,
                 lambda m: bool(m.get("is_free"))):
        for m in models:
            try:
                if pick(m) and _name(m):
                    return _name(m)
            except Exception:
                continue
    return "auto"


def _quota_hint(detail):
    """上游 detail -> 人话解释（issue #123 后续）。只认已实测过的码。"""
    d = str(detail or "")
    if "10605" in d or "isQueued" in d:
        return ("上游排队中（10605 / RESOURCE_LIMIT）：建议按 retryAfterSeconds 退避，不保证放行；原因未确认。")
    if "112" in d and ("pricingUrl" in d or "code" in d):
        return "上游模型额度/套餐拒绝（112）：不代表总余额为零，核对实际账号、区域和模型权限"
    if "Billing daily count exceeded" in d or "110" in d:
        return "当日计费次数上限（110）：次日 0 点重置"
    if "430" in d:
        return "客户端/网关版本过低（430）：上游要求升级"
    if "TOKEN_EXPIRE" in d or "expire" in d.lower():
        return "凭证过期：需要重新登录该账号"
    return ""

def build_qoder_body(payload, account, model_key, realm=None, conversation=None):
    """构造 agent_chat_generation 上游请求体（返回 dict，随后 qoder_encode）。

    以官方 baseprompt.json 为骨架：每次覆写 request/session id、时间戳、
    模型配置（按出口区域的官方清单取元数据）、系统提示词与会话、工具、
    参数与 business 会话名。
    """
    r = realm or (account.realm if account else CURRENT_REALM)
    body = copy.deepcopy(BASEPROMPT)   # isolate nested template without a JSON round trip
    messages = normalize_roles(payload.get("messages") or [])
    messages = sanitize_messages(messages)
    model = payload.get("model") or ""
    messages = backfill_reasoning_content(messages, model, model_key)

    # 形态决策**单点**（设计文档 §0 原则①）：Anthropic 等新入口会把决定好的
    # 结果放在 payload["_qd_structured"] 里下传，这里优先采信它；只有 chat 主链路
    # （不带该键）才在本函数内自行判定。两处各判一次会导致「桥接层以为结构化、
    # flatten 却走文本化」的错配，症状正是客户端收到信封文本。
    use_structured = payload.get("_qd_structured")
    if use_structured is None:
        use_structured = structured_tool_history_enabled(
            model=model, model_key=model_key, realm=r, messages=messages)
    else:
        use_structured = bool(use_structured)
    system_text, flat, images = flatten_messages(
        messages, keep_reasoning=is_deepseek_model(model, model_key),
        structured=use_structured)
    # 图片在三协议桥接后统一预处理：不删图/不降分辨率，避免每轮重新转大 PNG。
    # 只处理 inline data URI；远程 URL 不由网关抓取，保留原有网络边界。
    flat, image_stats = qoder_images.prepare_messages(flat)
    if image_stats["optimized"] or image_stats["codec_missing"]:
        log("image preflight: count=%d optimized=%d chars=%d->%d codec_missing=%d"
            % (image_stats["images"], image_stats["optimized"],
               image_stats["bytes_before"], image_stats["bytes_after"],
               image_stats["codec_missing"]), tag="image")
    if images:
        images = [_url for _m in flat
                  for _text, _urls in [_flatten_content_text(_m.get("content"))]
                  for _url in _urls]
    # 模板只保留 system 基座（其自带的示例 user 轮次是样例内容，必须丢弃），
    # 真实会话随后追加。
    tmpl_system = [m for m in (body.get("messages") or [])
                   if (m or {}).get("role") == "system"]
    if system_text is None:
        # 客户端没给 system：优先用 --system-prompt 覆盖值（非默认时），
        # 否则保留模板自带的 Qoder 系统提示词。
        if SYSTEM_PROMPT and SYSTEM_PROMPT != DEFAULT_SYSTEM_PROMPT:
            system_text = SYSTEM_PROMPT
        else:
            body["messages"] = tmpl_system
    if system_text is not None:
        body["messages"] = [{"role": "system", "content": system_text}]
    # 追加真实会话
    body["messages"].extend(flat)

    # 最新用户提示词（上游 chat_context 高亮 + business 会话名）
    prompt = ""
    for m in reversed(flat):
        if m["role"] == "user" and m.get("content"):
            _c = m["content"]
            if isinstance(_c, list):
                _c = "\n".join(
                    str(p.get("text") or "") for p in _c
                    if isinstance(p, dict) and p.get("type") == "text")
            prompt = _c
            break

    nid = str(uuid.uuid4())
    body["request_id"] = nid
    # issue #128：用量面板的「一条对话」看起来是按 chat_record_id 归组的（官方
    # 客户端一次对话内该值不变）。同一会话复用会话 id；request_id 仍然每请求
    # 唯一 —— 计费/限额按请求计数，不能被并掉。
    body["chat_record_id"] = (conversation or {}).get("id") or nid
    body["request_set_id"] = str(uuid.uuid4())
    body["session_id"] = str(uuid.uuid4())
    body["stream"] = True
    body["agent_id"] = "agent_common"
    body["aliyun_user_type"] = getattr(account, "user_type", "") or \
        qoder_sign.DEFAULT_USER_TYPE

    # model_config（按出口区域的官方清单取元数据 + 动态 key）——动态清单优先
    # （catalog_meta_for），官方改显示名（Kimi-K3 -> Kimi-K3.1-Pro）后这里
    # 发出去的 display_name 仍是官方此刻的名字，与真实客户端一致。
    catalog_meta = catalog_meta_for(model_key, r)
    mc = body.get("model_config") or {}
    mc["key"] = model_key
    mc["display_name"] = catalog_meta.get("display_name") or model_key
    mc["model"] = ""
    mc["format"] = "openai"
    mc["is_vl"] = bool(catalog_meta.get("is_vl"))
    mc["is_reasoning"] = bool(catalog_meta.get("is_reasoning"))
    mc["api_key"] = ""
    mc["url"] = ""
    mc["source"] = "system"
    mc["max_input_tokens"] = catalog_meta.get("max_input_tokens") or 180000
    # issue #106：官方清单默认 ~200K，但模型实际支持 1M 档——允许按请求/设置抬窗。
    try:
        _ctx = model_context_override(payload, model_key, model, r)
        if _ctx:
            mc["max_input_tokens"] = _ctx
    except Exception as _exc:
        log("context override failed: %s" % _exc, level="WARN", tag="chat")
    body["model_config"] = mc

    # chat_context 高亮与模型配置副本
    cc = body.get("chat_context") or {}
    txt = cc.get("text") or {}
    txt["text"] = prompt
    cc["text"] = txt
    extra = cc.get("extra") or {}
    oc = extra.get("originalContent") or {}
    oc["text"] = prompt
    extra["originalContent"] = oc
    extra["modelConfig"] = dict(mc)
    cc["extra"] = extra
    if images and _keep_toplevel_images():
        # issue #59：上游**只读** messages 里的结构化 image_url parts（v1.4.2 实测），
        # 顶层这两份是纯冗余：10 张图 15MB -> 出站 48MB -> qoder_encode 后 ~64MB，
        # 撞上阿里云 SLB 30MB 限制被 RST（Broken pipe），还会把账号池打到熔断。
        # 默认不再填充；QD_KEEP_TOPLEVEL_IMAGES=1 可恢复旧行为（仅排查用）。
        cc["imageUrls"] = images
        body["image_urls"] = images
    body["chat_context"] = cc

    # parameters：max_tokens / reasoning_effort
    params = body.get("parameters") or {}
    max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    # issue #101：已知该模型的上游输出上限时先 clamp（学到的或 catalog 带的）
    if max_tokens:
        max_tokens, _mt_clamped = clamp_max_tokens(
            max_tokens, model_key or model, realm)
        if _mt_clamped:
            payload["_qd_max_tokens_clamped"] = True
    if max_tokens:
        try:
            params["max_tokens"] = int(max_tokens)
        except (TypeError, ValueError):
            pass
    # 提取与 usage 记账共用同一实现（requested_effort），避免两处口径漂移
    effort = requested_effort(payload)
    if effort:
        norm, note = normalize_reasoning_effort(effort, catalog_meta)
        if note:
            log("reasoning_effort %s on '%s' (supported=%s)"
                % (note, model_key or model, supported_efforts(catalog_meta)), tag="chat")
        if norm:
            params["reasoning_effort"] = norm
    body["parameters"] = params

    # tools：客户端给了就用客户端的（custom freeform 已降级），否则置空，
    # 避免把 Qoder 桌面端自带的 agent 工具（Bash/Edit/...）泄漏给普通客户端。
    body["tools"] = payload.get("tools") or []
    # 出站统一嵌套 chat function schema：/v1/responses 的平铺 function 工具
    # （{"type":"function","name":...}）和 custom 降级产物都是顶层 name 形态，
    # 上游要求 tools[].function 对象，否则 400
    # "'function' is a required property, expected an object - 'tools.N'"。
    normalize_tools(body)
    _apply_kimi_tool_protocol(body, payload, model_key)

    # business 会话卡片
    biz = body.get("business") or {}
    # issue #128：同一次对话复用同一个 id 与 begin_at —— 上游用量面板按它归组，
    # 每请求一个新 uuid 会把一个 agent 回合拆成几十条「<1min 的对话」，
    # 日上限（code 110）被迅速刷爆。拿不到会话身份时保持旧行为。
    if conversation:
        biz["id"] = conversation["id"]
        biz["begin_at"] = conversation["begin_at"]
    else:
        biz["id"] = str(uuid.uuid4())
        biz["begin_at"] = int(time.time() * 1000)
    biz["name"] = (prompt[:30] if prompt else "chat")
    biz.setdefault("product", "cli")
    biz.setdefault("type", "agent")
    biz.setdefault("stage", "start")
    # 计费归属（2026-10-30 实测，Kimi-K3.1-Pro 被 qoder 后台记成 auto 的根因）：
    # 官方 CLI 1.1.81 的 business 块带 model_key，上游计费归因读它；网关此前
    # 不发，kmodel_latest 这类模型被静默归到 auto（老模型不受影响，疑似有
    # model_config 兜底）。与官方客户端对齐，始终带上。
    biz["model_key"] = model_key
    # 同理对齐版本号：官方报自己的真实版本，模板里 0.1.43 是上个时代的残留。
    # 版本号不影响计费归属（A/B 实测），但身份故事应与 cosy-version 同源。
    biz["version"] = qoder_sign.COSY_VERSION
    body["business"] = biz
    return body


# ---------------------------------------------------------------------------
# Qoder 数据面：上游打开与 SSE 信封解包
# ---------------------------------------------------------------------------
class RateLimited(Exception):
    """Upstream throttled this request (429 / 频控)。"""

    def __init__(self, http_error=None, detail="", wait=60):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        super(RateLimited, self).__init__(
            "upstream rate limit: %s" % (self.detail[:200] or "429"))


# 账号级错误短冷却的“等待续上”上限：单账号池在传输/瞬时故障后只有
# 3s（多账号 15s）冷却，此时**等待**比报 429 更正确——429 只留给真正的
# 上游频控（model_cooldowns，由上游 HTTP 429 设置）。
ERROR_COOLDOWN_WAIT_MAX = 10.0


def _realm_ready_total(realm, model=None):
    """本区域当前可轮换的就绪账号数 —— **唯一口径**（#29 的成因就是两处口径分叉）。

    冷却函数曾用 len(POOL.accounts)（全局），open_upstream 用 count_ready(realm)。
    双区池（1 cn + 1 intl）下前者是 2、后者是 1，于是「本区域唯一账号」被当成
    多号池，single_account 判定反了 -> 冷却不再压到 3s。两处都必须走这里。
    """
    if not POOL or not realm:
        # 无池、或调用方给不出 realm（例如仅实现部分接口的测试桩对象）-> 保守 1
        # （= 单号池语义：冷却压短）。绝不因为「判不出区域」而误判成多号池。
        return 1
    try:
        return max(1, POOL.count_ready(realm, model=model))
    except Exception:
        return 1


def _short_error_cooldown_wait(realm, model, exclude=None, allowed_uids=None):
    """账号级短冷却（含**预算内**的模型级频控窗）的最短剩余秒数；不适用返回 0。

    两条冷却来源**一起收集、取最小值**（#29 起）：
      · cooldown_until：账号级错误/传输冷却（原有语义）；
      · model_cooldowns[model]：**仅当剩余 <= ERROR_COOLDOWN_WAIT_MAX(10s)** 时纳入；
        超出预算的长频控仍交回 429 路径（客户端退避），不在这里吞掉。
    为什么不再短路：旧实现「遇到任一 model_cooldowns > now 就 return 0」，会让
    「号 A 长频控」连带把「号 B 只有 3s 可自愈」一起放弃（#29 的次生坑）。
    本函数**只决定等不等、不写任何冷却字段**，退避写者语义不变。
    已登记的语义变化：真 429 窗口 <=10s 时会被「等待 + 重试」，客户端收到
    延迟后的成功而不是 429（>10s 的行为不变）。
    - exclude（本请求已试过的账号）**不算**：等它冷却好了也不会再被本请求使用。
    """
    if not POOL:
        return 0.0
    exclude = exclude or set()
    now = time.time()
    waits = []
    for a in POOL.accounts:
        if a.realm != realm or not a.enabled or not a.access_token:
            continue
        if a.uid in exclude or (allowed_uids is not None and a.uid not in allowed_uids):
            continue
        m_remain = a.model_cooldowns.get(model, 0.0) - now
        if m_remain > 0:
            if m_remain <= ERROR_COOLDOWN_WAIT_MAX:
                waits.append(m_remain)      # 预算内的频控窗：值得等它过去
            continue                        # 超预算：跳过该号（别的号仍可纳入）
        remain = a.cooldown_until - now
        if 0 < remain <= ERROR_COOLDOWN_WAIT_MAX:
            waits.append(remain)
    return min(waits) if waits else 0.0


def retry_after_seconds(model, realm):
    """上游频控的最短等待（仅 model_cooldowns——429 的正当语义）。"""
    if not POOL:
        return 60
    now = time.time()
    waits = [a.model_cooldowns.get(model, 0.0) - now
             for a in POOL.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model):
    """True 仅当该区域账号全部被**上游频控**（model_cooldowns）挡住。

    账号级错误冷却（cooldown_until）不算频控——否则传输故障后客户端会连续
    收到误导性的 `429 usage exceeds frequency limit`。
    """
    if not POOL:
        return (False, 0)
    existing = [a for a in POOL.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    now = time.time()
    waits = [a.model_cooldowns.get(model, 0.0) - now for a in existing]
    if waits and all(w > 0 for w in waits):
        return (True, max(1, int(min(waits))))
    return (False, 0)


# ---------------------------------------------------------------------------
# 上游会话身份（issue #128）
# ---------------------------------------------------------------------------
# 官方客户端在一次对话里全程复用同一个 business.id / session_id，因此 Qoder 用量
# 面板把整段对话算成**一条**；网关此前每个 HTTP 请求都新造 uuid —— 一个 Codex
# 回合里有几十次工具步进，就被上游记成几十条「<1min 的对话」，日上限（code 110）
# 很快被刷爆。
#
# 策略：同一客户端会话复用同一身份与 begin_at。
#   1) 客户端显式给了会话键（X-Conversation-Id / session_id / metadata…）就用它；
#   2) 没给时退化为「首条 user 消息 + 系统提示 + 模型」的摘要 —— Codex 全量回放
#      历史，首条消息在一次对话里恒定，因此同一对话能稳定命中；
#   3) 两者都拿不到 -> 保持旧行为（每次新造），绝不把无关请求并成一条。
# 可用 QD_CONVERSATION_SHARE=0 整体关闭（回到 v1.7.14 行为）。
CONVERSATION_SHARE = (os.environ.get("QD_CONVERSATION_SHARE") or "1").strip() \
    not in ("0", "false", "False", "no")
_CONV_TTL_SECS = float(os.environ.get("QD_CONVERSATION_TTL") or 6 * 3600)
_CONV_MAX_ENTRIES = 4096
_CONVERSATIONS = OrderedDict()      # key -> {"id","begin_at","at"}
_CONV_LOCK = threading.Lock()


def _conversation_key_from_history(payload):
    """会话判据：**最后一条 user 消息** + 系统提示 + 模型（issue #128 后续）。

    为什么不是首条：Codex 全量回放历史，若用首条，整个窗口（可能几小时）永远是
    同一个 id —— 用量面板里就成了一条超长对话，看不出「这次对话花了多久」。
    用最后一条 user 消息：同一次对话内（工具步进时）它不变，用户下一次提问才变，
    正好等于「一次对话 = 一次用户提问 + 后续工具步进」。
    """
    if not isinstance(payload, dict):
        return ""
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return ""
    last_user = ""
    system = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str((p or {}).get("text") or "")
                                for p in content if isinstance(p, dict))
        text = str(content or "")
        if role == "system" and not system:
            system = text[:200]
        elif role == "user":
            last_user = text[:400]        # 一直覆盖到最后一条
    if not last_user:
        return ""
    raw = "\x00".join([str(payload.get("model") or ""), system, last_user])
    return "turn:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _conversation_window_key(payload, client_session_key=None):
    """窗口级会话键（整段对话恒定）：显式键优先，否则用「system + 首条 user + 模型」。

    issue #136：上游的 prompt 缓存是**按 session_id** 命中的，所以会话窗口内
    必须复用同一个 session_id；而用量归组要按「一次提问」（#128 后续）。
    两者解耦：窗口键喂 session_id，提问键喂 business.id / chat_record_id。
    """
    key = str(client_session_key or "").strip()
    if key:
        return key
    if not isinstance(payload, dict):
        return ""
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return ""
    system = ""
    first_user = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str((p or {}).get("text") or "")
                                for p in content if isinstance(p, dict))
        text = str(content or "")
        if role == "system" and not system:
            system = text[:200]
        elif role == "user" and not first_user:
            first_user = text[:400]
        if system and first_user:
            break
    if not first_user:
        return ""
    # 只用「模型 + 首条 user 消息」：system 里常带**每轮都会变**的运行时上下文
    # （cwd / git 状态 / 当前时间），把它算进键会让窗口键每轮都变 → session_id
    # 每轮都换 → 上游 prompt 缓存永远打不中（issue #136 的第二层原因）。
    raw = "\x00".join([str(payload.get("model") or ""), first_user])
    return "win:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def conversation_identity(payload, client_session_key=None):
    """本请求的上游会话身份（issue #128 / #136）。

    返回 {"id", "window_id", "begin_at"} 或 None：
      · id        —— **按一次提问**（用法归组：business.id / chat_record_id）
      · window_id —— **按整段对话**（喂 session_id，保证上游 prompt 缓存命中）
      · begin_at  —— 本次提问的起点（用量面板据此显示单次对话时长）

    #136 的根因就是这两者被合成一个：v1.7.17 把 id 改成「一次提问一个」，
    session_id 跟着变，于是每次新提问上游缓存都从 0 开始（面板上就是
    98% ↔ 0% 来回跳）。现在窗口内 session_id 恒定，提问边界只影响归组。
    """
    if not CONVERSATION_SHARE:
        return None
    window_key = _conversation_window_key(payload, client_session_key)
    key = str(client_session_key or "").strip()
    if key:
        # issue #128 后续：用量按「一次提问」归组（用户下次提问才换 id）。
        _turn = _conversation_key_from_history(payload)
        if _turn:
            key = key + "|" + _turn
    else:
        key = _conversation_key_from_history(payload)
    if not key:
        return None
    now = time.time()
    with _CONV_LOCK:
        entry = _CONVERSATIONS.get(key)
        if entry is not None and now - entry["at"] <= _CONV_TTL_SECS:
            entry["at"] = now
            _CONVERSATIONS.move_to_end(key)
            return {"id": entry["id"], "begin_at": entry["begin_at"],
                    "window_id": entry.get("window_id") or entry["id"]}
        entry = {"id": str(uuid.uuid4()), "begin_at": int(now * 1000),
                 "window_id": window_key or "", "at": now}
        _CONVERSATIONS[key] = entry
        _CONVERSATIONS.move_to_end(key)
        while len(_CONVERSATIONS) > _CONV_MAX_ENTRIES:
            _CONVERSATIONS.popitem(last=False)
        return {"id": entry["id"], "begin_at": entry["begin_at"],
                "window_id": entry.get("window_id") or entry["id"]}



def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id") or
        headers.get("Conversation-Id") or
        headers.get("X-Session-Id") or
        headers.get("Session-Id") or
        payload.get("conversation_id") or
        payload.get("session_id") or
        (payload.get("metadata") or {}).get("conversation_id")
    )
    if key:
        return str(key).strip()
    return None


# 上游瞬时故障（与客户端参数无关，值得同账号快速重试）
TRANSIENT_HTTP_CODES = (418, 500, 502, 503, 504)
TRANSIENT_MAX_RETRIES = 2          # 同账号额外重试次数（1s、2s 退避）

# issue #38：上游「排队」（403 + isQueued/10605 + retryAfterSeconds）时，
# 网关**等队列放行后重试**，而不是把排队 JSON 当错误交给客户端——客户端会把
# 它当成请求被拒（直接结束本轮）。三档都是可调的，默认值保守：
#   单轮最长等 QUEUE_WAIT_MAX_SECS；整次请求总预算 QUEUE_WAIT_BUDGET_SECS；
#   最多等 QUEUE_MAX_ROUNDS 轮。超出预算仍按原错误返回（fail-open 语义不变）。
# issue #57 后续（逆向官方客户端）：官方排队是**独立轮询协议**，总等待默认 60 分钟
# （QODER_MODEL_QUEUE_MAX_WAIT_MS=3600000），每轮 sleep retryAfterSeconds。
# 网关是同步 HTTP，不能真等 60 分钟；这里给分钟级预算（默认 10 分钟 / 6 轮），
# 仍可用 QD_QUEUE_* 覆盖。
QUEUE_MAX_ROUNDS = int(os.environ.get("QD_QUEUE_MAX_ROUNDS") or 3)
QUEUE_WAIT_MAX_SECS = int(os.environ.get("QD_QUEUE_WAIT_MAX_SECS") or 180)
QUEUE_WAIT_BUDGET_SECS = int(os.environ.get("QD_QUEUE_WAIT_BUDGET_SECS") or 600)
QUEUE_WAIT_JITTER_SECS = float(os.environ.get("QD_QUEUE_WAIT_JITTER") or 2.0)

# issue #87：排队「可见」还是「快速失败」——
#   visible（默认）：流式请求**先发响应头**，排队期间持续发心跳 + 排队提示帧，
#                    客户端能看到「上游排队中（已等待 Ns）」；超预算发中文错误帧；
#   fast          ：不等，立即 503 + Retry-After + 中文原因（客户端自己重试）；
#   wait          ：旧行为（在 open_upstream 内静默 sleep，客户端零字节——就是
#                    「卡住」的来源，保留仅为兼容）。
QUEUE_MODE = (os.environ.get("QD_QUEUE_WAIT_MODE") or "visible").strip().lower()
if QUEUE_MODE not in ("visible", "fast", "wait"):
    QUEUE_MODE = "visible"


# #158: response-scoped observations, never a provider-risk verdict. Raw error
# text, account IDs, endpoints and model names are not retained in the snapshot.
_QUEUE_OBSERVATIONS = OrderedDict()
_QUEUE_OBSERVATION_LOCK = threading.Lock()
_QUEUE_OBSERVATION_TTL = 86400
_QUEUE_OBSERVATION_GAP = 900
_QUEUE_OBSERVATION_LIMIT = 256


def queue_fields(detail):
    """Read bounded nested JSON (some providers encode message as JSON text)."""
    pending = [detail]
    found = {}
    for _ in range(16):
        if not pending:
            break
        value = pending.pop(0)
        if isinstance(value, str):
            if len(value) > 8192:
                continue
            try:
                value = json.loads(value)
            except (TypeError, ValueError, RecursionError):
                continue
            if isinstance(value, str):
                pending.append(value)
                continue
        if not isinstance(value, dict):
            continue
        for key in ("code", "isQueued", "queueType", "queueCount",
                    "retryAfterSeconds", "serviceAvailable"):
            if key in value:
                found[key] = value[key]
        for key in ("body", "message", "error", "data"):
            if isinstance(value.get(key), (dict, str)):
                pending.append(value[key])
    if str(found.get("code")) != "10605" and found.get("isQueued") is not True:
        return None
    kind = found.get("queueType")
    kind = kind if isinstance(kind, str) and re.fullmatch(r"p[0-9]", kind) else "unknown"
    result = {"queue_type": kind, "service_available": None,
              "retry_after": None, "queue_count": None, "reason": "unknown"}
    if type(found.get("serviceAvailable")) is bool:
        result["service_available"] = found["serviceAvailable"]
    for key, source in (("retry_after", "retryAfterSeconds"), ("queue_count", "queueCount")):
        value = found.get(source)
        if type(value) is int and 0 <= value <= 86400:
            result[key] = value
    return result


def _queue_observation_prune(now):
    for key, value in list(_QUEUE_OBSERVATIONS.items()):
        if now - value["last"] > _QUEUE_OBSERVATION_TTL:
            del _QUEUE_OBSERVATIONS[key]


def note_queue_observation(scope, detail=None, success=False):
    if not isinstance(scope, str) or not scope:
        return
    fields = queue_fields(detail) if not success else None
    if not fields and not success:
        return
    now = time.monotonic()
    with _QUEUE_OBSERVATION_LOCK:
        _queue_observation_prune(now)
        if success:
            _QUEUE_OBSERVATIONS.pop(scope, None)
            return
        item = _QUEUE_OBSERVATIONS.get(scope)
        if item is None or now - item["last"] > _QUEUE_OBSERVATION_GAP:
            item = {"first": now, "count": 0}
        item.update(last=now, count=item["count"] + 1, fields=fields)
        _QUEUE_OBSERVATIONS[scope] = item
        _QUEUE_OBSERVATIONS.move_to_end(scope)
        while len(_QUEUE_OBSERVATIONS) > _QUEUE_OBSERVATION_LIMIT:
            _QUEUE_OBSERVATIONS.popitem(last=False)


def queue_observation_snapshot():
    now = time.monotonic()
    with _QUEUE_OBSERVATION_LOCK:
        _queue_observation_prune(now)
        items = list(_QUEUE_OBSERVATIONS.values())
        active = [v for v in items if now - v["last"] <= _QUEUE_OBSERVATION_GAP]
        persistent = [v for v in active if v["count"] >= 3 and v["last"] - v["first"] >= 900]
        types = {}
        for item in active:
            kind = item["fields"]["queue_type"]
            types[kind] = types.get(kind, 0) + 1
        return {"reason": "unknown", "persistent": bool(persistent),
                "active_scopes": len(active), "persistent_scopes": len(persistent),
                "observations": sum(v["count"] for v in active), "queue_types": types,
                "max_observed_span_seconds": int(max((v["last"] - v["first"] for v in active), default=0)),
                "retention_seconds": _QUEUE_OBSERVATION_TTL,
                "process_only": True, "max_gap_seconds": _QUEUE_OBSERVATION_GAP,
                "scope_limit": _QUEUE_OBSERVATION_LIMIT,
                "hint": "持续排队不等于已确认风控；p4 与 serviceAvailable 不能单独证明原因，需官方客户端对照或上游确认。"}


def _queue_scope(account, model, endpoint=None):
    if endpoint is None:
        endpoint = qoder_accounts.account_realm_config(account).get("gateway", "")
    value = (str(getattr(account, "uid", "")), str(endpoint), str(model))
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def queue_mode():
    """当前排队策略（visible / fast / wait）。"""
    return QUEUE_MODE


QUEUE_MSG_WAIT_ZH = "上游模型排队中（10605 / RESOURCE_LIMIT）：已等待 %d 秒，仍在队列中"
QUEUE_MSG_FAIL_ZH = ("上游模型排队超时（已等待 %d 秒，上限 %d 秒）：请稍后重试，"
                     "请按建议退避；持续排队原因未确认，不应无限重试或据此判定账号正常/风控。")


def visible_queue_acquire(seed_exc, reopen, emit, budget_secs=None, started=None):
    """visible 模式（issue #87）：在**已建立的流**里等待排队并持续报告进度。

    - emit("queue", info)   ：每轮发一次排队状态（客户端可见「已等待 Ns」）
    - emit("timeout", info) ：超出预算，info 里有中文原因
    - emit("error", info)   ：排队后重试遇到其它错误
    返回 reopen() 的结果；超预算或失败返回 None（调用方负责收尾）。
    """
    budget = int(QUEUE_WAIT_BUDGET_SECS if budget_secs is None else budget_secs)
    start = (time.time() - float(getattr(seed_exc, "waited", 0) or 0)
             if started is None else started)
    exc = seed_exc
    while True:
        waited = int(max(0, time.time() - start))
        emit("queue", {"queued": True, "waited_seconds": waited,
                       "wait_seconds": int(exc.retry_after),
                       "budget_seconds": budget,
                       "message": QUEUE_MSG_WAIT_ZH % waited})
        if time.time() + exc.retry_after > start + budget:
            emit("timeout", {"waited_seconds": waited, "budget_seconds": budget,
                             "code": "upstream_queued",
                             "message": QUEUE_MSG_FAIL_ZH % (waited, budget)})
            return None
        time.sleep(min(int(exc.retry_after), 60) + 1
                   + random.uniform(0, QUEUE_WAIT_JITTER_SECS))
        try:
            return reopen()
        except UpstreamQueued as exc2:
            exc = exc2
        except Exception as exc3:
            emit("error", {"message": "排队等待后重试失败：%s" % str(exc3)[:160],
                           "code": "upstream_queued_retry_failed"})
            return None


class UpstreamQueued(UpstreamStatus):
    """上游 10605 排队（issue #87）。

    携带 retryAfterSeconds，供 fast（立即返回 503）与 visible（流内等待）使用；
    继承 UpstreamStatus 以保证既有 except 分支不会把它当未知错误。
    """

    def __init__(self, retry_after, detail="", waited=0):
        retry_after = int(retry_after or 0)
        # 503 而不是 403：排队是「暂时不可用」，客户端应当重试而不是放弃
        super(UpstreamQueued, self).__init__(503, detail or "10605 queued")
        self.retry_after = max(1, retry_after)
        self.detail = detail or ""
        self.waited = int(waited)



_QUALITY_CACHE = {"at": 0.0, "data": None}


def quality_snapshot(limit=500, ttl=30.0):
    """面板质量指标（issue #87）：TTFT p50/p90、输出速度、排队率、回读命中。

    数据来自 usage/usage.jsonl 尾部（跳过没有 account 的测试夹具行），
    30 秒缓存，避免面板轮询把 JSON 解析做满。
    """
    now = time.time()
    if _QUALITY_CACHE["data"] and now - _QUALITY_CACHE["at"] < ttl:
        return _QUALITY_CACHE["data"]
    ttfts, tps = [], []
    rows = 0
    try:
        with open(USAGE_LOG, encoding="utf-8", errors="ignore") as fh:
            tail = fh.readlines()[-limit:]
        for line in tail:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("account"):
                continue          # 测试夹具 / 无账号行不计入
            rows += 1
            if isinstance(r.get("ttft_ms"), (int, float)):
                ttfts.append(float(r["ttft_ms"]))
            if r.get("completion_tokens") and r.get("gen_ms"):
                tps.append(float(r["completion_tokens"])
                           / max(0.001, float(r["gen_ms"]) / 1000.0))
    except Exception:
        pass

    def _pct(values, p):
        if not values:
            return None
        values = sorted(values)
        return round(values[min(len(values) - 1, int(len(values) * p))], 1)

    data = {
        "requests": rows,
        "ttft_p50_ms": _pct(ttfts, 0.5),
        "ttft_p90_ms": _pct(ttfts, 0.9),
        "tps_p50": round(tps and sorted(tps)[len(tps) // 2], 1) if tps else None,
        "queue_total": int(RISK_STATS.get("queue") or 0),
        "queue_rate": (round((RISK_STATS.get("queue") or 0) / rows, 4)
                       if rows else None),
        "recovery": {"leak": int(RECOVERY_STATS.get("leak") or 0),
                     "dsml": int(RECOVERY_STATS.get("dsml") or 0)},
        "queue_mode": queue_mode(),
    }
    _QUALITY_CACHE.update({"at": now, "data": data})
    return data


# issue #64：风控/排队信号计数（只读观测，供 /health 与面板排查）。
RISK_STATS = {"queue": 0, "rate": 0, "rejected": 0, "version_gate": 0,
              "last": None}
# issue #87：回读命中计数（面板「回读命中率」用）——leak=文本复述、dsml=原生标记
RECOVERY_STATS = {"leak": 0, "dsml": 0}
_RISK_LOCK = threading.Lock()


def note_risk(kind, detail=""):
    """记一次风控/排队信号（queue/rate/rejected/version_gate）。"""
    try:
        with _RISK_LOCK:
            if kind in RISK_STATS:
                RISK_STATS[kind] = int(RISK_STATS[kind]) + 1
            RISK_STATS["last"] = {
                "kind": kind,
                "at": time.strftime("%m-%d %H:%M:%S"),
                "detail": str(detail or "")[:120],
            }
    except Exception:
        pass


def risk_snapshot():
    """给 /health 用的快照（不阻塞、不含凭证）。"""
    with _RISK_LOCK:
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in RISK_STATS.items()}


def _queue_wait_seconds(detail):
    """从上游 403/排队响应体里解析「等多久」；不是排队信号则返回 0。

    形如：{"code":"10605","message":"...","isQueued":true,
           "retryAfterSeconds":30}
    """
    d = detail or ""
    low = d.lower()
    if "10605" not in d and "isqueued" not in low and "queue" not in low:
        return 0
    m = re.search(r'retryAfterSeconds\D+(\d+)', d)
    if not m:
        return 30                       # 排队但没给时长：按 30s 保守处理
    try:
        return max(1, int(m.group(1)))
    except Exception:
        return 30
CHECKIN_MIN_GAP = 1.0              # 签到防风控：账号之间的最小间隔（秒）
_CLIENT_FAULT_MARKERS = (
    "invalid_parameter_error",     # 如 Range of max_tokens 校验失败
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    '"Range of ',
    # 上游内容安全审核：确定性拒绝，重试无效（本轮日志实证
    # InternalError.Algo.DataInspectionFailed: Input text data may contain
    # inappropriate content.）
    "DataInspectionFailed",
    "inappropriate content",
    "input text data may contain",
    "ContentFilter",
    "SensitiveContent",
)

# 内容审核类错误（用户输入侧问题，需专门的中文解释）
_CONTENT_POLICY_MARKERS = (
    "DataInspectionFailed",
    "inappropriate content",
    "input text data may contain",
    "ContentFilter",
    "SensitiveContent",
)

# 请求自身的确定性错误（issue #59 跟进）：换号也是同一个请求体，结果不会变。
# 刻意不含 authentication_error / permission_error——那两类可能是账号级问题
# （凭证失效/无权限），换号有意义，仍走冷却轮换。
_REQUEST_FAULT_MARKERS = ("invalid_parameter_error", "invalid_request_error",
                          '"Range of ') + _CONTENT_POLICY_MARKERS


def _is_request_fault(code, detail):
    """请求自身的确定性错误（413 出站超限/参数非法/内容审核）：换号不改变结果。"""
    return code == 413 or any(m in (detail or "")
                              for m in _REQUEST_FAULT_MARKERS)


def _is_image_conversion_timeout(detail):
    """Provider image conversion timeout, distinct from worker first-token timeout."""
    text = str(detail or "").lower()
    return bool(re.search(r"image[ _-]+conversion[ _-]+(?:timeout|timed[ _-]+out)", text))


def _is_first_token_timeout(detail):
    """上游「首字超时」类 504（issue：#137 现场 DSH 每轮卡 4-5 分钟后失败）。

    这类错误的特征是上游 worker 迟迟不产出首 token；**同账号立刻重试几乎必然
    再超时**，只会把客户端拖到 idle timeout。应当直接换账号（换 worker）而不是
    原地退避重试。
    """
    text = str(detail or "").lower()
    return ("first token timeout" in text or "first_token_timeout" in text
            or "upstream timeout" in text)


def _is_transient_upstream(code, detail):
    """判断一次上游 HTTP 错误是否属于瞬时故障（可重试）。

    - 客户端参数/权限类错误（invalid_parameter_error 等）→ 永不重试
    - 418（上游把自己的 provider 故障包装成 418+provider_error）与 5xx → 瞬时
    - 其余 4xx 带 provider_error（"Error in upstream response"）→ 瞬时
    """
    detail = detail or ""
    if code in (401, 403, 429):
        return False        # 凭证/频控各自有专门处理路径，不属瞬时重试类
    if any(m in detail for m in _CLIENT_FAULT_MARKERS):
        return False
    if code in TRANSIENT_HTTP_CODES or code >= 500:
        return True
    if 400 <= code < 500 and "provider_error" in detail:
        return True
    return False


def _is_transient_transport(exc):
    """传输层瞬时故障（对 qoder.sh 的 TLS/连接抖动很常见）：可同账号重试。

    覆盖 SSL EOF/重置、连接重置/中止、超时、以及 URLError 包装的上述原因
    （含按字符串描述判断的情形，如 "SSL: UNEXPECTED_EOF_WHILE_READING"）。
    """
    if isinstance(exc, urllib.error.URLError):
        return _is_transient_transport(getattr(exc, "reason", None))
    if isinstance(exc, (ssl.SSLError, ConnectionResetError,
                        ConnectionAbortedError, TimeoutError,
                        ConnectionError, OSError)):
        return True
    if isinstance(exc, str):
        low = exc.lower()
        return any(k in low for k in ("ssl", "eof", "reset", "timed out",
                                      "broken pipe", "connection"))
    return False


# issue #64：容器运行时的自检项。COSY 签名带时间戳，容器时钟漂移会直接 401，
# 这是「容器里莫名失效」的常见原因之一，必须在启动时看得见。
_CLOCK_SKEW = {"seconds": None, "checked_at": 0.0}


def _measure_clock_skew(timeout=4.0):
    """用上游 Date 头测时钟漂移（无鉴权、只发 HEAD，结果缓存 10 分钟）。"""
    now = time.time()
    if _CLOCK_SKEW["seconds"] is not None and now - _CLOCK_SKEW["checked_at"] < 600:
        return _CLOCK_SKEW["seconds"]
    # 走 qoder_accounts 的代理感知 opener（#27：代理只认环境变量，且每请求现建），
    # 不在这里直连——否则容器里设了 HTTPS_PROXY 也测不准、还破坏出站基线。
    try:
        skew = qoder_accounts.upstream_clock_skew(timeout=timeout)
        if skew is None:
            return None
        _CLOCK_SKEW.update({"seconds": skew, "checked_at": now})
        return skew
    except Exception:
        return None


def env_report():
    """容器/部署环境自检（给启动日志与 /health 用，不含凭证）。"""
    try:
        writable = os.access(ACCOUNTS_DIR, os.W_OK)
    except Exception:
        writable = False
    ident = os.path.join(ACCOUNTS_DIR, "machine_identity.json")
    return {
        "container": _in_container(),
        "proxy": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                      or os.environ.get("https_proxy") or os.environ.get("http_proxy")),
        "accounts_writable": bool(writable),
        "identity_cached": os.path.exists(ident),
        "clock_skew_s": _CLOCK_SKEW["seconds"],
        "traffic_guard": _traffic_guard_mode() or "auto",
    }


def _in_container():
    """是否跑在容器里（issue #57 的风控告警用，判定保守）。"""
    try:
        if os.path.exists("/.dockerenv"):
            return True
        with open("/proc/1/cgroup", encoding="utf-8", errors="ignore") as fh:
            blob = fh.read()
        return ("docker" in blob) or ("containerd" in blob) \
            or ("kubepods" in blob)
    except Exception:
        return False


def _friendly_net_error(exc, target=""):
    """把 urlopen/连接类异常翻成可操作的中文（issue #52）。

    报告者点「测试」只看到 `<urlopen error [WinError 10061] 由于目标计算机积极拒绝
    无法连接。>`——这是**本机连不上某个地址**，不是上游业务错误。最常见三种原因：
    ① 服务没起/端口不对；② 环境变量代理指向一个已经关掉的本地代理（#27 只认 env）；
    ③ 域名/端口写错。这里把它们直接写进错误里。
    """
    s = str(exc or "")
    low = s.lower()
    tgt = target or "目标地址"
    if ("10061" in s or "connection refused" in low
            or "actively refused" in low or "积极拒绝" in s):
        return ("连接被拒绝（WinError 10061）：%s 没有在监听。请检查 ①服务是否已启动、"
                "端口是否一致 ②环境变量 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 指向的"
                "代理是否还在运行（网关只认环境变量，不读系统代理）③地址/域名是否"
                "写错。原始错误：%s" % (tgt, s[:180]))
    if "10060" in s or "timed out" in low or "超时" in s:
        return ("连接超时：%s 不可达（网络/代理/防火墙）。原始错误：%s"
                % (tgt, s[:180]))
    if "10054" in s or "reset" in low or "远程主机强迫关闭" in s:
        return ("连接被重置：%s 主动断开（常见于代理拦截或 TLS 握手被打断）。"
                "原始错误：%s" % (tgt, s[:180]))
    return s[:300]


# issue #137：排队 / 额度这类「不是凭证问题」的错误，上游详情里带着信封层的
# "code":"403"。不少客户端只看错误文字归类——DSH 的 pi-ai 适配层第一条规则就是
# /\b(?:401|403)\b/ -> AUTH，于是界面显示成「API 密钥无效」。这里把详情里独立的
# 401/403 改写成 HTTP_401/HTTP_403（\b 两侧都是单词字符，不再命中），并在文末
# 附一句客户端分类器认得的英文原因：排队 -> rate limit，额度 -> quota exhausted /
# usage limit reached。
_AUTH_LIKE_CODE_RE = re.compile(r"(?<![0-9A-Za-z_])(401|403)(?![0-9A-Za-z_])")
_QUEUED_CLIENT_HINT = " [rate limit: upstream model queue is full (10605), retry later]"
_QUOTA_CLIENT_HINT = (" [usage limit reached: upstream quota or plan access rejected (112); "
                      "this does not establish that the displayed balance is zero]")
_DAILY_CLIENT_HINT = (" [usage limit reached: upstream daily request count "
                      "exceeded (110), retry after local midnight]")
# 这些错误类型对客户端一律不用 401/403 状态码（改 429，与 OpenAI insufficient_quota 一致）
_NON_AUTH_ERROR_TYPES = frozenset({"upstream_queued", "quota_exhausted",
                                   "quota_daily_limit"})


def tool_echo_mode():
    """工具协议残片的收尾策略（issue #135）：strict | lenient。

    优先级：环境变量 QD_TOOL_ECHO_MODE（显式覆盖）> 面板设置 > strict。
    面板改了就生效，不必重启（每次请求现读）。
    """
    env = (os.environ.get("QD_TOOL_ECHO_MODE") or "").strip().lower()
    if env in ("strict", "lenient"):
        return env
    try:
        val = str(qoder_settings.tool_echo_mode(ACCOUNTS_DIR) or "").strip().lower()
    except Exception:
        val = ""
    return val if val in ("strict", "lenient") else TOOL_ECHO_MODE


def anthropic_thinking_mode(key_entry=None):
    """Anthropic 入口 thinking block 输出策略（issue #146）：empty | off。

    优先级：1) 每 Key 覆盖  2) 面板全局设置  3) 环境变量 QD_ANTHROPIC_THINKING  4) off。
    """
    if isinstance(key_entry, dict):
        key_mode = str(key_entry.get("anthropic_thinking") or "").strip().lower()
        if key_mode in ("empty", "off"):
            return key_mode
    try:
        panel_mode = str(qoder_settings.anthropic_thinking(ACCOUNTS_DIR) or "").strip().lower()
    except Exception:
        panel_mode = ""
    if panel_mode in ("empty", "off"):
        return panel_mode
    env_mode = (os.environ.get("QD_ANTHROPIC_THINKING") or "").strip().lower()
    return env_mode if env_mode in ("empty",) else "off"

def _neutralize_auth_codes(text):
    """把文字里独立出现的 401/403 改写成 HTTP_401/HTTP_403（issue #137）。"""
    return _AUTH_LIKE_CODE_RE.sub(r"HTTP_\1", str(text or ""))


def _client_error_status(code, err_type):
    """发给客户端的状态码：非凭证类错误不用 401/403（issue #137）。"""
    try:
        c = int(code)
    except Exception:
        return code
    if err_type in _NON_AUTH_ERROR_TYPES and c in (401, 403):
        return 429
    return c


def friendly_upstream_error(code, detail):
    """把上游错误转成对客户端可读的消息；瞬时故障给出重试指引。

    返回 (message, err_type)。
    """
    detail = (detail or "").strip()
    try:
        code_i = int(code)
    except Exception:
        code_i = 502
    _safe = _neutralize_auth_codes(detail)
    if _is_image_conversion_timeout(detail):
        return ("上游图片转换超时 (HTTP %s)：图片转换服务在时限内未完成。"
                "这不是 Key 失效；网关保留图片，限制自动重试且不冷却/轮换账号。"
                "可稍后重试；若重复出现，请分批发送图片或检查原图格式。"
                "上游详情：%s" % (code_i, _safe[:400] or "(无详情)"),
                "upstream_image_conversion_timeout")
    # issue #87：排队（fast 模式在这里统一给出中文原因，所有入口共用）
    if ("10605" in detail or "isQueued" in detail
            or "RESOURCE_LIMIT" in detail) and _to_int_status(code) in (403, 503):
        return ("上游模型排队中（10605 / RESOURCE_LIMIT）：请稍后重试，"
                "请按建议退避；持续排队原因未确认，不能仅凭队列号排除账号或出口因素。"
                "上游详情：%s" % (_safe[:160] or "(无详情)")
                + _QUEUED_CLIENT_HINT, "upstream_queued")
    # issue #108：额度耗尽也走 403，但重试无效——让客户端看懂该充值/换号
    _quota_kind = _quota_cooldown(detail)[0]
    if _quota_kind == "exhausted":
        return ("上游拒绝本次模型额度/套餐权限（112）：这不等于账号总余额为零。"
                "请核对实际出口账号、当前模型可用额度与套餐权限；不要只看两区合计余额。"
                "上游详情：%s" % (_safe[:160] or "(无详情)")
                + _QUOTA_CLIENT_HINT, "quota_exhausted")
    # issue #137：每日次数上限（110）此前落到兜底分支，原样显示成 upstream 403
    if _quota_kind == "daily":
        return ("上游账号今日请求次数已达上限（110）：本地 0 点后恢复，"
                "或启用其它账号。这不是你的 Key 问题。上游详情：%s"
                % (_safe[:160] or "(无详情)") + _DAILY_CLIENT_HINT,
                "quota_daily_limit")
    # issue #64：版本过低是硬门控（官方客户端会自己升级，网关只能提示）
    if "430" in str(code):
        return ("客户端/网关版本过低（430）：上游要求升级后才能继续。"
                "请升级到最新版；这不是账号或网络问题。", "version_too_low")
    # issue #59：出站体积超限（本地拦下，没发出去）
    if "payload too large" in detail:
        return ("请求体过大（超过上游 30MB 限制）：网关已在本地拦下、未发送。"
                "请减少图片数量/尺寸（或改用更小的图）后重试。详情：%s"
                % detail[:160], "payload_too_large")
    # issue #38/#51：上游排队 / 资源不足（10605 RESOURCE_LIMIT）
    if "10605" in detail or "isQueued" in detail or "RESOURCE_LIMIT" in detail:
        return ("上游排队中（10605 / RESOURCE_LIMIT）：已按 retryAfterSeconds 等待并重试，"
                "仍未放行；持续排队原因未确认，不能仅凭队列号判定容量或风控。"
                "上游详情：%s" % (_safe[:200] or "(无详情)")
                + _QUEUED_CLIENT_HINT, "upstream_queued")
    if "pricingUrl" in detail:
        return ("上游返回套餐购买/权限入口，但未明确报告余额耗尽。请核对本次模型和出口账号的套餐权限。"
                "上游详情：%s" % _safe[:200], "upstream_plan_rejected")
    if "missing_tool_name" in detail or "missing_tool_call" in detail:
        return ("上游工具调用缺少名称或实际调用，已阻止伪装为正常结束；请重试该轮。", "upstream_incomplete_tool_call")
    if "reasoning_only" in detail or "empty_tool_turn" in detail:
        return ("上游只返回思考或空响应，没有正文及可执行工具调用；本轮未完成，未把思考改成正文。", "upstream_empty_response")
    if "truncated_arguments" in detail:
        return ("上游工具参数被截断或不是合法 JSON；本轮未完整结束，请重试，不会重复已发送的输出。", "upstream_incomplete_tool_call")
    # 0) issue #37：工具参数非法（确定性、可重试但不是账号问题）
    if "invalid JSON arguments" in detail:
        return ("上游生成的工具调用参数不是合法 JSON，本轮**未执行任何工具**；"
                "网关已按协议返回失败，请重试该轮。上游详情：%s"
                % (detail[:200] or "(无详情)"),
                "invalid_tool_arguments")
    # 1) 上游内容安全审核（确定性拒绝，先于瞬时判断——重试无效）
    if any(m in detail for m in _CONTENT_POLICY_MARKERS):
        return ("上游内容安全审核未通过 (DataInspectionFailed)：输入可能含不当内容，"
                "属确定性拒绝、重试无效。请检查/缩短输入（系统提示词、超长历史、"
                "工具定义或粘贴的代码/文本）后重试。上游详情：%s"
                % detail[:300],
                "content_policy_rejected")
    if _is_first_token_timeout(detail):
        return ("上游首字/推理超时 (HTTP %s)：本次未拿到可用响应；不代表 Key 或额度失效。"
                "网关不再针对该超时重复请求同一账号。上游详情：%s" % (code_i, _safe[:200]),
                "upstream_transient_error")
    if _is_transient_upstream(code_i, detail) or (
            "provider_error" in detail and "invalid_" not in detail):
        return ("上游瞬时故障 (HTTP %s)：网关已对同账号自动重试仍失败，"
                "请稍后重试。上游详情：%s"
                % (code_i, detail[:300] or "(无详情)"),
                "upstream_transient_error")
    return ("upstream %s: %s" % (code_i, detail)), "upstream_error"


def _to_int_status(status):
    """信封 statusCodeValue 可能是 int 或 str，统一成 int（失败回 502）。"""
    try:
        return int(status)
    except Exception:
        return 502


# issue #108：上游对「额度耗尽」/「每日计费上限」也回 403，与「凭证被拒」同码不同因：
# 重试和 60s 冷却都不会变好，账号还会被反复挑中（每次白等 60-105s）。
_QUOTA_112_COOLDOWN = 1800


def _quota_cooldown(detail):
    """额度类 403 的 (病因, 冷却秒数)；不是额度问题返回 ('', 0)。

    112 额度耗尽 -> 30 分钟；110 每日次数上限 -> 到本地 0 点（封顶 6h，免得时区乱跳）。
    detail 可能被套在字符串里（转义形态 \\"code\\":\\"112\\"），先去掉反斜杠再匹配。
    """
    s = str(detail or "").replace("\\", "")
    if re.search(r'"code"\s*:\s*"?112\b', s):
        return "exhausted", _QUOTA_112_COOLDOWN
    if "Billing daily count exceeded" in s or re.search(r'"code"\s*:\s*"?110\b', s):
        now = time.time()
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)) + 86400
        # +1：向上取整到整秒，免得日志把 0 点显示成 23:59
        return "daily", int(max(60, min(6 * 3600, midnight - now))) + 1
    return "", 0


def _note_quota_cooldown(account, status, detail, where, model=None):
    """额度类 403：按病因冷却账号并打日志；返回 True 表示已处理（调用方应轮换）。

    issue #108：配额不是凭证失效，所以不改 enabled、也不喂降权。冷却**刻意不传**
    single_account——note_error 会把它压成 3s，单号池里 bug 会原样保留。
    """
    kind, cd = _quota_cooldown(detail)
    if not kind or not account or not hasattr(account, "note_error"):
        return False
    model_scoped = False
    if kind == "exhausted" and model:
        credits = getattr(account, "credits", None)
        try:
            positive = qoder_accounts.shared_unlimited(credits) or float(qoder_accounts.effective_remain(credits) or 0) > 0
        except (TypeError, ValueError, AttributeError):
            positive = False
        model_scoped = bool(positive)
    actual = float(account.note_error(
        "%s HTTP %s quota %s realm=%s model=%s: %s" % (where, status, kind,
            getattr(account, "realm", ""), model or "?", str(detail)[:80]),
        cooldown=300 if model_scoped else cd,
        **({"model": model} if model_scoped else {})) or 0)
    note_risk("rejected", detail)
    if kind == "exhausted":
        what = ("quota/plan rejected (112), positive balance snapshot - model cooling 5min"
                if model_scoped else "quota/plan rejected (112) - cooling 30min")
    else:
        what = "daily count limit (110) - cooling until %s" % time.strftime(
            "%m-%d %H:%M", time.localtime(time.time() + actual))
    log("account %s %s (%.0fs), rotating" % (account.uid[:8], what, actual),
        level="WARN", tag="chat")
    return True


def _handle_envelope_account_cooldown(account, exc, model=None, session_key=None):
    """信封层非 200（如 403 队列满/Token被拒、429 等）冷却该账号并解绑会话，触发下次选号轮换。"""
    if isinstance(exc, InvalidToolArguments):
        # issue #37：坏参数是模型生成内容的问题，**不是账号不可用**——
        # 记录成账号冷却会让一个健康账号被无辜停用（还会解绑会话）。
        return
    if not account or not hasattr(account, "note_error"):
        return
    status_int = _to_int_status(getattr(exc, "status", 502))
    detail = getattr(exc, "detail", "") or ""
    # 图片转换是 provider 的请求处理阶段，不是账号健康信号。
    if _is_image_conversion_timeout(detail):
        return
    # issue #71：排队**不是账号问题**——必须在解绑会话/冷却之前短路，
    # 否则单号池会被打成「no usable account」，面板看起来就是凭证失效。
    _is_queue71 = ("10605" in detail or "isQueued" in detail)
    if _is_queue71 and status_int in (401, 403) \
            and not qoder_accounts.session_dead(detail):
        note_risk("queue", detail)
        log("account %s queued via envelope (HTTP %s) - not penalising the "
            "account (issue #71)" % (account.uid[:8], status_int),
            level="WARN", tag="chat")
        return
    if session_key and POOL:
        try:
            POOL.affinity.unbind(session_key)
        except Exception:
            pass
    # #29：口径必须与 open_upstream 一致 —— 用**本区域**就绪数，而不是全局账号数，
    # 否则双区池里本区域唯一账号会被当成多号池，冷却不压 3s。
    total = _realm_ready_total(getattr(account, "realm", "") or "", model)
    # 针对 10605 / retryAfterSeconds 解析冷却时长
    retry_secs = 30
    m = re.search(r'retryAfterSeconds\D+(\d+)', detail)
    if m:
        try:
            retry_secs = int(m.group(1))
        except Exception:
            pass
    if status_int == 429:
        actual = account.note_error("envelope 429", model=model,
                                    cooldown=retry_secs)
        log("account %s throttled via envelope (429), cooling for %.0fs "
            "(requested %ds, realm_ready=%d)"
            % (account.uid[:8], float(actual or 0), retry_secs, total),
            level="WARN", tag="chat")
    elif status_int in (401, 403):
        # issue #108：额度类 403 要的是长冷却 + 换号，不是「凭证被拒」的 60s
        if _note_quota_cooldown(account, status_int, detail, "envelope", model=model):
            return
        dead = qoder_accounts.session_dead(detail)
        # 10605 排队冷却为模型级或账号级
        is_queue = "10605" in detail or "isQueued" in detail
        note_risk("queue" if is_queue else "rejected", detail)
        cd = 300 if dead else 60
        actual = account.note_error("envelope HTTP %s: %s"
                                    % (status_int, detail[:80]),
                                    cooldown=cd, single_account=(total <= 1),
                                    model=model if is_queue else None)
        log("account %s rejected via envelope (HTTP %s, queue=%s), cooling for "
            "%.0fs (requested %ds, realm_ready=%d) and rotating"
            % (account.uid[:8], status_int, is_queue, float(actual or 0), cd,
               total), level="WARN", tag="chat")
        if dead:
            account.enabled = False
            account.save(ACCOUNTS_DIR) if account.path else None
            log("account %s session dead via envelope (TOKEN_EXPIRE) - disabled"
                % account.uid[:8], level="ERROR")
    else:
        account.note_error("envelope HTTP %s: %s" % (status_int, detail[:80]),
                           cooldown=15, single_account=(total <= 1))
        # task-74 三族之三：未知信封错误 -> 降权窗口；**内容审核类除外**（那是
        # 请求内容的确定性拒绝，不代表账号有问题）；单号池不接（total<=1）。
        if total > 1 and not any(m in detail for m in _CLIENT_FAULT_MARKERS):
            account.note_unknown_failure("envelope HTTP %s" % status_int)

def _alt_realm_ready(realm, model=""):
    """另一个区是否还有可用账号（issue #71 跨区 failover 用）。

    issue #83：`realm` 可能是 None（#76 起入口不再把「未指定」折叠成当前区）。
    旧实现 `"intl" if realm == "cn" else "cn"` 会把 None 当成「非 cn」-> 返回 cn，
    于是「切到 cn -> 又排队 -> 再切 cn」无限循环（现场日志刷屏、请求永不结束）。
    现在先把 None 解析成**实际出口**，再取另一区；解析结果与目标相同就返回空。
    """
    base = realm or detect_model_realm(model) or CURRENT_REALM
    base = base if base in ("cn", "intl") else "cn"
    other = "intl" if base == "cn" else "cn"
    if not other or other == base or exclusive_realm(model):
        return ""
    try:
        return other if POOL and POOL.count_ready(other, model=model or None) > 0 else ""
    except Exception:
        return ""


def _envelope_queue_action(exc, realm, model, state):
    """排队信封的下一步（issue #71）：failover / wait / giveup。

    返回 (action, target_realm, wait_secs)。state 是每个请求一份的字典：
    {rounds, deadline, switched}。三条规则：
      1. 另一区有就绪账号 -> 先跨区试一次（排队是区域+模型维度，本次现场就是
         cn 排队而 intl 同模型 200）；
      2. 否则按 retryAfterSeconds 等，但总轮次/总时长受请求级预算约束；
      3. 预算耗尽 -> giveup，由调用方转成 429 + Retry-After（不是 403）。
    """
    q = _envelope_queue_wait(exc)
    if q <= 0:
        return "wait", "", 0
    if not state.get("switched"):
        alt = _alt_realm_ready(realm, model)
        if alt:
            state["switched"] = True
            return "failover", alt, 0
    state["rounds"] = int(state.get("rounds") or 0) + 1
    if state["rounds"] > QUEUE_MAX_ROUNDS \
            or time.time() > float(state.get("deadline") or 0):
        return "giveup", "", q
    if q > QUEUE_WAIT_MAX_SECS:
        return "giveup", "", q
    return "wait", "", min(q + 1, QUEUE_WAIT_MAX_SECS)


# ---------------------------------------------------------------------------
# 上游输出上限（max_tokens）学习缓存（issue #101）
#
# 官方 catalog 里没有 maxOutputTokens，客户端只能踩坑试出来：超过上限时上游回
#   400 invalid_parameter_error: Range of max_tokens should be [1, 131072]
# 这里从错误文本里解析 N，按 (realm, model) 记住并持久化，之后请求直接 clamp
# （并在 /v1/models 暴露 max_output_tokens），客户端不必再猜。
# ---------------------------------------------------------------------------
_MAX_OUT_LIMITS = {}          # (realm, model) -> (limit, ts)
_MAX_OUT_LIMIT_TTL = 7 * 86400
_MAX_TOKENS_RANGE_RE = re.compile(
    r"Range of max_tokens should be \[1,\s*(\d+)\]", re.I)


def _max_out_limits_path():
    return os.path.join(USAGE_DIR, "max-output-limits.json")


def _load_max_out_limits():
    path = _max_out_limits_path()
    if _MAX_OUT_LIMITS or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for key, val in (data or {}).items():
            if not isinstance(val, dict):
                continue
            realm, _, model = key.partition("|")
            lim = int(val.get("limit") or 0)
            if lim > 0:
                _MAX_OUT_LIMITS[(realm, model)] = (lim, float(val.get("at") or 0))
    except Exception as exc:
        log("load max-output-limits failed: %s" % exc, level="WARN", tag="chat")


def _save_max_out_limits():
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        data = {}
        for (realm, model), (lim, ts) in _MAX_OUT_LIMITS.items():
            data["%s|%s" % (realm, model)] = {"limit": int(lim), "at": ts}
        with open(_max_out_limits_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log("save max-output-limits failed: %s" % exc, level="WARN", tag="chat")


def learn_max_output_limit(detail, model="", realm=None):
    """从上游 400 文本里学「该模型输出上限」，返回学到的 N（没学到返回 None）。"""
    text = str(detail or "")
    m = _MAX_TOKENS_RANGE_RE.search(text)
    if not m:
        return None
    try:
        limit = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    realm = realm or CURRENT_REALM
    keys = [(realm, model or "")]
    try:
        up = qoder_catalog.resolve_upstream_key(model, realm=realm)
        if up and up != model:
            keys.append((realm, up))
    except Exception:
        pass
    prev = _MAX_OUT_LIMITS.get(keys[0])
    if prev and prev[0] == limit:
        return limit
    for key in keys:
        _MAX_OUT_LIMITS[key] = (limit, time.time())
    _save_max_out_limits()
    log("learned max_tokens limit for %s/%s = %d (clamping from now on)"
        % (realm, model or "?", limit), level="WARN", tag="chat")
    return limit


def _resolve_upstream_key_safe(model, realm=None):
    try:
        return qoder_catalog.resolve_upstream_key(model, realm=realm) or ""
    except Exception:
        return ""


def max_output_limit(model, realm=None):
    """已学到的输出上限（含 catalog 自带的 maxOutputTokens），没有则 None。"""
    realm = realm or CURRENT_REALM
    _load_max_out_limits()
    for cand in (model or "", _resolve_upstream_key_safe(model, realm)):
        hit = _MAX_OUT_LIMITS.get((realm, cand))
        if hit and time.time() - hit[1] < _MAX_OUT_LIMIT_TTL:
            return hit[0]
    try:
        meta = catalog_meta_for(model, realm) if model else {}
    except Exception:
        meta = {}
    for k in ("maxOutputTokens", "max_output_tokens", "max_output"):
        v = (meta or {}).get(k)
        if isinstance(v, int) and v > 0:
            return v
    return None


def _context_token_count(label, meta):
    """把 1M/400K/200K 这类档位名解析成 token 数（对照官方 context_config）。"""
    s = str(label or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    cfg = (meta or {}).get("context_config") or {}
    for k, v in cfg.items():
        if str(k).strip().lower() == s.lower() and isinstance(v, dict):
            try:
                return int(v.get("token_count") or 0) or None
            except (TypeError, ValueError):
                return None
    return None


def model_context_override(payload, model_key, model, realm):
    """本请求实际生效的上下文覆盖（token 数），没有则 None。

    优先级：请求头 X-QD-Context（入口写进 payload["_qd_context"]）
            > 面板设置 model_context（按上游 key 与客户端模型名两查）。
    超出官方 context_config 的档位会被忽略并打 WARN——上游不认的窗口发过去只会 400。
    """
    raw = (payload or {}).get("_qd_context")
    source = "header"
    if raw in (None, ""):
        try:
            table = qoder_settings.model_context(ACCOUNTS_DIR)
        except Exception:
            table = {}
        raw = table.get(model_key) if model_key else None
        if raw in (None, "") and model:
            raw = table.get(model)
        source = "settings"
    if raw in (None, ""):
        return None
    meta = {}
    try:
        for m in qoder_catalog.models_for_realm(realm or CURRENT_REALM):
            if m.get("key") == (model_key or model):
                meta = m
                break
    except Exception:
        meta = {}
    tokens = _context_token_count(raw, meta)
    if tokens is None:
        log("context override %r (%s) not in official context_config for %s - ignored"
            % (raw, source, model_key or model), level="WARN", tag="chat")
    return tokens


def _model_context_snapshot():
    try:
        return dict(qoder_settings.model_context(ACCOUNTS_DIR))
    except Exception:
        return {}

def clamp_max_tokens(value, model, realm=None):
    """把请求里的 max_tokens 压到已知上限以内（返回 (value, clamped?)）。"""
    try:
        val = int(value)
    except (TypeError, ValueError):
        return value, False
    limit = max_output_limit(model, realm)
    if limit and val > limit:
        log("max_tokens %d > upstream limit %d for %s - clamped"
            % (val, limit, model or "?"), level="WARN", tag="chat")
        return limit, True
    return val, False

def _exc_http_status(exc, default=502):
    """从任意异常里取一个 HTTP 状态码（issue #100）。

    异常族有三套字段名：
      · UpstreamStatus / UpstreamQueued -> .status
      · RateLimited                    -> .http_error（**没有** .status）
      · urllib.error.HTTPError         -> .code
    取不到就回落 default —— 之前 _handle_responses / chat 直接读 exc.status，
    排队耗尽后拿到 RateLimited 就 AttributeError，把 200 的流硬生生截断。
    """
    if isinstance(exc, RateLimited):
        return 429
    for attr in ("status", "http_error", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return default


def _as_rate_limited_for_queue(exc, model=""):
    """排队类信封错误 -> RateLimited（429 + Retry-After）；其它原样返回。

    issue #71：排队最终以 403 抛给客户端，很容易被当成凭证失效。
    """
    q = _envelope_queue_wait(exc) if exc is not None else 0
    if q <= 0:
        return exc
    return RateLimited(None, getattr(exc, "detail", "") or "", wait=q)


def _queue_budget():
    """日志里显示的「本轮上限」：排队按 QUEUE_MAX_ROUNDS，其余按瞬时重试数。"""
    return max(TRANSIENT_MAX_RETRIES + 1, QUEUE_MAX_ROUNDS)


def _envelope_queue_wait(exc):
    """信封层排队信号 -> 建议等待秒数（issue #38 后续 / #51 补充）。

    上游有时先以 HTTP 200 建流，再把「排队」作为 SSE 信封投递：
      status=403 detail={"code":"10605","message":"{...isQueued:true...}",
                         "retryAfterSeconds":30,...}
    此前只在 **HTTP 层**做了等待，信封层仍按普通 403 处理：冷却账号 + 换号，
    换不到就直接把 403 丢给客户端（报告者看到的 request error）。
    """
    return _queue_wait_seconds(getattr(exc, "detail", "") or "")


def _envelope_retry_wait(exc, attempt):
    """重开上游前的等待秒数：排队按 retryAfterSeconds，其它按次数退避。"""
    q = _envelope_queue_wait(exc)
    if q > 0:
        return min(q + 1, QUEUE_WAIT_MAX_SECS)
    return attempt


def _take_image_retry(usage_ctx):
    """One shared image-conversion retry across HTTP and SSE for a request."""
    if not isinstance(usage_ctx, dict):
        return TRANSIENT_MAX_RETRIES > 0
    count = int(usage_ctx.get("_image_conversion_retries") or 0)
    if count >= min(1, TRANSIENT_MAX_RETRIES):
        return False
    usage_ctx["_image_conversion_retries"] = count + 1
    return True


def should_retry_envelope(exc, emitted_bytes, attempt, usage_ctx=None):
    # issue #37：坏参数是**模型内容问题**，不是瞬时上游故障——绝不自动重发
    # （否则会重复计费，且下一次很可能同样非法）。客户端想重试由它自己决定。
    """流内错误信封是否值得**重开上游**再试。

    关键前提（access log 记 200 而业务错 418 的根因）：上游先以 HTTP200
    建流，provider 故障以 SSE 信封 statusCodeValue=418 投递——此时 urlopen
    层重试覆盖不到。只要 **尚未向客户端发出任何字节**、未超预算、且错误属
    瞬时类（418/5xx/provider_error，且非客户端参数错），就值得重开。
    """
    if isinstance(exc, InvalidToolArguments):
        return False
    if emitted_bytes:
        return False
    if _is_image_conversion_timeout(getattr(exc, "detail", "")):
        # Only one reopen before output; do not multiply slow conversions by pool size.
        return attempt < min(1, TRANSIENT_MAX_RETRIES) and _take_image_retry(usage_ctx)
    _q = _envelope_queue_wait(exc)
    if _q > 0:
        # queue: wait per retryAfterSeconds, allow more rounds than transient
        return (_q <= QUEUE_WAIT_MAX_SECS
                and attempt < max(QUEUE_MAX_ROUNDS, TRANSIENT_MAX_RETRIES))
    if attempt >= TRANSIENT_MAX_RETRIES:
        return False
    status_int = _to_int_status(getattr(exc, "status", 502))
    detail = getattr(exc, "detail", "") or ""
    # 401/403 (含 10605 排队被拒) 与 429 在未吐出字节前均允许通过 reopen 换号重试
    if status_int in (401, 403, 429):
        return True
    return _is_transient_upstream(status_int, detail)


def aggregate_with_envelope_retry(resp, payload, session_key, realm, model,
                                  holder, account, usage_ctx=None):
    """非流式：流内瞬时错误信封 / 传输抖动 -> 重开上游重新聚合。

    仅用于客户端尚未收到任何字节的非流式路径。返回 (chat_obj, account)：
      - 最终信封错误以 UpstreamStatus 抛出（调用方既有分支处理：记账+友好提示）
      - 重开时 open_upstream 的 RateLimited/HTTPError 记日志后仍以**原信封**
        错误上抛（原错误才是本次请求的真实结果，且其分支已具备友好映射）
      - 传输层瞬时错误（TLS EOF 等）同样触发重开
    传入的 resp 由调用方的 with/finally 关闭；本函数只负责关闭重开的新连接。
    """
    reopen_options = {"usage_ctx": usage_ctx} if usage_ctx is not None else {}
    if usage_ctx is None:
        usage_ctx = {}
    cur = resp
    try:
        # issue #51 补充：排队可能要多等几轮（仅排队时 should_retry 才允许）
        for attempt in range(TRANSIENT_MAX_RETRIES + 1 + QUEUE_MAX_ROUNDS):
            try:
                obj = aggregate_stream(cur, model, None, holder=holder,
                                       allowed_names=_tool_names_from_payload(payload))
                return obj, account
            except UpstreamStatus as exc:
                _handle_envelope_account_cooldown(account, exc, model=model, session_key=session_key)
                if not should_retry_envelope(exc, False, attempt, usage_ctx=usage_ctx):
                    # issue #71：排队耗尽 -> 429 + Retry-After（不是 403）
                    raise _as_rate_limited_for_queue(exc, model)
                log("in-stream envelope status %s on model '%s' "
                    "(try %d/%d), reopening upstream"
                    % (exc.status, model, min(attempt + 1, _queue_budget()),
                       _queue_budget()), level="WARN", tag="chat")
                time.sleep(_envelope_retry_wait(exc, attempt + 1))
                if _is_image_conversion_timeout(exc.detail):
                    reopen_options["usage_ctx"] = usage_ctx
                    cur.close()  # Release the original team lease before same-account reopen.
                try:
                    new_resp, account, _ = open_upstream(
                        payload, session_key=session_key, target_realm=realm,
                        **dict(reopen_options, **({"only_account": account}
                            if _is_image_conversion_timeout(exc.detail) else {})))
                except Exception as reopen_exc:
                    log("reopen after envelope error failed: %s"
                        % str(reopen_exc)[:160], level="WARN", tag="chat")
                    # 以原信封错误进入既有处理路径（排队则转 429，issue #71）
                    raise _as_rate_limited_for_queue(exc, model)
                if cur is not resp:
                    try:
                        cur.close()
                    except Exception:
                        pass
                cur = new_resp
            except Exception as exc:
                if attempt >= TRANSIENT_MAX_RETRIES or \
                        not _is_transient_transport(exc):
                    raise
                log("in-stream transport error on model '%s' (try %d/%d): "
                    "%s - reopening upstream"
                    % (model, attempt + 1, TRANSIENT_MAX_RETRIES + 1,
                       str(exc)[:120]), level="WARN", tag="chat")
                time.sleep(attempt + 1)
                try:
                    new_resp, account, _ = open_upstream(
                        payload, session_key=session_key, target_realm=realm,
                        **reopen_options)
                except Exception as reopen_exc:
                    log("reopen after transport error failed: %s"
                        % str(reopen_exc)[:160], level="WARN", tag="chat")
                    raise exc
                if cur is not resp:
                    try:
                        cur.close()
                    except Exception:
                        pass
                cur = new_resp
    finally:
        if cur is not resp:
            try:
                cur.close()
            except Exception:
                pass


class PayloadTooLarge(UpstreamStatus):
    """出站请求体超过上游 SLB 限制（issue #59）。

    阿里云 SLB 对单个 POST body 有 30MB 硬限制，超过直接 RST（本地表现为
    Broken pipe）。必须**在发网络请求前**拦住：Broken pipe 会被 _is_transient_transport
    当成瞬态故障反复重试，最终把账号池打到熔断（报告者的连锁故障）。
    """

    def __init__(self, size, limit):
        super(PayloadTooLarge, self).__init__(
            413,
            "payload too large: %d bytes > %d limit; the request was not sent"
            % (int(size), int(limit)))
        self.size = int(size)
        self.limit = int(limit)


# ---------------------------------------------------------------------------
# issue #56：团队/企业订阅「自己工作用」的风险收敛
#
# 能避的风险（本模块负责）：
#   1) 单席位**并发打爆** —— 团队套餐按席位限流，一个账号被多路并发很容易撞
#      10605/RESOURCE_LIMIT；这里给每账号一个并发闸门（默认 1）。
#   2) **机器枪式**请求节奏 —— 请求启动之间加最小间隔 + 抖动，避免固定频率特征。
#   3) **误共享** —— 记录同一账号来自多少个不同客户端 IP；超过阈值打 WARN，
#      提醒你可能有人共用这把 Key（组织审计最先看到的就是这个）。
# 不能避的：额度归属、组织后台可见性、席位被回收、以及条款层面的问题。
# ---------------------------------------------------------------------------
_CLIENT_CTX = threading.local()
_ADMIN_JOBS = qoder_admin.JobRegistry()
_TEAM_GUARD_LOG = {"warned_wait": False, "ips": {}}


def _team_guard_mode():
    raw = str(os.environ.get("QD_TEAM_GUARD") or "auto").strip().lower()
    if raw in ("1", "on", "true", "yes", "enable", "enabled"):
        return "on"
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return "off"
    return "auto"


def _team_max_concurrency():
    try:
        return max(1, int(os.environ.get("QD_TEAM_MAX_CONCURRENCY") or 1))
    except Exception:
        return 1


def _team_min_gap():
    try:
        return max(0.0, float(os.environ.get("QD_TEAM_MIN_GAP_MS") or 400) / 1000.0)
    except Exception:
        return 0.4


def _is_team_account(account):
    """团队/企业账号判定（issue #56 / #90）。

    口径与 Account.is_team() 同源：组织字段（OAuth 登录即带）> userType > 共享额度池。
    issue #90 起因：userinfo 根本不返回 user_type，企业号只靠 userType 判定会漏，
    结果团队闸门/共享额度都不生效。
    """
    if account is None:
        return False
    probe = getattr(account, "is_team", None)
    if callable(probe):
        try:
            return bool(probe())
        except Exception:
            pass
    ut = str(getattr(account, "user_type", "") or "").lower()
    if "team" in ut or "enterprise" in ut:
        return True
    if str(getattr(account, "organization_id", "") or "").strip() \
            or str(getattr(account, "organization_name", "") or "").strip():
        return True
    cred = getattr(account, "credits", None)
    if isinstance(cred, dict):
        shared = cred.get("shared")
        if isinstance(shared, dict) and (shared.get("size")
                                        or shared.get("unlimited")):
            return True
        for pkg in cred.get("packages") or []:
            if isinstance(pkg, dict) and pkg.get("name") == "共享额度":
                return True
    return False


def _traffic_guard_mode():
    """issue #64：把「团队账号并发闸门」扩展成**对所有账号**生效的流量守卫。

    出口/IP 我们控制不了，但「自己把某个号打到并发过高/节奏异常」是能控的：
    - on   ：所有账号都走闸门（推荐给 intl / 数据中心出口场景）
    - off  ：全部关闭
    - 未设置：保持旧行为（只对团队账号生效）
    """
    raw = str(os.environ.get("QD_TRAFFIC_GUARD") or "").strip().lower()
    if raw in ("1", "on", "true", "yes", "enable", "enabled"):
        return "on"
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return "off"
    return ""


def _team_guard_on(account):
    forced = _traffic_guard_mode()
    if forced == "on":
        return True
    if forced == "off":
        return False
    mode = _team_guard_mode()
    if mode == "off":
        return False
    if mode == "on":
        return True
    return _is_team_account(account)


def _team_acquire(account, model=""):
    """拿一个团队账号租约（并发闸门 + 最小间隔）。拿不到返回 None（fail-open）。"""
    if not _team_guard_on(account):
        return None
    try:
        now = time.time()
        gap = _team_min_gap()
        with account._team_lock:
            # 过期租约（>10min）视为泄漏，惰性清理，避免异常路径永久占坑
            account.team_leases = {k: v for k, v in
                                   (getattr(account, "team_leases", {}) or {}).items()
                                   if now - v < 600}
            last = float(getattr(account, "team_last_start", 0) or 0)
        wait = max(0.0, last + gap - now)
        if wait > 0:
            time.sleep(min(wait, 5.0) + random.uniform(0, 0.12))
        deadline = time.time() + float(os.environ.get("QD_TEAM_WAIT_SECS") or 30)
        limit = _team_max_concurrency()
        while True:
            with account._team_lock:
                if len(getattr(account, "team_leases", {})) < limit:
                    lid = uuid.uuid4().hex
                    account.team_leases[lid] = time.time()
                    account.team_last_start = time.time()
                    return lid
            if time.time() >= deadline:
                if not _TEAM_GUARD_LOG["warned_wait"]:
                    _TEAM_GUARD_LOG["warned_wait"] = True
                    log("team guard: concurrency limit %d reached for %s - "
                        "proceeding without lease (issue #56)"
                        % (limit, account.uid[:8]), level="WARN", tag="chat")
                return None
            time.sleep(0.05)
    except Exception:
        return None


def _team_release(account, lease):
    if not lease or account is None:
        return
    try:
        with account._team_lock:
            (getattr(account, "team_leases", {}) or {}).pop(lease, None)
    except Exception:
        pass


def _team_note_client(account):
    """记录本账号见过的客户端 IP；过多说明可能被共用（issue #56）。"""
    if not _team_guard_on(account):
        return
    try:
        ip = getattr(_CLIENT_CTX, "ip", None)
        if not ip:
            return
        now = time.time()
        with account._team_lock:
            seen = {k: v for k, v in
                    (getattr(account, "team_client_ips", {}) or {}).items()
                    if now - v < 3600}
            seen[ip] = now
            account.team_client_ips = seen
            n = len(seen)
        limit = int(os.environ.get("QD_TEAM_MAX_CLIENTS") or 3)
        if n > limit:
            key = account.uid[:8]
            last = _TEAM_GUARD_LOG["ips"].get(key, 0)
            if now - last > 1800:
                _TEAM_GUARD_LOG["ips"][key] = now
                log("team guard: account %s used from %d distinct clients in "
                    "the last hour - if this is a shared key, the org audit "
                    "will see it (issue #56)" % (account.uid[:8], n),
                    level="WARN", tag="chat")
    except Exception:
        pass


class _TeamLeaseResp(object):
    """把响应包一层：关闭/退出时释放团队租约（issue #56）。"""

    def __init__(self, resp, account, lease):
        self._resp = resp
        self._account = account
        self._lease = lease

    def __getattr__(self, name):
        return getattr(self._resp, name)

    def __iter__(self):
        return iter(self._resp)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self._resp.close()
        finally:
            _team_release(self._account, self._lease)


# ---------------------------------------------------------------------------
# 本地 web 工具（web_search / web_fetch 由网关本地执行）
# ---------------------------------------------------------------------------
WEB_TOOLS_MAX_ROUNDS = int(os.environ.get("QD_WEB_TOOLS_ROUNDS") or 3)


def web_tools_enabled():
    """是否开启本地 web 工具：QD_WEB_TOOLS 优先，其次面板持久化开关。"""
    raw = str(os.environ.get("QD_WEB_TOOLS") or "").strip().lower()
    if raw in ("1", "on", "true", "yes", "enable", "enabled"):
        return True
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return False
    try:
        return qoder_settings.web_tools_enabled(ACCOUNTS_DIR)
    except Exception:
        return False


def builtin_web_tool_names(tools):
    """客户端声明的内置 web 工具名集合（web_search / web_fetch）。"""
    names = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = str(tool.get("type") or "")
        if kind in qoder_webtools.SEARCH_DECL_TYPES:
            names.add(qoder_webtools.WEB_SEARCH_NAME)
        elif kind in qoder_webtools.FETCH_DECL_TYPES:
            names.add(qoder_webtools.WEB_FETCH_NAME)
    return names


def rewrite_web_tools(tools, names):
    """把内置声明换成网关自己的同名 function 声明（其它工具原样保留）。"""
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = str(tool.get("type") or "")
        if kind in qoder_webtools.SEARCH_DECL_TYPES \
                or kind in qoder_webtools.FETCH_DECL_TYPES:
            continue
        out.append(tool)
    for decl in qoder_webtools.tool_declarations():
        if decl["function"]["name"] in names:
            out.append(decl)
    return out


def _chat_chunks(obj, model):
    """把聚合好的 chat 对象摊成 SSE 分片（web 工具路径用：内容是整段回放）。"""
    cid = (obj or {}).get("id") or "chatcmpl-web"
    created = int((obj or {}).get("created") or time.time())
    mdl = (obj or {}).get("model") or model or ""
    text = (((obj or {}).get("choices") or [{}])[0].get("message") or {}).get(
        "content") or ""
    base = {"id": cid, "object": "chat.completion.chunk",
            "created": created, "model": mdl}
    yield dict(base, choices=[{"index": 0, "delta": {"role": "assistant"},
                               "finish_reason": None}])
    if text:
        yield dict(base, choices=[{"index": 0, "delta": {"content": text},
                                   "finish_reason": None}])
    tail = dict(base, choices=[{"index": 0, "delta": {},
                                "finish_reason": "stop"}])
    if (obj or {}).get("usage"):
        tail["usage"] = (obj or {}).get("usage")
    yield tail


def _chat_chunks_iter(obj, model):
    return [("data: " + json.dumps(c, ensure_ascii=False) + "\n\n").encode()
            for c in _chat_chunks(obj, model)]


def open_upstream(payload, session_key=None, target_realm=None, usage_ctx=None,
                  only_account=None, on_queue=None):
    """构造 COSY 签名请求并打开上游 SSE。返回 (resp, account, encoded)。

    usage_ctx：可选**出参** dict。传入时回填本次请求的推理档位审计信息
    （effort_requested = 客户端原值；effort = 实际写进上游 parameters 的值，
    None 表示未下发）。不传 = 与改动前逐字节一致（旧调用点全部不传）。

    账号轮换规则：
      - 401/403：凭证被拒 -> 冷却该账号（单账号池时短冷却）换号
      - 429    ：模型粒度频控 -> 解析 reset 时间做模型冷却换号
      - 418/5xx/provider_error（瞬时上游故障）-> 同账号快速重试 2 次
                （1s/2s 退避），仍失败短冷却(15s/单账号3s)换号
      - 其他 4xx（客户端参数/请求形态问题）-> 快速失败，冷却换号，不重试；
        其中请求自身的确定性错误（413 出站超限/参数非法/内容审核，见
        _is_request_fault）换号结果不变：不冷却不换号，直接抛给客户端
      - 熔断/降权三族记账（note_failure/note_unknown_failure）延迟到同一
        请求换号成功后才落账：全池同样失败（断网/上游整体故障）不计入，
        避免网络恢复后全池仍被熔断锁死
    全部账号失败后抛 RateLimited 或最后一个错误。
    """
    if usage_ctx is None:
        usage_ctx = {}
    model = str(payload.get("model") or "")
    route = None
    allowed_uids = None
    if POOL is not None:
        entry = usage_ctx.get("_key_entry") or getattr(_CLIENT_CTX, "key_entry", None) or {}
        if only_account is None or entry:
            try:
                route = qoder_routing.resolve(ACCOUNTS_DIR, list(POOL.accounts), model,
                    key=entry, explicit_realm=target_realm)
            except ValueError as error:
                raise UpstreamStatus(503, "routing configuration error: " + str(error))
            allowed_uids = route["allowed_uids"]
            if allowed_uids is not None and not allowed_uids:
                raise UpstreamStatus(403, "API Key / routing account scope has no eligible account")
            if route["realm"]:
                target_realm = route["realm"]
            usage_ctx["route_rule"] = (route["matched"] or {}).get("id") or ""
            if only_account is not None and allowed_uids is not None and only_account.uid not in allowed_uids:
                raise UpstreamStatus(403, "account is outside API Key / routing scope")
    # issue #128：整段对话复用同一上游会话身份（拿不到就 None = 旧行为）
    _conv = conversation_identity(payload, session_key)
    if only_account is not None and not target_realm:
        # issue #86：钉死账号时区域跟随该账号（自动路由可能把它换掉）
        target_realm = getattr(only_account, "realm", "") or None
    realm = target_realm or detect_model_realm(model) or CURRENT_REALM
    owner = exclusive_realm(model)
    if owner and realm != owner:
        raise UpstreamStatus(400, "model is only available in realm '%s'; refusing cross-realm fallback" % owner)
    if not target_realm:
        # 步2：只在**用户没显式指定**时，按账号可用性兜底换区
        if POOL and allowed_uids is not None:
            other = "intl" if realm == "cn" else "cn"
            if (POOL.count_ready(realm, model=model, allowed_uids=allowed_uids) == 0
                    and POOL.count_ready(other, model=model, allowed_uids=allowed_uids) > 0
                    and not exclusive_realm(model)):
                realm = other
        else:
            realm = fallback_realm_by_accounts(realm, model)
        if realm != CURRENT_REALM:
            # #26：换区可见（覆盖所有自动路径：独占表 / 清单驱动 / 账号回退）
            log("request routed to realm %s (default=%s, model=%r)"
                % (realm, CURRENT_REALM, model), tag="realm")
            if usage_ctx is not None:
                # 确定性原因 → 面板直接显示，不必自己推断
                usage_ctx["realm_reason"] = auto_realm_reason(model, realm)
    model_key = resolve_model_key(model, realm=realm)

    # issue #86：钉死账号时用它构造请求体（身份类字段必须与被测号一致）
    representative = (only_account if only_account is not None
                      else (POOL.pick(realm=realm, **({"allowed_uids": allowed_uids}
                           if allowed_uids is not None else {})) if POOL else None))
    body_obj = build_qoder_body(payload, representative, model_key, realm=realm,
                                  conversation=_conv)
    if usage_ctx is not None:
        # 实际下发档位直接取自请求体（build_qoder_body 只在 norm 非 None 时写入），
        # 与真实出站内容同源，不做二次推算。
        usage_ctx["effort_requested"] = requested_effort(payload)
        usage_ctx["effort"] = (body_obj.get("parameters") or {}).get(
            "reasoning_effort")
        # #26：本次实际出口（自动路由后可能与请求开始时不同）—— 调用方据此
        # 在响应头/日志里对用户可见，也让 usage 行与真实出账区一致。
        usage_ctx["realm"] = realm
    # Size only: custom encoding preserves padded Base64 length. Do not allocate
    # the encoded multi-MB body here: account/session fields are still provisional.
    _plain = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    _size = 4 * ((len(_plain) + 2) // 3)
    del _plain
    # issue #59：超限就地报 413，不发网络请求、不轮询账号（Broken pipe 会打爆账号池）
    if _size > MAX_UPSTREAM_BODY_BYTES:
        log("outbound payload too large (%d bytes > %d) - refusing to send "
            "(issue #59)" % (_size, MAX_UPSTREAM_BODY_BYTES),
            level="WARN", tag="chat")
        raise PayloadTooLarge(_size, MAX_UPSTREAM_BODY_BYTES)

    client_session_key = session_key
    route_policy_active = bool(route and (route.get("configured") or route.get("tiers"))) and only_account is None
    route_policy = route.get("policy", {}) if route_policy_active else {}
    if not session_key:
        session_key = derive_affinity_key(body_obj.get("messages"))
        if session_key and AFFINITY_DEBUG:
            log("affinity: derived %s for %d msgs"
                % (session_key, len(body_obj.get("messages") or [])))

    # issue #86：钉死账号按单账号池处理（冷却压到 3s、不跨号轮换）
    total = 1 if only_account is not None else _realm_ready_total(realm, model)
    if only_account is None and POOL and allowed_uids is not None:
        total = POOL.count_ready(realm, model=model, allowed_uids=allowed_uids)
    tried = set()
    route_round = 0
    route_round_retries = route_policy.get("request_retry", 0)
    route_account_cap = route_policy.get("max_retry_accounts", 0)
    route_affinity_key = session_key if route_policy.get("session_affinity", True) else None
    route_round_size = min(total, route_account_cap) if route_account_cap else total
    route_iteration_budget = (max(1, route_round_size)+2)*(1+route_round_retries) if route_policy_active else total+2
    last_error = None
    last_429 = None
    last_429_detail = ""
    last_queue_wait = 0        # 队列 403（10605）解析出的 retryAfterSeconds
    # 排队预算按**请求**计（不是按账号）：否则 N 个账号各等满 6 轮，
    # 单请求会被拖到 3N 分钟（现场就出现过 197s）。
    queue_tries = 0
    queue_start = time.time()
    waited_cool = False
    # 熔断/降权三族（note_failure/note_unknown_failure）先记进 blame，延迟到
    # 「同一请求换号成功」才落账：全部账号同样失败时（本机断网、上游整体
    # 故障、超大包被 RST），逐个记账会把全池误打进 30 分钟熔断，网络恢复也
    # 解不开。换号后成功了，才说明先前失败的账号确实有问题。
    blame = []                     # [(绑定方法, message)]

    # 额外 +2 次迭代预算：只供“短错误冷却等待续上”使用（正常轮换仍由
    # tried 集合自然终止）。
    for _ in range(route_iteration_budget):
        if only_account is not None:
            # issue #86：账号测试必须落在被点的那个号上，不能走会话粘性/轮询
            account = only_account if only_account.uid not in tried else None
        else:
            if route_account_cap and len(tried) >= route_account_cap:
                account = None
            elif route is not None and (route["matched"] or allowed_uids is not None or route_policy_active):
                account = qoder_routing.pick(POOL, route, realm, model, route_affinity_key, tried) if POOL else None
            else:
                account = POOL.pick_for_session(realm=realm, session_key=session_key,
                                                exclude=tried, model=model) if POOL else None
        if account is None:
            if route_policy_active and route_round < route_round_retries and tried:
                route_round += 1
                tried.clear()
                waited_cool = False
                usage_ctx["routing_round"] = route_round
                continue
            if not waited_cool and (not route_account_cap or len(tried) < route_account_cap):
                wait = _short_error_cooldown_wait(realm, model,
                    exclude=tried, **({"allowed_uids": allowed_uids} if allowed_uids is not None else {}))
                if wait > 0:
                    waited_cool = True
                    log("accounts in short error-cooldown for '%s' "
                        "(%.1fs left) - waiting instead of failing"
                        % (model, wait), level="WARN", tag="chat")
                    time.sleep(wait + 0.25)
                    continue     # 冷却到期后重新 pick，服务该请求
            break
        if account.realm != realm and only_account is None:
            if session_key and POOL:
                POOL.affinity.unbind(session_key)
            continue
        tried.add(account.uid)
        # 身份相关的 aliyun_user_type 需要真实账号
        body_obj["aliyun_user_type"] = account.user_type or \
            qoder_sign.DEFAULT_USER_TYPE
        # Resolve only after choosing the actual account, before signing.
        assign_prefix_session(body_obj, account, model_key,
                              client_session_key=client_session_key,
                              usage_ctx=usage_ctx,
                              # issue #136：session_id 用**窗口级** id（整段对话恒定），
                              # 不能用「一次提问」那个 id —— 否则每轮新提问缓存都归零。
                              fallback_session_id=((_conv or {}).get("window_id")
                                                   or (_conv or {}).get("id")))
        encoded = qoder_encode(
            json.dumps(body_obj, ensure_ascii=False).encode("utf-8"))
        # Session/user_type may grow after preliminary preflight. Guard the actual
        # signed wire body as well, before acquiring leases or sending any bytes.
        if len(encoded) > MAX_UPSTREAM_BODY_BYTES:
            raise PayloadTooLarge(len(encoded), MAX_UPSTREAM_BODY_BYTES)
        # 官方候选推理主机（国际版 api1/api2/api3）：传输层失败时切换域名，
        # 签名只覆盖 path，换主机不影响签名有效性。
        hosts = gateway_candidates(account.realm, qoder_accounts.account_realm_config(account))
        host_i = 0
        raw_url = hosts[host_i] + CHAT_PATH

        # issue #56：团队账号的并发闸门 + 最小间隔（非团队账号直接返回 None）
        _team_lease = _team_acquire(account, model)
        _team_note_client(account)

        # ---- 发起请求（瞬时上游故障同账号快速重试） ----
        resp = None
        last_exc = None
        detail = ""
        tries = 0                 # 瞬时故障重试计数（1s/2s 退避）
        while True:
            try:
                sess = SESSIONS.get(account)
                headers = sess.headers(encoded, raw_url, model_key=model_key,
                                       sse=True)
                chat_url = validate_public_http_url(raw_url)
                req = urllib.request.Request(chat_url,
                                             data=encoded.encode("utf-8"),
                                             method="POST", headers=headers)
                if qoder_accounts.is_configured_vpc_host(
                        urllib.parse.urlsplit(chat_url).hostname):
                    resp = qoder_accounts._opener_for_request().open(req, timeout=600)
                else:
                    resp = urllib.request.urlopen(req, timeout=600)
                break
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                # 错误体只能读一次：把已读详情挂到异常上，处理器二次 read
                # 会拿到残缺/空内容，统一从 qoder_detail 取。
                try:
                    exc.qoder_detail = detail
                except Exception:
                    pass
                last_exc = exc
                # issue #38：上游排队 -> 等放行后重试同一账号（不把排队 JSON
                # 交给客户端）。仅在预算与单轮上限内等待；超出即按原错误走
                # 既有分类（fail-open 语义不变）。
                note_queue_observation(_queue_scope(account, model_key, raw_url), detail)
                if usage_ctx.get("_queue_probe_once"):
                    usage_ctx["_queue_probe_account"] = account
                qwait = _queue_wait_seconds(detail)
                if usage_ctx.get("_queue_probe_once"):
                    _team_release(account, _team_lease)
                    _team_lease = None
                    if qwait > 0:
                        raise UpstreamQueued(qwait, detail)
                    raise UpstreamStatus(exc.code, detail)
                _can_wait = (qwait > 0 and queue_tries < QUEUE_MAX_ROUNDS
                             and qwait <= QUEUE_WAIT_MAX_SECS
                             and (time.time() - queue_start) + qwait
                             <= QUEUE_WAIT_BUDGET_SECS)
                # issue #87：visible 模式由调用方（流式 handler）提供 on_queue，
                # 它在**流内**发心跳+排队提示；fast 模式直接抛给 handler 返回 503。
                if _can_wait and on_queue is not None:
                    queue_tries += 1
                    log("upstream queued on '%s' model '%s' - waiting %ds "
                        "(round %d/%d, visible)"
                        % (account.uid[:8], model, qwait, queue_tries,
                           QUEUE_MAX_ROUNDS), level="WARN", tag="chat")
                    try:
                        on_queue(qwait, detail, queue_tries, QUEUE_MAX_ROUNDS)
                    except Exception:
                        pass
                    time.sleep(qwait + 1 + random.uniform(0, QUEUE_WAIT_JITTER_SECS))
                    continue
                if qwait > 0 and queue_mode() != "wait":
                    raise UpstreamQueued(qwait, detail,
                                         int(time.time() - queue_start))
                if _can_wait:
                    queue_tries += 1
                    log("upstream queued on '%s' model '%s' - waiting %ds "
                        "(round %d/%d)"
                        % (account.uid[:8], model, qwait, queue_tries,
                           QUEUE_MAX_ROUNDS), level="WARN", tag="chat")
                    # 抖动 0~2s：多客户端同时被唤醒会一起打同一队列
                    time.sleep(qwait + 1 + random.uniform(0, QUEUE_WAIT_JITTER_SECS))
                    continue
                # 429/401/403 与客户端参数错：立即进入分类，不重试
                if exc.code in (429, 401, 403):
                    break
                # 图片转换超时与首字超时不同：保留输入，只允许同账号重试一次。
                # 重试耗尽后在外层直接上抛，不把整个账号池都拖入转换/冷却。
                if _is_image_conversion_timeout(detail):
                    if _take_image_retry(usage_ctx):
                        tries += 1
                        log("image conversion timeout: one bounded retry, images retained",
                            level="WARN", tag="image")
                        time.sleep(1)
                        continue
                    break
                # 首字超时：同账号重试几乎必然再超时（会拖到客户端 idle timeout），
                # 直接 break 让外层换账号——换 worker 才有意义。
                if _is_first_token_timeout(detail):
                    log("upstream first-token timeout on '%s' model '%s' - "
                        "rotating to another account instead of retrying"
                        % (account.uid[:8], model), level="WARN", tag="chat")
                    note_risk("timeout", detail)
                    break
                if _is_transient_upstream(exc.code, detail) \
                        and tries < TRANSIENT_MAX_RETRIES:
                    backoff = tries + 1      # 1s, 2s（tries 从 0 计）
                    log("transient upstream HTTP %d on '%s' model '%s' "
                        "(try %d/%d), retry in %ds"
                        % (exc.code, account.uid[:8], model, tries + 1,
                           TRANSIENT_MAX_RETRIES + 1, backoff),
                        level="WARN", tag="chat")
                    tries += 1
                    time.sleep(backoff)
                    continue
                break
            except Exception as exc:
                if usage_ctx.get("_queue_probe_once"):
                    _team_release(account, _team_lease)
                    _team_lease = None
                    raise
                last_exc = exc
                detail = ""
                # 传输层瞬时故障（TLS EOF / 连接重置 / 超时）同样原地重试；
                # 若还有官方备用推理域名，优先换域名（对整域故障更有效）。
                if _is_transient_transport(exc) \
                        and tries < TRANSIENT_MAX_RETRIES:
                    if host_i + 1 < len(hosts):
                        host_i += 1
                        raw_url = hosts[host_i] + CHAT_PATH
                        log("transient transport error on '%s' model '%s' "
                            "(try %d/%d): %s - switching gateway host to %s"
                            % (account.uid[:8], model, tries + 1,
                               TRANSIENT_MAX_RETRIES + 1, str(exc)[:120],
                               hosts[host_i]),
                            level="WARN", tag="chat")
                    else:
                        log("transient transport error on '%s' model '%s' "
                            "(try %d/%d): %s - retry in %ds"
                            % (account.uid[:8], model, tries + 1,
                               TRANSIENT_MAX_RETRIES + 1,
                               str(exc)[:120], tries + 1),
                            level="WARN", tag="chat")
                    tries += 1
                    time.sleep(tries)
                    continue
                break

        if resp is not None:
            # 同一请求换号后成功了：先前失败的账号确实有问题，此刻才落三族账
            # （喂熔断/降权）。随后统一复位本账号；note_success 是
            # clear_error(model) 的超集，刻意不动 balance_until。
            for note, msg in blame:
                note(msg)
            account.note_success(model=model)
            if qoder_keepalive.enabled() and only_account is None:
                # 缓存保温：登记这条刚成功的真实对话（默认关闭，见
                # qoder_keepalive 模块说明）。配套 issue：上游隐式前缀缓存
                # 实测 ~5 分钟闲置过期，闲置后首发全量重写（0% 命中全价）。
                # 只登记真实客户端流量：only_account 路径是账号测试/诊断探测。
                try:
                    qoder_keepalive.observe(
                        payload, session_id=body_obj.get("session_id"),
                        account_uid=account.uid, realm=realm,
                        model=str(payload.get("model") or ""))
                except Exception:
                    pass
            try:
                resp._qd_queue_scope = _queue_scope(account, model_key, raw_url)
            except (AttributeError, TypeError):
                pass
            if _team_lease:
                # 关闭/退出时自动释放租约（handler 都走 with resp / resp.close）
                return _TeamLeaseResp(resp, account, _team_lease), account, encoded
            return resp, account, encoded

        # 本次尝试失败：先还租约，再做错误分类（分类里可能 continue/raise）
        _team_release(account, _team_lease)
        _team_lease = None

        exc = last_exc
        # 重试多轮时同一个异常的 body 可能已被读空：detail 为空就走挂在异常上的
        # qoder_detail，否则排队判定会失灵、退回「凭证被拒」（现场 197s 后 key 失效）。
        if not detail:
            detail = getattr(exc, "qoder_detail", "") or ""
        # ---- 错误分类（与原有轮换语义一致） ----
        if isinstance(exc, urllib.error.HTTPError):
            if _is_image_conversion_timeout(detail):
                log("image conversion timeout exhausted: no account rotation/cooldown",
                    level="WARN", tag="image")
                raise exc
            if exc.code == 429:
                note_risk("rate", detail)
                account.note_error("HTTP 429 (model throttled)", model=model,
                                   cooldown=60)
                # task-74 三族之一：账号级软限流退避（连续才涨；单号池不接，
                # 免得一次排队就把唯一账号冷 10 分钟——那是变相打死）。
                if total > 1:
                    account.note_soft_rate("HTTP 429 upstream")
                log("account %s throttled on '%s' (429), retry in 60s"
                    % (account.uid[:8], model))
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code in (401, 403):
                _is_queue_http = bool(_queue_wait_seconds(detail))
                note_risk("queue" if _is_queue_http else "rejected", detail)
                if _is_queue_http:
                    # issue #69 现场：上游排队超过预算后仍返回 **HTTP 403 + 10605**。
                    # 这是模型级排队，**不是**账号问题——绝不能按「凭证被拒」处理：
                    # 冷却 60s 会把唯一账号打成「no usable account」，面板看起来
                    # 就像「Key 失效」。这里只记录、换号（如果有别的号），不惩罚。
                    last_429 = exc
                    last_429_detail = detail
                    last_queue_wait = _queue_wait_seconds(detail)
                    log("upstream still queued (HTTP %s / 10605) on '%s' model "
                        "'%s' after %ds - not penalising the account"
                        % (exc.code, account.uid[:8], model,
                           int(time.time() - queue_start)),
                        level="WARN", tag="chat")
                    last_error = exc
                    continue
                # issue #108：额度类 403 长冷却换号，不吃 single_account 的 3s clamp
                if _note_quota_cooldown(account, exc.code, detail, "HTTP", model=model):
                    if session_key and POOL:
                        POOL.affinity.unbind(session_key)
                    last_error = exc
                    continue
                log("account %s rejected (HTTP %s), rotating"
                    % (account.uid[:8], exc.code))
                # issue #64：排队(10605)不解绑会话亲和——排队是模型级的，换号
                # 不会变快，反而造成「同会话在多账号间跳」的异常画像。
                if session_key and POOL and not _is_queue_http:
                    POOL.affinity.unbind(session_key)
                dead = qoder_accounts.session_dead(detail)
                account.note_error("HTTP %s %s" % (exc.code, detail[:80]),
                                   cooldown=300 if dead else 60,
                                   single_account=(total <= 1))
                if dead:
                    account.enabled = False
                    account.save(ACCOUNTS_DIR) if account.path else None
                    log("account %s session dead (TOKEN_EXPIRE) - disabled"
                        % account.uid[:8], level="ERROR")
                last_error = exc
                continue
            if _is_transient_upstream(exc.code, detail):
                # 重试后仍是瞬时故障：上游侧问题，短冷却换号（不重罚账号）
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                account.note_error(
                    "HTTP %s upstream transient: %s" % (exc.code, detail[:80]),
                    cooldown=15, single_account=(total <= 1))
                # task-74 三族之二：延迟记账——同一请求换号成功后才喂熔断
                # 计数（3 次才起跳；单号池不接）
                if total > 1:
                    blame.append((account.note_failure,
                                  "HTTP %s upstream transient" % exc.code))
                log("upstream still transient (HTTP %d) after %d tries on "
                    "'%s' - short cooldown, rotating"
                    % (exc.code, TRANSIENT_MAX_RETRIES + 1, account.uid[:8]),
                    level="WARN", tag="chat")
                last_error = exc
                continue
            # 其他 4xx（客户端参数/请求形态问题）：快速失败，冷却换号
            if 400 <= exc.code < 500:
                # issue #59 跟进：请求自身的确定性错误（413 出站超限/参数非法/
                # 内容审核）换号结果不变——轮换只会把全池冷 60s，其他用户跟着
                # 吃 503 "no usable account"。直接把原错误抛给客户端。
                if _is_request_fault(exc.code, detail):
                    log("request-fault HTTP %s on '%s' model '%s' - request "
                        "itself is at fault: no rotation, no cooldown"
                        % (exc.code, account.uid[:8], model),
                        level="WARN", tag="chat")
                    raise exc
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                account.note_error("HTTP %s: %s" % (exc.code, detail[:80]),
                                   cooldown=60, single_account=(total <= 1))
                last_error = exc
                continue
            raise
        else:
            if session_key and POOL:
                POOL.affinity.unbind(session_key)
            if exc is not None:
                if _is_transient_transport(exc):
                    # 传输抖动重试耗尽：短冷却换号（不重罚账号）
                    account.note_error("transport transient: %s" % str(exc)[:100],
                                       cooldown=15, single_account=(total <= 1))
                    # task-74 三族之二（同族）：延迟记账——换号成功后才喂熔断计数
                    if total > 1:
                        blame.append((account.note_failure,
                                      "transport transient: %s"
                                      % type(exc).__name__))
                    log("upstream transport still failing after %d tries on "
                        "'%s' - short cooldown"
                        % (TRANSIENT_MAX_RETRIES + 1, account.uid[:8]),
                        level="WARN", tag="chat")
                else:
                    account.note_error(str(exc)[:120], cooldown=60,
                                       single_account=(total <= 1))
                    # task-74 三族之三：延迟记账——换号成功后才进降权窗口
                    if total > 1:
                        blame.append((account.note_unknown_failure,
                                      str(exc)[:120]))
                last_error = exc
            continue

    # 循环耗尽仍未成功：丢弃 blame，不落三族账——所有试过的账号都同样失败，
    # 更像请求级或网络级故障，不是某个账号的问题。
    if blame:
        log("all tried accounts failed on '%s' - request/network level fault, "
            "not counting toward breaker/downgrade" % model,
            level="WARN", tag="chat")
    if last_error is not None:
        if last_429 is not None:
            # 排队（10605/403）用上游给的 retryAfterSeconds，其余走模型冷却口径
            raise RateLimited(last_429, last_429_detail,
                              wait=last_queue_wait
                              or retry_after_seconds(model, realm))
        raise last_error
    throttled, wait = realm_model_throttled(realm, model)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    raise RuntimeError(
        "no usable account for realm '%s': all are disabled, cooling down, or expired"
        % realm)


def sse_with_heartbeat(source, send, interval=None, idle_limit=None):
    """在等待上游数据的空档里发送 SSE 注释心跳（`: ping`），并原样转发数据。

    背景：`xhigh` 长上下文请求的上游首字延迟可达 40–71 秒（实测日志），
    这段时间网关对客户端一言不发；客户端/中间代理一旦空闲超时就会断开并
    重连（表现为“一直重连”）。SSE 规范允许以 `:` 开头的注释行，客户端会
    忽略，因此用它做保活。

    source     : 上游数据迭代器（iter_inner_sse 的输出，产出 bytes）
    send       : 发送函数（接收 bytes）
    interval   : 空闲多久发一次心跳（秒），默认 5（可用 QD_SSE_HEARTBEAT 覆盖）
    idle_limit : 单次等待上限（秒），默认 900；超过抛 TimeoutError

    读线程把上游数据放入队列，主线程只在“队列空闲”时补心跳，保证顺序与
    错误传播（上游异常原样在主线程抛出）。
    """
    if interval is None:
        try:
            interval = float(os.environ.get("QD_SSE_HEARTBEAT", "5") or 5)
        except Exception:
            interval = 5.0
    if interval <= 0:
        # 显式关闭心跳：退化为直通
        for item in source:
            yield item
        return
    if idle_limit is None:
        idle_limit = 900.0

    import queue as _queue
    import threading as _threading

    box = _queue.Queue()
    done = object()

    def _reader():
        try:
            for item in source:
                box.put(item)
        except BaseException as exc:          # 上游错误/断连也要送主线程
            box.put(exc)
        finally:
            box.put(done)

    reader = _threading.Thread(target=_reader, daemon=True)
    reader.start()

    waited = 0.0
    while True:
        try:
            item = box.get(timeout=interval)
        except _queue.Empty:
            send(b": ping\n\n")               # SSE 注释：客户端忽略
            waited += interval
            if waited >= idle_limit:
                raise TimeoutError(
                    "upstream produced no data for %.0fs" % waited)
            continue
        if item is done:
            return
        if isinstance(item, BaseException):
            raise item
        waited = 0.0
        yield item


# ---------------------------------------------------------------------------
# 上游内层错误可观测性（task-34）
# ---------------------------------------------------------------------------
# 形态：HTTP 200 + 信封 statusCodeValue=200，但**内层 chunk** 里带 error
# （如 provider_error → invalid_request_error："Messages with role 'tool'
# must be a response to a preceding message with 'tool_calls'"）。
# 既有路径对此完全无感：客户端只看到 200 的空正文，排障成本极高。
# 本段只做计数 + 按类别首次告警，**绝不改行为**（不吞、不改映射、不改重试）。
_INNER_ERR_LOCK = threading.Lock()
_INNER_ERR_STATS = {"total": 0, "kinds": {}, "warned": set()}


def inner_error_snapshot():
    """内层错误计数快照（挂到 /usage/perf 等运行信息输出）。"""
    with _INNER_ERR_LOCK:
        return {"total": _INNER_ERR_STATS["total"],
                "kinds": dict(_INNER_ERR_STATS["kinds"]),
                "warned": sorted(_INNER_ERR_STATS["warned"])}


def _inner_error_kind(code, message):
    """把内层错误归类（用于首次告警去重）。返回 "" = 不是错误形态。"""
    low = ("%s %s" % (code or "", message or "")).lower().strip()
    if not low:
        return ""
    if any(k in low for k in ("datainspectionfailed", "inappropriate content",
                              "contentfilter", "sensitivecontent",
                              "data_inspection")):
        return "content_policy"
    if any(k in low for k in ("rate_limit", "throttl", "frequency limit",
                              "10605", "too many")):
        return "rate_limit"
    if any(k in low for k in ("invalid_request", "invalid_parameter",
                              "provider_error", "not found",
                              "must be a response", "unsupported")):
        return "invalid_request"
    if any(k in low for k in ("unauthor", "forbidden", "permission",
                              "expired", "token")):
        return "auth"
    return "other"


def note_inner_upstream_error(obj, status=None):
    """检测内层 chunk 的错误指示：计数 + 按类别首次 WARN。返回命中类别。

    只观测、不改行为：调用点不做任何分支，yield 与清洗流程原样继续。
    """
    if not isinstance(obj, dict):
        return ""
    err = obj.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or ""
        msg = err.get("message") or ""
    elif isinstance(err, str):
        code, msg = "", err
    else:
        return ""
    kind = _inner_error_kind(code, msg)
    if not kind:
        return ""
    with _INNER_ERR_LOCK:
        _INNER_ERR_STATS["total"] += 1
        kinds = _INNER_ERR_STATS["kinds"]
        kinds[kind] = kinds.get(kind, 0) + 1
        total = _INNER_ERR_STATS["total"]
        first = kind not in _INNER_ERR_STATS["warned"]
        if first:
            _INNER_ERR_STATS["warned"].add(kind)
    if first:
        log("upstream error hidden in HTTP 200 envelope: kind=%s status=%s "
            "code=%s total=%d | %s"
            % (kind, status, str(code)[:40], total,
               ("%s %s" % (code, msg)).strip()[:300]),
            level="WARN", tag="chat")
    return kind


def iter_inner_sse(resp, holder=None):
    """把上游 SSE 信封流解包成标准 OpenAI chunk 的 "data: ..." 行。

    上游帧形态：
        data:{"headers":{...},"body":"<内层 OpenAI chunk JSON 字符串>","statusCodeValue":200}
        data:{"body":"[DONE]"}
        event:finish{...}          <- 计时元数据，忽略

    规则：
      - statusCodeValue != 200 -> 抛 UpstreamStatus（body 为错误详情）
      - body == "[DONE]"       -> 结束迭代
      - 内层 chunk 里的 usage 记入 holder
    """
    scope = getattr(resp, "_qd_queue_scope", None)
    for raw in resp:
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8")
            except Exception:
                continue
        else:
            line = raw
        data = strip_data_prefix(line)
        if not data:
            continue
        try:
            outer = json.loads(data)
        except Exception:
            continue
        if not isinstance(outer, dict):
            continue
        status = outer.get("statusCodeValue")
        body = outer.get("body")
        if status not in (None, 200, "200"):
            note_queue_observation(scope, body)
            limit = 8192 if holder and holder.get("_queue_probe_once") else 400
            raise UpstreamStatus(status, body if isinstance(body, str)
                                 else json.dumps(outer, ensure_ascii=False)[:limit])
        if not isinstance(body, str):
            continue
        if body == "[DONE]":
            break
        try:
            inner = json.loads(body)
        except Exception:
            continue
        if isinstance(inner, dict):
            # task-34：只观测（计数 + 首次告警），不影响下面的清洗/透传分支。
            note_inner_upstream_error(inner, status=status)
            note_queue_observation(scope, inner.get("error"))
            if holder is not None and holder.get("_queue_probe_once") and inner.get("error"):
                raise UpstreamStatus(502, json.dumps(inner["error"], ensure_ascii=False)[:8192])
            choices = inner.get("choices")
            if not inner.get("error") and isinstance(choices, list) and any(
                    isinstance(ch, dict) and ch.get("finish_reason") in ("stop", "tool_calls", "function_call", "length")
                    for ch in choices):
                note_queue_observation(scope, success=True)
                if holder is not None:
                    holder["_upstream_finished"] = True
            if holder is not None and inner.get("usage") \
                    and not holder.get("usage"):
                holder["usage"] = inner["usage"]
        cleaned = clean_chunk(body)
        if not cleaned:
            continue
        yield ("data: " + cleaned + "\n\n").encode("utf-8")


def aggregate_stream(resp, model, resp_id=None, holder=None, allowed_names=None):
    """把上游信封流折叠成一个非流式 chat.completion 对象。

    allowed_names：本次请求声明的工具名集合；用于「泄漏文本回读」的守卫
    （见 parse_leaked_tool_calls / issue #8）。
    """
    content, reasoning, finish = [], [], "stop"
    tool_calls_map = {}
    usage = holder.get("usage") if holder else None
    started = time.time()
    first_chunk_at = None
    created = None

    for line in _sse_normalize_tool_call_indices(iter_inner_sse(resp, holder=holder)):
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if not isinstance(chunk, dict):
            continue
        if first_chunk_at is None:
            first_chunk_at = time.time()
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        if chunk.get("created"):
            created = chunk["created"]
        if chunk.get("usage"):
            usage = chunk["usage"]
            if holder is not None:
                holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                _c = delta["content"]
                if isinstance(_c, str):
                    content.append(_c)
                elif isinstance(_c, list):
                    for _p in _c:
                        if isinstance(_p, str):
                            content.append(_p)
                        elif (isinstance(_p, dict)
                              and isinstance(_p.get("text"), str)):
                            content.append(_p["text"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls_map)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": call_id or _new_id("call_"),
                        "type": tc.get("type") or "function",
                        "function": {"name": fn_name, "arguments": fn_args},
                    }
                else:
                    entry = tool_calls_map[idx]
                    if call_id:
                        entry["id"] = call_id
                    if fn_name:
                        entry["function"]["name"] = \
                            (entry["function"]["name"] or "") + fn_name
                    if fn_args:
                        entry["function"]["arguments"] = \
                            (entry["function"]["arguments"] or "") + fn_args
            fc = delta.get("function_call")
            if fc and isinstance(fc, dict) and fc.get("name"):
                idx = 0
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": _new_id("call_"),
                        "type": "function",
                        "function": {"name": fc.get("name") or "",
                                     "arguments": fc.get("arguments") or ""},
                    }
                else:
                    entry = tool_calls_map[idx]
                    if fc.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fc["name"]
                    if fc.get("arguments"):
                        entry["function"]["arguments"] += fc["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    # 回读（issue #8）：上游未给结构化 tool_calls，但正文恰好是网关自己写入
    # 历史的「LEAK_MARKER + JSON 数组」形态（模型照格式复述）——还原为结构化
    # 调用并从正文移除，避免客户端把 JSON 当正文显示、本轮调用不执行。
    if not tool_calls_map and message.get("content"):
        # issue #16-B1（非流式侧）：正文可能是「散文 + 回声块」。先按标记切分，
        # 散文照常保留，只对标记起的那一段做回读/吞掉判定。
        _head16, _block16 = _find_marker_split(message["content"],
                                               allow_prefix=False)
        _echo_src = _block16 or ""
        if _echo_src:
            _rec, _clean = parse_leaked_tool_calls(_echo_src, allowed_names)
            if not _rec and TOOL_CALL_OPEN in _echo_src.lower():
                # issue #137：marker 后面跟的是伪 XML 调用，不是 JSON 数组
                _rec, _clean = parse_tool_call_tags(_echo_src, allowed_names)
                if _rec:
                    RECOVERY_STATS["tool_tag"] = \
                        RECOVERY_STATS.get("tool_tag", 0) + 1
            if _rec:
                message["content"] = _head16 + (_clean or "")
                tool_calls_map = {i: c for i, c in enumerate(_rec)}
            elif _echo_with_bad_args(_echo_src, allowed_names):
                # issue #37（fail-closed，**先于 #9/#11**）：外层数组完整合法、
                # 只是内层参数坏了——不泄漏、不伪造调用，也不拼装成功对象：
                # 整轮以协议错误失败（HTTP 502）。真截断的数组 parse 不过，
                # 不会被这里抢走（仍由 #9 吞掉）。
                log("tool-call echo has invalid JSON arguments (%d chars) — "
                    "failing the turn closed (issue #37)" % len(_echo_src),
                    level="WARN", tag="chat")
                raise InvalidToolArguments()
            elif _leaked_partial_droppable(_echo_src, allowed_names):
                # issue #72：声明了 tools 却只拿到残缺回声 -> fail-closed（502），
                # 不再把「空调用 + 正常内容」当成成功返回。
                _kind72 = ("marker_only" if _marker_only_echo(_echo_src)
                           else "partial_array")
                log("dropped truncated leaked tool-call echo (%d chars, %s, "
                    "tools_declared=%d) — failing the turn closed (issue #72)"
                    % (len(_echo_src), _kind72, len(allowed_names)),
                    level="WARN", tag="chat")
                raise EmptyToolEcho(len(_echo_src), _kind72)
            elif _tool_echo_droppable(_echo_src, allowed_names):
                # issue #11：工具结果回声块同样不还原、不透传。
                log("dropped leaked tool-result echo (%d chars) — issue #11"
                    % len(_echo_src), level="WARN", tag="chat")
                message["content"] = _head16
            else:
                # issue #49 方向 C：四条路径都没认出来 -> 至少留痕（非流式侧）
                _log_unrecovered_leak(_echo_src, "aggregate")
                # issue #120 后续：非流式侧同样不许把协议残片留在 content 里。
                # 非流式没有"重试"概念，但抛 EmptyToolEcho 会走既有 502 出口，
                # 客户端重发这一轮 —— 比返回一段被污染的回答强。
                if _leak_garbage_only(_echo_src):
                    log("dropped bare tool-protocol residue (%d chars) — "
                        "issue #49 form3 (aggregate)" % len(_echo_src),
                        level="WARN", tag="chat")
                    message["content"] = _head16
                elif (allowed_names
                        and (_leak_residue_present(_outside_fences(_echo_src))
                             or _dsml_residue_present(_outside_fences(_echo_src)))
                        and not _echo_names_all_undeclared(_echo_src, allowed_names)):
                    log("unrecovered leak residue in tools session (%d chars) — "
                        "failing the turn closed (aggregate)" % len(_echo_src),
                        level="WARN", tag="chat")
                    raise EmptyToolEcho(len(_echo_src), "unrecovered_aggregate")
    # issue #51：DeepSeek DSML 回退（非流式 chat 侧此前**完全没有**这一层，
    # 只有 Responses 有）——模型直接吐原生 DSML 时整段协议会当正文返回。
    if not tool_calls_map and "DSML" in (message.get("content") or ""):
        _dsml_calls, _dsml_clean = parse_dsml_tool_calls(message["content"])
        if _dsml_calls:
            tool_calls_map = {
                i: {"id": c.get("id") or _new_id("call_"),
                    "type": "function",
                    "function": {"name": c.get("name") or "",
                                 "arguments": c.get("arguments") or "{}"}}
                for i, c in enumerate(_dsml_calls)}
            message["content"] = _dsml_clean or ""
            if finish in ("stop", None):
                finish = "tool_calls"
        else:
            _log_unrecovered_leak(message["content"], "aggregate-dsml")
            # issue #120 后续：非流式 DSML 回退也解析不出来时，不许把整段
            # DSML 协议留在 content 里当正文返回（这正是 17:38 那条 4828 字符
            # 泄漏在非流式侧的等价路径）。空壳残片丢弃，其余走 502 出口。
            _bad_dsml = message["content"] or ""
            if _leak_garbage_only(_bad_dsml):
                log("dropped bare tool-protocol residue (%d chars) — "
                    "issue #49 form3 (aggregate-dsml)" % len(_bad_dsml),
                    level="WARN", tag="chat")
                message["content"] = ""
            elif (allowed_names
                    and (_leak_residue_present(_outside_fences(_bad_dsml))
                         or _dsml_residue_present(_outside_fences(_bad_dsml)))
                    and not _echo_names_all_undeclared(_bad_dsml, allowed_names)):
                log("unrecovered DSML residue in tools session (%d chars) — "
                    "failing the turn closed (aggregate-dsml)" % len(_bad_dsml),
                    level="WARN", tag="chat")
                raise EmptyToolEcho(len(_bad_dsml), "unrecovered_aggregate_dsml")
    # issue #137：伪 XML 工具调用回退（`<tool_call>Name {json}</tool_call>`）。
    # 非流式侧与 DSML 同位置：模型没吐结构化 tool_calls，正文里只有这种标签。
    if not tool_calls_map and TOOL_CALL_OPEN in (message.get("content")
                                                or "").lower():
        _tag_calls, _tag_clean = parse_tool_call_tags(
            message.get("content") or "", allowed_names)
        if _tag_calls:
            tool_calls_map = {i: c for i, c in enumerate(_tag_calls)}
            message["content"] = _tag_clean or ""
            if finish in ("stop", None):
                finish = "tool_calls"
            RECOVERY_STATS["tool_tag"] = \
                RECOVERY_STATS.get("tool_tag", 0) + 1
        else:
            _log_unrecovered_leak(message.get("content") or "",
                                  "aggregate-tool-tag")
    if not tool_calls_map:
        _raise_unstructured_tool_echo(message.get("content") or "", allowed_names)
    # Never erase native calls into successful empty completion.
    if any(not (v.get("function") or {}).get("name") for v in tool_calls_map.values()):
        raise EmptyToolEcho(0, "missing_tool_name")
    for entry in tool_calls_map.values():
        args = (entry.get("function") or {}).get("arguments") or ""
        if args.strip():
            try:
                json.loads(args)
            except (ValueError, TypeError):
                raise EmptyToolEcho(len(args), "truncated_arguments")
    if tool_calls_map:
        ordered = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
        message["tool_calls"] = ordered
        if finish in ("stop", None):
            finish = "tool_calls"
    elif finish == "tool_calls":
        raise EmptyToolEcho(0, "missing_tool_call")
    if (allowed_names and not tool_calls_map and not message.get("content", "").strip()
            and finish not in ("length", "content_filter")):
        raise EmptyToolEcho(0, "reasoning_only" if reasoning else "empty_tool_turn")
    out = {
        "id": resp_id or "chatcmpl-qoder",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        out["usage"] = normalize_usage_for_client(usage)   # issue #112
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["first_chunk_at"] = first_chunk_at
    return out


# ---------------------------------------------------------------------------
# Responses API (/v1/responses) <-> Chat Completions translation
# ---------------------------------------------------------------------------
def local_ip_addresses():
    """Every non-loopback IPv4 address this machine answers on."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
            probe.close()
        except Exception:
            pass
    return found


def _new_id(prefix):
    return prefix + uuid.uuid4().hex


def _flatten_content(content):
    """Flatten Responses-style content into text, or OpenAI vision parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # 单 part 形态当单元素列表（同 _flatten_content_text 的 dict 修复）
        content = [content]
    elif not isinstance(content, list):
        return str(content)
    texts, parts = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            parts.append({"type": "text", "text": piece})
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            t = piece.get("text") or ""
            texts.append(t)
            parts.append({"type": "text", "text": t})
        elif ptype in ("input_image", "image_url", "image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = "data:%s;base64,%s" % (mime, piece["data"])
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if any(p.get("type") == "image_url" for p in parts):
        return parts          # multimodal: keep structured parts
    return "\n".join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Responses API "custom" (freeform) tools
#
# Codex 等客户端把文件编辑工具声明为 custom freeform tool：
#     {"type": "custom", "name": "apply_patch", "format": {...grammar...}}
# 上游 chat 端点没有 custom 概念 -> 出站降级为单 "input" 字符串参数的
# function tool，入站再还原为 custom_tool_call。否则工具被静默忽略。
# ---------------------------------------------------------------------------
CUSTOM_TOOL_HINT = (
    "This is a freeform tool. Put the COMPLETE raw payload into the single "
    "'input' string parameter, verbatim. Do not wrap it in JSON, do not wrap "
    "it in markdown code fences, do not add commentary."
)


_UNSUPPORTED_TOOL_LOG = {}


def _log_unsupported_tool(kind, where="tools"):
    """未知工具类型被丢弃时记一次 WARN（同类每 10 分钟一条，issue #63）。"""
    key = str(kind or "?")
    now = time.time()
    if now - _UNSUPPORTED_TOOL_LOG.get(key, 0) < 600:
        return
    _UNSUPPORTED_TOOL_LOG[key] = now
    log("dropped unsupported tool type %r at %s - Qoder upstream only accepts "
        '"function" (issue #63)' % (key, where), level="WARN", tag="chat")


def _is_custom_tool(tool):
    return isinstance(tool, dict) and str(tool.get("type") or "").lower() == "custom"


_NS_MAX_DEPTH = 4
_NS_CHILD_KEYS = ("tools", "children", "functions")


def _tool_declared_name(tool):
    """工具声明里的名字（Responses 平铺形态或 Chat function 形态）。"""
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    name = tool.get("name") or (fn.get("name") if isinstance(fn, dict) else "")
    return str(name or "").strip()


def _is_namespace_tool(tool):
    """是不是 Codex / MCP 的 namespace 容器（issue #127）。"""
    if not isinstance(tool, dict):
        return False
    if str(tool.get("type") or "").strip() == "namespace":
        return True
    # 有些客户端省略 type，只给 name + children/functions/tools
    if tool.get("type") in (None, ""):
        for key in _NS_CHILD_KEYS:
            if isinstance(tool.get(key), list) and tool.get(key):
                return True
    return False


def _flatten_ns_tools(tools, depth=0, ns_map=None, seen=None):
    """把 `type:"namespace"` 容器展开成平铺工具（issue #127 F1）。

    Codex 把 MCP server / multi-agent 工具装在 namespace 容器里发出；上游只认
    `function`，整块透传会 400（#63），此前的取舍是整块丢弃 —— 结果是工具静默
    消失。展开的产物只有 function/custom 条目，容器本身不出站，不触发同一个
    `unknown variant` 错误。

    返回 (flat_tools, ns_map)：ns_map 记录「工具名 -> namespace」，用于回程把
    namespace 字段补回（客户端按 (name, namespace, kind) 三元组匹配执行器）。
    重名策略：同名只留第一个并打 WARN（两个 MCP server 有同名工具时展平后
    无法区分，保留第一个至少让其中一个是可用的）。
    """
    ns_map = {} if ns_map is None else ns_map
    seen = set() if seen is None else seen
    flat = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if _is_namespace_tool(tool):
            if depth >= _NS_MAX_DEPTH:
                log("namespace tool %r nested deeper than %d - dropped (issue #127)"
                    % (_tool_declared_name(tool) or "?", _NS_MAX_DEPTH),
                    level="WARN", tag="chat")
                continue
            ns = str(tool.get("name") or tool.get("namespace") or "").strip()
            children = []
            for key in _NS_CHILD_KEYS:
                val = tool.get(key)
                if isinstance(val, list) and val:
                    children = val
                    break
            # 子层用**独立**的 seen：共享会让递归先登记、父层再判重时
            # 把所有子工具都当成重复丢掉（issue #127 自查到）。
            sub, _ = _flatten_ns_tools(children, depth + 1, ns_map, set())
            for item in sub:
                name = _tool_declared_name(item)
                if name and name in seen:
                    log("duplicate tool name %r after namespace flatten - "
                        "keeping the first (issue #127)" % name,
                        level="WARN", tag="chat")
                    continue
                if name:
                    seen.add(name)
                    if ns:
                        ns_map[name] = ns
                flat.append(item)
            continue
        name = _tool_declared_name(tool)
        if name and name in seen:
            log("duplicate tool name %r - keeping the first (issue #127)" % name,
                level="WARN", tag="chat")
            continue
        if name:
            seen.add(name)
        flat.append(tool)
    return flat, ns_map


def _additional_tools_from_input(inp):
    """收集 input 里的 additional_tools（Codex Desktop 0.162+，issue #127 F3）。"""
    out = []
    if not isinstance(inp, list):
        return out
    for item in inp:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") not in ("additional_tools", "mcp_tools"):
            continue
        for key in ("tools", "value"):
            val = item.get(key)
            if isinstance(val, list):
                out.extend(val)
                break
    return out


def _stamp_tool_namespace(item, ns_map):
    """回程：把 namespace 补到 function_call/custom_tool_call 项上（issue #127）。"""
    if not isinstance(item, dict) or not ns_map:
        return item
    name = str(item.get("name") or "")
    ns = ns_map.get(name) if name else None
    if ns and not item.get("namespace"):
        item["namespace"] = ns
    return item


def custom_tool_names(tools):
    """Names of tools declared as freeform/custom in a Responses request.

    issue #127：新版 Codex Desktop 会把 `exec` 这类 freeform 工具**嵌在
    namespace 容器里**。不递归进来，回程就判成 function_call，客户端按
    custom_tool_call 匹配不到执行器 —— 调用不执行、模型无限重试。
    """
    names = set()
    flat, _ = _flatten_ns_tools(tools)
    for t in flat:
        if _is_custom_tool(t) and _tool_declared_name(t):
            names.add(_tool_declared_name(t))
    return names


def _downgrade_custom_tool(tool):
    """Rewrite a Responses custom tool into a Chat function tool."""
    desc = tool.get("description") or ""
    fmt = tool.get("format") or {}
    extra = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        extra = "\n\nGrammar:\n" + str(fmt["definition"])
    elif isinstance(fmt, dict) and fmt.get("syntax"):
        extra = "\n\nGrammar syntax: %s" % str(fmt["syntax"])
    return {
        "type": "function",
        "name": tool.get("name") or "",
        "description": (desc + "\n\n" + CUSTOM_TOOL_HINT + extra).strip(),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Complete raw payload for this tool, verbatim.",
                }
            },
            "required": ["input"],
        },
    }


def _tools_for_chat(tools):
    """只保留 Qoder 上游能表达的工具（issue #63）。

    Codex / MCP 会发 `type: "namespace"`、`web_search` 这类 Responses 专有工具；
    原样透传会让上游在反序列化阶段 400（`unknown variant namespace, expected
    function`），整个回合失败。这里把它们丢掉并记一次告警——function 工具不受
    影响，custom 仍按原逻辑降级。
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if _is_custom_tool(t):
            out.append(_downgrade_custom_tool(t))
            continue
        _kind = t.get("type")
        if _kind in (None, "function"):
            out.append(t)
            continue
        _log_unsupported_tool(_kind, "responses-tools")
    return out


def _unwrap_custom_input(args):
    """Pull the freeform string back out of an {"input": "..."} argument blob."""
    if not isinstance(args, str):
        return json.dumps(args or "", ensure_ascii=False)
    try:
        parsed = json.loads(args)
    except Exception:
        return args
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        if val is not None:
            return json.dumps(val, ensure_ascii=False)
    if isinstance(parsed, str):
        return parsed
    return args


_RESPONSES_IGNORED_ONCE = set()


def _note_ignored_responses(payload):
    """issue #87：把当前**未实现**的 Responses 专有字段显式记下来。

    客户端（Codex 等）会带 store / previous_response_id / include / text 等字段，
    我们目前按「无状态 + 全量历史」处理。静默忽略会让排查毫无线索，这里每个
    字段只告警一次（不改变行为，只是让现象可解释）。
    """
    try:
        for key in ("store", "previous_response_id", "include", "text",
                    "truncation", "metadata", "service_tier", "background"):
            if payload.get(key) is None or key in _RESPONSES_IGNORED_ONCE:
                continue
            _RESPONSES_IGNORED_ONCE.add(key)
            log("responses: field %r is not implemented - ignored (issue #87)"
                % key, level="WARN", tag="chat")
    except Exception:
        pass


def responses_to_chat(payload):
    """Translate a Responses API request body into a Chat Completions body."""
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = payload.get("input")
    pending_reasoning = ""
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message"):
                body = _flatten_content(item.get("content"))
                if body:
                    role = item.get("role") or "user"
                    if role == "developer":
                        role = "system"
                    # 与相邻 function_call 的 assistant 文本合并到同一条，
                    # 避免打断 tool 序列
                    if role == "assistant" and messages \
                            and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        if prev.get("content"):
                            prev["content"] = str(prev["content"]) + "\n" + str(body)
                        else:
                            prev["content"] = body
                        if pending_reasoning and "reasoning_content" not in prev:
                            prev["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                    else:
                        msg_dict = {"role": role, "content": body}
                        if role == "assistant" and pending_reasoning:
                            msg_dict["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                        messages.append(msg_dict)
            elif itype == "reasoning":
                r_text = ""
                summ = item.get("summary")
                if isinstance(summ, list):
                    r_text = "\n".join(p.get("text", "") for p in summ
                                       if isinstance(p, dict) and p.get("text"))
                elif isinstance(summ, str):
                    r_text = summ
                if not r_text:
                    cnt = item.get("content")
                    if isinstance(cnt, str):
                        r_text = cnt
                    elif isinstance(cnt, list):
                        r_text = _flatten_content(cnt)
                if r_text:
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["reasoning_content"] = r_text
                    else:
                        pending_reasoning = r_text
            elif itype == "function_call_output":
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    if raw_out.get("type") in ("input_image", "image_url", "image") \
                            or "image_url" in raw_out:
                        content = _flatten_content([raw_out])
                    else:
                        content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "function_call":
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {"role": "assistant", "content": "",
                                "tool_calls": [tc_item]}
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call":
                raw_input = item.get("input")
                if isinstance(raw_input, (dict, list)):
                    raw_input = json.dumps(raw_input, ensure_ascii=False)
                if not isinstance(raw_input, str):
                    raw_input = "" if raw_input is None else str(raw_input)
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": json.dumps({"input": raw_input},
                                                ensure_ascii=False),
                    },
                }
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {"role": "assistant", "content": "",
                                "tool_calls": [tc_item]}
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call_output":
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            else:
                # 未知 item 不静默丢弃：丢工具调用会让上游记录不一致
                log("responses: WARNING unhandled input item type=%r keys=%s"
                    % (itype, sorted(item.keys())[:8]))
    chat = {"model": payload.get("model"), "messages": messages}
    for key in ("temperature", "top_p", "seed"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    effort = None
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    if not effort:
        effort = payload.get("reasoning_effort")
    if effort:
        chat["reasoning_effort"] = effort
    # issue #127：工具清单可能分两处（顶层 tools + input 里的 additional_tools），
    # 且可能包在 type:"namespace" 容器里。先合并再展平，最后才做类型过滤。
    _raw_tools = list(payload.get("tools") or [])
    _raw_tools.extend(_additional_tools_from_input(payload.get("input")))
    if _raw_tools:
        _flat_tools, _ns_map = _flatten_ns_tools(_raw_tools)
        chat["tools"] = _tools_for_chat(_flat_tools)
        if _ns_map:
            # 回程补 namespace 用（不下发给上游）
            chat["_qd_tool_ns"] = _ns_map
    if payload.get("tool_choice"):
        chat["tool_choice"] = payload["tool_choice"]
    if payload.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    else:
        # Responses 语义默认允许并行工具调用（显式传出，避免上游按串行处理）
        chat["parallel_tool_calls"] = True
    if isinstance(payload.get("stream_options"), dict):
        chat["stream_options"] = payload["stream_options"]
    _note_ignored_responses(payload)
    return chat


def _responses_usage(u):
    if not u:
        return None
    det = u.get("completion_tokens_details") or {}
    pdet = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens") or 0,
        "input_tokens_details": {
            "cached_tokens": u.get("cached_tokens")
            or u.get("prompt_cache_hit_tokens")
            or det.get("cached_tokens") or pdet.get("cached_tokens") or 0,
        },
        "output_tokens": u.get("completion_tokens") or 0,
        "output_tokens_details": {
            "reasoning_tokens": det.get("reasoning_tokens") or 0},
        "total_tokens": u.get("total_tokens") or 0,
    }


def chat_to_response(chat_obj, model, custom_names=None, ns_map=None,
                     allowed_names=None):
    """Fold a Chat Completions object into a Responses API response object.

    custom_names: the set of tool names the client declared as freeform.
    Calls to those tools are re-inflated into custom_tool_call items so
    clients such as Codex recognise them.
    """
    custom_names = custom_names or set()
    choice = (chat_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    output = []
    if reasoning:
        output.append({
            "id": _new_id("rs_"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        call_id = tc.get("id") or _new_id("call_")
        name = fn.get("name") or ""
        if name and name in custom_names:
            output.append(_stamp_tool_namespace({
                "id": _new_id("ctc_"),
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": _unwrap_custom_input(fn.get("arguments") or ""),
            }, ns_map))
        else:
            output.append(_stamp_tool_namespace({
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": fn.get("arguments") or "{}",
            }, ns_map))
    # DeepSeek DSML 工具调用回退
    if not (msg.get("tool_calls")):
        dsml_calls, clean_t = parse_dsml_tool_calls(text)
        if dsml_calls:
            for dc in dsml_calls:
                output.append({
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                })
            text = clean_t
    # issue #137：伪 XML 工具调用回退（`<tool_call>Name {json}</tool_call>`）。
    # 与 DSML 回退同位：客户端（Codex 等）走 Responses 协议时同样不许把这种
    # 标签当正文透传。
    if not (msg.get("tool_calls")) and TOOL_CALL_OPEN in text.lower():
        tag_calls, clean_t = parse_tool_call_tags(text, allowed_names)
        for tc in tag_calls or []:
            output.append({
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": tc.get("id") or _new_id("call_"),
                "name": (tc.get("function") or {}).get("name") or "",
                "arguments": (tc.get("function") or {}).get("arguments") or "{}",
            })
        if tag_calls:
            text = clean_t or ""
    if text or not output:
        output.append({
            "id": _new_id("msg_"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text,
                         "annotations": []}] if text else [],
        })
    finish = choice.get("finish_reason") or "stop"
    obj = {
        "id": _new_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed" if finish != "length" else "incomplete",
        "model": model,
        "output": output,
        "output_text": text,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "metadata": {},
    }
    u = _responses_usage(chat_obj.get("usage"))
    if u:
        obj["usage"] = u
    if finish == "length":
        obj["incomplete_details"] = {"reason": "max_output_tokens"}
    return obj


def stream_responses_events(inner_lines, model, holder):
    """Yield Responses-API SSE frames translated from chat-completions chunks.

    inner_lines: 已解包的 "data: {chunk}" 行迭代器（iter_inner_sse 输出）。
    """
    resp_id, msg_id, rs_id = _new_id("resp_"), _new_id("msg_"), _new_id("rs_")
    created = int(time.time())
    # 上游重开后本生成器会被重建：若 seq 从 0 重来，客户端看到的
    # sequence_number 会回退。从 holder 恢复历史最大号，保证严格单调。
    seq = int(holder.get("seq") or 0) if isinstance(holder, dict) else 0
    if isinstance(holder, dict):
        holder["resp_id"] = resp_id
    text_parts, reason_parts = [], []
    outputs = []
    reason_index = None
    msg_index = None
    finish = "stop"
    usage = None
    tool_calls_map = {}
    text_buffer = ""
    dsml_tool_calls = []
    custom_names = set(holder.get("custom_names") or ())

    def resp_obj(status):
        obj = {
            "id": resp_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model,
            "output": [o for o in outputs if o],
            "output_text": "".join(text_parts),
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "metadata": {},
        }
        u = _responses_usage(usage)
        if u:
            obj["usage"] = u
        return obj

    def ev(etype, payload_obj):
        nonlocal seq
        seq += 1
        if isinstance(holder, dict):
            holder["seq"] = seq        # 供外层补发终态事件时续号
        data = {"type": etype, "sequence_number": seq}
        data.update(payload_obj)
        body = json.dumps(data, ensure_ascii=False)
        return ("event: " + etype + "\ndata: " + body + "\n\n").encode("utf-8")

    def reason_item(status):
        return {
            "id": rs_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": "".join(reason_parts)}],
        }

    def msg_item(status):
        item = {"id": msg_id, "type": "message", "status": status,
                "role": "assistant", "content": []}
        if text_parts:
            item["content"] = [{"type": "output_text",
                                "text": "".join(text_parts),
                                "annotations": []}]
        return item

    yield ev("response.created", {"response": resp_obj("in_progress")})
    yield ev("response.in_progress", {"response": resp_obj("in_progress")})
    for raw in inner_lines:
        data = strip_data_prefix(raw.decode("utf-8", "replace")
                                 if isinstance(raw, bytes) else raw)
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if usage is None and chunk.get("usage"):
            usage = chunk["usage"]
            holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if piece:
                if reason_index is None:
                    reason_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added",
                             {"output_index": reason_index,
                              "item": reason_item("in_progress")})
                    yield ev("response.reasoning_summary_part.added", {
                        "item_id": rs_id, "output_index": reason_index,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    })
                reason_parts.append(piece)
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": rs_id, "output_index": reason_index,
                    "summary_index": 0, "delta": piece,
                })
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                call_id = tc.get("id") or ""
                if idx not in tool_calls_map:
                    out_idx = len(outputs)
                    outputs.append(None)
                    c_id = call_id or _new_id("call_")
                    is_custom = bool(fn_name) and fn_name in custom_names
                    entry = {
                        "output_index": out_idx,
                        "id": c_id,
                        "name": fn_name,
                        "arguments": fn_args,
                        "custom": is_custom,
                        "item_id": _new_id("ctc_" if is_custom else "fc_"),
                    }
                    tool_calls_map[idx] = entry
                    item = {"id": entry["item_id"], "status": "in_progress",
                            "call_id": c_id, "name": fn_name}
                    # issue #127：namespace 必须在 added 帧就带上，
                    # 否则客户端按 (name, namespace, kind) 匹配不到执行器
                    _stamp_tool_namespace(item, (holder or {}).get("tool_ns"))
                    if is_custom:
                        item["type"] = "custom_tool_call"
                        item["input"] = ""
                    else:
                        item["type"] = "function_call"
                        item["arguments"] = ""
                    yield ev("response.output_item.added",
                             {"output_index": out_idx, "item": item})
                else:
                    entry = tool_calls_map[idx]
                    if fn_name and not entry["name"]:
                        entry["name"] = fn_name
                        if fn_name in custom_names:
                            entry["custom"] = True
                    if fn_args:
                        entry["arguments"] += fn_args
                        if entry.get("custom"):
                            yield ev("response.custom_tool_call_input.delta", {
                                "output_index": entry["output_index"],
                                "item_id": entry["item_id"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
                        else:
                            yield ev("response.function_call_arguments.delta", {
                                "output_index": entry["output_index"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
            piece = delta.get("content")
            if piece:
                if msg_index is None:
                    if reason_index is not None:
                        full_r = "".join(reason_parts)
                        yield ev("response.reasoning_summary_text.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0, "text": full_r,
                        })
                        yield ev("response.reasoning_summary_part.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": full_r},
                        })
                        outputs[reason_index] = reason_item("completed")
                        yield ev("response.output_item.done",
                                 {"output_index": reason_index,
                                  "item": outputs[reason_index]})
                    msg_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added", {
                        "output_index": msg_index,
                        "item": {"id": msg_id, "type": "message",
                                 "status": "in_progress", "role": "assistant",
                                 "content": []},
                    })
                    yield ev("response.content_part.added", {
                        "item_id": msg_id, "output_index": msg_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "",
                                 "annotations": []},
                    })
                # DSML 缓冲：不把原始 DSML 标签流给客户端
                text_buffer += piece
                # issue #16-B1：散文与 marker 同帧 -> 先把 marker 之前的散文
                # 发出（不拖字），marker 起留在缓冲里等流末判定。
                # 含 "<" 的散文不走这条捷径（交给下面的 DSML 剥离逻辑）。
                _emit16, _hold16 = _find_marker_split(text_buffer)
                if _hold16 and _emit16 and "<" not in _emit16:
                    text_parts.append(_emit16)
                    yield ev("response.output_text.delta", {
                        "item_id": msg_id, "output_index": msg_index,
                        "content_index": 0, "delta": _emit16,
                    })
                    text_buffer = _hold16
                # 泄漏回读（issue #8）：缓冲本身是标记/候选时先压住，流末统一判定。
                _leak_defer = _marker_starts_at(text_buffer) or (
                    not any(text_parts) and (
                        _leak_prefix_hold(text_buffer)
                        or _tool_echo_prefix_hold(text_buffer)))
                while text_buffer and not _leak_defer:
                    idx = text_buffer.find("<")
                    if idx == -1:
                        text_parts.append(text_buffer)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": text_buffer,
                        })
                        text_buffer = ""
                        break
                    m = DSML_CALLS_RE.search(text_buffer)
                    if m and m.start() == idx:
                        if idx > 0:
                            lead = text_buffer[:idx]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                        calls_found, _ = parse_dsml_tool_calls(m.group(0))
                        if calls_found:
                            dsml_tool_calls.extend(calls_found)
                        text_buffer = text_buffer[m.end():]
                        continue
                    cand = text_buffer[idx:idx + 30]
                    is_cand = ("DSML" in cand) or (
                        len(cand) < 10 and not any(c in cand
                                                   for c in (" ", "\t", "\n", ">")))
                    if is_cand:
                        lead = text_buffer[:idx]
                        text_parts.append(lead)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": lead,
                        })
                        text_buffer = text_buffer[idx:]
                        break
                    else:
                        next_lt = text_buffer[idx + 1:].find("<")
                        if next_lt != -1:
                            flush_len = idx + 1 + next_lt
                            lead = text_buffer[:flush_len]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                            text_buffer = text_buffer[flush_len:]
                        else:
                            text_parts.append(text_buffer)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": text_buffer,
                            })
                            text_buffer = ""
                            break
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if reason_index is not None and outputs[reason_index] is None:
        full_r = "".join(reason_parts)
        yield ev("response.reasoning_summary_text.done", {
            "item_id": rs_id, "output_index": reason_index,
            "summary_index": 0, "text": full_r,
        })
        yield ev("response.reasoning_summary_part.done", {
            "item_id": rs_id, "output_index": reason_index,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": full_r},
        })
        outputs[reason_index] = reason_item("completed")
        yield ev("response.output_item.done",
                 {"output_index": reason_index, "item": outputs[reason_index]})
    # 1. 已完成的结构化工具调用
    for idx in sorted(tool_calls_map.keys()):
        entry = tool_calls_map[idx]
        if entry.get("custom"):
            yield ev("response.custom_tool_call_input.done", {
                "output_index": entry["output_index"],
                "item_id": entry["item_id"],
                "call_id": entry["id"],
                "input": _unwrap_custom_input(entry["arguments"]),
            })
            fc_item = _stamp_tool_namespace({
                "id": entry["item_id"],
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": entry["id"],
                "name": entry["name"],
                "input": _unwrap_custom_input(entry["arguments"]),
            }, (holder or {}).get("tool_ns"))
        else:
            yield ev("response.function_call_arguments.done", {
                "output_index": entry["output_index"],
                "call_id": entry["id"],
                "arguments": entry["arguments"],
            })
            fc_item = _stamp_tool_namespace({
                "id": entry["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": entry["id"],
                "name": entry["name"],
                "arguments": entry["arguments"],
            }, (holder or {}).get("tool_ns"))
        outputs[entry["output_index"]] = fc_item
        yield ev("response.output_item.done",
                 {"output_index": entry["output_index"], "item": fc_item})
    # 冲刷剩余缓冲文本
    if text_buffer:
        if not tool_calls_map and not dsml_tool_calls:
            _raise_unstructured_tool_echo(text_buffer, holder.get("allowed_names"))
        leak_calls, leak_clean = (None, text_buffer)
        dropped_leak = False
        dropped_tool_echo = False
        dropped_bad_args = False
        # issue #16-B1：散文已先发出时 any(text_parts) 为真，但缓冲本身若以
        # 标记开头（切分后剩下的回声块）仍必须走回读/吞掉判定，否则会漏。
        if not any(text_parts) or _marker_starts_at(text_buffer):
            leak_calls, leak_clean = parse_leaked_tool_calls(
                text_buffer, holder.get("allowed_names"))
            # issue #37（fail-closed）**先于 #9/#11**：坏参数块的外层数组完整
            # 合法，带 DSML/围栏残片时会被 _leaked_partial_droppable 误判成
            # 「截断」并静默吞掉（正常完成）——那既不是失败也不是恢复。
            if leak_calls is None and _echo_with_bad_args(
                    text_buffer, holder.get("allowed_names")):
                dropped_bad_args = True
            elif leak_calls is None and _leaked_partial_droppable(
                    text_buffer, holder.get("allowed_names")):
                dropped_leak = True
            elif leak_calls is None and _tool_echo_droppable(
                    text_buffer, holder.get("allowed_names")):
                dropped_tool_echo = True
        if leak_calls:
            dsml_tool_calls.extend(
                {"id": c.get("id"),
                 "name": (c.get("function") or {}).get("name") or "",
                 "arguments": (c.get("function") or {}).get("arguments") or "{}"}
                for c in leak_calls)
            calls_rem, clean_rem = None, leak_clean
        elif dropped_leak:
            # issue #72：客户端声明了 tools、本轮又没恢复出任何调用时，
            # **不能以 response.completed 收尾**（agent 会误判任务完成）。
            _allowed72 = holder.get("allowed_names") or set()
            _kind72 = ("marker_only" if _marker_only_echo(text_buffer)
                       else "partial_array")
            log("dropped truncated leaked tool-call echo (%d chars, %s, "
                "tools_declared=%d) — failing the turn closed (issue #72)"
                % (len(text_buffer), _kind72, len(_allowed72)),
                level="WARN", tag="chat")
            raise EmptyToolEcho(len(text_buffer), _kind72)
        elif dropped_tool_echo:
            # issue #11：工具结果回声块 -> 同样吞掉，不走 DSML 回退。
            log("dropped leaked tool-result echo (%d chars) — issue #11"
                % len(text_buffer), level="WARN", tag="chat")
            calls_rem, clean_rem = None, None
        elif dropped_bad_args:
            # issue #37（fail-closed）：不输出协议，也不走正常完成——抛错，
            # 由 Responses 出口发 response.failed。
            log("tool-call echo has invalid JSON arguments (%d chars) — "
                "failing the turn closed (issue #37)" % len(text_buffer),
                level="WARN", tag="chat")
            raise InvalidToolArguments()
        else:
            calls_rem, clean_rem = parse_dsml_tool_calls(text_buffer)
            if not calls_rem:
                # issue #49 方向 C：Responses 侧同样留痕（DSML 也解析不出来时）
                _log_unrecovered_leak(clean_rem or text_buffer, "responses")
                # issue #120 后续：Responses 侧也不许把残片当正文补发。
                # 空壳残片（只有标记 + 空标签）直接丢：text_buffer 不清空就不会进
                # text_parts，等于静默丢弃；带协议结构又还原不了的走 502 出口。
                _allowed_resp = holder.get("allowed_names") or set()
                if (not _leak_garbage_only(clean_rem or text_buffer)
                        and _allowed_resp
                        and (_leak_residue_present(_outside_fences(text_buffer))
                             or _dsml_residue_present(_outside_fences(text_buffer)))
                        and not _echo_names_all_undeclared(text_buffer, _allowed_resp)):
                    log("unrecovered leak residue in tools session (%d chars) — "
                        "failing the turn closed (responses)" % len(text_buffer),
                        level="WARN", tag="chat")
                    raise EmptyToolEcho(len(text_buffer), "unrecovered_responses")
        if calls_rem:
            dsml_tool_calls.extend(calls_rem)
        if clean_rem:
            text_parts.append(clean_rem)
            if msg_index is not None:
                yield ev("response.output_text.delta", {
                    "item_id": msg_id, "output_index": msg_index,
                    "content_index": 0, "delta": clean_rem,
                })
        text_buffer = ""
    # 2. DSML 回退：无结构化工具调用时用解析出的 DSML 调用
    full_text = "".join(text_parts)
    dsml_calls = dsml_tool_calls
    if not dsml_calls:
        extra_calls, clean_text = parse_dsml_tool_calls(full_text)
        if extra_calls:
            dsml_calls = extra_calls
            full_text = clean_text
    if dsml_calls and not tool_calls_map:
        for dc in dsml_calls:
            out_idx = len(outputs)
            fc_item = {
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": dc.get("id") or _new_id("call_"),
                "name": dc.get("name") or "",
                "arguments": dc.get("arguments") or "{}",
            }
            outputs.append(fc_item)
            yield ev("response.output_item.added", {
                "output_index": out_idx,
                "item": dict(fc_item, status="in_progress", arguments=""),
            })
            yield ev("response.function_call_arguments.delta", {
                "output_index": out_idx,
                "call_id": fc_item["call_id"],
                "delta": fc_item["arguments"],
            })
            yield ev("response.function_call_arguments.done", {
                "output_index": out_idx,
                "call_id": fc_item["call_id"],
                "arguments": fc_item["arguments"],
            })
            yield ev("response.output_item.done",
                     {"output_index": out_idx, "item": fc_item})
    # 3. 有文本或没有任何输出项时，补 message 项
    has_other_items = any(o for o in outputs if o)
    if msg_index is not None or full_text or not has_other_items:
        if msg_index is None:
            msg_index = len(outputs)
            outputs.append(None)
            yield ev("response.output_item.added", {
                "output_index": msg_index,
                "item": {"id": msg_id, "type": "message",
                         "status": "in_progress", "role": "assistant",
                         "content": []},
            })
            yield ev("response.content_part.added", {
                "item_id": msg_id, "output_index": msg_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "",
                         "annotations": []},
            })
        yield ev("response.output_text.done", {
            "item_id": msg_id, "output_index": msg_index,
            "content_index": 0, "text": full_text,
        })
        yield ev("response.content_part.done", {
            "item_id": msg_id, "output_index": msg_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": full_text,
                     "annotations": []},
        })
        outputs[msg_index] = msg_item("completed")
        yield ev("response.output_item.done",
                 {"output_index": msg_index, "item": outputs[msg_index]})
    status = "completed" if finish != "length" else "incomplete"
    final = resp_obj(status)
    if finish == "length":
        final["incomplete_details"] = {"reason": "max_output_tokens"}
    yield ev("response.completed", {"response": final})


def _responses_completed_quiet(holder, model=""):
    """issue #135：lenient 模式下用「正常完成」收尾（保留已产出的正文）。"""
    holder = holder or {}
    text = holder.get("text") or holder.get("content") or ""
    return ("event: response.completed\ndata: " + json.dumps({
        "type": "response.completed",
        "response": {
            "id": holder.get("id") or "resp_lenient",
            "object": "response",
            "status": "completed",
            "model": model or "",
            "output": ([{"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": text}]}]
                        if text else []),
            "usage": _responses_usage(holder.get("usage")) or {},
        },
    }, ensure_ascii=False) + "\n\n")


def _responses_failed_frame(holder, code, message):
    """Responses 流的终态失败事件（response.failed）。

    stream_responses_events 的 ev() 闭包把最新 sequence_number 同步进
    holder["seq"]，这里在其上 +1 续号，保证严格大于已发出的事件——
    Responses 客户端只认终态事件，缺了它会一直等（原实现只 _sse_end）。
    """
    seq = int((holder or {}).get("seq") or 0) + 1
    data = {
        "type": "response.failed",
        "sequence_number": seq,
        "response": {
            "id": (holder or {}).get("resp_id") or _new_id("resp_"),
            "object": "response",
            "status": "failed",
            "error": {"code": str(code), "message": message},
        },
    }
    return ("event: response.failed\ndata: "
            + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Which configured API key the caller used, set by _key_ok(). Its bound
    # realm decides the upstream exit for this request alone.
    key_entry = None

    def handle(self):
        try:
            super(Handler, self).handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def finish(self):
        try:
            super(Handler, self).finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    server_version = "qd-proxy/" + VERSION

    def log_message(self, fmt, *args):
        # 静默看板高频轮询的正常 200 GET，异常状态与业务操作照常记录。
        try:
            req_path = (getattr(self, "path", None)
                        or (args[0] if args else "")).split("?")[0]
            if req_path == "/favicon.ico":
                return           # 浏览器自动请求的 404 不刷屏
            status_code = int(args[1]) if len(args) > 1 \
                and str(args[1]).isdigit() else 200
            if status_code < 400 and getattr(self, "command", "GET") == "GET":
                quiet_prefixes = (
                    "/logs", "/usage", "/accounts", "/scheduler",
                    "/health", "/panel/status", "/realm",
                )
                if any(req_path == p or req_path.startswith(p + "/")
                       for p in quiet_prefixes):
                    return
        except Exception:
            pass
        log(fmt % args)

    def _sse_begin(self):
        """开始一个 HTTP/1.1 SSE 流：用 chunked 编码，保持连接可复用。

        之前用 `Connection: close` + 裸写字节：客户端（连接池型 harness）
        会把该连接视为可复用，下一次请求落在已半关闭的连接上，表现为
        “一直重连 / 连不上”。改为 chunked 后，流结束发 0 长度的终止块，
        连接保持 keep-alive，可安全复用。
        """
        if getattr(self, "_chunked", False):
            return          # 幂等：排队可视化会提前开流，后续调用不能重复发头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        realm_used = getattr(self, "realm_used", "") or ""
        if realm_used:
            # #26：流式响应同样标注实际出口 + 原因
            self.send_header("X-Qoder-Realm", realm_used)
            realm_reason = getattr(self, "realm_reason", "") or ""
            if realm_reason:
                self.send_header("X-Qoder-Realm-Reason", realm_reason)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._chunked = True

    def _sse_write(self, data):
        """按 chunked 编码写一段数据（data: bytes）。"""
        if not data:
            return
        if not getattr(self, "_chunked", False):
            # 理论上不会发生；保守回退成裸写
            self.wfile.write(data)
            self.wfile.flush()
            return
        self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        """结束 chunked 流（写 0 长度终止块），连接保持可复用。"""
        if not getattr(self, "_chunked", False):
            return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        self._chunked = False

    def _queue_notice(self, flavor, info):
        """排队提示帧（issue #87 visible 模式）。

        先用 SSE 注释心跳保活，再发一条**客户端可读**的排队状态：
        chat 走 `qoder_queue` 字段；responses 走 `response.qoder_queue` 事件；
        anthropic 走 `ping` 事件（带 qoder_queue 附加字段，客户端会忽略未知键）。
        """
        self._sse_write(b": ping\n\n")
        body = json.dumps(info, ensure_ascii=False)
        if flavor == "anthropic":
            self._sse_write(("event: ping\ndata: "
                             + json.dumps({"type": "ping", "qoder_queue": info},
                                          ensure_ascii=False) + "\n\n").encode("utf-8"))
        elif flavor == "responses":
            self._sse_write(("event: response.qoder_queue\ndata: "
                             + json.dumps({"type": "response.qoder_queue",
                                           "sequence_number": 0,
                                           "qoder_queue": info},
                                          ensure_ascii=False) + "\n\n").encode("utf-8"))
        else:
            self._sse_write(("data: " + json.dumps(
                {"id": "chatcmpl-qoder-queue",
                 "object": "chat.completion.chunk",
                 "created": int(time.time()),
                 "model": info.get("model") or "",
                 "choices": [{"index": 0, "delta": {"content": ""},
                              "finish_reason": None}],
                 "qoder_queue": info}, ensure_ascii=False) + "\n\n").encode("utf-8"))

    def _queue_error_frame(self, flavor, message, waited, budget):
        """排队超预算（visible 模式）：给明确的中文错误，而不是静默断开。"""
        info = {"message": message, "waited_seconds": int(waited),
                "budget_seconds": int(budget), "code": "upstream_queued"}
        if flavor == "anthropic":
            self._sse_write(("event: error\ndata: " + json.dumps(
                {"type": "error", "error": {"type": "overloaded_error",
                                            "message": message},
                 "qoder_queue": info}, ensure_ascii=False) + "\n\n").encode("utf-8"))
        elif flavor == "responses":
            self._sse_write(("event: response.failed\ndata: " + json.dumps(
                {"type": "response.failed",
                 "response": {"status": "failed",
                              "error": {"code": "upstream_queued",
                                        "message": message}},
                 "qoder_queue": info}, ensure_ascii=False) + "\n\n").encode("utf-8"))
        else:
            self._sse_write(("data: " + json.dumps(
                {"error": {"message": message, "type": "upstream_queued",
                           "code": 503}, "qoder_queue": info},
                ensure_ascii=False) + "\n\n").encode("utf-8"))
            self._sse_write(b"data: [DONE]\n\n")

    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        for _k, _v in (extra_headers or {}).items():
            self.send_header(str(_k), str(_v))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        realm_used = getattr(self, "realm_used", "") or ""
        if realm_used:
            # #26：本次**实际出口**（自动路由后可能与请求开始时不同）
            self.send_header("X-Qoder-Realm", realm_used)
            realm_reason = getattr(self, "realm_reason", "") or ""
            if realm_reason:
                self.send_header("X-Qoder-Realm-Reason", realm_reason)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message, err_type="server_error", retry_after=None):
        code = _client_error_status(code, err_type)   # issue #137
        extra = {"Retry-After": int(retry_after)} if retry_after else None
        self._json(code, {"error": {"message": message, "type": err_type,
                                    "code": code}}, extra_headers=extra)

    def _unknown_path_error(self, path):
        """404，并针对最常见的「接入地址填错」给出可执行提示。

        聚合网关（new-api / one-api 等）配渠道时最常见的两类错：
          · base URL 里已经带了 /v1，客户端又拼一次 -> /v1/v1/chat/completions；
          · 把 OpenAI 的路径整段填进 base URL -> /v1/chat/completions/v1/chat/completions。
        这两类以前只会得到一句 not found，很难自证；现在直接把原因写进错误体并记 WARN。
        """
        p = str(path or "")
        hint = ""
        if p.count("/v1") > 1:
            hint = ("base URL 里已经带了 /v1，客户端又拼了一次；把渠道地址填成 "
                    "http://<host>:8790（不带 /v1）即可")
        elif p.rstrip("/").endswith("/v1/chat/completions") and p.count("/") > 3:
            hint = "base URL 填成了完整接口路径；只需填 http://<host>:8790"
        try:
            log("404 unknown path: %s%s"
                % (p, (" — " + hint) if hint else ""),
                level="WARN", tag="http")
        except Exception:
            pass
        return self._error(404, "not found: %s%s" % (p, (" — " + hint) if hint else ""),
                           "invalid_request_error")

    def _rate_limited(self, exc):
        """429 with Retry-After, so clients back off instead of hammering."""
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        body = json.dumps({
            "error": {
                "message": ("upstream rate limit reached for this model; "
                            "retry in %ds" % wait)
                + ((" - " + _neutralize_auth_codes(exc.detail[:200]))   # issue #137
                   if getattr(exc, "detail", "") else ""),
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, filename, obj):
        """Send a JSON document as a browser download.

        Content-Disposition is quoted because the filename is generated from
        user-controlled parts (the realm filter) and could otherwise break the
        header or allow a response-splitting attempt.
        """
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", str(filename))[:120] \
            or "export.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % safe)
        self.send_header("Cache-Control", "no-store")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _supplied_key(self):
        """The key the caller presented, from the header or the ?key= query."""
        supplied = (self.headers.get("Authorization") or "").removeprefix(
            "Bearer ").strip()
        if supplied:
            return supplied
        # Anthropic 客户端（Claude Code 等）用 x-api-key 头，而不是
        # Authorization: Bearer —— 漏掉这一路回退会让 /v1/messages 全量 401。
        anthropic_key = (self.headers.get("x-api-key") or "").strip()
        if anthropic_key:
            return anthropic_key
        # 浏览器顶层导航无法设置头，所以 ?key= 也接受（看板跨设备打开用）。
        try:
            query = parse_qs(urlparse(self.path).query)
            return (query.get("key") or [""])[0].strip()
        except Exception:
            return ""

    def _key_ok(self):
        """True when the request carries a right key (or no key is needed)."""
        # 面板会话同时解锁管理 API，浏览器无需在 localStorage 存 API key。
        if self._panel_ok():
            return True
        self.key_entry = identify_key(self._supplied_key())
        if self.key_entry:
            return True
        if not auth_required():
            return True
        return False

    def _key_realm(self):
        """Realm bound to the key this request used, or "" when unbound."""
        return (self.key_entry or {}).get("realm") or ""

    def _key_id_for_usage(self):
        """usage 行归属用的 API key 标识（panel 分配的 id / legacy / launcher）。

        只取 `key_entry["id"]`——它是 panel 生成的稳定 id，**绝不含明文 key**
        （本仓红线）；无 key（面板会话 / 未开鉴权）时返回空串，调用方据此不写该键。
        """
        return str((self.key_entry or {}).get("id") or "")

    def _cross_realm_error(self, model, realm):
        """解释模型/出口错配，替代上游晦涩的 403。"""
        if not realm or not model:
            return ""
        owner = exclusive_realm(model)
        if not owner or owner == realm:
            return ""
        name = (self.key_entry or {}).get("name") or "当前 Key"
        served = "国内版" if owner == "cn" else "国际版"
        used = "国内版" if realm == "cn" else "国际版"
        return ("模型 %s 只在%s提供，但「%s」绑定的是%s出口。"
                "请改用对应出口的 Key，或把该 Key 的出口改为「跟随面板切换」。"
                % (model, served, name, used))

    def _banned_model_error(self, model):
        """P1-4 全局封禁：命中即返回文案（不碰上游、不扣任何点数）。"""
        if not is_model_banned(model):
            return ""
        return banned_model_message(model)

    def _key_model_error(self, model):
        """P1-4 每 Key 模型白名单：在请求到达上游之前拒绝。

        未设 models 的 Key 不受限（空列表 = 不限制），因此这是 no-op，
        除非管理员显式配置了限制。
        """
        entry = getattr(self, "key_entry", None)
        if not entry:
            return ""
        if qoder_settings.key_allows_model(entry, model):
            return ""
        return key_model_message(entry, model)

    def _model_precheck(self, model, realm):
        """模型级前置拒绝（统一落点，全部在 open_upstream 之前）。

        顺序：跨区错配[既有] -> 全局封禁[P1-4] -> Key 白名单[P1-4]。
        三处请求入口（chat / responses / anthropic messages）**共用本方法**——
        新增入口必须一并挂上，否则会出现「chat 被拦、别的协议绕过」的缺口。
        """
        return (self._cross_realm_error(model, realm)
                or self._banned_model_error(model)
                or self._key_model_error(model))

    def _apply_context_header(self, target):
        """把 X-QD-Context 请求头写进将传给 build_qoder_body 的 payload（#106）。

        先无条件清掉 body 里的同名键：客户端不能靠伪造 _qd_context 提窗；
        只有 HTTP 头能设置。值可为 1M/400K/200K（对照官方 context_config）
        或正整数 token 数，非法值在 model_context_override 里统一忽略并告警。
        """
        try:
            if isinstance(target, dict):
                target.pop("_qd_context", None)
                hdr = str(self.headers.get("X-QD-Context") or "").strip()[:12]
                if hdr:
                    target["_qd_context"] = hdr
        except Exception:
            pass

    def _request_realm(self, explicit=None):
        """Pick the upstream exit for this request.

        Priority: an explicit ?realm= argument, then the realm bound to the
        API key, then the X-Realm header / ?realm= query, and finally the
        global switch. Returning None lets open_upstream() fall back to
        model-based detection.
        """
        if explicit:
            return explicit
        bound = self._key_realm()
        if bound:
            return bound
        header = self.headers.get("X-Realm")
        if header:
            return header
        try:
            return parse_qs(urlparse(self.path).query).get("realm", [None])[0]
        except Exception:
            return None

    def _authorized(self):
        if self._key_ok():
            _CLIENT_CTX.key_entry = getattr(self, "key_entry", None)
            return True
        self._error(401, "invalid api key", "invalid_request_error")
        return False

    # ---- web panel access ----
    def _panel_token(self):
        """Session token from the X-Panel-Token header.

        Deliberately header-only: a token in the query string leaks through
        browser history, the Referer header and any reverse-proxy access log.
        """
        return (self.headers.get("X-Panel-Token") or "").strip()

    def _panel_ok(self):
        return PANEL.valid(self._panel_token())

    def _client_is_loopback(self):
        """agents 写配置只信 loopback：改的是网关本机的文件。"""
        try:
            host = self.client_address[0]
            return ipaddress.ip_address(host).is_loopback
        except Exception:
            return False

    @staticmethod
    def _is_panel_route(path):
        """Management endpoints shown in the web panel.

        Model listings stay reachable with the API key alone so that plain
        OpenAI clients can keep discovering models.
        """
        if path.startswith("/admin"):
            return True
        if path.startswith("/accounts"):
            return True
        if path.startswith("/usage") or path.startswith("/v1/usage"):
            return True
        if path.startswith("/tasks") or path.startswith("/scheduler"):
            return True
        if path.startswith("/settings"):
            return True
        if path.startswith("/logs"):
            return True
        if path.startswith("/diag"):
            return True
        if path.startswith("/update"):
            return True
        if path.startswith("/agents"):
            # /agents/cli 是唯一的例外：公开只读的配置器源码端点（远程机器
            # curl 取用），面板拦截会挡掉它的目标用户。
            return path != "/agents/cli"
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # #26：每个请求入口清空「本次实际出口」标注。self 在 keep-alive 连接上是
        # **复用**的，不清空的话后续非 chat 端点（/v1/models、/usage/*、/realm）
        # 会带回上一次 chat 的残留值 —— 那会让面板把「上一次的出口」当成「本次的」。
        self.realm_used = ""
        self.realm_reason = ""
        _CLIENT_CTX.key_entry = None
        # issue #56：记录客户端 IP（团队账号的「是否被共用」统计用）
        try:
            _CLIENT_CTX.ip = self.client_address[0]
        except Exception:
            pass
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required",
                               "invalid_request_error")
        if path.startswith("/admin/"):
            return self._handle_admin_get(path, query)
        if path in ("/", "/dashboard", "/ui"):
            return self._dashboard()
        if path == "/i18n/manifest":
            return self._i18n_manifest()
        if path.startswith("/i18n/"):
            return self._i18n_pack(path[len("/i18n/"):])
        if path == "/panel/status":
            # 不带 token 也要回答：看板需要先知道是否显示登录页。
            info = {
                "panel_password_required": True,
                "panel_password_is_default":
                    qoder_settings.panel_password_is_default(ACCOUNTS_DIR),
                "authenticated": self._panel_ok(),
            }
            info["api_key_set"] = bool(API_KEY)
            return self._json(200, info)
        if path in ("/ping", "/healthz", "/livez", "/readyz"):
            # 极简探活端点：**不需要面板密码/API Key、不查账号池**。
            # 客户端/守护脚本常用 GET /ping 判活，之前返回 404 会被判成
            # “网关不可用” → 反复重连。这里返回纯文本 pong。
            body = b"pong\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/agents/cli":
            # 配置器 CLI 源码（远程机器直接 curl 取用，免面板鉴权；
            # 与仓库内 qoder_agents.py 逐字节一致）。只读。
            try:
                cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "qoder_agents.py")
                with open(cli_path, "rb") as fh:
                    body = fh.read()
            except OSError as exc:
                return self._error(500, "agents cli source unavailable: %s" % exc)
            self.send_response(200)
            self.send_header("Content-Type", "text/x-python; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/agents":
            if not self._authorized():
                return
            try:
                return self._json(200, qoder_agents.overview(ACCOUNTS_DIR))
            except qoder_agents.AgentConfigError as exc:
                return self._error(500, str(exc))
        if path == "/agents/template":
            try:
                return self._json(200, qoder_agents.template(
                    str((query.get("client") or [""])[0]),
                    str((query.get("base_url") or ["http://127.0.0.1:8790"])[0]),
                    model=str((query.get("model") or ["auto"])[0])))
            except qoder_agents.AgentConfigError as exc:
                return self._error(400, str(exc), "invalid_request_error")
        if path == "/agents/models":
            # 网关自己的模型清单（面板配置页预填用；与 /v1/models 同源字段）
            if not self._authorized():
                return
            realm_q = query.get("realm", [None])[0]
            return self._json(200, gateway_models_for_agents(realm_q))
        if path == "/update/check":
            # 项目新版本检测（看板「运行信息 · 新版本检测」；结果缓存 6h，force=1 强制刷新）
            _u = parse_qs(urlparse(self.path).query)
            try:
                return self._json(200, check_for_update(
                    force=bool(_u.get("force", [None])[0])))
            except Exception as exc:
                return self._error(500, "update check failed: %s" % exc)
        if path == "/update/plan":
            # 一键更新前的预检（issue: 一键更新）：能不能更新、当前/目标版本、
            # 是否容器、是否被禁用。不下载任何东西。
            # issue #132：面板只凭 /update/check 的 has_update 就渲染按钮，
            # 在「环境变量禁用 / 容器内 / 非本机访问」三种必然失败的情况下
            # 仍然给出一个按了就报错的动作。这里把**权威可执行性**一并回显。
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            _info = update_plan_info()
            _client = (self.client_address or ["?"])[0]
            _loopback = _is_loopback(_client)
            _reasons = []
            if _info.get("disabled"):
                _reasons.append("已被环境变量禁用（QD_UPDATE_DISABLE=1）")
            if _info.get("container"):
                _reasons.append("运行在容器里：请改用镜像更新")
            if not _loopback and not _update_allow_remote():
                _reasons.append("仅允许在本机打开面板执行"
                                "（或设置 QD_UPDATE_ALLOW_REMOTE=1）")
            if not _info.get("has_update"):
                if _info.get("lookup_failed"):
                    _reasons.append("无法访问 GitHub（限流或网络不可达），"
                                    "稍后重试或检查代理")
                else:
                    _reasons.append("当前已是最新版本")
            _info["client_is_loopback"] = bool(_loopback)
            _info["can_apply"] = bool(_info.get("can_apply")) and not _reasons
            _info["apply_blocked_reasons"] = _reasons
            return self._json(200, _info)
        if path == "/diag/vm":
            # 本机虚拟化检测（看板「签到与福利中心」展示；中文输出）。
            # 以官方风控桥 runtime-info 的 vmInfo 为准，桥不可用时本机交叉校验。
            # issue #123 后续：把「判定来源」与「桥是否真的可用」一并回显，
            # 否则用户看到「本地：单板计算机」+「官方风控：KVM」会以为是自相矛盾。
            _q = parse_qs(urlparse(self.path).query)
            try:
                _realm = _q.get("realm", [None])[0] or CURRENT_REALM
                _st = local_vm_status(_realm, force=bool(_q.get("force", [None])[0]))
                _exe = qoder_accounts.runtime_info_exe(_realm)
                _raw = qoder_accounts.run_runtime_info(_realm) or {}
                _vmi = _raw.get("vmInfo") if isinstance(_raw.get("vmInfo"), dict) else {}
                _st["bridge_exe"] = _exe or ""
                _st["bridge_available"] = bool(_exe)
                _st["bridge_vm_info"] = {
                    k: _vmi.get(k) for k in
                    ("isVm", "brand", "percentage", "vmTypeCode")
                    if k in _vmi}
                _st["local_evidence"] = list(_st.get("evidence") or [])
                return self._json(200, _st)
            except Exception as exc:
                return self._error(500, "vm status failed: %s" % exc)
        if path == "/health":
            rep = current_account()
            info = {
                "ok": True,
                "service": "qoder-proxy",
                "version": VERSION,
                "pid": os.getpid(),
                "started_at": int(STARTED_AT),
                "started_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(STARTED_AT)),
                "realm": CURRENT_REALM,
                "accounts": len(POOL.accounts) if POOL else 0,
                "accounts_ready": POOL.count_ready() if POOL else 0,
                "api_key_required": bool(API_KEY),
                "risk": risk_snapshot(),
                "queue_observation": queue_observation_snapshot(),
                "env": env_report(),
                "quality": quality_snapshot(),
                "image_preflight": qoder_images.status(),
                "analytics_cache": _USAGE_REPORT_CACHE.status(),
            }
            if self._key_ok():
                info.update({
                    "uid": rep.uid if rep else None,
                    "domain": rep.domain if rep else None,
                    "issuer": "qoder" if rep else None,
                    "credential_file": os.path.basename(rep.path)
                    if rep and rep.path else None,
                    "expires_at": rep.expires_at if rep else None,
                })
            return self._json(200, info)
        if path == "/realm":
            # #26：面板需要知道「自动路由是否开着」——mode 由 auto_enabled + current
            # 推导：自动开 = 未显式指定时会按模型/账号可用性兜底换区；否则恒用 current。
            return self._json(200, {"current": CURRENT_REALM,
                                    "options": ["intl", "cn"],
                                    "auto_enabled": bool(AUTO_REALM_ENABLED),
                                    "auto_env": "QD_AUTO_REALM"})
        if path in ("/models/refresh", "/v1/models/refresh"):
            # issue #58：容器里模型清单陈旧时，不必重启即可重新向上游发现。
            if not self._authorized():
                return
            req_realm = self._request_realm() or CURRENT_REALM
            with _lock:
                _models_cache.pop(req_realm, None)
            try:
                live = read_dynamic_models(realm=req_realm)
            except Exception as exc:
                return self._error(502, "model refresh failed: %s" % exc)
            if not live:
                return self._json(200, {"ok": False, "realm": req_realm,
                                        "error": "upstream returned no models "
                                                 "(check account/network)"})
            _save_models_cache(req_realm, live)
            entries = merge_catalog(live, realm=req_realm)
            # 与 fetch_models 同步注册显示名 + 作废离线 memo：手动刷新后
            # 新显示名立即可解析，不用等节流窗口。
            _register_live_names(entries)
            with _lock:
                _models_cache[req_realm] = {"at": time.time(),
                                            "data": entries}
                _offline_models_cache.pop(req_realm, None)
            return self._json(200, {"ok": True, "realm": req_realm,
                                    "count": len(entries),
                                    "ids": [mid for mid, _m in entries]})
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                return
            req_realm = self._request_realm() or CURRENT_REALM
            try:
                entries = fetch_models(realm=req_realm)
            except Exception as exc:
                return self._error(502, str(exc))
            merge = str((query.get("merge_realms") or [""])[0]).lower() in (
                "1", "true", "yes", "on")
            if merge:
                # #26 步4：两区并集（**opt-in**）。默认必须保持单区视图 ——
                # 否则 _verify_models 与既有客户端会看到「多出来的」别区模型。
                merged = fetch_models_merged()
                data = [model_entry(mid, meta, realm=r)
                        for mid, meta, r in merged]
                return self._json(200, {"object": "list", "data": data,
                                        "realm": "all", "merged": True,
                                        "realms": ["cn", "intl"]})
            data = [model_entry(mid, meta, realm=req_realm)
                    for mid, meta in entries]
            try:
                catalog_source = qoder_catalog.snapshot_source(req_realm)
            except Exception as exc:   # 只读探针：失败也不能拖垮模型列表
                catalog_source = "unknown"
                log("catalog source probe failed: %s" % exc, level="WARN")
            return self._json(200, {"object": "list", "data": data,
                                    "realm": req_realm or CURRENT_REALM,
                                    "catalog_source": catalog_source})
        if path in ("/usage", "/v1/usage"):
            if not self._authorized():
                return
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or CURRENT_REALM
            return self._json(200, usage_snapshot(realm=req_realm))

        if path == "/pricing":
            # 参考价目表（跟进 wb 的「OpenRouter 价估算」）：只读，不发网络请求
            if not self._authorized():
                return
            _ov = _PRICING_OVERRIDES()
            _seen = set()
            try:
                with open(USAGE_LOG, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            _seen.add(json.loads(line).get("model") or "")
                        except Exception:
                            continue
            except Exception:
                pass
            _unpriced = sorted(m for m in _seen if m
                               and qoder_pricing.price_for(m, _ov) is None)
            return self._json(200, {
                "ok": True,
                "table": qoder_pricing.table(USAGE_DIR),
                "unpriced": _unpriced,
                "note": "参考 OpenRouter 公布价折算，仅用于估算，不能用于结算；"
                        "可在 usage/pricing-overrides.json 覆盖单价",
            })
        if path == "/accounts/credits/usage":
            # 官方积分消耗（0.4.3 客户端新增接口）：summary + 近 365 天逐日消耗
            # ?uid= 只看单个账号；不传则汇总全部账号（realm 可选过滤）
            if not self._authorized():
                return
            want_uid = (query.get("uid") or [None])[0]
            want_realm = (query.get("realm") or [None])[0]
            rows = []
            daily_sum = {}
            total = peak = 0.0
            peak_date = ""
            for acc in (POOL.accounts if POOL else []):
                if want_uid and acc.uid != want_uid:
                    continue
                if want_realm and getattr(acc, "realm", "") != want_realm:
                    continue
                try:
                    cu = acc.fetch_credit_usage()
                except Exception as exc:
                    cu = {"ok": False, "error": str(exc)[:120]}
                rows.append({
                    "uid": acc.uid,
                    "nickname": acc.nickname or acc.uid[:8],
                    "realm": getattr(acc, "realm", ""),
                    "ok": bool(cu.get("ok")),
                    "total": cu.get("total") or 0,
                    "peak": cu.get("peak") or 0,
                    "peak_date": cu.get("peak_date") or "",
                    "error": cu.get("error") or "",
                })
                if not cu.get("ok"):
                    continue
                total += float(cu.get("total") or 0)
                if float(cu.get("peak") or 0) >= peak:
                    peak = float(cu.get("peak") or 0)
                    peak_date = cu.get("peak_date") or peak_date
                for item in (cu.get("daily") or []):
                    d = item.get("date") or ""
                    if d:
                        daily_sum[d] = daily_sum.get(d, 0.0) + float(item.get("value") or 0)
            daily = [{"date": k, "value": round(v, 4)}
                     for k, v in sorted(daily_sum.items())]
            return self._json(200, {
                "accounts": rows,
                "total": round(total, 4),
                "peak": round(peak, 4),
                "peak_date": peak_date,
                "daily": daily,
                "unit": "credits",
            })
        if path == "/accounts/credits/grants":
            # 积分获取历史（跟进 wb）：只读内存里的积分快照，不发上游请求
            if not self._authorized():
                return
            rows = []
            snap_at = None
            for acc in (POOL.accounts if POOL else []):
                cred = getattr(acc, "credits", None)
                if not isinstance(cred, dict):
                    continue
                at = cred.get("updated_at")
                if at and (snap_at is None or at > snap_at):
                    snap_at = at
                for pkg in cred.get("packages") or []:
                    if not isinstance(pkg, dict):
                        continue
                    rows.append({
                        "account": (getattr(acc, "nickname", "") or
                                    (getattr(acc, "uid", "") or "")[:8]),
                        "realm": getattr(acc, "realm", "") or "",
                        "name": pkg.get("name") or "",
                        "size": pkg.get("size"),
                        "remain": pkg.get("remain"),
                        "used": pkg.get("used"),
                        "expires_at": pkg.get("expires_at"),
                        "updated_at": at,
                    })
            rows.sort(key=lambda r: (r.get("updated_at") or 0), reverse=True)
            total_size = sum(int(r["size"] or 0) for r in rows)
            total_remain = sum(int(r["remain"] or 0) for r in rows)
            return self._json(200, {
                "ok": True, "rows": rows,
                "summary": {"count": len(rows), "size": total_size,
                            "remain": total_remain,
                            "snapshot_at": snap_at},
                "note": "来自最近一次积分刷新/签到的快照，不是实时上游数据",
            })
        if path == "/accounts/activity":
            # 签到 / 领取动作流水（跟进 wb_activity）：只读本地 jsonl，不发上游请求
            if not self._authorized():
                return
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except Exception:
                limit = 200
            rows = qoder_activity.recent(USAGE_DIR, limit=limit)
            ok_count = sum(1 for r in rows if r.get("ok"))
            earned = sum(int(r.get("amount") or 0) for r in rows
                         if r.get("kind") == "checkin" and r.get("ok"))
            return self._json(200, {
                "ok": True, "rows": rows,
                "summary": {"count": len(rows), "ok": ok_count,
                            "failed": len(rows) - ok_count,
                            "earned_credits": earned},
                "note": "本机动作流水（签到/领取），30 天保留，不代表上游发放明细",
            })
        if path == "/usage/remaining":
            # 剩余用量估算（跟进 wb）：按近 24h 实际消耗速率折算可用天数
            if not self._authorized():
                return
            now = time.time()
            used = {}
            try:
                with open(USAGE_LOG, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        ts = row.get("ts") or row.get("time") or 0
                        try:
                            ts = float(ts)
                        except Exception:
                            continue
                        if now - ts > 86400:
                            continue
                        if row.get("error"):
                            continue
                        uid = row.get("account") or ""
                        slot = used.setdefault(uid[:8], {"credit": 0.0,
                                                         "tokens": 0})
                        slot["credit"] += float(row.get("credit") or 0)
                        slot["tokens"] += int(row.get("total_tokens") or 0)
            except Exception:
                pass
            out = []
            for acc in (POOL.accounts if POOL else []):
                cred = getattr(acc, "credits", None) or {}
                remain = qoder_accounts.effective_remain(cred)
                key = (getattr(acc, "uid", "") or "")[:8]
                day = used.get(key) or {"credit": 0.0, "tokens": 0}
                days = None
                if isinstance(remain, (int, float)) and day["credit"] > 0:
                    days = round(float(remain) / day["credit"], 1)
                out.append({
                    "account": getattr(acc, "nickname", "") or key,
                    "uid": key,
                    "realm": getattr(acc, "realm", "") or "",
                    "remain": remain,
                    "used_24h_credit": round(day["credit"], 2),
                    "used_24h_tokens": day["tokens"],
                    "days_left": days,
                    "unlimited": qoder_accounts.shared_unlimited(cred),
                })
            out.sort(key=lambda r: (r["days_left"] is None, r["days_left"] or 0))
            return self._json(200, {"ok": True, "rows": out,
                                    "window_hours": 24,
                                    "note": "按近 24h 消耗线性外推，仅供参考"})
        if path == "/usage/recent":
            if not self._authorized():
                return
            try:
                limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
            except ValueError:
                limit = 100
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
            except ValueError:
                page = 1
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or CURRENT_REALM
            return self._json(200, recent_usage(limit, realm=req_realm, page=page))
        if path == "/credits/probe":
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            _q = parse_qs(urlparse(self.path).query)
            return self._handle_credits_probe({
                "uid": (_q.get("uid") or [""])[0],
                "realm": (_q.get("realm") or [None])[0],
                "limit": (_q.get("limit") or [None])[0]})
        if path == "/credits/summary":
            # issue #21：只读积分汇总。**刻意不放进 _is_panel_route** ——
            # 同宿主机轮询方用 API key 即可拉取；实现内绝不触发上游查询
            # （对照 /accounts/credits 的逐号强刷，那个适合手动点击）。
            if not self._authorized():
                return
            return self._json(200, credits_summary())
        if path == "/accounts/credits":
            if not self._authorized():
                return
            # #25：只强刷 enabled 且有凭证的号 —— 禁用号/坏凭证号打上游会被
            # 回一个空账本，把本地账本再零化一次；被跳过的号在 skipped 里可区分。
            targets, skipped = _refreshable_accounts(POOL.accounts if POOL else [])
            for a in targets:
                res = a.fetch_credits()
                # issue #43：手动查询已经拿到新快照 -> 清掉待刷新，别紧接着重复查
                if (isinstance(res, dict) and res.get("ok")
                        and CREDITS_USAGE_REFRESHER is not None):
                    CREDITS_USAGE_REFRESHER.mark_manual(a.uid)
            return self._json(200, {"accounts": account_views(),
                                    "skipped": skipped,
                                    "refreshed": len(targets)})
        if path == "/accounts":
            if not self._authorized():
                return
            return self._json(200, {
                "accounts": account_views(realm=query.get("realm", [None])[0]
                                          or CURRENT_REALM),
                "storage": ACCOUNTS_DIR,
                "usable": POOL.count_ready() if POOL else 0,
            })
        if path == "/accounts/export":
            if not self._authorized():
                return
            realm = (query.get("realm") or [None])[0] or None
            if realm not in ("intl", "cn"):
                realm = None
            include_secrets = (query.get("secrets") or ["1"])[0] \
                not in ("0", "false", "no")
            uids = []
            for raw in query.get("uid") or []:
                uids.extend(part.strip() for part in str(raw).split(",")
                            if part.strip())
            if uids:
                known = {a.uid for a in (POOL.accounts if POOL else [])}
                missing = [u for u in uids if u not in known]
                if missing:
                    return self._error(404, "no such account: %s"
                                       % ", ".join(missing[:5]),
                                       "invalid_request_error")
            doc = qoder_accounts.build_export_document(
                POOL.accounts if POOL else [],
                realm=realm, include_secrets=include_secrets,
                uids=uids or None)
            if (query.get("download") or ["0"])[0] in ("1", "true", "yes"):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                if len(uids) == 1:
                    label = uids[0][:8]
                else:
                    label = realm + "-" if realm else ""
                name = "qoder-accounts-%s%s.json" % (label, stamp)
                return self._download(name, doc)
            return self._json(200, doc)
        if path == "/accounts/login/poll":
            if not self._authorized():
                return
            state = (query.get("state") or [""])[0]
            return self._json(200, POOL.poll_login(state))
        if path == "/usage/analytics":
            if not self._authorized():
                return
            # ?range=day|week|month|all|custom&since=&until=（不传 = 历史全量，
            # 输出与改动前逐字节一致；语义见 range_window）
            req_range, req_since, req_until = range_query(query)
            return self._json(200, cached_usage_report("analytics",
                range=req_range, since=req_since, until=req_until))
        if path == "/usage/stats":
            # 统计概览（新版数据看板）：KPI / Top 榜 / 热力图 / 缓存 / 24h 曲线一次取回
            if not self._authorized():
                return
            req_range, req_since, req_until = range_query(query)
            return self._json(200, cached_usage_report("stats",
                range=req_range, since=req_since, until=req_until))
        if path == "/usage/by-account":
            if not self._authorized():
                return
            req_range, req_since, req_until = range_query(query)
            return self._json(200, {"accounts": usage_by_account(
                range=req_range, since=req_since, until=req_until)})
        if path == "/usage/by-key":
            if not self._authorized():
                return
            # P1-3 收尾：按 API key 聚合（key_id 已脱敏；缺失归 "(no-key)"）。
            # 与 /usage/by-account 同源同 range —— 同一窗口下两者合计相等。
            req_range, req_since, req_until = range_query(query)
            return self._json(200, {"keys": usage_by_key(
                range=req_range, since=req_since, until=req_until)})
        if path == "/usage/perf":
            if not self._authorized():
                return
            try:
                sample = max(10, min(20000,
                                     int((query.get("sample") or ["5000"])[0])))
            except ValueError:
                sample = 5000
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or CURRENT_REALM
            req_range, req_since, req_until = range_query(query)
            return self._json(200, perf_stats(sample, realm=req_realm,
                                              range=req_range,
                                              since=req_since,
                                              until=req_until))
        if path == "/usage/timeseries":
            if not self._authorized():
                return
            # 时序图：分桶 token/积分序列（桶宽自适应，可 ?bucket= 覆盖）
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or CURRENT_REALM
            req_range, req_since, req_until = range_query(query)
            return self._json(200, usage_timeseries(
                realm=req_realm, range=req_range, since=req_since,
                until=req_until,
                bucket_seconds=(query.get("bucket") or [None])[0]))
        if path == "/usage/daily":
            if not self._authorized():
                return
            # 7 天 / 30 天堆叠柱状图：日期 × 分组（模型/区域/Key/账号）
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or CURRENT_REALM
            return self._json(200, usage_daily(
                days=(query.get("days") or ["7"])[0],
                group=(query.get("group") or ["model"])[0],
                realm=req_realm))
        if path == "/tasks":
            if not self._authorized():
                return
            import qoder_tasks
            uid = (query.get("uid") or [None])[0]
            # 账号池与积分按视图分区：?realm=cn|intl 时只列该区域账号
            realm_q = (query.get("realm") or [None])[0]
            if realm_q not in ("cn", "intl"):
                realm_q = None
            view = qoder_tasks.fetch_tasks_view(POOL, realm=realm_q, uid=uid)
            if view.get("msg") and not view.get("tasks"):
                return self._json(200, view)
            return self._json(200, view)
        if path == "/scheduler":
            if not self._authorized():
                return
            return self._json(200, SCHEDULER.status() if SCHEDULER
                              else {"enabled": False, "msg": "未运行"})
        if path == "/settings":
            if not self._authorized():
                return
            return self._json(200, runtime_settings_view())
        if path == "/logs":
            if not self._authorized():
                return
            try:
                limit = int(query.get("limit", ["200"])[0])
            except (ValueError, TypeError):
                limit = 200
            level = query.get("level", [""])[0]
            tag = query.get("tag", [""])[0]
            search = query.get("search", [""])[0]
            try:
                since_id = int(query.get("since_id", ["0"])[0])
            except (ValueError, TypeError):
                since_id = 0
            return self._json(200, get_logs(limit=limit, level=level, tag=tag,
                                            search=search, since_id=since_id))
        if path == "/logs/export":
            if not self._authorized():
                return
            log_data = get_logs(limit=5000)
            lines = ["[%s] [%s] [%s] %s" % (item["ts"], item["level"],
                                             item["tag"], item["msg"])
                     for item in log_data["logs"]]
            text_content = "\n".join(lines).encode("utf-8")
            filename = "qd-proxy-%s.log" % time.strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
            self.send_header("Content-Length", str(len(text_content)))
            if cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(text_content)
            return
        if path == "/settings/reveal":
            # 看板只画掩码 Key，复制明文需要显式请求；必须面板会话。
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            if qoder_settings.panel_password_is_default(ACCOUNTS_DIR):
                # 默认密码 = 局域网内任何人都能登录；明文 Key 不能这样交出去。
                return self._error(
                    403,
                    "面板仍在使用默认密码，局域网内任何人都能登录后读到明文 API "
                    "Key。请先在「设置 → 面板密码」修改密码，再查看明文 Key。",
                    "permission_error")
            wanted = (query.get("id") or [""])[0]
            for entry in configured_keys():
                if entry.get("id") == wanted:
                    return self._json(200, {"id": wanted,
                                            "key": entry.get("key") or ""})
            return self._error(404, "no such key", "invalid_request_error")
        return self._unknown_path_error(path)

    def _dashboard(self):
        # >>> hub-patch: dashboard-redirect（docs/merge/patches 里可复查；S04/S05 只允许这一段差异）
        # 融合模式：hub/server.py 会设 HUB_PORTAL_URL —— 根路径直接跳到控制台，
        # 避免"三个端口三张脸"。没设时保持原行为（单引擎独立跑 / 上游测试）。
        _portal = os.environ.get("HUB_PORTAL_URL", "").strip()
        if _portal:
            self.send_response(302)
            self.send_header("Location", _portal)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        # <<< hub-patch: dashboard-redirect
        try:
            with open(DASHBOARD_HTML, "rb") as fh:
                body = fh.read()
        except Exception as exc:
            return self._error(500, "dashboard.html unavailable: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- locale packs ----------------------------------------------------
    # i18n/<code>.json is the whole registry: the folder listing decides which
    # languages the panel offers, so a new locale is a file drop, not a code
    # change. Files are re-read per request (like _dashboard) so a dropped or
    # edited pack takes effect without restarting the gateway.

    @staticmethod
    def _i18n_tiers_ok(data):
        """True when every tier is an object of string -> string.

        The frontend's load() applies the same rule, but validating only there
        would let a broken pack show up in the language dropdown and then do
        nothing when picked — a silent no-op is the worst failure shape for a
        feature whose whole promise is "missing key falls back quietly".
        Rejecting here makes a malformed pack invisible, which is consistent.
        """
        for tier in ("messages", "patterns", "fragments"):
            value = data.get(tier)
            if value is None:
                continue
            if not isinstance(value, dict):
                return False
            for key, item in value.items():
                if not isinstance(item, str):
                    return False
        return True

    @staticmethod
    def _i18n_read(name):
        """Return (abs_path, body_bytes, parsed_dict) for a locale pack.

        Raises ValueError for anything that is not a safe, well-formed pack:
        the name is checked against the allowlist *before* the filesystem is
        touched, and the resolved path must still live inside I18N_DIR so a
        symlink planted in the folder cannot expose accounts/ or usage data.
        """
        if not name or not I18N_FILE_RE.match(name):
            raise ValueError("invalid locale name")
        abs_path = os.path.realpath(os.path.join(I18N_DIR, name))
        root = os.path.realpath(I18N_DIR)
        if os.path.dirname(abs_path) != root:
            raise ValueError("locale path escapes the i18n folder")
        try:
            with open(abs_path, "rb") as fh:
                body = fh.read()
        except Exception:
            raise ValueError("locale pack unreadable")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            raise ValueError("locale pack is not valid JSON")
        if not isinstance(data, dict):
            raise ValueError("locale pack must be a JSON object")
        if not Handler._i18n_tiers_ok(data):
            raise ValueError("locale tiers must be string->string objects")
        return abs_path, body, data

    def _i18n_manifest(self):
        packs = []
        try:
            names = sorted(os.listdir(I18N_DIR))
        except Exception:
            names = []       # no i18n/ folder yet is a normal state, not an error
        for name in names:
            if not I18N_FILE_RE.match(name):
                continue
            try:
                _, _, data = self._i18n_read(name)
            except ValueError as exc:
                log("i18n: skipping %s (%s)" % (name, exc))
                continue
            meta = data.get("@locale")
            meta = dict(meta) if isinstance(meta, dict) else {}
            # The filename is the authority on the code: a pack that claims to
            # be "en" while filed as de.json would otherwise be fetched by a
            # name that does not match what the panel then loads.
            meta["code"] = name[:-len(".json")]
            meta.setdefault("name", meta["code"])
            meta.setdefault("order", 100)
            meta.setdefault("dir", "ltr")
            try:
                meta["order"] = int(meta["order"])
            except Exception:
                meta["order"] = 100
            packs.append(meta)
        packs.sort(key=lambda p: (p.get("order", 100), p.get("code", "")))
        return self._json(200, {"default": "zh", "packs": packs})

    def _i18n_pack(self, name):
        try:
            _, body, _ = self._i18n_read(name)
        except ValueError:
            # 404, never the exception text: this body is rendered into a toast.
            return self._error(404, "no such locale pack",
                               "invalid_request_error")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_chunked_body(self, max_bytes=MAX_PAYLOAD_BYTES):
        """Decode a Transfer-Encoding: chunked request body into bytes.

        Some clients (Claude Code / undici in particular) stream large
        /v1/messages bodies with chunked encoding instead of Content-Length.
        Reading only Content-Length made the payload look empty, so the
        Anthropic bridge answered "messages is required" and the unread chunk
        frames were parsed as bogus HTTP requests on the keep-alive socket.
        """
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            size_field = line.split(b";", 1)[0].strip()
            if not size_field:
                continue
            try:
                size = int(size_field, 16)
            except ValueError:
                raise BadJSON()
            if size == 0:
                # Consume optional trailers up to the terminating blank line.
                while True:
                    trailer = self.rfile.readline(65536)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > max_bytes:
                # The current chunk has not been read; close rather than
                # parsing its data as another chunk header.
                self.close_connection = True
                raise BodyTooLarge(total)
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(remaining, 65536))
                if not data:
                    raise BadJSON()
                chunks.append(data)
                remaining -= len(data)
            self.rfile.read(2)  # CRLF after the chunk data
        return b"".join(chunks)

    def _read_payload(self, max_bytes=MAX_PAYLOAD_BYTES, allow_list=False):
        """Parse the request body into a dict (or a list when allow_list)."""
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            try:
                raw = self._read_chunked_body(max_bytes=max_bytes)
                data = json.loads(raw.decode("utf-8", "replace") or "{}")
            except (BodyTooLarge, BadJSON):
                raise
            except Exception:
                raise BadJSON()
            if isinstance(data, dict):
                return data
            if allow_list and isinstance(data, list):
                return data
            return {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length > max_bytes:
            raise BodyTooLarge(length)
        if length < 0:
            raise BadJSON()
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(raw or "{}")
        except Exception:
            raise BadJSON()
        if isinstance(data, dict):
            return data
        if allow_list and isinstance(data, list):
            return data
        return {}

    def _payload_or_error(self, allow_list=False):
        """Read the body, replying with the right error and returning None."""
        try:
            return self._read_payload(allow_list=allow_list)
        except BodyTooLarge as exc:
            self._error(413, "payload too large (%d bytes > %d limit)"
                        % (exc.length, MAX_PAYLOAD_BYTES),
                        "invalid_request_error")
            return None
        except BadJSON:
            self._error(400, "invalid JSON body", "invalid_request_error")
            return None

    def _handle_settings_save(self):
        """Persist panel-managed settings from the web settings tab."""
        payload = self._payload_or_error()
        if payload is None:
            return
        reply = {}
        if "api_keys" in payload:
            raw = payload.get("api_keys")
            if not isinstance(raw, list):
                return self._error(400, "api_keys must be a list",
                                   "invalid_request_error")
            # 面板只画掩码 Key：空值表示“保留原值”，不是“清空”。
            existing = {entry.get("id"): entry for entry in configured_keys()}
            cleaned = []
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    return self._error(400, "each api key must be an object",
                                       "invalid_request_error")
                entry_id = str(item.get("id") or "").strip()
                value = str(item.get("key") or "").strip()
                if not value and entry_id and entry_id in existing:
                    value = existing[entry_id].get("key") or ""
                if not entry_id:
                    entry_id = "k%d" % index
                if value and len(value) < 4:
                    return self._error(400,
                                       "api key must be at least 4 characters",
                                       "invalid_request_error")
                if not value:
                    return self._error(400,
                                       "a key entry is empty - fill it in or "
                                       "remove the row",
                                       "invalid_request_error")
                realm = str(item.get("realm") or "").strip().lower()
                if realm not in ("", "intl", "cn"):
                    return self._error(400, "realm must be intl, cn or empty",
                                       "invalid_request_error")
                created_at = item.get("created_at") \
                    or (existing.get(entry_id, {}).get("created_at")
                        if entry_id in existing else None) \
                    or time.strftime("%Y/%m/%d %H:%M")
                # issue #131-1：区分「字段没提交」与「显式提交空列表」。
                # 老客户端/精简载荷不含 models 时保留旧白名单，只有明确写
                # [] 才表示「清除限制」——否则一次改名就能把授权范围放大。
                _prev = existing.get(entry_id) or {}
                if "models" in item:
                    models_val = item.get("models")
                else:
                    models_val = _prev.get("models")
                note_val = (str(item.get("note")).strip()
                            if "note" in item else (_prev.get("note") or ""))
                cleaned.append({
                    "id": entry_id,
                    "name": str(item.get("name") or "").strip(),
                    "key": value,
                    "realm": realm,
                    "enabled": item.get("enabled", True) is not False,
                    "created_at": created_at,
                    # P1-4：每 Key 模型白名单（规范化统一在
                    # qoder_settings._clean_key_entry 一处完成）。
                    "models": models_val,
                    "note": note_val,
                    "anthropic_thinking": (item.get("anthropic_thinking", _prev.get("anthropic_thinking") or "")),
                    "account_ids": item.get("account_ids", _prev.get("account_ids") or []),
                    "account_groups": item.get("account_groups", _prev.get("account_groups") or []),
                })
            try:
                qoder_settings.set_api_keys(ACCOUNTS_DIR, cleaned)
            except ValueError as error:
                return self._error(400, str(error), "invalid_request_error")
            reply["api_keys_saved"] = len(cleaned)
        if "anthropic_thinking" in payload:
            # issue #146：全局 Anthropic thinking 输出模式（empty/off/空串回退）
            reply["anthropic_thinking"] = qoder_settings.set_anthropic_thinking(
                ACCOUNTS_DIR, payload.get("anthropic_thinking"))
        if "banned_models" in payload:
            # P1-4：全局封禁列表（逗号/分号/换行分隔均可，清洗在 qoder_settings）。
            reply["banned_models"] = qoder_settings.set_banned_models(
                ACCOUNTS_DIR, payload.get("banned_models"))
        if "auth_disabled" in payload:
            qoder_settings.set_auth_disabled(ACCOUNTS_DIR,
                                             payload.get("auth_disabled"))
            reply["auth_disabled"] = bool(payload.get("auth_disabled"))
        new_key = payload.get("api_key")
        if new_key is not None:
            new_key = str(new_key).strip()
            if new_key and len(new_key) < 4:
                return self._error(400, "api key must be at least 4 characters",
                                   "invalid_request_error")
            global API_KEY, API_KEY_FILE_SET
            qoder_settings.set_api_key(ACCOUNTS_DIR, new_key)
            API_KEY = new_key
            API_KEY_FILE_SET = True
            reply["api_key_set"] = bool(new_key)
        if "limits" in payload:
            raw_limits = payload.get("limits")
            if not isinstance(raw_limits, dict):
                return self._error(400, "limits must be an object",
                                   "invalid_request_error")
            # 先全量校验、后落盘：避免一半合法一半 400 时留下部分应用。
            updates = []
            for key, scopes in raw_limits.items():
                if key not in qoder_settings.LIMIT_KEYS:
                    return self._error(400, "unknown limit: %s" % key,
                                       "invalid_request_error")
                if not isinstance(scopes, dict):
                    return self._error(400, "limits.%s must be an object" % key,
                                       "invalid_request_error")
                for scope, value in scopes.items():
                    if scope not in qoder_settings.LIMIT_SCOPES:
                        return self._error(400, "unknown scope: %s" % scope,
                                           "invalid_request_error")
                    updates.append((key, scope, value))
            for key, scope, value in updates:
                qoder_settings.set_limit(ACCOUNTS_DIR, key, scope, value)
            reply["limits"] = qoder_settings.limits_data(ACCOUNTS_DIR)
            # task-61：保存后立刻推给池（阈值 + free 名单同批），让配置真正生效；
            # 推完若某个阈值 > 0，池会自行惰性启动后台余额刷新线程。
            try:
                POOL.apply_settings(limits_data=reply["limits"],
                                    free_models=free_model_names(CURRENT_REALM))
            except Exception as exc:
                reply["limits_apply_error"] = str(exc)
                log("quota limits apply failed after save: %s" % exc,
                    level="WARN")
        if payload.get("restart_scheduler"):
            if SCHEDULER:
                SCHEDULER.stop()
                SCHEDULER.start()
            reply["scheduler"] = "restarted"
        reply.update(runtime_settings_view())
        return self._json(200, reply)

    def _handle_panel(self, path):
        """Panel login, logout and the settings screen (password)."""
        payload = self._payload_or_error()
        if payload is None:
            return
        if path == "/panel/login":
            client_ip = self.client_address[0] \
                if hasattr(self, "client_address") and self.client_address \
                else "127.0.0.1"
            now = time.time()
            with _login_lock:
                _prune_login_attempts(now)
                attempts = [t for t in _login_attempts.get(client_ip, [])
                            if now - t < 60]
                _login_attempts[client_ip] = attempts
                if len(attempts) >= 5:
                    wait_sec = int(60 - (now - attempts[0]))
                    return self._error(429,
                                       "too many login attempts, please wait %ds"
                                       % max(1, wait_sec),
                                       "rate_limit_error")
            password = str(payload.get("password") or "")
            if not qoder_settings.verify_panel_password(ACCOUNTS_DIR, password):
                with _login_lock:
                    _login_attempts.setdefault(client_ip, []).append(now)
                time.sleep(0.5)   # 撞库缓解
                return self._error(401, "invalid panel password",
                                   "invalid_request_error")
            with _login_lock:
                _login_attempts.pop(client_ip, None)
            token = PANEL.create()
            return self._json(200, {
                "ok": True,
                "token": token,
                "using_default_password":
                    qoder_settings.panel_password_is_default(ACCOUNTS_DIR),
            })
        if path == "/panel/logout":
            # issue #131-5：先认证再吊销。revoke_all() 会换签名密钥，等于把
            # **所有**已登录浏览器踢下线；此前它在 _panel_ok() 之前执行，任
            # 何能连到端口的人不带任何凭据就能做到这件事。
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            # 退出登录不能只往内存黑名单里写一笔：那份 dict 重启即丢，已退出的
            # token 会活到 TTL（7 天）结束——比改动前的「重启全清」更差。
            # 直接换密钥：本 token 与其它浏览器的会话一起作废。本地单人面板无感，
            # 多人/局域网场景反而更安全。
            PANEL.revoke_all()
            return self._json(200, {"ok": True})
        # 之后所有路由都要面板会话
        if not self._panel_ok():
            return self._error(401, "panel password required",
                               "invalid_request_error")
        if path == "/panel/password":
            current = str(payload.get("current") or "")
            new = str(payload.get("new") or "")
            if not qoder_settings.verify_panel_password(ACCOUNTS_DIR, current):
                # issue #41：原密码错是**业务校验失败**，不是会话失效。
                # 401 会让前端弹回登录遮罩（用户看到「登录 UI」而不是原因）；
                # 403 保持「已登录但不允许」的语义，前端直接显示 msg。
                return self._error(403, "当前密码不正确",
                                   "invalid_request_error")
            if len(new) < 4:
                return self._error(400, "new password must be at least 4 characters",
                                   "invalid_request_error")
            qoder_settings.set_panel_password(ACCOUNTS_DIR, new)
            if new != qoder_settings.DEFAULT_PANEL_PASSWORD:
                # 换密码使其它浏览器会话全部失效
                PANEL.revoke_all()
            token = PANEL.create()
            return self._json(200, {"ok": True, "token": token})
        return self._unknown_path_error(path)

    # issue #121：/diag/model-billing 已移除——上游对网关身份一律回报 "auto"，    # 该字段不携带计费信息，据此提示用户只会误导（用户实测国际版同样如此）。
    def _admin_account_views(self):
        return [a.public() for a in (POOL.accounts if POOL else [])]

    def _handle_admin_get(self, path, query):
        accounts = list(POOL.accounts) if POOL else []
        if path == "/admin/overview":
            stamp = _usage_log_fingerprint()
            metadata = tuple((a.uid, a.nickname, getattr(a, "group", ""), a.realm,
                              bool(a.enabled), bool(a.access_token), a.cooldown_until,
                              a.expires_at) for a in accounts)
            now = time.time()
            key = ("admin-overview", stamp, int(now//10), metadata)
            result = _USAGE_REPORT_CACHE.get(key, lambda: qoder_admin.build_overview(
                _iter_usage_rows_forward(now-6*3600-600), accounts, now=now), ttl=10,
                valid=lambda: _usage_log_fingerprint() == stamp)
            result.update(version=VERSION, realm=CURRENT_REALM,
                          keys=sum(k.get("enabled", True) is not False for k in configured_keys()),
                          models=len({qoder_catalog.display_id(m) for r in ("cn", "intl")
                                      for m in qoder_catalog.models_for_realm(r)}),
                          routing_rules=len(qoder_routing.load_rules(ACCOUNTS_DIR)),
                          log_dir=LOG_DIR, uptime_seconds=max(0, int(now-STARTED_AT)))
            return self._json(200, result)
        if path == "/admin/groups":
            return self._json(200, {"ok": True, "groups": qoder_admin.groups_view(accounts),
                                    "accounts": self._admin_account_views()})
        if path == "/admin/routing":
            try:
                config = qoder_routing.load_config(ACCOUNTS_DIR)
            except ValueError as error:
                return self._error(500, str(error), "configuration_error")
            return self._json(200, dict(config, ok=True,
                                    models=[{"id":mid,"realm":r} for r in ("cn","intl") for mid in qoder_catalog.display_names(r)],
                                    strategies=list(qoder_routing.STRATEGIES),
                                    groups=qoder_admin.groups_view(accounts),
                                    accounts=self._admin_account_views()))
        if path == "/admin/jobs":
            value = _ADMIN_JOBS.get((query.get("id") or [""])[0])
            if not value:
                return self._error(404, "job not found", "invalid_request_error")
            return self._json(200, {"ok": True, "job": value})
        return self._unknown_path_error(path)

    def _admin_test_account(self, account):
        model = _default_test_model(account.realm)
        started = time.time()
        try:
            response, used, _ = open_upstream({"model": model, "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}]},
                target_realm=account.realm, only_account=account)
            with response:
                obj = aggregate_stream(response, model, holder={"usage": None})
            record_usage(model, obj.get("usage"), stream=False,
                         account=used.uid, key_id="panel-group-test",
                         elapsed_ms=int((time.time()-started)*1000))
            return {"ok": True, "model": model, "elapsed_ms": int((time.time()-started)*1000)}
        except Exception as error:
            detail = str(getattr(error, "detail", "") or getattr(error, "qoder_detail", "") or error)
            return {"ok": False, "model": model, "error": detail[:300], "hint": _quota_hint(detail)}

    def _handle_admin(self, path, payload):
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object", "invalid_request_error")
        if path == "/admin/routing/save":
            try:
                config = qoder_routing.save_config(ACCOUNTS_DIR,
                    rules=payload.get("rules"), tiers=payload.get("tiers"),
                    policy=payload.get("policy"), expected_revision=payload.get("revision"))
            except ValueError as error:
                return self._error(400, str(error), "invalid_request_error")
            return self._json(200, dict(config, ok=True))
        if path == "/admin/routing/preview":
            key_id = str(payload.get("key_id") or "")
            key = next((k for k in configured_keys() if k.get("id") == key_id), None)
            if key_id and key is None:
                return self._error(404, "API Key not found", "invalid_request_error")
            try:
                result = qoder_routing.preview(ACCOUNTS_DIR, list(POOL.accounts) if POOL else [],
                    str(payload.get("model") or "auto"), key=key,
                    realm=(payload.get("realm") or (key or {}).get("realm")
                           or exclusive_realm(str(payload.get("model") or "auto"))),
                    fallback_realm=detect_model_realm(str(payload.get("model") or "auto")) or CURRENT_REALM,
                    config=({"rules": qoder_routing.normalize_rules(payload["draft"].get("rules", [])),
                             "tiers": qoder_routing.normalize_tiers(payload["draft"].get("tiers", [])),
                             "policy": qoder_routing.normalize_policy(payload["draft"].get("policy")),
                             "configured": True, "board_enabled": True} if isinstance(payload.get("draft"), dict) else None))
            except ValueError as error:
                return self._error(400, str(error), "invalid_request_error")
            return self._json(200, result)
        if POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/admin/accounts/group":
            uids = payload.get("uids")
            if not isinstance(uids, list) or not uids or len(uids) > 500:
                return self._error(400, "uids must contain 1..500 account ids", "invalid_request_error")
            group = payload.get("group", "")
            if not isinstance(group, str) or len(group.strip()) > 64 or any(ord(c) < 32 for c in group):
                return self._error(400, "invalid account group", "invalid_request_error")
            targets = [POOL.get(str(uid)) for uid in dict.fromkeys(uids)]
            if any(a is None for a in targets):
                return self._error(404, "account not found", "invalid_request_error")
            for account in targets:
                account.group = group.strip()
                account.save(ACCOUNTS_DIR)
            return self._json(200, {"ok": True, "changed": len(targets),
                                    "accounts": self._admin_account_views()})
        if path == "/admin/groups/action":
            if "group" not in payload or not isinstance(payload["group"], str):
                return self._error(400, "group is required", "invalid_request_error")
            group = payload["group"].strip()
            action = str(payload.get("action") or "")
            if action not in ("enable", "disable", "refresh", "test", "checkin", "credits"):
                return self._error(400, "unsupported group action", "invalid_request_error")
            targets = [a for a in list(POOL.accounts) if getattr(a, "group", "") == group]
            if not targets:
                return self._error(404, "account group is empty", "invalid_request_error")
            if action in ("enable", "disable"):
                for account in targets:
                    account.enabled = action == "enable"
                    account.save(ACCOUNTS_DIR)
                return self._json(200, {"ok": True, "action": action, "group": group,
                    "results": [{"uid": a.uid, "ok": True} for a in targets],
                    "accounts": self._admin_account_views()})
            # Snapshot only the requested group; jobs never fall back to the whole pool.
            def worker(account):
                if action == "test":
                    return self._admin_test_account(account)
                if action == "refresh":
                    ok = account.refresh()
                    account.save(ACCOUNTS_DIR)
                    return {"ok": bool(ok), "error": account.last_error if not ok else ""}
                if action == "credits":
                    return account.fetch_credits()
                import qoder_tasks
                return qoder_tasks.run_checkin(account, gap=0)
            try:
                job_id = _ADMIN_JOBS.submit(action, group, targets, worker)
            except RuntimeError as error:
                return self._error(409, str(error), "conflict")
            return self._json(202, {"ok": True, "action": action, "group": group,
                                    "job_id": job_id, "job": _ADMIN_JOBS.get(job_id)})
        return self._unknown_path_error(path)

    def _handle_credits_probe(self, payload):
        # issue #141：额度端点自动探测（主机 × 路径矩阵）。
        # 用于确认某个区域到底哪个端点有数据（INTL 的 /api/v2/quota/usage
        # 经常 404/403）。只读：不改账号，只写探测结果缓存。
        if not self._panel_ok():
            return self._error(401, "panel password required",
                               "invalid_request_error")
        uid = str((payload or {}).get("uid") or "").strip()
        realm = (payload or {}).get("realm") or None
        limit = (payload or {}).get("limit")
        try:
            limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit = None
        accs = []
        if uid and POOL:
            a = POOL.get(uid)
            if a is not None:
                accs = [a]
        elif POOL:
            accs = [a for a in POOL.accounts
                    if (not realm or a.realm == realm)][:3]
        if not accs:
            return self._error(404, "no account to probe",
                               "invalid_request_error")
        results = []
        for a in accs:
            try:
                res = a.probe_credits_endpoints(ACCOUNTS_DIR, limit=limit)
            except Exception as exc:
                res = {"ok": False, "error": str(exc)[:160], "rows": []}
            results.append({"uid": a.uid, "nickname": a.nickname,
                            "realm": a.realm, **res})
        return self._json(200, {"ok": any(r.get("ok") for r in results),
                                "results": results})

    def _handle_diag_queue(self, payload):
        # issue #139：上游排队（10605）诊断——一次最小探测，把业务码、
        # retryAfterSeconds、queueCount 与机器身份来源一起回显。
        # 排队字段只作观测，不据此断言容量、账号或身份被针对。
        model = str((payload or {}).get("model") or "").strip() or "auto"
        realm = (payload or {}).get("realm") or self._request_realm()
        uid = str((payload or {}).get("uid") or "").strip()
        acc = POOL.get(uid) if uid and POOL else None
        if uid and acc is None:
            return self._error(404, "no such account")
        if realm is not None and realm not in ("cn", "intl"):
            return self._error(400, "invalid realm")
        if acc is not None and realm is not None and acc.realm != realm:
            return self._error(400, "account realm does not match requested realm")
        probe = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 8,
        }
        out = {"ok": True, "model": model, "realm": realm,
               "account": (acc.uid[:8] if acc else None),
               "code": None, "kind": "", "retry_after": None,
               "queue_count": None, "queue_type": "unknown",
               "service_available": None, "reason": "unknown",
               "detail": "", "hint": ""}
        t0 = time.time()
        probe_ctx = {"_queue_probe_once": True}
        try:
            resp, used, _ = open_upstream(
                probe, target_realm=realm,
                only_account=acc if acc else None,
                usage_ctx=probe_ctx)
            out["account"] = used.uid[:8] if used else out["account"]
            out["realm"] = used.realm if used else realm
            acc = used or acc
            try:
                holder = {"_queue_probe_once": True}
                aggregate_stream(resp, model, holder=holder)  # HTTP 200 is not success
                if not holder.get("_upstream_finished"):
                    raise UpstreamStatus(502, "upstream ended without a completed response")
            finally:
                resp.close()
            out["code"] = 200
            out["kind"] = "ok"
            out["account"] = used.uid[:8] if used else out["account"]
        except UpstreamStatus as exc:
            acc = probe_ctx.get("_queue_probe_account") or acc
            if acc is not None:
                out["account"], out["realm"] = acc.uid[:8], acc.realm
            detail = str(getattr(exc, "detail", "") or "")
            out["code"] = exc.status
            out["kind"] = "error"
            out["detail"] = detail[:400]
            out["hint"] = _quota_hint(detail)
            if "10605" in detail or "isQueued" in detail:
                out["kind"] = "queued"
            elif _quota_cooldown(detail)[0]:
                out["kind"] = _quota_cooldown(detail)[0]
            m = re.search(r"retryAfterSeconds\D+(\d+)", detail)
            if m:
                out["retry_after"] = int(m.group(1))
            mc = re.search(r"queueCount\D+(\d+)", detail)
            if mc:
                out["queue_count"] = int(mc.group(1))
        except Exception as exc:
            out["code"] = 0
            out["kind"] = "error"
            out["detail"] = str(exc)[:200]
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        # 机器身份来源：native（官方桥）还是 derived（派生值）。
        # 上游对「陌生设备」可能更倾向排队，这个字段能帮用户判断。
        try:
            _realm = realm if realm in ("cn", "intl") else CURRENT_REALM
            _native = qoder_accounts.native_machine_identity(
                _realm, acc.uid if acc else "")
            out["identity_source"] = "native" if _native else "derived"
        except Exception:
            out["identity_source"] = "unknown"
        out["queue_observation"] = queue_observation_snapshot()
        if out["kind"] == "queued":
            fields = queue_fields(detail) or {}
            out.update(fields)
            out["detail"] = ""  # never expose arbitrary provider body in queue telemetry
            _cnt = out.get("queue_count")
            out["advice"] = ("上游排队原因未确认；p4 与 serviceAvailable:true 不足以确认风控或容量。"
                             "请按 retryAfterSeconds 退避并避免无限重试，使用官方客户端做同账号同时间对照，"
                             "必要时向上游支持确认。QD_QUEUE_WAIT_MODE=fast 仅改变本地等待策略，"
                             "初始排队快速返回 503，不会解除限制。")
            if _cnt:
                out["advice"] += " 当前队列长度约 %d。" % _cnt
        elif out["kind"] in ("exhausted", "daily"):
            out["advice"] = ("上游模型额度/套餐权限或次数限制，不等于总余额耗尽；"
                             "先核对实际账号、区域、模型和额度更新时间，再决定是否充值或换号。")
        return self._json(200, out)

    def _handle_diag_checkin(self, payload):
        """issue #123：签到诊断——把每一步的上游结果原样吐出来，便于定位。

        只读：不发 claim（不会误领/重复领）。回显内容不含 token，只给状态与
        活动 key/标题，够用来比对"官方客户端能领、网关看不到"的差异。
        """
        uid = str((payload or {}).get("uid") or "").strip()
        acc = POOL.get(uid) if uid else None
        if acc is None:
            return self._error(404, "no such account: %s" % (uid or "-"))
        out = {"ok": True, "uid": acc.uid[:8], "realm": acc.realm,
               "enabled": bool(acc.enabled),
               "capability": None, "capability_reason": "",
               "status": None, "campaigns": None, "can_checkin": None,
               "last_checkin": acc.last_checkin}
        try:
            cap, reason = acc.checkin_capability()
            out["capability"] = cap
            out["capability_reason"] = reason
        except Exception as exc:
            out["capability_reason"] = "probe failed: %s" % str(exc)[:120]
        try:
            ok, st = acc.checkin_status()
            out["status"] = {"ok": ok, "detail": st}
        except Exception as exc:
            out["status"] = {"ok": False, "error": str(exc)[:200]}
        try:
            out["can_checkin"] = bool(acc.can_checkin())
        except Exception:
            pass
        try:
            res = acc.campaigns()
            items = res if isinstance(res, list) else (
                (res or {}).get("campaigns") or (res or {}).get("list")
                or (res or {}).get("data") or [])
            if not isinstance(items, list):
                items = []
            out["campaigns"] = {
                "count": len(items),
                # 字段名各区域不同（campaignId/id/title/name/...），这里把
                # 标量字段原样列出来，比猜字段名对定位更有用；token 不在活动
                # 对象里，回显安全。
                "items": [{"keys": sorted(c.keys())[:12],
                           "scalars": {k: v for k, v in c.items()
                                        if isinstance(v, (str, int, bool))}
                           if isinstance(c, dict) else {},
                           "label": qoder_accounts.campaign_label(c)}
                          for c in items[:8] if isinstance(c, dict)],
            }
        except Exception as exc:
            out["campaigns"] = {"error": str(exc)[:200]}
        return self._json(200, out)


    def _handle_accounts(self, path, payload):
        """Account-management endpoints (dashboard uses these)."""
        if POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/accounts/import" and isinstance(payload, list):
            payload = {"data": payload}
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object",
                               "invalid_request_error")
        if path in ("/accounts/credits", "/accounts/credits/fetch"):
            uid = payload.get("uid")
            realm = payload.get("realm")
            if uid:
                targets = [POOL.get(uid)]
            elif realm and realm != "all":
                targets = [a for a in POOL.accounts if a.realm == realm]
            else:
                targets = list(POOL.accounts)
            # #25：与 GET 同一条守卫（显式 uid 指向禁用号时也会落到 skipped）
            targets, skipped = _refreshable_accounts(targets)
            results = []
            for account in targets:
                res = account.fetch_credits()
                account.fetch_plan()
                results.append({"uid": account.uid, "ok": res.get("ok", False),
                                "credits": account.credits,
                                "plan": account.plan,
                                "error": res.get("error", "")})
            return self._json(200, {"results": results, "skipped": skipped,
                                    "accounts": account_views()})
        if path == "/tasks/run":
            import qoder_tasks
            if not POOL:
                return self._json(200, {"ok": False, "msg": "账号池不可用"})
            uid = payload.get("uid")
            if uid and uid != "all":
                target = POOL.get(uid)
                if not target:
                    return self._json(200, {"ok": False,
                                            "msg": "未找到指定的账号"})
                targets = [target]
            else:
                # 不再按区域过滤：签到能力运行时探测，接口不存在的账号会在
                # 日志里给出明确原因（历史问题：国际版点签到完全没反应）。
                # issue #102：签到与「是否接推理」解耦——禁用但开了
                # checkinWhenDisabled 的账号仍参与（凭证缺失的仍排除）
                targets = [a for a in POOL.accounts if a.checkin_eligible()]
            if not targets:
                return self._json(200, {"ok": False, "msg": "未找到可用账号（账号已禁用或缺少凭证）"})
            res = qoder_tasks.run_batch_checkin(targets, gap=1.0)
            qoder_tasks.invalidate_panel_cache()   # 写操作后失效面板短缓存
            return self._json(200, {
                "ok": res["ok"],
                "credit_added": res["credit_added"],
                "logs": res["logs"],
                "accounts_count": res["accounts_count"],
            })
        if path == "/tasks/travel":
            # 看板福利按钮的对应动作：批量领取 Pro 福利包（官方仅国内版 sash 活动）
            import qoder_tasks
            if not POOL:
                return self._json(200, {"ok": False, "msg": "账号池不可用"})
            uid = payload.get("uid")
            if uid and uid != "all":
                target = POOL.get(uid)
                if not target:
                    return self._json(200, {"ok": False,
                                            "msg": "未找到指定的账号"})
                targets = [target]
            else:
                # issue #102：签到与「是否接推理」解耦——禁用但开了
                # checkinWhenDisabled 的账号仍参与（凭证缺失的仍排除）
                targets = [a for a in POOL.accounts if a.checkin_eligible()]
            if not targets:
                return self._json(200, {"ok": False, "msg": "未找到可用账号（账号已禁用或缺少凭证）"})
            res = qoder_tasks.run_batch_pro_claim(targets)
            qoder_tasks.invalidate_panel_cache()   # 写操作后失效面板短缓存
            return self._json(200, {
                "ok": True,
                "results": res["results"],
                "msg": res["msg"],
                "logs": res["logs"],
                "accounts_count": res["accounts_count"],
            })
        if path == "/scheduler/config":
            # issue #70 后续：/scheduler/* 走的是账号路由前缀（is_account_route），
            # 端点必须在这里，否则会被兜底成「unknown account endpoint」。
            if not SCHEDULER:
                return self._error(503, "scheduler not running",
                                   "invalid_request_error")
            try:
                st = SCHEDULER.set_schedule(
                    checkin_times=(payload or {}).get("checkin_times"),
                    keepalive_times=(payload or {}).get("keepalive_times"),
                    cron=(payload or {}).get("cron"),
                    jitter_secs=(payload or {}).get("jitter_secs"))
            except Exception as exc:
                return self._error(400, "schedule update failed: %s" % exc,
                                   "invalid_request_error")
            return self._json(200, {"ok": True, "scheduler": st})
        if path == "/scheduler/trigger":
            if SCHEDULER:
                return self._json(200, SCHEDULER.trigger_now())
            return self._json(200, {"ok": False, "msg": "调度器未初始化"})
        if path == "/scheduler/toggle":
            if SCHEDULER:
                SCHEDULER.enabled = not SCHEDULER.enabled
                SCHEDULER.log("用户切换调度器状态为: %s"
                              % ("启用" if SCHEDULER.enabled else "暂停"))
                return self._json(200, SCHEDULER.status())
            return self._json(200, {"ok": False, "msg": "调度器未初始化"})
        if path == "/logs/clear":
            clear_logs()
            return self._json(200, {"ok": True})
        if path == "/realm":
            new_realm = payload.get("realm")
            if new_realm in ("intl", "cn"):
                save_persisted_realm(new_realm)
            return self._json(200, {"ok": True, "current": CURRENT_REALM,
                                    "persisted": True})
        if path == "/accounts/checkin-when-disabled":
            # issue #102：禁用只表示不接推理流量；允许该账号在禁用期间照常签到
            uid = payload.get("uid")
            target = POOL.get(uid) if uid else None
            if not target:
                return self._json(200, {"ok": False, "msg": "未找到指定的账号"})
            target.checkin_when_disabled = bool(payload.get("value"))
            if target.path:
                target.save(os.path.dirname(target.path))
            return self._json(200, {"ok": True, "uid": target.uid,
                                    "checkinWhenDisabled": target.checkin_when_disabled,
                                    "accounts": account_views()})
        if path == "/accounts/checkin":
            # 账号面板的「每日签到」按钮：**只做每日签到领积分**——Credits 类活动
            # （如"每天领 100 Credits"，旧 sash 接口兜底），不领券/兑换码类活动，
            # 也不领 Pro 福利包（那些走签到与福利中心的按钮）。
            #
            # 并发：签到是「每账号一次上游往返」，串行时 N 个账号 = N 个 RTT +
            # N 次间隔，面板上就是「点了没反应」。现在默认 3 并发
            # （QD_CHECKIN_CONCURRENCY 1–8），启动前加 0~0.8s 抖动错开。
            import qoder_tasks
            from concurrent.futures import ThreadPoolExecutor, as_completed
            uid = payload.get("uid")
            targets = [POOL.get(uid)] if uid else list(POOL.accounts)
            targets = [a for a in targets
                       if a is not None and a.checkin_eligible()]
            results = [None] * len(targets)

            def _one(idx, account):
                # 首个任务立即开始，其余 0~0.3s 错开（避免机器枪特征又不拖慢整批）
                if idx:
                    try:
                        time.sleep(random.uniform(0, 0.3))
                    except Exception:
                        pass
                res = qoder_tasks.run_checkin(account, gap=CHECKIN_MIN_GAP,
                                              only_daily=True)
                qoder_activity.record(
                    USAGE_DIR, "checkin", bool(res.get("ok")),
                    uid=account.uid, nickname=account.nickname,
                    realm=getattr(account, "realm", "") or "",
                    detail=(res.get("msg") or res.get("reason") or ""),
                    amount=res.get("earned_credit") or 0)
                return {
                    "uid": account.uid,
                    "nickname": account.nickname,
                    "ok": bool(res.get("ok")),
                    "earned_credit": res.get("earned_credit") or 0,
                    "msg": (res.get("logs") or [""])[-1],
                    "logs": res.get("logs") or [],
                    "credits": res.get("credits"),
                    # issue #20：透传活动平台的精确结论（前端一直在读）。
                    "message": res.get("message"),
                    "claimed": res.get("claimed") or [],
                    "next_available_at": res.get("next_available_at"),
                    "next_available_note": res.get("next_available_note"),
                }

            if targets:
                try:
                    workers = max(1, min(8, int(
                        os.environ.get("QD_CHECKIN_CONCURRENCY") or 3)))
                except Exception:
                    workers = 3
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_one, i, a): i
                               for i, a in enumerate(targets)}
                    for fut in as_completed(futures):
                        idx = futures[fut]
                        try:
                            results[idx] = fut.result()
                        except Exception as exc:
                            acc = targets[idx]
                            results[idx] = {
                                "uid": acc.uid, "nickname": acc.nickname,
                                "ok": False, "earned_credit": 0,
                                "msg": "签到异常: %s" % str(exc)[:120],
                                "logs": [], "credits": None,
                                "message": None, "claimed": [],
                                "next_available_at": None,
                                "next_available_note": None}
            results = [r for r in results if r]
            qoder_tasks.invalidate_panel_cache()   # 写操作后失效面板短缓存
            return self._json(200, {"results": results,
                                    "accounts": account_views()})
        if path == "/accounts/login/start":
            platform = payload.get("platform") or "CLI"
            target_realm = payload.get("realm") or CURRENT_REALM
            if target_realm not in ("intl", "cn"):
                target_realm = "cn"
            try:
                started = POOL.start_login(realm=target_realm,
                                           platform=platform, vpc=payload.get("vpc"))
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            except Exception as exc:
                return self._error(502, "could not start login: %s" % exc)
            log("oauth device login started (realm=%s, state=%s)"
                % (target_realm, started["state"][:8]))
            return self._json(200, started)
        if path == "/accounts/login/cancel":
            state = payload.get("state") or ""
            return self._json(200, {"cancelled": POOL.cancel_login(state)})
        if path in ("/accounts/import/pat", "/accounts/import-pat"):
            pat = payload.get("pat") or ""
            realm = payload.get("realm") or CURRENT_REALM
            if realm not in ("intl", "cn"):
                realm = "cn"
            try:
                account = POOL.import_pat(pat, realm=realm, vpc=payload.get("vpc"))
            except Exception as exc:
                return self._error(400, "PAT import failed: %s" % exc)
            log("imported PAT account %s (realm=%s)"
                % (account.uid[:8], realm))
            return self._json(200, {"imported": [account.public()],
                                    "accounts": account_views()})
        if path == "/accounts/import/desktop":
            # 两步确认：{} 只读扫描（双区：桌面 App auth.v1.dat + CLI user）；
            # {"path":...} 按确认导入该凭证；{"all":true} 导入全部有效项。
            target_path = payload.get("path")
            if target_path:
                realm = payload.get("realm")
                try:
                    account = qoder_accounts.import_desktop_credential(
                        path=target_path, realm=realm)
                except Exception as exc:
                    return self._error(400, "import failed: %s" % exc)
                log("imported %s (%s) from local client credential"
                    % (account.uid[:8], account.realm), tag="accounts")
                return self._json(200, {
                    "imported": [account.public()],
                    "accounts": account_views(),
                    "pool_uids": [a.uid for a in POOL.accounts],
                })
            if payload.get("all"):
                try:
                    imported = qoder_accounts.import_desktop_credential()
                except Exception as exc:
                    return self._error(400, "import failed: %s" % exc)
                for account in imported:
                    log("imported %s (%s) from local client credential"
                        % (account.uid[:8], account.realm), tag="accounts")
                return self._json(200, {
                    "imported": [a.public() for a in imported],
                    "accounts": account_views(),
                    "pool_uids": [a.uid for a in POOL.accounts],
                })
            try:
                detected = qoder_accounts.scan_desktop_credentials()
            except Exception as exc:
                detected = []
                log("desktop credential scan failed: %s" % exc)
            return self._json(200, {
                "detected": detected,
                "accounts": account_views(),
                "pool_uids": [a.uid for a in POOL.accounts],
            })
        if path == "/accounts/refresh":
            uid = payload.get("uid")
            targets = [POOL.get(uid)] if uid else list(POOL.accounts)
            # #25：第三个同类入口 —— 同样只对 enabled 且有凭证的号打上游
            # （refresh 会打 OAuth 端点并 save 落盘，对禁用号纯属副作用）。
            targets, skipped = _refreshable_accounts(targets)
            results = []
            for account in targets:
                ok = account.refresh()
                account.save(ACCOUNTS_DIR)
                results.append({"uid": account.uid, "ok": ok,
                                "error": account.last_error})
            return self._json(200, {"results": results, "skipped": skipped})
        if path == "/accounts/test":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            account = POOL.get(uid)
            if not account:
                return self._error(404, "no such account")
            # issue #134：默认用免费模型探活，零额度号也能测出凭证好坏；
            # 显式传 model 时仍以调用方为准。
            test_model = (payload.get("model")
                          or _default_test_model(acc_realm(account)))
            test_payload = {
                "model": test_model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
            t0 = time.time()
            try:
                # issue #86：必须测试被点的那个号（此前会落到池里任意账号）
                resp, acc, _ = open_upstream(
                    test_payload, target_realm=acc_realm(account),
                    only_account=account)
                with resp:
                    chat_obj = aggregate_stream(resp, test_model, None)
                wall_ms = int((time.time() - t0) * 1000)
                choices = chat_obj.get("choices") or []
                msg = (choices[0].get("message") or {}) if choices else {}
                reply_text = (msg.get("content") or msg.get("reasoning_content")
                              or "OK").strip()
                if len(reply_text) > 80:
                    reply_text = reply_text[:77] + "..."
                acc.clear_error()      # 清的是**实际**被测账号（issue #86）
                log("account test: uid=%s model=%s wall=%dms ok=True"
                    % (account.uid[:8], test_model, wall_ms), tag="accounts")
                return self._json(200, {"ok": True, "uid": account.uid,
                                        "model": test_model,
                                        "elapsed_ms": wall_ms,
                                        "reply": reply_text})
            except RateLimited as exc:
                wall_ms = int((time.time() - t0) * 1000)
                return self._json(200, {"ok": False, "uid": account.uid,
                                        "status": 429,
                                        "error": "rate limited: %s" % exc.detail[:150],
                                        "elapsed_ms": wall_ms})
            except UpstreamStatus as exc:
                # issue #123 后续：信封层 403/429 以前落到通用分支，只显示
                # "upstream status 403"，看不到业务码（10605/112/110）——
                # 把 detail 原样带回面板，并按已知码给出人话解释。
                wall_ms = int((time.time() - t0) * 1000)
                _detail = str(getattr(exc, "detail", "") or "")[:400]
                _hint = _quota_hint(_detail)
                # issue #134：额度耗尽的号本来就不能服务请求，**测试**再去给它
                # 叠一分钟冷却纯属副作用（连点两次测试就把它压住了）。
                # 只有真正的凭证/服务端故障才记冷却。
                if "112" not in _detail and "110" not in _detail \
                        and "10605" not in _detail and "isQueued" not in _detail:
                    account.note_error(
                        "HTTP %s: %s" % (exc.status, _detail[:80]), cooldown=60)
                log("account test: uid=%s model=%s wall=%dms upstream=%s %s"
                    % (account.uid[:8], test_model, wall_ms, exc.status,
                       _hint or _detail[:120]), level="WARN", tag="accounts")
                return self._json(200, {"ok": False, "uid": account.uid,
                                        "status": exc.status,
                                        "error": "upstream HTTP %s: %s"
                                        % (exc.status, _detail[:200]),
                                        "detail": _detail,
                                        "hint": _hint,
                                        "elapsed_ms": wall_ms})
            except urllib.error.HTTPError as exc:
                wall_ms = int((time.time() - t0) * 1000)
                try:
                    detail = exc.read(400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                account.note_error("HTTP %d: %s" % (exc.code, detail[:80]),
                                   cooldown=60)
                log("account test: uid=%s model=%s wall=%dms error=%d"
                    % (account.uid[:8], test_model, wall_ms, exc.code),
                    level="WARN", tag="accounts")
                return self._json(200, {"ok": False, "uid": account.uid,
                                        "status": exc.code,
                                        "error": "HTTP %d: %s"
                                        % (exc.code, detail[:150]),
                                        "elapsed_ms": wall_ms})
            except Exception as exc:
                wall_ms = int((time.time() - t0) * 1000)
                account.note_error(str(exc)[:80], cooldown=60)
                log("account test: uid=%s model=%s wall=%dms exc=%s"
                    % (account.uid[:8], test_model, wall_ms, exc),
                    level="WARN", tag="accounts")
                return self._json(200, {
                    "ok": False, "uid": account.uid, "status": 500,
                    "error": _friendly_net_error(
                        exc,
                        qoder_accounts.account_realm_config(account)["gateway"]),
                    "elapsed_ms": wall_ms})
        if path == "/accounts/set":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            if "vpcEndpoint" in payload:
                try:
                    changed = POOL.set_vpc(uid, payload["vpcEndpoint"])
                except ValueError as exc:
                    return self._error(400, str(exc), "invalid_request_error")
                if changed is None:
                    return self._error(404, "no such account")
                import qoder_tasks
                qoder_tasks.invalidate_panel_cache()
                return self._json(200, {"account": changed})
            updated = POOL.set_enabled(uid, bool(payload.get("enabled")))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s %s" % (uid[:8],
                                   "enabled" if payload.get("enabled")
                                   else "disabled"))
            return self._json(200, {"account": updated})
        if path == "/accounts/set-all":
            POOL.set_all_enabled(bool(payload.get("enabled")))
            return self._json(200, {"accounts": account_views()})
        if path == "/accounts/delete":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            removed = POOL.remove(uid)
            log("account %s deleted" % uid[:8])
            return self._json(200, {"deleted": removed,
                                    "accounts": account_views()})
        if path == "/accounts/import":
            # 支持的文档形态见 qoder_accounts._coerce_account_rows。
            # 选项：dryRun / overwrite / realm ("intl"|"cn")
            blob = payload.get("data") if "data" in payload else payload
            if not isinstance(blob, (dict, list)):
                return self._error(400, "the document must be a JSON object "
                                   "or array", "invalid_request_error")
            rows, problem = qoder_accounts._coerce_account_rows(blob)
            if problem:
                return self._error(400, "cannot read the document: %s" % problem,
                                   "invalid_request_error")
            dry_run = bool(payload.get("dryRun"))
            overwrite = bool(payload.get("overwrite"))
            forced_realm = (payload.get("realm") or "").strip().lower() or None
            if forced_realm and forced_realm not in ("intl", "cn"):
                return self._error(400, "realm must be intl or cn",
                                   "invalid_request_error")
            if dry_run:
                return self._json(200, {
                    "dryRun": True,
                    "count": len(rows),
                    "result": POOL.preview_import_rows(rows, realm=forced_realm,
                                                       group=payload.get("group"),
                                                       overwrite=overwrite),
                    "accounts": account_views(),
                })
            report = POOL.import_rows(rows, realm=forced_realm, group=payload.get("group"),
                                      overwrite=overwrite)
            log("account import: %d added, %d updated, %d skipped, %d invalid"
                % (len(report["added"]), len(report["updated"]),
                   len(report["skipped"]), len(report["invalid"])))
            return self._json(200, {
                "count": len(rows),
                "result": report,
                "accounts": account_views(),
            })
        return self._error(404, "unknown account endpoint",
                           "invalid_request_error")

    def _handle_anthropic(self, path, payload):
        """Anthropic /v1/messages（task-78 接线；设计文档 .team/72-ANTHROPIC-BRIDGE-DESIGN.md §7）。

        三条易错点逐条落地：
          1) 形态决策**只算一次**（写进 chat_req["_qd_structured"] 下传）；
          2) 流式守卫链与 chat 链逐字一致，allowed_names 取**转换后的 chat_req**；
          3) 出站只做协议翻译，不在本层重写吞掉/回读。
        能力差异（有意）：流式**不做流内信封重开** —— Anthropic 客户端对重复
        message_start 的容忍度未知，宁可给明确的 event: error 收尾；非流式仍走
        aggregate_with_envelope_retry（此时尚未向客户端写任何字节，重试安全）。
        """
        if path.endswith("/count_tokens"):
            # 纯本地估算：**不发上游**（判据 4）
            return self._json(
                200, qoder_anthropic.estimate_anthropic_tokens(payload))
        ok, problem = qoder_anthropic.validate_request(payload)
        if not ok:
            return self._error(400, problem, "invalid_request_error")
        req_realm = self._request_realm()
        model_hint = payload.get("model") or "auto"
        # 与 open_upstream 同一解析器（含动态显示名兜底）：官方改显示名窗口期
        # Anthropic 首条请求才不会原样透传（meta 取不到 -> 思考档位被静默丢弃）。
        model_key = resolve_model_key(model_hint, realm=req_realm)
        # 传 supported 档位表，否则 effort_from_anthropic 会静默丢弃思考档位。
        meta = catalog_meta_for(model_key, req_realm)
        chat_req = qoder_anthropic.messages_to_chat(
            payload, catalog_meta=meta, supported=supported_efforts(meta))
        self._apply_context_header(chat_req)  # issue #106
        model = chat_req.get("model") or payload.get("model") or "auto"
        want_stream = bool(payload.get("stream"))
        session_key = extract_session_key(self.headers, chat_req)
        # issue #55：带上 system 来源与 messages 内 system 条数（缓存问题可自证）
        fp = prompt_fingerprint(
            chat_req.get("messages"),
            sys_src=_anthropic_sys_src(payload),
            n_msg_system=qoder_anthropic.count_msg_system(payload))
        t_start = time.time()
        # ---- 形态决策单点：在这里算唯一一次，随 chat_req 下传 ----
        # issue #79：realm 未显式指定时（#76 起入口传 None），形态判定必须和
        # open_upstream 用同一个兜底 —— auto 模式要求 realm == "cn"，None 会让
        # DeepSeek / Qwen 等白名单永远不生效，历史工具调用回退文本化并被模型复述。
        _realm_for_shape = (req_realm or detect_model_realm(model)
                            or CURRENT_REALM)
        chat_req["_qd_structured"] = structured_tool_history_enabled(
            model=model, model_key=model_key, realm=_realm_for_shape,
            messages=chat_req.get("messages"))
        holder = {"usage": None,
                  "allowed_names": _tool_names_from_payload(chat_req)}
        log("anthropic: model=%s stream=%s msgs=%d effort=%r structured=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               bool(chat_req.get("_qd_structured"))))
        # account 预置在 try 之外：下面所有 except 里的 record_error 都会引用它，
        # 而选号成功前它就是 None。必须在任何可能抛异常的语句之前。
        account = None
        try:
            usage_ctx = {"tenant_scope": self._key_id_for_usage(), "_key_entry": getattr(self, "key_entry", None)}
            blocked = self._model_precheck(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            upstream, account, _ = open_upstream(
                chat_req, session_key=session_key, target_realm=req_realm,
                usage_ctx=usage_ctx)
            # #26：记录实际出口（自动路由后可能与 req_realm 不同）→ 响应头可见
            self.realm_used = usage_ctx.get("realm") or req_realm
            self.realm_reason = usage_ctx.get("realm_reason") or ""
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            return self._rate_limited(exc)
        except UpstreamQueued as exc:
            # issue #99：这个入口以前没接 #87 的排队分支，10605 排队会零等待
            # 直接 503 且丢掉 Retry-After。此刻**还没写过任何字节**
            # （_sse_begin 尚未调用），所以流内等待不存在「重复 message_start」
            # 的顾虑——docstring 里那条能力差异只适用于 aggregate_stream 之后。
            _msg, _et = friendly_upstream_error(503, exc.detail)
            if not want_stream or queue_mode() != "visible":
                return self._error(503, _msg, _et, retry_after=exc.retry_after)
            self._sse_begin()
            _q0 = time.time()

            def _emit_q(kind, info):
                info = dict(info or {})
                info["model"] = model
                if kind == "queue":
                    self._queue_notice("anthropic", info)
                else:
                    self._queue_error_frame(
                        "anthropic", info.get("message") or _msg,
                        info.get("waited_seconds") or int(time.time() - _q0),
                        info.get("budget_seconds") or QUEUE_WAIT_BUDGET_SECS)

            _got = visible_queue_acquire(
                exc,
                lambda: open_upstream(chat_req, session_key=session_key,
                                      target_realm=req_realm,
                                      usage_ctx=usage_ctx),
                _emit_q, started=_q0)
            if _got is None:
                self._sse_end()
                return
            upstream, account, _ = _got
            self.realm_used = usage_ctx.get("realm") or req_realm
            self.realm_reason = usage_ctx.get("realm_reason") or ""
        except UpstreamStatus as exc:
            # issue #59：PayloadTooLarge(413) 等本地拦下的错误——明确状态码，
            # 不进账号冷却/重试链路（上游根本没收到这个请求）。
            _code = _to_int_status(exc.status)
            record_error(model, _code, exc.detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            _msg, _etype = friendly_upstream_error(_code, exc.detail)
            # issue #99：即使走到兜底分支，也把上游建议的重试间隔透传出去
            return self._error(_code, _msg, _etype,
                               retry_after=getattr(exc, "retry_after", None))
        except urllib.error.HTTPError as exc:
            detail = getattr(exc, "qoder_detail", "")
            if not detail:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            msg, etype = friendly_upstream_error(exc.code, detail)
            return self._error(exc.code, msg, etype)
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            if message.startswith("no usable account"):
                return self._error(503, message
                                   + " - add or enable one at the dashboard (/)")
            return self._error(502, "upstream unreachable: %s" % exc)
        with upstream:
            if want_stream:
                self._sse_begin()
                first_ms = None
                try:
                    # 守卫链与 chat 主链路**逐字一致**（顺序与参数都不能改）：
                    #   sse_with_heartbeat(recover_leaked_tool_calls(
                    #       iter_inner_sse(...), allowed_names=...))
                    # allowed_names 必须来自**转换后的 chat_req**；用 Anthropic
                    # 原始 payload 会取不到工具名 -> 回读守卫失效 -> #9/#11 泄漏回归。
                    inner = sse_with_heartbeat(
                        recover_leaked_tool_calls(
                            iter_inner_sse(upstream, holder=holder),
                            allowed_names=_tool_names_from_payload(chat_req)),
                        self._sse_write)
                    for frame in qoder_anthropic.stream_anthropic_events(
                            inner, model, holder,
                            thinking_mode_override=anthropic_thinking_mode(getattr(self, "key_entry", None))):
                        if first_ms is None:
                            first_ms = int((time.time() - t_start) * 1000)
                        self._sse_write(frame)
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):
                    wall = int((time.time() - t_start) * 1000)
                    record_usage(model, holder.get("usage"), stream=True,
                                 elapsed_ms=wall, ttft_ms=first_ms,
                                 gen_ms=(wall - first_ms)
                                 if first_ms is not None else None,
                                 fp=fp, account=account.uid,
                                 effort=usage_ctx.get("effort"),
                                 effort_requested=usage_ctx.get("effort_requested"),
                                 key_id=self._key_id_for_usage(),
                                 realm_reason=usage_ctx.get("realm_reason", ""),
                                 session_trace=usage_ctx)
                    return
                except UpstreamStatus as exc:
                    # Anthropic 侧不重开（见 docstring）：直接给终态 error 事件。
                    _handle_envelope_account_cooldown(account, exc, model=model,
                                                      session_key=session_key)
                    wall = int((time.time() - t_start) * 1000)
                    record_error(model, exc.status, exc.detail,
                                 elapsed_ms=wall, stream=True,
                                 account=account.uid if account else None,
                                 key_id=self._key_id_for_usage())
                    msg, _ = friendly_upstream_error(_to_int_status(exc.status),
                                                     exc.detail)
                    log("anthropic upstream status %s: %s"
                        % (exc.status, msg[:200]), level="ERROR", tag="chat")
                    self._sse_write(qoder_anthropic.anthropic_error_frame(msg))
                    self._sse_end()
                    return
                self._sse_end()
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, holder.get("usage"), stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms)
                             if first_ms is not None else None,
                             fp=fp, account=account.uid,
                             effort=usage_ctx.get("effort"),
                             effort_requested=usage_ctx.get("effort_requested"),
                             key_id=self._key_id_for_usage(),
                             realm_reason=usage_ctx.get("realm_reason", ""),
                             session_trace=usage_ctx)
                return
            try:
                chat_obj, account = aggregate_with_envelope_retry(
                    upstream, chat_req, session_key, req_realm, model,
                    holder, account, usage_ctx=usage_ctx)
            except UpstreamStatus as exc:
                record_error(model, exc.status, exc.detail,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             stream=want_stream,
                             account=account.uid if account else None,
                             key_id=self._key_id_for_usage())
                msg, etype = friendly_upstream_error(_to_int_status(exc.status),
                                                     exc.detail)
                return self._error(
                    exc.status if str(exc.status).isdigit() else 502, msg, etype)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             stream=want_stream,
                             account=account.uid if account else None,
                             key_id=self._key_id_for_usage())
                return self._error(502, "upstream stream error: %s" % exc)
            wall = int((time.time() - t_start) * 1000)
            result = qoder_anthropic.chat_to_messages(
                chat_obj, model=model,
                thinking_mode_override=anthropic_thinking_mode(getattr(self, "key_entry", None)))
            record_usage(model, chat_obj.get("usage"), stream=False,
                         elapsed_ms=wall, fp=fp, account=account.uid,
                         effort=usage_ctx.get("effort"),
                         effort_requested=usage_ctx.get("effort_requested"),
                         key_id=self._key_id_for_usage(),
                         realm_reason=usage_ctx.get("realm_reason", ""),
                         session_trace=usage_ctx)
            return self._json(200, result)

    def _handle_web_tools(self, path, payload, names):
        """本地代跑 web_search / web_fetch：模型要搜就由网关去搜，再把结果回灌。

        只在客户端声明了内置 web 工具（type=web_search / web_fetch）且开关打开时
        走这里；普通 function 工具照常走原来的透传路径。
        """
        is_responses = path.endswith("/responses")
        want_stream = bool(payload.get("stream"))
        try:
            chat_req = (responses_to_chat(payload) if is_responses
                        else dict(payload))
        except Exception as exc:
            return self._error(400, "invalid request: %s" % exc,
                               "invalid_request_error")
        chat_req = dict(chat_req)
        chat_req["tools"] = rewrite_web_tools(chat_req.get("tools") or [], names)
        chat_req["stream"] = False
        model = str(chat_req.get("model") or "")
        # issue #131-2：这条分支此前直接 open_upstream，跳过了模型封禁与 Key
        # 白名单 —— 只要声明一个 web_search/web_fetch 就能用本不该授权的模型。
        # 授权必须与协议/工具无关，统一在分流之后、推理之前判一次。
        _blocked = self._model_precheck(model, self._request_realm())
        if _blocked:
            return self._error(400, _blocked, "invalid_request_error")
        session_key = extract_session_key(self.headers, payload)
        messages = list(chat_req.get("messages") or [])
        realm = self._request_realm()
        usage_ctx = {"tenant_scope": self._key_id_for_usage(), "_key_entry": getattr(self, "key_entry", None)}
        obj = None
        rounds = 0
        _acc = {}          # 多轮消耗累加（issue #131-3）
        for _ in range(max(1, WEB_TOOLS_MAX_ROUNDS)):
            req = dict(chat_req, messages=messages)
            try:
                resp, account, _ = open_upstream(
                    req, target_realm=realm, usage_ctx=usage_ctx,
                    session_key=session_key)
            except RateLimited as exc:
                return self._rate_limited(exc)
            except urllib.error.HTTPError as exc:
                detail = getattr(exc, "qoder_detail", "") or ""
                msg, etype = friendly_upstream_error(exc.code, detail)
                return self._error(exc.code, msg, etype)
            except Exception as exc:
                return self._error(502, str(exc)[:200], "upstream_error")
            _round_t0 = time.time()
            with resp:
                obj = aggregate_stream(resp, model,
                                       holder={"usage": None},
                                       allowed_names=names)
            # issue #131-3：这条分支此前完全不记账 —— 看板/按 Key 账号统计、
            # 以及按本地流水折叠的每日限额都漏掉这部分消耗。每轮都记一条
            # （账号/Key/模型与真实出站一致），并累加成总数回给客户端。
            _u = obj.get("usage") or {}
            if _u:
                try:
                    record_usage(model, _u, stream=want_stream,
                                 elapsed_ms=int((time.time() - _round_t0) * 1000),
                                 account=account.uid if account else None,
                                 effort=usage_ctx.get("effort"),
                                 effort_requested=usage_ctx.get("effort_requested"),
                                 key_id=self._key_id_for_usage(),
                                 realm_reason=usage_ctx.get("realm_reason", ""),
                                 session_trace=usage_ctx)
                except Exception as exc:
                    log("web tools: record_usage failed: %s" % exc,
                        level="WARN", tag="webtools")
                for _k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    try:
                        _acc[_k] = _acc.get(_k, 0) + int(_u.get(_k) or 0)
                    except (TypeError, ValueError):
                        pass
                for _k in ("credits", "credit"):
                    if _u.get(_k) is not None:
                        try:
                            _acc[_k] = float(_acc.get(_k) or 0) + float(_u[_k])
                        except (TypeError, ValueError):
                            pass
            msg = ((obj.get("choices") or [{}])[0].get("message") or {})
            calls = [c for c in (msg.get("tool_calls") or [])
                     if (c.get("function") or {}).get("name") in names]
            if not calls:
                break
            rounds += 1
            log("web tools: round %d, %d call(s) -> local execution"
                % (rounds, len(calls)), tag="webtools")
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": msg.get("tool_calls") or []})
            for call in calls:
                fn = call.get("function") or {}
                try:
                    result = qoder_webtools.run_tool(fn.get("name"),
                                                     fn.get("arguments"))
                except Exception as exc:
                    result = json.dumps({"error": str(exc)[:120]},
                                        ensure_ascii=False)
                messages.append({"role": "tool",
                                 "tool_call_id": call.get("id") or "",
                                 "content": result})
        self.realm_used = usage_ctx.get("realm") or (realm or "")
        obj = obj or {"choices": [{"index": 0, "message": {"role": "assistant",
                                   "content": ""}, "finish_reason": "stop"}]}
        if _acc:
            # 多轮合计回给客户端：客户端只看到一次请求，但上游扣的是每轮的钱。
            _usage = dict(obj.get("usage") or {})
            _usage.update({k: v for k, v in _acc.items() if v})
            obj = dict(obj, usage=_usage)
        if is_responses:
            if not want_stream:
                return self._json(200, chat_to_response(obj, model))
            self._sse_begin()
            for frame in stream_responses_events(
                    _chat_chunks_iter(obj, model), model,
                    {"usage": obj.get("usage")}):
                self._sse_write(clean_responses_frame(frame))
            return
        if not want_stream:
            return self._json(200, obj)
        self._sse_begin()
        for chunk in _chat_chunks(obj, model):
            self._sse_write("data: " + json.dumps(chunk, ensure_ascii=False)
                            + "\n\n")
        self._sse_write("data: [DONE]\n\n")

    def _handle_responses(self, payload):
        """Serve /v1/responses by translating to chat completions upstream."""
        session_key = extract_session_key(self.headers, payload)
        chat_req = responses_to_chat(payload)
        # issue #127：custom 判型必须用**合并 + 展平后**的清单（顶层 tools 与
        # input.additional_tools 都可能含容器），否则回程判型与出站不一致。
        custom_names = custom_tool_names(chat_req.get("tools") or [])
        tool_ns = chat_req.get("_qd_tool_ns") or {}
        self._apply_context_header(chat_req)  # issue #106
        model = payload.get("model") or "auto"
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        log("responses: model=%s stream=%s msgs=%d effort=%r custom_tools=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               sorted(custom_names) or "-"))
        holder = {"usage": None, "custom_names": custom_names,
                  "tool_ns": tool_ns,
                  "allowed_names": _tool_names_from_payload(chat_req)}
        # account 预置在 try 之外：下面所有 except 里的 record_error 都会引用它，
        # 而选号成功前它就是 None。放在 try 内、open_upstream 之前也行 —— 只要
        # 在**任何可能抛异常的语句之前**（_request_realm 也不保证不抛）。
        account = None
        try:
            req_realm = self._request_realm()
            usage_ctx = {"tenant_scope": self._key_id_for_usage(), "_key_entry": getattr(self, "key_entry", None)}          # P1-3：推理档位审计出参（下传给 open_upstream）
            blocked = self._model_precheck(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            upstream, account, _ = open_upstream(
                chat_req, session_key=session_key, target_realm=req_realm,
                usage_ctx=usage_ctx)
            # #26：记录实际出口（自动路由后可能与 req_realm 不同）→ 响应头可见
            self.realm_used = usage_ctx.get("realm") or req_realm
            self.realm_reason = usage_ctx.get("realm_reason") or ""
        except UpstreamQueued as exc:
            # issue #87：排队——visible 在流内等待并持续报告进度；
            # fast / 非流式直接 503 + Retry-After + 中文原因。
            _msg, _et = friendly_upstream_error(503, exc.detail)
            if not want_stream or queue_mode() != "visible":
                return self._error(503, _msg, _et, retry_after=exc.retry_after)
            self._sse_begin()
            _q0 = time.time()

            def _emit_q(kind, info):
                info = dict(info or {})
                info["model"] = chat_req.get("model") or ""
                if kind == "queue":
                    self._queue_notice("responses", info)
                else:
                    self._queue_error_frame(
                        "responses", info.get("message") or _msg,
                        info.get("waited_seconds") or int(time.time() - _q0),
                        info.get("budget_seconds") or QUEUE_WAIT_BUDGET_SECS)

            _got = visible_queue_acquire(
                exc,
                lambda: open_upstream(chat_req, session_key=session_key,
                                      target_realm=req_realm,
                                      usage_ctx=usage_ctx),
                _emit_q, started=_q0)
            if _got is None:
                self._sse_end()
                return
            upstream, account, _ = _got
            self.realm_used = usage_ctx.get("realm") or req_realm
            self.realm_reason = usage_ctx.get("realm_reason") or ""
        except RateLimited as exc:
            t = time.time() - t_start
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int(t * 1000), stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            return self._rate_limited(exc)
        except UpstreamStatus as exc:
            # issue #59：PayloadTooLarge(413) 等**上游未收到**的错误走这里——
            # 明确状态码 + 可读文案，且不进入账号冷却/重试链路。
            _code = _to_int_status(exc.status)
            record_error(model, _code, exc.detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            _msg, _etype = friendly_upstream_error(_code, exc.detail)
            return self._error(_code, _msg, _etype)
        except urllib.error.HTTPError as exc:
            # 错误体在 open_upstream 中已读过（挂在 qoder_detail），二次 read
            # 会拿到残缺内容——优先取挂载值。
            detail = getattr(exc, "qoder_detail", "")
            if not detail:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            msg, etype = friendly_upstream_error(exc.code, detail)
            return self._error(exc.code, msg, etype)
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            if message.startswith("no usable account"):
                return self._error(503, message
                                   + " - add or enable one at the dashboard (/)")
            return self._error(502, "upstream unreachable: %s" % exc)
        with upstream:
            if want_stream:
                self._sse_begin()
                first_ms = None
                # 流内信封重试：上游可能 HTTP200 建流后在信封里投 418
                # （access log 记 200 + 业务错 418 即此形态）。只要还没向
                # 客户端写出任何上游字节，就重开上游再试。
                attempts = 0
                cur = upstream
                pump_exc = None
                # issue #83: cross-realm failover at most once per request
                _switched71 = False
                try:
                    while True:
                        try:
                            # 统计已消费的上游数据行：错误信封本身不产出
                            # 行，故 n==0 表示还没消费到任何上游数据 → 可重开
                            consumed = {"n": 0}

                            def _count_src(src, _c=consumed):
                                for item in src:
                                    _c["n"] += 1
                                    yield item

                            # issue #87：Responses 与 chat 走**同一条**回读链
                            # （此前只有 chat/Anthropic 有，Codex 侧泄漏型工具
                            # 调用只能等流末才被解析出来）。
                            inner = _count_src(
                                sse_with_heartbeat(
                                    recover_leaked_tool_calls(
                                        iter_inner_sse(cur, holder=holder),
                                        allowed_names=_tool_names_from_payload(chat_req)),
                                    self._sse_write))
                            for frame in stream_responses_events(inner, model, holder):
                                if first_ms is None:
                                    first_ms = int((time.time() - t_start) * 1000)
                                self._sse_write(clean_responses_frame(frame))
                            break
                        except (BrokenPipeError, ConnectionResetError,
                                ConnectionAbortedError):
                            wall = int((time.time() - t_start) * 1000)
                            record_usage(model, holder.get("usage"), stream=True,
                                         elapsed_ms=wall, ttft_ms=first_ms,
                                         gen_ms=(wall - first_ms)
                                         if first_ms is not None else None,
                                         fp=fp, account=account.uid,
                                         effort=usage_ctx.get("effort"),
                                         effort_requested=usage_ctx.get("effort_requested"),
                                         key_id=self._key_id_for_usage(),
                                         realm_reason=usage_ctx.get("realm_reason", ""),
                                         session_trace=usage_ctx)
                            return
                        except UpstreamStatus as exc:
                            _handle_envelope_account_cooldown(account, exc, model=model, session_key=session_key)
                            # 控制帧（response.created 等）先于数据，不能算
                            # “已输出”；以上游数据行计数判断能否重开。
                            if consumed.get("n", 0) == 0 and should_retry_envelope(
                                    exc, False, attempts, usage_ctx=usage_ctx):
                                attempts += 1
                                log("responses in-stream envelope status %s "
                                    "(try %d/%d), reopening upstream"
                                    % (exc.status,
                                       min(attempts + 1, _queue_budget()),
                                       _queue_budget()),
                                    level="WARN", tag="chat")
                                time.sleep(_envelope_retry_wait(exc, attempts))
                                if _is_image_conversion_timeout(exc.detail):
                                    cur.close()  # Release team concurrency before reopening.
                                try:
                                    # 必须用转换后的 chat 请求体重开：原始
                                    # Responses 体的会话在 input 字段里，而
                                    # build_qoder_body 只读 messages -> 空会话。
                                    cur2, account, _ = open_upstream(
                                        chat_req, session_key=session_key,
                                        target_realm=req_realm,
                                        usage_ctx=usage_ctx,
                                        **({"only_account": account} if
                                           _is_image_conversion_timeout(exc.detail) else {}))
                                except Exception as rex:
                                    log("responses reopen failed: %s"
                                        % str(rex)[:160], level="WARN")
                                    pump_exc = exc
                                    break
                                if cur is not upstream:
                                    try:
                                        cur.close()
                                    except Exception:
                                        pass
                                cur = cur2
                                continue
                            # issue #71：排队耗尽 -> 先试另一区（同一模型、区域级排队），
                            # 仍不行再转成 429 + Retry-After，而不是把 403 丢给客户端。
                            _alt71 = ("" if self.headers.get("X-Realm") else
                                      _alt_realm_ready(req_realm, model))
                            if (_envelope_queue_wait(exc) > 0 and _alt71
                                    and not _switched71):
                                log("queue exhausted -> failing over to %s (issue #71)"
                                    % _alt71, level="WARN", tag="chat")
                                _switched71 = True
                                try:
                                    cur2, account, _ = open_upstream(
                                        chat_req, session_key=session_key,
                                        target_realm=_alt71,
                                        usage_ctx=usage_ctx)
                                except Exception as rex:
                                    log("cross-realm reopen failed: %s"
                                        % str(rex)[:120], level="WARN")
                                    pump_exc = _as_rate_limited_for_queue(exc, model)
                                    break
                                if cur is not upstream:
                                    try:
                                        cur.close()
                                    except Exception:
                                        pass
                                cur = cur2
                                continue
                            pump_exc = _as_rate_limited_for_queue(exc, model)
                            break
                finally:
                    if cur is not upstream:
                        try:
                            cur.close()
                        except Exception:
                            pass
                if pump_exc is not None and isinstance(
                        pump_exc, InvalidToolArguments) \
                        and not getattr(pump_exc, "integrity_failure", False) \
                        and tool_echo_mode() == "lenient":
                    # issue #135：上游只吐了工具协议残片（Qwen3.8-Flash 实测高频）。
                    # strict（默认）会让这一轮流以 response.failed 结束，某些客户端
                    # （Codex）收到后直接静默结束对话 —— 用户看到的就是「话说到一半
                    # 突然没了，而且没有任何提示」。lenient 下改为正常收尾：保留已产生
                    # 的正文，本轮算完成，用户可以直接继续。
                    log("lenient tool-echo: converting fail-closed to normal "
                        "completion (%s) - issue #135" % str(pump_exc)[:160],
                        level="WARN", tag="chat")
                    record_error(model, 200, "lenient_tool_echo: %s"
                                 % str(pump_exc)[:160],
                                 elapsed_ms=int((time.time() - t_start) * 1000),
                                 stream=True,
                                 account=account.uid if account else None,
                                 key_id=self._key_id_for_usage())
                    try:
                        self._sse_write(_responses_completed_quiet(holder, model))
                    except Exception:
                        pass
                    self._sse_end()
                    return
                if pump_exc is not None:
                    # issue #100：pump 出来的可能是 RateLimited（排队耗尽），
                    # 它没有 .status —— 统一经 _exc_http_status 取，别再裸读。
                    _pst = _exc_http_status(pump_exc, 429)
                    record_error(model, _pst, pump_exc.detail,
                                 elapsed_ms=int((time.time() - t_start) * 1000),
                                 stream=True,
                                 account=account.uid if account else None,
                                 key_id=self._key_id_for_usage())
                    msg, _etype = friendly_upstream_error(_to_int_status(_pst),
                                                          pump_exc.detail)
                    log("responses upstream status %s: %s"
                        % (_pst, msg[:200]), level="ERROR", tag="chat")
                    # Responses 协议靠终态事件收尾：只关流会让客户端一直等。
                    try:
                        self._sse_write(_responses_failed_frame(
                            holder,
                            _client_error_status(_to_int_status(_pst), _etype),
                            msg))
                    except Exception:
                        pass
                    self._sse_end()
                    return
                self._sse_end()
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, holder.get("usage"), stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms)
                             if first_ms is not None else None,
                             fp=fp, account=account.uid,
                             effort=usage_ctx.get("effort"),
                             effort_requested=usage_ctx.get("effort_requested"),
                             key_id=self._key_id_for_usage(),
                             realm_reason=usage_ctx.get("realm_reason", ""),
                             session_trace=usage_ctx)
                return
            try:
                chat_obj, account = aggregate_with_envelope_retry(
                    upstream, chat_req, session_key, req_realm, model,
                    holder, account, usage_ctx=usage_ctx)
            except UpstreamStatus as exc:
                record_error(model, exc.status, exc.detail,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             stream=want_stream,
                             account=account.uid if account else None,
                             key_id=self._key_id_for_usage())
                msg, etype = friendly_upstream_error(_to_int_status(exc.status),
                                                     exc.detail)
                return self._error(exc.status if str(exc.status).isdigit() else 502,
                                   msg, etype)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             stream=want_stream,
                             account=account.uid if account else None,
                             key_id=self._key_id_for_usage())
                return self._error(502, "upstream stream error: %s" % exc)
            wall = int((time.time() - t_start) * 1000)
            result = chat_to_response(chat_obj, model, custom_names,
                                       ns_map=tool_ns,
                                       allowed_names=_tool_names_from_payload(chat_req))
            record_usage(model, chat_obj.get("usage"), stream=False,
                         elapsed_ms=wall, fp=fp, account=account.uid,
                         effort=usage_ctx.get("effort"),
                         effort_requested=usage_ctx.get("effort_requested"),
                         key_id=self._key_id_for_usage(),
                         realm_reason=usage_ctx.get("realm_reason", ""),
                         session_trace=usage_ctx)
            return self._json(200, result)

    def do_POST(self):
        # #26：同 do_GET —— 每次请求入口清空，避免 keep-alive 复用实例时把上一次
        # chat 的出口/原因带到别的响应上。
        self.realm_used = ""
        self.realm_reason = ""
        _CLIENT_CTX.key_entry = None
        # issue #56：记录客户端 IP（团队账号「是否被共用」统计用）
        try:
            _CLIENT_CTX.ip = self.client_address[0]
        except Exception:
            pass
        path = self.path.split("?")[0]
        if path.startswith("/admin/"):
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            body = self._payload_or_error()
            if body is None:
                return
            return self._handle_admin(path, body)
        if path == "/accounts/note":
            # issue #69：账号备注（面板内直接编辑；纯展示字段，不参与路由判定）
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            try:
                body = self._read_payload()
            except Exception as exc:
                return self._error(400, "invalid json: %s" % exc,
                                   "invalid_request_error")
            uid = str((body or {}).get("uid") or "").strip()
            note = str((body or {}).get("note") or "").strip()[:120]
            if not uid:
                return self._error(400, "uid is required",
                                   "invalid_request_error")
            if not POOL:
                return self._error(503, "account pool not ready",
                                   "invalid_request_error")
            hit = None
            for acc in POOL.accounts:
                if acc.uid == uid:
                    hit = acc
                    break
            if hit is None:
                return self._error(404, "account not found: %s" % uid,
                                   "invalid_request_error")
            hit.note = note
            try:
                hit.save(ACCOUNTS_DIR)
            except Exception as exc:
                return self._error(500, "save failed: %s" % exc)
            return self._json(200, {"ok": True, "uid": uid, "note": note})
        if path == "/update/apply":
            # 一键更新：下载官方 release -> 校验 -> 备份 -> 替换 -> 自动重启。
            # 只允许面板会话 + 本机（可用 QD_UPDATE_ALLOW_REMOTE=1 放开）。
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            if UPDATE_DISABLED:
                return self._error(403, "update is disabled (QD_UPDATE_DISABLE=1)",
                                   "invalid_request_error")
            if qoder_updater._in_container():
                return self._error(409,
                                   "运行在容器里：镜像重建会覆盖本地改动，请在宿主机"
                                   "执行 docker compose pull && docker compose up -d",
                                   "invalid_request_error")
            if not _update_allow_remote() and not _is_loopback(
                    (self.client_address or ["?"])[0]):
                return self._error(403, "update is loopback-only "
                                        "(set QD_UPDATE_ALLOW_REMOTE=1 to allow)",
                                   "invalid_request_error")
            try:
                res = apply_update_now()
            except Exception as exc:
                return self._error(500, "update failed: %s" % exc)
            if not res.get("ok"):
                return self._json(200, res)
            restarted = qoder_updater.schedule_restart(_LAUNCH_ARGV, delay=1.5)
            log("update: %s -> %s (%d files, backup=%s), restarting=%s"
                % (res.get("from"), res.get("to"), len(res.get("files") or []),
                   res.get("backup") or "-", restarted), tag="update")
            return self._json(200, dict(res, restarting=restarted))
        if path == "/settings/tool-echo-mode":
            # issue #135：strict / lenient 面板切换（立即生效，无需重启）
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            try:
                body = self._read_payload()
            except Exception as exc:
                return self._error(400, "invalid json: %s" % exc,
                                   "invalid_request_error")
            mode = str((body or {}).get("mode") or "").strip().lower()
            if mode not in ("strict", "lenient", ""):
                return self._error(400, "mode must be strict or lenient",
                                   "invalid_request_error")
            try:
                saved = qoder_settings.set_tool_echo_mode(ACCOUNTS_DIR, mode)
            except Exception as exc:
                return self._error(500, "save failed: %s" % exc)
            log("tool echo mode set to %s (panel)" % (saved or "(default)"),
                tag="panel")
            return self._json(200, {"ok": True, "tool_echo_mode": saved,
                                    "effective": tool_echo_mode()})
        if path == "/settings/anthropic-thinking":
            # issue #146：Anthropic thinking block 模式切换（empty / off / "" 清除）
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            try:
                body = self._read_payload()
            except Exception as exc:
                return self._error(400, "invalid json: %s" % exc,
                                   "invalid_request_error")
            mode = str((body or {}).get("mode") or "").strip().lower()
            if mode not in ("empty", "off", ""):
                return self._error(400, "mode must be empty, off, or empty string",
                                   "invalid_request_error")
            try:
                saved = qoder_settings.set_anthropic_thinking(ACCOUNTS_DIR, mode)
            except Exception as exc:
                return self._error(500, "save failed: %s" % exc)
            log("anthropic thinking mode set to %s (panel)" % (saved or "(default)"),
                tag="panel")
            return self._json(200, {"ok": True, "anthropic_thinking": saved,
                                    "effective": anthropic_thinking_mode()})
        if path == "/settings/model-context":
            # issue #106：为某个模型设定上下文档（label="" 清除回到官方默认）
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            try:
                body = self._read_payload()
            except Exception as exc:
                return self._error(400, "invalid json: %s" % exc,
                                   "invalid_request_error")
            model = str((body or {}).get("model") or "").strip()
            label = (body or {}).get("label")
            if not model:
                return self._error(400, "model required", "invalid_request_error")
            label = str(label).strip() if label not in (None, "") else ""
            if label.isdigit():
                label = int(label)
            try:
                table = qoder_settings.set_model_context(ACCOUNTS_DIR, model, label)
            except Exception as exc:
                return self._error(500, "save failed: %s" % exc)
            log("model context set: %s -> %s" % (model, label or "(default)"),
                tag="panel")
            return self._json(200, {"ok": True, "model_context": table})
        if path == "/settings/web-tools":
            # 本地 web 工具开关（面板 UI）：默认关，开/关都立即生效
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            try:
                body = self._read_payload()
            except Exception as exc:
                return self._error(400, "invalid json: %s" % exc,
                                   "invalid_request_error")
            enabled = bool((body or {}).get("enabled"))
            try:
                qoder_settings.set_web_tools_enabled(ACCOUNTS_DIR, enabled)
            except Exception as exc:
                return self._error(500, "save failed: %s" % exc)
            log("local web tools %s (panel)" % ("enabled" if enabled else "disabled"),
                tag="webtools")
            return self._json(200, {"ok": True, "web_tools": web_tools_enabled()})
        if path == "/credits/probe":
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._handle_credits_probe(payload)
        if path == "/settings/save":
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            return self._handle_settings_save()
        if path in ("/panel/login", "/panel/logout", "/panel/password"):
            return self._handle_panel(path)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required",
                               "invalid_request_error")
        if path in ("/agents/apply", "/agents/restore"):
            # 写**网关所在机器**的客户端配置文件：面板会话不够，还必须来自
            # loopback（远程/容器调用按设计拒绝——那是配置器 CLI 的活）。
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            if not self._client_is_loopback():
                return self._error(
                    403,
                    "agent config writes are loopback-only: run the "
                    "configurator CLI on the machine where the client "
                    "lives (curl /agents/cli)",
                    "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            try:
                client_id = str(payload.get("client") or "").strip()
                base_url = str(payload.get("base_url") or "").strip()
                api_key = str(payload.get("api_key") or "").strip()
                model = str(payload.get("model") or "").strip() or None
                if path == "/agents/apply":
                    models = None
                    if payload.get("fetch_models"):
                        models = qoder_agents.fetch_models_from_gateway(
                            base_url, api_key)
                    result = qoder_agents.integrate(
                        ACCOUNTS_DIR, client_id, base_url, api_key,
                        model=model, models=models)
                else:
                    result = qoder_agents.restore(ACCOUNTS_DIR, client_id)
                return self._json(200, result)
            except qoder_agents.AgentConfigError as exc:
                return self._error(400, str(exc), "invalid_request_error")
        is_account_route = (
            path.startswith("/accounts/")
            or path == "/realm"
            or path.startswith("/tasks")
            or path.startswith("/scheduler")
            or path.startswith("/logs")
        )
        if not is_account_route and path not in (
                "/v1/chat/completions", "/chat/completions",
                "/v1/responses", "/responses",
                # Anthropic Messages（task-78 接线）：两条都要进白名单，
                # 否则会在 404 分支被拦掉（CORS 无需另加，"/v1" 前缀已覆盖）。
                "/v1/messages", "/v1/messages/count_tokens",
                # issue #123：签到诊断（只读，面板鉴权）
                "/diag/checkin", "/diag/queue", "/credits/probe"):
            if path in ("/v1/completions", "/completions"):
                # legacy Completions（prompt 而非 messages）本网关不实现：
                # 明确 404 优于"接受请求却按空会话转发上游"。
                return self._error(
                    404,
                    "the legacy /v1/completions API is not supported here: "
                    "this gateway speaks Chat Completions and Responses. "
                    "Use /v1/chat/completions with messages=[...] instead.",
                    "invalid_request_error")
            return self._unknown_path_error(path)
        if not self._authorized():
            return
        payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
        if payload is None:
            return
        if path == "/diag/queue":
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            return self._handle_diag_queue(payload)
        if path == "/diag/checkin":
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            return self._handle_diag_checkin(payload)
        if is_account_route:
            return self._handle_accounts(path, payload)
        # 本地 web 工具：客户端声明了内置 web_search / web_fetch 时由网关代跑
        if path in ("/v1/chat/completions", "/chat/completions",
                    "/v1/responses", "/responses") and web_tools_enabled():
            _web_names = builtin_web_tool_names(payload.get("tools") or [])
            if _web_names:
                return self._handle_web_tools(path, payload, _web_names)
        if path in ("/v1/responses", "/responses"):
            return self._handle_responses(payload)
        if path in ("/v1/messages", "/v1/messages/count_tokens"):
            return self._handle_anthropic(path, payload)

        # ---- Chat Completions 主链路 ----
        normalize_tool_choice(payload)
        normalize_tools(payload)
        translate_max_completion_tokens(payload)
        model = payload.get("model") or "auto"
        payload.setdefault("stream", False)
        # 保持原始终端 stream 意图
        want_stream = bool(payload.get("stream"))
        # 转换统一在 build_qoder_body 内做（压平/清洗/工具）
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(payload.get("messages"))
        t_start = time.time()
        effort = requested_effort(payload)   # 与 usage 记账、请求体构造同源
        self._apply_context_header(payload)  # issue #106：X-QD-Context 抬窗
        log("chat: model=%s client_effort=%r stream=%s msgs=%d"
            % (model, effort, want_stream,
               len(payload.get("messages") or [])))
        # account 预置在 try 之外：下面所有 except 里的 record_error 都会引用它，
        # 而选号成功前它就是 None。必须在任何可能抛异常的语句之前。
        account = None
        try:
            req_realm = self._request_realm()
            usage_ctx = {"tenant_scope": self._key_id_for_usage(), "_key_entry": getattr(self, "key_entry", None)}          # P1-3：推理档位审计出参（下传给 open_upstream）
            blocked = self._model_precheck(model, req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            upstream, account, _ = open_upstream(payload, session_key=session_key,
                                                 target_realm=req_realm,
                                                 usage_ctx=usage_ctx)
            # #26：记录实际出口（自动路由后可能与 req_realm 不同）→ 响应头可见
            self.realm_used = usage_ctx.get("realm") or req_realm
            self.realm_reason = usage_ctx.get("realm_reason") or ""
        except UpstreamQueued as exc:
            # issue #87：排队——visible 在流内等待并持续报告进度；
            # fast / 非流式给 503 + Retry-After + 中文原因。
            _msg, _et = friendly_upstream_error(503, exc.detail)
            if not want_stream or queue_mode() != "visible":
                return self._error(503, _msg, _et, retry_after=exc.retry_after)
            self._sse_begin()
            _q0 = time.time()

            def _emit_q(kind, info):
                info = dict(info or {})
                info["model"] = model
                if kind == "queue":
                    self._queue_notice("chat", info)
                else:
                    self._queue_error_frame(
                        "chat", info.get("message") or _msg,
                        info.get("waited_seconds") or int(time.time() - _q0),
                        info.get("budget_seconds") or QUEUE_WAIT_BUDGET_SECS)

            _got = visible_queue_acquire(
                exc,
                lambda: open_upstream(payload, session_key=session_key,
                                      target_realm=req_realm,
                                      usage_ctx=usage_ctx),
                _emit_q, started=_q0)
            if _got is None:
                self._sse_end()
                return
            upstream, account, _ = _got
            self.realm_used = usage_ctx.get("realm") or req_realm
            self.realm_reason = usage_ctx.get("realm_reason") or ""
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            return self._rate_limited(exc)
        except UpstreamStatus as exc:
            # issue #59：PayloadTooLarge(413) 等**上游未收到**的错误走这里——
            # 明确状态码 + 可读文案，且不进入账号冷却/重试链路。
            _code = _to_int_status(exc.status)
            record_error(model, _code, exc.detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            _msg, _etype = friendly_upstream_error(_code, exc.detail)
            return self._error(_code, _msg, _etype)
        except urllib.error.HTTPError as exc:
            # 错误体在 open_upstream 中已读过（挂在 qoder_detail），二次 read
            # 会拿到残缺内容——优先取挂载值。
            detail = getattr(exc, "qoder_detail", "")
            if not detail:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            msg, etype = friendly_upstream_error(exc.code, detail)
            return self._error(exc.code, msg, etype)
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         stream=want_stream,
                         account=account.uid if account else None,
                         key_id=self._key_id_for_usage())
            if message.startswith("no usable account"):
                return self._error(503, message
                                   + " - add or enable one at the dashboard (/)")
            return self._error(502, "upstream unreachable: %s" % exc)

        with upstream:
            holder = {"usage": None}
            if want_stream:
                self._sse_begin()
                emitted = False
                first_ms = None
                # 流内信封重试（同上：200 建流后信封投 418 的形态），仅在
                # 尚未向客户端写出任何上游字节时重开。
                attempts = 0
                cur = upstream
                pump_exc = None
                # issue #83: cross-realm failover at most once per request
                _switched71 = False
                try:
                    while True:
                        try:
                            for line in sse_with_heartbeat(
                                    recover_leaked_tool_calls(
                                        iter_inner_sse(cur, holder=holder),
                                        allowed_names=_tool_names_from_payload(payload), audit=usage_ctx),
                                    self._sse_write):
                                if first_ms is None:
                                    first_ms = int((time.time() - t_start) * 1000)
                                emitted = True
                                self._sse_write(line)
                            break
                        except (BrokenPipeError, ConnectionResetError,
                                ConnectionAbortedError):
                            # 客户端断开；上游已产出的部分照常记账。
                            wall = int((time.time() - t_start) * 1000)
                            record_usage(model, holder.get("usage"), stream=True,
                                         elapsed_ms=wall, ttft_ms=first_ms,
                                         gen_ms=(wall - first_ms)
                                         if first_ms is not None else None,
                                         fp=fp, account=account.uid,
                                         effort=usage_ctx.get("effort"),
                                         effort_requested=usage_ctx.get("effort_requested"),
                                         key_id=self._key_id_for_usage(),
                                         realm_reason=usage_ctx.get("realm_reason", ""),
                                         session_trace=usage_ctx)
                            return
                        except UpstreamStatus as exc:
                            _handle_envelope_account_cooldown(account, exc, model=model, session_key=session_key)
                            if should_retry_envelope(exc, emitted, attempts, usage_ctx=usage_ctx):
                                attempts += 1
                                log("chat in-stream envelope status %s on "
                                    "model '%s' (try %d/%d), reopening upstream"
                                    % (exc.status, model,
                                       min(attempts + 1, _queue_budget()),
                                       _queue_budget()),
                                    level="WARN", tag="chat")
                                time.sleep(_envelope_retry_wait(exc, attempts))
                                if _is_image_conversion_timeout(exc.detail):
                                    cur.close()  # Release team concurrency before reopening.
                                try:
                                    cur2, account, _ = open_upstream(
                                        payload, session_key=session_key,
                                        target_realm=req_realm,
                                        usage_ctx=usage_ctx,
                                        **({"only_account": account} if
                                           _is_image_conversion_timeout(exc.detail) else {}))
                                except Exception as rex:
                                    log("chat reopen failed: %s"
                                        % str(rex)[:160], level="WARN")
                                    pump_exc = exc
                                    break
                                if cur is not upstream:
                                    try:
                                        cur.close()
                                    except Exception:
                                        pass
                                cur = cur2
                                continue
                            # issue #71：排队耗尽 -> 先试另一区（同一模型、区域级排队），
                            # 仍不行再转成 429 + Retry-After，而不是把 403 丢给客户端。
                            _alt71 = ("" if self.headers.get("X-Realm") else
                                      _alt_realm_ready(req_realm, model))
                            if (_envelope_queue_wait(exc) > 0 and _alt71
                                    and not _switched71):
                                log("queue exhausted -> failing over to %s (issue #71)"
                                    % _alt71, level="WARN", tag="chat")
                                _switched71 = True
                                try:
                                    cur2, account, _ = open_upstream(
                                        payload, session_key=session_key,
                                        target_realm=_alt71,
                                        usage_ctx=usage_ctx)
                                except Exception as rex:
                                    log("cross-realm reopen failed: %s"
                                        % str(rex)[:120], level="WARN")
                                    pump_exc = _as_rate_limited_for_queue(exc, model)
                                    break
                                if cur is not upstream:
                                    try:
                                        cur.close()
                                    except Exception:
                                        pass
                                cur = cur2
                                continue
                            pump_exc = _as_rate_limited_for_queue(exc, model)
                            break
                finally:
                    if cur is not upstream:
                        try:
                            cur.close()
                        except Exception:
                            pass
                if pump_exc is not None:
                    exc = pump_exc
                    # issue #100：同上——RateLimited 没有 .status
                    _cst = _exc_http_status(exc, 429)
                    wall = int((time.time() - t_start) * 1000)
                    record_error(model, _cst, exc.detail,
                                 elapsed_ms=wall, stream=True,
                                 account=account.uid if account else None,
                                 key_id=self._key_id_for_usage())
                    msg, etype = friendly_upstream_error(
                        _to_int_status(_cst), exc.detail)
                    err = json.dumps({"error": {
                        "message": msg, "type": etype,
                        "code": _client_error_status(_cst, etype)}},   # issue #137
                        ensure_ascii=False)
                    try:
                        self._sse_write(("data: %s\n\n" % err).encode("utf-8"))
                        self._sse_write(b"data: [DONE]\n\n")
                    except Exception:
                        pass
                    self._sse_end()
                    return
                if not emitted:
                    err = json.dumps({"error": {
                        "message": "empty upstream stream",
                        "type": "server_error"}})
                    self._sse_write(("data: %s\n\n" % err).encode("utf-8"))
                self._sse_write(b"data: [DONE]\n\n")
                self._sse_end()
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, holder.get("usage"), stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms)
                             if first_ms is not None else None,
                             fp=fp, account=account.uid,
                             effort=usage_ctx.get("effort"),
                             effort_requested=usage_ctx.get("effort_requested"),
                             key_id=self._key_id_for_usage(),
                             realm_reason=usage_ctx.get("realm_reason", ""),
                             session_trace=usage_ctx)
                return
            try:
                result, account = aggregate_with_envelope_retry(
                    upstream, payload, session_key, req_realm, model,
                    holder, account, usage_ctx=usage_ctx)
            except UpstreamStatus as exc:
                record_error(model, exc.status, exc.detail,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             stream=want_stream,
                             account=account.uid if account else None,
                             key_id=self._key_id_for_usage())
                code = exc.status if str(exc.status).isdigit() else 502
                try:
                    code = int(code)
                except Exception:
                    code = 502
                if code < 400 or code > 599:
                    code = 502
                msg, etype = friendly_upstream_error(
                    _to_int_status(exc.status), exc.detail)
                return self._error(code, msg, etype)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             stream=want_stream,
                             account=account.uid if account else None,
                             key_id=self._key_id_for_usage())
                return self._error(502, "upstream stream error: %s" % exc)
            wall = int((time.time() - t_start) * 1000)
            first_at = result.get("first_chunk_at")
            first_ms = int((first_at - t_start) * 1000) if first_at else None
            record_usage(model, result.get("usage"), stream=False,
                         elapsed_ms=wall, ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid,
                         effort=usage_ctx.get("effort"),
                         effort_requested=usage_ctx.get("effort_requested"),
                         key_id=self._key_id_for_usage(),
                         realm_reason=usage_ctx.get("realm_reason", ""),
                         session_trace=usage_ctx)
            return self._json(200, result)


def acc_realm(account):
    return account.realm if account else CURRENT_REALM


def _keepalive_send(entry):
    """保温一次：同 payload、同账号、同 session_id，stream=False + max_tokens=1。

    由 qoder_keepalive 后台线程在会话闲置超过 QD_CACHE_KEEPALIVE_INTERVAL
    后调用（默认 240s，小于上游 ~5min TTL）。session_key 传登记时的最终
    session_id，open_upstream 会把它当作窗口键原样回填 —— 与原请求同一个
    会话身份；前缀逐字节相同 → 命中上游缓存并续期，消耗 ≈ 一次缓存读。
    记账 key_id=keepalive，面板可见可审计。失败抛异常由 tick 淘汰该会话。
    """
    acc = POOL.get(entry["account_uid"]) if POOL else None
    if acc is None:
        raise LookupError("keepalive: account %s unavailable"
                          % (entry["account_uid"] or "")[:8])
    payload = dict(entry["payload"])
    payload["stream"] = False
    payload["max_tokens"] = 1
    usage_ctx = {}
    resp, used, _enc = open_upstream(payload, session_key=entry["session_id"],
                                     target_realm=entry["realm"] or None,
                                     only_account=acc, usage_ctx=usage_ctx)
    try:
        obj = aggregate_stream(resp, entry["model"], holder={"usage": None})
    finally:
        try:
            resp.close()
        except Exception:
            pass
    usage = obj.get("usage") or {}
    if usage:
        try:
            record_usage(entry["model"], usage, stream=False,
                         account=(used.uid if used else None)
                         or entry["account_uid"],
                         key_id="keepalive",
                         realm_reason=usage_ctx.get("realm_reason", ""),
                         session_trace=usage_ctx)
        except Exception:
            pass
    return usage


def main():
    global POOL, ACCOUNTS_DIR, API_KEY, SYSTEM_PROMPT, USAGE_DIR, USAGE_LOG, \
        USAGE_SUMMARY, SCHEDULER
    API_KEY_GENERATED = False
    ap = argparse.ArgumentParser(
        description="Qoder (qoder.com.cn / qoder.com) -> OpenAI-compatible proxy")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    # 8788 被 mimo-api-proxy.mjs 占用，8789 是 wb-proxy 默认端口；
    # Qoder 网关默认 8790。Docker 显式传 --port 8790。
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or "8790"))
    ap.add_argument("--lan", action="store_true",
                    help="listen on every interface so other devices on the "
                         "LAN can reach it (implies --host 0.0.0.0 and forces "
                         "an api key)")
    ap.add_argument("--api-key",
                    default=os.environ.get("API_KEY")
                    or os.environ.get("QD_PROXY_KEY") or None,
                    help="require this bearer token on /v1/* (optional)")
    ap.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT,
                    help="system message used when the request has none "
                         "(default: keep the official Qoder template prompt)")
    ap.add_argument("--usage-dir", default=None,
                    help="where to store usage.jsonl / usage-summary.json "
                         "(default: ./usage)")
    ap.add_argument("--accounts-dir",
                    default=os.environ.get("ACCOUNTS_DIR") or None,
                    help="where the per-account credential files live "
                         "(default: ./accounts)")
    ap.add_argument("--panel-password", default=None,
                    help="set the web panel password on startup (default: "
                         "admin)")
    ap.add_argument("--pid-file", default=None,
                    help="write the server PID to this file (stop.bat uses it; "
                         "removed on exit)")
    args = ap.parse_args()
    if qoder_accounts.vpc_config_error():
        sys.stderr.write(
            "[ERROR] invalid QD_CN_VPC_ENDPOINT: %s\n"
            "        expected an instance name (e.g. acme) or one of\n"
            "        acme.vpc.qoder.com.cn / acme-openapi.vpc.qoder.com.cn /\n"
            "        acme-gateway.vpc.qoder.com.cn\n"
            % qoder_accounts.vpc_config_error())
        raise SystemExit(1)
    qoder_logpaths.install_stream_logging(os.path.dirname(os.path.abspath(__file__)))
    if not args.usage_dir and not os.environ.get("QD_PROXY_USAGE_DIR"):
        _migration = qoder_logpaths.migrate_legacy_usage(
            os.path.dirname(os.path.abspath(__file__)), USAGE_DIR)
        if _migration["copied"] or _migration["conflicts"]:
            log("legacy usage preserved: copied=%s conflicts=%s" %
                (_migration["copied"], _migration["conflicts"]), tag="system")

    # issue #50：把 PID 落盘，交给 stop.bat 精确停止（只认这个 PID + 命令行
    # 校验，绝不做「杀掉所有 python」那种模糊匹配）。
    if args.pid_file:
        try:
            with open(args.pid_file, "w", encoding="utf-8") as _pf:
                _pf.write(str(os.getpid()))

            def _drop_pid_file(path=args.pid_file):
                try:
                    os.remove(path)
                except Exception:
                    pass

            atexit.register(_drop_pid_file)
        except Exception as _exc:
            log("pid file write failed (%s): %s" % (args.pid_file, _exc),
                level="WARN")

    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"
    if args.usage_dir:
        USAGE_DIR = os.path.abspath(args.usage_dir)
        USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
        USAGE_SUMMARY = os.path.join(USAGE_DIR, "usage-summary.json")

    # 拒绝启动第二份：Windows 上 SO_REUSEADDR 会让两个 socket 绑同一端口，
    # 连接被静默分流，极难诊断。
    #
    # issue #89：只问「有没有人应答 /health」会把「刚收到 SIGTERM、还在收尾的旧
    # 实例」误判成健康双开——升级（kill 后立刻 start）时几乎必撞。现在按 应答者
    # 的 pid 是否还活着 分类，并对「端口尚未释放」做有限重试。
    def _probe_health(timeout=2.0):
        try:
            url = "http://%s:%d/health" % (
                "127.0.0.1" if args.host == "0.0.0.0" else args.host,
                args.port)
            with urllib.request.urlopen(url, timeout=timeout) as probe:
                return json.loads(probe.read().decode("utf-8"))
        except Exception:
            return None

    existing = None
    for _attempt in range(3):
        existing = _probe_health()
        if classify_existing_instance(existing) != "stale":
            break
        print()
        print("  [WARN] %d 端口仍有人在应答 /health，但它的进程已经退出"
              % args.port)
        print("         （旧实例正在释放端口）。继续启动新实例：等待 %d/3 ..."
              % (_attempt + 1))
        sys.stdout.flush()
        time.sleep(1.2)
        existing = None
    _kind = classify_existing_instance(existing)
    if _kind == "foreign":
        who = (existing or {}).get("service") or "an unknown HTTP service"
        print()
        print("  [ERROR] port %d is already taken by another program: %s"
              % (args.port, who))
        print("          qd-proxy itself is NOT running - nothing was "
              "started.")
        print()
        print("  Fix: start qoder-proxy on a different port, e.g.")
        print("          %s %d" % (START_SCRIPT, args.port + 1))
        print("          python qoder_proxy.py --port %d" % (args.port + 1))
        print()
        for cmd in port_owner_hint(args.port):
            print("  Check who owns the port:  %s" % cmd)
        print()
        raise SystemExit(1)
    if _kind == "healthy":
        print()
        print("  [已有一个反代在 %d 端口运行，无需重复启动]" % args.port)
        print("  PID: %s · 版本: %s · 启动于: %s"
              % (existing.get("pid") or "?", existing.get("version") or "?",
                 existing.get("started_iso") or "?"))
        print("  账号: %s @ %s" % (existing.get("uid", "?"),
                                   existing.get("domain", "?")))
        print("  看板: http://127.0.0.1:%d/" % args.port)
        print()
        print("  如果要重启: 用 stop 脚本（Windows: stop-qoder-proxy.bat /"
              " Linux·macOS: ./stop-qoder-proxy.sh），再启动本程序。")
        print()
        return

    API_KEY = args.api_key
    SYSTEM_PROMPT = args.system_prompt
    if args.accounts_dir:
        ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    # LAN 模式绝不能带默认密钥：网关花的是账号自己的上游额度，
    # 可猜的默认值等于让全网段的人白嫖。首次生成一次并持久化。
    if args.lan and not API_KEY:
        API_KEY, API_KEY_GENERATED = qoder_settings.ensure_launcher_key(
            ACCOUNTS_DIR)
    # 面板保存的 Key 优先于自动生成的 LAN Key（.bat 重启后浏览器改动仍生效）；
    # 命令行 --api-key 依然最高优先级。
    global API_KEY_FILE_SET
    saved_key, key_from_panel = qoder_settings.api_key_override(ACCOUNTS_DIR)
    if key_from_panel and not args.api_key:
        API_KEY = saved_key
        API_KEY_FILE_SET = True
    if args.panel_password:
        qoder_settings.set_panel_password(ACCOUNTS_DIR, args.panel_password)
        log("panel      : password set from --panel-password")
    elif qoder_settings.panel_password_is_default(ACCOUNTS_DIR):
        log("panel      : password is still the default 'admin' - change it "
            "in the panel")
        if args.host == "0.0.0.0":
            log("SECURITY   : LAN mode + default panel password 'admin' - "
                "anyone on this network can log in and read your API key. "
                "Change it in the panel, or restart with --panel-password.",
                level="WARN")

    POOL = qoder_accounts.AccountPool(ACCOUNTS_DIR, log=log)
    POOL.load()
    load_persisted_realm()
    # task-61：把设置里的护栏阈值 + free 名单**同批**推给池（R3）；阈值全 0 时
    # 不产生任何副作用，任一 > 0 则惰性启动后台上新线程（G1 的恢复路径）。
    try:
        POOL.apply_settings(limits_data=qoder_settings.limits_data(ACCOUNTS_DIR),
                            free_models=free_model_names(CURRENT_REALM))
    except Exception as exc:
        log("quota limits apply failed at startup: %s" % exc, level="WARN")
    # issue #43：余额刷新与护栏**解耦**——护栏阈值全 0 时也要能刷新「用过的号」。
    # 默认 5 分钟合并一次，空闲不轮询（QD_CREDITS_USAGE_INTERVAL 可调）。
    global CREDITS_USAGE_REFRESHER
    try:
        CREDITS_USAGE_REFRESHER = qoder_accounts.CreditsUsageRefresher(
            POOL, log=log)
        CREDITS_USAGE_REFRESHER.start()
        log("credits usage refresher started (interval=%ds)"
            % CREDITS_USAGE_REFRESHER.interval_seconds, tag="credits")
    except Exception as exc:
        CREDITS_USAGE_REFRESHER = None
        log("credits usage refresher failed to start: %s" % exc, level="WARN")
    # issue #57：容器出口反代**国际版**容易被上游风控（聊天变排队）。检测到
    # 容器 + intl 账号时给出明确告警——不阻断，只提示换自建出口或改用国内版。
    try:
        _intl = [a for a in (POOL.accounts if POOL else [])
                 if getattr(a, "realm", "") == "intl" and a.enabled]
        if _intl and _in_container():
            log("container egress + intl accounts detected (%d): upstream may "
                "rate-limit or queue the intl gateway from datacenter IPs. "
                "Prefer a residential egress or the CN realm (issue #57)."
                % len(_intl), level="WARN", tag="realm")
    except Exception:
        pass
    # issue #64：容器/部署自检——时钟漂移会让 COSY 签名直接 401，
    # 账户目录不可写会让设备身份无法持久（重建即换设备）。
    try:
        _skew = _measure_clock_skew()
        _env = env_report()
        log("env: container=%s proxy=%s accounts_writable=%s identity_cached=%s "
            "clock_skew=%ss traffic_guard=%s"
            % (_env["container"], _env["proxy"], _env["accounts_writable"],
               _env["identity_cached"],
               "?" if _skew is None else _skew, _env["traffic_guard"]),
            tag="realm")
        if _skew is not None and abs(_skew) > 120:
            log("clock skew %ss detected: COSY signatures are time-based and will "
                "fail with 401 - fix container time sync (issue #64)" % _skew,
                level="WARN", tag="realm")
        if not _env["accounts_writable"]:
            log("accounts dir is not writable: device identity cannot persist, "
                "container rebuild will look like a new device (issue #18)",
                level="WARN", tag="realm")
    except Exception:
        pass
    from qoder_scheduler import Scheduler
    SCHEDULER = Scheduler(POOL)
    SCHEDULER.start()

    if not POOL.accounts:
        # 永不静默采用本机客户端登录：先报告扫描结果，由用户在看板确认导入。
        try:
            detected = qoder_accounts.scan_desktop_credentials()
        except Exception:
            detected = []
        usable = [d for d in detected if d.get("valid")]
        if usable:
            log("no accounts yet - detected %d local credential(s), NOT importing"
                % len(usable))
            for d in usable:
                log("  available: %s  %s  %s" % (
                    (d.get("uid") or "?")[:8], d.get("nickname") or "(no name)",
                    d.get("realmName") or d.get("realm")))
            log("open the dashboard and click [Scan local credentials] to import")
        else:
            log("no accounts yet - no local Qoder credentials found on this machine")
        # 不在这里退出：看板必须可达，才能通过浏览器完成登录。
    rep = current_account()
    log("accounts   : %d total, %d usable"
        % (len(POOL.accounts), POOL.count_ready()))
    for account in POOL.accounts:
        log("  - %s  %s  %s  %s" % (
            account.uid[:8], account.nickname or "(no name)",
            account.realm, account.domain))
    log("store      : %s" % ACCOUNTS_DIR)
    log("credential : %s" % (rep.path if rep else "-"))
    log("account    : %s @ %s" % (rep.uid if rep else "-",
                                  rep.domain if rep else "-"))
    log("realm      : %s (%s)" % (
        CURRENT_REALM,
        get_realm_config(CURRENT_REALM).get("domain") or "qoder.com.cn"))
    if qoder_accounts.vpc_configured():
        _cn = qoder_accounts.get_realm_config("cn")
        log("vpc        : %s (openapi=%s gateway=%s)"
            % (qoder_accounts.vpc_instance(), _cn["openapi"], _cn["gateway"]),
            tag="realm")
        if CURRENT_REALM != "cn":
            log("VPC endpoint is CN-only, but the current realm is %s - "
                "switch to CN in the dashboard or set QD_PROXY_DEFAULT_REALM=cn"
                % CURRENT_REALM, level="WARN", tag="realm")
    log("catalog    : %s" % (BASEPROMPT_PATH if BASEPROMPT
                             else "baseprompt.json MISSING"))

    if args.host == "0.0.0.0":
        ips = local_ip_addresses() or ["<this-pc-ip>"]
        print()
        print("  " + "=" * 62)
        print("  LAN MODE - reachable from other devices")
        print()
        for ip in ips:
            print("    API       : http://%s:%s/v1" % (ip, args.port))
            print("    Dashboard : http://%s:%s/" % (ip, args.port))
        print()
        print("    API Key   : %s" % API_KEY)
        if API_KEY_GENERATED:
            print("                (newly generated & saved to "
                  "accounts/settings.json)")
        else:
            print("                (reused from accounts/settings.json)")
        print()
        print("    Open the dashboard (key already included):")
        print("      http://%s:%s/?key=%s" % (ips[0], args.port, API_KEY))
        print()
        print("    Clients: Base URL = the API address above, then paste "
              "the key.")
        print()
        if IS_WINDOWS:
            print("    If nothing can connect, allow python through the")
            print("    firewall: run %s once as administrator." % FIREWALL_SCRIPT)
        else:
            print("    If nothing can connect, open the port in the firewall")
            print("    once:  sudo ./%s %d" % (FIREWALL_SCRIPT, args.port))
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

    if not POOL.accounts:
        print()
        print("  " + "=" * 62)
        print("  NO ACCOUNTS YET")
        print()
        print("  Open the dashboard and click [+ 添加账号 (OAuth)]:")
        print("      http://127.0.0.1:%d/" % args.port)
        print()
        print("  The browser flow adds the account automatically.")
        print("  This window must stay open.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

    # issue #89：旧实例刚退出时端口可能还没释放——有限重试，并在此期间复查
    # /health：如果是**健康**实例起来了，就按「已在运行」退出而不是报错。
    server = None
    for _attempt in range(3):
        try:
            server = _ExclusiveHTTPServer((args.host, args.port), Handler)
            break
        except OSError as exc:
            _kind2 = classify_existing_instance(_probe_health(1.0))
            if _kind2 == "healthy":
                print()
                print("  [已有一个反代在 %d 端口运行，无需重复启动]" % args.port)
                print("  看板: http://127.0.0.1:%d/" % args.port)
                print()
                return
            # A failed TCP probe cannot prove TIME_WAIT: a listener may be
            # bound but not accepting, filtered, or start after the probe.
            # Keep exclusive binding on every retry; never fall back to reuse.
            if _attempt < 2:
                print("  [WARN] bind %s:%d 失败（%s）——端口可能仍在释放，"
                      "重试 %d/3 ..." % (args.host, args.port, exc, _attempt + 1))
                sys.stdout.flush()
                time.sleep(1.5)
                continue
            print()
            print("  [ERROR] failed to listen on %s:%d - %s"
                  % (args.host, args.port, exc))
            print("          the port is reserved or held by another program")
            print("          (possibly a qd-proxy instance that is still shutting down);")
            print("          qd-proxy did NOT start.")
            print()
            print("  Fix: wait a second and retry, stop the program holding the")
            print("       port, or pick another port:")
            for cmd in port_owner_hint(args.port):
                print("          %s" % cmd)
            print("          %s %d" % (START_SCRIPT, args.port + 1))
            print()
            raise SystemExit(1)
    log("listening  : http://%s:%d/v1  (api key: %s)"
        % (args.host, args.port, "on" if API_KEY else "off"))
    log("dashboard  : http://%s:%d/" % (args.host, args.port))

    # 启动预热（后台线程，不阻塞监听）：拉一次动态模型清单并注册显示名，
    # 让「客户端跳过 /v1/models 直接发新模型名」的第一条请求也能解析对
    # （官方改显示名窗口期，如 kmodel_latest 的 Kimi-K3 -> Kimi-K3.1-Pro）。
    # 失败静默——落盘缓存 / 本机目录缓存 / 冻结别名表仍然兜底。
    def _warm_models():
        for _r in ("cn", "intl"):
            try:
                fetch_models(realm=_r)
            except Exception:
                pass

    _warm = threading.Thread(target=_warm_models, name="qd-models-warm",
                             daemon=True)
    _warm.start()
    # 缓存保温线程（可选，默认关闭）：会话闲置超过上游缓存 TTL 前用同
    # payload 发一个 max_tokens=1 的最小请求续期，避免闲置后首发全量重写。
    if qoder_keepalive.enabled():
        qoder_keepalive.configure(_keepalive_send)
        qoder_keepalive.start_loop()
        log("cache keepalive on: interval=%ss max_age=%ss max_convs=%s"
            % (qoder_keepalive.interval(), qoder_keepalive.max_age(),
               qoder_keepalive.max_convs()), tag="keepalive")
    # 进程生命周期内保持 handler 引用：SetConsoleCtrlHandler 存的是裸指针，
    # 回调被 GC 会在关窗时崩溃。
    _ctrl_handler = install_console_close_handler()
    serve_http_server(server)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
