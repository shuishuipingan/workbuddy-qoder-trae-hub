# -*- coding: utf-8 -*-
"""hub/tests/_test_firstrun_tool.py —— 把「空环境首次可用」接进回归。

背景：本轮发现的两个真 bug（建合并 key 401、保存路由 401）**只在空环境出现**：
有数据的实例里会话恰好带着能过的令牌，于是套件永远是绿的。

之前这条线靠手工起实例验证，容易被忘。现在把它固化成套件：
直接调用 hub/tools/verify_firstrun.py（它自己起隔离实例、用完就拆）。

跳过开关：
    HUB_SKIP_HEAVY=1   跳过本条（约 40–60s）；CI 想跑快时用，本地/验收请保留。
"""
import os
import subprocess
import sys

from _lib import harness as H
from _lib import paths as P

TOOL = os.path.join(P.REPO, "hub", "tools", "verify_firstrun.py")

if not os.path.exists(TOOL):
    H.skip("F0 空环境首跑工具不存在", TOOL)
    H.finish()

if os.environ.get("HUB_SKIP_HEAVY") == "1":
    H.skip("F0 空环境首跑（HUB_SKIP_HEAVY=1）", "按环境变量跳过重型用例")
    H.finish()

# HUB_FIRSTRUN_WITH_DATA=1 时带上 --with-data：会把仓库里现有的账号目录**复制**到
# 隔离数据根，用来验证「加账号后合并视图是并集」（默认关，避免每次都复制凭据）。
ARGS = [sys.executable, TOOL]
if os.environ.get("HUB_FIRSTRUN_WITH_DATA") == "1":
    ARGS.append("--with-data")

proc = subprocess.run(
    ARGS,
    cwd=P.REPO,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=float(os.environ.get("HUB_FIRSTRUN_TIMEOUT") or 240),
    env=dict(os.environ, PYTHONIOENCODING="utf-8"),
)
out = (proc.stdout or "") + (proc.stderr or "")
tail = "\n".join(out.strip().splitlines()[-14:])

H.check("F01 verify_firstrun.py 退出码 0（空环境：建 key / 存路由 / 自检 / 取模型都通过）",
        proc.returncode == 0, "exit=%s\n%s" % (proc.returncode, tail))

m = None
for line in out.splitlines():
    if line.startswith("SUMMARY:"):
        m = line.strip()

passed = bool(m and "失败 0" in m)
H.check("F02 工具自报『失败 0』", passed, m or "(没有 SUMMARY 行)\n" + tail)

# 关键三步必须在输出里出现过 PASS（防止工具被改成"永远绿"）
for label, needle in (
    ("F03 建合并 key 那一项通过", "建一把合并 key"),
    ("F04 保存路由那一项通过", "保存路由偏好"),
    ("F05 新 key 取模型那一项通过", "请求 /api/v1/models"),
):
    ok = any(("[PASS]" in ln and needle in ln) for ln in out.splitlines())
    H.check(label, ok, "输出里没看到 PASS 行包含『%s』\n%s" % (needle, tail))

H.finish()
