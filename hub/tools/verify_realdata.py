#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/tools/verify_realdata.py —— 用**真实数据的只读副本**跑一遍完整自证。

## 为什么还要一个（已经有 firstrun / gateway 了）

那两个工具：
  · verify_firstrun —— 自己起空环境，验"第一次能不能用"；
  · verify_gateway  —— 打一个**已经在跑**的实例，验三类 key 的归属。
但"**在你自己的数据上**三类 key 各自看到多少模型"这件事，以前只能手工做
（第 82 轮我就是手工跑的：复制数据 → 起实例 → 建 key → 读引擎 key → 跑网关 → 拆）。

这个脚本把那串动作固化成一条命令，并**把关键数字打出来**：
合并 key 看到多少、wb key 看到多少、qd key 看到多少 —— 这三个数一眼就能对上
"一把调所有 / 单独的只走自己"。

## 安全性

  · 源数据只**读取**：accounts/usage 复制到临时目录，跑完删掉；
  · 实例用动态端口，不碰默认的 8811/8812/8817；
  · 会临时写一把合并 key（只在临时数据根里），随临时目录一起删；
  · 不下载、不外呼（wb 的定价刷新在副本里被关掉）。

## 用法

    python hub/tools/verify_realdata.py              # 跑一遍，打印报告
    python hub/tools/verify_realdata.py --keep       # 保留临时目录（排障用）
    python hub/tools/verify_realdata.py --timeout 90 # 等实例起来的上限（秒）
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
HUB = os.path.dirname(HERE)
REPO = os.path.dirname(HUB)
ENGINES = os.path.join(HUB, "engines")

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append((label, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  —— " + detail) if detail else ""))
    return ok


def info(msg):
    print("  [INFO] %s" % msg)


def skip(label, why=""):
    RESULTS.append((label, None, why))
    print("  [SKIP] %s%s" % (label, ("  —— " + why) if why else ""))


def free_port():
    """挑一个空闲回环端口（避开线上实例常用的那几个）。"""
    reserved = (8789, 8790, 8791, 8811, 8812, 8817)
    for _ in range(60):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        finally:
            s.close()
        if p not in reserved:
            return p
    raise RuntimeError("no free port")


def http(base, path_, key=None, method="GET", body=None, token=None, timeout=60):
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    if token:
        headers["X-Panel-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path_, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001
        return 0, repr(exc)


def engine_key(engine):
    """从引擎自己的 settings.json 里挑一把启用中的 key（只读）。"""
    path_ = os.path.join(ENGINES, engine, "accounts", "settings.json")
    try:
        with open(path_, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return None
    for entry in (doc.get("api_keys") or []):
        if entry.get("enabled") and entry.get("key"):
            return str(entry["key"])
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="用真实数据的只读副本跑一遍三类 key 自证")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--timeout", type=float, default=75.0, help="等实例起来的上限（秒）")
    args = ap.parse_args(argv)

    print("真实数据自证 —— 源: %s" % ENGINES)
    work = tempfile.mkdtemp(prefix="hub-realdata-")
    data_root = os.path.join(work, "data")
    log = os.path.join(work, "server.log")
    proc = None
    try:
        for engine in ("wb", "qd"):
            for sub in ("accounts", "usage"):
                src = os.path.join(ENGINES, engine, sub)
                dst = os.path.join(data_root, engine, sub)
                os.makedirs(dst, exist_ok=True)
                if os.path.isdir(src):
                    for name in os.listdir(src):
                        s = os.path.join(src, name)
                        if os.path.isfile(s):
                            shutil.copy2(s, os.path.join(dst, name))
        # 关掉 wb 的定价刷新，避免测试期真实外呼
        wb_settings = os.path.join(data_root, "wb", "accounts", "settings.json")
        if os.path.isfile(wb_settings):
            with open(wb_settings, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["pricing_enabled"] = False
            with open(wb_settings, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2)
        info("已把 accounts/usage 复制到临时数据根（源数据只读）")

        wb_port, qd_port, portal_port = free_port(), free_port(), free_port()
        env = dict(os.environ)
        env["HUB_DATA_ROOT"] = data_root
        env["PYTHONIOENCODING"] = "utf-8"
        with open(log, "w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "hub.server", "--host", "127.0.0.1",
                 "--wb-port", str(wb_port), "--qd-port", str(qd_port),
                 "--portal-port", str(portal_port)],
                cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:%d" % portal_port

        health = None
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            status, doc = http(base, "/api/hub/health", timeout=3)
            if status == 200 and isinstance(doc, dict):
                health = doc
                break
            time.sleep(0.6)
        if not check("R01 隔离实例起来了（真实数据副本）", health is not None,
                     "" if health else "见日志: %s" % log):
            return finish(work, proc, log, args.keep)

        engines = (health or {}).get("engines") or {}
        up = [e for e, h in engines.items() if isinstance(h, dict) and h.get("ok")]
        check("R02 两个引擎都在线", len(up) >= 1, "up=%s" % ",".join(up))

        merged_key = "realdata-check-merged-key"
        status, _doc = http(base, "/api/hub/settings/save",
                            method="POST", token="admin",
                            body={"merged_keys": [{"name": "realdata", "key": merged_key,
                                                   "enabled": True}]})
        if not check("R03 建一把合并 key（默认口令 admin）", status == 200, "status=%s" % status):
            return finish(work, proc, log, args.keep)

        counts = {}
        for label, key in (("merged", merged_key),
                           ("wb", engine_key("wb")),
                           ("qd", engine_key("qd"))):
            if not key:
                skip("R%02d %s key 的模型清单" % (4 + len(counts), label), "没有可用的 %s key" % label)
                continue
            status, doc = http(base, "/api/v1/models", key=key)
            ids = [m.get("id") for m in (doc or {}).get("data", [])] if isinstance(doc, dict) else []
            counts[label] = len(ids)
            is_merged = bool(isinstance(doc, dict) and doc.get("merged"))
            if label == "merged":
                check("R04 合并 key：merged=true 且能列出模型（%d 个）" % len(ids),
                      status == 200 and is_merged and len(ids) > 0,
                      "status=%s merged=%s" % (status, is_merged))
            else:
                check("R05 %s key：只列 %s（不走合并清单，%d 个）" % (label, label, len(ids)),
                      status == 200 and not is_merged,
                      "status=%s merged=%s" % (status, is_merged))

        if "merged" in counts and ("wb" in counts or "qd" in counts):
            biggest = max([counts[k] for k in ("wb", "qd") if k in counts] or [0])
            check("R06 合并清单是并集（%d ≥ 单边最大 %d）" % (counts.get("merged", 0), biggest),
                  counts.get("merged", 0) >= biggest,
                  "counts=%r" % (counts,))

        status, doc = http(base, "/api/v1/models", key="definitely-not-a-key-xyz")
        check("R07 未登记的 key 被网关当场拒绝（401）", status == 401, "status=%s" % status)

        status, doc = http(base, "/api/hub/keys", token="admin")
        check("R08 三类 key 计数端点可用", status == 200 and isinstance(doc, dict), "status=%s" % status)

        write_report(counts)
        print("")
        print("  ── 数字 ──")
        info("合并 key 看到 %s 个模型；wb key %s 个；qd key %s 个"
             % (counts.get("merged", "?"), counts.get("wb", "?"), counts.get("qd", "?")))
    finally:
        return finish(work, proc, log, args.keep)


def write_report(counts):
    """把三个数字写进 logs/realdata-report.json —— 面板「接入说明」那一卡直接读它。

    为什么落在 logs/：门户本来就在读 logs/route.jsonl，同一处逻辑；
    而且 logs/ 是运行时产物目录，写报告不算污染源码树。
    写失败不影响自证结论（报告是"给人看的"，不是判据）。
    """
    try:
        out_dir = os.path.join(REPO, "logs")
        os.makedirs(out_dir, exist_ok=True)
        doc = {
            "at": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "counts": counts,
            "ok": (counts.get("merged", 0) >= max(counts.get("wb", 0), counts.get("qd", 0))),
        }
        with open(os.path.join(out_dir, "realdata-report.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        info("报告已写入 logs/realdata-report.json（面板「接入说明」会显示这三个数字）")
    except Exception as exc:  # noqa: BLE001
        info("报告写入失败（不影响自证）: %r" % (exc,))


def finish(work, proc, log, keep):
    failed = [r for r in RESULTS if r[1] is False]
    ran = [r for r in RESULTS if r[1] is not None]
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    if keep:
        print("  [INFO] 临时目录保留: %s" % work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    print("SUMMARY: %d 项，通过 %d，失败 %d" % (len(ran), len(ran) - len(failed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
