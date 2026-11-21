"""Opt-in, process-local DNS policy. Never disables TLS or changes system DNS.
Only explicitly allowlisted vendor hosts use authenticated DNS-over-HTTPS.
All HTTP Host headers, TLS SNI, certificate verification and proxies stay intact.
"""
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request

HOSTS = frozenset(('copilot.tencent.com', 'www.workbuddy.cn', 'www.workbuddy.ai'))
_ORIGINAL = socket.getaddrinfo
_LOCAL = threading.local()
_CACHE = {}
_LOCK = threading.Lock()
_INSTALLED = False


def enabled():
    return os.environ.get('WB_VENDOR_DOH', '').lower() in ('1', 'true', 'yes')


def _fetch(host):
    url = 'https://dns.alidns.com/resolve?name=' + urllib.parse.quote(host) + '&type=A'
    # No HTTP proxy; the OS route itself is deliberately not modified.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url, headers={'Accept': 'application/dns-json'})
    with opener.open(req, timeout=5) as response:
        data = json.loads(response.read(65536).decode('utf-8'))
    if data.get('Status') != 0:
        raise socket.gaierror('DoH response is not successful')
    ips, ttls = [], []
    for row in data.get('Answer', []):
        if row.get('type') != 1:
            continue
        address = ipaddress.ip_address(row.get('data', ''))
        if address.version != 4 or not address.is_global:
            continue
        ips.append(str(address))
        ttls.append(int(row.get('TTL') or 60))
    if not ips:
        raise socket.gaierror('DoH returned no public IPv4 addresses')
    return tuple(dict.fromkeys(ips)), max(30, min(600, min(ttls)))


def _resolve(host):
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(host)
        if cached and now < cached[1]:
            return cached[0]
    try:
        _LOCAL.resolving = True
        ips, ttl = _fetch(host)
    except Exception:
        # Bounded stale use during resolver outages, not indefinite IP pinning.
        if cached and now < cached[1] + 3600:
            return cached[0]
        raise
    finally:
        _LOCAL.resolving = False
    with _LOCK:
        _CACHE[host] = (ips, now + ttl)
    return ips


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    normalized = host.decode('ascii') if isinstance(host, bytes) else str(host)
    normalized = normalized.lower().rstrip('.')
    if not enabled() or normalized not in HOSTS or getattr(_LOCAL, 'resolving', False):
        return _ORIGINAL(host, port, family, type, proto, flags)
    # This policy currently obtains IPv4 answers only. Do not synthesize IPv6.
    if family not in (socket.AF_UNSPEC, socket.AF_INET, 0):
        return _ORIGINAL(host, port, family, type, proto, flags)
    try:
        ips = _resolve(normalized)
    except Exception as exc:
        raise socket.gaierror('vendor DNS-over-HTTPS failed: %s' % type_name(exc)) from exc
    rows = []
    for ip in ips:
        rows.extend(_ORIGINAL(ip, port, socket.AF_INET, type, proto,
                              flags | socket.AI_NUMERICHOST))
    return rows


def type_name(exc):
    return exc.__class__.__name__


def install():
    global _INSTALLED
    if enabled() and not _INSTALLED:
        socket.getaddrinfo = getaddrinfo
        _INSTALLED = True


def snapshot():
    with _LOCK:
        return {host: {'addresses': list(item[0]),
                       'ttl_remaining': max(0, int(item[1] - time.monotonic()))}
                for host, item in _CACHE.items()}
