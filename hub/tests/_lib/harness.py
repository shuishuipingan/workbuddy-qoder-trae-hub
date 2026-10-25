# -*- coding: utf-8 -*-
"""hub/tests/_lib/harness.py —— 零依赖断言 / 汇总 / 进程收尾（与 admin 那套同口径）。

口径（三个编排器一致，便于拼总账）：
    [PASS] / [FAIL] / [MISSING] / [SKIP] / [INFO]
    SUMMARY: TOTAL N checks, P passed, F failed, S skipped
    退出码：0 全过 / 1 有红 / 2 环境或参数问题

与前两套的差异：hub 的门槛是「别人文件没落地时不许崩」，所以 missing()/skip() 是一等公民 ——
它们不抛异常、也不静默：分别在汇总里计 missing / skipped，由编排器的 --strict 决定是否判红。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

# Windows 控制台/管道的默认编码可能是 cp936：中文判据会变乱码甚至抛 UnicodeEncodeError。
# 这里自锁两条流（hub/server.py:152-157 是同样的做法），把编码问题钉在最近的一层。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

_state = {"total": 0, "passed": 0, "failed": 0, "missing": 0, "skipped": 0}
_failed_labels = []


def check(label, ok, detail=""):
    _state["total"] += 1
    if ok:
        _state["passed"] += 1
        print("  [PASS] %s" % label)
    else:
        _state["failed"] += 1
        _failed_labels.append(label)
        print("  [FAIL] %s%s" % (label, ("  " + str(detail)) if detail else ""))
    return bool(ok)


def eq(label, actual, expected):
    return check(label, actual == expected, "actual=%r expected=%r" % (actual, expected))


def missing(what, why=""):
    """前置文件没落地：记一条 MISSING（算红，但绝不抛）。"""
    _state["total"] += 1
    _state["failed"] += 1
    _state["missing"] += 1
    _failed_labels.append("MISSING: " + what)
    print("  [MISSING] %s%s" % (what, ("  " + str(why)) if why else ""))
    return False


def skip(what, why=""):
    """环境/前置换不来：记一条 SKIP（不计红；--strict 时编排器把 skip 升级成红）。"""
    _state["total"] += 1
    _state["skipped"] += 1
    print("  [SKIP] %s%s" % (what, ("  " + str(why)) if why else ""))
    return False


def info(msg):
    print("  [INFO] %s" % msg)


def guard(label, fn, *args, **kwargs):
    """跑一段可能抛的代码：抛了记 FAIL 并把异常带出来（套件自身不许崩）。"""
    try:
        return True, fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        check(label, False, "%s: %s" % (type(exc).__name__, exc))
        return False, None


def summary():
    print("")
    print("  SUMMARY: TOTAL %d checks, %d passed, %d failed, %d skipped%s" % (
        _state["total"], _state["passed"], _state["failed"], _state["skipped"],
        (" (%d missing)" % _state["missing"]) if _state["missing"] else ""))
    if _failed_labels:
        print("  failed: %s" % " | ".join(_failed_labels[:12]))
    print("  RESULT: %s" % ("RED (exit 1)" if _state["failed"] else "GREEN (exit 0)"))
    return 1 if _state["failed"] else 0


def finish():
    sys.exit(summary())


#: 永不分配给测试的端口：**旧默认**（8789/8790/8791，操作者线上实例常用）
#: ∪ **新默认**（8811/8812/8817，与启动器一致）。
RESERVED_PORTS = (8789, 8790, 8791, 8811, 8812, 8817)


def free_port():
    """挑一个空闲回环端口。**永不返回 RESERVED_PORTS 里的端口**（线上实例与默认端口）。"""
    for _ in range(50):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        if port not in RESERVED_PORTS:
            return port
    raise RuntimeError("no free port found")


def port_is_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_get(url, timeout=3.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.getcode(), resp.read()


def http_json(url, timeout=3.0):
    status, body = http_get(url, timeout=timeout)
    return status, json.loads(body.decode("utf-8", "replace"))


def wait_health(port, timeout=30.0, host="127.0.0.1"):
    """轮询 /health；返回 (payload|None, 耗时秒)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            status, payload = http_json("http://%s:%d/health" % (host, port), timeout=1.5)
            if status == 200:
                return payload, time.time() - t0
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return None, time.time() - t0


def popen(cmd, cwd=None, env=None, log_path=None):
    """起后台进程；stdout/stderr 合并写入 log_path。"""
    out = open(log_path, "wb") if log_path else subprocess.DEVNULL
    merged = dict(os.environ)
    merged["PYTHONIOENCODING"] = "utf-8"
    merged["PYTHONUNBUFFERED"] = "1"
    if env:
        merged.update(env)
    return subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, env=merged)


def kill_tree(proc, wait=8.0):
    """收掉进程并等它真退出；返回是否已退出（引擎跑在守护线程里，杀父进程即可）。"""
    if proc is None:
        return True
    try:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=wait)
    except Exception:  # noqa: BLE001
        return False
    return proc.poll() is not None


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def wait_log(path, needle, timeout=60.0, poll=0.4):
    """条件等待：轮询日志直到出现 needle（或超时）。返回 (命中, 实际等待秒, 全文)。

    为什么不用固定 sleep：qd 引擎冷启动要 3–5s（解 QMC 目录、读 umid），机器负载高时会更久；
    固定窗口会产出**间歇红**，而间歇红会训练团队「重跑一次就好」——那比假绿更难治。
    """
    t0 = time.time()
    while True:
        text = read_text(path)
        if needle in text:
            return True, time.time() - t0, text
        if time.time() - t0 >= timeout:
            return False, time.time() - t0, text
        time.sleep(poll)


def tail(path, n=25):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip() for ln in fh if ln.strip()]
    except OSError:
        return []
    return lines[-n:]


def copy_tree(src, dst, skip_names=("__pycache__",)):
    """整树复制到临时目录，返回复制文件数。

    skip_names 同时作用于**目录名与文件名**（第一版只过滤目录，导致「把元套件排除掉」这个
    意图静默失效，沙箱里又跑了一次元套件 -> 无限递归；这是实测踩过的坑）。
    """
    count = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_names]
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            if name in skip_names:
                continue
            shutil.copy2(os.path.join(root, name), os.path.join(target, name))
            count += 1
    return count
