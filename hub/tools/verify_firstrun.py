#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/tools/verify_firstrun.py —— 「空环境第一次能不能用」的可复跑回归。

为什么单独一条：之前的套件都在**已有数据**的实例上验证。而本轮发现的两个真 bug
（建合并 key 401、保存路由 401）**只在空环境里出现** —— 它们来自"令牌当口令"，
有数据时会话里恰好带着能过的令牌，于是永远看不见。

它做的事（全部走真实 HTTP，不碰 UI）：

  F01 起一个隔离实例（临时数据根 + 空闲端口）
  F02 三家引擎都在线
  F03 空环境的自检：合并 key 为 0（这是起点，不是失败）
  F04 **用默认口令 admin 建一把合并 key**（这一步曾经稳定 401）
  F05 /api/hub/keys 能看到这把 key（掩码）
  F06 **用默认口令保存路由偏好**（这一步也曾经稳定 401）
  F07 用新 key 请求 /api/v1/models：200 且 merged=true
  F08 自检里 keys-merged 变为通过
  F09 拆实例、清临时目录（--keep 可保留现场）

用法：
    python hub/tools/verify_firstrun.py
    python hub/tools/verify_firstrun.py --keep          # 保留临时目录与日志
    python hub/tools/verify_firstrun.py --python <exe>  # 指定解释器（默认用当前）

只用标准库；退出码 0/1，可直接接 CI。
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RESULTS = []

#: 面板依赖的融合端点 —— 与 hub/server.py 的 CAP_ENDPOINTS、前端 PANEL_CAPS、
#: 接入说明页的 CAPS **同一份清单**。四处一致才不会出现"横幅说全在、页面说缺一项"。
CAP_ENDPOINTS = ("hub/keys", "hub/selfcheck", "hub/try", "hub/accounts",
                 "hub/route-stats", "hub/rewards", "hub/scheduler")


def check(label, ok, detail=""):
    RESULTS.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (("   —— " + str(detail)[:240]) if (detail and not ok) else ""),
          flush=True)


def skip(label, why=""):
    print("  [SKIP] " + label + (("   —— " + why) if why else ""), flush=True)


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def call(base, path_, method="GET", body=None, token=None, timeout=20, bearer=None):
    """token → X-Panel-Token（**面板**通道）；bearer → Authorization（**API** 通道）。
    两者不能混：/api/v1/* 只认 Bearer，用面板头去打会被引擎当成无效 key ——
    第一版本脚本就犯了这错，于是"合并 key 被接受"其实是打到了面板端点上（假绿）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Panel-Token"] = token
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(base + path_, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)[:200]


def main(argv=None):
    ap = argparse.ArgumentParser(description="空环境首次可用回归")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--with-data", action="store_true",
                    help="把仓库里现有的账号目录**复制**到隔离数据根，用来验证『加账号后合并视图是并集』")
    args = ap.parse_args(argv)

    wb, qd, trae, portal = free_port(), free_port(), free_port(), free_port()
    root = tempfile.mkdtemp(prefix="hub-firstrun-")
    log_path = os.path.join(root, "server.log")
    base = "http://127.0.0.1:%d" % portal
    print("隔离实例: 数据根=%s  端口 wb/qd/trae/portal=%d/%d/%d/%d"
          % (root, wb, qd, trae, portal), flush=True)

    # 可选：复制现有账号目录（只读复制，不写回源目录）——
    # 空环境下合并视图必然退化成单边，只有带账号才能验"并集"这件事本身。
    if args.with_data:
        for eng in ("wb", "qd", "trae"):
            src = os.path.join(REPO, "hub", "engines", eng, "accounts")
            dst = os.path.join(root, eng, "accounts")
            if os.path.isdir(src):
                try:
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    print("  已复制 %s 账号目录 → %s" % (eng, dst), flush=True)
                except Exception as exc:  # noqa: BLE001
                    print("  [!] 复制 %s 账号失败: %r" % (eng, exc), flush=True)

    env = dict(os.environ)
    env["HUB_DATA_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [args.python, "-m", "hub.server", "--host", "127.0.0.1",
         "--wb-port", str(wb), "--qd-port", str(qd), "--trae-port", str(trae),
         "--portal-port", str(portal)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        # F01/F02：等门户 + 三家引擎
        deadline = time.time() + 90
        health = None
        while time.time() < deadline:
            st, body = call(base, "/api/hub/health", timeout=3)
            if st == 200:
                try:
                    doc = json.loads(body)
                except Exception:
                    doc = {}
                engines = doc.get("engines") or {}
                if all(engines.get(e) for e in ("wb", "qd", "trae")):
                    health = doc
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        check("F01 隔离实例起来了（门户 /api/hub/health 有应答）", health is not None,
              "进程活着=%s 日志尾部见 %s" % (proc.poll() is None, log_path))
        if health is None:
            return finish(root, proc, log, args.keep)
        engines = health.get("engines") or {}
        check("F02 三家引擎都在线",
              all((engines.get(e) or {}).get("ok") is True for e in ("wb", "qd", "trae")),
              json.dumps(engines, ensure_ascii=False)[:200])

        # F03：空环境起点
        st, body = call(base, "/api/hub/keys")
        counts = {}
        try:
            counts = (json.loads(body) or {}).get("counts") or {}
        except Exception:
            pass
        check("F03 空环境起点：合并 key 为 0（这是起点，不是失败）",
              st == 200 and int(counts.get("merged") or 0) == 0, "counts=%s" % counts)

        # F04：用**默认口令**建合并 key（曾经稳定 401）
        secret = "fr-" + os.urandom(8).hex()
        st, body = call(base, "/api/hub/settings/save", "POST",
                        {"merged_keys": [{"name": "firstrun", "key": secret, "enabled": True}]},
                        token="admin")
        check("F04 用默认口令 admin 建一把合并 key（曾经 401）", st == 200 and '"ok": false' not in body,
              "status=%s body=%s" % (st, body[:160]))

        # F05：能看到它（掩码）
        st, body = call(base, "/api/hub/keys")
        try:
            doc = json.loads(body)
        except Exception:
            doc = {}
        merged = doc.get("merged") or []
        check("F05 /api/hub/keys 能看到这把 key（掩码、不是明文）",
              st == 200 and len(merged) >= 1 and secret not in body,
              "n=%d body=%s" % (len(merged), body[:160]))

        # F06：用默认口令保存路由（曾经也 401）
        st, body = call(base, "/api/hub/settings/save", "POST",
                        {"routing": {"enabled": True, "prefer_realm": "intl", "engine_prefer": "auto"}},
                        token="admin")
        st2, body2 = call(base, "/api/hub/settings")
        try:
            prefer = ((json.loads(body2) or {}).get("routing") or {}).get("prefer_realm")
        except Exception:
            prefer = None
        check("F06 用默认口令 admin 保存路由偏好并读回（曾经 401）",
              st == 200 and prefer == "intl", "save=%s prefer=%s body=%s" % (st, prefer, body[:120]))

        # F07：新 key 取模型
        st, body = call(base, "/api/v1/models", bearer=secret)
        try:
            doc = json.loads(body)
            n = len(doc.get("data") or [])
            merged_flag = doc.get("merged") is True
        except Exception:
            n, merged_flag = 0, False
        # ⚠️ 这里**不能**要求 merged=true：空环境里两个引擎都没有账号，
        #   合并视图没有可借用的上游凭据，会退化成"能答的那一边"（实测 n=17）。
        #   判据只钉「这把 key 被认了」——401 才是问题。退化本身在下面说明。
        check("F07 用新 key 请求 /api/v1/models：被接受（不是 401）",
              st == 200 and n > 0, "status=%s merged=%s n=%s" % (st, merged_flag, n))
        if st == 200 and not merged_flag:
            print("        · 说明：空环境无账号 → 合并视图退化为单边（merged=false, n=%d）；"
                  "加账号后即为并集。" % n, flush=True)

        # F07b（仅 --with-data）：账号齐了以后，合并视图必须是**并集**
        if args.with_data:
            try:
                merged_ids = set(str(m.get("id") or "").lower()
                                 for m in (json.loads(body).get("data") or []) if m.get("id"))
            except Exception:
                merged_ids = set()
            stx, bodyx = call(base, "/api/hub/models")
            per = {}
            try:
                for item in ((json.loads(bodyx) or {}).get("data") or []):
                    eng = str(item.get("engine") or "?")
                    per[eng] = per.get(eng, 0) + 1
            except Exception:
                per = {}
            biggest = max(per.values()) if per else 0
            check("F07b 带账号时：合并视图是并集（merged=true 且条数 >= 任一边）",
                  merged_flag and len(merged_ids) >= biggest and len(per) >= 2,
                  "merged=%s 合并=%d 各引擎=%s" % (merged_flag, len(merged_ids), per))

        # F08：自检认为"合并 key 已登记"
        st, body = call(base, "/api/hub/selfcheck")
        try:
            doc = json.loads(body)
        except Exception:
            doc = {}
        km = [c for c in (doc.get("checks") or []) if c.get("id") == "keys-merged"]
        check("F08 自检里 keys-merged 通过", bool(km) and km[0].get("ok") is True,
              json.dumps(km[:1], ensure_ascii=False)[:200])
        # F08b：融合端点清单（口径：405 也算有 —— 端点只收 POST，例如 /hub/try）
        missing_caps = []
        for name in CAP_ENDPOINTS:
            st_cap, _body = call(base, "/api/" + name, timeout=10)
            if st_cap not in (200, 204, 405):
                missing_caps.append("%s(%s)" % (name, st_cap))
        check("F08b 融合端点 %d 项全在（与启动横幅 / 面板能力表同一份清单）" % len(CAP_ENDPOINTS),
              not missing_caps, "缺: " + ", ".join(missing_caps))

    finally:
        return finish(root, proc, log, args.keep)


def finish(root, proc, log, keep):
    ok_teardown = True
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        ok_teardown = proc.poll() is not None
    except Exception:
        ok_teardown = False
    try:
        log.close()
    except Exception:
        pass
    check("F09 隔离实例已收掉（无孤儿进程）", ok_teardown, "poll=%s" % (proc.poll(),))
    if keep:
        print("  现场保留: %s" % root, flush=True)
    else:
        try:
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass
    failed = [r for r in RESULTS if not r[1]]
    print("")
    print("SUMMARY: %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
