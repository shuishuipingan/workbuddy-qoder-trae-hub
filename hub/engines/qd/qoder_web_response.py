"""Request-scoped web evidence and Responses events; no provider dependencies."""
import copy
import hashlib
import html
import ipaddress
import json
import re
import time
import urllib.parse
import uuid


def domains(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("domain filters must be arrays of at most 100 hostnames")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("domain filters must contain hostnames")
        host = value.strip().lower().rstrip(".")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("invalid domain filter") from None
        if (len(host) > 253 or not host or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split("."))):
            raise ValueError("domain filters require hostnames without scheme, path or wildcard")
        if host not in result:
            result.append(host)
    return result


def matches_domain(url, allowed=(), blocked=()):
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
        host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return False
    matches = lambda values: any(host == d or host.endswith("." + d) for d in values)
    return bool(host) and (not allowed or matches(allowed)) and not matches(blocked)


def clean_text(value, limit):
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))[:limit]


def public_link(url):
    if not isinstance(url, str) or any(c in url for c in ("\\", "\r", "\n", "\t")):
        return False
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
            return False
        _ = p.port
        host = p.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def url_key(url):
    p = urllib.parse.urlsplit(url)
    # Fragment changes do not create another source. Preserve query and scheme.
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def parse_arguments(arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


class WebEvidence:
    """Private to one API request: never shares account results across tenants."""

    def __init__(self, tools=()):
        self.sources = {}
        self.cache = {}
        self.allowed = []
        self.blocked = []
        allow_sets = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") not in (
                    "web_search", "web_search_preview", "web_search_preview_2025_03_11"):
                continue
            filters = tool.get("filters") or {}
            if not isinstance(filters, dict):
                raise ValueError("web search filters must be an object")
            allowed = domains(filters.get("allowed_domains"))
            if allowed:
                allow_sets.append(allowed)
            self.blocked.extend(domains(filters.get("blocked_domains")))
            if tool.get("external_web_access") is False:
                raise ValueError("cached-only web search is not supported by this backend")
            if tool.get("search_content_types") not in (None, ["text"]):
                raise ValueError("only text web search is currently supported")
        # Reject ambiguous duplicate declarations rather than broaden a restriction.
        if allow_sets and any(values != allow_sets[0] for values in allow_sets):
            raise ValueError("conflicting allowed_domains across web search declarations")
        self.allowed = allow_sets[0] if allow_sets else []

    def remember(self, url, title="", published_time="", fetched=False):
        if not public_link(url):
            return None
        key = url_key(url)
        if key not in self.sources:
            self.sources[key] = {"id": "S%d" % (len(self.sources) + 1), "url": url,
                                 "title": clean_text(title, 1000) or url, "fetched": False}
        source = self.sources[key]
        source["fetched"] = source["fetched"] or fetched
        if title:
            source["title"] = clean_text(title, 1000)
        if published_time:
            source["published_time"] = str(published_time)[:100]
        return source

    def observe(self, name, result):
        result = copy.deepcopy(result)
        if not result.get("ok"):
            return result
        if name == "web_search":
            kept = []
            for item in result.get("results") or []:
                url = item.get("url")
                if not public_link(url) or not matches_domain(url, self.allowed, self.blocked):
                    continue
                source = self.remember(url, item.get("title"), item.get("published_time"))
                item["source_id"] = source["id"]
                kept.append(item)
            result["results"] = kept
        elif name == "web_fetch":
            url = result.get("final_url") or result.get("url")
            if not matches_domain(url, self.allowed, self.blocked):
                return {"ok": False, "error": "Final URL is outside the request's web domain filters"}
            source = self.remember(url, result.get("title"), fetched=True)
            if source:
                result["source_id"] = source["id"]
                # Preserve an input URL alias without inventing another source ID.
                if public_link(result.get("url")):
                    self.sources[url_key(result["url"])] = source
        return result

    def cache_key(self, name, args, account, backend, origin=""):
        scope = (getattr(account, "uid", ""), getattr(account, "realm", ""), origin)
        if origin:
            token = account.access_token
            scope += (getattr(account, "organization_id", ""),
                      hashlib.sha256(token.encode("utf-8")).digest())
        return (backend, scope, name,
                json.dumps(args, ensure_ascii=False, sort_keys=True))

    def render(self, text):
        by_id = {s["id"]: s for s in self.sources.values()}
        def replace(match):
            source = by_id.get(match.group(1))
            if not source:
                return match.group(0)
            label = source["title"].replace("[", "(").replace("]", ")").replace("\n", " ")
            return "[%s](%s)" % (label, source["url"])
        return re.sub(r"\[(S\d+)\](?!\()", replace, text)

    def annotations(self, text):
        citations = []
        for match in re.finditer(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|https?://[^\s<>\])]+", text):
            url = match.group(2) or match.group(0).rstrip(".,;。；，")
            try:
                source = self.sources.get(url_key(url))
            except ValueError:
                source = None
            if source:
                citations.append({"type": "url_citation", "start_index": match.start(),
                                  "end_index": match.end(), "url": source["url"],
                                  "title": source["title"]})
        return citations


class ResponsesWebOutput:
    """Real tool lifecycle events, stable IDs and evidence-backed citations."""

    def __init__(self, payload, model, evidence, begin=None, write=None):
        self.payload = payload
        self.requested_model = model
        self.resolved_model = None
        self.parallel_tool_calls = payload.get("parallel_tool_calls", True)
        self.evidence = evidence
        self.begin = begin
        self.write = write
        self.id = "resp_" + uuid.uuid4().hex
        self.created_at = int(time.time())
        self.seq = 0
        self.items = []
        self.started = False
        self.ended = False

    def _response_fields(self):
        return {"id": self.id, "object": "response", "created_at": self.created_at,
                "model": self.resolved_model or "", "requested_model": self.requested_model,
                "resolved_model": self.resolved_model, "tools": self.payload.get("tools") or [],
                "tool_choice": self.payload.get("tool_choice", "auto"),
                "parallel_tool_calls": self.parallel_tool_calls,
                "metadata": self.payload.get("metadata") or {}}

    def response(self, status="in_progress"):
        return dict(self._response_fields(), status=status, output=copy.deepcopy(self.items),
                    output_text="", error=None, incomplete_details=None)

    def emit(self, kind, **data):
        if not self.write:
            return
        self.seq += 1
        event = dict(data, type=kind, sequence_number=self.seq)
        self.write(("event: %s\ndata: %s\n\n" % (
            kind, json.dumps(event, ensure_ascii=False))).encode("utf-8"))

    def start(self):
        if self.write and not self.started:
            self.begin()
            self.started = True
            self.emit("response.created", response=self.response())
            self.emit("response.in_progress", response=self.response())

    def tool_start(self, name, arguments, executing=True):
        args = parse_arguments(arguments)
        action = ({"type": "search", "query": str(args.get("query") or "")} if name == "web_search"
                  else {"type": "open_page", "url": str(args.get("url") or "")})
        item = {"id": "ws_" + uuid.uuid4().hex, "type": "web_search_call",
                "status": "in_progress", "action": action}
        index = len(self.items)
        self.items.append(item)
        self.emit("response.output_item.added", output_index=index, item=copy.deepcopy(item))
        self.emit("response.web_search_call.in_progress", output_index=index, item_id=item["id"])
        if executing:
            self.emit("response.web_search_call.searching", output_index=index, item_id=item["id"])
        return index

    def tool_done(self, index, result):
        item = self.items[index]
        item["status"] = "completed" if result.get("ok") else "failed"
        if (item["action"]["type"] == "search"
                and "web_search_call.action.sources" in (self.payload.get("include") or [])):
            item["action"]["sources"] = [
                {"type": "url", "url": row["url"]} for row in result.get("results") or []]
        if result.get("ok"):
            self.emit("response.web_search_call.completed", output_index=index, item_id=item["id"])
        self.emit("response.output_item.done", output_index=index, item=copy.deepcopy(item))

    def enhance(self, response):
        response = copy.deepcopy(response)
        actual = response.get("resolved_model")
        if isinstance(actual, str) and actual.strip():
            self.resolved_model = actual
        for item in response.get("output") or []:
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    part["annotations"] = self.evidence.annotations(part.get("text") or "")
        response.update(self._response_fields())
        response["output"] = copy.deepcopy(self.items) + response.get("output", [])
        return response

    def stream_final(self, frames):
        # Reuse the mature text/function converter; only add web state/offsets.
        for frame in frames:
            raw = frame.decode("utf-8") if isinstance(frame, bytes) else frame
            data = json.loads(raw.split("\ndata: ", 1)[1])
            kind = data.pop("type")
            data.pop("sequence_number", None)
            if kind == "response.created":
                continue
            if "output_index" in data:
                data["output_index"] += len(self.items)
            if kind == "response.output_text.done":
                for index, annotation in enumerate(self.evidence.annotations(data.get("text") or "")):
                    self.emit("response.output_text.annotation.added", item_id=data["item_id"],
                              output_index=data["output_index"], content_index=data["content_index"],
                              annotation_index=index, annotation=annotation)
            part = data.get("part")
            if isinstance(part, dict) and part.get("type") == "output_text":
                part["annotations"] = self.evidence.annotations(part.get("text") or "")
            item = data.get("item")
            if isinstance(item, dict):
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        part["annotations"] = self.evidence.annotations(part.get("text") or "")
            if "response" in data:
                previous_model = self.resolved_model
                data["response"] = self.enhance(data["response"])
                if kind == "response.in_progress" and self.resolved_model == previous_model:
                    continue
            self.emit(kind, **data)
        self.ended = True

    def fail(self, message):
        for item in self.items:
            if item["status"] == "in_progress":
                item["status"] = "failed"
        response = self.response("failed")
        response["error"] = {"code": "server_error", "message": message}
        self.emit("response.failed", response=response)
        self.ended = True
