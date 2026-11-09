# -*- coding: utf-8 -*-
"""会话缓存保温（cache keepalive）——可选优化，默认关闭。

背景（配套 issue「上游隐式前缀缓存实测 ~5 分钟闲置过期」）：

  上游对 prompt 前缀的缓存有闲置过期行为，实测约 5 分钟未被命中即失效
  （与阿里云百炼显式缓存公开语义一致：5 分钟、命中续期）。会话闲置超过
  该窗口后，下一发请求全量重写——面板表现为 0% 命中、全价积分（124k
  prompt ≈ 137 credits，命中时 ≈ 13.8）。v1.7.23 修掉的 #136 是「会话键
  每提问轮换」，与本机制（闲置过期）正交，网关无法消除，只能缓解。

机制：

  网关在成功转发一条真实对话请求后（open_upstream 成功返回处），把客户端
  原始 payload 按**最终 session_id** 记录在内存里（只保留最近 MAX_CONVS
  个，超出 LRU 淘汰）。后台线程每 20s 巡检：会话闲置 >= INTERVAL 秒且未
  超过 MAX_AGE 时，用同 payload 重发一次 stream=False、max_tokens=1 的
  最小请求——prompt 前缀逐字节相同，命中上游缓存并续期；消耗 ≈ 一次缓存
  读（124k ≈ 10.7 credits），远低于全量重写（≈137）。

边界与隐私：

  - 默认 QD_CACHE_KEEPALIVE=0：不开、不存任何东西，行为与改动前一致；
  - 只存 payload 本身，不落盘；上限 MAX_CONVS 个会话（默认 2）；
  - 保温请求照常记账，key_id 固定为 "keepalive"，面板可见、可审计；
  - 上游对「同会话高频重复请求」的风控容忍度未知：请从小 INTERVAL、少
    会话起步，观察 /health 的 risk 遥测（queue/rate/rejected）再调大；
  - 发送函数由网关注册（configure），本模块不 import 网关，避免环依赖，
    也让 tick() 可以用假 sender 单测。
"""

import json
import os
import threading
import time
from collections import OrderedDict

_TRUTHY = ("1", "true", "yes", "on")

_LOCK = threading.Lock()
_ENTRIES = OrderedDict()   # session_id -> entry；LRU，尾部最新
_SENDER = None             # callable(entry) -> usage dict；失败抛异常
_STOP = threading.Event()
_THREAD = None
_STATS = {"pings_sent": 0, "pings_failed": 0, "stale_drops": 0}


def _env(name, default):
    raw = (os.environ.get(name) or "").strip()
    return raw if raw else default


def _truth(name, default):
    return str(_env(name, default)).lower() in _TRUTHY


def _int(name, default, lo, hi):
    try:
        v = int(str(_env(name, default)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def enabled():
    """QD_CACHE_KEEPALIVE，默认 0（关闭）。"""
    return _truth("QD_CACHE_KEEPALIVE", "0")


def interval():
    """闲置多久后保温一次，秒。默认 240——必须小于上游 ~5min TTL。"""
    return _int("QD_CACHE_KEEPALIVE_INTERVAL", 240, 30, 86400)


def max_age():
    """闲置超过该时长的会话不再保温（人早走了，省下这笔读费用），秒。"""
    return _int("QD_CACHE_KEEPALIVE_MAX_AGE", 1800, 60, 86400)


def max_convs():
    """内存里最多保留几个会话的 payload（LRU），默认 2。"""
    return _int("QD_CACHE_KEEPALIVE_MAX_CONVS", 2, 1, 64)


def configure(send_fn):
    """注册发送函数：entry -> usage dict；失败抛异常。"""
    global _SENDER
    _SENDER = send_fn


def observe(payload, session_id, account_uid, realm, model):
    """登记一条刚成功转发的真实对话请求。关闭/异常时静默返回 False。

    payload 是客户端原始请求 dict（open_upstream 入参形态）；这里 json
    往返做一次深拷贝，之后无论网关/客户端怎么改它，保温用的都是登记时的
    快照——前缀稳定性由快照保证。
    """
    if not enabled() or not session_id:
        return False
    try:
        stored = json.loads(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return False
    entry = {
        "payload": stored,
        "session_id": str(session_id),
        "account_uid": str(account_uid or ""),
        "realm": str(realm or ""),
        "model": str(model or ""),
        "at": time.time(),
        "pings": 0,
    }
    with _LOCK:
        _ENTRIES.pop(entry["session_id"], None)
        _ENTRIES[entry["session_id"]] = entry
        _ENTRIES.move_to_end(entry["session_id"])
        while len(_ENTRIES) > max_convs():
            _ENTRIES.popitem(last=False)
    return True


def drop(session_id):
    """显式淘汰一个会话（供网关在账号不可用等场景调用）。"""
    with _LOCK:
        return _ENTRIES.pop(str(session_id), None) is not None


def status():
    """只读快照：不含 payload，供诊断端点/日志使用。"""
    with _LOCK:
        now = time.time()
        out = [{"session_id": sid[:16], "account": e["account_uid"][:8],
                "model": e["model"], "idle_s": round(now - e["at"], 1),
                "pings": e["pings"]}
               for sid, e in _ENTRIES.items()]
        return {"enabled": enabled(), "interval_s": interval(),
                "max_age_s": max_age(), "max_convs": max_convs(),
                "entries": out, "stats": dict(_STATS)}


def tick(now=None):
    """巡检一次：到期会话发保温请求，超龄淘汰。永不抛异常。

    返回 {"sent", "failed", "stale"}。sender 异常视为该会话不可保温
    （账号下线/上游拒绝）——淘汰它，下一发真实请求会重新登记。
    """
    now = time.time() if now is None else now
    sent = failed = stale = 0
    due = []
    with _LOCK:
        for sid, e in _ENTRIES.items():
            age = now - e["at"]
            if age >= max_age():
                due.append((sid, "stale"))
            elif age >= interval() and _SENDER is not None:
                due.append((sid, "ping"))
        for sid, action in due:
            e = _ENTRIES.pop(sid, None)   # 先摘下：成功放回，失败即淘汰
            if e is None:
                continue
            if action == "stale":
                stale += 1
                continue
            # 快照与摘取之间 observe() 可能刚刷新过它：没到点就放回去
            if now - e["at"] < interval():
                _ENTRIES[sid] = e
                _ENTRIES.move_to_end(sid)
                continue
            try:
                _SENDER(dict(e))
            except Exception:
                failed += 1
                continue
            e["at"] = now
            e["pings"] += 1
            _ENTRIES[sid] = e
            _ENTRIES.move_to_end(sid)
            sent += 1
        _STATS["pings_sent"] += sent
        _STATS["pings_failed"] += failed
        _STATS["stale_drops"] += stale
    return {"sent": sent, "failed": failed, "stale": stale}


def _loop():
    while not _STOP.wait(20.0):
        try:
            tick()
        except Exception:
            pass


def start_loop():
    """启动后台巡检线程（幂等）。"""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="qd-cache-keepalive",
                               daemon=True)
    _THREAD.start()
    return _THREAD


def stop_loop():
    """停掉后台线程（测试用）。"""
    _STOP.set()
