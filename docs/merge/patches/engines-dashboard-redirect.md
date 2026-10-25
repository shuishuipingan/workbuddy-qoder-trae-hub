# 引擎补丁 · 根路径重定向到控制台

## 为什么

融合层起门户后，三个端口会露出**三张不同的脸**：

```
:8811/  → WorkBuddy 用量看板    ← 上游自带的单引擎看板
:8812/  → Qoder 用量看板        ← 同上
:8817/  → WorkBuddy x Qoder-hub · 控制台
```

用户打开错端口就会以为"这还是两个项目"。这个补丁让**两个引擎的根路径 302 到控制台**，
于是"打开哪个端口都进同一个后台"。

## 怎么做的

- `hub/server.py`：起引擎**之前**设 `HUB_PORTAL_URL=http://<host>:<portal_port>/`
  （只在 `--portal-port` 有效时设；引擎不 import hub，保持通用）。
- `wb_proxy.py` / `qoder_proxy.py` 的 `_dashboard()` 顶部：
  读到该环境变量就 302，否则照旧返回自带看板。

## 边界（重要）

- **没设环境变量 = 行为完全不变**：单引擎独立运行、上游自带测试都不会受影响。
  实测：`python -m hub.server --engines wb --wb-port 9121` → `/` 仍是「WorkBuddy 用量看板」。
- 改动被 `# >>> hub-patch: dashboard-redirect` / `# <<< ...` 包住。
  源码完整性判据 **S04/S05** 只在**剥掉标记块之后**要求与上游逐字节相同 ——
  这样"引擎原样搬入"仍被钉住，同时允许写明理由的最小补丁。
- 上游同步（`hub/tools/sync_upstream.py`）拉到新版引擎后，这段补丁需要重新贴回；
  标记块就是给它定位用的。
