/**
 * 引擎设置的**中文名**表。
 *
 * 为什么需要：高级设置卡最初直接渲染 `daily_chat_web_attempts` 这种原始键名，
 * 用户看不懂 —— 原话是"你倒是说作用是啥，中文名是啥呀"。
 * 这里给出每个键的中文名/英文名，卡片里主标题用中文，原始键名放次要位置。
 * 没收录的键会退化成"键名本身"，不会消失。
 */
export const SETTING_LABELS: Record<string, { zh: string; en: string }> = {
  "accounts_collapsed": { zh: "账号列表默认折叠", en: "Collapse account list by default" },
  "accounts_separate_tab": { zh: "账号按标签页分组", en: "Group accounts in separate tabs" },
  "auth_required": { zh: "强制 API Key", en: "Require an API key for /v1" },
  "auto_switch_product": { zh: "自动切换产品线", en: "Switch product line when quota runs out" },
  "credits_refresh_hours": { zh: "积分刷新间隔（小时）", en: "Credits refresh interval (hours)" },
  "daily_chat_model": { zh: "日聊模型", en: "Model used for the daily activity ping" },
  "daily_chat_web": { zh: "网页通道日聊", en: "Daily activity via the web channel" },
  "daily_chat_web_attempts": { zh: "日聊重试次数", en: "Daily chat attempts" },
  "daily_chat_web_fallback": { zh: "日聊失败回退模型通道", en: "Fall back to model channel on failure" },
  "daily_chat_web_random_prompt": { zh: "日聊随机提示词", en: "Randomise the daily chat prompt" },
  "daily_chat_web_retry": { zh: "日聊失败重试", en: "Retry daily chat on failure" },
  "daily_chat_web_timeout": { zh: "日聊超时（秒）", en: "Daily chat timeout (seconds)" },
  "daily_credit_limit": { zh: "每账号每日积分上限（0=不限）", en: "Daily credit cap per account (0 = off)" },
  "daily_token_limit": { zh: "每账号每日 token 上限（0=不限）", en: "Daily token cap per account (0 = off)" },
  "egress_backoff_enabled": { zh: "出口退避", en: "Back off from failing egress" },
  "filter_models_by_key": { zh: "按 Key 过滤模型清单", en: "Filter /v1/models by the key allowance" },
  "free_model_credit_fallback": { zh: "免费额度用尽回退计费模型", en: "Fall back to paid model when free credits run out" },
  "keep_backups": { zh: "账号文件保留备份数", en: "Account file backups to keep" },
  "key_before_hidden": { zh: "隐藏前缀 Key 优先", en: "Prefer keys with hidden prefix" },
  "local_web_tools": { zh: "允许本地 web 工具", en: "Allow gateway-run web_search / web_fetch" },
  "log_db_enabled": { zh: "请求日志入库", en: "Store request logs in the local DB" },
  "log_db_max_days": { zh: "日志保留天数", en: "Log retention (days)" },
  "model_daily_token_limit": { zh: "每模型每日 token 上限（0=不限）", en: "Daily token cap per model (0 = off)" },
  "models_only_available": { zh: "模型清单只列当前可用", en: "List only currently usable models" },
  "pricing_enabled": { zh: "定价刷新开关", en: "Enable pricing refresh" },
  "pricing_variant_inherit": { zh: "定价变体继承", en: "Inherit pricing variants" },
  "quota_scan_interval_hours": { zh: "额度扫描间隔（小时）", en: "Quota scan interval (hours)" },
  "realm_fallback_enabled": { zh: "区域自动回退", en: "Fall back to the other realm" },
  "reasoning_effort_map": { zh: "推理档位映射", en: "Map reasoning effort levels" },
  "remaining_priority_enabled": { zh: "剩余额度优先调度", en: "Prefer accounts with most remaining quota" },
  "reserve_credits": { zh: "额度保留值（0=不用）", en: "Reserved credits (0 = none)" },
  "section_max_height": { zh: "列表最大高度（px）", en: "Max list height (px)" },
  "ui_language": { zh: "界面语言（zh/en）", en: "UI language (zh/en)" },
  "update_check_enabled": { zh: "启动时检查更新", en: "Check for updates on start" },
  "window_probe": { zh: "窗口探测在线状态", en: "Probe the desktop window for liveness" },
  "web_tools": { zh: "允许联网工具（qd/trae）", en: "Allow web tools (qd/trae)" },
  "enable_anthropic_messages": { zh: "Anthropic 协议兼容（/v1/messages）", en: "Anthropic protocol (/v1/messages)" },
  "web_tools_rounds": { zh: "联网工具最大轮数", en: "Max web tool rounds" },
};

export function settingLabel(key: string, lang: string): string {
  const hit = SETTING_LABELS[key];
  if (!hit) return key;
  return lang === "en" ? hit.en : hit.zh;
}
