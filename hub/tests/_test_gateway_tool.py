# -*- coding: utf-8 -*-
"""_test_gateway_tool.py —— 让 hub/tools/verify_gateway.py 每次都被**真的执行**一遍。

由来（第 78 轮）：给那个工具加了 G10（两个引擎的错误体形状一致）之后才发现，
它从来没被判据跑过 —— 仓里只有一条静态检查确认它列了端点清单（W49），
也就是说 G01–G10 随时可以烂掉而没人知道。

做法：起一个隔离实例（临时数据根 + 动态端口）→ 登记一把合并 key →
跑 verify_gateway.py --base ... --key-merged ... → 断言退出码 0 且 G10 出现在输出里。
全程不碰仓库里的 accounts/usage（HUB_DATA_ROOT 指到临时目录）。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

TOOL = os.path.join(P.HUB, "tools", "verify_gateway.py")
if not os.path.exists(TOOL):
    H.skip("G00 verify_gateway.py 尚未落地", TOOL)
    H.finish()

MERGED_KEY = "gw-suite-merged-key-001"
work = tempfile.mkdtemp(prefix="hub-gwtool-")
data_root = os.path.join(work, "data")
os.makedirs(data_root, exist_ok=True)
log = os.path.join(work, "server.log")
proc = None
wb_port, qd_port, portal_port = H.free_port(), H.free_port(), H.free_port()


def _post(path_, body, token=None):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Panel-Token"] = token
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (portal_port, path_),
                                 data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


try:
    proc = H.popen([sys.executable, "-m", "hub.server", "--host", "127.0.0.1",
                    "--wb-port", str(wb_port), "--qd-port", str(qd_port),
                    "--portal-port", str(portal_port)],
                   # cwd 必须是仓库根：python -m hub.server 要能找到 hub 包；
                   # 写入隔离靠 HUB_DATA_ROOT（数据根搬到临时目录），不靠 cwd。
                   cwd=P.REPO, env={"HUB_DATA_ROOT": data_root}, log_path=log)
    health = None
    for _ in range(120):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/hub/health" % portal_port,
                                        timeout=1.5) as resp:
                health = json.loads(resp.read().decode("utf-8", "replace"))
                break
        except Exception:
            time.sleep(0.5)
    H.check("G00a 隔离实例起来了（导入工具前的前置）", health is not None,
            "日志尾部: %s" % (H.tail(log, 5),))
    if health is None:
        H.finish()

    status = _post("/api/hub/settings/save",
                   {"merged_keys": [{"name": "gw", "key": MERGED_KEY, "enabled": True}]},
                   token="admin")
    H.check("G00b 已登记一把合并 key（工具需要一个能通过的 key）", status == 200, "status=%s" % status)

    out = subprocess.run([sys.executable, TOOL, "--base",
                          "http://127.0.0.1:%d" % portal_port,
                          "--key-merged", MERGED_KEY],
                         cwd=P.REPO, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=180)
    text = (out.stdout or "") + (out.stderr or "")
    H.check("G00c verify_gateway.py 退出码 0（工具自身全绿）", out.returncode == 0,
            "\n".join([ln for ln in text.splitlines() if "[FAIL]" in ln][:4]) or text[-300:])
    H.check("G00d 输出里出现了 G10（错误体形状一致那条真的跑了）", "G10" in text and "[PASS] G10" in text,
            "\n".join([ln for ln in text.splitlines() if "G10" in ln][:2]))
    H.check("G00e 合并 key 的两条关键判定通过（G02 merged / G06 带 X-Hub-Route）",
            "[PASS] G02" in text and "[PASS] G06" in text,
            "\n".join([ln for ln in text.splitlines() if "G02" in ln or "G06" in ln][:3]))
finally:
    if proc is not None:
        H.kill_tree(proc)
    H.info("临时目录: %s" % work)

H.finish()
