# 任务：接入说明视图（api）

文件：`src/views/api.tsx`（当前是占位文件，直接替换）。

## 内容（等价重写，不加新功能）
1. **一个地址**卡：显示 `location.origin` + `/api/v1`，带复制按钮（`navigator.clipboard`，失败时 toast 提示手动复制）。
2. **三类 key 走哪条路**：合并 key / 引擎 key。数据用 `hubKeys()`（返回 `{ merged, rules }`）。
   没有合并 key 时显示引导（键 `health.noMergedKey`）+ 「去设置」按钮（`navigate("/settings")`）。
3. **可直接粘贴的 curl 示例**：代码块（`font-mono text-xs`、`bg-surface-2`），带复制按钮。
4. **引擎端点一览**：三家名字 + 版本 + 在线状态，取自 `hubHealth()`。

## 可用键（举例）
`health.mergedKeys` `health.noMergedKey` `health.engineKeys` `common.copy` `common.copied` `common.copyFailed`
`engine.label` `app.tagline` `nav.api` `common.retry` `state.errTitle`

# 通用约束（每个视图都适用）

工程根：`<workspace>\WorkBuddy x Qoder-hub\admin\app`
技术栈：React 19 + TS 5.9 + Tailwind v4 + Radix(shadcn 模式) + TanStack Query。

## 动手前先读
1. `src/views/accounts.tsx` —— 列表 + 写操作的**模板**（作用域 tab、搜索、三态、乐观更新、确认对话框）。
2. `src/components/ui/index.ts` —— 基元清单：Button / Input / Field / Card / Badge / EngineBadge / EmptyState /
   Skeleton(SkeletonRows|SkeletonCard) / TableWrap+Th+Td+Tr / Dialog+DialogContent / Switch / ToastProvider+useToast。
3. `src/lib/api.ts` —— 请求函数与类型（ENGINES / ENGINE_LABELS / engineXxx / hubXxx / fanout）。
4. `src/lib/i18n/index.tsx` —— `useLang()` 返回 `{ t, lang, setLang }`。
5. `src/views/accounts.test.tsx` —— 测试模板（`vi.stubGlobal("fetch")`、`waitFor` 超时给 4000）。

## 硬约束
- **只允许**创建/修改：`src/views/<你的视图>.tsx` 和 `src/views/<你的视图>.test.tsx`。
- **禁止修改**：`src/lib/**`、`src/components/**`、`src/App.tsx`、`src/styles/**`、`package.json`、`src/lib/i18n/messages.ts`。
- **禁止新增 i18n 词条**：只用已存在的键；缺合适键时挑语义最近的（`common.*` / `state.*` / 该视图自己的前缀）。
- 零硬编码颜色：只用语义类（`bg-surface` / `text-fg-dim` / `border-line` / `text-err` / `bg-accent-soft` …）。
- 五态齐全：default / hover / active / focus-visible（2px accent 轮廓）/ disabled；可点 `cursor-pointer`，禁用 `cursor-not-allowed`。
- 加载态：页面数据用骨架；按钮提交用 `loading` 属性防重复。
- 空状态三件套：图标 + 一句引导 + 主操作；错误态必须给「重试」。
- 数字列 `tnum`，长文本 `truncate`。

## 交付
- 默认导出组件（`export default function XxxView()`）。
- 3–5 条 Vitest 测试：加载态、空/错误态、一个真实交互。
- 自检必须通过：`npx vitest run src/views/<视图>.test.tsx` 与 `npx tsc --noEmit`。
- 完成后 `send_message` 给 lead：文件路径、测试结果、用到的 i18n 键、做不到的点。
