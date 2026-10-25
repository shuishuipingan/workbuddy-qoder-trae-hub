#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_persistence.py —— 写入 → 重启 → 逐项核对（第 235 轮）。

为什么要有它：用户最怕"重启之后我的账号/设置/密钥没了"。
这类问题在单进程测试里看不出来，必须真的起停一次。

做的三件事：
  1. 起一个干净数据根的实例（复制一份夹具）
  2. 写三类数据：引擎级 API Key、引擎设置项、合并 key
  3. 重启（同端口同数据），逐项读回来比对

用法：
  python hub/tools/verify_persistence.py            # 用临时数据根
  python hub/tools/verify_persistence.py --keep     # 保留数据根便于排查
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
PORTS = {"wb": 16911, "qd": 16912, "trae": 16913}
PORTAL = 16917


def wait_port(port, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def call(method, path, body=None, tok=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORTAL, path), data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Panel-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except Exception as exc:                       # noqa: BLE001
        return {"_err": str(exc)[:80]}


def boot(data_root):
    env = dict(os.environ)
    env["HUB_DATA_ROOT"] = data_root
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [PY, "-u", "-m", "hub.server", "--host", "127.0.0.1"]
    for name, port in PORTS.items():
        cmd += ["--%s-port" % name, str(port)]
    cmd += ["--portal-port", str(PORTAL)]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = wait_port(PORTAL)
    return proc, ok


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:                              # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python hub/tools/verify_persistence.py")
    ap.add_argument("--keep", action="store_true", help="保留临时数据根")
    args = ap.parse_args(argv)

    root = tempfile.mkdtemp(prefix="hub-persist-")
    if not args.keep:
        pass
    checks = []

    proc = None
    try:
        proc, up = boot(root)
        if not up:
            print("实例没起来（端口被占？）")
            return 1

        tok = call("POST", "/api/wb/panel/login", {"password": "admin"}).get("token", "")
        doc = call("GET", "/api/wb/settings", tok=tok)
        keys = [{"id": k.get("id"), "name": k.get("name"), "key": "",
                 "realm": k.get("realm"), "models": k.get("models")}
                for k in (doc.get("api_keys") or [])]
        keys.append({"id": "k-persist", "name": "persist-check", "key": "sk-persist-123",
                     "realm": "", "models": ""})
        call("POST", "/api/wb/settings/save",
             {"api_keys": keys, "pricing_refresh_minutes": 17}, tok=tok)
        call("POST", "/api/hub/settings/save",
             {"merged_keys": [{"id": "", "name": "persist-merged",
                               "key": "mrg-persist-1", "enabled": True}]}, "admin")

        stop(proc)
        proc, up = boot(root)
        if not up:
            print("重启后没起来")
            return 1

        tok = call("POST", "/api/wb/panel/login", {"password": "admin"}).get("token", "")
        doc = call("GET", "/api/wb/settings", tok=tok)
        names = [k.get("name") for k in (doc.get("api_keys") or [])]
        checks.append(("引擎级 API Key 重启后仍在", "persist-check" in names, str(names)))
        checks.append(("设置项重启后仍是新值", doc.get("pricing_refresh_minutes") == 17,
                       str(doc.get("pricing_refresh_minutes"))))
        merged = [m.get("name") for m in (call("GET", "/api/hub/keys").get("merged") or [])]
        checks.append(("合并 key 重启后仍在", "persist-merged" in merged, str(merged)))
    finally:
        if proc:
            stop(proc)
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("数据根保留在:", root)

    bad = 0
    for name, ok, detail in checks:
        print(("  [PASS] " if ok else "  [FAIL] ") + name + ("" if ok else "  → " + detail))
        if not ok:
            bad += 1
    print("SUMMARY: %d 项，%d 通过，%d 失败" % (len(checks), len(checks) - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
