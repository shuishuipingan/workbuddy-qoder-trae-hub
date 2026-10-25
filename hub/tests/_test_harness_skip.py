# -*- coding: utf-8 -*-
"""_test_harness_skip.py —— 元层：证明「别人文件没落地时，测试报缺失/跳过而不是崩」。

这条要求只靠嘴说没用，得能自证 —— 做法是把**测试自己**搬进一棵「空 hub」：
  临时树 = hub/tests/** + hub/__init__.py + 空 hub/core/（没有引擎、没有 server.py、没有聚合器/门户）
然后在临时树里跑真正的编排器，断言：
  * 依赖缺失的套件报 [SKIP]（或 MISSING）而不是抛异常；
  * 编排器退出码是 0（默认口径：skip 不算红）；
  * 加 --strict 后同一棵树退出码变 1（skip 被升级成红）；
  * 全程 stderr 无 Traceback（"不许崩"的硬判据）。
  另外顺带自证 harness 的端口安全阀：free_port() 永不返回 8789/8790/8791。
此处不碰仓库任何既有文件：只读源码 + 往临时目录写副本。
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

if os.environ.get("HUB_TESTS_IN_SANDBOX"):
    H.skip("元套件在沙箱树里被二次调用，直接跳过（防递归）")
    H.finish()


def run(cmd, cwd):
    log = os.path.join(cwd, "_run.log")
    proc = H.popen(cmd, cwd=cwd, log_path=log)
    rc = proc.wait(timeout=180)
    with open(log, encoding="utf-8", errors="replace") as fh:
        return rc, fh.read()


def suite_log_of(runner_out, suite_file):
    """编排器的输出里带 `logs: <dir>`；套件的 [SKIP] 正文在那个目录的同名日志里。

    断言要落在对的那一层：编排器只打「每套件一行 + 失败 tail」，正文在套件日志。
    """
    marker = "(logs: "
    for line in runner_out.splitlines():
        if marker in line:
            logdir = line.split(marker, 1)[1].rstrip(") ").strip()
            path = os.path.join(logdir, suite_file + ".log")
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return fh.read()
    return ""


# ---------- 架一棵「空 hub」 ----------
work = tempfile.mkdtemp(prefix="hub-empty-")
empty = os.path.join(work, "hub")
os.makedirs(os.path.join(empty, "core"), exist_ok=True)
# 关键：不要把自己复制进去 —— 否则临时树的 run_all 会再跑一次本套件，无限递归（实测踩过）。
H.copy_tree(os.path.join(P.HUB, "tests"), os.path.join(empty, "tests"),
           skip_names=("__pycache__", os.path.basename(__file__)))
shutil.copy2(os.path.join(P.HUB, "__init__.py"), os.path.join(empty, "__init__.py"))
leftover = os.path.join(empty, "tests", os.path.basename(__file__))
if os.path.exists(leftover):                 # 防御：沙箱里绝不能有本套件，否则 run_all 递归
    os.remove(leftover)
H.check("X01 空 hub 树已就位（没有引擎 / server.py / aggregator.py / portal.py）",
        os.path.isdir(os.path.join(empty, "core"))
        and not os.path.exists(os.path.join(empty, "server.py"))
        and not os.path.exists(os.path.join(empty, "core", "aggregator.py"))
        and not os.path.exists(os.path.join(empty, "core", "portal.py")), empty)

# ---------- 依赖缺失的套件：SKIP 而不是崩 ----------
rc_agg, out_agg = run([sys.executable, os.path.join(empty, "tests", "run_all.py"), "aggregator"], cwd=work)
agg_log = suite_log_of(out_agg, "_test_aggregator.py")
H.check("X02 聚合器缺失时：套件报 [SKIP]，编排器退出码 0",
        rc_agg == 0 and "[SKIP]" in agg_log and "skipped=1" in out_agg.replace("  ", " "),
        "rc=%s | 套件日志含 SKIP=%s | 编排器行: %s" % (
            rc_agg, "[SKIP]" in agg_log, [l.strip() for l in out_agg.splitlines() if "aggregator" in l][:1]))
H.check("X02b 且没有任何 Traceback（不许崩）", "Traceback" not in out_agg and "Traceback" not in agg_log)

rc_portal, out_portal = run([sys.executable, os.path.join(empty, "tests", "run_all.py"), "portal"], cwd=work)
portal_log = suite_log_of(out_portal, "_test_portal.py")
H.check("X03 门户缺失时：套件报 [SKIP]，编排器退出码 0",
        rc_portal == 0 and "[SKIP]" in portal_log,
        "rc=%s | 套件日志含 SKIP=%s" % (rc_portal, "[SKIP]" in portal_log))
H.check("X03b 编排器的汇总里能看到 skipped 计数（不是静默跳过）",
        "skipped=" in out_agg and "skipped=" in out_portal)

# ---------- --strict：skip 升级成红 ----------
rc_strict, out_strict = run([sys.executable, os.path.join(empty, "tests", "run_all.py"), "--strict", "aggregator"], cwd=work)
H.check("X04 --strict 下同一棵树退出码变 1（skip 被当成红）", rc_strict == 1, "rc=%s" % rc_strict)
H.check("X04b --strict 的提示写明原因", "[strict]" in out_strict)

# ---------- 全量跑（混合：有的红、有的 skip）也不许崩 ----------
rc_all, out_all = run([sys.executable, os.path.join(empty, "tests", "run_all.py")], cwd=work)
H.check("X05 空树上跑全量：退出码 1（有红），但不崩", rc_all == 1 and "Traceback" not in out_all, "rc=%s" % rc_all)
H.check("X05b 汇总行仍然是三套编排器的统一口径（能被解析）",
        "SUMMARY: " in out_all and "TOTAL" in out_all and "checks" in out_all,
        [l for l in out_all.splitlines() if l.startswith("SUMMARY")][:1])

# ---------- wait_log 的自证（E12c/d/e 靠它做条件等待） ----------
wlog = os.path.join(work, 'waitlog-probe.txt')
with open(wlog, 'w', encoding='utf-8') as fh:
    fh.write('engine wb up\n')
ok_hit, waited_hit, text_hit = H.wait_log(wlog, 'engine wb up', timeout=2.0)
H.check('X07 wait_log 命中已存在的行（且返回等待时长与全文）',
        ok_hit is True and waited_hit < 1.0 and 'engine wb up' in text_hit, 'waited=%.2f' % waited_hit)
ok_miss, waited_miss, _ = H.wait_log(wlog, 'engine NEVER up', timeout=0.6, poll=0.2)
H.check('X07b wait_log 对不存在的行按时超时（不是立刻返回、也不是挂死）',
        ok_miss is False and waited_miss >= 0.5, 'waited=%.2f' % waited_miss)

# 追加写入后能被再次读到（模拟引擎冷启动时日志滞后于 /health）
with open(wlog, 'a', encoding='utf-8') as fh:
    fh.write('engine qd up\n')
ok_app, _, _ = H.wait_log(wlog, 'engine qd up', timeout=1.0)
H.check('X07c wait_log 能等到「后来才写进日志」的行（这正是间歇红的根因场景）', ok_app is True)

# ---------- 端口安全阀 ----------
ports = [H.free_port() for _ in range(30)]
H.check("X06 free_port() 永不返回保留端口（8789/8790/8791 旧默认 ∪ 8811/8812/8817 新默认）",
        all(p not in (8789, 8790, 8791, 8811, 8812, 8817) for p in ports), ports[:8])
H.check("X06b free_port() 每次给的都是当前空闲端口", all(H.port_is_free(p) for p in ports))

H.info("空 hub 树: %s" % empty)
H.finish()
