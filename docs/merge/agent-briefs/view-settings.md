# 任务：设置视图（settings）

文件：`src/views/settings.tsx`（当前是占位文件，直接替换）。

## 内容（等价重写）
1. **作用域切换**：三家引擎 tab（`ENGINES` / `ENGINE_LABELS`），切换后重新取该家设置。
2. **面板密码状态**：读 `engineSettings(eng)`，展示 `panel_password_is_default`（是 → 黄色警告）、`auth_required`、`api_key_masked`。
3. **API Key 列表**：读 `settings.api_keys`（`name` / `masked` / `enabled` / `realm`），表格展示；
   **没有 key 时用空状态**（图标 + 引导 + 按钮），不要显示空表。
4. **限额**：读 `settings.limits`（对象），key/value 列表，数字 `tnum`；缺失显示「—」。
5. **保存**：一个可编辑字段（建议 `pricing_refresh_minutes`，数字输入）→ `saveSettings(eng, {...})`。
   提交时按钮 `loading`；成功 toast；**失败就地**显示错误（`Field` 的 `error`），不要只弹 toast。

## 可用键（举例）
`settings.title` `settings.desc` `common.save` `common.saving` `common.saved` `common.saveFailed`
`common.retry` `common.refresh` `state.errTitle` `engine.label` `accounts.col.realm` `common.enabled` `common.disabled`

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
