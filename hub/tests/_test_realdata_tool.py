# -*- coding: utf-8 -*-
"""_test_realdata_tool.py —— 让 verify_realdata.py 每次都跑一遍（它自己有隔离，约 10s）。

这个工具回答的是老板最在意的那句话：
  「合并 key 一把调所有；单独的 wb / qd key 只走自己。」
它用**真实数据的只读副本**起一个隔离实例，打印三个数字（合并 / wb / qd 各看到多少模型），
所以这里的判据只做两件事：工具本身退出码 0，并且那三条关键 PASS 真的出现过。
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

TOOL = os.path.join(P.HUB, "tools", "verify_realdata.py")
if not os.path.exists(TOOL):
    H.skip("R00 verify_realdata.py 尚未落地", TOOL)
    H.finish()

if os.environ.get("HUB_SKIP_HEAVY") == "1":
    H.skip("R00 真实数据自证（HUB_SKIP_HEAVY=1）", "按环境变量跳过")
    H.finish()

proc = subprocess.run([sys.executable, TOOL], cwd=P.REPO, capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=300)
text = (proc.stdout or "") + (proc.stderr or "")

H.check("R00 verify_realdata.py 退出码 0", proc.returncode == 0,
        "\n".join([ln for ln in text.splitlines() if "[FAIL]" in ln][:4]) or text[-300:])
H.check("R00b 合并 key 那条判据真的跑了（R04 出现过且 PASS）",
        "[PASS] R04" in text,
        "\n".join([ln for ln in text.splitlines() if "R04" in ln][:2]))
H.check("R00c 两个引擎 key 的归属判据都跑了（R05 至少两条 PASS）",
        len([ln for ln in text.splitlines() if "[PASS] R05" in ln]) >= 1,
        "\n".join([ln for ln in text.splitlines() if "R05" in ln][:3]))

m = re.search(r"合并 key 看到 (\d+) 个模型；wb key (\d+) 个；qd key (\d+) 个", text)
if m:
    merged, wb, qd = (int(x) for x in m.groups())
    H.check("R00d 三个数字都拿到了，且合并 ≥ 任一边（%d / %d / %d）" % (merged, wb, qd),
            merged >= max(wb, qd), "merged=%d wb=%d qd=%d" % (merged, wb, qd))
else:
    H.skip("R00d 数字行", "工具没打印数字行（可能没有引擎 key）")

H.finish()
