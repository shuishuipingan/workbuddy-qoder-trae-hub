# 00 · 后台 Web 合并方案（决策书 + 接口契约）

> 项目：把 `合并damo\wb`（WorkBuddy2API-Hub v1.6.17）全面融合进 `合并damo\qoder-proxy`（Qoder2API-Hub v1.3.4）
> 决策人：Lead（小代码酱）｜集团：萌喵重工｜状态：**已定稿，可开工**
> 本文是**唯一权威契约**：任何人写后台页面，以本文的模块边界与函数签名为准。

---

## 一、现状：两份单体看板，必须重构而不是拼接

| | wb（WorkBuddy） | qoder（Qoder） |
|---|---|---|
| 看板文件 | `dashboard.html` **405 KB / 7699 行** | `dashboard.html` **190 KB / 3934 行** |
| 页面 | 账号网关 / 数据指标 / 网关设置 / 运行日志 | 网关总览 / 数据指标 / 运行日志 / 设置 |
| 样式 | 内嵌 `<style>`（:7-487） | 内嵌 `<style>` |
| 脚本 | 内嵌三块 `<script>`（含 1031 行 i18n 字典） | 内嵌脚本 |
| 接口调用 | 收敛在 `getJSON`/`postJSON` **74 处** + 4 处裸 fetch | 同类包装函数 |
| 外链 | **零外链**（离线可用） | **零外链** |

**结论：直接拼两份 HTML = 11600 行单体怪物，维护性归零。**
必须做的是**重构式合并**：抽出共同骨架（设计系统 + 外壳 + 请求层），两侧能力变成可插拔视图。

---

## 二、决策（4 条，全部已拍板）

### D1. 新建 `admin/` 作为**唯一**后台前端工程，零构建、零依赖
- 位置：`qoder-proxy/admin/`
- 技术约束：**原生 ES Module + 原生 CSS 变量，不用任何框架、不用任何 CDN、不用打包器**。
  理由：项目哲学是「纯标准库 + 双击即启 + 拔网线能用」，后台必须同样离线自足。
- 旧的两份 `dashboard.html` **不删除**，作为行为参照物保留（`legacy/` 归档），迁移完成后再删。

### D2. 后端把静态资源与 API 分开：`/admin/*` 静态、`/api/*` 数据（旧路径保留兼容别名）
- `GET /` → 重定向到 `/admin/`；`GET /admin/*` → 从 `admin/` 目录直吐（带 `no-store`）。
- 现有 55~65 条管理接口**全部保留原路径**（兼容旧客户端与旧书签），新前端统一走 `/api/*` 别名层，
  由后端做一次转发/别名映射（`/api/accounts` → `/accounts`，`/api/usage/recent` → `/usage/recent`…）。
  这样**前端只依赖一套路径**，两侧后端能力合并时不用改前端。

### D3. 视图按「能力域」切，不按「产品」切
不再有「WorkBuddy 页」「Qoder 页」，只有 7 个能力域视图：

| 视图 | id | 负责内容 | 数据来源（合并后） |
|---|---|---|---|
| 总览 | `overview` | 双引擎出口状态、账号池健康、调度器、今日用量、实时流水摘要 | `/api/health` `/api/accounts` `/api/scheduler` `/api/usage` |
| 账号池 | `accounts` | 双引擎账号列表、导入/OAuth/PAT/桌面扫描、启停、出口绑定、额度 | `/api/accounts` 系列 |
| 模型清单 | `models` | 双方模型目录合并、能力徽标、上下文/输出上限、峰谷价与等价价 | `/api/models` `/api/pricing` |
| 数据指标 | `metrics` | KPI、按账号/按 Key/按模型透视、时序、TTFT/速度/缓存命中 | `/api/usage/*` |
| 请求流水 | `logs` | 最近请求逐行、错误流水、运行日志、筛选导出 | `/api/usage/recent` `/api/logs` |
| 设置 | `settings` | 限额四护栏、定价刷新、本地网络工具、面板密码、API Key、调度开关 | `/api/settings` 系列 |
| 诊断 | `diag` | 自检、版本、代理槽、桌面凭据扫描（后续轮次） | `/api/diag` |

### D4. 三态设计：**正常态 / 空态 / 未登录态**，一个都不能少
新后台第一次打开必然是「未登录」；合并后的账号池第一次必然是「空」。
两者都必须有专门设计的界面，而不是白屏或报错。

---

## 三、目录结构（`qoder-proxy/admin/`）

```
admin/
├── index.html              外壳（唯一 HTML）
├── css/
│   ├── tokens.css          设计令牌：颜色/字号/间距/圆角/阴影/动效（亮暗双主题）
│   └── app.css             组件与布局（卡片/表格/按钮/徽章/弹层/骨架屏/响应式）
├── js/
│   ├── app.js              引导：视图注册、路由、主题、刷新调度、全局错误兜底
│   ├── api.js              统一请求层：鉴权、错误归一、端点表、轮询、去重
│   ├── store.js            轻量响应式状态（get/set/subscribe，无依赖）
│   ├── i18n.js             中英字典 + 切换（`t(key, vars)`）
│   ├── components/ui.js    通用组件与格式化
│   └── views/
│       ├── overview.js     总览
│       ├── accounts.js     账号池
│       ├── models.js       模型清单
│       ├── metrics.js      数据指标
│       ├── logs.js         请求流水 / 运行日志
│       └── settings.js     设置
└── dev/
    ├── serve.py            零依赖开发服务器：静态 + 反代（`--backend` / `--mock`）
    └── mock/*.json         离线演示数据
```

---

## 四、接口契约（**必须逐字实现**，跨模块调用只认这些签名）

### 4.1 `js/api.js`

```js
export const ApiError = class extends Error {};       // .status  数字；.body 原文；.kind
export const api = {
  get(path, params),        // → Promise<any>；params 为对象，自动 querystring；path 走 /api/*
  post(path, body),         // → Promise<any>；body 为对象，JSON 提交
  status(),                 // → Promise<{panel_password_required, authenticated, ...}>  GET /api/panel/status
  login(password),          // → Promise<{ok:boolean, error?:string}>                  POST /api/panel/login
  logout(),                 // → Promise<void>                                          POST /api/panel/logout
  probe(),                  // → Promise<boolean>  探活 /api/health（免鉴权）
  base(),                   // → string 当前后端基址（默认 ''，同源）
  setBase(url),             // 供 `?backend=` 覆盖（开发/多实例）
  endpoints,                // → 端点表：{ overview: {health:'/api/health', ...}, ... } 只读
};
// 约定：401 一律抛 ApiError{status:401,kind:'unauthorized'}，由 app.js 统一切到登录态。
// 约定：鉴权走**自持令牌**（实测，源码 wb_proxy.py:7448-7456）：
//   login() 成功后把返回的 token 存 sessionStorage，之后每个管理请求带 header `X-Panel-Token: <token>`。
//   不是 Cookie 会话——不带令牌一律 401。详见 docs/merge/02-接口实测样本.md §一。
```

### 4.2 `js/store.js`

```js
export const store = {
  get(key, fallback),            // 读
  set(key, value),               // 写（浅比较，变化才通知）
  update(key, fn),               // 函数式写
  subscribe(key, handler),       // → unsubscribe 函数
  snapshot(),                    // → 冻结副本（调试用）
};
```

### 4.3 `js/i18n.js`

```js
export const i18n = {
  t(key, vars),                  // 'accounts.title' → '账号池'；vars 做 {name} 插值
  lang(),                        // 'zh' | 'en'
  setLang(code),                 // 切换并持久化到 localStorage['hub-lang']，触发 store 'lang' 变更
  dicts,                         // 只读字典对象
};
export const t = i18n.t;         // 便捷别名
```

### 4.4 `js/components/ui.js`

```js
// 全部返回 HTMLElement 或 string（标注 [S] 的返回字符串，便于拼表格）
export function h(tag, attrs, ...children);     // 迷你 DOM 构造器：attrs 支持 class/style/on*/data-*
export function card({title, subtitle, actions, body, className});   // 卡片
export function table({columns, rows, empty, rowKey, onRowClick});   // 数据表（列定义见下）
export function badge(text, tone);              // tone: 'ok'|'warn'|'err'|'dim'|'info'
export function statCard({label, value, unit, hint, trend, tone});   // KPI 卡片
export function skeleton(lines);                // 骨架屏
export function emptyState({icon, title, desc, action});             // 空态
export function errorState({title, desc, retry});                    // 错误态
export function toast(message, tone, ms);       // 右下角提示
export function modal({title, body, actions, width});                // 弹层，返回 {close()}
export function confirmDialog({title, desc, danger});                // → Promise<boolean>
export function tabs({items, active, onChange});                     // 页签
export function segmented({items, value, onChange});                 // 分段控件
export function switchBox({checked, onChange, label});               // 开关
export function copyButton(text, label);                             // 复制按钮（带成功反馈）
export function formatNumber(n, digits);        // 1234 → '1,234'；大数自动 K/M
export function formatTokens(n);                // 1234567 → '1.23M'
export function formatMoney(n, currency);       // '¥' / '$'
export function formatMs(ms);                   // 1234 → '1.23 s'
export function formatTime(ts, mode);           // mode: 'rel'|'abs'|'hm'
export function escapeHtml(s);
// 列定义：{ key, label, width, align, render(row, index) → string|HTMLElement, sortable }
```

### 4.5 视图契约（`js/views/*.js`）

每个视图**必须**默认导出一个对象：

```js
export default {
  id: 'overview',                       // 与文件名一致，app.js 注册用
  title: '总览',                         // 导航标题（经 i18n）
  icon: '◎',                            // 单字符图标（不引外链字体）
  order: 10,                            // 导航排序
  async load(ctx),                      // 拉数据 → 返回数据对象（可 throw）
  render(root, data, ctx),              // 首次渲染（root 为空容器）
  update(root, data, ctx),              // 可选：轮询刷新时只更新差异，缺省则调 render
  destroy(root),                        // 可选：离开页面清理定时器/监听
};
// ctx = { api, store, t, ui, schedule(fn, ms) → id, clear(id) }
```

---

## 五、样式契约（`css/tokens.css` 必须导出的变量）

| 组 | 变量 |
|---|---|
| 主题 | `--bg` `--bg-elev` `--bg-sunken` `--fg` `--fg-dim` `--fg-mute` `--line` `--line-strong` |
| 品牌 | `--accent` `--accent-fg` `--accent-soft` |
| 语义 | `--ok` `--warn` `--err` `--info` 及各自的 `*-soft` |
| 尺度 | `--r-sm/md/lg/full` `--sp-1..8` `--fs-xs/sm/md/lg/xl/2xl` `--lh-tight/normal` |
| 阴影 | `--shadow-sm/md/lg` |
| 动效 | `--dur-fast/base/slow` `--ease` |
| 布局 | `--sidebar-w` `--header-h` `--page-max` |

暗色为默认（`data-theme="dark"`），亮色走 `html[data-theme="light"]` 覆盖；系统跟随由 `app.js` 解析
`prefers-color-scheme` 后写 `localStorage['hub-theme']`，并在 `<head>` 内联抢跑脚本避免首屏闪错。

---

## 六、开发与验收

- **开发服务器**：`python admin/dev/serve.py --port 8899 --backend http://127.0.0.1:8789`
  → 静态服务 `admin/` 并把 `/api/*` 反代到 `--backend`（去掉 `/api` 前缀）。
  `--mock` 时不反代，直接吃 `admin/dev/mock/*.json`，用于离线演示与自动化测试。
- **隔离验证**：需要真网关时，用 `合并damo\wb` 起一个**独立实例**（自定义 `ACCOUNTS_DIR` /
  `WB_PROXY_USAGE_DIR` / `--port` / `--panel-password`），**绝不触碰线上 8789**。
- **自动化测试**：Node 无头断言（源码契约校验 + DOM 桩渲染），纳入 `admin/tests/run_all.py`；
  浏览器级验证用 Playwright 走 `_mobile_check.py` 同款思路（可选，需装依赖）。

---

## 七、分工与写入边界（本轮）

| 成员 | 中文名 | 独占写入范围 |
|---|---|---|
| 奶盖 | `nai-gai` | `admin/index.html` `admin/css/*` `admin/js/app.js` `admin/js/api.js` `admin/js/store.js` `admin/js/i18n.js` `admin/js/components/ui.js` `admin/README.md` |
| 阿喵 | `a-miao` | `admin/js/views/overview.js` `admin/js/views/accounts.js` `admin/js/views/models.js` |
| 团子 | `tuan-zi` | `admin/js/views/metrics.js` `admin/js/views/logs.js` `admin/js/views/settings.js` `admin/dev/serve.py` `admin/dev/mock/*` |
| 布丁 | `bu-ding` | `admin/tests/*` `docs/merge/04-验收与测试矩阵.md` |
| 柚子 | `you-zi` | `docs/merge/03-融合映射与迁移清单.md` `docs/merge/05-工程脚手架.md` |
| Lead | 小代码酱 | 本决策书 · 后端融合总纲 · 总装与验收 |

**铁律**：只写自己那一格；跨模块只调 §4 的签名；不得新增第三方依赖；不得触碰 `合并damo\wb` 与线上实例。


---

## 十、附录 · 样式类名契约（第 2 轮补定 · **权威**）

> 起因：实测发现三个视图自造了 **65 个** 前缀类名（`ov-*` 12 个 / `acc-*` 32 个 / `mdl-*` 21 个），
> 而 `app.css` 只提供通用工具类 → 两边名字对不上，KPI 卡片直接堆成一列（实测：`.ov-pool` 的
> `getComputedStyle().display === 'block'`，8 张卡各占 940px 宽纵向堆叠）。
> 这是契约缺口，不是谁的错 —— 本节把它补齐。

### 10.1 视图**只准**使用下列通用类（app.css 必须全部提供）

| 组 | 类名 |
|---|---|
| 布局网格 | `.grid`（配 `.grid--kpi` / `.grid--2` / `.grid--3`） |
| 页头 | `.page-head` `.page-head__title` `.page-head__sub` `.page-head__actions` |
| 工具条 | `.toolbar` `.toolbar__group` `.toolbar__spacer` |
| 键值对 | `.kv` `.kv__row` `.kv__k` `.kv__v` |
| 时刻/槽位 | `.slots` `.slot` `.slot__hour` `.slot__label` |
| 状态点 | `.dot` + `.dot--ok` `.dot--warn` `.dot--err` `.dot--dim` |
| 进度条 | `.bar` `.bar__fill` `.bar__num` |
| 徽标组 | `.badges` |
| 表格宿主 | `.table-host` |
| 说明脚注 | `.note` `.foot` |
| 空态容器 | 用 `ui.emptyState()`，不要自造 |

**新规则**：视图不得自造 `<前缀>-*` 布局类名。确有新结构 → 先在本文登记，由 app.css 统一实现。

### 10.2 过渡垫片（本轮必须提供，下一轮清理）

为不阻塞已落盘的三个视图，`app.css` 追加一段**兼容垫片**，把已存在的 65 个类名映射到 §10.1 的通用类：

```
ov-grid → .grid            ov-pool / ov-kpi → .grid.grid--kpi
ov-kv / ov-kv-row → .kv / .kv__row    ov-kv-label → .kv__k    ov-kv-value → .kv__v
ov-slots / ov-slot / ov-slot-hour / ov-slot-label → .slots / .slot / .slot__hour / .slot__label
ov-actions → .toolbar / .page-head__actions

acc-toolbar / acc-toolbar-actions / acc-search → .toolbar / .toolbar__group
acc-summary / acc-quota / acc-quota-bar / acc-quota-fill / acc-quota-num → .kv / .bar / .bar__fill / .bar__num
acc-name / acc-name-main / acc-name-sub / acc-health / acc-daily / acc-row-actions → 表格单元格内的内联块（.cell-* 见下）
acc-table-host / mdl-table-host → .table-host
acc-paths / acc-path / acc-path-label / acc-path-desc / acc-empty / acc-empty-storage
acc-oauth* / acc-pat / acc-desktop* / acc-json* / acc-credits* → 弹层内的 .kv/.note 组合

mdl-toolbar / mdl-search → .toolbar / .toolbar__group
mdl-summary → .note          mdl-table-host → .table-host
mdl-name / mdl-id / mdl-ctx / mdl-desc / mdl-link / mdl-note / mdl-detail
mdl-dk / mdl-dv / mdl-dvv → .kv/.kv__row/.kv__k/.kv__v 与 .note
mdl-badges / mdl-efforts → .badges
mdl-price / mdl-price-live / mdl-equiv / mdl-disabled-reason → .cell-* 文本变体
```

垫片段落必须以注释标明 `/* [shim] 第 2 轮遗留类名映射，下一轮删除 */`，便于一次性移除。

### 10.3 单元格文本变体（视图表格内需要，app.css 需提供）

`.cell-main` `.cell-sub`（主/次两行）、`.cell-mono`（等宽 id）、`.cell-num`（右对齐数字）、
`.cell-mute`（次要说明）、`.cell-actions`（行内按钮组）。


---

## 十二、引擎作用域三档语义（新目标首轮定稿 · **权威**）

> 老板原话：「**默认就是两个合并的**，然后 **wb 就是单独的 wb**，然后 **qoder 就是单独的 qoder**」。
> 实测现状缺口：切换器有三档，但「默认」**不是合并** —— `/api/health` 返回的是 wb 引擎的响应（无 `service` 字段）。本轮把语义补正。

### 12.1 三档定义

| 档位 | `localStorage['hub-engine']` | 请求落点 | 含义 |
|---|---|---|---|
| **合并（默认）** | `''`（缺省） | `/api/hub/*` | 两个引擎**并排展示** |
| WorkBuddy | `'wb'` | `/api/wb/*` | 只看 WorkBuddy |
| Qoder | `'qd'` | `/api/qd/*` | 只看 Qoder |

**已有一条现成资产**：`/api/hub/*` 命名空间**豁免引擎作用域改写**（`admin/js/api.js:506` 有注释说明），所以合并态天然走聚合端点，不会和作用域改写打架。

### 12.2 逐视图的合并态行为（**这是本轮最容易做错的地方**）

不是所有视图都该"合并" —— **有些东西本质上属于单个引擎**。逐条定死：

| 视图 | 合并态 | 依据 |
|---|---|---|
| 总览 | 两引擎的**健康 / 用量并联**成两块，各自带 engine 标 | 两块信息同构，可并排 |
| 福利中心 | **合并**（已实现，走 `/api/hub/rewards`） | 上游两边都是「签到/福利」，语义同构 |
| 账号池 | **并集表**，每行带 engine 徽标 | 账号是各自引擎的账号，并集才完整 |
| 模型清单 | **并集**，已有 `engine` 标 | 同上 |
| 数据指标 | **合并统计**（两引擎用量相加）**+ 一个"只看某引擎"的快捷筛选** | 合并看总量，单看要能下钻 |
| 请求流水 | **按时间合并排序**，每行带 engine 标 | 两条流水本质是同一件事 |
| **设置** | ⚠️ **不合并** —— 设置天然是**单引擎**的（面板密码/API Key/限额各自独立）。合并态下显示**引擎选择器**，而不是把两边设置混在一起 | 混在一起会让人误以为改一处影响两边 |

### 12.3 三条硬要求

1. **合并态下每一行/每一块都要看得出属于哪个引擎**（徽标或分组）—— 否则用户分不清，合并就退化成"混成一锅"。
2. **某个引擎不可达时，另一个仍要完整显示**；不可达的那个显示可读原因，**不是整体报错**（这是 `{wb, qd, errors}` 三件套的形状决定的，也是 rewards 已经验证过的模式）。
3. **缺字段一律 None 不当 0**（沿用既有口径）。

### 12.4 判据（布丁）

- `scope=''` 时请求落在 `/api/hub/*`；`'wb'`/`'qd'` 时落在 `/api/{wb,qd}/*`
- 合并态视图里**每条数据都带 engine 标识**（对照 12.3 第 1 条）
- 设置视图在合并态下**不合并**（对照 12.2 最后一行）
