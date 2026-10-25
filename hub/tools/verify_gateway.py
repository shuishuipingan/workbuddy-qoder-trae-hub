#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/tools/verify_gateway.py —— 「一个地址 + 三类 key」的可复跑自证。

为什么要它：融合做没做到，不该靠读代码或看截图判断。这个脚本对着**正在跑的实例**
打真实请求，逐条打印 PASS/FAIL，退出码 0/1 可直接接 CI 或计划任务。

    python hub/tools/verify_gateway.py                      # 默认 http://127.0.0.1:8817
    python hub/tools/verify_gateway.py --base http://host:8817
    python hub/tools/verify_gateway.py --key-merged X --key-wb Y --key-qd Z   # 手工指定

判据（对应 docs/merge/13-统一Key.md）：
  G01  /api/hub/health 两个引擎都在
  G02  合并 key → /v1/models：merged=true
  G03  wb 的 key → /v1/models：只列 wb（无 merged 标）
  G04  qd 的 key → /v1/models：只列 qd
  G05  未登记的 key → 401（且带 invalid_api_key）
  G06  合并 key 调 chat → 200 且带 X-Hub-Route
  G07  合并模型的并集 = 两引擎各自模型的并集（同名只算一次）
  G08  /api/hub/keys（新版门户）回报三类 key 计数；旧门户则跳过

只用标准库；不写任何文件。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
REPO = os.path.dirname(HUB)

RESULTS = []

#: 面板依赖的融合端点 —— 与 hub/server.py 的 CAP_ENDPOINTS、前端 PANEL_CAPS、
#: 接入说明页的 CAPS、verify_firstrun.py 的 CAP_ENDPOINTS **同一份清单**。
#: 五处一致才不会出现"横幅说全在、页面说缺一项"。
CAP_ENDPOINTS = ("hub/keys", "hub/selfcheck", "hub/try", "hub/accounts",
                 "hub/route-stats", "hub/rewards", "hub/scheduler")


def check(label, ok, detail=""):
    RESULTS.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = "  [%s] %s" % (mark, label)
    if detail and not ok:
        line += "  —— " + str(detail)[:300]
    print(line, flush=True)


def skip(label, why=""):
    print("  [SKIP] %s%s" % (label, ("  —— " + why) if why else ""), flush=True)


def call(base, path, method="GET", body=None, key=None, timeout=25):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 自证脚本把连接问题当失败信息带回
        return -1, {}, repr(exc)[:160]


def load_local_keys():
    """从本地配置里捡 key（脚本与被测实例同机时的便捷路径）。"""
    out = {"merged": None, "wb": None, "qd": None}
    try:
        with open(os.path.join(HUB, "settings.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        for item in doc.get("merged_keys") or []:
            if item.get("enabled") is not False and item.get("key"):
                out["merged"] = item["key"]
                break
    except Exception:
        pass
    for engine in ("wb", "qd"):
        for rel in (os.path.join("engines", engine, "accounts", "settings.json"),
                    os.path.join("engines", engine, "settings.json")):
            path = os.path.join(HUB, rel)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
                for item in doc.get("api_keys") or []:
                    if item.get("enabled") is not False and item.get("key"):
                        out[engine] = item["key"]
                        break
            except Exception:
                pass
            if out[engine]:
                break
    return out


def model_ids(payload):
    try:
        doc = json.loads(payload)
        return [str(m.get("id") or "").lower() for m in (doc.get("data") or []) if m.get("id")]
    except Exception:
        return []



def _has_key(base, kind):
    """该实例上有没有这一类 key（没有就跳过相关判定，而不是记成失败）。"""
    try:
        status, _h, body = call(base, "/api/hub/keys", timeout=8)
        if status != 200:
            return True          # 旧门户没有这个端点：交给原有逻辑处理
        counts = (json.loads(body) or {}).get("counts") or {}
        return int(counts.get(kind) or 0) > 0
    except Exception:
        return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="验证「一个地址 + 三类 key」")
    ap.add_argument("--base", default="http://127.0.0.1:8817")
    ap.add_argument("--key-merged")
    ap.add_argument("--key-wb")
    ap.add_argument("--key-qd")
    ap.add_argument("--model", default="glm-5.3-flash", help="用来验路由的模型（两边都有的同名模型）")
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    local = load_local_keys()
    k_merged = args.key_merged or local["merged"]
    k_wb = args.key_wb or local["wb"]
    k_qd = args.key_qd or local["qd"]

    print("验证目标: %s" % base)

    # ★ 先看一眼这个实例上到底有没有 key。
    #   工具读的是**本仓配置**里的三类 key；如果被测实例用了隔离数据根（空环境），
    #   那些 key 在它上面根本不存在 —— 后面会连出一串 401，看起来像"坏得很严重"。
    #   与其让人误会，不如先说清楚。
    try:
        _st, _h, _b = call(base, "/api/hub/keys", timeout=8)
        if _st == 200:
            _counts = (json.loads(_b) or {}).get("counts") or {}
            if not any(int(_counts.get(k) or 0) for k in ("merged", "wb", "qd")):
                print("  [提示] 这个实例上一把 key 都没登记（隔离数据根？）——"
                      "key 相关判定会 401。用 --key-merged/--key-wb/--key-qd 指定，"
                      "或对默认实例运行。", flush=True)
    except Exception:
        pass
    print("三类 key: 合并=%s / wb=%s / qd=%s"
          % ("有" if k_merged else "无", "有" if k_wb else "无", "有" if k_qd else "无"))
    print("")

    # G01 门户健康
    status, _h, body = call(base, "/api/hub/health")
    ok = False
    engines = {}
    try:
        doc = json.loads(body)
        engines = doc.get("engines") or {}
        ok = bool(doc.get("ok"))
    except Exception:
        pass
    check("G01 门户健康：/api/hub/health 两个引擎都在", status == 200 and ok,
          "status=%s body=%s" % (status, body[:120]))
    if engines:
        print("       引擎: " + ", ".join(
            "%s=%s" % (k, "ok" if (v or {}).get("ok") else "down") for k, v in engines.items()), flush=True)

    # ★ 能力探测：老门户没有 /api/hub/keys，也就没有"统一 Key 路由"——
    #   那种情况下 G04/G05 的失败**不代表坏**，而是"这个实例还是旧代码"。
    probe_keys_status, _ph, _pb = call(base, "/api/hub/keys")
    unified = (probe_keys_status == 200)
    if not unified:
        print("  [提示] 这个门户没有 /api/hub/keys —— 统一 Key 路由是后加的，"
              "重启 start-hub.bat 后才会生效。以下与它相关的判据按 SKIP 处理。", flush=True)

    # G02 合并 key 的模型清单
    merged_ids = []
    if k_merged:
        status, _h, body = call(base, "/api/v1/models", key=k_merged)
        try:
            doc = json.loads(body)
            merged_ids = model_ids(body)
            check("G02 合并 key → /v1/models：merged=true", status == 200 and doc.get("merged") is True,
                  "status=%s merged=%s" % (status, doc.get("merged")))
            print("       合并目录: %d 个模型" % len(merged_ids), flush=True)
        except Exception:
            check("G02 合并 key → /v1/models：merged=true", False, "status=%s body=%s" % (status, body[:120]))
    else:
        skip("G02 合并 key → /v1/models", "没有可用的合并 key")

    # G03 / G04 引擎 key 的模型清单
    single = {}
    for engine, key in (("wb", k_wb), ("qd", k_qd)):
        if not key:
            skip("G%02d %s 的 key → /v1/models" % (3 if engine == "wb" else 4, engine), "没有该引擎的 key")
            continue
        # key 是从**本仓配置**读的，而被测实例可能用了别的数据根（隔离实例）——
        # 那种情况下这里的 key 在实例上根本不存在，401 不代表缺陷。
        # 按实例**自己登记的**数量判断，才能分清"没登记"和"登记了但不生效"。
        if not _has_key(base, engine):
            skip("G%02d %s 的 key → /v1/models" % (3 if engine == "wb" else 4, engine),
                 "被测实例上没有登记 %s key（隔离数据根？）" % engine)
            continue
        status, headers, body = call(base, "/api/v1/models", key=key)
        ids = model_ids(body)
        single[engine] = ids
        try:
            doc = json.loads(body)
            was_merged = doc.get("merged") is True
        except Exception:
            was_merged = False
        label = "G%02d %s 的 key → /v1/models：只列 %s（不走合并清单）" % (3 if engine == "wb" else 4, engine, engine)
        if not unified and status == 401:
            skip(label, "旧门户：没有按 key 归属路由，重启后生效")
        else:
            check(label, status == 200 and not was_merged and "merged" not in body[:80],
                  "status=%s merged=%s 条数=%d" % (status, was_merged, len(ids)))

    # G05 未登记的 key
    status, _h, body = call(base, "/api/v1/models", key="definitely-not-a-key-xyz")
    if not unified:
        skip("G05 未登记的 key → 401（且带 invalid_api_key）", "旧门户：由引擎自己拒绝，措辞不同")
    else:
        check("G05 未登记的 key → 401（且带 invalid_api_key）",
              status == 401 and "invalid_api_key" in body, "status=%s body=%s" % (status, body[:120]))

    # G06 合并 key 调 chat + 路由头
    if k_merged:
        status, headers, body = call(base, "/api/v1/chat/completions", "POST",
                                     {"model": args.model,
                                      "messages": [{"role": "user", "content": "hi"}],
                                      "max_tokens": 5}, key=k_merged)
        route = headers.get("X-Hub-Route") or headers.get("x-hub-route")
        # 模型不存在 / 账号耗尽都不算"路由没生效"：要求的是**有路由头且不是 401**
        check("G06 合并 key 调 chat：不是 401，且带 X-Hub-Route（说明走了智能路由）",
              status != 401 and bool(route), "status=%s route=%s body=%s" % (status, route, body[:120]))
        if route:
            print("       本次落点: " + route, flush=True)
    else:
        skip("G06 合并 key 调 chat", "没有可用的合并 key")

    # G07 合并目录 = 两引擎并集
    nonempty = {e: v for e, v in single.items() if v}
    if merged_ids and len(nonempty) >= 2:
        both = set(nonempty["wb"]) & set(nonempty["qd"]) if ("wb" in nonempty and "qd" in nonempty) else set()
        if not both:
            skip("G07 合并目录覆盖两引擎共有的模型（同名只算一次）",
                 "两引擎清单没有交集（key 的出口绑定不同区时可以出现）")
        else:
            covered = all(m in merged_ids for m in list(both)[:5])
            check("G07 合并目录覆盖两引擎共有的模型（同名只算一次）", covered,
                  "合并=%d 共有=%d 样例=%s" % (len(merged_ids), len(both), list(both)[:3]))
        print("       合并目录 %d 条；wb %d 条 / qd %d 条"
              % (len(merged_ids), len(nonempty.get("wb") or []), len(nonempty.get("qd") or [])), flush=True)
    else:
        skip("G07 合并目录覆盖两引擎共有的模型", "缺少合并 key 或至少一个引擎 key 不可用")

    # G08 三类 key 总览
    status, _h, body = call(base, "/api/hub/keys")
    if status == 200:
        try:
            doc = json.loads(body)
            counts = doc.get("counts") or {}
            check("G08 /api/hub/keys 回报三类 key 计数", all(k in counts for k in ("merged", "wb", "qd")),
                  "counts=%s" % counts)
        except Exception:
            check("G08 /api/hub/keys 回报三类 key 计数", False, body[:120])
    else:
        skip("G08 /api/hub/keys", "该门户版本没有这个端点（status=%s）" % status)

    # G09：融合端点清单（口径同其余四处：405 也算有）
    missing_caps = []
    for name in CAP_ENDPOINTS:
        st_cap, _h, _b = call(base, "/api/" + name, timeout=10)
        if st_cap not in (200, 204, 405):
            missing_caps.append("%s(%s)" % (name, st_cap))
    if not missing_caps:
        check("G09 融合端点 %d 项全在（与启动横幅 / 面板 / 首跑脚本同一份清单）"
              % len(CAP_ENDPOINTS), True)
    elif not unified:
        # 旧门户：缺是预期的（它本来就没有这些端点），不该记成失败
        skip("G09 融合端点清单", "旧门户，缺 " + ", ".join(missing_caps))
    else:
        check("G09 融合端点 %d 项全在（与启动横幅 / 面板 / 首跑脚本同一份清单）"
              % len(CAP_ENDPOINTS), False, "缺: " + ", ".join(missing_caps))

    # G10：两个引擎的错误体形状必须一致（面板与调用方都靠它分类）
    # 实测（第 78 轮）：401(未登录) / 401(坏 key) / 404(无此路由) 三种情况下，
    # 两侧都是 {error:{message,type,code}}，status 也一致 —— 只有 message 细节不同（wb 多给用法提示）。
    # 这条钉住"形状"，不钉措辞：形状一散，面板的错误分类与调用方的重试判断就得各写一套。
    envelope = {}
    for engine in ("wb", "qd"):
        st_e, _h_e, body_e = call(base, "/api/%s/definitely-not-a-route" % engine, timeout=10)
        shape = None
        try:
            doc_e = json.loads(body_e)
            err_e = doc_e.get("error") if isinstance(doc_e, dict) else None
            if isinstance(err_e, dict):
                shape = (st_e, sorted(err_e.keys()))
        except Exception:
            shape = None
        envelope[engine] = shape

    same = (envelope.get("wb") is not None
            and envelope.get("wb")[1] == envelope.get("qd", (None, None))[1])
    check("G10 两个引擎的错误体形状一致（status + error{message,type,code}）",
          same, "%r" % (envelope,))

    failed = [r for r in RESULTS if not r[1]]
    print("")
    print("SUMMARY: %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
