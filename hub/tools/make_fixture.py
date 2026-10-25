# -*- coding: utf-8 -*-
"""hub/tools/make_fixture.py —— 造一份**能触发边界显示**的临时数据根（第 117 轮）。

为什么需要它：像"账号最近出错"这种标记，只有在**真的有错**时才显示。
真实数据里 32 个账号全都健康 —— 于是这段 UI 永远没人见过它长什么样，
只能靠读代码判断"应该没问题"。这个工具把那种情况造出来：

    python hub/tools/make_fixture.py --out .fixture-data --scenario all

它会：
  1) 从 hub/engines/<e>/{accounts,usage} 复制一份真实数据（保证 schema 合法）；
  2) 按场景**打补丁**（给一个账号写 lastError / last_error）；
  3) 追加一行"带账号的失败" usage（让按账号的计数不为 0）；
  4) 打印一条可直接粘贴的启动命令。

设计原则：**只做加法**，不伪造整个 schema —— 复制真实数据再改一处，
比手写一份假数据更不容易"造出一个引擎根本不认的形状"。
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))     # hub/tools
HUB = os.path.dirname(HERE)                           # hub/
ENGINES_DIR = os.path.join(HUB, "engines")
ENGINES = ("wb", "qd", "trae")

#: 各引擎账号文件里"上次错误"的字段名（实测差异，见 docs/merge/18-数据契约.md）
ERR_FIELD = {"wb": ("lastError", "lastErrorDetail"), "qd": ("lastError",), "trae": ("last_error", "last_error_at")}

#: 这些字段是**运行时字段**：引擎把它们排除在持久化之外（wb_accounts.py 的
#: RUNTIME_ONLY_FIELDS），所以**往磁盘里写不生效** —— 只有进程内真的失败过才会有。
#: 本工具对这类引擎只能造出"请求数"那一半，lastError 那半得靠真实失败。
RUNTIME_ONLY = {"wb": {"lastError", "lastErrorDetail", "cooldownUntil"}}


def log(msg):
    print(msg, flush=True)


def copy_engine(engine, out_root):
    src = os.path.join(ENGINES_DIR, engine)
    dst = os.path.join(out_root, engine)
    n = 0
    for sub in ("accounts", "usage"):
        s = os.path.join(src, sub)
        if not os.path.isdir(s):
            continue
        d = os.path.join(dst, sub)
        os.makedirs(d, exist_ok=True)
        for name in os.listdir(s):
            if name.endswith((".json", ".jsonl")):
                shutil.copy2(os.path.join(s, name), os.path.join(d, name))
                n += 1
    return n


def account_files(engine, out_root):
    d = os.path.join(out_root, engine, "accounts")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, n) for n in sorted(os.listdir(d))
            if n.endswith(".json") and n not in ("settings.json", "active_realm.json")]


def pick_visible_account(engine, out_root):
    """挑一个**界面上能看见状态**的账号：优先 enabled != false。

    踩过（第 124 轮）：夹具默认挑第一个账号，而 qd 的第一个恰好是"本地停用"——
    healthOf 在冷却/错误之前就先返回 disabled，于是补丁写进去了、界面上什么都没变。
    """
    files = account_files(engine, out_root)
    if not files:
        return None
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if json.load(fh).get("enabled") is not False:
                    return path
        except Exception:
            continue
    return files[0]


def patch_account_error(engine, out_root):
    """给第一个账号写上"上次错误" —— 两种命名风格都写，覆盖所有引擎的读法。"""
    path = pick_visible_account(engine, out_root)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception:
        return None
    y, m, d = time.localtime()[:3]
    obj["lastError"] = "fixture: upstream returned 502 (synthetic)"
    obj["lastErrorDetail"] = "fixture: upstream returned 502 (synthetic)"
    obj["last_error"] = "fixture: upstream returned 502 (synthetic)"
    obj["last_error_at"] = "%04d-%02d-%02dT00:00:00" % (y, m, d)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    return os.path.basename(path)


def patch_account_cooldown(engine, out_root, seconds=900):
    """给第一个账号写一个**未来**的冷却时间戳（第 124 轮）。

    为什么单独一个场景：冷却判定在 122 轮才改成"从时间戳算"，
    而错误/失败两个场景都不产生冷却 —— 于是"冷却中"那条 UI 从来没被真跑过。
    """
    path = pick_visible_account(engine, out_root)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception:
        return None
    until = time.time() + seconds
    # 三家写法都写一遍：wb/qd 驼峰、trae 蛇形
    obj["cooldownUntil"] = until
    obj["cooldown_until"] = until
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    return os.path.basename(path)

def append_failure_rows(engine, out_root, count=3):
    """追加带 account 的失败行 —— 让"按账号的请求数/失败"不为 0。"""
    path = pick_visible_account(engine, out_root)
    usage_dir = os.path.join(out_root, engine, "usage")
    os.makedirs(usage_dir, exist_ok=True)
    usage = os.path.join(usage_dir, "usage.jsonl")
    if not path:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            uid = json.load(fh).get("uid") or ""
    except Exception:
        uid = ""
    now = int(time.time())
    n = 0
    with open(usage, "a", encoding="utf-8") as fh:
        for i in range(count):
            row = {
                "at": now + i,
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now + i)),
                "model": "fixture-model",
                "account": uid,
                "status": 503,
                "message": "fixture: upstream exploded (synthetic)",
                "error": True,
                "elapsed_ms": 250 + i,
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="造一份能触发边界显示的临时数据根")
    ap.add_argument("--out", default=".fixture-data", help="输出目录（会被清空后重建）")
    ap.add_argument("--scenario", default="all",
                    choices=("all", "account-error", "usage-fail", "cooldown"),
                    help="account-error=写上次错误；usage-fail=追加失败行；cooldown=写未来冷却时间戳；all=前两者")
    ap.add_argument("--engine", action="append", choices=ENGINES, help="只处理某几家（默认三家）")
    ap.add_argument("--keep", action="store_true", help="输出目录已存在时不删除")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    out = os.path.abspath(args.out)
    if os.path.isdir(out) and not args.keep:
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)

    picked = args.engine or list(ENGINES)
    log("夹具输出: %s" % out)
    for engine in picked:
        copied = copy_engine(engine, out)
        if not copied:
            log("  [%s] 没有可复制的数据（hub/engines/%s）" % (engine, engine))
            continue
        notes = []
        if args.scenario in ("all", "account-error"):
            who = patch_account_error(engine, out)
            if who:
                if engine in RUNTIME_ONLY:
                    notes.append("账号 %s 已写 lastError（⚠ 该引擎把它当运行时字段，磁盘写入不生效）" % who)
                else:
                    notes.append("账号 %s 已写入 lastError" % who)
        if args.scenario in ("all", "usage-fail"):
            n = append_failure_rows(engine, out)
            if n:
                notes.append("追加 %d 条带账号的失败行" % n)
        if args.scenario == "cooldown":
            who = patch_account_cooldown(engine, out)
            if who:
                notes.append("账号 %s 写入未来冷却（%s）" % (who, "运行时字段对 wb 无效" if engine in RUNTIME_ONLY else "可持久化"))
        log("  [%s] 复制 %d 个文件；%s" % (engine, copied, "；".join(notes) or "未打补丁"))

    log("")
    log("启动：")
    log("  HUB_DATA_ROOT=\"%s\" python -u -m hub.server --wb-port 9781 --qd-port 9782 --trae-port 9783 --portal-port 9787" % out)
    log("")
    log("看什么：")
    log("  · 账号池 → 第一个账号应有「最近出错」徽标（悬停显示合成错误原文）")
    log("    注意：wb 的 lastError 是运行时字段（RUNTIME_ONLY_FIELDS），磁盘注入无效。")
    log("    cooldown 场景同样不可靠：wb 是运行时字段，qd 会在启动时把 cooldownUntil 重置为 0")
    log("    （实测 2026-10-19：补丁写进文件，引擎起来后又变回 0）。要看冷却态，")
    log("    只能靠 _test_ui_states.js 的 U6（直接喂数据给 render）。")
    log("  · 账号池 → 昵称第二行应显示「最近 N 次」")
    log("  · 数据指标 → 失败行会让成功率低于 100%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
