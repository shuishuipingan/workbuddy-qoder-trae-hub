"""Optional in-memory or Redis prefix affinity for upstream Qoder session IDs.

The Redis key contains only hashes of the account/model scope and serialized
message prefix. The value is a random UUID, never prompt content or a token.
No external Python packages are required (RESP2 over a short TCP connection).
"""

import hashlib
import hmac
import json
from collections import OrderedDict
import socket
import ssl
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit


class RedisProtocolError(Exception):
    pass


class RedisClient:
    def __init__(self, url, timeout=0.4):
        parsed = urlsplit(url)
        if parsed.scheme not in ("redis", "rediss") or not parsed.hostname:
            raise ValueError("QD_SESSION_REDIS_URL must be redis:// or rediss://")
        self.host = parsed.hostname
        self.port = parsed.port or 6379
        self.database = int((parsed.path or "/0").strip("/") or 0)
        if not 0 <= self.database <= 15:
            raise ValueError("Redis database must be between 0 and 15")
        self.username = unquote(parsed.username) if parsed.username else None
        self.password = unquote(parsed.password) if parsed.password else None
        self.tls = parsed.scheme == "rediss"
        self.timeout = float(timeout)

    @staticmethod
    def _encode(*parts):
        encoded = [str(p).encode("utf-8") for p in parts]
        return (b"*%d\r\n" % len(encoded) + b"".join(
            b"$%d\r\n" % len(p) + p + b"\r\n" for p in encoded))

    @staticmethod
    def _read(file):
        kind = file.read(1)
        line = file.readline()
        if not kind or not line.endswith(b"\r\n"):
            raise RedisProtocolError("incomplete Redis response")
        line = line[:-2]
        if kind == b"-":
            raise RedisProtocolError(line.decode("utf-8", "replace"))
        if kind == b"+":
            return line.decode("utf-8")
        if kind == b":":
            return int(line)
        if kind == b"$":
            size = int(line)
            if size == -1:
                return None
            if size < 0 or size > 1024 * 1024:
                raise RedisProtocolError("invalid Redis bulk length")
            data = file.read(size + 2)
            if len(data) != size + 2 or not data.endswith(b"\r\n"):
                raise RedisProtocolError("incomplete Redis bulk reply")
            return data[:-2].decode("utf-8")
        if kind == b"*":
            count = int(line)
            if count < 0 or count > 10000:
                raise RedisProtocolError("invalid Redis array length")
            return [RedisClient._read(file) for _ in range(count)]
        raise RedisProtocolError("unsupported Redis reply type")

    def execute(self, *command):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        try:
            sock.settimeout(self.timeout)
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self.host)
                sock.settimeout(self.timeout)
            with sock.makefile("rb") as reader:
                def call(*parts):
                    sock.sendall(self._encode(*parts))
                    return self._read(reader)
                if self.password is not None:
                    auth = (("AUTH", self.username, self.password)
                            if self.username else ("AUTH", self.password))
                    if call(*auth) != "OK":
                        raise RedisProtocolError("Redis authentication failed")
                if self.database and call("SELECT", self.database) != "OK":
                    raise RedisProtocolError("Redis database selection failed")
                return call(*command)
        finally:
            sock.close()


class PrefixSessionCache:
    backend = "redis"
    # One Redis transaction selects the longest prefix and claims both the
    # stable conversation anchor and current full prefix. All keys use a
    # shared hash tag. This small client intentionally does not follow Redis
    # Cluster MOVED/ASK redirections; use a standalone/proxied endpoint.
    ASSIGN = """
local selected = false
local matched = 0
for i = #KEYS, 1, -1 do
    local value = redis.call('GET', KEYS[i])
    if value then
        selected = value
        matched = i
        break
    end
end
if not selected then selected = ARGV[1] end
redis.call('SET', KEYS[1], selected, 'EX', ARGV[2])
if #KEYS > 1 then
    redis.call('SET', KEYS[#KEYS], selected, 'EX', ARGV[2])
end
if matched > 1 and matched < #KEYS then
    redis.call('EXPIRE', KEYS[matched], ARGV[2])
end
return selected
"""

    def __init__(self, redis_url, ttl=3600, timeout=0.4, hash_secret=None):
        self.redis = RedisClient(redis_url, timeout=timeout)
        self.ttl = max(60, min(int(ttl), 86400))
        if not hash_secret:
            raise ValueError("a stable private prefix hash secret is required")
        self.hash_secret = str(hash_secret).encode("utf-8")

    def keys(self, messages, account_uid, realm, model_key,
             client_session_key=None, tenant_scope="", context=None):
        messages = messages or []
        # A system-only prefix is shared by many unrelated conversations.
        # The minimum anchor must include the first non-empty user message.
        anchor = next((index + 1 for index, message in enumerate(messages)
                       if isinstance(message, dict)
                       and message.get("role") == "user"
                       and message.get("content")), None)
        if anchor is None:
            return []
        # Never share session state across authenticated API keys, accounts,
        # regions, models, explicit client conversations, or tool/parameter
        # configurations. HMAC avoids offline guesses of private prompts.
        scope = json.dumps([tenant_scope, account_uid, realm, model_key,
                            client_session_key, context], ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
        scope_digest = hmac.new(self.hash_secret, scope.encode("utf-8"),
                                hashlib.sha256).hexdigest()[:32]
        state = hmac.new(self.hash_secret, digestmod=hashlib.sha256)
        result = []
        for index, message in enumerate(messages, 1):
            blob = json.dumps(message, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":")).encode("utf-8")
            state.update(len(blob).to_bytes(8, "big"))
            state.update(blob)
            if index >= anchor:
                result.append("qoder:session:v2:{%s}:%s" %
                              (scope_digest, state.hexdigest()))
        # Bound command size for very long conversations while preserving the
        # anchor and newest (most useful) prefix candidates.
        if len(result) > 128:
            result = result[:1] + result[-127:]
        return result

    def resolve(self, messages, account_uid, realm, model_key,
                client_session_key=None, tenant_scope="", context=None):
        keys = self.keys(messages, account_uid, realm, model_key,
                         client_session_key=client_session_key,
                         tenant_scope=tenant_scope, context=context)
        if not keys:
            return None
        chosen = self._assign(keys)
        try:
            return str(uuid.UUID(chosen))
        except (ValueError, TypeError, AttributeError):
            raise RedisProtocolError("invalid cached upstream session UUID") from None

    def _assign(self, keys):
        return self.redis.execute("EVAL", self.ASSIGN, len(keys), *keys,
                                  str(uuid.uuid4()), self.ttl)


class MemorySessionCache(PrefixSessionCache):
    """Bounded, thread-safe TTL/LRU map for a single gateway process."""
    backend = "memory"

    def __init__(self, ttl=3600, max_entries=4096, hash_secret=None):
        self.ttl = max(60, min(int(ttl), 86400))
        if not hash_secret:
            raise ValueError("a stable private prefix hash secret is required")
        self.hash_secret = str(hash_secret).encode("utf-8")
        self.max_entries = max(1, min(int(max_entries), 100000))
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def _assign(self, keys):
        with self._lock:
            now = time.monotonic()
            selected = None
            matched = None
            for key in reversed(keys):
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if entry[1] <= now:
                    del self._entries[key]
                    continue
                selected = entry[0]
                matched = key
                break
            selected = selected or str(uuid.uuid4())
            # Refresh the matched prefix plus stable anchor and current tail,
            # exactly like the Redis transaction. Never retain prompt content.
            for key in dict.fromkeys([matched, keys[0], keys[-1]]):
                if key is not None:
                    self._entries[key] = (selected, now + self.ttl)
                    self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return selected
