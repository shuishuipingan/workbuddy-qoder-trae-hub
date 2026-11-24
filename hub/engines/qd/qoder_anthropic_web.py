"""Anthropic wire format for the gateway's hosted web tools."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict

import qoder_anthropic
import qoder_settings
from qoder_web_response import WebEvidence, domains, matches_domain, parse_arguments, public_link, url_key
from qoder_webtools import format_search_result


_TYPES = {"web_search_20250305": "web_search", "web_fetch_20250910": "web_fetch"}
_RESULT_TYPES = {name: name + "_tool_result" for name in _TYPES.values()}
_ID_PREFIX = "srvtoolu_qd_"
_DEFERRED_PREFIX = _ID_PREFIX + "pending_"
_FALLBACK_REPLAY_KEY = secrets.token_bytes(32)
_DEFERRED_TTL = 600
_DEFERRED_MAX = 128
_deferred = OrderedDict()
_deferred_lock = threading.Lock()


def error_body(status, message):
    kind = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
            529: "overloaded_error"}.get(status, "api_error")
    return {"type": "error", "error": {"type": kind, "message": message}}


def _encode_source(source, key):
    raw = json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(hmac.digest(key, raw, hashlib.sha256) + raw).decode("ascii")


def _decode_source(token, key):
    try:
        if not isinstance(token, str) or len(token) > 24000:
            raise ValueError
        signed = base64.b64decode(token, altchars=b"-_", validate=True)
        tag, raw = signed[:32], signed[32:]
        if not hmac.compare_digest(tag, hmac.digest(key, raw, hashlib.sha256)):
            raise ValueError
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid or expired gateway web search result") from None


def _fetch_text(document):
    source = document.get("source")
    data = source.get("data") if isinstance(source, dict) else None
    return data[:20000] if isinstance(data, str) else ""


def _blocks(message):
    content = message.get("content")
    if isinstance(content, dict):
        return [content]
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def applies(payload):
    return any(isinstance(tool, dict) and str(tool.get("type") or "").startswith(("web_search_", "web_fetch_"))
               for tool in payload.get("tools") or []) or any(
        isinstance(message, dict) and message.get("role") == "assistant"
        and any(str(block.get("id") or "").startswith(_ID_PREFIX) for block in _blocks(message))
        for message in payload.get("messages") or [])


class WebRequest:
    def __init__(self, payload, scope, accounts_dir):
        self.scope = scope
        self.model = payload.get("model")
        self.accounts_dir = accounts_dir
        self._replay_key = None
        self.limits = {}
        self.usage = {}
        self.used_before = {}
        filters = {}
        ordinary = []
        for tool in payload.get("tools") or []:
            kind = str(tool.get("type") or "") if isinstance(tool, dict) else ""
            if not kind.startswith(("web_search_", "web_fetch_")):
                ordinary.append(tool)
                continue
            name = _TYPES.get(kind)
            if name is None or tool.get("name") != name or name in self.limits:
                raise ValueError("unsupported or duplicate native web declaration: " + kind)
            if tool.get("allowed_callers") not in (None, ["direct"]) or any(
                    tool.get(key) is not None for key in ("user_location", "url_sources", "max_content_tokens")):
                raise ValueError("unsupported native web tool option")
            if name == "web_fetch" and tool.get("citations") not in (None, {"enabled": False}):
                raise ValueError("native web fetch citations are not supported")
            limit = tool.get("max_uses", 8)
            if type(limit) is not int or limit < 0:
                raise ValueError("web tool max_uses must be a non-negative integer")
            allowed = domains(tool.get("allowed_domains"))
            blocked = domains(tool.get("blocked_domains"))
            if allowed and blocked:
                raise ValueError("web tool allowed_domains and blocked_domains are mutually exclusive")
            filters[name] = (allowed, blocked)
            self.limits[name] = min(limit, 32)
        self.evidence = WebEvidence(tool_filters=filters)
        self.prior_urls = set()
        messages, self.resume = self._join_deferred(payload.get("messages") or [])
        self.payload = dict(payload, tools=ordinary, messages=self._restore(messages))

    def defer(self, calls, client_calls, account, config, *, rounds, tokens_left):
        batch = DeferredWeb(self, calls, client_calls, account, config, rounds, tokens_left)
        with _deferred_lock:
            _prune_deferred()
            _deferred[batch.id] = batch
            while len(_deferred) > _DEFERRED_MAX:
                _deferred.popitem(last=False)
        return batch

    def _join_deferred(self, messages):
        assistant_blocks = {i: _blocks(message) for i, message in enumerate(messages)
                            if isinstance(message, dict) and message.get("role") == "assistant"
                            and isinstance(message.get("content"), (list, dict))}
        calls = {block["id"]: (i, block) for i, blocks in assistant_blocks.items() for block in blocks
                 if block.get("type") == "server_tool_use"
                 and str(block.get("id") or "").startswith(_DEFERRED_PREFIX)}
        if not calls:
            return messages, None
        # Anthropic returns results for a deferred batch in the next assistant message.
        # Put each paired call beside its result for Chat history conversion.
        edited = dict(assistant_blocks)
        awaiting = dict(calls)
        for i, blocks in assistant_blocks.items():
            paired = []
            for block in blocks:
                key = str(block.get("tool_use_id") or "")
                if block.get("type") in _RESULT_TYPES.values() and key in awaiting:
                    origin, call = awaiting[key]
                    if origin >= i or block["type"] != _RESULT_TYPES.get(str(call.get("name") or "")):
                        raise ValueError("invalid result for a pending web tool")
                    paired.append(call)
                    awaiting.pop(key)
            if paired:
                edited[i] = paired + blocks
        for i, blocks in edited.items():
            edited[i] = [block for block in blocks if not (
                block.get("type") == "server_tool_use" and str(block.get("id") or "") in calls
                and calls[block["id"]][0] == i)]
        rewritten = [dict(message, content=edited[i]) if i in edited else message
                     for i, message in enumerate(messages)]
        if not awaiting:
            return rewritten, None
        origin, call = next(iter(awaiting.values()))
        if origin != len(messages) - 2 or not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
            raise ValueError("pending web tools require the preceding client tool results")
        batch_id = call["id"].rsplit("_", 1)[0]
        with _deferred_lock:
            _prune_deferred()
            batch = _deferred.get(batch_id)
        if batch is None or batch.scope != self.scope or batch.model != self.model:
            raise ValueError("pending web tool continuation is unavailable")
        if self.limits != batch.limits or self.evidence.tool_filters != batch.filters:
            raise ValueError("pending web tools require the original web tool declarations")
        results = [block.get("tool_use_id") for block in _blocks(messages[-1])
                   if block.get("type") == "tool_result"]
        ordinary = {str(block.get("id") or "") for block in assistant_blocks[origin]
                    if block.get("type") == "tool_use"}
        if ordinary != batch.client_ids or len(results) != len(batch.client_ids) or set(results) != batch.client_ids:
            raise ValueError("pending web tools require all client tool results")
        if awaiting != {block["id"]: (origin, block) for block in batch.blocks}:
            raise ValueError("pending web tool calls were modified")
        return rewritten, batch

    @property
    def replay_key(self):
        if self._replay_key is None:
            try:
                secret = qoder_settings.panel_session_secret(self.accounts_dir).encode()
            except (OSError, ValueError):
                secret = _FALLBACK_REPLAY_KEY
            # Separate this use from panel sessions and scope replay to the API key and realm.
            self._replay_key = hmac.digest(secret, b"qoder.anthropic.web.v1\0" + self.scope.encode(), hashlib.sha256)
        return self._replay_key

    def _restore(self, messages):
        converted = []
        for message in messages:
            if not isinstance(message, dict):
                converted.append(message)
                continue
            blocks = _blocks(message)
            if message.get("role") == "user":
                text = message.get("content") if isinstance(message.get("content"), str) else ""
                parts = [text]
                for block in blocks:
                    if block.get("type") in ("text", "tool_result"):
                        parts.append(qoder_anthropic._tool_result_text(block) if block.get("type") == "tool_result"
                                     else str(block.get("text") or ""))
                for url in re.findall(r"https?://[^\s<>\"']+", "\n".join(parts)):
                    url = url.rstrip(").,;]}")
                    if public_link(url):
                        self.prior_urls.add(url_key(url))
            starts = {block.get("id"): block for block in blocks
                      if block.get("type") == "server_tool_use"
                      and str(block.get("id") or "").startswith(_ID_PREFIX)}
            if not starts or message.get("role") != "assistant":
                converted.append(message)
                continue
            pending = []
            for block in blocks:
                kind = block.get("type")
                if kind == "server_tool_use" and str(block.get("id") or "") in starts:
                    continue
                tool_id = str(block.get("tool_use_id") or "")
                call = starts.get(tool_id) if kind in _RESULT_TYPES.values() else None
                if call is None or _RESULT_TYPES.get(str(call.get("name") or "")) != kind:
                    pending.append(block)
                    continue
                content = block.get("content")
                if kind == _RESULT_TYPES["web_search"] and isinstance(content, list):
                    rows = []
                    for item in content:
                        if not isinstance(item, dict):
                            raise ValueError("invalid gateway web search result")
                        row = _decode_source(item.get("encrypted_content"), self.replay_key)
                        if row["url"] != item.get("url") or row["title"] != item.get("title"):
                            raise ValueError("invalid gateway web search result")
                        source = self.evidence.remember(row["url"], row["title"], row["published_time"])
                        self.prior_urls.add(url_key(row["url"]))
                        rows.append(dict(row, source_id=source["id"]))
                    result = format_search_result({"results": rows, "engine": "gateway"})
                elif kind == _RESULT_TYPES["web_fetch"] and isinstance(content, dict) and content.get("type") == "web_fetch_result":
                    document = content.get("content")
                    url = content.get("url")
                    if not isinstance(document, dict) or not public_link(url):
                        raise ValueError("invalid gateway web fetch result")
                    result = "Fetched %s\n\n%s" % (url, _fetch_text(document))
                else:
                    result = json.dumps({"error": content.get("error_code", "unavailable")
                                         if isinstance(content, dict) else "unavailable"})
                starts.pop(tool_id)
                pending.append(dict(call, type="tool_use"))
                converted.append(dict(message, content=pending))
                converted.append({"role": "user", "content": [{"type": "tool_result",
                                  "tool_use_id": tool_id, "content": result}]})
                pending = []
            if pending:
                converted.append(dict(message, content=pending))
        return converted

    def unavailable(self, name, params):
        if self.used_before.get(name, 0) + self.usage.get(name, 0) >= self.limits[name]:
            return "max_uses_exceeded"
        if name == "web_search":
            return None
        url = params.get("url")
        if not public_link(url):
            return "invalid_tool_input"
        if not matches_domain(url, *self.evidence.filters_for(name)):
            return "url_not_allowed"
        if url_key(url) not in self.prior_urls and url_key(url) not in self.evidence.sources:
            return "url_not_in_prior_context"
        return None


def completion_tokens(usage, message):
    reported = usage.get("completion_tokens")
    if type(reported) is int and reported > 0:
        return reported
    # Usage can be missing. Count visible UTF-8 bytes rather than giving
    # the next round a fresh budget or guessing a tokenizer's compression.
    parts = [message.get("content"), message.get("reasoning_content")]
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        parts.extend((function.get("name"), function.get("arguments")))
    return max(1, sum(len(part.encode("utf-8", "replace")) for part in parts
                      if isinstance(part, str)))


def _prune_deferred():
    now = time.monotonic()
    while _deferred and next(iter(_deferred.values())).expires_at <= now:
        _deferred.popitem(last=False)


class DeferredWeb:
    def __init__(self, request, calls, client_calls, account, config, rounds, tokens_left):
        self.id = _DEFERRED_PREFIX + uuid.uuid4().hex
        self.expires_at = time.monotonic() + _DEFERRED_TTL
        self.scope = request.scope
        self.model = request.model
        self.limits = dict(request.limits)
        self.filters = request.evidence.tool_filters
        self.usage = {name: request.used_before.get(name, 0) + request.usage.get(name, 0)
                      for name in request.limits}
        self.account = account
        self.config = config
        self.rounds = rounds
        self.tokens_left = tokens_left
        self.identity = self.account_identity(account)
        self.client_ids = {call["id"] for call in client_calls}
        self.blocks = [dict(type="server_tool_use", id=self.id + "_" + str(i),
                            name=call["function"]["name"], input=parse_arguments(call["function"].get("arguments")))
                       for i, call in enumerate(calls)]
        self.lock = threading.Lock()
        self.results = []

    def account_identity(self, account):
        if self.config["backend"] != "qoder":
            return None
        import qoder_official_web

        try:
            origin = qoder_official_web.service_origin(account)
        except (qoder_official_web.OfficialWebError, ValueError):
            origin = ""
        credential = str(getattr(account, "access_token", "") or "").encode()
        return (getattr(account, "uid", ""), getattr(account, "realm", ""), origin,
                getattr(account, "organization_id", ""), hashlib.sha256(credential).digest())


class WebOutput:
    def __init__(self, request, model, thinking, begin=None, write=None):
        self.request = request
        self.model = model
        self.thinking = thinking
        self.begin = begin
        self.write = write
        self.id = "msg_" + uuid.uuid4().hex
        self.blocks = []
        self.citations = {}
        self.started = False

    def emit(self, name, **data):
        if self.write:
            body = json.dumps(dict(data, type=name), ensure_ascii=False)
            self.write(("event: %s\ndata: %s\n\n" % (name, body)).encode())

    def start(self, usage=None):
        if self.write and not self.started:
            self.begin()
            self.started = True
            self.emit("message_start", message={"id": self.id, "type": "message", "role": "assistant",
                      "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None,
                      "usage": dict(usage or {"input_tokens": 0}, output_tokens=0)})

    def block(self, index, block):
        kind = block["type"]
        initial = dict(block)
        citations = initial.pop("citations", ())
        deltas = {"text": ("text", "text_delta", "text"),
                  "thinking": ("thinking", "thinking_delta", "thinking"),
                  "tool_use": ("input", "input_json_delta", "partial_json"),
                  "server_tool_use": ("input", "input_json_delta", "partial_json")}
        if kind in deltas:
            key, delta_type, delta_key = deltas[kind]
            value = block[key]
            initial[key] = {} if key == "input" else ""
        self.emit("content_block_start", index=index, content_block=initial)
        if kind in deltas:
            delta = json.dumps(value, ensure_ascii=False) if key == "input" else value
            self.emit("content_block_delta", index=index, delta={"type": delta_type, delta_key: delta})
            if kind == "thinking":
                self.emit("content_block_delta", index=index,
                          delta={"type": "signature_delta", "signature": block["signature"]})
        for citation in citations:
            self.emit("content_block_delta", index=index,
                      delta={"type": "citations_delta", "citation": citation})
        self.emit("content_block_stop", index=index)

    def append(self, block):
        if self.write:
            self.start()
            self.block(len(self.blocks), block)
        self.blocks.append(block)

    def tool_start(self, name, arguments, executing=True):
        tool_id = _ID_PREFIX + uuid.uuid4().hex
        index = len(self.blocks)
        self.append({"type": "server_tool_use", "id": tool_id, "name": name,
                     "input": parse_arguments(arguments)})
        return index

    def tool_done(self, index, result):
        call = self.blocks[index]
        self.tool_result(call, result)

    def tool_result(self, call, result):
        tool_id, name = call["id"], call["name"]
        if not result.get("ok"):
            content = {"type": name + "_tool_result_error", "error_code": result.get("error_code", "unavailable")}
        elif name == "web_search":
            content = []
            for row in result.get("results") or []:
                if len(row["url"]) > 4096:
                    continue
                source = {"url": row["url"], "title": str(row.get("title") or "")[:1000],
                          "snippet": str(row.get("snippet") or "")[:1500],
                          "published_time": str(row.get("published_time") or "")[:100]}
                token = _encode_source(source, self.request.replay_key)
                content.append({"type": "web_search_result", "url": source["url"], "title": source["title"],
                                "page_age": source["published_time"] or None,
                                "encrypted_content": token})
                if source["snippet"]:
                    self.citations[source["url"]] = {"type": "web_search_result_location",
                        "url": source["url"], "title": source["title"],
                        "encrypted_index": token, "cited_text": source["snippet"][:150]}
        else:
            for link in result.get("links") or []:
                url = link.get("url") if isinstance(link, dict) else None
                if public_link(url):
                    self.request.prior_urls.add(url_key(url))
            content = {"type": "web_fetch_result", "url": result.get("final_url") or result["url"],
                       "content": {"type": "document", "title": result.get("title") or "",
                                   "source": {"type": "text", "media_type": "text/plain",
                                              "data": result.get("text") or ""}}}
        self.append({"type": _RESULT_TYPES[name], "tool_use_id": tool_id, "content": content})

    def _annotate(self, block):
        if block["type"] == "text":
            block["text"] = self.request.evidence.render(block["text"])
            urls = dict.fromkeys(a["url"] for a in self.request.evidence.annotations(block["text"]))
            citations = [self.citations[url] for url in urls if url in self.citations]
            if citations:
                block["citations"] = citations
        return block

    def tool_round(self, chat, hosted_names, deferred=None):
        blocks = qoder_anthropic.chat_to_messages(
            chat, self.model, thinking_mode_override=self.thinking)["content"]
        pending = iter(deferred.blocks) if deferred else None
        for block in blocks:
            if block["type"] == "tool_use" and block["name"] in hosted_names:
                if pending is not None:
                    self.append(next(pending))
            else:
                self.append(self._annotate(block))

    def finish(self, chat, *, content_emitted=False):
        message = qoder_anthropic.chat_to_messages(chat, self.model, thinking_mode_override=self.thinking)
        if (chat.get("choices") or [{}])[0].get("finish_reason") == "content_filter":
            message["stop_reason"] = "refusal"
        message["content"] = ([] if content_emitted else
                              [self._annotate(block) for block in message["content"]])
        usage = message["usage"]
        if self.request.usage:
            usage["server_tool_use"] = {name + "_requests": count for name, count in self.request.usage.items()}
        if self.write:
            self.start(usage)
            for index, block in enumerate(message["content"], len(self.blocks)):
                self.block(index, block)
            self.emit("message_delta", delta={"stop_reason": message["stop_reason"], "stop_sequence": None},
                      usage=usage)
            self.emit("message_stop")
        message["id"] = self.id
        message["content"] = self.blocks + message["content"]
        return message

    def fail(self, message, status=502):
        self.emit("error", error=error_body(status, message)["error"])
