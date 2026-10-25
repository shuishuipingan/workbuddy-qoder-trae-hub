# -*- coding: utf-8 -*-
"""hub/core/fingerprint.py —— 跨引擎设备指纹**分区**派生（决策 D-D · 最高优先级）。

═══════════════════════════════════════════════════════════════════════════
风险（实测证据）
═══════════════════════════════════════════════════════════════════════════
两个引擎的 derive_id **算法、盐串、切片三处逐字节相同**：

  wb : hub/engines/wb/wb_fingerprint.py:11-17
       seed = f"{salt}:{uid or 'anonymous'}"
       return hashlib.md5(seed.encode("utf-8")).hexdigest()[:36]

  qd : hub/engines/qd/qoder_fingerprint.py:20-33
       seed = f"{salt}:{uid or 'anonymous'}"      ← 同一行
       return hashlib.md5(seed.encode("utf-8")).hexdigest()[:36]

两引擎并库后，**同一个 uid 会派生出同一个 machineId**：
同一台"虚拟物理设备"同时给 workbuddy.ai 与 qoder.com 发请求，
把账号的虚拟设备同时暴露给两家上游（docs/merge/03-融合映射与迁移清单.md:158）。

═══════════════════════════════════════════════════════════════════════════
两个必须写进脑子的坑（照抄 03 号 §4.1 第 3、4 条）
═══════════════════════════════════════════════════════════════════════════
1. **不要"顺手修正" [:36]**
   hashlib.md5(...).hexdigest() **恒为 32 个十六进制字符**，所以 [:36] 是**恒等切片**，
   结果就是那 32 位。改成真 36 位 = 等价换盐 = 破坏性变更。
   （qoder_fingerprint.py:25-29 有原注释，本模块照抄该事实。）
   本模块的 legacy 分支**逐字节复刻** [:36] 写法，而不是"简化"成不加切片。

2. **拼接顺序参与哈希**
   旧算法：  f"{salt}:{uid}"            ← uid 在后
   新算法：  f"hub:{engine}:{salt}:{uid}" ← uid 仍在后，前缀加在最前面
   顺序是必要的一部分：若把 engine 插在 uid 之后（如 f"{salt}:{uid}:{engine}"），
   就与"换盐"不等价了；而把命名空间放最前，等价于给盐加前缀，
   **不可能与旧值撞车**（旧 seed 以 salt 开头，新 seed 以 "hub:" 开头）。

═══════════════════════════════════════════════════════════════════════════
灰度开关
═══════════════════════════════════════════════════════════════════════════
    HUB_FINGERPRINT_MODE = "legacy"（**默认**）| "namespaced"

  * 默认 legacy = 零风险：输出与当前线上**逐字节相同**，可直接替换现有调用。
  * 只有显式设成 namespaced 才切新算法；切之前需要 YG 批准 + 新账号验证
    （docs/merge/01-后端融合总纲.md:95-99 的破坏性变更声明）。
  * 另兼容逐引擎开关 WB_FINGERPRINT_LEGACY / QD_FINGERPRINT_LEGACY
    （01 号 §5 第 3 条提出、03 号 §4.1 第 2 条细化为双开关：能单侧回退）。
    解析优先级：显式 mode 参数 > 逐引擎 LEGACY 开关 > HUB_FINGERPRINT_MODE > "legacy"。

本模块**只读**，不写入任何引擎数据，也不 import 任何引擎模块
（keep it dependency-free：hub 内核只依赖标准库）。
"""
import hashlib
import os

__all__ = [
    "MODES", "DEFAULT_MODE",
    "LEGACY_ENV", "ENGINE_LEGACY_ENV", "MODE_ENV",
    "normalize_engine", "mode_for", "derive", "derive_id_legacy", "derive_id",
    "generate_request_id", "generate_request_id_legacy",
    "KNOWN_SALTS", "verify_against_engines",
]

#: 合法模式
MODES = ("legacy", "namespaced")

#: ★ 默认必须是 legacy（零风险）。改这一行等于改全局默认，不要动。
DEFAULT_MODE = "legacy"

MODE_ENV = "HUB_FINGERPRINT_MODE"
LEGACY_ENV = "HUB_FINGERPRINT_LEGACY"          # 兼容 01 号 §5 提的写法
ENGINE_LEGACY_ENV = {
    "wb": "WB_FINGERPRINT_LEGACY",
    "qd": "QD_FINGERPRINT_LEGACY",
}

#: 两个引擎实际在用的盐（实测行号见 03 号 §4.1 表）
#:   wb : wb_fingerprint.py:42 "machine" / :43 "session" / :22 "req"
#:   qd : qoder_fingerprint.py:245 "machine" / :246 "session" / :38 "req"
#:        (+ :45 "machinetype"，qd 独有)
KNOWN_SALTS = {
    "wb": ("machine", "session", "req"),
    "qd": ("machine", "session", "req", "machinetype"),
}

_ENGINES = ("wb", "qd")


def normalize_engine(engine):
    """把引擎名收敛成 'wb' / 'qd'；None 或未知返回 ''。

    刻意不 import hub/__init__.py 的 ENGINE_ALIASES：内核模块要能独立运行
    （本文件支持 `python hub/core/fingerprint.py` 直接自检）。别名表很小，这里
    内联一份并加断言测试保证与 hub.resolve_engine 一致。
    """
    v = str(engine or "").strip().lower()
    if v in ("wb", "workbuddy", "wb-proxy"):
        return "wb"
    if v in ("qd", "qoder", "qoder-proxy"):
        return "qd"
    return ""


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def mode_for(engine, mode=None):
    """决定某引擎这次走哪条算法。返回 'legacy' 或 'namespaced'。

    优先级（高 → 低）：
      1. 显式传的 mode 参数
      2. 逐引擎开关 <ENGINE>_FINGERPRINT_LEGACY=1  → legacy（可单侧回退）
      3. 全局 HUB_FINGERPRINT_LEGACY=1             → legacy
      4. HUB_FINGERPRINT_MODE 的值（非法值 → 回落默认）
      5. DEFAULT_MODE（= legacy）
    """
    if mode is not None:
        m = str(mode).strip().lower()
        if m in MODES:
            return m
        # 非法显式值：不猜，回落默认，避免"以为切了其实没切"
        return DEFAULT_MODE
    eng = normalize_engine(engine)
    env_name = ENGINE_LEGACY_ENV.get(eng)
    if env_name and _truthy(os.environ.get(env_name)):
        return "legacy"
    if _truthy(os.environ.get(LEGACY_ENV)):
        return "legacy"
    m = str(os.environ.get(MODE_ENV) or "").strip().lower()
    if m in MODES:
        return m
    return DEFAULT_MODE


def derive_id_legacy(uid, salt, engine=None):
    """**逐字节复刻**两个引擎的原始 derive_id（回退用）。

    engine 参数只为签名对称/日志可读，**不参与哈希**——传什么都返回旧值。
    复刻对象（两处逐字节相同）：
        wb  wb_fingerprint.py:11-17
        qd  qoder_fingerprint.py:20-33
    具体两行：
        seed = f"{salt}:{uid or 'anonymous'}"
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:36]
    `[:36] 是恒等切片`——保留它，别改（改了 = 换盐，见模块 docstring 坑 1）。
    """
    seed = f"{salt}:{uid or 'anonymous'}"
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:36]


def derive_id(uid, salt, engine):
    """**新算法**：盐串加引擎命名空间前缀。

    格式与拼接顺序照 docs/merge/01-后端融合总纲.md:88-93 定稿：
        seed = f"hub:{engine}:{salt}:{uid or 'anonymous'}"
    同样用 [:36]（恒等切片，与旧实现保持同一种写法，避免两个分支看起来不一样）。

    与旧值**保证不相等**：旧 seed 以 salt 开头，新 seed 以 "hub:" 开头，
    前缀不可能被抵消（顺序参与哈希——见 03 号 §4.1 第 4 条）。
    """
    eng = normalize_engine(engine)
    if not eng:
        # 不静默兜底成某个引擎：那会把"没传引擎"变成"悄悄用了 wb 的命名空间"，
        # 是最难查的一类串号。直接报错，逼调用方给对。
        raise ValueError(
            "derive_id(uid, salt, engine) 需要 engine 为 'wb' 或 'qd'，收到 %r" % (engine,))
    seed = f"hub:{eng}:{salt}:{uid or 'anonymous'}"
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:36]


def derive(uid, salt, engine, mode=None):
    """统一入口：按 mode_for(engine, mode) 选 legacy / namespaced。

    默认（不设任何环境变量）走 **legacy** —— 与线上逐字节相同，可直接替换。
    """
    if mode_for(engine, mode) == "namespaced":
        return derive_id(uid, salt, engine)
    return derive_id_legacy(uid, salt, engine)


def generate_request_id_legacy(uid):
    """旧实现的请求 ID：稳定前缀 + 微秒时间戳。

    与 wb_fingerprint.py:20-24 / qoder_fingerprint.py:36-40 同形。
    """
    import time
    prefix = derive_id_legacy(uid, "req")
    suffix = str(time.time_ns() % 1000000).zfill(6)
    return f"{prefix}-{suffix}"


def generate_request_id(uid, engine, mode=None):
    """请求 ID 的 mode-aware 版本。"""
    import time
    prefix = derive(uid, "req", engine, mode=mode)
    suffix = str(time.time_ns() % 1000000).zfill(6)
    return f"{prefix}-{suffix}"


def verify_against_engines(cases=None):
    """对拍：直接 import 两个引擎的**原实现**，逐字节比对 legacy 分支。

    返回 [(engine, uid, salt, mine, theirs, ok), ...]。
    这是本文件里唯一会 import 引擎模块的地方，**且只在显式调用时**发生
    （模块导入期不碰引擎目录，保持内核纯净）。
    找不到引擎模块时抛 ImportError，由调用方决定是否跳过。
    """
    import sys
    here = os.path.dirname(os.path.abspath(__file__))      # hub/core
    root = os.path.dirname(os.path.dirname(here))          # qoder-proxy/
    out = []
    for eng, modname, path in (
        ("wb", "wb_fingerprint", os.path.join(root, "hub", "engines", "wb")),
        ("qd", "qoder_fingerprint", os.path.join(root, "hub", "engines", "qd")),
    ):
        if path not in sys.path:
            sys.path.insert(0, path)
        mod = __import__(modname)
        for uid, salt in (cases or [("uid-a", "machine"), ("uid-a", "session"),
                                    ("", "machine"), ("uid-中文", "req")]):
            mine = derive_id_legacy(uid, salt, eng)
            theirs = mod.derive_id(uid, salt)
            out.append((eng, uid, salt, mine, theirs, mine == theirs))
    return out


def _selftest():
    """`python hub/core/fingerprint.py` 自检。"""
    ok = True

    def line(label, passed, extra=""):
        nonlocal ok
        if not passed:
            ok = False
        print(("  [%s] " % ("PASS" if passed else "FAIL")) + label + (("  " + extra) if extra else ""))

    print("=" * 72)
    print("hub/core/fingerprint.py 自检")
    print("=" * 72)
    print("  默认模式            : %s" % DEFAULT_MODE)
    print("  %s : %r" % (MODE_ENV, os.environ.get(MODE_ENV)))
    print("  逐引擎开关          : %s"
          % ", ".join("%s=%r" % (v, os.environ.get(v)) for v in ENGINE_LEGACY_ENV.values()))
    print("")

    # ---- 1. 同一 (uid,salt,engine) 在两种模式下的值 ----
    print("[1] 同一 (uid, salt, engine) 在两种模式下的取值")
    print("    %-28s %-34s %-34s" % ("(salt, mode)", "wb", "qd"))
    uid = "uid-0000000000000001"
    for salt in ("machine", "session", "req"):
        lg_w, ns_w = derive(uid, salt, "wb", mode="legacy"), derive(uid, salt, "wb", mode="namespaced")
        lg_q, ns_q = derive(uid, salt, "qd", mode="legacy"), derive(uid, salt, "qd", mode="namespaced")
        print("    %-28s %-34s %-34s" % (salt + " / legacy", lg_w, lg_q))
        print("    %-28s %-34s %-34s" % (salt + " / namespaced", ns_w, ns_q))
        # ★ 核心判据 ①：namespaced 下 wb / qd 必须不同
        line("namespaced 下 %s: wb != qd" % salt, ns_w != ns_q)
        # legacy 下本来就是同一个值（这正是风险本身）
        line("legacy 下 %s: wb == qd（即风险现象）" % salt, lg_w == lg_q,
             "← 两引擎原算法逐字节相同，故相等")
        line("namespaced 下 %s: 与 legacy 不同" % salt, ns_w != lg_w and ns_q != lg_q)
    print("")

    # ---- 2. legacy 与线上实现对拍（假设 ①：md5("salt:"+uid)[:36]） ----
    print("[2] legacy 与手算 md5 对拍（验收判据 ②）")
    for uid_, salt_ in (("uid-a", "machine"), ("uid-a", "session"), ("", "req"), ("中文-uid", "machine")):
        expect = hashlib.md5(("%s:%s" % (salt_, uid_ or "anonymous")).encode("utf-8")).hexdigest()[:36]
        got = derive_id_legacy(uid_, salt_, "wb")
        line("legacy(%r, %r) == md5('%s:%s')[:36]" % (uid_, salt_, salt_, uid_ or "anonymous"),
             got == expect, got)
        line("  ^ 长度 == 32（md5 hexdigest 恒 32，[:36] 为恒等切片）", len(got) == 32)
    print("")

    # ---- 3. 与两个引擎的原实现直接对拍 ----
    print("[3] 与引擎原实现直接对拍（import hub/engines/*/ 真实模块）")
    try:
        rows = verify_against_engines()
        for eng, uid_, salt_, mine, theirs, same in rows:
            line("%s derive_id(%r,%r): legacy 逐字节相同" % (eng, uid_, salt_), same,
                 "%s vs %s" % (mine, theirs))
    except Exception as exc:      # 引擎目录不可用时不算失败，但要显式说出来
        print("    [SKIP] 无法 import 引擎模块（%r）—— 引擎目录不完整时属正常" % (exc,))
    print("")

    # ---- 4. 未命名空间时不许静默兜底 ----
    print("[4] 边界：engine 非法/缺失必须报错，不许静默兜底")
    for bad in ("", None, "zz", "  "):
        try:
            derive_id(uid, "machine", bad)
            line("derive_id(engine=%r) 抛 ValueError" % (bad,), False, "它没抛")
        except ValueError:
            line("derive_id(engine=%r) 抛 ValueError" % (bad,), True)
    line("legacy 分支不校验 engine（回退路径要能照常工作）",
         derive_id_legacy(uid, "machine", None) == derive_id_legacy(uid, "machine", "wb"))
    print("")

    # ---- 5. 模式解析优先级 ----
    print("[5] 模式解析优先级")
    saved = {k: os.environ.get(k) for k in (MODE_ENV, LEGACY_ENV, *ENGINE_LEGACY_ENV.values())}
    try:
        for k in saved:
            os.environ.pop(k, None)
        line("无任何环境变量 → legacy（默认零风险）", mode_for("wb") == "legacy")
        os.environ[MODE_ENV] = "namespaced"
        line("%s=namespaced → namespaced" % MODE_ENV, mode_for("wb") == "namespaced")
        os.environ["WB_FINGERPRINT_LEGACY"] = "1"
        line("WB_FINGERPRINT_LEGACY=1 压过全局 → wb=legacy 且 qd 仍 namespaced（可单侧回退）",
             mode_for("wb") == "legacy" and mode_for("qd") == "namespaced")
        line("显式 mode 参数优先于一切 → namespaced",
             mode_for("wb", mode="namespaced") == "namespaced")
        os.environ[MODE_ENV] = "bogus"
        os.environ.pop("WB_FINGERPRINT_LEGACY", None)
        line("%s 非法值 → 回落 legacy（不猜）" % MODE_ENV, mode_for("qd") == "legacy")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("")

    # ---- 6. 引擎别名与 hub.resolve_engine 一致性 ----
    print("[6] 引擎别名与 hub.resolve_engine 一致性")
    try:
        import sys as _sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if root not in _sys.path:
            _sys.path.insert(0, root)
        from hub import resolve_engine as _hre
        for probe in ("wb", "qd", "workbuddy", "qoder", "wb-proxy", "qoder-proxy", "WB", "zz"):
            line("normalize_engine(%r) == hub.resolve_engine(%r)" % (probe, probe),
                 normalize_engine(probe) == (_hre(probe) or ""))
    except Exception as exc:
        print("    [SKIP] 无法 import hub（%r）" % (exc,))
    print("")

    print("=" * 72)
    print("自检结果: %s" % ("ALL PASS" if ok else "有 FAIL —— 见上面 [FAIL] 行"))
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    # Windows 控制台默认 GBK，而本文件通篇中文 —— 不显式切 UTF-8 会输出乱码，
    # 自检结果就没法贴进验收记录（hub/server.py:153-155 用的是同一套写法）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(_selftest())
