# -*- coding: utf-8 -*-
"""_test_import_isolation.py —— ② 导入隔离：两个引擎同进程共存、零模块重名冲突。

两层判据：
  文件级：两个引擎目录的模块名集合交集必须为空（wb_* 与 qoder_* 天然前缀隔离）；
  运行级：把两个目录同时插进 sys.path、在一个子进程里 import 两边入口，
          断言每个引擎的模块都解析到**自己那个目录**的文件、没有任何模块名被对方顶掉。

隔离措施：子进程 cwd = 临时目录；wb 引擎的 ACCOUNTS_DIR / WB_PROXY_USAGE_DIR 指到临时目录；
并在导入前后对两个引擎目录做快照，断言 **import 不写仓库**（引擎数据目录一个文件都不许变）。
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P


def snapshot(root):
    """目录内容快照：相对路径 -> (大小, mtime_ns)。用于「导入不许写盘」的比对。"""
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
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


# ---------- 文件级：零重名 ----------
wb_names = set(P.engine_files("wb"))
qd_names = set(P.engine_files("qd"))
trae_names = set(P.engine_files("trae"))
# 第 185 轮：从"两家"扩到"三家" —— 两两交集都要空，不能只看 wb∩qd。
pairs = {"wb∩qd": sorted(wb_names & qd_names),
         "wb∩trae": sorted(wb_names & trae_names),
         "qd∩trae": sorted(qd_names & trae_names)}
bad = {k: v for k, v in pairs.items() if v}
H.check("I01 三家引擎目录的模块名两两交集为空", not bad, "重名: %s" % bad)
H.info("wb=%d, qd=%d, trae=%d, 并集=%d"
       % (len(wb_names), len(qd_names), len(trae_names),
          len(wb_names | qd_names | trae_names)))
H.check("I01b wb 侧模块名全部以 wb_ 开头", all(n.startswith("wb_") for n in wb_names))
H.check("I01c qd 侧模块名全部以 qoder_ 开头", all(n.startswith("qoder_") for n in qd_names))
H.check("I01d trae 侧模块名全部以 trae_ 开头", all(n.startswith("trae_") for n in trae_names))
H.info("对照：Lead 口头说的是「实测 21 个模块」——21 是**运行时真正被导入**的模块数（见 I03），")
H.info("      而这里是目录里可导入的源文件数（%d+%d+%d=%d）；两个数不是一回事，故都记下来。"
       % (len(wb_names), len(qd_names), len(trae_names), len(wb_names | qd_names | trae_names)))

# ---------- 运行级：同进程双导入 ----------
if not (os.path.exists(os.path.join(P.WB_ENGINE, "wb_proxy.py"))
        and os.path.exists(os.path.join(P.QD_ENGINE, "qoder_proxy.py"))
        and os.path.exists(os.path.join(P.TRAE_ENGINE, "trae_proxy.py"))):
    H.missing("I02 三家入口 import（wb_proxy / qoder_proxy / trae_proxy 缺一不可）",
              "%s | %s | %s" % (P.WB_ENGINE, P.QD_ENGINE, P.TRAE_ENGINE))
    H.finish()

CHILD = r'''
import json, os, sys
WBDIR, QDDIR, TRAEDIR = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, WBDIR)
sys.path.insert(0, QDDIR)
sys.path.insert(0, TRAEDIR)
out = {"entries": {}, "by_prefix": {}, "collisions": [], "error": None}
try:
    import wb_proxy
    import qoder_proxy
    import trae_proxy
    out["entries"]["wb_proxy"] = getattr(wb_proxy, "__file__", None)
    out["entries"]["qoder_proxy"] = getattr(qoder_proxy, "__file__", None)
    out["entries"]["trae_proxy"] = getattr(trae_proxy, "__file__", None)
    seen = {}
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        if name.startswith("wb_") or name.startswith("qoder_") or name.startswith("trae_"):
            pref = ("wb_" if name.startswith("wb_")
                    else "qoder_" if name.startswith("qoder_") else "trae_")
            out["by_prefix"].setdefault(pref, []).append([name, f])
        if name in seen and seen[name] != f:
            out["collisions"].append([name, seen[name], f])
        seen[name] = f
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
'''

workdir = tempfile.mkdtemp(prefix="hub-import-")
wb_before = snapshot(P.WB_ENGINE)
qd_before = snapshot(P.QD_ENGINE)
trae_before = snapshot(P.TRAE_ENGINE)
env = dict(os.environ)
env["ACCOUNTS_DIR"] = os.path.join(workdir, "wb-accounts")
env["WB_PROXY_USAGE_DIR"] = os.path.join(workdir, "wb-usage")
env["TRAE_PROXY_USAGE_DIR"] = os.path.join(workdir, "trae-usage")
def run_child():
    return subprocess.run([sys.executable, "-c", CHILD,
                           P.WB_ENGINE, P.QD_ENGINE, P.TRAE_ENGINE],
                          capture_output=True, text=True, timeout=180, cwd=workdir, env=env)


proc = run_child()
if proc.returncode != 0 and 'SyntaxError' in (proc.stderr or ''):
    # 引擎文件正在被改写（编辑器非原子保存 / 多文件重构中间态）：复跑一次再判
    import time as _t
    _t.sleep(0.4)
    proc = run_child()
    if proc.returncode == 0:
        H.info('首次 import 撞上引擎文件正在改写（SyntaxError），复跑后正常 —— 不是缺陷')
H.check("I03 子进程能同时 import 三家入口（wb_proxy / qoder_proxy / trae_proxy）",
        proc.returncode == 0,
        (proc.stderr or "").strip().splitlines()[-3:] if proc.stderr else "rc=%d" % proc.returncode)
if proc.returncode != 0:
    H.finish()
data = json.loads(proc.stdout.strip().splitlines()[-1])

wb_entry = data["entries"].get("wb_proxy") or ""
qd_entry = data["entries"].get("qoder_proxy") or ""
trae_entry = data["entries"].get("trae_proxy") or ""
ok_entries = (
    os.path.normcase(os.path.dirname(wb_entry)) == os.path.normcase(P.WB_ENGINE)
    and os.path.normcase(os.path.dirname(qd_entry)) == os.path.normcase(P.QD_ENGINE)
    and os.path.normcase(os.path.dirname(trae_entry)) == os.path.normcase(P.TRAE_ENGINE)
    and all([wb_entry, qd_entry, trae_entry]))
H.check("I04 三个入口都解析到自己引擎目录里的文件", ok_entries,
        "wb=%s | qd=%s | trae=%s" % (wb_entry, qd_entry, trae_entry))
H.check("I04b 三个入口是互不相同的文件（没有互相顶替）",
        len({wb_entry, qd_entry, trae_entry}) == 3,
        "%s" % [wb_entry, qd_entry, trae_entry])

wb_mods = data["by_prefix"].get("wb_", [])
qd_mods = data["by_prefix"].get("qoder_", [])
trae_mods = data["by_prefix"].get("trae_", [])
H.info("运行时导入的引擎模块数：wb_=%d + qoder_=%d + trae_=%d = %d"
       % (len(wb_mods), len(qd_mods), len(trae_mods),
          len(wb_mods) + len(qd_mods) + len(trae_mods)))
H.check("I05 运行时确实导入了三家的模块（wb_ / qoder_ / trae_ 都非空）",
        len(wb_mods) > 0 and len(qd_mods) > 0 and len(trae_mods) > 0,
        "wb=%d qd=%d trae=%d" % (len(wb_mods), len(qd_mods), len(trae_mods)))

stray = [n for n, f in wb_mods
         if os.path.normcase(os.path.dirname(f)) != os.path.normcase(P.WB_ENGINE)]
stray += [n for n, f in qd_mods
          if os.path.normcase(os.path.dirname(f)) != os.path.normcase(P.QD_ENGINE)]
stray += [n for n, f in trae_mods
          if os.path.normcase(os.path.dirname(f)) != os.path.normcase(P.TRAE_ENGINE)]
H.check("I06 没有引擎模块被解析到别家目录（这是隔离的硬判据）", not stray,
        "越界: %s" % stray[:6])
H.check("I06b 没有任何模块名对应两个不同文件（零顶替）",
        not data["collisions"], "冲突: %s" % data["collisions"][:4])

# ---------- 导入不许写仓库 ----------
wb_after = snapshot(P.WB_ENGINE)
qd_after = snapshot(P.QD_ENGINE)
trae_after = snapshot(P.TRAE_ENGINE)
H.check("I07 import 过程没有改动 wb 引擎目录（含 accounts/usage）",
        wb_before == wb_after, "变化: %s" % sorted(set(wb_before) ^ set(wb_after))[:6])
H.check("I07b import 过程没有改动 qd 引擎目录（含 accounts/umid）",
        qd_before == qd_after, "变化: %s" % sorted(set(qd_before) ^ set(qd_after))[:6])
H.check("I07c import 过程没有改动 trae 引擎目录（含 accounts/usage）",
        trae_before == trae_after, "变化: %s" % sorted(set(trae_before) ^ set(trae_after))[:6])
H.info("临时工作目录: %s（子进程的账号/用量目录都指到它，仓库零写入）" % workdir)

H.finish()
