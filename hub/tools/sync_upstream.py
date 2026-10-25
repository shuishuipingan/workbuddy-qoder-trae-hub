#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/tools/sync_upstream.py —— 上游同步管线（把手工流程收成一条命令）。

## 为什么要它

今天（2026-10-14）wb 上游更新了 3 轮：v1.6.17→v1.7.4（235 个提交）、b843ff3（catalog 修复）。
每次同步都要重复同一串动作：拉上游 → 三方合并（上游改动 vs 本地补丁）→ 分发到 4 个副本
→ 保留用户副本的端口定制 → 逐字节校验 → 跑测试。全套手工做一遍要几十步，
**而其中"覆盖错方向"这一类错误真的发生过**（把旧版 wbhub-pr 的文件盖到了更新的镜像上）。
这个工具就是那次事故之后写的：流程固化 + 每步自证。

## 它做什么（wb）

  1. 上游：`git fetch` + `git merge --ff-only`（拒绝脏工作区，除非 --force）
  2. 快照：临时 worktree 检出 origin/main（用 worktree 而不是 tar：tar 对
     中文文件名报 "Invalid empty pathname"，实测过）
  3. 打补丁：`docs/merge/patches/wb/*.patch` 依序应用到快照
  4. 分发：
       · 镜像      合并damo/wb                  ← 全部 tracked 文件
       · vendored  hub/engines/wb              ← wb_*.py + dashboard.html + pricing/pricing.json
       · 用户副本  Desktop/wb Desktop/wb-proxy ← 仅 --with-user-dirs 时才碰（那是用户
                                                  的部署）：全部 tracked（保留 pricing.json），
                                                  再叠 `patches/user-local/*.patch`（端口 8789 等）
  5. 校验：镜像 vs vendored 逐字节；--with-user-dirs 时加验用户副本 = 镜像 + 补丁
  6. （可选 --test）跑 hub 静态判据 + wb 的补丁相关测试

## 用法

    python hub/tools/sync_upstream.py --wb                  # 同步 wb（镜像 + vendored）
    python hub/tools/sync_upstream.py --wb --with-user-dirs # 连用户副本一起（默认不碰）
    python hub/tools/sync_upstream.py --wb --check          # 只校验现状（不写盘）
    python hub/tools/sync_upstream.py --wb --dry-run        # 只打印计划
    python hub/tools/sync_upstream.py --wb --test           # 同步后跑测试
    python hub/tools/sync_upstream.py --qd                  # 同步 qd 引擎文件
    python hub/tools/sync_upstream.py --wb --qd --test

## 路径（环境变量可覆盖）

    WB_SYNC_REPO     wb 上游 git 仓           默认 <home>/wbhub-pr
    WB_SYNC_USER_DIRS wb 用户副本（; 分隔）   默认 Desktop/wb;Desktop/wb-proxy
    QD_SYNC_REPO     qd 上游 git 仓           默认 Desktop/qoder-proxy
    SYNC_TEMP        临时目录根               默认 %TEMP%/hub-sync

只依赖标准库 + git。
"""
import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))            # hub/tools
HUB = os.path.dirname(HERE)                                  # hub/
REPO = os.path.dirname(HUB)                                  # qoder-proxy/
SIBLING = os.path.dirname(REPO)                              # 合并damo/
DESKTOP = os.path.dirname(SIBLING)                           # Desktop/
PATCH_DIR = os.path.join(REPO, "docs", "merge", "patches")

WB_MIRROR = os.path.join(SIBLING, "wb")
WB_VENDOR = os.path.join(HUB, "engines", "wb")
QD_VENDOR = os.path.join(HUB, "engines", "qd")
# Trae（第 109 轮）：第三家。它的文件在仓根（不在 scripts/），vendored 目录另有一份。
TRAE_VENDOR = os.path.join(HUB, "engines", "trae")
#: Trae 的分发集：引擎代码 + 目录快照 + 自带看板 + 定价表（与 hub/tests 的判据一致）
TRAE_SYNC_GLOBS = ("trae_", "dashboard", "pricing/trae_pricing.json")

#: 用户副本里**保留**的运行时数据文件（同步时不覆盖）
KEEP_IN_USER_DIRS = {"pricing/pricing.json"}
#: 分发到 vendored 引擎目录的文件（与 hub/tests/_lib/paths.py 的判据一致）
VENDOR_EXTRA = {"dashboard.html", "pricing/pricing.json"}


def env_path(name, default):
    return os.environ.get(name) or default


def wb_repo():
    """wb 上游 git 仓。默认按候选顺序找：~/wbhub-pr（本机实证）→ Desktop/wbhub-pr。"""
    override = os.environ.get("WB_SYNC_REPO")
    if override:
        return override
    # 主目录从工作区路径推导（Desktop/ 的上一级），不依赖 HOME/USERPROFILE ——
    # 实测：在「空环境」里跑（sandbox / 计划任务）expanduser("~") 会返回字面 "~"。
    home = os.path.dirname(DESKTOP)
    for cand in (os.path.join(home, "wbhub-pr"),
                 os.path.join(DESKTOP, "wbhub-pr"),
                 os.path.join(os.path.expanduser("~"), "wbhub-pr")):
        if os.path.isdir(os.path.join(cand, ".git")):
            return cand
    return os.path.join(home, "wbhub-pr")


def qd_repo():
    return env_path("QD_SYNC_REPO", os.path.join(DESKTOP, "qoder-proxy"))


def trae_repo():
    """Trae 上游仓（本机实证：合并damo/trae2api-hub）。用 TRAE_SYNC_REPO 覆盖。"""
    return env_path("TRAE_SYNC_REPO", os.path.join(SIBLING, "trae2api-hub"))


def user_dirs():
    raw = env_path("WB_SYNC_USER_DIRS",
                   ";".join([os.path.join(DESKTOP, "wb"), os.path.join(DESKTOP, "wb-proxy")]))
    return [p for p in (s.strip() for s in raw.split(";")) if p]


def temp_root():
    root = env_path("SYNC_TEMP", os.path.join(tempfile.gettempdir(), "hub-sync"))
    os.makedirs(root, exist_ok=True)
    return root


# --------------------------------------------------------------------------- 基础
class Step(object):
    """一步的结果：写盘动作与校验都记账，最后统一汇报。"""

    def __init__(self, log):
        self.log = log
        self.ok = True
        self.notes = []

    def note(self, text):
        self.notes.append(text)
        self.log("  " + text)

    def fail(self, text):
        self.ok = False
        self.notes.append("FAIL: " + text)
        self.log("  [FAIL] " + text)


def run(cmd, cwd=None, check=True, capture=True):
    """跑一条子进程命令；失败时把 tail 打出来（git 的错误信息足够定位）。"""
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=600)
    out = proc.stdout.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise RuntimeError("%s 失败 (rc=%d):\n%s" % (" ".join(cmd[:3]), proc.returncode, out[-1500:]))
    return proc.returncode, out


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def rel_files(root):
    """root 下全部文件的相对路径（正斜杠），跳过 .git/__pycache__。"""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            full = os.path.join(base, f)
            out.append(os.path.relpath(full, root).replace("\\", "/"))
    return sorted(out)


def git_tracked_files(repo, ref="origin/main"):
    """git 里该 ref 的 tracked 文件（-z：不做引号转义，中文名安全）。"""
    rc, out = run(["git", "-C", repo, "ls-tree", "-rz", "--name-only", ref])
    return [p for p in out.split("\0") if p]


def repo_is_dirty(repo):
    rc, out = run(["git", "-C", repo, "status", "--porcelain"])
    return bool(out.strip())


def apply_patches(snapshot, patch_dir, force=False):
    """按文件名序把 patch_dir 下的 *.patch 应用到快照（快照必须是 git 仓）。"""
    if not os.path.isdir(patch_dir):
        return []
    names = sorted(n for n in os.listdir(patch_dir) if n.endswith(".patch"))
    applied = []
    for n in names:
        cmd = ["git", "-C", snapshot, "apply", "--whitespace=nowarn"]
        if force:
            cmd.append("--3way")
        cmd.append(os.path.join(patch_dir, n))
        rc, out = run(cmd, check=False)
        if rc != 0:
            raise RuntimeError("补丁 %s 应用失败（上游可能改了同一段代码，需手工合并）:\n%s"
                               % (n, out[-1200:]))
        applied.append(n)
    return applied


def copy_tree_filtered(src, dst, want=None, skip=None, dry=False):
    """把 src 里的文件按过滤器拷到 dst；返回 (拷贝数, 跳过清单)。"""
    want = want or (lambda rel: True)
    skip = skip or set()
    copied, skipped = 0, []
    for rel in rel_files(src):
        if rel in skip:
            skipped.append(rel)
            continue
        if not want(rel):
            continue
        s = os.path.join(src, rel)
        d = os.path.join(dst, rel.replace("/", os.sep))
        if not dry:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
        copied += 1
    return copied, skipped


def diff_report(a_root, b_root, label_a, label_b, only=None):
    """两份树的逐字节差异清单（只比 .py/.html/.json/.md，跳过运行时数据）。"""
    skip_prefix = ("accounts/", "usage/", "logs/", "data/", "monitor_logs/", "__pycache__/")
    exts = (".py", ".html", ".json", ".md", ".js", ".css", ".umid")
    files = [f for f in rel_files(a_root) if not f.startswith(skip_prefix)
             and f.lower().endswith(exts)]
    if only:
        files = [f for f in files if f in only]
    bad = []
    for rel in files:
        pa = os.path.join(a_root, rel.replace("/", os.sep))
        pb = os.path.join(b_root, rel.replace("/", os.sep))
        if not os.path.exists(pb):
            continue                      # 只在两边都有的文件上比（单边独有另行说明）
        if sha256(pa) != sha256(pb):
            bad.append(rel)
    return bad


# --------------------------------------------------------------------------- wb
# ---------------------------------------------------------------- 受控补丁：根路径 302
REDIRECT_MARK = "hub-patch: dashboard-redirect"
REDIRECT_BLOCK = [
    "# >>> hub-patch: dashboard-redirect（docs/merge/patches 里可复查；S04/S05 只允许这一段差异）",
    '# 融合模式：hub/server.py 会设 HUB_PORTAL_URL —— 根路径直接跳到控制台，',
    '# 避免"三个端口三张脸"。没设时保持原行为（单引擎独立跑 / 上游测试）。',
    '_portal = os.environ.get("HUB_PORTAL_URL", "").strip()',
    "if _portal:",
    "    self.send_response(302)",
    '    self.send_header("Location", _portal)',
    '    self.send_header("Content-Length", "0")',
    '    self.send_header("Cache-Control", "no-store")',
    "    self.end_headers()",
    "    return",
    "# <<< hub-patch: dashboard-redirect",
]


def ensure_portal_redirect(engine_dir, log):
    """同步会整文件覆盖引擎源码 —— 根路径 302 的受控补丁必须在这一步重贴。

    这个坑踩过两次（qd、wb 各一次），所以做成**幂等后置步骤**：
    已带标记就跳过；没有就插到 _dashboard() 的第一行。
    """
    if not os.path.isdir(engine_dir):
        return
    for name in sorted(os.listdir(engine_dir)):
        if not name.endswith("_proxy.py"):
            continue
        path_ = os.path.join(engine_dir, name)
        try:
            body = io.open(path_, encoding="utf-8").read()
        except OSError:
            continue
        if REDIRECT_MARK in body:
            log("  [redirect] %s 已带补丁" % name)
            continue
        lines = body.split("\n")
        idx = None
        for i, l in enumerate(lines):
            if re.match(r"^\s+def _dashboard\(", l):
                idx = i
                break
        if idx is None:
            log("  [redirect] %s 没有 _dashboard()，跳过" % name)
            continue
        indent = (re.match(r"^(\s*)", lines[idx]).group(1) or "") + "    "
        lines[idx + 1:idx + 1] = [indent + b for b in REDIRECT_BLOCK]
        io.open(path_, "w", encoding="utf-8").write("\n".join(lines))
        log("  [redirect] %s 已重贴 %d 行" % (name, len(REDIRECT_BLOCK)))

def sync_wb(args, log, step):
    repo = wb_repo()
    if not os.path.isdir(os.path.join(repo, ".git")):
        step.fail("wb 上游不是 git 仓: %s（用 WB_SYNC_REPO 覆盖）" % repo)
        return

    log("【1/6】拉上游")
    ref = "HEAD" if args.offline else "origin/main"
    if args.offline:
        log("  [offline] 跳过 fetch/merge，用本地 HEAD 快照")
    elif args.check:
        rc, out = run(["git", "-C", repo, "fetch", "origin"], check=False)
        log("  fetch: %s" % (out.splitlines()[-1] if out else "ok"))
    else:
        run(["git", "-C", repo, "fetch", "origin"])
        if repo_is_dirty(repo) and not args.force:
            step.fail("%s 工作区有未提交改动 —— 先 stash/提交，或用 --force 跳过检查" % repo)
            return
        rc, out = run(["git", "-C", repo, "merge", "--ff-only", "origin/main"], check=False)
        if rc != 0:
            step.fail("快进失败（本地 main 与 origin/main 分叉）：%s" % out[-300:])
            return
        log("  " + (out.splitlines()[-1] if out else "already up to date"))
    rc, head = run(["git", "-C", repo, "log", "--format=%h %s", "-1", ref])
    log("  %s = %s" % (ref, head))

    if args.check:
        log("【2/6】--check：跳过检出与写盘，只做现状校验")
        verify_all(args, log, step, mirror_head=head)
        return

    log("【2/6】检出快照（临时 worktree）")
    snap = os.path.join(temp_root(), "wb-snapshot")
    if os.path.isdir(snap):
        run(["git", "-C", repo, "worktree", "remove", "--force", snap], check=False)
        shutil.rmtree(snap, ignore_errors=True)
    run(["git", "-C", repo, "worktree", "add", "--detach", snap, "origin/main"])
    # 关掉 autocrlf：补丁与校验都要求 LF（Windows 上 git 默认会转 CRLF，实测会把校验搞红）
    run(["git", "-C", snap, "config", "core.autocrlf", "false"], check=False)
    run(["git", "-C", snap, "config", "core.eol", "lf"], check=False)

    try:
        log("【3/6】应用 wb 补丁")
        applied = apply_patches(snap, os.path.join(PATCH_DIR, "wb"))
        log("  应用: %s" % (", ".join(applied) if applied else "(无)"))

        tracked = set(git_tracked_files(repo, "origin/main"))
        log("【4/6】分发（tracked %d 个文件）" % len(tracked))

        # 4a 镜像：全部 tracked
        n, _ = copy_tree_filtered(snap, WB_MIRROR, want=lambda r: r in tracked, dry=args.dry_run)
        log("  镜像     合并damo/wb         <- %d 个文件" % n)

        # 4b vendored：wb_*.py + dashboard.html + pricing
        def vend_want(rel):
            return (rel.startswith("wb_") and rel.endswith(".py")) or rel in VENDOR_EXTRA
        n, _ = copy_tree_filtered(snap, WB_VENDOR, want=vend_want, dry=args.dry_run)
        log("  vendored hub/engines/wb     <- %d 个文件" % n)
        # 镜像与 vendored 都要贴：否则 verify 会报「镜像 vs vendored 不一致」
        ensure_portal_redirect(WB_VENDOR, log)
        ensure_portal_redirect(WB_MIRROR, log)

        if args.with_user_dirs:
            log("【5/6】用户副本（--with-user-dirs：保留运行时数据 + 本地补丁）")
            user_patches = os.path.join(PATCH_DIR, "user-local")
            if os.path.isdir(user_patches):
                applied_u = apply_patches(snap, user_patches)
                log("  叠加 user-local: %s" % (", ".join(applied_u) if applied_u else "(无)"))
            for d in user_dirs():
                if not os.path.isdir(d):
                    log("  跳过（不存在）: %s" % d)
                    continue
                n, skipped = copy_tree_filtered(snap, d, want=lambda r: r in tracked,
                                                skip=KEEP_IN_USER_DIRS, dry=args.dry_run)
                log("  %-28s <- %d 个文件（保留 %s）" % (d, n, ",".join(skipped) or "无"))
        else:
            log("【5/6】用户副本：跳过（默认不碰用户桌面；需要时加 --with-user-dirs）")
    finally:
        if not args.dry_run:
            run(["git", "-C", repo, "worktree", "remove", "--force", snap], check=False)
            shutil.rmtree(snap, ignore_errors=True)

    log("【6/6】校验")
    verify_all(args, log, step, mirror_head=head, user_patched=True)


def verify_all(args, log, step, mirror_head="", user_patched=False):
    """镜像 vs vendored（硬判据）；用户副本仅 --with-user-dirs 时校验。"""
    bad = diff_report(WB_MIRROR, WB_VENDOR, "mirror", "vendor",
                      only=set(f for f in rel_files(WB_VENDOR) if f.endswith((".py", ".html"))))
    if bad:
        step.fail("镜像 vs vendored 有 %d 个文件不一致: %s" % (len(bad), ", ".join(bad[:6])))
    else:
        log("  ✅ 镜像 = vendored（逐字节）")

    if getattr(args, "with_user_dirs", False):
        verify_user_copies(log, step)
    else:
        log("  （用户副本未校验 —— 未开 --with-user-dirs）")


def verify_user_copies(log, step):
    """用户副本 = 镜像 + user-local 补丁 —— 用「打上补丁的镜像」去比。

    ⚠️ 第一版这里直接 diff 镜像↔用户，**判据本身是错的**：补丁触及的那几个文件
    本该不同（端口 8788→8789）。修正后：把镜像里这些文件拷进临时 git 仓、打上
    同一份 user-local 补丁，再与用户副本逐字节比 —— 比的是"补丁是否如实落盘"。
    """
    patch_dir = os.path.join(PATCH_DIR, "user-local")
    patched = patched_files_in_patches(patch_dir)
    if not patched:
        return
    tmp = os.path.join(temp_root(), "verify-user")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    run(["git", "-C", tmp, "init", "-q"], check=False)
    run(["git", "-C", tmp, "config", "core.autocrlf", "false"], check=False)
    for rel in patched:
        src = os.path.join(WB_MIRROR, rel.replace("/", os.sep))
        if not os.path.exists(src):
            continue
        dst = os.path.join(tmp, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    try:
        apply_patches(tmp, patch_dir)
    except RuntimeError as exc:
        step.fail("校验用补丁应用失败：%s" % str(exc)[:200])
        return
    for d in user_dirs():
        if not os.path.isdir(d):
            continue
        bad = []
        for rel in patched:
            a = os.path.join(tmp, rel.replace("/", os.sep))
            b = os.path.join(d, rel.replace("/", os.sep))
            if not os.path.exists(a) or not os.path.exists(b) or sha256(a) != sha256(b):
                bad.append(rel)
        if bad:
            step.fail("用户副本 %s ≠ 镜像+补丁: %s" % (d, ", ".join(bad)))
        else:
            log("  ✅ %s = 镜像 + user-local 补丁" % os.path.basename(d))


def patched_files_in_patches(patch_dir):
    """补丁文件里 `+++ b/<path>` 提到的相对路径集合。"""
    out = set()
    if not os.path.isdir(patch_dir):
        return out
    for n in sorted(os.listdir(patch_dir)):
        if not n.endswith(".patch"):
            continue
        with open(os.path.join(patch_dir, n), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("+++ b/"):
                    out.add(line[6:].strip())
    return out


# --------------------------------------------------------------------------- qd
QD_SYNC_SET = ("qoder_",)          # 前缀
QD_FILES_EXTRA = ("baseprompt.json", "dashboard.html", "README.md")


def sync_qd(args, log, step):
    """qd 的上游仓**就是用户的运行目录**（Desktop/qoder-proxy）：
    这里只做「快进 + 把引擎文件同步到融合仓根与 vendored 目录」。
    融合仓自己的文件（Dockerfile/docker-compose/README-HUB/.gitignore/hub/admin/docs/merge）
    一律不碰 —— 它们不属于上游。"""
    repo = qd_repo()
    if not os.path.isdir(os.path.join(repo, ".git")):
        step.fail("qd 上游不是 git 仓: %s（用 QD_SYNC_REPO 覆盖）" % repo)
        return

    log("【1/4】拉上游")
    ref = "HEAD" if getattr(args, "offline", False) else "origin/main"
    if getattr(args, "offline", False):
        log("  [offline] 跳过 fetch/merge，用本地 HEAD 快照")
        ref = "HEAD"
    elif args.check:
        run(["git", "-C", repo, "fetch", "origin"], check=False)
    else:
        run(["git", "-C", repo, "fetch", "origin"])
        if repo_is_dirty(repo) and not args.force:
            step.fail("%s 工作区有未提交改动 —— 先 stash/提交，或用 --force" % repo)
            return
        rc, out = run(["git", "-C", repo, "merge", "--ff-only", "origin/main"], check=False)
        if rc != 0 and "Already up to date" not in out:
            # 用户可能就停在本地提交上；不强行失败，但要说清楚
            log("  ⚠ 未快进：%s" % out.splitlines()[-1][:120])
        else:
            log("  " + (out.splitlines()[-1] if out else "already up to date"))
    rc, head = run(["git", "-C", repo, "log", "--format=%h %s", "-1", ref])
    log("  %s = %s" % (ref, head))

    log("【2/4】确定同步集")
    files = []
    for rel in git_tracked_files(repo, "origin/main"):
        if rel.startswith(QD_SYNC_SET) and rel.endswith(".py"):
            files.append(rel)
        elif rel in QD_FILES_EXTRA:
            files.append(rel)
        elif rel.startswith("umid/"):
            files.append(rel)
    log("  %d 个文件（qoder_*.py + %s + umid/）" % (len(files), ", ".join(QD_FILES_EXTRA)))

    if args.check:
        log("【3/4】--check：跳过写盘")
    else:
        log("【3/4】分发")
        for target, label in ((REPO, "融合仓根"), (QD_VENDOR, "vendored hub/engines/qd")):
            n = 0
            for rel in files:
                s = os.path.join(repo, rel.replace("/", os.sep))
                d = os.path.join(target, rel.replace("/", os.sep))
                if not os.path.exists(s):
                    continue
                if args.dry_run:
                    n += 1
                    continue
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
                n += 1
            log("  %-24s <- %d 个文件" % (label, n))

    ensure_portal_redirect(QD_VENDOR, log)
    ensure_portal_redirect(REPO, log)      # qd 的镜像就是融合仓根

    log("【4/4】校验")
    bad = diff_report(REPO, QD_VENDOR, "repo", "vendor",
                      only=set(f for f in rel_files(QD_VENDOR) if f.endswith(".py")))
    if bad:
        step.fail("融合仓根 vs vendored(qd) 不一致: %s" % ", ".join(bad[:6]))
    else:
        log("  ✅ 融合仓根 = vendored(qd)（.py 逐字节）")



# --------------------------------------------------------------------------- trae
def sync_trae(args, log, step):
    """Trae 的上游仓 = 合并damo/trae2api-hub（第 109 轮加入同步管线）。

    与 wb/qd 的差别：融合仓**仓根没有** Trae 文件（它只活在 hub/engines/trae/），
    所以这里只有一个分发目标。分发集见 TRAE_SYNC_GLOBS。
    """
    repo = trae_repo()
    if not os.path.isdir(os.path.join(repo, ".git")):
        step.fail("trae 上游不是 git 仓: %s（用 TRAE_SYNC_REPO 覆盖）" % repo)
        return

    log("【1/3】拉上游")
    ref = "HEAD" if getattr(args, "offline", False) else "origin/main"
    if getattr(args, "offline", False):
        log("  [offline] 跳过 fetch/merge，用本地 HEAD 快照")
        ref = "HEAD"
    elif args.check:
        run(["git", "-C", repo, "fetch", "origin"], check=False)
    else:
        run(["git", "-C", repo, "fetch", "origin"])
        if repo_is_dirty(repo) and not args.force:
            step.fail("%s 工作区有未提交改动 —— 先 stash/提交，或用 --force" % repo)
            return
        rc, out = run(["git", "-C", repo, "merge", "--ff-only", "origin/main"], check=False)
        if rc != 0 and "Already up to date" not in out:
            log("  ⚠ 未快进：%s" % (out.splitlines()[-1][:120]))
        else:
            log("  " + (out.splitlines()[-1] if out else "already up to date"))
    rc, head = run(["git", "-C", repo, "log", "--format=%h %s", "-1", ref])
    log("  %s = %s" % (ref, head))

    log("【2/3】确定同步集")
    files = []
    for rel in git_tracked_files(repo, "origin/main"):
        base = rel.split("/")[-1]
        if rel.endswith(".py") and base.startswith("trae_"):
            files.append(rel)
        elif rel.endswith(".json") and base.startswith("trae_catalog"):
            files.append(rel)
        elif base.startswith("dashboard") and rel.endswith((".html", ".js", ".css")):
            files.append(rel)
        elif rel == "pricing/trae_pricing.json":
            files.append(rel)
    log("  %d 个文件（trae_*.py + catalog + dashboard* + pricing）" % len(files))

    if args.check:
        log("【3/3】--check：跳过写盘")
        return

    log("【3/3】分发到 vendored")
    n = 0
    for rel in files:
        s = os.path.join(repo, rel.replace("/", os.sep))
        d = os.path.join(TRAE_VENDOR, rel.replace("/", os.sep))
        if not os.path.exists(s):
            continue
        if args.dry_run:
            n += 1
            continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)
        n += 1
    log("  %-24s <- %d 个文件" % ("vendored hub/engines/trae", n))
    ensure_portal_redirect(TRAE_VENDOR, log)


# --------------------------------------------------------------------------- 入口
def main(argv=None):
    ap = argparse.ArgumentParser(description="上游同步管线（wb / qd → 融合仓全部副本）")
    ap.add_argument("--wb", action="store_true", help="同步 wb 上游")
    ap.add_argument("--qd", action="store_true", help="同步 qd 上游")
    ap.add_argument("--trae", action="store_true", help="同步 trae 上游（第 109 轮）")
    ap.add_argument("--check", action="store_true", help="只校验现状，不写盘")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    ap.add_argument("--force", action="store_true", help="工作区脏也继续（跳过检查）")
    ap.add_argument("--with-user-dirs", action="store_true",
                    help="同时分发到用户桌面副本（默认不碰：那是用户的部署）")
    ap.add_argument("--offline", action="store_true",
                    help="离线：跳过 git fetch/merge，直接用本地 HEAD 快照（网络不通时用）")
    ap.add_argument("--test", action="store_true", help="同步后跑 hub 静态判据 + wb 补丁测试")
    args = ap.parse_args(argv)

    if not (args.wb or args.qd or args.trae):
        ap.print_help()
        return 2

    # ★ 控制台编码兜底（第 109 轮实测）：中文 Windows 的 stdout 是 GBK，
    #   末尾那句 "RESULT: ✅ …" 会让 print 抛 UnicodeEncodeError ——
    #   于是**同步成功了却返回非零**，脚本里看到的就是"失败"。
    #   errors="replace" 只是把画不出来的字符打成 ?，不改变流程。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    def log(msg):
        print(msg, flush=True)

    step = Step(log)
    # 引擎名按参数拼，别再手写"a 和 b"的组合式（加第三家时那串空格会算错）
    _picked = [n for n, on in (("wb", args.wb), ("qd", args.qd), ("trae", args.trae)) if on]
    log("上游同步管线 · " + " + ".join(_picked))
    log("  镜像   : %s" % WB_MIRROR)
    log("  vendored: %s" % WB_VENDOR)
    log("  补丁   : %s" % PATCH_DIR)
    log("")

    try:
        if args.wb:
            sync_wb(args, log, step)
            log("")
        if args.qd:
            sync_qd(args, log, step)
            log("")
        if args.trae:
            sync_trae(args, log, step)
            log("")
    except Exception as exc:  # noqa: BLE001 - 工具要把失败原因原样带给操作者
        step.fail("%s: %s" % (type(exc).__name__, exc))
        return 1

    if args.test and step.ok:
        log("【测试】hub 静态判据 + wb 补丁相关套件")
        py = os.path.join(REPO, "hub", "..", "wb", "python", "python.exe")
        py = os.path.normpath(py)
        python = py if os.path.exists(py) else sys.executable
        for cmd, cwd, label in (
            ([python, "hub/tests/run_all.py", "static"], REPO, "hub 静态"),
        ):
            rc, out = run(cmd, cwd=cwd, check=False)
            tail = " | ".join(out.splitlines()[-3:])
            log("  %s: %s" % (label, tail[:150]))
            if rc != 0:
                step.fail("%s 判据未通过" % label)
        wb_tests = [os.path.join(WB_MIRROR, "tests", t) for t in
                    ("_test_auto_realm.py", "_test_remote_catalog.py")]
        for t in wb_tests:
            if not os.path.exists(t):
                continue
            rc, out = run([python, t], cwd=WB_MIRROR, check=False)
            log("  %s: %s" % (os.path.basename(t), "OK" if rc == 0 else "FAIL"))
            if rc != 0:
                step.fail("%s 未通过" % os.path.basename(t))

    log("")
    if step.ok:
        log("RESULT: ✅ 同步完成且全部校验通过")
        if not args.check and not args.dry_run:
            log("  提示：运行中的引擎仍是旧代码 —— 需要重启对应进程才会加载新版本。")
        return 0
    log("RESULT: ❌ 有步骤失败（上面 [FAIL] 行）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
