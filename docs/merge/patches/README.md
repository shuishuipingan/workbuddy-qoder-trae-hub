# 本地补丁集（上游同步用）

这两个目录里的 `.patch` 是**融合仓相对上游的全部本地改动**。它们的用途只有一个：
让「上游更新」这件事可以**一键重放**，而不是每次手工三方合并。

配套工具：`hub/tools/sync_upstream.py`（推荐用法：`python hub/tools/sync_upstream.py --wb`）。

---

## 一、两个目录的分工

| 目录 | 应用对象 | 内容 |
|---|---|---|
| `wb/` | **所有** wb 副本（镜像 / vendored / 用户副本） | 融合仓自己的功能补丁 |
| `user-local/` | **只有**用户副本（`Desktop/wb`、`Desktop/wb-proxy`） | 本机部署定制（端口 8789 等），不进镜像 |

应用顺序（工具自动执行）：先 `wb/*.patch`（按文件名序），再 `user-local/*.patch`。

## 二、当前补丁清单

### wb/

| 文件 | 内容 | 判据 |
|---|---|---|
| `01-auto-realm-proxy.patch` | 区域智能换区：`AUTO_REALM_ENABLED` / `realm_candidates()` / `_open_upstream_auto()` / 三协议接线 / `X-WB-Realm(-Reason)` 响应头 / `record_usage.realm_reason` / `/realm` 扩展 / `runtime_settings_view` 暴露 | `tests/_test_auto_realm.py` |
| `02-auto-realm-settings.patch` | `wb_settings.realm_routing()` / `set_realm_routing()`（默认开、默认 cn 优先） | 同上 |
| `03-readme-suite-count.patch` | README 套件计数 146→147（因为本仓新增了一个套件） | `tests/_test_release_engineering.py` |
| `04-auto-realm-tests.patch` | 新增 `tests/_test_auto_realm.py`（18 项） | 自身即判据 |

### user-local/

| 文件 | 内容 |
|---|---|
| `01-port-8789.patch` | 把默认端口 8788 改成 8789（`wb_proxy.py` 的 docstring + `--port` 默认值、`wb_agents.py` 示例、`README.md` 文档共 18 处） |

> 端口补丁是**本机部署定制**，刻意不进镜像：镜像与 vendored 保持上游默认 8788，
> 而用户副本默认 8789（他们的实例就跑在 8789）。两边各自自洽，互不污染。

## 三、增删补丁

**加一条**：在对应目录里放 `NN-名字.patch`（git 标准格式，`git diff` 产物）。
新文件补丁要带 `new file mode` 头（缺了它 `git apply` 会报 `dev/null: No such file`，实测踩过）。

**改一条**：直接编辑——但改完必须自证：

```bash
# 用「干净上游 + 全部补丁」重建，结果必须与镜像逐字节一致
python hub/tools/sync_upstream.py --wb --check
```

**删一条**：连同它引入的代码一起删（补丁是声明式的，不会自动撤销历史应用）。

## 四、不变式（sync_upstream.py 每次都会校验）

```
① 镜像 合并damo/wb  ==  vendored hub/engines/wb     （.py + dashboard.html 逐字节）
② 用户副本            ==  镜像 + user-local 补丁      （逐字节）
③ 上游 origin/main    ==  快进后的本地 main          （--ff-only）
```

三条任何一条不成立 → 工具以 `[FAIL]` 退出并指出是哪几个文件。

## 五、典型场景

```bash
# 日常：上游更新了，一条命令同步 + 校验 + 跑测试
python hub/tools/sync_upstream.py --wb --test

# 只想看看现状对不对（不写盘）
python hub/tools/sync_upstream.py --wb --check

# 上游改了和本地补丁同一段代码 → 工具会报「补丁应用失败」
#   处理：手工合并那一段 → 重新生成该补丁 → 再跑 --check 自证
```

## 六、为什么不用 git submodule / fork？

融合仓要的是**两个上游的源码原样在场**（逐字节可比对），加上一层薄薄的本地改动。
submodule 会在目录里留 `.git`，破坏「镜像 = 上游快照」这个可以直接 `diff` 的性质；
fork 则要维护两条分支的 rebase。补丁集 + 校验工具是最小的可信机制：
**改动全部可见（就是这几个 .patch）、可重放（一条命令）、可自证（逐字节校验）。**
