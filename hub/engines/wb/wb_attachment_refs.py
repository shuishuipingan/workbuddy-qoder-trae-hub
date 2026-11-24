"""Local gateway attachment storage and bounded, authenticated retrieval."""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import sqlite3
import ssl
import urllib.request
import stat
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

MAX_FILE = 8 * 1024 * 1024
UPLOAD_LIMIT = MAX_FILE + 128 * 1024
TTL = 24 * 3600
MAX_STORED = 64 * 1024 * 1024
MAX_COUNT = 128
ID = re.compile(r"file-[0-9a-f]{32}\Z")
_NETWORK_SLOTS = threading.BoundedSemaphore(4)


class ReferenceError(ValueError):
    pass


def owner_for(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""


def filename_of(value):
    if not isinstance(value, str):
        return ""
    value = value[:4096].replace(chr(92), "/").rsplit("/", 1)[-1]
    return "".join(c for c in value[:200] if c.isprintable())


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.directory / "files.sqlite3"), timeout=3)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                mime TEXT NOT NULL, purpose TEXT NOT NULL, created INTEGER NOT NULL,
                body BLOB NOT NULL)""")
        except BaseException:
            db.close()
            raise
        return db

    def put(self, owner, name, mime, purpose, data):
        if not owner or not data or len(data) > MAX_FILE:
            raise ReferenceError("empty or oversized upload (maximum 8 MiB)")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", purpose):
            raise ReferenceError("invalid upload purpose")
        ident, now = "file-" + uuid4().hex, int(time.time())
        name = filename_of(name) or "attachment"
        mime = mimetypes.guess_type(name)[0] or mime or "application/octet-stream"
        from contextlib import closing
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM files WHERE created < ?", (now - TTL,))
            count, size = db.execute("SELECT count(*), coalesce(sum(length(body)), 0) FROM files").fetchone()
            if count >= MAX_COUNT or size + len(data) > MAX_STORED:
                raise ReferenceError("attachment store is full; delete earlier uploads")
            db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)",
                       (ident, owner, name, mime, purpose, now, data))
        return self._metadata(ident, name, purpose, now, len(data))

    @staticmethod
    def _metadata(ident, name, purpose, created, size):
        return {"id": ident, "object": "file", "filename": name, "purpose": purpose,
                "created_at": created, "expires_at": created + TTL, "bytes": size,
                "status": "processed"}

    def _row(self, owner, ident, columns):
        if not owner or not ID.fullmatch(ident) or not (self.directory / "files.sqlite3").is_file():
            return None
        from contextlib import closing
        with closing(self._connect()) as db:
            return db.execute("SELECT " + columns + " FROM files WHERE id=? AND owner=? AND created>=?",
                              (ident, owner, int(time.time()) - TTL)).fetchone()

    def get(self, owner, ident):
        result = self._row(owner, ident, "name,mime,body")
        if result is None:
            raise ReferenceError("unknown, expired or inaccessible file ID; upload to this gateway")
        return result

    def info(self, owner, ident):
        row = self._row(owner, ident, "name,purpose,created,length(body)")
        return self._metadata(ident, *row) if row else None

    def delete(self, owner, ident):
        if self.info(owner, ident) is None:
            return False
        from contextlib import closing
        with closing(self._connect()) as db, db:
            return db.execute("DELETE FROM files WHERE id=? AND owner=?", (ident, owner)).rowcount == 1


def parse_upload(content_type, body):
    if len(content_type) > 500 or "\r" in content_type or "\n" in content_type:
        raise ReferenceError("invalid upload content type")
    if content_type.split(";", 1)[0].lower() == "multipart/form-data":
        from email import policy
        from email.parser import BytesParser
        try:
            msg = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + content_type + "\r\n\r\n").encode("ascii") + body)
            if not msg.is_multipart() or msg.defects:
                raise ValueError()
            parts = list(msg.iter_parts())
            if len(parts) > 8:
                raise ValueError()
            fields = {}
            for part in parts:
                name = part.get_param("name", header="content-disposition")
                if name not in ("file", "purpose"):
                    continue
                if name in fields or part.defects or part.is_multipart():
                    raise ValueError()
                fields[name] = (part.get_filename(), part.get_content_type(),
                                part.get_payload(decode=True) or b"")
            name, mime, data = fields["file"]
            purpose = fields.get("purpose", (None, None, b"user_data"))[2]
            if len(purpose) > 40:
                raise ValueError()
            return name, mime, purpose.decode("ascii"), data
        except (ValueError, UnicodeError, KeyError, TypeError) as exc:
            raise ReferenceError("invalid multipart upload; expected one file and purpose") from exc
    if content_type.split(";", 1)[0].lower() == "application/json":
        try:
            obj = json.loads(body.decode("utf-8"))
            encoded = obj["file_data"]
            if not isinstance(encoded, str):
                raise ValueError()
            mime = ""
            if encoded[:5].lower() == "data:":
                prefix, encoded = encoded[5:].split(",", 1)
                if not prefix.lower().endswith(";base64"):
                    raise ValueError()
                mime = prefix.split(";", 1)[0]
            data = base64.b64decode(encoded, validate=True)
            purpose = obj.get("purpose", "user_data")
            if not isinstance(purpose, str):
                raise ValueError()
            return obj.get("filename", "attachment"), mime, purpose, data
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ReferenceError("invalid JSON upload; expected filename and Base64 file_data") from exc
    raise ReferenceError("expected multipart/form-data or application/json")


def _shared_path(directory, owner, peer, value):
    # Only an operator-selected key on local loopback can refer to the inbox.
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            raise ValueError()
        config_path = directory / "config.json"
        with config_path.open("rb") as cfg_file:
            configured = json.loads(cfg_file.read(8192).decode("utf-8")).get("path_owner", "")
        if not owner or not isinstance(configured, str) or not hmac.compare_digest(owner, configured):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ReferenceError("local path access is not enabled for this client/key") from exc
    if value[:7].lower() == "file://":
        src = urlsplit(value)
        if src.netloc.lower() not in ("", "localhost") or src.query or src.fragment:
            raise ReferenceError("expected a local inbox file URI")
        value = unquote(src.path)
        if os.name == "nt" and re.match(r"^/[a-zA-Z]:/", value):
            value = value[1:]
    # No subdirectories, UNC shares, device names or alternate data streams.
    if len(value) > 2048 or "\x00" in value or '..' in value.replace(chr(92), '/').split('/'):
        raise ReferenceError("path must name a file directly in the attachment inbox")
    root = directory / "inbox"
    try:
        if root.is_symlink() or (getattr(root.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError()
        root = root.resolve(strict=True)
        path = Path(value)
        name = path.name
        if (not name or name in (".", "..") or ":" in name or chr(92) in name or
                PureWindowsPath(name).is_reserved() or name != name.rstrip(" .")):
            raise ValueError()
        if path.is_absolute():
            if path.parent != root:
                raise ValueError()
        elif len(path.parts) != 1:
            raise ValueError()
        path = root / name
        st = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                getattr(st, "st_file_attributes", 0) & 0x400 or path.resolve(strict=True) != path):
            raise ValueError()
        with path.open("rb") as fp:
            opened = os.fstat(fp.fileno())
            if not os.path.samestat(st, opened) or opened.st_nlink != 1:
                raise ValueError()
            data = fp.read(MAX_FILE + 1)
    except (OSError, ValueError) as exc:
        raise ReferenceError("only regular files placed directly in the attachment inbox are readable") from exc
    if len(data) > MAX_FILE:
        raise ReferenceError("file exceeds the 8 MiB byte limit")
    return name, mimetypes.guess_type(name)[0] or "", data


def _public_address(raw):
    ip = ipaddress.ip_address(raw)
    # Also exclude IPv6 transition/translation space rather than allow an
    # IPv4-in-IPv6 address to be routed by a local translator.
    if ip.version != 4:
        return False
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved and
            not ip.is_unspecified and not ip.is_loopback and not ip.is_link_local)


def _public_dns_for_fakeip(host, timeout):
    # Fall back only when system DNS returns exclusively TUN synthetic IPv4s.
    # Neither synthetic DNS answers nor resolver redirects are dialled for files.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request("https://dns.alidns.com/resolve?name=" + host + "&type=A",
                                 headers={"Accept": "application/dns-json"})
    with opener.open(req, timeout=timeout) as response:
        obj = json.loads(response.read(32769))
    if obj.get("Status") != 0:
        return ()
    return tuple(dict.fromkeys(str(ipaddress.IPv4Address(a["data"]))
                               for a in obj.get("Answer", []) if a.get("type") == 1))


def _resolve_public_addresses(host, timeout):
    # Use the native resolver, then connect to the validated numeric address.
    # Bound caller wait and cap concurrent DNS tasks (native DNS cannot cancel).
    if not _NETWORK_SLOTS.acquire(blocking=False):
        raise ReferenceError("attachment download is busy; retry later")
    from queue import Queue, Empty
    results = Queue(1)
    def lookup():
        try:
            rows = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            addresses = tuple(dict.fromkeys(row[4][0] for row in rows))
            fake_net = ipaddress.IPv4Network("198.18.0.0/15")
            if addresses and all(ipaddress.IPv4Address(a) in fake_net for a in addresses):
                addresses = _public_dns_for_fakeip(host, timeout)
            results.put(addresses)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, http.client.HTTPException):
            results.put(())
        finally:
            _NETWORK_SLOTS.release()
    try:
        threading.Thread(target=lookup, daemon=True).start()
    except BaseException:
        _NETWORK_SLOTS.release()
        raise
    try:
        addresses = results.get(timeout=timeout)
    except Empty as exc:
        raise ReferenceError("HTTPS attachment DNS lookup timed out") from exc
    if not addresses or any(not _public_address(a) for a in addresses):
        raise ReferenceError("attachment link does not resolve exclusively to public IPv4 addresses")
    return addresses


def _download(value, deadline):
    try:
        if len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or chr(92) in value:
            raise ValueError()
        uri = urlsplit(value)
        if (uri.scheme != "https" or not uri.hostname or uri.username is not None or
                uri.password is not None or uri.port not in (None, 443)):
            raise ValueError()
        host = uri.hostname.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host) or "." not in host:
            raise ValueError()
        # Reject IP literals. Certificates are checked for the original host.
        if re.fullmatch(r"[0-9.]+", host):
            raise ValueError()
        target = urlunsplit(("", "", uri.path or "/", uri.query, ""))
        target.encode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise ReferenceError("attachment link must use HTTPS on port 443, without embedded credentials") from exc

    def remaining():
        wait = min(4, deadline - time.monotonic())
        if wait <= 0:
            raise ReferenceError("HTTPS attachment download timed out")
        return wait

    addresses = _resolve_public_addresses(host, remaining())
    # No system proxy, redirects, cookies, or gateway authorization headers.
    conn = http.client.HTTPSConnection(host, timeout=remaining(), context=ssl.create_default_context())
    watchdog_socket = None

    def connect(_addr, _timeout, _source):
        nonlocal watchdog_socket
        raw = socket.create_connection((addresses[0], 443), timeout=remaining())
        try:
            watchdog_socket = raw.dup()
        except OSError:
            raw.close()
            raise
        return raw

    def interrupt():
        if watchdog_socket is not None:
            try:
                watchdog_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    conn._create_connection = connect
    timer = threading.Timer(max(0, deadline - time.monotonic()), interrupt)
    timer.daemon = True
    timer.start()
    try:
        conn.request("GET", target, headers={"Accept-Encoding": "identity", "Connection": "close"})
        sock = conn.sock
        if sock is not None:
            sock.settimeout(remaining())
        with conn.getresponse() as response:
            if response.status != 200:
                raise ReferenceError("attachment link did not return a direct HTTP 200 (redirects are disabled)")
            if (response.getheader("Content-Encoding") or "identity").strip().lower() != "identity":
                raise ReferenceError("compressed HTTPS responses are not supported")
            length = response.length
            if length is not None and (length < 0 or length > MAX_FILE):
                raise ReferenceError("invalid file size or exceeds the 8 MiB byte limit")
            data = bytearray()
            while True:
                wait = remaining()
                if conn.sock is not None:
                    conn.sock.settimeout(wait)
                chunk = response.read1(min(65536, MAX_FILE + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_FILE:
                    raise ReferenceError("file exceeds the 8 MiB byte limit")
            remaining()
            if length is not None and len(data) != length:
                raise ReferenceError("HTTPS attachment response was incomplete")
            mime = (response.getheader("Content-Type") or "").split(";", 1)[0]
            return filename_of(unquote(uri.path)) or "attachment", mime, bytes(data)
    except ReferenceError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ReferenceError("HTTPS attachment download failed or TLS validation did not pass") from exc
    finally:
        timer.cancel()
        conn.close()
        if watchdog_socket is not None:
            watchdog_socket.close()


class Resolver:
    def __init__(self, directory, owner, peer):
        self.directory = Path(directory)
        self.owner = owner
        self.peer = peer
        self.deadline = time.monotonic() + 12
        self.count = 0
        self.used_text = 0
        self.extractions = 0
        self.inline_bytes = 0

    def begin_inline(self, size):
        if self.inline_bytes + size > 2 * MAX_FILE + 1024:
            raise ReferenceError("request exceeds the inline attachment processing limit")
        self.inline_bytes += size

    def begin_extraction(self):
        if self.extractions >= 4:
            raise ReferenceError("at most four document extractions per request")
        self.extractions += 1

    def reserve(self, size):
        if self.used_text + size > 512 * 1024:
            raise ReferenceError("request exceeds the 512 KiB attachment text limit")
        self.used_text += size

    def resolve(self, fields):
        if not self.owner:
            raise ReferenceError("attachment references require an authenticated API key")
        if self.count >= 4:
            raise ReferenceError("at most four file references per request")
        self.count += 1
        ident = fields.get("file_id") or fields.get("fileId") or fields.get("id")
        location = fields.get("file_url") or fields.get("url") or fields.get("path") or fields.get("file_path")
        if isinstance(ident, str) and ID.fullmatch(ident):
            return Store(self.directory).get(self.owner, ident)
        if isinstance(location, str) and location:
            if location.lower().startswith("https:"):
                return _download(location, self.deadline)
            if location.lower().startswith("file://") or not urlsplit(location).scheme or (
                    os.name == "nt" and re.match(r"^[a-zA-Z]:[\\/]", location)):
                return _shared_path(self.directory, self.owner, self.peer, location)
            raise ReferenceError("only a public HTTPS link or a local inbox path is supported")
        if ident:
            raise ReferenceError("external file ID cannot be resolved; upload the file to this gateway")
        raise ReferenceError("no file bytes, gateway upload ID, path or link supplied")
