# -*- coding: utf-8 -*-
"""_test_smoke_dual_engine.py —— ③ 端到端冒烟：一个进程、**三家引擎**、三个端口。

（文件名里的 dual 是历史：第 87 轮从两家扩到三家，改名会牵动文档清单，故保留。）

做法（每一步都可复核）：
  1. 把整个 hub/ 复制到临时目录再跑 —— 引擎的数据目录会落在副本里，**仓库零写入**；
  2. 顺手在副本的 wb 引擎里写 accounts/settings.json = {"pricing_enabled": false}：
     否则 wb 的定价刷新线程会在首次启动时真的出网取价（wb_settings.py:886-887 默认开；
     wb_pricing.py:2371-2378 关掉即空转）。测试机不该有外呼。
  3. 端口全部动态挑空闲，**硬性排除 8789/8790/8791**（线上实例与默认端口）；
  4. 起 `python -m hub.server --host 127.0.0.1 --wb-port X --qd-port Y --trae-port Z`，等三个 /health；
  5. 断言三方形状：wb 不带 service，qd= qoder-proxy，trae= trae2api-hub —— 这是区分引擎的可靠特征；
     并额外断言两边的 payload **不相同**（防两个引擎抢同一个端口的回归，Lead 踩过一次）；
  6. finally 收进程，断言端口重新空闲（不留孤儿）；并比对仓库引擎目录前后快照。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P
sys.path.insert(0, P.REPO)
from hub import ENGINE_META                     # noqa: E402  引擎特征表（唯一真源）

HEALTH_TIMEOUT = 45.0


def port_owner(port):
    """监听该端口的 PID（Windows: netstat -ano）。拿不到就返回 None —— 调用方记 SKIP，绝不猜。"""
    # 注意：不能只靠 PATH 找 netstat —— 在 PATH 被裁剪的环境（CI 精简容器、某些自动化沙箱）里
    # 会静默退化成 SKIP，而「该红的时候静默跳过」本身就是一种盲区。所以带绝对路径兜底。
    out = None
    for exe in ("netstat", "netstat.exe", r"C:\\Windows\\System32\\netstat.exe"):
        try:
            out = subprocess.run([exe, "-ano", "-p", "tcp"], capture_output=True, text=True,
                                 timeout=20).stdout
            break
        # ⚠️ 只吞"这条 exe 起不来"，**不要**吞所有异常：
        #   这里原来写的是 except Exception —— 于是本套件少写一行 import subprocess 时，
        #   NameError 被吞成"三个候选都不可用"，端口归属断言**在任何机器上都永远 SKIP**。
        #   实测（第 44 轮）：E12 从来没真正跑过，而它守的正是"端口被别人家的实例占着"这种假绿。
        except (OSError, subprocess.SubprocessError):
            continue
    if not out:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        local = parts[1]                       # 形如 127.0.0.1:64846
        if local.rsplit(":", 1)[-1] == str(port):
            return int(parts[4])
    return None


def snapshot(root):
    out = {}
    if not os.path.isdir(root):
        return out
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = st.st_size
    return out


if not os.path.exists(os.path.join(P.HUB, "server.py")):
    H.missing("E01 hub/server.py 不存在，端到端冒烟无法进行", P.HUB)
    H.finish()

wb_port = H.free_port()
qd_port = H.free_port()
trae_port = H.free_port()
H.check("E02 动态端口不与线上/默认端口冲突，且当前空闲",
        wb_port not in (8789, 8790, 8791, 8811, 8812, 8817) and qd_port not in (8789, 8790, 8791, 8811, 8812, 8817)
        and wb_port != qd_port and H.port_is_free(wb_port) and H.port_is_free(qd_port),
        "wb=%d qd=%d" % (wb_port, qd_port))

work = tempfile.mkdtemp(prefix="hub-smoke-")
copy_root = os.path.join(work, "hub")     # 必须叫 hub：cwd=work 时 `-m hub.server` 才解析得到这个包
n = H.copy_tree(P.HUB, copy_root)
H.check("E03 把 hub/ 整树复制到临时目录（隔离数据写入）", n > 20, "复制 %d 个文件到 %s" % (n, copy_root))
H.info("副本里 server.py=%s" % os.path.exists(os.path.join(copy_root, "server.py")))

settings_dir = os.path.join(copy_root, "engines", "wb", "accounts")
os.makedirs(settings_dir, exist_ok=True)
with open(os.path.join(settings_dir, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"pricing_enabled": False}, fh)
H.info("已在副本里关掉 wb 定价刷新（避免测试期真实外呼取价）")

wb_before, qd_before = snapshot(P.WB_ENGINE), snapshot(P.QD_ENGINE)
log = os.path.join(work, "server.log")
proc = None
wb_health = qd_health = None
try:
    # 数据目录必须搬出仓库：引擎默认把 accounts/usage 写在自己的引擎目录里（paths.py:125-160），
    # 不设 HUB_DATA_ROOT 的话，冒烟一跑就往 repo 的 hub/engines/<e>/{accounts,usage} 落文件 ——
    # 这正是 E11 抓到的（2026-10-09：wb 引擎目录 5.9 分钟前被改写）。
    os.makedirs(os.path.join(work, "data"), exist_ok=True)
    data_root_env = {"HUB_DATA_ROOT": os.path.join(work, "data")}
    proc = H.popen([sys.executable, "-m", "hub.server", "--host", "127.0.0.1",
                    "--wb-port", str(wb_port), "--qd-port", str(qd_port),
         "--trae-port", str(trae_port)],
                   cwd=work, env=data_root_env, log_path=log)
    H.check("E04 hub.server 已启动（pid=%s）" % proc.pid, proc.poll() is None)

    wb_health, wb_secs = H.wait_health(wb_port, timeout=HEALTH_TIMEOUT)
    H.check("E05 wb 引擎 /health 在 %.0fs 内应答" % HEALTH_TIMEOUT, wb_health is not None,
            "payload=%s | 日志尾部: %s" % (wb_health, H.tail(log, 6)))
    if wb_health is None:
        H.info("server.log 全文尾部:\n    " + "\n    ".join(H.tail(log, 25)))

    qd_health, qd_secs = H.wait_health(qd_port, timeout=HEALTH_TIMEOUT)
    H.check("E06 qd 引擎 /health 在 %.0fs 内应答" % HEALTH_TIMEOUT, qd_health is not None,
            "payload=%s | 日志尾部: %s" % (qd_health, H.tail(log, 6)))

    trae_health, trae_secs = H.wait_health(trae_port, timeout=HEALTH_TIMEOUT)
    H.check("E06b trae 引擎 /health 在 %.0fs 内应答" % HEALTH_TIMEOUT, trae_health is not None,
            "payload=%s | 日志尾部: %s" % (trae_health, H.tail(log, 6)))

    healths = {"wb": wb_health, "qd": qd_health, "trae": trae_health}
    # 第 188 轮：形状断言**从注册表派生** —— 加一家引擎只需在 ENGINE_META 填一行，
    # 测试不用改。原来这里写死 `== "qoder-proxy"` / `== "trae2api-hub"`，加第四家必然漏。
    for _e in ENGINE_META:
        _h = healths.get(_e)
        if not _h:
            continue
        H.info("%s /health（%s）= %s" % (_e, _e, json.dumps(_h, ensure_ascii=False)[:200]))
        H.check("E07-%s /health 形状：ok=true" % _e, _h.get("ok") is True, _h)
        _want = ENGINE_META[_e].get("service")
        if _want is None:
            H.check("E07b-%s /health **没有** service 字段（注册表如此）" % _e,
                    "service" not in _h, sorted(_h.keys()))
        else:
            H.check("E08-%s /health service == %s（注册表 ENGINE_META 说的）" % (_e, _want),
                    _h.get("service") == _want and "version" in _h, _h)

    if all(healths.get(e) for e in ENGINE_META):
        H.check("E09 各家端口的 payload 互不相同（防「引擎抢同一端口」的回归）",
                len({json.dumps(healths[e], sort_keys=True) for e in ENGINE_META}) == len(ENGINE_META),
                "payload 有重复")
        H.check("E09b 区分特征稳定：有无 service 与注册表完全一致",
                all(("service" in healths[e]) == (ENGINE_META[e].get("service") is not None)
                    for e in ENGINE_META))

        def wait_or_report(needle, label, limit=20.0):
            """在 server.log 里等某个片段出现；超时把 waited 报出来（调用方补全量日志）。"""
            started = time.time()
            waited = 0.0
            ok = False
            while time.time() - started < limit:
                waited = time.time() - started
                # H.tail 返回**行列表**（不是字符串），必须 join；参数是路径不是文件对象。
                if needle in "\n".join(H.tail(log, 400) or []):
                    ok = True
                    break
                time.sleep(0.2)
            H.check(label, ok,
                    '' if ok else ('等了 %.1fs 仍未出现 —— 下面给全量日志与归因上下文' % waited))
            if ok:
                H.info('%s 等 %.1fs' % (label, waited))
            return ok, waited

        need_ctx = False
        for needle, label in (('engine wb up', 'E12c wb 启动序列'), ('engine qd up', 'E12c qd 启动序列'),
                          ('engine trae up', 'E12c trae 启动序列')):
            ok, _ = wait_or_report(needle, label)
            need_ctx = need_ctx or not ok
        ok_d, _ = wait_or_report(':%d/v1' % wb_port, 'E12d wb banner 端口 == 我分配的端口')
        ok_e, _ = wait_or_report(':%d/v1' % qd_port, 'E12e qd banner 端口 == 我分配的端口')
        ok_f, _ = wait_or_report(':%d/v1' % trae_port, 'E12e trae banner 端口 == 我分配的端口')
        need_ctx = need_ctx or (not ok_d) or (not ok_e) or (not ok_f)

        if need_ctx:
            # 失败时把「为什么」一起打出来：进程还在不在、端口被谁占、引擎自己的最后几行说了什么
            H.info('E12 归因上下文: 我的 pid=%s 存活=%s；端口 wb:%d owner=%s / qd:%d owner=%s（None=netstat 取不到）'
                   % (proc.pid, proc.poll() is None, wb_port, port_owner(wb_port), qd_port, port_owner(qd_port)))
            H.info('E12 归因上下文: 机器上 python 进程数=%d'
                   % (len(subprocess.run(['tasklist', '/FI', 'IMAGENAME eq python.exe'], capture_output=True,
                                         text=True, timeout=20).stdout.splitlines()) if os.name == 'nt' else -1))
            full = H.read_text(log)
            H.info('E12 失败全量日志（%d 行）:' % len(full.splitlines()))
            for line in full.splitlines()[-200:]:
                H.info('  server.log | ' + line)
finally:
    killed = H.kill_tree(proc)
    H.check("E10 进程已收掉（无孤儿）", killed, "pid=%s" % (proc.pid if proc else None))
    time.sleep(0.8)
    released = False
    for _ in range(20):                      # 监听关掉后端口可能被 TIME_WAIT 短暂占住，给点耐心
        if H.port_is_free(wb_port) and H.port_is_free(qd_port):
            released = True
            break
        time.sleep(0.3)
    H.check("E10b 两个端口都重新空闲（监听真的释放了）", released,
            "wb_free=%s qd_free=%s" % (H.port_is_free(wb_port), H.port_is_free(qd_port)))

# ★★ E11 重做（Lead 第 49 轮 · 追到第 2 次红之后定案）
#
# 原判据：snapshot(P.WB_ENGINE) == wb_before —— **拿文件系统差异推断「谁写的」，而那是推不出来的**。
#
# 为什么不可靠：**仓库里那份 hub/engines/wb 正是开发实例在用的目录**。
#   只要同时跑着一个开发实例（python -m hub.server --wb-port 8811 …，用的就是默认数据根），
#   它每处理一个请求就往那里写 —— E11 分不清「冒烟写的」和「那个实例写的」。
#   实测：两次红都出在我用浏览器猛刷后台、开发实例正在写的时候。
#
# 而冒烟本身**从一开始就是对的**（上面 103 行把 HUB_DATA_ROOT 指到了临时目录）——
#   E11 报的不是「冒烟写坏了」，而是「**恰好有别的东西也在写那个目录**」。
#
# 正确的查法：**验配置，而不是盯共享资源**。
#   ① 冒烟必须把数据根设在仓库之外（结构性，与并发无关）；
#   ② 临时副本里必须带着 engines/（写会落在副本里）。
# 文件系统差异降为 INFO —— 它当年确实抓到过真问题（2026-10-09），
#   但那个信号只在「没有别的实例在跑」时才成立，不适合判红。
try:
    _dr = os.path.abspath(data_root_env['HUB_DATA_ROOT'])
except NameError:
    _dr = ''
_repo_abs = os.path.abspath(P.HUB)
H.check('E11 冒烟把数据根设在仓库之外（HUB_DATA_ROOT 指向临时目录）',
        bool(_dr) and os.path.commonpath([_dr, _repo_abs]) != _repo_abs,
        'HUB_DATA_ROOT=%s / 仓库=%s' % (_dr, _repo_abs))
H.check('E11b 临时副本里带着 engines/（写会落在副本里）',
        os.path.isdir(os.path.join(copy_root, 'engines')),
        'copy_root/engines 不存在')
if snapshot(P.WB_ENGINE) != wb_before or snapshot(P.QD_ENGINE) != qd_before:
    H.info('E11c 仓库引擎目录在冒烟期间有变化 —— **这不判红**：若同时有开发实例在用默认数据根，'
           '它也会写这里，两者在文件系统上无法区分（见 E11 的说明）')
H.info("临时副本与日志: %s" % work)
for line in H.tail(log, 8):
    H.info("server.log | " + line)
H.info("（副本目录保留给排查；清理可手动删 %s）" % work)

H.finish()
