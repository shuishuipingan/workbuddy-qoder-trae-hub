#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/server.py —— 双引擎网关中枢的进程入口。

一个进程里同时起 WorkBuddy 与 Qoder 两个引擎，各自监听自己的端口：

    python -m hub.server                     # wb:8789  qd:8790
    python -m hub.server --wb-port 8801 --qd-port 8802
    python -m hub.server --engines wb        # 只起一个

数据目录（HUB_DATA_ROOT）：默认两个引擎各自用 hub/engines/<e>/{accounts,usage}；
设了 HUB_DATA_ROOT 就搬到 <root>/<e>/{accounts,usage}。解析与透传统一走
hub/core/paths.py，启动横幅会打印**实际生效**的路径。

设计取舍（诚实说明）：
  * 引擎的 main() 被放进**守护线程**运行，argv 是临时替换的——这样引擎代码
    一行都不用改。代价是它们共用 stdout 与 GIL；对 IO 密集的反代场景无影响。
  * 本轮是"同进程双端口"，还不是"单端口路径前缀"（/api/wb、/api/qd）。
    路径前缀化属于下一阶段，见 docs/merge/01-后端融合总纲.md §2 / §7。
"""
import argparse
import os
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hub import (HUB_VERSION, HUB_NAME, HUB_PORT, DEFAULT_PORTS, resolve_engine,
                     ENGINES, ENGINE_META)
    from hub.core import paths
else:
    from . import (HUB_VERSION, HUB_NAME, HUB_PORT, DEFAULT_PORTS, resolve_engine,
                   ENGINES, ENGINE_META)
    from .core import paths

HERE = os.path.dirname(os.path.abspath(__file__))
# 目录表从注册表派生（第 100 轮）：加引擎不再改这里。
ENGINE_DIRS = {name: os.path.join(HERE, "engines", meta["dir"]) for name, meta in ENGINE_META.items()}


def engine_ready(engine):
    """引擎的模块能不能被导入（不启动服务）。"""
    d = ENGINE_DIRS.get(engine)
    meta = ENGINE_META.get(engine) or {}
    return bool(d) and bool(meta) and os.path.isfile(os.path.join(d, meta["entry"]))


# --------------------------------------------------------------------------- 日志统一
# 各家引擎/门户自己打的方括号前缀 —— 全部替换成产品名，
# 否则一行一个名字，看着不像一个产品（用户口径：日志要统一）。
_LOG_PREFIXES = ("wb-proxy", "qd-proxy", "trae2api", "portal", "machine-identity")
_LOG_PREFIX_RE = None


class _UnifiedLog(object):
    """把各家日志的 [xxx] 前缀统一成 [产品名] 的 stdout 包装。

    引擎与门户都是**同进程线程**，共享 sys.stdout；在这里做一次行首重写，
    比让每个引擎改自己的 print 靠谱（上游每次同步都会覆盖那些文件）。
    """

    def __init__(self, stream, brand):
        self._stream = stream
        self._brand = brand
        self._buf = ""
        global _LOG_PREFIX_RE
        if _LOG_PREFIX_RE is None:
            import re as _re
            _LOG_PREFIX_RE = _re.compile(
                r"^\[(?:" + "|".join(_LOG_PREFIXES) + r")\]\s*")

    def _rewrite(self, text):
        return _LOG_PREFIX_RE.sub("[%s] " % self._brand, text)

    #: 引擎自报的 per-port 广告（listening/dashboard）—— 融合模式下端口是内部实现，
    #: 打出来只会让"一个产品一个入口"这句话自相矛盾。HUB_LOG_VERBOSE=1 可放出来。
    _NOISE_RE = None

    def _is_noise(self, text):
        import re as _re
        if _UnifiedLog._NOISE_RE is None:
            # 行首可能带时间戳（"13:54:51 listening : ..."），所以先允许一个可选前缀。
            _UnifiedLog._NOISE_RE = _re.compile(
                r"^(?:\S+\s+)?(listening|dashboard)\s*:\s*http://\S+:\d+")
        return bool(_UnifiedLog._NOISE_RE.match(text))

    def write(self, text):
        import os as _os
        if not isinstance(text, str):
            return self._stream.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if _os.environ.get("HUB_LOG_VERBOSE", "").strip() in ("1", "true", "yes"):
                self._stream.write(self._rewrite(line) + "\n")
                continue
            if self._is_noise(line):
                continue
            # trae 的空账号方框：里面写着"打开 8813 点 OAuth"，与单入口矛盾。
            if "NO ACCOUNTS YET" in line:
                self._skip_box = True
                continue
            if getattr(self, "_skip_box", False):
                if line.strip().startswith("====="):
                    self._skip_box = False
                continue
            self._stream.write(self._rewrite(line) + "\n")
        return len(text)

    def flush(self):
        if self._buf:
            self._stream.write(self._rewrite(self._buf))
            self._buf = ""
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def start_engine(engine, host, port, extra=None):
    """在守护线程里跑某个引擎的 main()；返回 Thread。

    第 100 轮起**完全由注册表驱动**：入口文件名与模块名都从 ENGINE_META 取，
    加第三个引擎（Trae）时这里一行没改。
    """
    d = ENGINE_DIRS[engine]
    meta = ENGINE_META[engine]
    if d not in sys.path:
        sys.path.insert(0, d)
    argv = [os.path.join(d, meta["entry"]),
            "--host", host, "--port", str(port)] + list(extra or [])

    def run():
        saved = sys.argv
        sys.argv = argv
        try:
            # 按注册表里的模块名 import —— 三家都用扁平模块名（wb_/qoder_/trae_ 前缀），
            # 实测可同进程共存、零重名冲突（第 100 轮加 Trae 时验证）。
            mod = __import__(meta["module"])
            mod.main()
        except SystemExit as exc:            # 端口被占等：不要让整进程跟着退
            print("  [%s] engine %s exited: %s" % (HUB_NAME, engine, exc), flush=True)
        except Exception as exc:             # noqa: BLE001 - 引擎自爆不能拖垮另一个
            print("  [%s] engine %s crashed: %r" % (HUB_NAME, engine, exc), flush=True)
        finally:
            sys.argv = saved

    t = threading.Thread(target=run, name="engine-" + engine, daemon=True)
    t.start()
    return t


def port_free(host, port):
    """端口预检：能 bind 上再立刻 close，视为空闲。

    为什么需要它（2026-10-15 实测事故）：引擎是**线程内**起的，sys.argv 是进程级共享，
    只能串行启动 + 逐个等 health。但 health 检查会被「这个端口上已经有人应答」骗过 ——
    操作者自己的 wb 正跑在 8789、hub 默认也是 8789 时，hub 把别人的实例当成自己刚起的
    引擎，串行保护失效，下一个引擎的 argv 盖掉上一个（现象：wb/qd 同时绑到 qd 的端口，
    hub 报 qd 健康检查返回的是 wb 的形状）。预检在动手之前就把端口冲突查出来。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False


def health_matches(engine, data):
    """应答体是不是**这个引擎**的形状（防 argv 串号后引擎答错端口）。

    第 100 轮起由注册表的 service 字段驱动 —— 加 Trae 时这里原本只认识两家，
    于是它的 /health（service='trae2api-hub'）永远匹配不上，整进程被判「起不来」。
    """
    if not isinstance(data, dict):
        return False
    svc = (ENGINE_META.get(engine) or {}).get("service")
    if svc:
        return data.get("service") == svc
    # 没有 service 字段的引擎（wb）：用它独有的字段认，并要求对方**没有** service
    return ("api_key_required" in data) and not data.get("service")


def wait_health(port, host="127.0.0.1", timeout=25.0, expect=None):
    import json
    import urllib.request
    deadline = time.time() + timeout
    url = "http://%s:%d/health" % (host, port)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                if expect and not health_matches(expect, data):
                    time.sleep(0.4)      # 端口上应答的是**别的引擎/别的实例**，继续等
                    continue
                return data
        except Exception:
            time.sleep(0.4)
    return None


def effective_dir(argv, flag, fallback):
    """从**合并后**的 argv 里取某个目录参数的实际生效值。

    为什么需要它：argparse 对重复出现的 --accounts-dir 是"后者覆盖前者"，
    而我们把 paths 的值放在前、用户的 --wb-args/--qd-args 放在后，所以用户的写法
    会赢。启动横幅若直接打印 paths 算出来的值，就会**报出一个引擎其实不会用的目录**
    —— 一份说假话的报告比不打印更糟。这里按最后一次出现取值，没有就用 fallback。

    同时接受 "--flag value" 与 "--flag=value" 两种写法。
    """
    value = fallback
    for index, token in enumerate(argv or []):
        if token == flag and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith(flag + "="):
            value = token.split("=", 1)[1]
    return value


def start_portal(host, port, engine_ports, admin_dir):
    """在守护线程里起融合面（hub/core/portal.py 的 main()）。返回 Thread。

    与引擎同一个理由：portal.main() 也读**进程级 sys.argv**，所以这里把参数当 list
    直接传进 main(argv) —— 不去动 sys.argv，因为引擎线程还活着，碰它就是踩上一轮那个坑。
    """
    from hub.core import portal
    # 端口参数**按注册表生成**（第 100 轮）：原来只拼了 wb/qd 两个，
    # 于是门户侧 ports 里永远没有 trae —— 它的 /health 一路 None，前端当"引擎未起"。
    argv = ["--host", host, "--port", str(port)]
    for _name in ENGINES:
        # 只给**真的会启动**的引擎端口；没起的传 0，portal 会把它当「未配置」而不是去乱连
        argv += ["--%s-port" % _name, str(engine_ports.get(_name, 0))]
    argv += ["--admin-dir", admin_dir]
    # 告诉门户「你是被 hub 拉起的」—— 它就不打自己那套横幅（横幅只该出现一次）。
    os.environ["HUB_EMBEDDED"] = "1"
    thread = threading.Thread(target=portal.main, args=(argv,), daemon=True, name="portal")
    thread.start()
    return thread


def wait_portal(host, port, timeout=20.0):
    """等融合面的 /api/hub/health 起来；返回它的 JSON，超时返回 None。

    注意探的是 /api/hub/health 而不是 /health —— 融合面自己没有 /health
    （/api/* 之外都是静态站）。
    """
    import json
    import urllib.request
    deadline = time.time() + timeout
    url = "http://%s:%d/api/hub/health" % (host, port)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(0.3)
    return None


#: 面板依赖的融合端点（与前端 PANEL_CAPS / 接入说明页同一份清单）。
CAP_ENDPOINTS = ("hub/keys", "hub/selfcheck", "hub/try", "hub/accounts",
                 "hub/route-stats", "hub/rewards", "hub/scheduler")


def probe_caps(host, port, timeout=8.0):
    """逐个探融合端点，返回**确认缺失**（404）的列表。

    为什么要它在**启动横幅**里：面板是不是旧版，最该在"开完机那一眼"就知道，
    而不是等用户进浏览器、翻到某一页才发现少了几块。

    判据只认 404 —— 这才是"后端没有这个端点"。
      · 405 算"有"（端点存在但只收 POST，例如 /hub/try）
      · 5xx / 超时 / 连接失败**不算缺失**：那是"现在取不到"，不是"没有"。
        第 29 轮实测：hub/rewards 要聚合三家 /tasks，冷启动经常超过 2 秒，
        旧口径会把它误报成"缺 1 项"，害人去翻代码。
    """
    import urllib.error
    import urllib.request
    missing = []
    for name in CAP_ENDPOINTS:
        url = "http://%s:%d/api/%s" % (host, port, name)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                resp.read(64)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                missing.append(name)
        except Exception:  # noqa: BLE001 —— 取不到 ≠ 没有：不记缺失
            continue
    return missing


def parse_args(argv=None):
    # 引擎相关参数**按注册表生成**（第 100 轮）：加 Trae 时这里没有手写任何 --trae-port。
    ap = argparse.ArgumentParser(description="WorkBuddy x Qoder x Trae 三引擎网关中枢")
    ap.add_argument("--host", default="127.0.0.1")
    for _name in ENGINES:
        ap.add_argument("--%s-port" % _name, type=int, default=DEFAULT_PORTS[_name],
                        help="%s 引擎端口（默认 %d）" % (ENGINE_META[_name]["label"], DEFAULT_PORTS[_name]))
    ap.add_argument("--engines", default=",".join(ENGINES),
                    help="要启动的引擎，逗号分隔（%s；也可写 all）" % " / ".join(ENGINES))
    for _name in ENGINES:
        ap.add_argument("--%s-args" % _name, default="",
                        help="透传给 %s 引擎的额外参数" % ENGINE_META[_name]["label"])
    ap.add_argument("--portal-port", type=int, default=0,
                    help="同时起融合面（hub/core/portal.py）并监听该端口；"
                         "0（默认）或省略 = 不起。融合面把 admin 静态站与两引擎接口并到"
                         "一个端口上：/ 与 /admin/* 是静态，/api/hub/* 是聚合，"
                         "/api/{wb,qd}/* 与 /api/* 是反代")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要启动什么")
    return ap.parse_args(argv)


def main(argv=None):
    # stdout 改行缓冲：启动进度（"engine wb up" / "portal up"）是**给人也给判据看的**。
    # 非 TTY 时（重定向到文件或管道 —— 正是测试、Docker 日志、tee 的抓法）Python 默认
    # 块缓冲，这几行会卡在缓冲区里，于是「读到 xd up 才算我拉起来的」这类判据会偶发判红。
    # 实测：同一份代码，重定向后 30 秒仍读不到 "engine qd up"；加 -u 才出现。
    # bu-ding 的 E12c 归属链正是读启动日志，所以这里从源头修掉，而不是让判据去等更久。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # 日志统一：把引擎/门户/身份缓存的 [xxx] 前缀全部换成产品名（用户口径：日志要统一）
    try:
        if not isinstance(sys.stdout, _UnifiedLog):
            sys.stdout = _UnifiedLog(sys.stdout, HUB_NAME)
        if not isinstance(sys.stderr, _UnifiedLog):
            sys.stderr = _UnifiedLog(sys.stderr, HUB_NAME)
    except Exception:
        pass
    args = parse_args(argv)
    wanted = []
    for raw in str(args.engines).replace(";", ",").split(","):
        token = str(raw).strip().lower()
        if token in ("all", "*", "both"):
            # both 是历史写法（原来只有两家），现在等同全部
            names = list(ENGINES)
        else:
            one = resolve_engine(token)
            names = [one] if one else []
        for name in names:
            if name not in wanted:
                wanted.append(name)
    if not wanted:
        print("  [%s] no engine selected (use --engines wb,qd)" % HUB_NAME)
        return 2

    plan = []
    for name in wanted:
        if not engine_ready(name):
            print("  [%s] engine %s is not installed at %s - skipped" % (HUB_NAME, name, ENGINE_DIRS[name]))
            continue
        port = getattr(args, "%s_port" % name)
        extra = getattr(args, "%s_args" % name)
        # 数据目录走 hub/core/paths.py。
        # **必须是 argv，不能是环境变量**：两个引擎跑在同一个进程里，而它们认的**同名**
        # 环境变量是 ACCOUNTS_DIR —— 进程级 env 会被后设的那个覆盖，于是两个引擎指到
        # 同一份账号。argv 在 start_engine() 里是按引擎临时替换的（sys.argv = argv），
        # 天然隔离。顺序：paths 的 --accounts-dir/--usage-dir 在前，用户的
        # --wb-args/--qd-args 在后，所以显式传参的优先级仍然高于 HUB_DATA_ROOT。
        extra_args = paths.cli_args_for_engine(name) + (extra.split() if extra else [])
        plan.append((name, port, extra_args))

    print("  [%s] %s v%s" % (HUB_NAME, HUB_NAME, HUB_VERSION))
    # 每家引擎的**端口**也要打出来（第 183 轮）：原来 --dry-run 只列数据目录，
    # 想确认"到底会绑到哪个端口"只能去翻代码或真起一遍。一行一个，好扫也好被脚本断言。
    for _name, _port, _tail in plan:
        print("  [%s]   %-4s port %d" % (HUB_NAME, _name, _port))

    # 实际生效的数据目录。设了 HUB_DATA_ROOT 就从引擎目录搬出去，否则沿用引擎目录内。
    root = paths.data_root()
    # 未设根时把「会写到哪里」明说出来：本机直起 hub 会往引擎目录里落账号与用量文件，
    # 这是仓库被污染的最常见来源（布丁的 E11 冒烟守卫就是为它立的）。
    # 与其只在文档里写一句，不如在**启动那一刻**打在横幅上 —— 读文档的人远少于看启动日志的人。
    print("  [%s] data root: %s"
          % (HUB_NAME,
             "%s  (HUB_DATA_ROOT)" % root if root else
             "(unset) 各引擎目录内 —— 本机直起会写 hub/engines/<引擎>/{accounts,usage}；"
             "想写到别处：设 HUB_DATA_ROOT=/some/dir"))
    for name, _port, argv_tail in plan:
        derived = {"accounts": paths.accounts_dir(name), "usage": paths.usage_dir(name)}
        # 按**实际生效值**报告：用户可能在 --wb-args/--qd-args 里自己给了目录，那时赢的是他
        shown = {"accounts": effective_dir(argv_tail, "--accounts-dir", derived["accounts"]),
                 "usage": effective_dir(argv_tail, "--usage-dir", derived["usage"])}
        for label in ("accounts", "usage"):
            folder = shown[label]
            overridden = folder != derived[label]
            if root and not overridden:
                # 显式给了根、且这一项没被用户覆盖：先把目录建好，别等引擎启动时才创建。
                # 被覆盖的那一项不建 —— 那是用户指定的位置，来不来自他决定。
                try:
                    os.makedirs(folder, exist_ok=True)
                except OSError as exc:
                    print("  [%s]   %-3s %-8s %s   [创建失败: %s]" % (HUB_NAME, name, label, folder, exc))
                    continue
            mark = "" if os.path.isdir(folder) else "   [尚未创建]"
            if overridden:
                mark += "   [argv 覆盖]"
            print("  [%s]   %-3s %-8s %s%s" % (HUB_NAME, name, label, folder, mark))
    if args.portal_port:
        host_shown = args.host if args.host != "0.0.0.0" else "0.0.0.0"
        # 一个入口：面板与所有 API 都在这一个端口上（合并 key / 引擎 key / ?realm= 都走它）。
        print("  [%s] 唯一入口  http://%s:%d/   （面板 + /api/v1 + /api/hub/* 全在这一个端口）"
              % (HUB_NAME, host_shown, args.portal_port))
        print("  [%s] API       http://%s:%d/api/v1    (/v1 等价；OpenAI 兼容，也支持 /v1/messages)"
              % (HUB_NAME, host_shown, args.portal_port))
        print("  [%s]   · 合并 key    → 智能路由（国内优先，跨引擎回退）" % HUB_NAME)
        print("  [%s]   · 引擎 key    → 按 key 归属自动送进对应引擎" % HUB_NAME)
        print("  [%s]   · 指定出口    → 加 ?realm=cn / ?realm=intl 即可" % HUB_NAME)
        # 引擎端口是**内部**的：只绑回环、只给门户反代用，不需要用户记。
        internal = "/".join(str(p) for p in (args.wb_port, args.qd_port, args.trae_port) if p)
        print("  [%s] 内部引擎端口 %s（仅本机回环，门户反代用；浏览器打开会 302 回上面那个入口）"
              % (HUB_NAME, internal))
    else:
        print("  [%s] portal: off（加 --portal-port %d 一条命令起面板+合并 API）" % (HUB_NAME, HUB_PORT))
    if args.dry_run:
        return 0
    if not plan:
        print("  [%s] nothing to start" % HUB_NAME)
        return 2

    # 必须**串行**启动并逐个等 health：引擎的 main() 读的是进程级 sys.argv，
    # 两个线程同时进入 argparse 会互相盖掉参数（实测：wb 起来时拿到了 qd 的端口，
    # 两个引擎同时绑到 8802，8801 空着）。串行的代价只是几秒启动时间。
    # ★ 一个产品只留**一张脸**：引擎自己那套旧"用量看板"不再直接对外，
    #   根路径 302 到融合面。实现走环境变量（引擎不 import hub，保持通用）：
    #   没设 = 引擎照旧提供看板（单引擎独立跑时就是这个行为）。
    if args.portal_port:
        portal_host = "127.0.0.1" if args.host in ("0.0.0.0", "", None) else args.host
        os.environ["HUB_PORTAL_URL"] = "http://%s:%d/" % (portal_host, int(args.portal_port))

    ok = True
    hub_health = None
    # 第 232 轮：记录真正起来的引擎 —— 一家起不来不该让**整站**不启动。
    # 面板本身已经能表达"某家没数据"（partialNotice/降级横幅），
    # 所以只要还有一家活着，就把面板起出来让用户看见谁挂了、怎么救。
    started = []
    for name, port, extra in plan:
        if not port_free(args.host, port):
            ok = False
            print("  [%s] engine %s port :%d is already in use - another instance running?" % (HUB_NAME, name, port))
            print("        (stop it, or pass --%s-port <free port>)" % name)
            continue
        thread = start_engine(name, args.host, port, extra)
        health = wait_health(port, args.host, expect=name)
        # 线程存活 = 我们的引擎真起来了（绑失败会 SystemExit → 线程结束）；
        # 端口空闲预检后，第一个应答者必然是我们的引擎，双保险。
        if health is None or not thread.is_alive():
            ok = False
            print("  [%s] engine %s did NOT answer /health on :%d" % (HUB_NAME, name, port))
        else:
            started.append(name)
            print("  [%s] engine %s up  /health -> %s" % (HUB_NAME, name, health))
    if not ok:
        if not started:
            # 一家都没起来 → 没有意义再起面板，按失败退出（保持原行为）
            print("  [%s] startup incomplete; see engine output above" % HUB_NAME)
            return 1
        print("  [%s] WARNING: only %s started (%s failed) - the panel will show the missing ones as down"
              % (HUB_NAME, ", ".join(started), ", ".join(n for n, _p, _e in plan if n not in started)))

    # 融合面：**在所有引擎都起来之后**再起（它自己会去 /api/hub/* 聚合两边的接口，
    # 提前起也能用，但那时 /api/hub/health 会报引擎为 None，起完再报更真实）。
    if args.portal_port:
        engine_ports = {}
        for name, port, _extra in plan:
            engine_ports[name] = port
        start_portal(args.host, args.portal_port, engine_ports, paths.admin_dir())
        hub_health = wait_portal(args.host, args.portal_port)
        if hub_health is None:
            ok = False
            print("  [%s] portal did NOT answer /api/hub/health on :%d" % (HUB_NAME, args.portal_port))
        else:
            up = [e for e, h in (hub_health.get("engines") or {}).items()
                  if isinstance(h, dict) and h.get("ok") is True]
            print("  [%s] portal up  http://%s:%d/   engines up: %s  (%s %s)"
                  % (HUB_NAME, args.host, args.portal_port, ",".join(up) or "-",
                     HUB_NAME, hub_health.get("version")))
            # 面板 ↔ 后端 一致性：把"缺哪块"在开完机那一眼说清（与浏览器里那几处同源）。
            missing = probe_caps(args.host, args.portal_port)
            if missing:
                print("  [%s] 融合端点: 缺 %d 项 → %s"
                      % (HUB_NAME, len(missing), ", ".join(missing)))
                print("        （面板比后端旧？重启后仍未齐 = 代码没更新全；"
                      "浏览器里「接入说明 → 能力自检」有逐项说明）")
            else:
                print("  [%s] 融合端点: %d 项全在（面板与后端一致）" % (HUB_NAME, len(CAP_ENDPOINTS)))
    if not ok:
        # 第 232 轮：这里的 ok 记录的是"有引擎没起来"。只要面板本身活着、
        # 且至少一家引擎在跑，就继续提供（面板会把缺的那家标成 down）——
        # 一家端口被占就让整站不可用，代价太大。
        portal_ok = hub_health is not None if args.portal_port else True
        if not portal_ok or not started:
            print("  [%s] startup incomplete; see output above" % HUB_NAME)
            return 1
        print("  [%s] 部分引擎未启动（%s），面板已起，缺失项会显示为 down"
              % (HUB_NAME, ", ".join(n for n, _p, _e in plan if n not in started)))
    else:
        print("  [%s] all up. Ctrl+C to stop." % HUB_NAME)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n  [%s] bye" % HUB_NAME)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
