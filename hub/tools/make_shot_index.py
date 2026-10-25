#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/shots/ 里的截图生成一个索引页（第 168 轮）。

为什么是 HTML 而不是合成图片：本仓库**零依赖**（不装 PIL），而验收时想看的是
"深色和浅色并排"—— 一张 HTML 就能做到，还能随时重新生成、随时点开原图。

用法：python hub/tools/make_shot_index.py
产物：docs/shots/index.html
"""
import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.path.join(ROOT, "docs", "shots")
MATRIX = os.path.join(SHOTS, "matrix")
PAGES = ["overview", "accounts", "models", "metrics", "logs", "api", "settings"]
PAGE_ZH = {"overview": "总览", "accounts": "账号池", "models": "模型清单",
           "metrics": "数据指标", "logs": "请求流水", "api": "接入说明",
           "settings": "设置"}


#: 页面 → 判定关键词（用来从测试套件里数"这页验过几条"）
#: 关键词要**贴页面**，不能宽（第 169 轮实测）：
#: 用"账号"会把「D16 metrics 渲染出『按账号』透视表头」也算进账号页；
#: 用"api"会把所有 api.js 的模块判据都算进「接入说明」页。宁可少算，不要虚高。
PAGE_KEYS = {
    "overview": ("总览", "overview"),
    "accounts": ("accounts", "账号页"),
    "models": ("models", "模型页", "模型清单"),
    "metrics": ("metrics", "指标页", "数据指标"),
    "logs": ("流水", "logs"),
    "api": ("接入说明", "api 页"),
    "settings": ("设置页", "settings"),
}


def count_checks():
    """扫描 admin/tests/_test_*.js 里的 check('...')，按页面关键词分组。

    为什么自动数而不是手写：手写的清单一定会漂 —— 加了用例没人记得回来改。
    数出来的数字每次生成时都是**当下**的真实值。
    """
    import re
    tests = os.path.join(ROOT, "admin", "tests")
    labels = []
    if os.path.isdir(tests):
        for name in sorted(os.listdir(tests)):
            if not (name.startswith("_test_") and name.endswith(".js")):
                continue
            try:
                with open(os.path.join(tests, name), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            labels.extend(re.findall(r"check\(\s*'([^']+)'", text))
            labels.extend(re.findall(r'check\(\s*"([^"]+)"', text))
    out = {}
    for page, keys in PAGE_KEYS.items():
        hits = [l for l in labels if any(k in l for k in keys)]
        out[page] = hits
    return out, len(labels)


#: hub/tests 的套件 → 区域（索引页按区域汇总，而不是列 19 行）
BACKEND_AREA = {
    "engine": ("引擎契约", ("engine_static", "engine_scan")),
    "aggregate": ("聚合与合并", ("aggregator", "hub_shapes", "merged_", "unified_keys", "contract")),
    "portal": ("门户与启动", ("portal", "smoke", "firstrun")),
    "tools": ("工具与自检", ("gateway_tool", "realdata_tool", "tools_health", "token_refresh",
                          "make_fixture", "verify_")),
    "meta": ("元判据", ("harness_skip", "import_isolation", "dead_symbols", "mutation")),
}


def count_backend():
    """统计 hub/tests 的套件与判据条数，按区域汇总。

    为什么要把后端也放上验收页：只看前端表格会给人"这项目只有界面"的错觉 ——
    实际上三引擎的搬运、聚合、鉴权全在后端那 19 套里。
    """
    import re
    tests = os.path.join(ROOT, "hub", "tests")
    suites = []
    if os.path.isdir(tests):
        suites = sorted(n for n in os.listdir(tests) if n.startswith("_test_") and n.endswith(".py"))
    buckets = {}
    for name in suites:
        area = "tools"
        for key, (_label, needles) in BACKEND_AREA.items():
            if any(nd in name for nd in needles):
                area = key
                break
        try:
            with open(os.path.join(tests, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        n = len(re.findall(r"H\.check\(\s*['\"]", text))
        b = buckets.setdefault(area, {"suites": 0, "checks": 0})
        b["suites"] += 1
        b["checks"] += n
    return buckets, len(suites)


def pngs(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.lower().endswith(".png"))


def main(argv=None):
    # 必须支持 --help：hub/tests 的 T01 会逐个工具跑一遍（第一版没写 argparse，
    # 结果 --help 直接把索引生成了，T01 当场红）。
    ap = argparse.ArgumentParser(
        prog="python hub/tools/make_shot_index.py",
        description="把 docs/shots 里的截图生成索引页（深/浅并排）")
    ap.add_argument("--out", default=os.path.join(SHOTS, "index.html"),
                    help="输出路径（默认 docs/shots/index.html）")
    args = ap.parse_args(argv)
    out_path = args.out

    single = pngs(SHOTS)
    matrix = set(pngs(MATRIX))
    parts = []
    parts.append("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>截图索引 · WorkBuddy x Qoder x Trae-hub</title>
<style>
  :root { color-scheme: dark light; }
  body { margin:0; padding:24px; font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
         background:#0e1116; color:#e6edf3; }
  h1 { font-size:20px; margin:0 0 4px; }
  h2 { font-size:16px; margin:32px 0 12px; padding-bottom:6px; border-bottom:1px solid #263041; }
  .note { color:#8b98a9; font-size:13px; margin-bottom:8px; }
  .pair { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:20px; }
  .pair figure { margin:0; }
  .pair figcaption { font-size:13px; color:#8b98a9; margin-top:4px; }
  img { width:100%; display:block; border-radius:8px; border:1px solid #263041; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:14px; }
  .grid figure { margin:0; }
  .grid figcaption { font-size:12px; color:#8b98a9; margin-top:4px; word-break:break-all; }
  /* 断点压到 480px：这一页本身常被窄窗口打开，760px 会让"并排对照"直接失效 */
  table.sum { width:100%; border-collapse:collapse; font-size:13px; }
  table.sum th, table.sum td { text-align:left; padding:6px 8px; border-bottom:1px solid #263041; vertical-align:top; }
  table.sum td:nth-child(-n+3), table.sum th:nth-child(-n+3) { white-space:nowrap; }
  table.sum th { color:#8b98a9; font-weight:500; }
  table.sum tr.zero td { color:#d29922; }
  @media (max-width:480px){ .pair { grid-template-columns:1fr; } }
  .pair img { max-height:520px; object-fit:cover; object-position:top; }
</style></head><body>
<h1>截图索引</h1>
<div class="note">视口固定 500px（移动断点）。深/浅成对展示；点图可看原图。</div>
""")

    # 0. 每页验过什么（数字来自测试套件，生成时现算）
    counts, total = count_checks()
    parts.append("<h2>每页验过什么</h2>\n")
    parts.append('<div class="note">数字 = admin/tests 里<strong>提到该页</strong>的判据条数（生成时现算，'
                 '共抓到 %d 条 check 标签）。<br>'
                 '⚠ 这是<strong>近似值</strong>：有约 15%% 的条目只是"顺带提到"（例如 '
                 '<code>W9 … hub/accounts 接口</code>、<code>W66a … logs/realdata-report.json</code>），'
                 '并不专属于该页。0 仍然说明这一页还没有任何判据。</div>\n' % total)
    parts.append('<table class="sum"><thead><tr><th>页面</th><th>判据</th><th>几条</th><th>举例</th></tr></thead><tbody>\n')
    for page in PAGES:
        hits = counts.get(page) or []
        sample = "；".join(h[:42] for h in hits[:3]) if hits else "—"
        cls = "" if hits else ' class="zero"'
        parts.append("<tr%s><td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>\n"
                     % (cls, PAGE_ZH.get(page, page), page, len(hits), sample))
    parts.append("</tbody></table>\n")

    # 0b. 后端（hub/tests）按区域汇总
    buckets, n_suites = count_backend()
    if buckets:
        total_checks = sum(b["checks"] for b in buckets.values())
        parts.append("<h2>后端验了什么（hub/tests · %d 套）</h2>\n" % n_suites)
        parts.append('<div class="note">判据条数按<strong>字面标签</strong>统计（%d 条）；'
                     '有部分用例把标签写成变量，所以比实际总数略少 —— 看趋势，不要当精确覆盖率。</div>\n'
                     % total_checks)
        parts.append('<table class="sum"><thead><tr><th>区域</th><th>套件</th><th>判据</th></tr></thead><tbody>\n')
        order = ["engine", "aggregate", "portal", "tools", "meta"]
        for key in order:
            b = buckets.get(key)
            if not b:
                continue
            label = BACKEND_AREA[key][0]
            parts.append("<tr><td>%s</td><td>%d</td><td>%d</td></tr>\n" % (label, b["suites"], b["checks"]))
        parts.append("</tbody></table>\n")

    # 1. 主题对照（成对）
    parts.append("<h2>深色 / 浅色对照（7 页）</h2>\n")
    for page in PAGES:
        d = "dark-%s.png" % page
        l = "light-%s.png" % page
        if d not in matrix and l not in matrix:
            continue
        parts.append('<div class="note">%s · %s</div>\n<div class="pair">\n' % (PAGE_ZH.get(page, page), page))
        for theme, name in (("深色", d), ("浅色", l)):
            if name in matrix:
                parts.append('  <figure><img src="matrix/%s" alt="%s"><figcaption>%s</figcaption></figure>\n' % (name, name, theme))
            else:
                parts.append('  <figure><div class="note">（缺 %s）</div></figure>\n' % theme)
        parts.append("</div>\n")

    # 2. 交互细节
    # 索引页自己的预览图不该出现在列表里（自我引用）
    # 索引页自己的预览图（index-*.png）不该出现在列表里 —— 自我引用
    SKIP = {"index.html"}
    detail = [n for n in single if n not in SKIP and not n.startswith("index-")]
    if detail:
        parts.append("<h2>交互与状态细节</h2>\n<div class=\"grid\">\n")
        for n in detail:
            parts.append('  <figure><img src="%s" alt="%s"><figcaption>%s</figcaption></figure>\n' % (n, n, n))
        parts.append("</div>\n")

    parts.append("</body></html>\n")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    print("已生成 %s（%d 张单图 + %d 张矩阵图）" % (out_path, len(detail), len(matrix)))


if __name__ == "__main__":
    main()
