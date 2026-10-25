# -*- coding: utf-8 -*-
"""hub —— WorkBuddy × Qoder 双引擎网关中枢（融合层）。

设计要点（见 docs/merge/01-后端融合总纲.md）：

  * 两个引擎的模块**原样保留**在 hub/engines/{wb,qd}/ 下，内部实现一行不改；
    它们的模块名前缀天然隔离（wb_* / qoder_*），实测可同进程共存、零重名冲突。
  * 融合只发生在 hub/core/（共享内核）与 hub/server.py（进程编排）两层。
  * 每个引擎的数据目录默认落在**自己引擎目录内**（hub/engines/<e>/accounts|usage），
    互不干扰，也便于单独起停。

版本：单一真源在这里。
"""

HUB_VERSION = "1.0.0"
# 产品名：三家合一个（第 100 轮起把 Trae 也并进来）。日志前缀与启动横幅都用它。
HUB_NAME = "WorkBuddy x Qoder x Trae-hub"

# ★ 引擎注册表 —— **唯一的真源**（第 100 轮统一）。
#   原来这台机器上散着 5 份"引擎是谁"的名单（aggregator/router/paths/server/hub），
#   加第三个引擎时它们必须一起改，漏一处就是"某个端点永远少一家"。
#   dir   = hub/engines/<dir>
#   entry = 该目录下的主程序文件名
#   module= 进程内 import 的模块名（三家都用扁平模块名，前缀天然隔离）
ENGINES = ("wb", "qd", "trae")
ENGINE_META = {
    "wb": {
        "label": "WorkBuddy", "dir": "wb", "entry": "wb_proxy.py",
        "module": "wb_proxy", "port": 8811, "version": "1.7.32", "icon": "wb",
        # /health 里的身份指纹：wb 没有 service 字段（靠 api_key_required 认）
        "service": None,
    },
    "qd": {
        "label": "Qoder", "dir": "qd", "entry": "qoder_proxy.py",
        "module": "qoder_proxy", "port": 8812, "version": "1.7.1", "icon": "qd",
        "service": "qoder-proxy",
    },
    "trae": {
        "label": "Trae", "dir": "trae", "entry": "trae_proxy.py",
        "module": "trae_proxy", "port": 8813, "version": "1.4.1", "icon": "trae",
        "service": "trae2api-hub",
    },
}
ENGINE_VERSIONS = {name: meta["version"] for name, meta in ENGINE_META.items()}
ENGINE_LABELS = {name: meta["label"] for name, meta in ENGINE_META.items()}

# 引擎别名 → 规范名（兼容 --engines wb、workbuddy、qoder、qd 等写法）
ENGINE_ALIASES = {
    "wb": "wb", "workbuddy": "wb", "wb-proxy": "wb",
    "qd": "qd", "qoder": "qd", "qoder-proxy": "qd",
    "trae": "trae", "trae2api": "trae", "trae2api-hub": "trae", "trae-proxy": "trae",
}

# 默认端口与启动器（start-hub.*）保持一致：8811/8812（引擎）+ 8817（融合面）。
# 为什么不是 8789/8790：那正是操作者常跑的单引擎实例端口，模块默认值撞上去会让
# 「python -m hub.server」直接起不来（或被健康检查误判）。
# 旧默认 8789/8790/8791 仍然在 harness.free_port 的排除表里（线上实例可能还在用）。
# 从注册表派生：加引擎只需要改 ENGINE_META 一处。
DEFAULT_PORTS = {name: meta["port"] for name, meta in ENGINE_META.items()}
HUB_PORT = 8817


def resolve_engine(name):
    """把用户写的引擎名规范成 'wb' / 'qd'；未知返回 None。"""
    return ENGINE_ALIASES.get(str(name or "").strip().lower())
