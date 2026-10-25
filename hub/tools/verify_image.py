#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_image.py -- 验**镜像里**的注册表与文件是不是三家（第 182 轮）。

为什么需要它：D 系列判据（_test_docker_contract.py）只查文件写得对不对。
可"源码三家、镜像两家"是真实发生过的（第 76 轮：Dockerfile 的 CMD 只起 wb/qd）。
只读文本永远发现不了这件事 —— 必须进容器里看一眼。

用法：
  python hub/tools/verify_image.py                  # 用已有镜像 hub:verify
  python hub/tools/verify_image.py --build          # 先 docker build
  python hub/tools/verify_image.py --image hub:test

退出码：0 全过（或环境没有 docker，SKIP）；1 有层没过。
"""
import argparse
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def run(cmd, timeout=180):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "docker not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:
        return 1, str(exc)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python hub/tools/verify_image.py",
        description="验镜像内的注册表与引擎目录（三家）")
    ap.add_argument("--image", default="hub:verify", help="镜像名（默认 hub:verify）")
    ap.add_argument("--build", action="store_true", help="先 docker build -t <image> .")
    args = ap.parse_args(argv)

    rc, out = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    if rc != 0:
        print("[SKIP] 本机没有可用的 docker：%s" % (out.strip().splitlines() or ["?"])[0][:80])
        return 0
    print("[1/4] docker 可用：%s" % out.strip()[:40])

    if args.build:
        print("[2/4] docker build -t %s ." % args.image)
        rc, out = run(["docker", "build", "-t", args.image, "."], timeout=600)
        if rc != 0:
            print("  build 失败：%s" % out.strip()[-300:])
            return 1
        print("  build OK")
    else:
        print("[2/4] 跳过构建（用已有镜像 %s）" % args.image)

    checks = []
    py = ("import hub; "
          "print('ENGINES=' + ','.join(hub.ENGINES)); "
          "print('NAME=' + hub.HUB_NAME)")
    rc, out = run(["docker", "run", "--rm", args.image, "python", "-c", py])
    engines = ""
    name = ""
    for line in out.splitlines():
        if line.startswith("ENGINES="):
            engines = line.split("=", 1)[1].strip()
        elif line.startswith("NAME="):
            name = line.split("=", 1)[1].strip()
    want = ("wb", "qd", "trae")
    got = tuple(x for x in engines.split(",") if x)
    checks.append(("L1 镜像内注册表是三家", got == want, "ENGINES=%s" % (engines or "(空)")))
    checks.append(("L2 镜像内产品名是三家", "Trae" in name, "NAME=%s" % (name or "(空)")))

    rc, out = run(["docker", "run", "--rm", args.image, "ls", "hub/engines"])
    dirs = sorted(x for x in out.split() if x)
    checks.append(("L3 镜像内有全部引擎目录", set(dirs) >= set(want), "dirs=%s" % dirs))

    rc, out = run(["docker", "run", "--rm", args.image, "python", "-c",
                   "import hub.core.portal as p; print('portal-ok')"])
    checks.append(("L4 镜像内门户模块可导入", "portal-ok" in out, out.strip()[:60]))

    # L5：镜像里真的"起一遍"（--dry-run 只打印计划，不绑端口）
    rc, out = run(["docker", "run", "--rm", args.image, "python", "-m", "hub.server",
                   "--dry-run", "--wb-port", "8789", "--qd-port", "8790",
                   "--trae-port", "8792", "--portal-port", "8791"])
    planned = {}
    for line in out.splitlines():
        m = re.search(r"\b(wb|qd|trae)\s+port\s+(\d+)", line)
        if m:
            planned[m.group(1)] = int(m.group(2))
    want_ports = {"wb": 8789, "qd": 8790, "trae": 8792}
    checks.append(("L5 --dry-run 计划里三家端口正确", planned == want_ports,
                   "planned=%s" % planned))

    print("")
    print("层级                                结果  实际")
    print("-" * 72)
    failed = 0
    for label, ok, detail in checks:
        if not ok:
            failed += 1
        print("%-35s %-5s %s" % (label, "PASS" if ok else "FAIL", detail))
    print("-" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
