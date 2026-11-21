"""Opt-in Qoder web tools using the caller's existing CN or INTL account.

Protocol observed in Qoder CLI and CLI CN 1.1.89. This is a tool backend, not a
model-provider-native search API. No CLI process, WASM binary or new dependency
is needed: qoder_sign already implements the required encoding and COSY signing.
"""
import json
import urllib.error
import urllib.parse

import qoder_accounts
from qoder_sign import SESSIONS, qoder_encode
from qoder_web_response import clean_text, domains, matches_domain, public_link

SEARCH_PATH = "/algo/api/v1/webSearch/oneSearch"
FETCH_PATH = "/algo/api/v1/webFetch"
INTL_WEB_ORIGIN = "https://center.qoder.sh"
MAX_QUERY_CHARS = 2048
MAX_RESULTS = 5
MAX_FETCH_CHARS = 20000


class OfficialWebError(Exception):
    """A safe-to-display error; never includes credentials or raw server bodies."""


def service_origin(account):
    """Resolve the HTTPS origin for this account's official web tools."""
    if account is None or not getattr(account, "access_token", ""):
        raise OfficialWebError("official web tools require an authenticated account")
    realm = getattr(account, "realm", "")
    if realm not in ("cn", "intl"):
        raise OfficialWebError("official web tools require a CN or INTL account")
    cfg = qoder_accounts.account_realm_config(account)
    if cfg.get("vpc_instance"):
        raise OfficialWebError("official web tools have not been verified for VPC accounts")
    # CN tools share the account's gateway. INTL tools use the center service,
    # independently of api1/api2/api3 or QD_INTL_GATEWAY inference overrides.
    # Keep the caller's account; never borrow credentials or retry in another realm.
    origin = (str(cfg.get("gateway") or "").rstrip("/") if realm == "cn"
              else INTL_WEB_ORIGIN)
    parsed = urllib.parse.urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        raise OfficialWebError("invalid official web gateway configuration")
    return origin


def _request(account, path, payload):
    url = service_origin(account) + path + "?Encode=1"
    envelope = {"payload": json.dumps(payload, ensure_ascii=False,
                                       separators=(",", ":")),
                "encodeVersion": "1"}
    body = qoder_encode(json.dumps(envelope, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8"))
    headers = SESSIONS.get(account).headers(
        body, url, sse=False, accept="application/json")
    try:
        # The signature binds the request path. Keep the signed POST at its
        # original URL and avoid automatic retries for billable tools.
        result = qoder_accounts.http_json(
            url, data=body.encode("ascii"), headers=headers, timeout=15, retries=1,
            follow_redirects=False)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise OfficialWebError("official web request failed (HTTP %s)" % status) from None
    except Exception:
        raise OfficialWebError("official web request failed (network or response error)") from None
    if not isinstance(result, dict):
        raise OfficialWebError("invalid official web response")
    if result.get("success") is False or result.get("errorCode") not in (None, 0, "0"):
        raise OfficialWebError("official web service returned an error")
    return result


def _public_link(url):
    """Reject malformed URLs, credentials and literal private addresses in results.

    Search results are not fetched here. DNS validation happens in web_fetch
    before any user URL is handed to the upstream service.
    """
    return public_link(url)


def search(account, query, limit=MAX_RESULTS, allowed_domains=None, blocked_domains=None):
    query = str(query or "").strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        return {"ok": False, "engine": "qoder", "results": [],
                "error": "query must contain 1-%s characters" % MAX_QUERY_CHARS}
    try:
        allowed = domains(allowed_domains)
        blocked = domains(blocked_domains)
        limit = max(1, min(MAX_RESULTS, int(limit)))
        payload = {
            "query": query, "timeRange": "NoLimit",
            "contents": {"mainText": False, "markdownText": False, "summary": False},
        }
        if allowed:
            payload["allowed_domains"] = allowed
        if blocked:
            payload["blocked_domains"] = blocked
        data = _request(account, SEARCH_PATH, payload)
        items = data.get("pageItems")
        if not isinstance(items, list):
            raise OfficialWebError("invalid official search response (missing pageItems)")
        results = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("link")
            if (not _public_link(url) or url in seen
                    or not matches_domain(url, allowed, blocked)):
                continue
            seen.add(url)
            result = {"title": clean_text(item.get("title"), 1000), "url": url,
                      "snippet": clean_text(item.get("snippet"), 1000)}
            if item.get("publishedTime"):
                result["published_time"] = str(item["publishedTime"])[:100]
            results.append(result)
            if len(results) >= limit:
                break
        return {"ok": True, "engine": "qoder", "results": results, "error": ""}
    except (OfficialWebError, ValueError, TypeError) as exc:
        return {"ok": False, "engine": "qoder", "results": [], "error": str(exc)}


def fetch(account, url):
    # Reuse the local backend's guard. Remote execution does not exempt a
    # user-supplied URL from scheme, credentials, DNS and private-IP checks.
    from qoder_webtools import check_url
    url = str(url or "").strip()
    if not _public_link(url):
        return {"ok": False, "url": url, "error": "blocked: invalid or non-public URL"}
    ok, reason = check_url(url)
    if not ok:
        return {"ok": False, "url": url, "error": "blocked: " + reason}
    try:
        data = _request(account, FETCH_PATH, {
            "url": url, "maxAge": 0, "timeout": 14000, "formats": ["markdown"],
        })
        items = data.get("results")
        item = items[0] if isinstance(items, list) and items else None
        if not isinstance(item, dict) or item.get("errorCode") not in (None, 0, "0"):
            raise OfficialWebError("invalid official fetch response")
        status = item.get("statusCode")
        if status is not None and (type(status) is not int or status >= 400):
            raise OfficialWebError("official fetch could not retrieve the page")
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        redirected = metadata.get("redirectedUrl")
        if redirected:
            if not _public_link(redirected) or not check_url(redirected)[0]:
                raise OfficialWebError("blocked: official fetch returned a non-public redirect")
        # Match the official client: prefer markdown, then plain text/HTML.
        for field in ("markdown", "text", "html", "rawHtml"):
            text = item.get(field)
            if isinstance(text, str) and text.strip():
                links = []
                raw_links = item.get("links")
                for group in (raw_links.values() if isinstance(raw_links, dict) else []):
                    for link in group if isinstance(group, list) else []:
                        if isinstance(link, dict) and _public_link(link.get("href")):
                            links.append({"url": link["href"], "title": clean_text(
                                link.get("text") or link.get("title"), 300)})
                return {"ok": True, "engine": "qoder", "url": url,
                        "final_url": redirected or url,
                        "content_type": {"markdown": "text/markdown", "text": "text/plain",
                                         "html": "text/html", "rawHtml": "text/html"}[field],
                        "links": links[:10], "truncated": len(text) > MAX_FETCH_CHARS,
                        "title": str(metadata.get("title") or "")[:1000],
                        "text": text[:MAX_FETCH_CHARS], "error": ""}
        raise OfficialWebError("official fetch returned no readable content")
    except OfficialWebError as exc:
        return {"ok": False, "url": url, "error": str(exc)}
