# 13 · 统一 Key（一个地址吃三类 key）

> 目标（老板原话）：**"就算是单独的 wb 和 qoder 里面的 key 也可以用一个 api 调用然后走对应的单独的 qoder 等；合并里面的 key 就是合并的一个就能调用所有的。"**

## 一、一句话规则

**你拿哪把 key 来，就走哪条路。**

| 你手上的 key | 走到哪 | 能看到多少模型 |
|---|---|---|
| **合并 key**（门户「合并路由」卡里登记的） | 智能路由：`wb:cn → qd:cn → wb:intl → qd:intl` | 全部（两引擎并集、同名去重） |
| **WorkBuddy 的 key**（wb 面板里那把） | 只走 wb | 只有 wb 的模型 |
| **Qoder 的 key**（qd 面板里那把） | 只走 qd | 只有 qd 的模型 |
| 都不是 | **网关当场 401**（带三类 key 的提示） | — |

三类 key 用的是**同一个地址**：

```
Base URL:  http://127.0.0.1:8817/api/v1
Header:    Authorization: Bearer <上面任意一把 key>
```

## 二、为什么以前"不像一个项目"

旧实现里，门户只是把客户端的 `Authorization` **原样转发**给引擎。于是：

* 合并 key 必须在 wb 与 qd 的 API Key 列表里**各登记一遍**，否则引擎 401；
* 引擎自己的 key 只能在 `/api/wb/…`、`/api/qd/…` 这两个带前缀的路径下用，
  直接用 `/api/v1` 会被送进默认引擎（wb），qd 的 key 到那里就是 401。

结果就是"两套东西各开一个门"。本轮把这一步补上。

## 三、实现（`hub/core/keys.py`）

```
engine_keys(engine)   读 engines/<engine>/accounts/settings.json 的 api_keys（按 mtime 缓存）
merged_keys()         读 hub/settings.json 的 merged_keys
classify(key)         → ("merged" | "wb" | "qd", entry) | (None, None)
upstream_key(engine)  给该引擎挑一把启用的 key（优先 realm 匹配 → 通吃 → 任意）
```

门户里的三条落点：

1. **路由**（`_handle_api`）：无引擎前缀时，先看合并 key（走智能路由）；
   不是合并 key 就看它属于哪个引擎，把请求送进那个引擎；都不认识且是推理路径 → 401。
2. **换 key**（`_proxy_once(upstream_key=…) / _proxy`）：合并 key 打引擎前**换用该引擎
   自己的 key** —— 这样合并 key 不必在引擎里重复登记；引擎自己的 key 则原样转发
   （尊重它原本的 realm 绑定与 models 白名单）。
3. **看板**（`GET /api/hub/keys` + 设置页「统一 Key」卡）：三类 key 各多少把、
   分别走哪条路、当前 API 地址一键复制（只回掩码）。

## 三·补 · 在面板里增删（一个地方管三类 key）

面板「设置 → 统一 Key」现在是**可写**的，不用再切引擎档位去各自面板里找：

| 类别 | 新增 | 删除 | 写到哪里 |
|---|---|---|---|
| 合并 key | 名称 + 密钥（留空自动生成）+ **hub 面板口令** | ×（立刻 401） | `hub/settings.json` · `merged_keys` |
| wb key | 名称 + 出口 + 模型白名单（可空） | × | 该引擎的 `accounts/settings.json` · `api_keys` |
| qd key | 同上 | × | 同上 |

两个实现细节：

* **删 key 的载荷是「整表 − 该 id」+ `deleted_api_key_ids`**：wb 认后者（upsert 退役），
  qd 只认前者（整表替换），两个字段一起发才对两边都成立（第 54 轮的结论，这里沿用）。
* **路由卡不再携带 `merged_keys`**：早先它把 key 表和路由偏好一起整表替换，
  现在 key 表搬走了，再带就是"保存一次路由把 key 全抹掉"。这一条我真实点了一次保存验证过。

## 三·补二 · 数据根统一

设了 `HUB_DATA_ROOT` 时，**融合层自己的配置也搬进去**（`<root>/hub-settings.json`），
与引擎的 `accounts/usage` 一致；`HUB_SETTINGS` 仍然优先级最高（测试与多实例隔离用它）。
判据：U40（跟随数据根）/ U41（显式优先）。
原因很实际：隔离实例里曾经出现「引擎读临时目录、面板读仓库」——同一个"数据根"只搬了半个应用。

## 四、契约（判据钉住的部分）

* `F04b`：未登记的 key 在推理路径上 → **401**（不再伪装成引擎 503）；
* `F04c`：401 的 body 必须带 `error.code=invalid_api_key` 与 `known_keys`；
* `F04d`：**引擎自己的 key 走同一个地址**能落到自己的引擎；
* `F05b`：关掉智能路由时，合并 key 仍应落默认引擎（不能因为关了路由就把自己锁死）；
* 合并 key 的 `/v1/models` 返回 `merged: true`；引擎 key 返回该引擎的清单。

## 五、一个仍然存在的边界

合并模式下，门户用**该引擎的一把 key** 去请求。如果那把 key 自己绑了区
（例如 qd 的"国内"key 绑 `cn`），那么该 key 的区限制会照常生效 ——
这不是 bug：**key 的绑定是引擎自己的策略**，门户只负责"用一把能用的 key 把请求送进去"。
要完全不受影响，就在引擎里留一把**不绑区**的 key（默认的"合并通吃"就是这种）。
