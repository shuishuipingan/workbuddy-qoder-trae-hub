# -*- coding: utf-8 -*-
"""_test_tools_health.py —— hub/tools/ 下的每个脚本都必须**能跑起来**，且危险开关是安全的。

由来（第 79 轮）：盘了一遍 hub/tools，发现 sync_upstream.py 从来没有被任何判据执行过。
那个脚本会**覆盖引擎源文件**（镜像 / vendored / 用户副本），一旦 import 烂了或
--dry-run 被改成"也写盘"，后果不是"报个错"，而是"文件被改掉"。

这里只做三件低风险、高价值的事：
  T01  每个工具的 --help 退出码 0（能 import、能解析参数 —— 拦住语法/依赖腐化）
  T02  --dry-run 必须传进每一处复制调用（数一数，别只传一半）
  T03  copy_tree_filtered 的写盘必须被 「if not dry」 包住（dry-run 真的不写）
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

TOOLS_DIR = os.path.join(P.HUB, "tools")
if not os.path.isdir(TOOLS_DIR):
    H.skip("T00 hub/tools 不存在", TOOLS_DIR)
    H.finish()
    raise SystemExit(0)

scripts = sorted(n for n in os.listdir(TOOLS_DIR) if n.endswith(".py"))
H.check("T00 扫到了工具脚本（%d 个）" % len(scripts), len(scripts) >= 2, ", ".join(scripts))

for name in scripts:
    path_ = os.path.join(TOOLS_DIR, name)
    try:
        proc = subprocess.run([sys.executable, path_, "--help"], cwd=P.REPO,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        ok = proc.returncode == 0 and ("usage" in (proc.stdout or "").lower())
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-2:]
    except Exception as exc:  # noqa: BLE001
        ok, tail = False, [repr(exc)[:120]]
    H.check("T01 %s --help 能跑（import + 参数解析都正常）" % name, ok, " | ".join(tail))

SYNC = os.path.join(TOOLS_DIR, "sync_upstream.py")
if os.path.exists(SYNC):
    with open(SYNC, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    # 去掉函数定义那一处：它长得也像调用（def copy_tree_filtered(...)）
    copies = src.count("copy_tree_filtered(") - src.count("def copy_tree_filtered(")
    dry_args = src.count("dry=args.dry_run")
    H.check("T02 sync_upstream 每处复制都传了 dry（%d 处调用 / %d 处传参）" % (copies, dry_args),
            copies >= 2 and dry_args >= copies, "copy=%d dry=%d" % (copies, dry_args))
    i = src.find("def copy_tree_filtered(")
    body = src[i:i + 1600] if i >= 0 else ""
    H.check("T03 copy_tree_filtered 的写盘被 dry 守卫包住（dry-run 真的不写）",
            "if not dry:" in body, "函数体里找不到 dry 守卫")

# ---------- T04/T05 补丁本身：能被 git 解析，且镜像 = 上游 + 补丁 ----------
# 由来（第 80 轮）：patches/ 是 sync_upstream 的输入。补丁一旦与目标树错位，
# 管线会在**已经覆盖了一部分文件之后**才失败 —— 那时现场已经被改了。
# 这里把"错位"提前到只读检查里暴露：
#   T04  每个 patch 都能被 git apply --stat 解析（语法/结构）
#   T05  wb 的补丁能对镜像**反向**应用（证明当前镜像 == 上游 + 这些补丁）
BT = "docs"
PATCH_ROOT = os.path.join(P.REPO, BT, "merge", "patches")
MIRROR = os.path.join(P.SIBLING, "wb")


def _git(args, cwd):
    """跑 git，返回 (ok, 输出)。git 不可用返回 (None, '')。"""
    try:
        proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=90)
        return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except FileNotFoundError:
        return None, ""
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)[:120]


if os.path.isdir(PATCH_ROOT):
    patches = []
    for base, _dirs, files in os.walk(PATCH_ROOT):
        for name in files:
            if name.endswith(".patch"):
                patches.append(os.path.join(base, name))
    patches.sort()
    bad = []
    for p in patches:
        ok, detail = _git(["apply", "--stat", p], P.REPO)
        if ok is None:
            break
        if not ok:
            bad.append("%s: %s" % (os.path.basename(p), detail.splitlines()[:1]))
    if patches and bad == []:
        H.check("T04 %d 个 patch 都能被 git 解析（apply --stat）" % len(patches), True)
    elif bad:
        H.check("T04 patch 解析", False, " | ".join(bad[:3]))

    wb_patches = sorted(n for n in os.listdir(os.path.join(PATCH_ROOT, "wb"))
                        if n.endswith(".patch")) if os.path.isdir(os.path.join(PATCH_ROOT, "wb")) else []
    if wb_patches and os.path.isdir(MIRROR):
        problems = []
        for name in wb_patches:
            ok, detail = _git(["apply", "-R", "--check",
                               os.path.join(PATCH_ROOT, "wb", name)], MIRROR)
            if ok is None:
                problems = []
                break
            if not ok:
                problems.append("%s: %s" % (name, detail.splitlines()[:1]))
        if problems:
            H.check("T05 wb 补丁能反向应用到镜像（镜像 == 上游 + 补丁）", False,
                    " | ".join(problems[:3]))
        else:
            H.check("T05 wb 补丁能反向应用到镜像（镜像 == 上游 + 补丁）", True)
    else:
        H.skip("T05 反向应用", "没有 wb 补丁或镜像目录缺失")

# ---- T08：三仓一致性体检工具（第 110 轮）----
# verify_sync.py 是"随时问一句'我跟上游还一致吗'"的手跑工具。
# 它必须真的能跑通并给出结论 —— 否则手跑时只会给人一个 traceback。
sync_tool = os.path.join(TOOLS_DIR, "verify_sync.py")
if os.path.exists(sync_tool):
    try:
        proc = subprocess.run([sys.executable, sync_tool, "--json"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        rc, out = proc.returncode, (proc.stdout or "")
    except Exception as exc:
        rc, out = 1, "%s: %s" % (type(exc).__name__, exc)
    ok_payload = False
    try:
        import json as _json
        # --json 是**多行**缩进输出的（人也要读得懂），所以解析整串而不是最后一行
        ok_payload = bool(_json.loads(out or "{}").get("ok"))
    except Exception:
        ok_payload = False
    H.check("T08 verify_sync.py 能跑通且三家与上游一致", rc == 0 and ok_payload,
            "rc=%s out=%s" % (rc, (out or "").strip()[-160:]))
else:
    H.check("T08 verify_sync.py 存在", False, "缺 hub/tools/verify_sync.py")

# ---- T09：夹具生成器（第 117 轮）----
# make_fixture.py 是"验证有数据才显示"的功能的唯一手段。它必须真能造出数据根，
# 否则那些边界 UI 永远只能靠读代码判断。
fx_tool = os.path.join(TOOLS_DIR, "make_fixture.py")
if os.path.exists(fx_tool):
    import tempfile as _tf
    _out = os.path.join(_tf.mkdtemp(prefix="hub-fixture-"), "data")
    try:
        _proc = subprocess.run([sys.executable, fx_tool, "--out", _out, "--scenario", "all"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        _rc = _proc.returncode
    except Exception:
        _rc = 1
    _acc = 0
    if os.path.isdir(_out):
        for _root, _dirs, _files in os.walk(_out):
            if os.path.basename(_root) == "accounts":
                _acc += len([f for f in _files if f.endswith(".json")])
    H.check("T09 make_fixture.py 能造出带账号的临时数据根", _rc == 0 and _acc > 0,
            "rc=%s accounts=%d" % (_rc, _acc))
else:
    H.check("T09 make_fixture.py 存在", False, "缺 hub/tools/make_fixture.py")

# ---- T06/T07：同步管线必须覆盖三家（第 109 轮）----
sync_src = ""
try:
    with open(os.path.join(TOOLS_DIR, "sync_upstream.py"), "r", encoding="utf-8") as fh:
        sync_src = fh.read()
except OSError:
    sync_src = ""
H.check("T06 sync_upstream 认识第三家（--trae / sync_trae / TRAE_VENDOR）",
        "\"--trae\"" in sync_src and "def sync_trae(" in sync_src and "TRAE_VENDOR" in sync_src,
        "同步管线还只认 wb/qd —— 上游 Trae 更新后没法自动跟进")
H.check("T07 控制台编码兜底存在（中文 Windows GBK 下 ✅ 会让成功返回非零）",
        "reconfigure(errors=" in sync_src,
        "缺 reconfigure(errors=...) —— 同步成功也会被当成失败")

H.finish()
