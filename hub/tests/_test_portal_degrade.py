# -*- coding: utf-8 -*-
"""_test_portal_degrade.py -- 上游发疯时，门户必须**降级**而不是崩（第 198 轮）。

由来：面板所有数字都来自三家引擎。真实故障里引擎会：
  * 返回 200 但内容不是 JSON（反代改造、PHP 兜底页、CDN 错误页）；
  * 返回 500 带一整页 HTML；
  * 卡住不响应（超时）。
这三种情况下，门户若把异常直接抛成 500，面板就整页白 —— 用户看到"后台坏了"，
而真实原因只是某一家临时抽风。

本套件用三家**假引擎**分别模拟这三种故障，断言 /api/hub/* 全部仍然 200、
坏掉的那家被标记为不可用、健康的引擎数据照常返回、整体耗时被 timeout 兜住。
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

ROOT = P.REPO
sys.path.insert(0, ROOT)
try:
    from hub import ENGINES
    from hub.core import portal
except Exception as exc:                      # noqa: BLE001
    H.skip("R00 门户 import 失败（半成品树）", "%s: %s" % (type(exc).__name__, exc))
    H.finish()

# 每家一种故障；对照组留着好断言"健康的那家确实回了数据"
MODES = {"wb": "garbage", "qd": "html500", "trae": "slow"}


class Sick(BaseHTTPRequestHandler):
    name = "wb"

    def log_message(self, *a):
        pass

    def handle(self):
        # 客户端超时后先关连接，服务端写入会抛 ConnectionAborted（Windows 常见）。
        # 那是**我们故意制造的故障**的副产品，不该刷一屏 traceback。
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self):
        mode = MODES.get(self.name, "ok")
        path = self.path.split("?")[0]
        if mode == "slow":
            time.sleep(4.0)                   # > aggregator.HTTP_TIMEOUT(2s)
        if mode == "garbage":
            raw = b"<html>not json at all</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if mode == "html500":
            raw = b"<html><body>502 Bad Gateway</body></html>"
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        body = {"ok": True, "service": self.name + "-svc", "version": "1.0.0",
                "accounts": 1, "realm": "cn"}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.do_GET()


servers, ports = [], {}
for eng in ENGINES:
    cls = type("Sick_" + eng, (Sick,), {"name": eng})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers.append(srv)
    ports[eng] = srv.server_address[1]

portal_port = H.free_port()
argv = ["--host", "127.0.0.1", "--port", str(portal_port)]
for eng in ENGINES:
    argv += ["--%s-port" % eng, str(ports[eng])]
threading.Thread(target=portal.main, args=(argv,), daemon=True).start()
base = "http://127.0.0.1:%d" % portal_port


def call(path, timeout=25):
    started = time.time()
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw, time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), time.time() - started
    except Exception as exc:                  # noqa: BLE001
        return 0, str(exc).encode(), time.time() - started


end = time.time() + 15
while time.time() < end:
    try:
        with urllib.request.urlopen(base + "/api/hub/health", timeout=3) as resp:
            if resp.status == 200:
                break
    except Exception:                         # noqa: BLE001
        time.sleep(0.3)

# R1 健康：坏了两家也必须 200，且三家键都在
status, raw, _ = call("/api/hub/health")
doc = {}
try:
    doc = json.loads(raw)
except Exception:
    doc = {}
engines = (doc.get("engines") or {})
H.check("R1 有引擎发疯时 /api/hub/health 仍返回 200", status == 200, "status=%d" % status)
H.check("R1b health 仍然列出全部引擎（不因故障少一家）",
        set(engines) == set(ENGINES), "实际=%s" % sorted(engines))
H.check("R1c 超时的那家被标成不可用（trae ok=False）",
        (engines.get("trae") or {}).get("ok") is not True,
        json.dumps(engines.get("trae"), ensure_ascii=False)[:120])

# R2 账号：200 + counts 里三家都在（0 也行）
status, raw, _ = call("/api/hub/accounts")
acc = {}
try:
    acc = json.loads(raw)
except Exception:
    acc = {}
counts = acc.get("counts") or {}
H.check("R2 发疯时 /api/hub/accounts 仍返回 200", status == 200, "status=%d" % status)
H.check("R2b accounts.counts 覆盖三家（坏的那家记 0）",
        set(counts) == set(ENGINES), "实际=%s" % sorted(counts))
H.check("R2c 坏掉的引擎在 errors 里留了痕（不是静默吞掉）",
        bool(acc.get("errors")), json.dumps(acc.get("errors"), ensure_ascii=False)[:140])

# R3 模型并集：坏两家也不能把整个接口拖垮
status, raw, _ = call("/api/hub/models")
H.check("R3 发疯时 /api/hub/models 仍返回 200", status == 200, "status=%d" % status)

# R4 用量：三家的键都在，坏的那家全 0
status, raw, _ = call("/api/hub/usage")
usage = {}
try:
    usage = json.loads(raw)
except Exception:
    usage = {}
H.check("R4 发疯时 /api/hub/usage 仍返回 200", status == 200, "status=%d" % status)
H.check("R4b usage 覆盖三家 + merged", set(usage) >= set(ENGINES) | {"merged"},
        "实际=%s" % sorted(usage))

# R5 耗时：慢那家 4s、超时 2s，一次 /api/hub/accounts 不该被拖到 4s 以上
status, raw, elapsed = call("/api/hub/accounts")
H.check("R5 慢引擎被 timeout 兜住（单次聚合 < 3.5s）", status == 200 and elapsed < 3.5,
        "%.2fs" % elapsed)

# R6 阳性对照：假引擎自己确实在发疯（否则上面全是空过）
try:
    with urllib.request.urlopen("http://127.0.0.1:%d/health" % ports["wb"], timeout=5) as resp:
        fake_status, fake_body = resp.status, resp.read(40)
except Exception as exc:                      # noqa: BLE001
    fake_status, fake_body = 0, str(exc).encode()
H.check("R6 阳性对照：wb 假引擎确实返回非 JSON", fake_status == 200 and b"{" not in fake_body,
        "%d %r" % (fake_status, fake_body[:24]))

H.finish()
