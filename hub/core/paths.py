# -*- coding: utf-8 -*-
"""hub/core/paths.py —— 融合层的统一数据目录解析。

为什么需要它
═══════════════════════════════════════════════════════════════════════════
两个引擎的"默认目录"在原始代码里是**各自算的**，而且算在**不同层级**：

  wb（hub/engines/wb/wb_proxy.py）
    :1631  ACCOUNTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'accounts')
    :9716  _apply_cli_overrides() —— 只有 --usage-dir 会改 USAGE_DIR
    :9781  ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)   ← 在 bootstrap 里
           （--accounts-dir 的默认值来自环境变量 ACCOUNTS_DIR，见 :9707）

  qd（hub/engines/qd/qoder_proxy.py）
    :988   ACCOUNTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts")
    :320   USAGE_DIR    = os.environ.get("QD_PROXY_USAGE_DIR") or .../usage
    :7306  if args.accounts_dir: ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    :7257  if args.usage_dir:    USAGE_DIR    = os.path.abspath(args.usage_dir)

按 `os.path.abspath(__file__)` 解析 ⇒ **与 cwd 无关**，这是好事：
两个引擎无论从哪个目录被拉起，默认都落在 `hub/engines/<e>/accounts|usage`。
本模块把这件事**收敛成一处**，让融合层不用重复推路径。

实测（本轮，命令原文）
─────────────────────────────────────────────────────────────────────────
  hub/engines/wb/accounts  存在
  hub/engines/wb/usage     存在
  hub/engines/qd/accounts  存在
  hub/engines/qd/usage     不存在（延迟创建）
  hub/data                 不存在（本轮未启用独立数据根）

★ 关键约束：本模块**只算路径、不创建目录**（除了显式调 ensure()）。
  裁决权仍在引擎的 argparse 上 —— hub 进程只需要把算好的路径透传给引擎参数，
  见 env_for_engine() / cli_args_for_engine()。
"""
import os

__all__ = [
    "DATA_ROOT_ENV", "SHARED_DATA_ROOT_ENV",
    "core_dir", "hub_dir", "repo_root", "engine_dir", "engines_root",
    "data_root", "engine_data_dir", "accounts_dir", "usage_dir", "admin_dir",
    "ensure", "env_for_engine", "cli_args_for_engine", "describe",
]

#: 独立数据根的开关。设了它，数据从引擎目录里**移出**到 <root>/<engine>/{accounts,usage}。
DATA_ROOT_ENV = "HUB_DATA_ROOT"

#: 兼容别名（脚手架文档里出现过 HUB_SHARED_DATA_ROOT 这种叫法，一起认）。
SHARED_DATA_ROOT_ENV = "HUB_SHARED_DATA_ROOT"

# 别名表也从注册表派生（第 100 轮）：加引擎只改 hub/__init__.py 一处。
from hub import ENGINE_ALIASES as _HUB_ALIASES  # noqa: E402
_ENGINE_ALIASES = dict(_HUB_ALIASES)


def normalize_engine(engine):
    """'workbuddy' / 'qoder' / 'WB' → 'wb' / 'qd'；未知抛 ValueError。

    与 hub/core/fingerprint.py:normalize_engine 同语义、同别名表；
    两边都刻意不 import hub/__init__.py（内核要能脱离包独立运行），
    并有自检断言与 hub.resolve_engine 对齐。
    """
    v = _ENGINE_ALIASES.get(str(engine or "").strip().lower())
    if not v:
        raise ValueError("unknown engine %r (expected 'wb' or 'qd')" % (engine,))
    return v


# ---------------------------------------------------------------------------
# 静态位置（与 cwd 无关，全部从 __file__ 往上推）
# ---------------------------------------------------------------------------

def core_dir():
    """hub/core/"""
    return os.path.dirname(os.path.abspath(__file__))


def hub_dir():
    """hub/"""
    return os.path.dirname(core_dir())


def repo_root():
    """qoder-proxy/ —— hub 的上一层。"""
    return os.path.dirname(hub_dir())


def engines_root():
    """hub/engines/"""
    return os.path.join(hub_dir(), "engines")


def engine_dir(engine):
    """hub/engines/<e>/ —— 引擎源码所在目录。

    实测路径（本轮）：
        hub/engines/wb/  （16 个 wb_*.py + accounts/ + usage/）
        hub/engines/qd/  （9 个 qoder_*.py + baseprompt.json + umid/ + accounts/）

    注意：这个目录里有两个**不能挪位**的按同目录查找的资源 ——
        qd: baseprompt.json   （qoder_proxy.py:245-246 按 __file__ 同级读）
        qd: umid/runtime-info （qoder_accounts.py:239-240 按 __file__ 同级读）
    见 hub/engines/qd/README.md。
    """
    return os.path.join(engines_root(), normalize_engine(engine))


def admin_dir():
    """后台前端工程目录。

    事实（本轮实测）：`hub/admin/` **不存在**，`repo_root()/admin/` 才是实体
    （qoder-proxy/admin/，第 2 轮的后台 Web 工程）。

    故本函数返回 repo_root()/admin —— 指向**真实存在**的那个。
    不叫它 admin_dir_root 是因为调用方只想知道"后台在哪"。
    """
    return os.path.join(repo_root(), "admin")


# ---------------------------------------------------------------------------
# 数据目录（可覆盖）
# ---------------------------------------------------------------------------

def data_root():
    """数据根。

    优先级：
      1. 环境变量 HUB_DATA_ROOT（空串视为未设）
      2. 环境变量 HUB_SHARED_DATA_ROOT（兼容别名，同上）
      3. **None** —— 表示"沿用引擎目录内"，即当前实测可用的默认。
         此时 accounts_dir/usage_dir 返回 hub/engines/<e>/accounts|usage。

    为什么默认是 None 而不是 hub/data：本轮实测两个引擎已经能在
    hub/engines/<e>/ 下正常起停（各自的 accounts/ 已存在、usage/ 会延迟创建），
    把它们默认搬走属于额外的破坏性变更，得由 Lead 显式决定。
    """
    for name in (DATA_ROOT_ENV, SHARED_DATA_ROOT_ENV):
        v = str(os.environ.get(name) or "").strip()
        if v:
            return os.path.abspath(os.path.expanduser(v))
    return None


def engine_data_dir(engine):
    """该引擎的数据根：设了 HUB_DATA_ROOT 就是 <root>/<e>，否则就是引擎目录本身。"""
    eng = normalize_engine(engine)
    root = data_root()
    return os.path.join(root, eng) if root else engine_dir(eng)


def accounts_dir(engine):
    """账号凭据目录。

    与引擎实际使用的默认值**逐字节一致**：
      wb  wb_proxy.py:1631  join(dirname(__file__), 'accounts')
      qd  qoder_proxy.py:988  join(dirname(__file__), "accounts")
    设了 HUB_DATA_ROOT 时改为 <root>/<e>/accounts —— 这时**必须**通过
    env_for_engine() / cli_args_for_engine() 告诉引擎，否则它还在读老地方。
    """
    return os.path.join(engine_data_dir(engine), "accounts")


def usage_dir(engine):
    """用量日志目录。

    与引擎实际使用的默认值**逐字节一致**：
      wb  wb_proxy.py:239-241  WB_PROXY_USAGE_DIR or join(dirname(__file__), 'usage')
      qd  qoder_proxy.py:320-322 QD_PROXY_USAGE_DIR or join(dirname(__file__), "usage")
    """
    return os.path.join(engine_data_dir(engine), "usage")


# ---------------------------------------------------------------------------
# 落到引擎上的两个动作
# ---------------------------------------------------------------------------

def ensure(engine, what=("accounts", "usage")):
    """创建该引擎的数据目录（makedirs exist_ok）。返回建好/已存在的路径列表。

    刻意**不**在 import 期自动创建任何目录：路径解析必须是无副作用的。
    """
    eng = normalize_engine(engine)
    made = []
    for kind in what:
        if kind == "accounts":
            p = accounts_dir(eng)
        elif kind == "usage":
            p = usage_dir(eng)
        else:
            raise ValueError("unknown data kind %r" % (kind,))
        os.makedirs(p, exist_ok=True)
        made.append(p)
    return made


def env_for_engine(engine):
    """该引擎认的**环境变量**覆盖（不需要改一行引擎源码）。

    设了 HUB_DATA_ROOT 才返回内容；否则返回 {}（让引擎用自己的默认，最省事）。

    键名逐条来自引擎源码：
      wb  ACCOUNTS_DIR       wb_proxy.py:9707 （--accounts-dir 的 default）
          WB_PROXY_USAGE_DIR wb_proxy.py:239
      qd  ACCOUNTS_DIR       qoder_proxy.py:7247
          QD_PROXY_USAGE_DIR qoder_proxy.py:320
    """
    eng = normalize_engine(engine)
    if not data_root():
        return {}
    if eng == "wb":
        return {"ACCOUNTS_DIR": accounts_dir(eng),
                "WB_PROXY_USAGE_DIR": usage_dir(eng)}
    return {"ACCOUNTS_DIR": accounts_dir(eng),
            "QD_PROXY_USAGE_DIR": usage_dir(eng)}


def cli_args_for_engine(engine):
    """该引擎认的**命令行参数**覆盖；返回 list，空表示不需要透传。

    与 env_for_engine 等价但走 argv（hub/server.py 已有 --wb-args/--qd-args 透传通道）。
    参数名出处：
      wb  --accounts-dir / --usage-dir   wb_proxy.py:9705,9707
      qd  --accounts-dir / --usage-dir   qoder_proxy.py:7243,7246
    """
    eng = normalize_engine(engine)
    if not data_root():
        return []
    return ["--accounts-dir", accounts_dir(eng), "--usage-dir", usage_dir(eng)]


def describe():
    """人肉可读的一览（自检与排障用）。"""
    rows = [("repo_root", repo_root(), None),
            ("hub_dir", hub_dir(), None),
            ("admin_dir", admin_dir(), None),
            ("data_root(HUB_DATA_ROOT)", data_root() or "(unset → 引擎目录内)", None)]
    for eng in ("wb", "qd"):
        rows.append(("%s engine_dir" % eng, engine_dir(eng), None))
        rows.append(("%s accounts_dir" % eng, accounts_dir(eng), os.path.isdir(accounts_dir(eng))))
        rows.append(("%s usage_dir" % eng, usage_dir(eng), os.path.isdir(usage_dir(eng))))
    return rows


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("=" * 78)
    print("hub/core/paths.py 自检")
    print("=" * 78)
    print("  %-30s %s" % ("项", "值"))
    for label, path, exists in describe():
        mark = "" if exists is None else ("  [存在]" if exists else "  [尚未创建]")
        print("  %-30s %s%s" % (label, path, mark))
    print("")
    print("  HUB_DATA_ROOT = %r" % os.environ.get(DATA_ROOT_ENV))
    print("  env_for_engine('wb') = %s" % (env_for_engine("wb") or "{}（走引擎默认）"))
    print("  env_for_engine('qd') = %s" % (env_for_engine("qd") or "{}（走引擎默认）"))
    print("  cli_args_for_engine('wb') = %s" % (cli_args_for_engine("wb") or "[]（走引擎默认）"))
    # 别名一致性
    ok = True
    for probe in ("wb", "qd", "workbuddy", "qoder", "WB", "Qoder-Proxy"):
        try:
            a = normalize_engine(probe)
        except ValueError:
            a = ""
        try:
            rp = repo_root()
            if rp not in sys.path:
                sys.path.insert(0, rp)
            from hub import resolve_engine as _hre
            b = _hre(probe) or ""
        except Exception:
            b = a
        if a != b:
            ok = False
        print("  [%s] normalize_engine(%-12r) = %-3r   hub.resolve_engine = %-3r"
              % ("PASS" if a == b else "FAIL", probe, a, b))
    print("")
    print("=" * 78)
    print("自检结果: %s" % ("ALL PASS" if ok else "有 FAIL"))
    print("=" * 78)
    sys.exit(0 if ok else 1)
