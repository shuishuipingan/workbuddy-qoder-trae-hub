# hub/tests —— 融合层（双引擎 + 聚合 + 门户）的验收套件

## 怎么跑

    python hub/tests/run_all.py                 # 全部（cwd 任意，路径全部基于 __file__）
    python hub/tests/run_all.py smoke           # 子串过滤
    python hub/tests/run_all.py --strict        # skip / 缺失也算红（CI 与最终验收用）
    python hub/tests/_test_smoke_dual_engine.py # 单套件直跑（调试判据时用）

环境：只要标准库 + PATH 上有 python。**不需要第三方依赖、不需要浏览器、不需要外网。**
环境变量：`HUB_TESTS_TIMEOUT`（单套件超时秒数，默认 300）。

## 六个套件各判什么

| 套件 | 层 | 判什么 | 检查数 |
|---|---|---|---|
| `_test_engine_static.py` | 静态契约 | wb 16 个 `wb_*.py` / qd 9 个 `qoder_*.py` + `baseprompt.json` + `umid/runtime-info`；**搬入件与原件逐字节相同**；两份 `derive_id` 函数体逐字节相同且仍是 md5 派生；hub 的版本表/默认端口/别名表；`server.py` 的「串行启动 + 逐个等 health」不许改回并行 | 21 |
| `_test_import_isolation.py` | 导入隔离 | 两个引擎目录的模块名交集为空；同进程子进程真 import 两边入口，每个 `wb_*`/`qoder_*` 模块都解析到**自己目录**的文件、零顶替；导入过程不写仓库（前后快照比对） | 11 |
| `_test_smoke_dual_engine.py` | 端到端 | 整树复制到临时目录后起 `python -m hub.server --wb-port X --qd-port Y`：两个 `/health` 都应答、形状正确（**qd 带 `service`/`version`，wb 不带**——这是区分两引擎的唯一可靠特征）、两边 payload 不相同（防「两个引擎抢同一端口」回归）；收进程后端口重新空闲；仓库引擎目录零写入 | 14 |
| `_test_aggregator.py` | 聚合器 | 公开函数在**引擎不可达**时必须返回 None/部分结果而不抛（用不可达目标逐个探测；形状猜错记 SKIP，不冤枉作者） | 9 |
| `_test_portal.py` | 门户 | `GET /` 的字节 **== admin/index.html**；`GET /api/hub/health` 是聚合 JSON；`/css/...` 与 `/admin/css/...` 指向同一静态根；`/admin/` 书签兼容；收进程后端口释放 | 11 |
| `_test_harness_skip.py` | 元层 | 把测试自己搬进一棵「空 hub」再跑编排器，证明**依赖缺失时报 `[SKIP]` 而不是崩**、默认 skip 不算红、`--strict` 时升级成红；顺带自证 `free_port()` 永不返回 8789/8790/8791 | 11 |

## 口径（四个编排器统一，便于拼总账）

- 套件自报：`SUMMARY: TOTAL N checks, P passed, F failed, S skipped`；编排器汇总同一组数字。
- 退出码：`0` 全过 / `1` 有红 / `2` 无匹配套件或环境不可用（防「0 suites → exit 0」的假绿）。
- `[SKIP]` 与 `[MISSING]` 不抛异常；默认不计红但会在汇总里显示 `skipped=N`，`--strict` 时判红。

## 三条实测教训（都写进了判据，别删）

1. **判据自己也会猜错形状**：`hub_health(ports)` 收的是字典，我先按 int 喂 → `AttributeError: int has no attribute get`。
   这错在判据不在产品，所以探测改成「第一候选定裁决、其余候选记 INFO」，并把形状不匹配（TypeError / `.get` / `.items`）识别为「我猜错」。
2. **测试隔离要连静态根一起搬**：门户的静态根默认是 `<repo>/admin`（`hub/core/portal.py:53-54`），只复制 `hub/` 会让 `GET /` 必然 404 —— 那是隔离做漏了，不是门户缺陷。
3. **`copy_tree` 的 skip 必须同时过滤文件名**：只过滤目录名时，「把元套件排除掉」的意图静默失效 → 沙箱里又跑了一次元套件 → 无限递归。现在 `skip_names` 同时也作用于文件，并且沙箱里还有一道 `os.remove` 兜底。

## 边界（不在这套判据里的东西）

- 浏览器级验证（Playwright / 真实渲染）不在内：那是 `admin/tests` 与 `wb/tests/_mobile_check.py` 的地盘。
- 不碰线上实例与默认端口：`free_port()` 硬排除 8789/8790/8791，端到端冒烟全部跑在临时副本里。
- 门户目前的接入方式是 `python -m hub.core.portal --port N`；`hub/server.py` 若将来加 `--portal-port`，`_test_portal.py` 的探测会**自动改用它**（P01 已按两种方式写）。
