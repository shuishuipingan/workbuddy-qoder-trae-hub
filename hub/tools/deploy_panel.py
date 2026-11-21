#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/tools/deploy_panel.py —— 把 admin/app/dist 发布到 admin/（不制造 404 空窗）。

为什么要有它：以前的发布是「先删 admin/static 再拷」——
中间那几百毫秒里，**正在用面板的浏览器再点一下路由**，就会拿到
`Failed to fetch dynamically imported module: .../authenticated-XXXX.js`。
用户看到的是"后台报错进不去"，实际只是发布窗口撞上了。

做法：先**覆盖**新文件（旧文件继续留着可用），再删掉已经不在新构建里的旧文件。
两个动作之间，任何旧 URL 都仍然可访问。

用法：python hub/tools/deploy_panel.py [--dry-run]
"""
import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
REPO = os.path.dirname(HUB)
DIST = os.path.join(REPO, "admin", "app", "dist")
TARGET = os.path.join(REPO, "admin")


def _walk(root):
    out = set()
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            out.add(os.path.relpath(full, root))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布 admin 面板（覆盖优先，删除在最后）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not os.path.isdir(DIST):
        print("dist 不存在：%s（先 npx vite build）" % DIST)
        return 2
    new_files = _walk(DIST)
    if "index.html" not in new_files or not any(f.startswith("static" + os.sep) for f in new_files):
        print("dist 缺少入口或静态资源")
        return 2
    old_files = {os.path.join(d, f) for d in ("static", "assets")
                 for f in _walk(os.path.join(TARGET, d))}
    copied = 0
    for rel in sorted(new_files, key=lambda f: (f == "index.html", f)):
        src = os.path.join(DIST, rel)
        dst = os.path.join(TARGET, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if args.dry_run:
            print("copy", rel)
            continue
        if rel == "index.html":
            fd, tmp = tempfile.mkstemp(prefix=".panel-", dir=TARGET)
            os.close(fd)
            try:
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        else:
            shutil.copy2(src, dst)
        copied += 1
    stale = sorted(old_files - new_files)
    # 只清理构建产物目录，别碰 logo.jpg / 手工放进去的东西
    removable = [r for r in stale if r.split(os.sep)[0] in ("static", "assets")]
    for rel in removable:
        if args.dry_run:
            print("remove", rel)
            continue
        try:
            os.remove(os.path.join(TARGET, rel))
        except OSError:
            pass
    if args.dry_run:
        return 0
    print("发布完成：覆盖 %d 个文件，清理 %d 个旧产物" % (copied, len(removable)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
