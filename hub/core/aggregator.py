# -*- coding: utf-8 -*-
"""hub/core/aggregator.py —— 融合层的聚合器（**纯逻辑，不启任何服务**）。

职责：把「同进程双端口」的两个引擎，聚合成一个可被 portal 直接消费的数据面。

设计约束（见 docs/merge/01-后端融合总纲.md / 00-后台Web合并方案.md）：

  * 只用标准库；**不 import 任何引擎模块**——两个引擎的状态一律通过它们各自的
    HTTP 端口取，这样聚合器与引擎的进程/线程模型完全解耦，也能单独指向别处的实例。
  * 每个函数都**不抛异常**：引擎没起、端口写错、超时、回包不是 JSON——
    一律把该引擎的值记成 None。调用方（portal）据此渲染「缺失」而不是 0 或白屏。
  * **字段缺失按 None，绝不补 0**：这是本仓的读端铁律（见 docs/merge/02-接口实测样本.md
    第三节）。空态下 /usage/perf 的指标本来就是 null，聚合层再把它当 0 会凭空造出
    「零消耗」这种不存在的事实。

鉴权口径（**实测确认，不是猜的**）：

  * 两个引擎的管理接口用**自持令牌**：POST /panel/login {password} → {ok, token}，
    之后每个请求带 X-Panel-Token（wb_proxy.py:7448-7456 _panel_token；qoder_proxy.py:5734-5743）。
  * /usage 与 /v1/usage 在**两边都是 panel 路由**（wb_proxy.py:7458-7466、
    qoder_proxy.py:5746-5756），必须先过 _panel_ok()，否则 401。
  * /v1/models 不在 panel 路由里，但 _key_ok() 的第一句就是
    「if self._panel_ok(): return True」（wb_proxy.py:7354-7359、qoder_proxy.py:5638-5648），
    所以**一个面板令牌同时覆盖 /health、/usage、/v1/models 三类取数**。
  * 两个引擎的密码**可能不同**，所以 passwords 支持按引擎给；缺省回落到
    DEFAULT_PASSWORDS（两个引擎的 DEFAULT_PANEL_PASSWORD 都是 "admin"，
    wb_settings.py:20 / qoder_settings.py:21），也可用 HUB_WB_PASSWORD / HUB_QD_PASSWORD 覆盖。

自检（需要两个引擎已经在跑）：

    python -m hub.core.aggregator --wb-port 8801 --qd-port 8802
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

if __package__ in (None, ""):                       # 直跑本文件时的兜底
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from hub import HUB_VERSION, ENGINE_VERSIONS
else:
    from .. import HUB_VERSION, ENGINE_VERSIONS

#: 引擎 id（顺序即输出顺序；与 hub/__init__.py 的 ENGINE_ALIASES 规范名一致）
# 引擎名单**只从注册表来**（第 100 轮）：加减引擎不再改这一行。
from hub import ENGINES  # noqa: E402

#: 单次 HTTP 超时（秒）。契约要求 2s —— 聚合层是「展示面」，宁可快失败也不要挂住页面。
HTTP_TIMEOUT = float(os.environ.get("HUB_HTTP_TIMEOUT") or 2.0)

#: 引擎主机。同进程双端口场景恒为回环；留成常量便于将来指向远端实例。
ENGINE_HOST = os.environ.get("HUB_ENGINE_HOST") or "127.0.0.1"

#: 缺省面板密码：两个引擎的 DEFAULT_PANEL_PASSWORD（见模块 docstring）
# ★ 按注册表生成（第 148 轮）：原来只有 wb/qd —— trae 没有默认口令，
#   门户侧就无法替它取数（/api/hub/* 里 trae 的分项会整段缺失）。
DEFAULT_PASSWORDS = {engine: "admin" for engine in ENGINES}

#: 令牌缓存：engine -> {"token": str, "port": int, "at": float}
_TOKENS = {}
_TOKEN_LOCK = threading.Lock()
#: 每引擎一把登录锁：避免并发首访时打出多个 /panel/login
_LOGIN_LOCKS = {name: threading.Lock() for name in ENGINES}


# --------------------------------------------------------------------------- 内部工具
def _port_of(ports, engine):
    """该引擎的端口；未配置/非法 → None。"""
    try:
        value = (ports or {}).get(engine)
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _configured(ports):
    return [name for name in ENGINES if _port_of(ports, name) is not None]


def _password_for(engine, passwords):
    """按引擎取面板密码：显式参数 > 环境变量 > 引擎默认。"""
    if isinstance(passwords, dict) and passwords.get(engine) is not None:
        return str(passwords.get(engine))
    env = os.environ.get("HUB_%s_PASSWORD" % engine.upper())
    if env:
        return env
    return str(DEFAULT_PASSWORDS.get(engine, "admin"))


def _request(engine, ports, path, method="GET", params=None, body=None,
             headers=None, timeout=None, token=True, passwords=None, _retried=False):
    """向某个引擎发一次请求，返回 (status, headers, raw_bytes)。

    网络层失败一律**抛**（由调用方决定是记 None 还是重试）；HTTP 状态码**返回不抛**。
    token=True 时自动带上该引擎缓存/新取的 X-Panel-Token。

    ★ 401/403 会**丢掉缓存令牌并重试一次**：
      缓存里的 X-Panel-Token 在"面板密码被改""引擎重启"之后会失效，
      而这不该表现成"整个面板一片不可用" —— 用户只是改了密码而已。
      （forget_token 一直存在却没人调用，等于这条自愈能力从没生效过；第 59 轮接上。）
    """
    port = _port_of(ports, engine)
    if port is None:
        raise ValueError("engine %s has no port configured" % engine)
    url = "http://%s:%d%s" % (ENGINE_HOST, port, path)
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
        if query:
            url += ("&" if "?" in url else "?") + query
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
    req_headers = dict(headers or {})
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req_headers.setdefault("Accept", "application/json")
    if token:
        tok = ensure_token(engine, ports, password=_password_for(engine, passwords))
        if tok:
            req_headers["X-Panel-Token"] = tok
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        if token and exc.code in (401, 403) and not _retried:
            forget_token(engine)                    # 缓存的令牌作废，下面重取一次
            return _request(engine, ports, path, method=method, params=params, body=body,
                            headers=headers, timeout=timeout, token=token,
                            passwords=passwords, _retried=True)
        # 其余状态码照实返回：调用方本来就在判 status != 200
        try:
            payload = exc.read()
        except Exception:  # noqa: BLE001
            payload = b""
        return exc.code, dict((exc.headers or {}).items()), payload


def _json_or_none(engine, ports, path, **kw):
    """取一个 JSON 端点；任何失败（含 401/超时/坏 JSON）→ None。"""
    try:
        status, _headers, raw = _request(engine, ports, path, **kw)
        if status != 200:
            return None
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def login(engine, ports, password=None, timeout=None):
    """POST /panel/login，返回 token；失败返回 None（不抛）。"""
    pw = _password_for(engine, {engine: password} if password is not None else None)
    try:
        status, _headers, raw = _request(
            engine, ports, "/panel/login", method="POST",
            body={"password": pw}, token=False, timeout=timeout)
        if status != 200:
            return None
        doc = json.loads(raw.decode("utf-8", "replace"))
        token = str((doc or {}).get("token") or "").strip()
        return token or None
    except Exception:
        return None


def ensure_token(engine, ports, password=None, timeout=None, force=False):
    """拿到（并缓存）该引擎的面板令牌。

    缓存键里带上端口：同一个 engine id 换了端口（例如重新起实例）时旧令牌自动失效。
    """
    port = _port_of(ports, engine)
    if port is None:
        return None
    with _TOKEN_LOCK:
        hit = _TOKENS.get(engine)
        if (not force) and hit and hit.get("token") and hit.get("port") == port:
            return hit["token"]
    lock = _LOGIN_LOCKS.setdefault(engine, threading.Lock())
    with lock:                                   # 只让一个线程去打登录
        with _TOKEN_LOCK:
            hit = _TOKENS.get(engine)
            if (not force) and hit and hit.get("token") and hit.get("port") == port:
                return hit["token"]
        token = login(engine, ports, password=password, timeout=timeout)
        with _TOKEN_LOCK:
            if token:
                _TOKENS[engine] = {"token": token, "port": port, "at": time.time()}
            else:
                _TOKENS.pop(engine, None)
        return token


def forget_token(engine=None):
    """丢掉令牌缓存（换密码、引擎重启后调用）。engine=None 清全部。"""
    with _TOKEN_LOCK:
        if engine is None:
            _TOKENS.clear()
        else:
            _TOKENS.pop(engine, None)


def _sum_known(values):
    """只把**已知**的值相加：全未知 → None。

    这是「缺失按 None 不要当 0」在聚合上的落点：一个引擎没起，不该把总量算成 0，
    也不该把它的缺失当成「消耗为零」摊进合计。
    """
    known = [v for v in values if v is not None]
    if not known:
        return None
    total = 0
    for v in known:
        try:
            total += float(v)
        except (TypeError, ValueError):
            return None                      # 有值但不是数：宁可说不知道
    if all(isinstance(v, int) for v in known) and float(total).is_integer():
        return int(total)
    return total


# --------------------------------------------------------------------------- 契约 API
def engine_health(ports):
    """{'wb': {...}|None, 'qd': {...}|None} —— 各自 HTTP 打 /health。

    /health 两边都免鉴权（wb_proxy.py:7557、qoder_proxy.py:5828），所以这里不带令牌。
    超时/未配置/非 200/坏 JSON 一律 None，**不抛**。
    """
    out = {}
    for engine in ENGINES:
        out[engine] = _json_or_none(engine, ports, "/health", token=False)
    return out


def hub_health(ports):
    """{'ok': bool, 'version': HUB_VERSION, 'engines': {engine: health|None}}。

    ok 的判据：**至少配置了一个引擎**，并且**每个已配置的引擎都 ok=true**。
    未配置的引擎不进判据（只起单引擎的部署同样算健康）；它们的值是 None。
    """
    engines = engine_health(ports)
    # ★ 版本补全：wb 的 /health 不报 version（qoder 报 1.4.1）。
    #   前端脚注要显示"这个产品由什么版本组成"，缺一个引擎的版本就显示不全 ——
    #   用融合层自带的 ENGINE_VERSIONS 补（它就是我们随包发布的引擎代码版本）。
    try:
        from hub import ENGINE_VERSIONS as _EV
    except Exception:
        try:
            from .. import ENGINE_VERSIONS as _EV
        except Exception:
            _EV = {}
    for name, info in list(engines.items()):
        if not isinstance(info, dict):
            continue
        shipped = _EV.get(name)
        reported = info.get("version")
        if shipped:
            # 以**随包版本**为准：引擎里那个版本常量经常落后于它的 release tag
            # （实测 trae 代码是 v1.1.5、自报 1.0.0），面板要显示的是"我们发布了什么"。
            if reported and str(reported) != str(shipped):
                info["version_reported"] = reported
            info["version"] = shipped
    configured = _configured(ports)
    ok = bool(configured) and all(
        isinstance(engines.get(name), dict) and engines[name].get("ok") is True
        for name in configured)
    # 每个引擎加一个 configured：没配端口的引擎（--engines wb）与"挂了"不是一回事，
    # 面板据此决定该不该报"取数失败"（第 90 轮）。
    _conf = set(_configured(ports))
    for _name, _info in list(engines.items()):
        if isinstance(_info, dict):
            _info["configured"] = _name in _conf
    return {"ok": ok, "version": HUB_VERSION, "engines": engines}


def hub_usage(ports, realm=None, range_=None, since=None, until=None):
    """{<engine>: {...}|None 每家一项, 'merged': {...}}（键由 ENGINES 决定）。

    合并两边的 /usage。/usage 是 panel 路由（两边都是），所以聚合器会自动带令牌。
    realm=None 时**不传 realm**，让每个引擎用它自己的当前出口；
    显式给了就透传（引擎侧语义见 wb_proxy._get_v1_usage）。

    merged 只含契约规定的三个字段：requests / total_tokens / credit。
    每个字段用「只累加已知值」的规则：两边都没有 → None；只有一边有 → 就是那一边。
    """
    per = {}
    for engine in ENGINES:
        params = {}
        if realm:
            params["realm"] = realm
        if range_:
            params["range"] = range_
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        doc = _json_or_none(engine, ports, "/usage", params=params or None)
        per[engine] = doc if isinstance(doc, dict) else None
    merged = {
        key: _sum_known([(per[e] or {}).get(key) for e in ENGINES])
        for key in ("requests", "total_tokens", "credit")
    }
    # ★ 按注册表返回（第 147 轮）：上面循环明明是 ENGINES，返回却只放了 wb/qd ——
    #   前端拿到的 /api/hub/usage 因此**没有 trae 这一项**，
    #   于是"网关调用量 1,210"的拆分提示只有两家（合计里算着 trae，分项里没有）。
    # ★ 按模型合并（第 35 轮）：三家 by_model 里都带 requests/tokens/cached/credit，
    #   同名模型跨引擎求和 —— 面板的「模型用量」要的是这个，不是每家分开列。
    by_model = {}
    for engine in ENGINES:
        doc = per.get(engine) or {}
        for model, item in (doc.get("by_model") or {}).items():
            if not isinstance(item, dict):
                continue
            cur = by_model.setdefault(str(model), {
                "requests": 0, "total_tokens": 0, "prompt_tokens": 0,
                "cached_tokens": 0, "credit": 0.0, "engines": []})
            cur["requests"] += int(item.get("requests") or 0)
            cur["total_tokens"] += int(item.get("total_tokens") or 0)
            cur["prompt_tokens"] += int(item.get("prompt_tokens") or 0)
            cur["cached_tokens"] += int(item.get("cached_tokens") or 0)
            cur["credit"] += float(item.get("credit") or 0.0)
            cur["engines"].append(engine)
    out = {engine: per.get(engine) for engine in ENGINES}
    out["merged"] = merged
    # 注意：不要往顶层加别的键 —— H1 判据钉死了
    # 「/api/hub/usage 的键 = ENGINES ∪ {merged}」。按模型的汇总走 /api/hub/by-model。
    return out



def hub_by_model(ports, realm=None, range_=None, since=None, until=None):
    """按模型合并的用量（同名模型跨引擎求和）。

    为什么单独一条：`hub_usage` 的顶层键有硬判据（H1），塞不进去；
    面板的「模型用量」只要这一块。
    """
    by_model = {}
    for engine in ENGINES:
        params = {}
        if realm:
            params["realm"] = realm
        if range_:
            params["range"] = range_
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        doc = _json_or_none(engine, ports, "/usage", params=params or None)
        if not isinstance(doc, dict):
            continue
        for model, item in (doc.get("by_model") or {}).items():
            if not isinstance(item, dict):
                continue
            cur = by_model.setdefault(str(model), {
                "requests": 0, "total_tokens": 0, "prompt_tokens": 0,
                "cached_tokens": 0, "credit": 0.0, "engines": []})
            cur["requests"] += int(item.get("requests") or 0)
            cur["total_tokens"] += int(item.get("total_tokens") or 0)
            cur["prompt_tokens"] += int(item.get("prompt_tokens") or 0)
            cur["cached_tokens"] += int(item.get("cached_tokens") or 0)
            cur["credit"] += float(item.get("credit") or 0.0)
            cur["engines"].append(engine)
    return {"by_model": by_model}

def hub_account_health(ports, limit=300):
    """按账号统计"最近有没有在失败"（第 114 轮）。

    为什么放在门户侧：浏览器只有合并令牌，逐引擎打 /usage/recent 会 401；
    聚合器这边带各引擎口令，一次拿三家、按账号归并。

    返回: {`accounts`: {"<engine>|<account>": {fails, total, last_at, last_msg}},
           `errors`: {engine: reason}, `sampled`: n}

    口径说明：`total` 是该账号在**样本内**的请求数，`fails` 是其中判为错误的行。
    样本 = 每家最近 limit 条（不是全量历史）—— 这是"最近怎么样"，不是"累计报表"。
    """
    accounts = {}
    errors = {}
    sampled = 0
    for engine in ENGINES:
        doc = _json_or_none(engine, ports, "/usage/recent",
                            params={"limit": int(limit), "page": 1})
        rows = (doc or {}).get("rows") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            errors[engine] = "no rows"
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            acct = row.get("account") or row.get("uid") or row.get("account_name")
            if not acct:
                continue
            sampled += 1
            key = "%s|%s" % (engine, acct)
            slot = accounts.setdefault(key, {
                "engine": engine, "account": str(acct),
                "fails": 0, "total": 0, "last_at": None, "last_msg": "",
            })
            slot["total"] += 1
            failed = bool(row.get("error")) or (row.get("status") is not None
                                                and row.get("message") is not None)
            if not failed:
                continue
            slot["fails"] += 1
            at = row.get("at")
            try:
                at = float(at)
            except (TypeError, ValueError):
                at = None
            if at is not None and (slot["last_at"] is None or at > slot["last_at"]):
                slot["last_at"] = at
                msg = row.get("message")
                if msg:
                    slot["last_msg"] = str(msg)[:180]
    return {"accounts": accounts, "errors": errors, "sampled": sampled}


def hub_timeseries(ports, realm=None, range_="week", since=None, until=None, bucket=None):
    """三家的 /usage/timeseries **按时间桶合并**（第 105 轮）。

    为什么必须在**门户侧**做：浏览器只有合并令牌，逐引擎请求会 401
    （引擎只认自己的 <token>::<engine> 槽）。聚合器这边有各引擎的面板口令，
    一次拿三家、合并成一条时间轴，前端只发一个请求。

    返回:
      {labels: [at...], series: {wb: [...], qd: [...], trae: [...]},
       totals: {requests: n}, errors: {engine: reason}, bucket_seconds: n}

    缺桶的引擎补 0（不是断线）：某家那段时间没请求，图上应该是贴着 0 的线，
    而不是凭空少一段 —— 少一段会让人以为"数据丢了"。
    """
    per = {}
    for engine in ENGINES:
        params = {"realm": realm or "all", "range": range_}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if bucket:
            params["bucket"] = bucket
        doc = _json_or_none(engine, ports, "/usage/timeseries", params=params)
        per[engine] = doc if isinstance(doc, dict) else None

    by_at = {}
    order = []
    errors = {}
    for engine in ENGINES:
        doc = per.get(engine)
        rows = (doc or {}).get("series") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            errors[engine] = "no series"
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            at = row.get("at")
            try:
                at = int(float(at))
            except (TypeError, ValueError):
                continue
            if at not in by_at:
                by_at[at] = {}
                order.append(at)
            by_at[at][engine] = int(row.get("requests") or 0)
            by_at[at]["__tok_" + engine] = int(row.get("total_tokens") or 0)
            by_at[at]["__cred_" + engine] = float(row.get("credit") or 0.0)
            by_at[at]["__prompt_" + engine] = int(row.get("prompt_tokens") or 0)
            by_at[at]["__cached_" + engine] = int(row.get("cached_tokens") or 0)
            by_at[at]["__errors_" + engine] = int(row.get("errors") or 0)
    order.sort()
    series = {}
    tokens_by_engine = {}
    for engine in ENGINES:
        series[engine] = [by_at[at].get(engine, 0) for at in order]
        # 第 143 轮：**按引擎的 token 序列**。原来只有跨引擎的 sum_tokens，
        # 于是"某一家到底贡献了多少 token"在接口上无法回答 —— 验证脚本只能拿
        # 请求数做断言，抓不住"token 全丢但请求数还在"这种半坏状态。
        tokens_by_engine[engine] = [int(by_at[at].get("__tok_" + engine, 0)) for at in order]
    # 合计三兄弟（requests / tokens / credit）—— KPI 卡的 sparkline 直接用它们，
    # 不必前端再按引擎相加（第 106 轮）。
    sum_tokens = [sum(by_at[at].get("__tok_" + e, 0) for e in ENGINES) for at in order]
    sum_credit = [round(sum(by_at[at].get("__cred_" + e, 0.0) for e in ENGINES), 6) for at in order]
    totals = {"requests": sum(series[e][i] for e in ENGINES for i in range(len(order))) if order else 0}
    bucket_seconds = 0
    for engine in ENGINES:
        doc = per.get(engine)
        if isinstance(doc, dict) and doc.get("bucket_seconds"):
            bucket_seconds = int(doc["bucket_seconds"])
            break
    # 缓存命中率 / 成功率要用：按桶的 prompt / cached / errors（跨引擎求和）
    sum_prompt = [sum(int(by_at[at].get("__prompt_" + e, 0)) for e in ENGINES) for at in order]
    sum_cached = [sum(int(by_at[at].get("__cached_" + e, 0)) for e in ENGINES) for at in order]
    sum_errors = [sum(int(by_at[at].get("__errors_" + e, 0)) for e in ENGINES) for at in order]
    prompt_by_engine = {e: [int(by_at[at].get("__prompt_" + e, 0)) for at in order] for e in ENGINES}
    cached_by_engine = {e: [int(by_at[at].get("__cached_" + e, 0)) for at in order] for e in ENGINES}
    return {"labels": order, "series": series, "totals": totals,
            "tokens": tokens_by_engine,
            "sum_tokens": sum_tokens, "sum_credit": sum_credit,
            "sum_prompt": sum_prompt, "sum_cached": sum_cached, "sum_errors": sum_errors,
            "prompt": prompt_by_engine, "cached": cached_by_engine,
            "errors": errors, "bucket_seconds": bucket_seconds}


def hub_models(ports):
    """两边 /v1/models 的并集，每条**原样透传**并打上 engine 标。

    返回 list；某个引擎取不到就只是不贡献条目（不抛、不占位）。
    条目的其余字段一个不动：这一层不做归一化，归一化是视图的事。
    """
    out = []
    for engine in ENGINES:
        doc = _json_or_none(engine, ports, "/v1/models")
        if not isinstance(doc, dict):
            continue
        data = doc.get("data")
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            entry["engine"] = engine          # 契约：每条打引擎标
            out.append(entry)
    return out


def engine_summary(ports):
    """每个引擎的 id/port/health —— portal 的 /api/hub/engines 直接用这个。

    这是给「哪个引擎在哪个端口、活着没有」这一个问题的最小答案，
    不重复 hub_health 的 ok/version 判断。
    """
    health = engine_health(ports)
    rows = []
    for engine in ENGINES:
        rows.append({
            "id": engine,
            "port": _port_of(ports, engine),
            "configured": _port_of(ports, engine) is not None,
            "health": health.get(engine),
            "up": isinstance(health.get(engine), dict)
                  and health[engine].get("ok") is True,
        })
    return rows


# --------------------------------------------------------------------------- 福利 / 签到
# 归一字段（本模块**定稿**的口径，视图与后续改造照这个来）：
#
#   checkin_done       bool|null  今日签到是否已完成。
#                                 True = 已领（引擎侧 status == "claimed"）；
#                                 False = 还没领但**确实可领**（status == "completed"）；
#                                 None = 无法判断（没有签到行、状态词不认识、或引擎没给数据）。
#                                 wb 的签到不在成长任务里（它是 Account.checkin()），所以 wb 侧
#                                 退一步用 account.lastCheckin 的**日期部分**与本地今天比对。
#   streak_days        int|null   连续签到天数（summary.streak_days）。非数/缺失 → None。
#   claimable_credits  number|null 当前**能领到的积分总额**，按引擎自己的可领判据累加：
#                                 status == "completed"，或 (current >= target 且 target > 0)——
#                                 后者是 wb_tasks.py:489 自己用的领奖条件，照抄它才不会少算。
#                                 已领（claimed）不计；daily_checkin 家族只算第一行
#                                 （qd 的 daily_checkin 与 daily_checkin_legacy 是同一笔
#                                 每日签到走的两条路径，都算会把 100 记成 200）。
#                                 空值口径三条：
#                                   tasks 为空                 -> None（引擎没告诉我）
#                                   tasks 非空但没有可领行      -> 0   （整张表看过了，是事实）
#                                   可领行但 reward_credit 缺失 -> 该行按 0 计（结果是个下界）
#   last_result        str|null   引擎自报的最近状态结论，原样透传。
#                                 ⚠ 目前两个引擎的 /tasks 都**没有**「上一次动作成功/失败」这种
#                                 字段，能拿到的最高级别信号是 summary.travel.state
#                                 （wb 的猫猫旅行 / qd 的福利包）与顶层 msg。
#                                 所以它的值通常是 arrived / idle / departed / unknown 这类词，
#                                 或者一句「未找到可用账号」——**不要**在面板上把它当
#                                 「上次领取成功没有」用。引擎不返回时 None。
#
# 缺失一律 None，绝不补 0（与 hub_usage 同一条读端铁律）。

#: 归一字段名（视图可以照这个列表断言存在性）
REWARD_FIELDS = ("checkin_done", "streak_days", "claimable_credits", "last_result")


def _as_number_or_none(value):
    """数字或 None。bool 不算数（Python 里 bool 是 int 的子类，True 会变成 1）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return int(number) if number.is_integer() else number


def _date_is_today(value):
    """'YYYY-MM-DD HH:MM:SS' / 'YYYY/MM/DD ...' 的日期部分 == 本地今天？

    拿不到可解析日期 → None（不知道，不是 False）。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    head = value.strip().replace("/", "-")[:10]
    parts = head.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return head == time.strftime("%Y-%m-%d")


def _normalize_rewards(engine, doc):
    """在引擎原样体上**附加**归一字段（同名键被归一值覆盖，保证面板只认这套名字）。"""
    doc = doc if isinstance(doc, dict) else {}
    tasks = doc.get("tasks") if isinstance(doc.get("tasks"), list) else []
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
    account = doc.get("account") if isinstance(doc.get("account"), dict) else {}

    # --- checkin_done ---
    checkin_done = None
    for row in tasks:
        if not isinstance(row, dict):
            continue
        if not str(row.get("task_code") or "").lower().startswith("daily_checkin"):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status == "claimed":
            checkin_done = True
        elif status in ("completed", "claimable"):
            checkin_done = False
        break                       # 只看第一行签到行，后面的 legacy 行不覆盖结论
    if checkin_done is None and engine == "wb":
        # wb 的签到不是成长任务；用凭据里的 lastCheckin 日期兜底
        checkin_done = _date_is_today(account.get("lastCheckin"))

    # --- streak_days ---
    streak_days = _as_number_or_none(summary.get("streak_days"))
    if isinstance(streak_days, float) and streak_days.is_integer():
        streak_days = int(streak_days)

    # --- claimable_credits ---
    # 三条规则，缺一不可（都是自测里踩出来的）：
    #   1. tasks 为空 → None：引擎没告诉我，0 会假装「确实没有可领的」；
    #   2. tasks 非空但一条可领的都没有 → **0**：整张表我看过了，这是事实；
    #   3. daily_checkin 家族（qd 的 daily_checkin + daily_checkin_legacy）
    #      **只算第一行** —— 它们是同一笔每日签到走的两条上游路径，
    #      两行都算会把每天的 100 积分记成 200。
    claimable_credits = None
    if tasks:
        total = 0
        checkin_seen = False
        for row in tasks:
            if not isinstance(row, dict):
                continue
            code = str(row.get("task_code") or "").lower()
            if code.startswith("daily_checkin"):
                if checkin_seen:
                    continue
                checkin_seen = True
            status = str(row.get("status") or "").strip().lower()
            if status == "claimed":
                continue
            current = _as_number_or_none(row.get("current"))
            target = _as_number_or_none(row.get("target"))
            claimable_row = (status == "completed") or (
                current is not None and target is not None and target > 0 and current >= target)
            if not claimable_row:
                continue
            # 可领行却没有可用的 reward_credit：引擎的坏行，按 0 计（下界），不因此把整项变 None
            amount = _as_number_or_none(row.get("reward_credit"))
            total += amount if amount is not None else 0
        claimable_credits = int(total) if float(total).is_integer() else total

    # --- last_result ---
    last_result = None
    travel = summary.get("travel")
    if isinstance(travel, dict):
        state = travel.get("state")
        if isinstance(state, str) and state.strip():
            last_result = state.strip()
    if last_result is None:
        msg = doc.get("msg")
        if isinstance(msg, str) and msg.strip():
            last_result = msg.strip()

    out = dict(doc)
    out["engine"] = engine
    out.update({
        "checkin_done": checkin_done,
        "streak_days": streak_days,
        "claimable_credits": claimable_credits,
        "last_result": last_result,
    })
    # 契约：这四个字段必须**全部存在** —— 少一个，视图读到的就是 undefined（静默半残）。
    # 这里用 REWARD_FIELDS 兜底而不是再抄一遍字面量：常量即规格（第 61 轮接上；
    # 在此之前它是个没人读的注释性常量，等于规格写在纸上而代码另写一份）。
    for _key in REWARD_FIELDS:
        out.setdefault(_key, None)
    return out


def _tasks_with_reason(engine, ports, passwords=None, timeout=None):
    """取某引擎的 /tasks；返回 (doc|None, 可读原因|None)。**不抛**。

    /tasks 两边都是 panel 路由（wb_proxy.py:7467、qoder_proxy.py:5756），所以先确保令牌；
    登录失败就直接把原因说清楚，而不是回一个裸 401（那个看不出是密码错还是账号空）。
    """
    if _port_of(ports, engine) is None:
        return None, "engine %s is not configured (no port)" % engine
    token = ensure_token(engine, ports, password=_password_for(engine, passwords))
    if not token:
        return None, ("panel login failed on %s - check the panel password "
                      "(default 'admin', or set HUB_%s_PASSWORD)"
                      % (engine, engine.upper()))
    try:
        status, _headers, raw = _request(engine, ports, "/tasks",
                                           passwords=passwords, timeout=timeout)
    except Exception as exc:
        return None, "unreachable: %s: %s" % (type(exc).__name__, exc)
    if status != 200:
        return None, "HTTP %d from /tasks" % status
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        return None, "bad JSON from /tasks: %s" % exc
    if not isinstance(doc, dict):
        return None, "/tasks did not return a JSON object"
    return doc, None


# 引擎 /tasks 的耗时差异极大（实测：wb 0.9s、trae 0.01s、qd 最长 60s+）。
# 聚合层原来的 2s 超时会让慢引擎**永远**取不到，面板就显示"福利接口取不到"。
# 这里按引擎做缓存 + 后台刷新：接口立刻返回（有缓存用缓存），慢的在后台补上。
_REWARDS_CACHE = {}
_REWARDS_BUSY = set()
_REWARDS_LOCK = threading.Lock()
_REWARDS_TTL = 120.0
_REWARDS_SLOW_TIMEOUT = 90.0


def _rewards_cached(engine):
    with _REWARDS_LOCK:
        item = _REWARDS_CACHE.get(engine)
    if not item:
        return None
    return item


def _rewards_refresh_bg(engine, ports, passwords):
    try:
        doc, reason = _tasks_with_reason(engine, ports, passwords, timeout=_REWARDS_SLOW_TIMEOUT)
        with _REWARDS_LOCK:
            _REWARDS_CACHE[engine] = {"doc": doc, "at": time.time(), "err": reason}
    except Exception as exc:
        with _REWARDS_LOCK:
            _REWARDS_CACHE[engine] = {"doc": None, "at": time.time(),
                                      "err": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        with _REWARDS_LOCK:
            _REWARDS_BUSY.discard(engine)


def _rewards_kick(engine, ports, passwords):
    """没有新鲜缓存时踢一次后台刷新（单飞：同一引擎同时只跑一个）。"""
    with _REWARDS_LOCK:
        if engine in _REWARDS_BUSY:
            return False
        _REWARDS_BUSY.add(engine)
    threading.Thread(target=_rewards_refresh_bg, args=(engine, ports, passwords),
                     daemon=True, name="rewards-" + engine).start()
    return True


def hub_rewards(ports, passwords=None):
    """两边 /tasks 的福利/签到聚合。

    返回 {'wb': {...}|None, 'qd': {...}|None,
          'errors': {'<engine>': None|str}}

    引擎项 = /tasks 原样体 + 本模块定稿的四个归一字段（见本段上方的字段口径表）。
    引擎不可达 / 未登录 / 回非 200 / 回坏 JSON → 该项 None，errors 里给可读原因；
    **任何情况下都不抛**（既有判据 A06 钉住了这条）。

    两个引擎**并发**取：超时是每引擎 2s，串行最坏要等两个 2s，并发只等一个。
    注意「引擎回了 200 但没有可用账号」（msg="未找到可用账号"）**不算错误** ——
    那是真实状态，errors 留 None，原因在 payload 的 msg / last_result 里。
    """
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(ENGINES))) as pool:
        futures = {pool.submit(_tasks_with_reason, engine, ports, passwords): engine
                   for engine in ENGINES}
        for future in as_completed(futures):
            engine = futures[future]
            try:
                results[engine] = future.result()
            except Exception as exc:                    # 兜底：连线程里都不该抛出来
                results[engine] = (None, "%s: %s" % (type(exc).__name__, exc))

    out = {"errors": {}, "cached": {}}
    now = time.time()
    for engine in ENGINES:
        doc, reason = results.get(engine, (None, "not attempted"))
        age = None
        cached = _rewards_cached(engine)
        if isinstance(doc, dict):
            # 快取成功就用新的，并写进缓存
            with _REWARDS_LOCK:
                _REWARDS_CACHE[engine] = {"doc": doc, "at": now, "err": None}
        elif cached and isinstance(cached.get("doc"), dict):
            # 快取失败但缓存里有旧数据：用旧的（面板不至于空着），同时后台刷新
            doc, reason = cached["doc"], None
            age = round(now - float(cached.get("at") or 0), 1)
            _rewards_kick(engine, ports, passwords)
        else:
            # 完全没有数据：踢后台用长超时取一次
            _rewards_kick(engine, ports, passwords)
        out[engine] = _normalize_rewards(engine, doc) if isinstance(doc, dict) else None
        out["errors"][engine] = reason
        out["cached"][engine] = age
    return out


# --------------------------------------------------------------------------- 合并态端点
# 决策书 §十二（三档语义）要求「合并态」把两个引擎**并排展示**。下面三条端点
# （accounts / scheduler / logs）共用一份「取两引擎同一路径」的实现，避免各写一遍
# 登录、超时与错误归一 —— hub_rewards 已经证过那套形状是对的。

def _panel_doc_with_reason(engine, ports, path, passwords=None):
    """取某引擎的任意 panel 路由；返回 (doc|None, 可读原因|None)。**不抛**。

    与 _tasks_with_reason 同源，只是路径可配。失败原因分四类且可读：
    端口未配置 / 面板登录失败 / HTTP 非 200 / 回包不是 JSON。
    """
    if _port_of(ports, engine) is None:
        return None, "engine %s is not configured (no port)" % engine
    token = ensure_token(engine, ports, password=_password_for(engine, passwords))
    if not token:
        return None, ("panel login failed on %s - check the panel password "
                      "(default 'admin', or set HUB_%s_PASSWORD)"
                      % (engine, engine.upper()))
    try:
        status, _headers, raw = _request(engine, ports, path, passwords=passwords)
    except Exception as exc:
        return None, "unreachable: %s: %s" % (type(exc).__name__, exc)
    if status != 200:
        return None, "HTTP %d from %s" % (status, path)
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        return None, "bad JSON from %s: %s" % (path, exc)
    return doc, None


def _both(ports, path, passwords=None):
    """并发取每一家引擎的同一路径 → {'<engine>': (doc, reason)}（键由 ENGINES 决定）。"""
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(ENGINES))) as pool:
        futures = {pool.submit(_panel_doc_with_reason, engine, ports, path, passwords): engine
                   for engine in ENGINES}
        for future in as_completed(futures):
            engine = futures[future]
            try:
                results[engine] = future.result()
            except Exception as exc:                 # 连线程里都不该抛出来
                results[engine] = (None, "%s: %s" % (type(exc).__name__, exc))
    return results


def hub_accounts(ports, passwords=None):
    """两引擎账号**并集**。

    返回 {'items': [{**账号原样字段, 'engine': 'wb'|'qd'}], 'counts': {...},
          'errors': {'<engine>': None|str}}

    并集而不是混装：**每条都带 engine 标**（决策书 §12.3 第 1 条 —— 不标来源的合并
    会让用户看到「12 个账号」却不知道是 8+4 还是别的组合）。
    某引擎不可达 → 它的条目为空、errors 给可读原因，**另一引擎照常返回**。
    """
    # ★ 带 realm=all（Lead 第 47 轮 · 聚合账号池只见 2 条）。
    #   实测：wb 引擎的 /accounts **默认只回当前 realm** —— 我们副本的 28 个账号
    #   全是 cn 区，而引擎默认 realm=intl，于是聚合层拿到 0 条；
    #   qd 同理只回当前区 2 条。合并态的"账号池"语义是**全部账号**（每条都带
    #   engine 与 realm 标，视图侧本来就能按区筛），所以这里必须显式 realm=all。
    #   （直接打引擎验证过：/accounts → 0 条，/accounts?realm=all → 28 条。）
    docs = _both(ports, "/accounts?realm=all", passwords)
    items, counts, errors = [], {}, {}
    # ★ 「没启动」不是「取数失败」（第 90 轮）：只开一个引擎（--engines wb）时，
    #   另一个引擎根本没配置端口，把它记成 error 会让面板四个页面同时报
    #   「「qd」侧取数失败」—— 用户明明是**故意**没开它。
    configured = set(_configured(ports))
    for engine in ENGINES:
        if engine not in configured:
            errors[engine] = None
            counts[engine] = 0
            continue
        doc, reason = docs.get(engine, (None, "not attempted"))
        errors[engine] = reason
        rows = []
        if isinstance(doc, dict):
            rows = doc.get("accounts") or []
        if not isinstance(rows, list):
            rows = []
        counts[engine] = len(rows)
        for row in rows:
            if isinstance(row, dict):
                merged = dict(row)
                merged["engine"] = engine          # ★ 来源标，不可省
                items.append(merged)
    return {"items": items, "counts": counts, "errors": errors,
            # 让面板能区分「没启动」与「取数失败」：前者不该报警
            "configured": sorted(configured)}


def hub_scheduler(ports, passwords=None):
    """两引擎调度器状态。

    返回 {'engines': {'wb': {...}|None, 'qd': {...}|None},
          'errors': {'<engine>': None|str}}
    调度器天然是**每引擎一个**，所以这里不合并成一份 —— 只是并排给出来。
    """
    docs = _both(ports, "/scheduler", passwords)
    out = {"engines": {}, "errors": {}}
    for engine in ENGINES:
        doc, reason = docs.get(engine, (None, "not attempted"))
        out["engines"][engine] = doc if isinstance(doc, dict) else None
        out["errors"][engine] = reason
    return out


def hub_logs(ports, passwords=None, limit=100):
    """两引擎运行日志**按时间合并倒序**。

    返回 {'items': [{**日志原样字段, 'engine': ...}], 'errors': {...}}
    每条带 engine 标；时间字段优先取 `ts`/`time`，取不到就按引擎分组保序（不编时间）。
    """
    docs = _both(ports, "/logs", passwords)
    items, errors = [], {}
    for engine in ENGINES:
        doc, reason = docs.get(engine, (None, "not attempted"))
        errors[engine] = reason
        rows = []
        if isinstance(doc, dict):
            rows = doc.get("logs") or []
        if not isinstance(rows, list):
            rows = []
        for row in rows[:limit]:
            if isinstance(row, dict):
                merged = dict(row)
                merged["engine"] = engine
                items.append(merged)

    def _key(row):
        for name in ("ts", "time", "at", "iso"):
            value = row.get(name)
            if isinstance(value, (int, float)):
                return (0, -float(value))
            if isinstance(value, str) and value:
                return (1, value)
        return (2, "")

    items.sort(key=_key, reverse=False)
    return {"items": items[:limit], "errors": errors}


# --------------------------------------------------------------------------- 自检入口
def _selftest(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m hub.core.aggregator",
        description="聚合器自检：需要引擎已经在跑（python -m hub.server --<engine>-port 8801 ...）")
    # 按注册表生成（第 149 轮）：原来只有 --wb-port/--qd-port，
    # 于是这个自检工具**没法指向 trae** —— 加第三家时它悄悄落后了。
    for _name in ENGINES:
        ap.add_argument("--%s-port" % _name, type=int, default=None)
    ap.add_argument("--realm", default=None, help="透传给 /usage 的 realm（缺省不传）")
    args = ap.parse_args(argv)

    ports = {}
    for _name in ENGINES:
        _p = getattr(args, "%s_port" % _name, None)
        if _p:
            ports[_name] = _p
    if not ports:
        print("  [aggregator] 至少给一个 --<engine>-port（如 --trae-port 8803）")
        return 2

    for engine in _configured(ports):
        token = ensure_token(engine, ports)
        print("  [aggregator] %s :%d  token -> %s"
              % (engine, _port_of(ports, engine), "ok" if token else "NONE"))

    health = hub_health(ports)
    print("  [aggregator] hub_health  ok=%s version=%s" % (health["ok"], health["version"]))
    for engine in ENGINES:
        h = health["engines"].get(engine)
        if h is None:
            print("    %-3s -> None（未配置或没起）" % engine)
        else:
            print("    %-3s -> realm=%s service=%s accounts=%s"
                  % (engine, h.get("realm"), h.get("service"), h.get("accounts")))

    usage = hub_usage(ports, realm=args.realm)
    print("  [aggregator] hub_usage   merged=%s" % (usage["merged"],))
    for engine in ENGINES:
        u = usage.get(engine)
        print("    %-3s -> %s" % (engine, "None" if u is None
                                  else "requests=%s total_tokens=%s credit=%s"
                                       % (u.get("requests"), u.get("total_tokens"), u.get("credit"))))

    models = hub_models(ports)
    by_engine = {}
    for item in models:
        by_engine[item.get("engine")] = by_engine.get(item.get("engine"), 0) + 1
    print("  [aggregator] hub_models %d 条 %s" % (len(models), by_engine))
    return 0 if health["ok"] else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(_selftest())
