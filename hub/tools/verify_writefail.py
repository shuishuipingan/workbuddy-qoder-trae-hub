#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_writefail.py -- 写盘失败时必须给出 500 write_failed，而不是断开连接（第 240 轮）。

造失败的两个办法（不碰 ACL，跨环境可复跑）：
  A. 设置：把 <settings>.json.tmp 换成同名目录 -> open(tmp, "w") 必然失败
  B. 账号：把 <uid>.json 换成同名目录       -> os.replace(tmp, path) 必然失败

判据：两条都要 500 + type=write_failed，且引擎仍然 /health 200。
用法：python hub/tools/verify_writefail.py [--keep]
"""
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time
import urllib.error, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
PORTS = {"wb": 16411, "qd": 16412, "trae": 16413}
PORTAL = 16417


def wait_port(port, timeout=45):
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
    url = "http://127.0.0.1:%d%s" % (PORTAL, path)
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Panel-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"_err": str(exc)[:80]}


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
    return proc, wait_port(PORTAL)


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def kind_of(body):
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("type") or err.get("kind") or "")
    return str(body.get("type") or body.get("kind") or "")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python hub/tools/verify_writefail.py")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    root = tempfile.mkdtemp(prefix="hub-writefail-")
    checks, blockers, proc = [], [], None
    try:
        subprocess.run([PY, os.path.join(ROOT, "hub", "tools", "make_fixture.py"),
                        "--out", root, "--scenario", "all"], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        proc, up = boot(root)
        if not up:
            print("实例没起来（端口被占？）")
            return 1
        _, login = call("POST", "/api/wb/panel/login", {"password": "admin"})
        tok = login.get("token", "")
        acc_dir = os.path.join(root, "wb", "accounts")
        settings = os.path.join(acc_dir, "settings.json")
        blocker_a = settings + ".tmp"
        shutil.rmtree(blocker_a, ignore_errors=True)
        os.makedirs(blocker_a, exist_ok=True)
        blockers.append(blocker_a)
        st, body = call("POST", "/api/wb/settings/save", {"pricing_refresh_minutes": 31}, tok)
        checks.append(("A 设置写失败 -> 500 write_failed",
                       st == 500 and kind_of(body) == "write_failed",
                       "status=%s kind=%s" % (st, kind_of(body) or "(空)")))
        checks.append(("A2 设置写失败后引擎仍活着", call("GET", "/api/wb/health")[0] == 200, "health != 200"))
        # C. 定价映射写失败（第 242 轮补）
        overrides = os.path.join(root, "wb", "usage", "pricing-overrides.json")
        os.makedirs(os.path.dirname(overrides), exist_ok=True)
        shutil.rmtree(overrides, ignore_errors=True)
        if os.path.isfile(overrides):
            os.unlink(overrides)
        os.makedirs(overrides, exist_ok=True)
        blockers.append(overrides)
        st3, body3 = call("POST", "/api/wb/pricing/mapping",
                          {"model": "gpt-4o", "or_id": "openai/gpt-4o"}, tok)
        checks.append(("C 定价映射写失败 -> 500 write_failed",
                       st3 == 500 and kind_of(body3) == "write_failed",
                       "status=%s kind=%s" % (st3, kind_of(body3) or "(空)")))
        checks.append(("C2 定价写失败后引擎仍活着", call("GET", "/api/wb/health")[0] == 200, "health != 200"))
        _, acc = call("GET", "/api/wb/accounts?realm=cn", tok=tok)
        lst = acc.get("accounts") or []
        if not lst:
            checks.append(("B 账号写失败 -> 500 write_failed", None, "数据根里没有账号"))
        else:
            uid = lst[0].get("uid") or lst[0].get("id")
            target = os.path.join(acc_dir, "%s.json" % uid)
            saved = None
            if os.path.isfile(target):
                with open(target, "rb") as fh:
                    saved = fh.read()
                os.unlink(target)
            os.makedirs(target, exist_ok=True)
            st2, body2 = call("POST", "/api/wb/accounts/set", {"uid": uid, "enabled": True}, tok)
            checks.append(("B 账号写失败 -> 500 write_failed",
                           st2 == 500 and kind_of(body2) == "write_failed",
                           "status=%s kind=%s" % (st2, kind_of(body2) or "(空)")))
            checks.append(("B2 账号写失败后引擎仍活着", call("GET", "/api/wb/health")[0] == 200, "health != 200"))
            shutil.rmtree(target, ignore_errors=True)
            if saved is not None:
                with open(target, "wb") as fh:
                    fh.write(saved)
    finally:
        for b in blockers:
            shutil.rmtree(b, ignore_errors=True)
        if proc:
            stop(proc)
        if args.keep:
            print("数据根保留在:", root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    bad = 0
    for name, ok, detail in checks:
        if ok is None:
            print("  [SKIP] " + name + "  (" + detail + ")")
            continue
        print(("  [PASS] " if ok else "  [FAIL] ") + name + ("" if ok else "  -> " + detail))
        if not ok:
            bad += 1
    total = len([c for c in checks if c[1] is not None])
    print("SUMMARY: %d 项，%d 失败" % (total, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
