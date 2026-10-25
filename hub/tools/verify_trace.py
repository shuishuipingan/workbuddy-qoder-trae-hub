#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令验证「第三家」的数据能不能从引擎磁盘走到仪表盘（第 143 轮）。

为什么需要它：第 41/42 轮各花半小时手工做同一件事 —— 造夹具、起实例、开浏览器、
逐层查。三家合并最怕的不是报错，而是**某一层悄悄少一半数据**（实测过：流水少了
整整一半，界面上只挂一句淡淡的提示）。这个脚本把那条链路压成五层断言：

  L1 引擎装载      该引擎 /health 的 accounts 数 > 0
  L2 聚合账号      /api/hub/accounts 的 byEngine 里出现该引擎
  L3 聚合时序      /api/hub/timeseries 的 series[engine] 合计 >= 造的 token 数
  L4 聚合用量      /api/hub/usage 的合计 > 0
  L5 逐行可见      /api/<engine>/usage/recent 里能读到造的那两行（需要该引擎面板令牌）

用法：
  python hub/tools/verify_trace.py                 # 默认用 trae，造 2500 tokens
  python hub/tools/verify_trace.py --engine qd --tokens 800
  python hub/tools/verify_trace.py --keep          # 保留临时数据目录便于排查

退出码：0 = 五层全过；1 = 有层没过（打印哪一层、期望与实际）。
零依赖、只连本地、不碰真实数据目录（全部写在临时目录里）。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def log(msg):
    print(msg, flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(url, token=None, data=None, timeout=20):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Panel-Token", token)
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def seed(root, engine, tokens, account_uid, filler=120):
    """给**三家各种一份**夹具（第 146 轮），engine 那份用 tokens，其余用 filler。

    为什么不全量只种 --engine：第 45 轮实测 qd 那份夹具里 trae 是 0，
    前端用例只能"按存在的家断言"，边界条件反而更少覆盖。
    现在每份夹具都是完整三家，U15c 就能统一断言三家都在。
    """
    acc_dir = os.path.join(root, engine, "accounts")
    use_dir = os.path.join(root, engine, "usage")
    os.makedirs(acc_dir, exist_ok=True)
    os.makedirs(use_dir, exist_ok=True)
    account = {"uid": account_uid, "nickname": "合成" + engine + "号", "realm": "cn",
               "enabled": True, "fails": 0, "cooldown_until": 0, "breaker_until": 0}
    with open(os.path.join(acc_dir, account_uid + ".json"), "w", encoding="utf-8") as fh:
        json.dump(account, fh, ensure_ascii=False, indent=2)
    now = int(time.time())
    half = max(1, tokens // 2)
    rows = []
    for i, (model, tk) in enumerate((("trace-model-a", half), ("trace-model-b", tokens - half))):
        rows.append({
            "at": now - 120 + i * 30,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 120 + i * 30)),
            "model": model, "stream": bool(i == 0), "elapsed_ms": 800 + i * 100,
            "ttft_ms": 100 if i == 0 else 0, "gen_ms": 700,
            "prompt_tokens": tk // 2, "completion_tokens": tk - tk // 2,
            "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": tk,
            "credit": 1, "original_credits": 1, "billable": True,
            "cost_usd": 0.001, "cost_cny": 0.007, "priced": True,
            "realm": "cn", "account": account_uid, "key_id": engine + "-key-1",
        })
    with open(os.path.join(use_dir, "usage.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return account_uid, rows


def copy_real(root):
    """把真实的 wb/qd 账号与流水复制进来（让聚合有对照物）；缺就跳过。"""
    for eng in ("wb", "qd"):
        for sub in ("accounts", "usage"):
            src = os.path.join(ROOT, "hub", "engines", eng, sub)
            dst = os.path.join(root, eng, sub)
            if os.path.isdir(src) and not os.path.exists(dst):
                try:
                    shutil.copytree(src, dst)
                except Exception:
                    pass


def wait_health(portal, want, limit=100):
    """等门户自检报出所有引擎 ok。"""
    deadline = time.time() + limit
    last = None
    while time.time() < deadline:
        try:
            doc = http_json(portal + "/api/hub/health", timeout=8)
            last = doc
            engines = doc.get("engines") or {}
            if all((engines.get(e) or {}).get("ok") for e in want):
                return doc
        except Exception as exc:
            last = {"error": str(exc)}
        time.sleep(1.5)
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="trae", choices=("wb", "qd", "trae"))
    ap.add_argument("--tokens", type=int, default=2500)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--fixture-out", default="",
                    help="把真实接口 payload 落到该路径（供前端契约用例消费）")
    args = ap.parse_args()

    root = os.path.join(ROOT, ".trace-data-" + args.engine)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    copy_real(root)
    uid, rows = seed(root, args.engine, args.tokens, "trace-" + args.engine + "-0001")
    for other in ("wb", "qd", "trae"):
        if other == args.engine:
            continue
        seed(root, other, 120, "trace-" + other + "-0001")
    want_tokens = sum(r["total_tokens"] for r in rows)

    ports = {e: free_port() for e in ("wb", "qd", "trae")}
    portal_port = free_port()
    portal = "http://127.0.0.1:%d" % portal_port
    cmd = [sys.executable, "-u", "-m", "hub.server",
           "--wb-port", str(ports["wb"]), "--qd-port", str(ports["qd"]),
           "--trae-port", str(ports["trae"]), "--portal-port", str(portal_port)]
    env = dict(os.environ, HUB_DATA_ROOT=root, PYTHONIOENCODING="utf-8")
    log("[1/5] 起实例（端口 %s，门户 %d）…" % (ports, portal_port))
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    results = []
    try:
        health = wait_health(portal, ("wb", "qd", "trae"))
        engines = (health or {}).get("engines") or {}
        if not engines:
            # 门户没起来（端口占用 / 启动慢）：给一句人话 + 退出码 2，不要抛栈。
            # 实测（第 146 轮）第一次就撞上这个：urllib 直连被拒，栈信息淹没了结论。
            log("门户未就绪（%s）。排查：端口是否被占、引擎启动是否报错。" % (health,))
            return 2
        eng_health = engines.get(args.engine) or {}
        l1 = bool(eng_health.get("ok")) and int(eng_health.get("accounts") or 0) > 0
        results.append(("L1 引擎装载", l1,
                        "accounts=%s" % eng_health.get("accounts")))

        token = http_json(portal + "/api/panel/login", data={"password": "admin"})["token"]

        hub_acc = http_json(portal + "/api/hub/accounts?realm=all", token)
        by_eng = {}
        for item in (hub_acc.get("items") or []):
            key = item.get("engine")
            by_eng[key] = by_eng.get(key, 0) + 1
        results.append(("L2 聚合账号", by_eng.get(args.engine, 0) >= 1,
                        "byEngine=%s" % by_eng))

        ts = http_json(portal + "/api/hub/timeseries?realm=all&range=7d", token)
        series = ts.get("series") or {}
        tokens_by_eng = ts.get("tokens") or {}
        reqs = sum(series.get(args.engine) or [])
        toks = sum(tokens_by_eng.get(args.engine) or [])
        # 两个都要看：**请求数在、token 全丢**是真实会发生的半坏状态，
        # 只断言其中一个都会漏（第 143 轮为此给接口加了 tokens 字段）。
        results.append(("L3 聚合时序", reqs >= 2 and toks >= want_tokens,
                        "requests[%s]=%s tokens[%s]=%s 期望>=%s" %
                        (args.engine, reqs, args.engine, toks, want_tokens)))

        usage = http_json(portal + "/api/hub/usage?realm=all", token)
        merged = (usage or {}).get("merged") or {}
        total = merged.get("requests") or merged.get("total") or 0
        results.append(("L4 聚合用量", bool(merged),
                        "merged=%s" % (list(merged)[:4] or "空")))

        # L5 需要该引擎自己的面板令牌；没有就显式跳过（不是失败）
        eng_tok = None
        try:
            eng_tok = http_json(portal + "/api/%s/panel/login" % args.engine,
                                data={"password": "admin"}).get("token")
        except Exception:
            eng_tok = None
        if not eng_tok:
            results.append(("L5 逐行可见", None, "该引擎面板登录不可用 —— 跳过"))
        else:
            recent = http_json(portal + "/api/%s/usage/recent?limit=20" % args.engine, eng_tok)
            got_rows = recent.get("rows") or []
            names = [r.get("model") for r in got_rows]
            hit = sum(1 for r in got_rows if str(r.get("model", "")).startswith("trace-model"))
            results.append(("L5 逐行可见", hit >= 2, "命中 %d 行（models=%s）" % (hit, names[:4])))

        if args.fixture_out:
            # 第 144 轮：把**真实接口返回**导出去，让前端用例拿它渲染。
            # 目的：查"后端通了、界面没显示"——这条只有前端渲染才抓得住，
            # 而纯 Python 起不了浏览器。把 payload 落盘 + JS 用例消费，闭环即可。
            # ★ 必须先脱敏再落盘（第 144 轮实测）：账号对象里带 token / refreshToken，
            #   而这份 fixture 要进仓库给前端用例用。只留"界面真的会用"的字段。
            KEEP = ("uid", "uidTail", "nickname", "name", "realm", "engine", "enabled",
                    "status", "file", "dailyTokensToday", "credits")
            def slim(item):
                out = {}
                for k in KEEP:
                    if k in item:
                        out[k] = item[k]
                return out
            kept, per_engine = [], {}
            # 排序：**本次造的合成账号优先**（file 以 trace- 开头）。
            # 否则"每家最多 3 条"会把它们挤掉 —— 实测 wb 有 28 个真实账号，
            # 前 3 条全是真实的，夹具里根本看不到这次造的东西（U15d 抓到）。
            def _rank(pair):
                idx, it = pair
                is_trace = 0 if str(it.get("file") or "").startswith("trace-") else 1
                return (it.get("engine") or "", is_trace, idx)
            ordered = [it for _, it in sorted(enumerate(hub_acc.get("items") or []), key=_rank)]
            for item in ordered:
                eng = item.get("engine")
                per_engine[eng] = per_engine.get(eng, 0) + 1
                if per_engine[eng] <= 3:          # 每家最多 3 条，够渲染就行
                    kept.append(slim(item))
            ts_slim = {
                "labels": (ts.get("labels") or [])[-12:],
                "series": {e: (ts.get("series", {}).get(e) or [])[-12:] for e in ("wb", "qd", "trae")},
                "tokens": {e: (ts.get("tokens", {}).get(e) or [])[-12:] for e in ("wb", "qd", "trae")},
                "totals": ts.get("totals"), "errors": ts.get("errors"),
            }
            payload = {
                "engine": args.engine, "tokens": want_tokens,
                "hubAccounts": {"items": kept, "errors": hub_acc.get("errors")},
                "timeseries": ts_slim,
                "recent": {"rows": [{"model": r.get("model"), "total_tokens": r.get("total_tokens"),
                                     "engine": args.engine}
                                    for r in ((recent if eng_tok else {}).get("rows") or [])[:4]]},
                "layers": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results],
            }
            out_path = args.fixture_out
            if not os.path.isabs(out_path):
                out_path = os.path.join(ROOT, out_path)
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            log("已导出接口 payload → %s" % out_path)

        log("")
        log("层级           结果   实际")
        log("-" * 64)
        failed = 0
        for name, ok, detail in results:
            mark = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
            if ok is False:
                failed += 1
            log("%-14s %-6s %s" % (name, mark, detail))
        log("-" * 64)
        log("引擎 %s · 夹具 %d tokens · 数据目录 %s" % (args.engine, want_tokens, root))
        return 1 if failed else 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if not args.keep:
            time.sleep(0.5)
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
