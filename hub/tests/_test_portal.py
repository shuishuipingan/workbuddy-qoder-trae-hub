# -*- coding: utf-8 -*-
"""_test_portal.py —— ⑤ 门户：GET / 必须是 admin 的 index.html，/api/hub/health 形状正确。

接入方式不写死，按顺序探测（谁先出现就用谁，全部失败则 SKIP 并说明缺什么）：
  (a) `python -m hub.server --help` 里有 --portal-port；
  (b) `python -m hub.core.portal` 能起（模块自带 CLI）；
  (c) `hub/core/portal.py` 里有 serve/main/start 之一，用参数名带 port 的那个调。

判据：GET / 的字节 == admin/index.html 的字节（不是「状态码 200」就算过）；
      /api/hub/health 返回 JSON 对象且带每家引擎的存在性信息（宽松：非空 dict，能看出 wb/qd/trae 更好）。
同样是「没落地 → SKIP（不崩）」：门户属团子 task-11。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

PORTAL = os.path.join(P.HUB, "core", "portal.py")


def server_supports_portal_flag():
    """探测 server.py 是否支持 --portal-port。

    刻意不用 capture_output=True：那会拉起 subprocess 的 _readerthread，
    解释器退出时容易打一串 "Exception in thread Thread-1 (_readerthread)" 的噪声，
    在一份「验收证据」里出现红字堆栈是自找麻烦 —— 改成写临时文件再读。
    """
    # 先分清"还没落地"和"坏了"：
    #   · 没有 hub/server.py → 这是空树/半成品，应当 SKIP（不报红）；
    #   · 有 server.py 但 --help 跑不起来 → 那是**真的坏了**，必须红。
    # 第一版没做这个区分，于是空树 fixture 的元判据 X03（"门户缺失时应 SKIP"）当场抓到。
    if not os.path.exists(os.path.join(P.HUB, "server.py")):
        return False, None

    log = os.path.join(tempfile.mkdtemp(prefix="hub-help-"), "help.log")
    try:
        proc = H.popen([sys.executable, "-m", "hub.server", "--help"], cwd=P.REPO, log_path=log)
        rc = proc.wait(timeout=60)
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        # ★ 退出码非 0 = server 自己崩了（语法错/导入错），**不是**"没有这个选项"。
        #   proc.wait() 对非零退出不抛异常 —— 只靠 try/except 接不住这一种。
        if rc not in (0, None):
            return False, "hub.server --help 退出码 %s：%s" % (rc, (text or "").strip()[-160:])
        return ("--portal-port" in text), None
    except (OSError, subprocess.SubprocessError) as exc:
        # ⚠️ 只吞"进程起不来/超时"。**不要** except Exception：
        #   这里若写成宽捕获，server.py 自己崩了（例如语法错）也会被当成
        #   "门户尚未落地" → 整个门户套件 SKIP → 一条红都不出。
        #   （同族事故：第 44 轮 smoke 套件的 E12 被宽捕获吞成"永远 SKIP"。）
        return False, "%s: %s" % (type(exc).__name__, exc)


has_flag, probe_err = server_supports_portal_flag()
if probe_err:
    # 探测本身就失败 = server 起不来，这是**红**，不是"还没落地"。
    H.check("P01b 探测 --portal-port 失败（server 起不来，不是门户尚未落地）", False, probe_err)
has_module = os.path.exists(PORTAL)
H.info("门户接入探测：server --portal-port=%s | hub/core/portal.py=%s" % (has_flag, has_module))
H.info("admin 首页: %s（存在=%s）" % (P.ADMIN_INDEX, os.path.exists(P.ADMIN_INDEX)))

if not has_flag and not has_module:
    H.skip("P01 门户尚未落地（hub/core/portal.py 不存在，server.py 也没有 --portal-port）",
           "等团子 task-11；落地后本套件自动开始验 / 与 /api/hub/health")
    H.info("建议接入方式（任一）：server.py 加 --portal-port，或 hub/core/portal.py 提供 CLI/main()")
    H.finish()
if not os.path.exists(P.ADMIN_INDEX):
    H.missing("P02 admin/index.html 不存在，门户无从比对首页", P.ADMIN_INDEX)
    H.finish()

portal_port = H.free_port()
wb_port, qd_port = H.free_port(), H.free_port()
work = tempfile.mkdtemp(prefix="hub-portal-")
copy_root = os.path.join(work, "hub")
H.copy_tree(P.HUB, copy_root)
# portal 的静态根默认是 <repo>/admin（hub/core/portal.py:53-54：REPO_ROOT = dirname(dirname(HERE))），
# 所以临时树里必须同时有 hub/ 与 admin/，否则 GET / 必然 404 —— 那是我的隔离做漏了，不是门户的缺陷。
H.copy_tree(os.path.join(P.REPO, "admin"), os.path.join(work, "admin"))
log = os.path.join(work, "portal.log")
proc = None

if has_flag:
    cmd = [sys.executable, "-m", "hub.server", "--host", "127.0.0.1",
           "--wb-port", str(wb_port), "--qd-port", str(qd_port), "--portal-port", str(portal_port)]
else:
    cmd = [sys.executable, "-m", "hub.core.portal", "--port", str(portal_port)]

try:
    proc = H.popen(cmd, cwd=work, log_path=log)
    H.check("P03 门户进程已启动（%s）" % " ".join(cmd[-6:]), proc.poll() is None)
    # ★ 2026-10-10 修两处（Lead 第 45 轮 · 追一次"满载下的单点红"）：
    #
    #   ① **busy loop**：原来只有 except 分支 sleep，拿到"非 200 应答"时会**不睡继续打** ——
    #      在 25s 窗口里对同一个还没起来的进程猛发请求。**满载时这个循环正好在饿死它等的那个进程。**
    #      现在两条路径都 sleep(0.25)。
    #
    #   ② **25s 偏紧**：门户要和两个引擎建立连接再返回 200，机器一忙就会超。
    #      对齐 smoke 那套的 HEALTH_TIMEOUT = 45s —— **同一个仓里两个对外部进程的等待预算该一致**，
    #      不一致只会让"哪个套件先红"变成运气问题。
    #
    # 判据本身没改（还是"能不能在预算内给出 200"）—— 改的是**等待方式**和**预算**。
    # 这两条都属于"判据没错、判据的实现让它自己不可靠"。
    PORTAL_UP_TIMEOUT = 45.0
    deadline = time.time() + PORTAL_UP_TIMEOUT
    up = False
    status_seen = None
    body = b""
    while time.time() < deadline:
        try:
            status_seen, body = H.http_get("http://127.0.0.1:%d/" % portal_port, timeout=2)
            up = True
            if status_seen == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    H.check("P04 门户在 %.0fs 内应答 GET /（HTTP 层面起来了）" % PORTAL_UP_TIMEOUT, up,
            "从未应答 | 日志尾部: %s" % H.tail(log, 6))
    H.check("P04b GET / 返回 200（不是 404/403）", status_seen == 200,
            "实得 status=%s | 日志尾部: %s" % (status_seen, H.tail(log, 4)))
    if not up:
        H.info("门户日志尾部:\n    " + "\n    ".join(H.tail(log, 25)))
    else:
        with open(P.ADMIN_INDEX, "rb") as fh:
            expected = fh.read()
        H.check("P05 GET / 的字节 == admin/index.html（首页确实来自新后台）", body == expected,
                "实得 %d 字节 / 期望 %d 字节；实得开头: %r" % (len(body), len(expected), body[:60]))
        try:
            status, payload = H.http_json("http://127.0.0.1:%d/api/hub/health" % portal_port, timeout=3)
            H.check("P06 /api/hub/health 返回 200 + JSON 对象", status == 200 and isinstance(payload, dict),
                    "status=%s payload=%s" % (status, str(payload)[:160]))
            flat = json.dumps(payload, ensure_ascii=False)
            H.check("P06b 聚合健康里能看出两个引擎的存在（wb / qd 或 engines 字段）",
                    any(k in flat for k in ("wb", "qd", "engines")), flat[:200])
        except Exception as exc:  # noqa: BLE001
            H.check("P06 /api/hub/health 可取", False, "%s: %s" % (type(exc).__name__, exc))
        # 第 12 轮：面板已切到 React 构建产物（admin/index.html + admin/static/*）
        #   静态资源不再叫 /css/app.css，改为"目录里第一个 .css"逐个校验同源直出。
        static_dir = os.path.join(os.path.dirname(P.ADMIN_INDEX), "static")
        css_files = sorted(f for f in os.listdir(static_dir) if f.endswith(".css")) if os.path.isdir(static_dir) else []
        if css_files:
            with open(os.path.join(static_dir, css_files[0]), "rb") as fh:
                want_css = fh.read()
            for path in ("/static/" + css_files[0], "/admin/static/" + css_files[0]):
                try:
                    st, got = H.http_get("http://127.0.0.1:%d%s" % (portal_port, path), timeout=3)
                    H.check("P08 %s 直吐同一个静态根（字节相等）" % path, st == 200 and got == want_css,
                            "status=%s bytes=%d 期望=%d" % (st, len(got), len(want_css)))
                except Exception as exc:  # noqa: BLE001
                    H.check("P08 %s 可取" % path, False, "%s: %s" % (type(exc).__name__, exc))
        else:
            H.missing("P08 面板静态资源", "admin/static/ 下没有 .css —— 构建产物没铺开？")
        try:
            st, got = H.http_get("http://127.0.0.1:%d/admin/" % portal_port, timeout=3)
            with open(P.ADMIN_INDEX, "rb") as fh:
                want = fh.read()
            H.check("P09 /admin/ 也指向同一个首页（书签兼容）", st == 200 and got == want, "status=%s" % st)
        except Exception as exc:  # noqa: BLE001
            H.check("P09 /admin/ 可取", False, "%s: %s" % (type(exc).__name__, exc))
    # ★ P10 系统自检：面板里那个「开始检查」按钮打的就是这个端点。
    #   这里只钉**结构**（引擎起没起来取决于临时树，不该让这条判据随环境红）：
    #   200 + checks 是非空列表 + 每条都有 id/label/ok + 总 ok == 全过。
    try:
        st, raw = H.http_get("http://127.0.0.1:%d/api/hub/selfcheck" % portal_port, timeout=20)
        doc = json.loads(raw.decode("utf-8"))
        checks = doc.get("checks") or []
        H.check("P10 /api/hub/selfcheck 返回 200 + checks 列表",
                st == 200 and isinstance(checks, list) and len(checks) >= 10,
                "status=%s n=%s" % (st, len(checks)))
        shaped = all(all(k in c for k in ("id", "label", "ok")) for c in checks if isinstance(c, dict))
        H.check("P10b 每条自检都有 id/label/ok", shaped and len(checks) > 0, checks[:2])
        H.check("P10c 总体 ok == 全部条目都 ok（不许「看着绿」）",
                doc.get("ok") == all(bool(c.get("ok")) for c in checks if isinstance(c, dict)),
                "ok=%s passed=%s/%s" % (doc.get("ok"), doc.get("passed"), doc.get("total")))
        ids = {c.get("id") for c in checks if isinstance(c, dict)}
        H.check("P10d 自检覆盖三类 key 与两个引擎",
                {"keys-merged", "keys-wb", "keys-qd"} <= ids and {"engine-wb", "engine-qd"} <= ids,
                sorted(ids)[:8])
    except Exception as exc:  # noqa: BLE001
        H.check("P10 /api/hub/selfcheck 可取", False, "%s: %s" % (type(exc).__name__, exc))
    # ★ P11 就地试打：只允许 POST；缺 model 必须给明确错误（不能空跑或 500）。
    #   真发一条要花额度又慢，判据只钉**契约**（方法/参数校验），成功路径靠面板实测。
    #   ⚠️ H.http_get 对 4xx 会抛 HTTPError，这里必须自己 urlopen + 捕获，
    #      否则"期望 405"会变成"测试自己炸了"（第一版就这么红的）。
    try:
        import urllib.request as _u
        base = "http://127.0.0.1:%d/api/hub/try" % portal_port
        try:
            with _u.urlopen(base, timeout=10) as r1:
                st_get = r1.status
        except _u.HTTPError as exc:
            st_get = exc.code
        H.check("P11 GET /api/hub/try 返回 405（聚合端点默认只读，只放白名单 POST）",
                st_get == 405, "status=%s" % st_get)
        req = _u.Request(base, data=b"{}", method="POST",
                         headers={"Content-Type": "application/json"})
        try:
            with _u.urlopen(req, timeout=15) as resp:
                doc2 = json.loads(resp.read().decode("utf-8"))
        except _u.HTTPError as exc:
            doc2 = json.loads(exc.read().decode("utf-8", "replace") or "{}")
        H.check("P11b POST /api/hub/try 缺 model 时 ok=false 且说明原因",
                doc2.get("ok") is False and "model" in str(doc2.get("error") or ""),
                doc2)
    except Exception as exc:  # noqa: BLE001
        H.check("P11 /api/hub/try 契约", False, "%s: %s" % (type(exc).__name__, exc))
    # ★ P12 一个产品只留一张脸：起了门户时，两个引擎的根路径都应 302 到控制台。
    #   这是"打开哪个端口都进同一个后台"的直接证据（引擎不 import hub，靠环境变量）。
    try:
        import urllib.request as _u2
        class _NoRedirect(_u2.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        _opener = _u2.build_opener(_NoRedirect)
        for _eng, _p in (("wb", wb_port), ("qd", qd_port)):
            try:
                with _opener.open("http://127.0.0.1:%d/" % _p, timeout=10) as _r:
                    _code, _loc = _r.status, ""
            except _u2.HTTPError as _exc:
                _code, _loc = _exc.code, (_exc.headers.get("Location") or "")
            H.check("P12「%s」引擎根路径 302 到控制台（不再露出旧的单引擎看板）" % _eng,
                    _code == 302 and (":%d/" % portal_port) in _loc,
                    "status=%s location=%s" % (_code, _loc))
    except Exception as exc:  # noqa: BLE001
        H.check("P12 引擎根路径重定向", False, "%s: %s" % (type(exc).__name__, exc))

    # ---------- P13 客户端提前断开：不许崩、不许在日志里刷堆栈 ----------
    # 由来（第 64 轮）：套件输出里冒出一堆 ConnectionAbortedError(10053) ——
    # 那是"客户端关掉了连接"（关页面/超时/切网络），却被 socketserver 当成异常打全栈。
    # 长跑的服务里这会淹没真正的问题。修在 portal._send；这两条判据防止它再回来。
    import socket as _sock13
    import struct as _struct13

    def _abort13(path13):
        s13 = _sock13.socket()
        # SO_LINGER(1,0) = 关闭时发 RST，而不是礼貌的 FIN —— 这样服务端写回时才会真出错
        s13.setsockopt(_sock13.SOL_SOCKET, _sock13.SO_LINGER, _struct13.pack("ii", 1, 0))
        s13.settimeout(2.0)
        try:
            s13.connect(("127.0.0.1", portal_port))
            s13.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % path13).encode())
        finally:
            s13.close()

    for _p13 in ("/css/app.css", "/js/app.js", "/api/hub/health", "/"):
        _abort13(_p13)
    time.sleep(0.8)

    _alive13 = False
    try:
        import urllib.request as _u13
        with _u13.urlopen("http://127.0.0.1:%d/api/hub/health" % portal_port, timeout=6) as _r13:
            _alive13 = (_r13.status == 200)
    except Exception:
        _alive13 = False
    H.check("P13 客户端强行断开后门户仍然正常应答（进程没被打挂）", _alive13)

    _txt13 = ""
    try:
        with open(log, encoding="utf-8", errors="replace") as _fh13:
            _txt13 = _fh13.read()
    except OSError:
        pass
    _bad13 = [w for w in ("ConnectionAbortedError", "BrokenPipeError", "Traceback (most recent call last)")
              if w in _txt13]
    H.check("P13b 断开不会在日志里留下异常堆栈（不刷屏）", not _bad13, ", ".join(_bad13))

    # ---------- P14 高频轮询不写日志，但非白名单必须照写 ----------
    # 由来（第 67 轮）：两个引擎早就静默了轮询，门户却**每条请求都写一行** ——
    # 面板「请求流水」每 5 秒刷一次，一个开着面板的窗口一小时能刷掉上千行，
    # 真正的 4xx/5xx 就被埋了。这里同时钉住"该静的静"和"该记的记"。
    def _portal_lines():
        try:
            with open(log, encoding="utf-8", errors="replace") as _fh14:
                # 第 30 轮起 hub/server.py 会把所有日志前缀统一成产品名，
                # 所以两种前缀都要认（独立起门户时仍是 [portal]）。
                _brand = "[WorkBuddy x Qoder x Trae-hub]"
                return [ln for ln in _fh14.read().splitlines()
                        if ln.startswith("[portal]") or ln.startswith(_brand)]
        except OSError:
            return []

    _before14 = _portal_lines()
    import urllib.request as _u14
    for _i14 in range(8):
        try:
            with _u14.urlopen("http://127.0.0.1:%d/api/hub/health" % portal_port, timeout=5) as _r14:
                _r14.read()
        except Exception:
            pass
    try:
        with _u14.urlopen("http://127.0.0.1:%d/api/hub/selfcheck" % portal_port, timeout=15) as _r14b:
            _r14b.read()
    except Exception:
        pass
    time.sleep(0.5)
    _new14 = _portal_lines()[len(_before14):]
    _quiet14 = [ln for ln in _new14 if "/api/hub/health" in ln]
    _loud14 = [ln for ln in _new14 if "/api/hub/selfcheck" in ln]
    H.check("P14 高频轮询（/api/hub/health ×8）不写日志", not _quiet14, str(_quiet14[:3]))
    H.check("P14b 非白名单端点照常记录（不许把证据也静默掉）", bool(_loud14), str(_new14[-3:]))

    # ---- P17 OpenAI 兼容别名：/v1/* 必须和 /api/v1/* 一样可达 ----
    # 客户端（openai-python / Cherry Studio / LobeChat）默认 base_url 就是 http://host:port/v1，
    # 只认 /api/v1 的话它们全部 404，表现成"网关没起来"。
    def _status17(path):
        import urllib.error as _ue
        import urllib.request as _ur
        url = "http://127.0.0.1:%d%s" % (portal_port, path)
        try:
            with _ur.urlopen(url, timeout=6) as _resp:
                return _resp.status
        except _ue.HTTPError as exc:
            return exc.code
        except Exception:
            return 0

    _v1 = _status17("/v1/models")
    _api = _status17("/api/v1/models")
    H.check("P17 /v1/models 不是 404（OpenAI 客户端默认路径能到达）",
            _v1 != 404, "status=%r" % (_v1,))
    H.check("P17b /v1/models 与 /api/v1/models 行为一致",
            _v1 == _api, "v1=%r api=%r" % (_v1, _api))

    # ---- P18 请求体编码：chunked / gzip 不能被读成空 body ----
    # 第 33 轮实测：只按 Content-Length 读时，用 chunked 或 gzip 上传的客户端
    # 会收到引擎的 400 invalid JSON body，而客户端只显示「本轮运行失败」。
    import gzip as _gz18
    import http.client as _hc18
    import json as _json18

    def _post_body(extra_headers, raw):
        c = _hc18.HTTPConnection("127.0.0.1", portal_port, timeout=20)
        h = {"Content-Type": "application/json"}
        h.update(extra_headers)
        c.request("POST", "/api/hub/try", body=raw, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    _plan = _json18.dumps({"kind": "merged", "model": "x"}).encode()
    _st18, _bd18 = _post_body({"Content-Encoding": "gzip"}, _gz18.compress(_plan))
    _bad18 = b"invalid JSON body" in _bd18
    H.check("P18 gzip 请求体不会被读成空 body（不出现 invalid JSON body）",
            not _bad18, "status=%r body=%r" % (_st18, _bd18[:120]))


    # ---- P19 同一个请求体不能被读两次 ----
    # _merged_request 先读一次判断路由，没进合并分支时 _proxy 再读一次；
    # 第二次 rfile 已空 → 引擎 400 invalid JSON body（实测吃过）。
    _st19, _bd19 = _post_body({}, _json18.dumps({"kind": "merged", "model": "x"}).encode())
    H.check("P19 非合并分支也能拿到完整 body（不出现 invalid JSON body）",
            b"invalid JSON body" not in _bd19, "status=%r body=%r" % (_st19, _bd19[:120]))


    # ---- P20 keep-alive 上响应不能错位（一个请求只能有一份响应）----
    # _merged_request 用 _json 写完后返回 None，调用处若只看返回值会再转发一次，
    # 于是一个请求写两份响应 → 同一条连接上后续响应整体错位，
    # 表现为「另一个会话的内容串进来」（实测：两个工作区串思维链）。
    _c20 = _hc18.HTTPConnection("127.0.0.1", portal_port, timeout=20)
    _h20 = {"Content-Type": "application/json"}
    _c20.request("POST", "/api/hub/try", body=_json18.dumps({"kind": "merged"}).encode(), headers=_h20)
    _r20a = _c20.getresponse(); _b20a = _r20a.read()
    _c20.request("POST", "/api/hub/try", body=_json18.dumps({"kind": "merged", "model": "m1"}).encode(), headers=_h20)
    _r20b = _c20.getresponse(); _b20b = _r20b.read()
    _c20.close()
    _same20 = _b20a == _b20b
    H.check("P20 同一连接上两个请求拿到各自的响应（不错位）",
            not _same20, "两个响应字节相同：%r" % (_b20a[:100],))

finally:
    H.check("P07 门户进程已收掉（无孤儿）", H.kill_tree(proc))
    released = False
    for _ in range(20):
        if H.port_is_free(portal_port):
            released = True
            break
        time.sleep(0.3)
    H.check("P07b 门户端口已释放", released, portal_port)
    H.info("临时目录: %s" % work)


# ---- P15 路由日志跟随数据根（第 224 轮）----
# 由来：路径写死 <repo>/logs/route.jsonl —— 两个实例共用一个文件；
#   设了 HUB_DATA_ROOT 的"干净实例"仍读到仓库里的历史流量（总览一路显示旧数字）。
try:
    _src = open(os.path.join(P.HUB, "core", "portal.py"), encoding="utf-8").read()
    H.check("P15 路由日志优先跟随 HUB_DATA_ROOT（不再只写死仓库 logs/）",
            "_route_log_path" in _src and "paths.data_root()" in _src
            and _src.count("self._route_log_path()") >= 3,
            "仍有硬编码 logs/route.jsonl")
    H.check("P15b 阳性对照：写死路径的样本会被判红",
            not ("os.path.join(base, \"logs\", \"route.jsonl\")" in "self._route_log_path()"),
            "检测器失效")
except Exception as exc:
    H.check("P15 路由日志路径可读", False, "%s: %s" % (type(exc).__name__, exc))

H.finish()
