"""wb_tasks.py —— 国内版成长任务与日常福利全自动完成引擎

致谢与开源参考：
- 本模块任务事件模型、小程序埋点指纹与动态专家解析参考并致谢开源项目：
  https://github.com/L0NE-6/WorkBuddy-Daily (MIT License)
- 猫猫旅行与 Buddy 盲盒安全生命周期参考并致谢开源项目：
  https://github.com/88lin/workbuddy-auto-signin (MIT License)

包含功能：
1. 成长任务查询、批量接取 (accept)、构造事件上报点亮 (report)、领奖入账 (claim)。
2. 连续打卡 (streak) 与能量 (energy) 余额查询。
3. 猫猫旅行 (buddy travel) 状态查询、自动派出与自动领奖。
4. 严格遵守 >= 1.0s 防风控间隔，并使用 wb_fingerprint 的稳定设备指纹。
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import wb_accounts as _accounts
import wb_fingerprint
import wb_identity

# 默认 https://copilot.tencent.com；WB_CN_CHAT_UPSTREAM 可覆盖（见 wb_identity.CN_CHAT_BASE）。
CHAT_BASE = wb_identity.CN_CHAT_BASE
BILL_BASE = "https://www.codebuddy.cn"
WEB_BASE = "https://www.workbuddy.cn"
#: 本模块只跑国内版，桌面端 UA 与 wb_identity 同一来源，避免两处各写一份
#: 版本号（国际版 5.8.4 对齐时就是这么漏掉一份的）。
DESKTOP_UA = wb_identity.DESKTOP_UA_CN
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
LIB_DOC_URL = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"
_EXPERT_ID_RE = re.compile(r"^ex_[A-Za-z0-9]{6,64}$")

MP_HEADERS = {
    "X-Client-Platform": "miniprogram",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; MicroMessenger/8.0.49 WeChat/0.8.0 MiniProgramEnv/android; wkbrowser xweb)"
}

MP_REPORT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Client-Product": "workbuddy-mp",
    "X-Client-Version": "2.4.0",
    "X-Client-Platform": "mp-weixin",
    "X-Platform": "wechatmp",
}

MP_TASK_SPECS = {
    "Sequential_Tasks_1": {"name": "小程序对话", "target": 1, "reward": 100},
    "Sequential_Tasks_2": {"name": "小程序专家对话", "target": 1, "reward": 200},
    "Sequential_Tasks_3": {"name": "小程序对话5次", "target": 5, "reward": 300},
    "Sequential_Tasks_4": {"name": "小程序定时任务", "target": 1, "reward": 100},
    "Sequential_Tasks_5": {"name": "小程序GLM5.2", "target": 1, "reward": 100},
    "Sequential_Tasks_6": {"name": "小程序对话10次", "target": 10, "reward": 400},
    "school_season": {"name": "校园日活动", "target": 1, "reward": 100},
}

# 运营活动任务走独立的 /portal/activity/* 契约，不在成长任务清单里。
# 星巴克协作与记者节活动的判据都已按官方小程序埋点实测校准。
CAMPAIGN_BASE = "https://www.codebuddy.cn"
CAMPAIGN_GAP = 3
CAMPAIGN_POLL = 6
CAMPAIGN_SLOW_GAP = 10
CAMPAIGN_SLOW_POLL = 6
CAMPAIGN_CLAIM_GAP = 4
CAMPAIGN_CLAIM_POLL = 5
CAMPAIGN_DESKTOP_WAIT = 60
CAMPAIGN_DESKTOP_POLL = 6

CAMPAIGN_SPECS = [
    {
        "code": "campaign_starbucks_2026",
        "label": "星巴克协作",
        "page": {
            "pageURL": "subpackages/secondary/starbucks-activity-428/index",
            "pageName": "starbucks_activity",
        },
        "tasks": [
            {"task": "ai_chat", "label": "与 AI 对话3次", "kind": "chat",
             "times": 3, "credit": 100},
            {"task": "expert", "label": "召唤专家并对话", "kind": "expert",
             "credit": 100},
            {"task": "app", "label": "解锁 App 强大能力", "kind": "app",
             "credit": 100},
        ],
    },
    {
        "code": "campaign_reporter_2026",
        "label": "记者节福利活动",
        "page": {
            "pageURL": "subpackages/journalist-benefit/index",
            "pageName": "journalist_benefit",
        },
        "tasks": [
            {"task": "expert_chat", "label": "体验「媒体工作助理」专家",
             "kind": "expert", "expert_id": "ex_NaiDbO06Adf3",
             "expert_name": "媒体工作助理", "credit": 50},
            {"task": "skill_use", "label": "体验「采访素材整理」技能",
             "kind": "skill", "skill_id": "skill_2117491159283752960",
             "skill_name": "interview-material-organizer", "credit": 50},
            {"task": "desktop_same", "label": "体验「内容发布排期」灵感",
             "kind": "desktop", "case_id": "content-calendar", "credit": 100},
        ],
    },
]

CAMPAIGN_CASES = {
    "content-calendar": {
        # 活动判据认产物目录名，不认注册表 slug 或中文名。
        "event_id": "cnb-content-calendar",
        "name": "内容发布排期表",
        "type": "table",
        "prompt_length": 359,
    },
}

MP_EXPERT_CATEGORY = "16-BackToSchool"


_log = lambda msg: None


def set_logger(fn):
    """Route task diagnostics to the caller's logger.

    The growth endpoints swallow their errors so one dead endpoint cannot
    abort a whole cycle. Without a logger those failures are invisible, and an
    upstream change looks identical to "no tasks today".
    """
    global _log
    _log = fn or (lambda msg: None)

TASK_SPECS = {
    "create_canvas": {"kind": "canvas", "target": 1, "reward": 300, "name": "创建设计任务"},
    "template_5": {"kind": "template", "target": 5, "reward": 200, "name": "模板创建任务"},
    "expert_5": {"kind": "expert", "target": 5, "reward": 200, "name": "使用专家助手"},
    "Expert_team_use_3": {"kind": "team", "target": 3, "reward": 150, "name": "使用专家团队"},
    "skill_1": {"kind": "skill", "target": 1, "reward": 100, "name": "体验技能"},
    "automation_1": {"kind": "automation", "target": 1, "reward": 100, "name": "创建自动化任务"},
    "playbook_prompt": {"kind": "playbook", "target": 1, "reward": 100, "name": "灵感案例使用"},
    "Expert_lighthouse": {"kind": "lighthouse", "target": 1, "reward": 100, "name": "轻量云专家使用"},
    "Buddy_App": {"kind": "buddy5", "target": 1, "reward": 100, "name": "进入 Buddy 应用"},
    "Buddy_App_QQ": {"kind": "buddy5", "target": 1, "reward": 100, "name": "企鹅教师助手"},
    "Hp_Appearance": {"kind": "skin", "target": 1, "reward": 100, "name": "应用主题外观"},
    "chat_5": {"kind": "chat", "target": 5, "reward": 100, "name": "发起 5 次对话"},
    "Model_chat_GLM5.2": {"kind": "glmchat", "target": 1, "reward": 100, "name": "体验 GLM-5.2"},
    "black_cat": {"kind": "cat", "target": 3, "reward": 100, "name": "夜猫子任务 (23:00-08:00)"},
    "RichMeow_Chat": {"kind": "richmeow", "target": 1, "reward": 100, "name": "桌面对话事件链"},
    "Library_read": {"kind": "library", "target": 1, "reward": 100, "name": "浏览资料库"},
    # first_buddy 是 auto 类（上游不接受接取），完成后领奖才发放 Buddy 实例；
    # 它是所有 single 任务与猫猫旅行的前置（"prerequisite not met: first_buddy"）。
    "first_buddy": {"kind": "chat", "target": 1, "reward": 300, "name": "领取一只 Buddy"},
    "skill_share": {"kind": "skill_share", "target": 1, "reward": 100,
                    "name": "分享技能给好友"},
    # 活动任务：在专家页召唤「无畏契约拼豆专家」并完成一次对话。事件里的 id
    # 必须是这个专家的真实 id，否则上游只接受事件但不推进进度。
    "WorkBuddy_Valorant": {"kind": "expert", "target": 1, "reward": 100,
                           "expert": ("ex_xQHHD1u5Jkua", "无畏契约拼豆专家"),
                           "name": "体验「无畏契约拼豆专家」"},
    "workstation_expert": {"kind": "expert", "target": 1, "reward": 100,
                           "expert": ("WorkspaceBuilder", "工作台搭建师"),
                           "name": "工作台搭建师"},
    "expert_use_maidehao": {"kind": "expert", "target": 1, "reward": 100,
                            "expert": ("ex_GDl31WahyIH0", "中国电商运营专家"),
                            "name": "中国电商运营专家"},
    "expert_use_douyinyin": {"kind": "expert", "target": 1, "reward": 100,
                             "expert": ("ex_FbY5nlWTqDod", "抖音策略师"),
                             "name": "抖音策略师"},
    "expert_use_shangjia_xuanchuan": {"kind": "expert", "target": 1, "reward": 100,
                                      "expert": ("ex_Weo3x2EZuSDm", "商家宣传页与海报设计师"),
                                      "name": "商家宣传页与海报设计师"},
    "expert_use_bowangwang": {"kind": "expert", "target": 1, "reward": 100,
                              "expert": ("ex_WZY136wV5aMt", "直播电商教练"),
                              "name": "直播电商教练"},
    "Sequential_Tasks_7": {"kind": "playbook", "target": 1, "reward": 100,
                           "name": "PC灵感功能"},
    "wb_wechat_oa_subscribe_task": {"unforgeable": True, "reward": 100,
                                    "reason": "需在微信关注「腾讯 WorkBuddy」公众号满 24 小时",
                                    "name": "关注官方公众号"},
    "Expert_Philanthropy": {"unforgeable": True, "reason": "真实捐款动作", "reward": 0, "name": "公益爱心捐赠"},
}

# 成长任务里的真实分享/模板动作。两者都不是普通 /v2/report 单事件：
# - skill_share 先在 Open Platform 创建真实分享渠道，再上报渠道动作；
# - template_5 使用服务端场景 id，并按四事件组计数。
SKILL_SHARE_CHANNEL_URL = (
    "https://open.workbuddy.cn/api/v2/open-platform/"
    "asset-shares/c2c/channels"
)
SKILL_MARKET_URL = (
    "https://www.codebuddy.cn/v2/operation-platform/market/skill/list"
)
SKILL_SHARE_PREFERRED_ID = "skill_2097350077599879168"
TEMPLATE_SCENES_URL = (
    "https://www.workbuddy.cn/console/as/support/scenes?locale=zh-CN"
)
TEMPLATE_FALLBACK_SCENES = [
    ("0", "幻灯片"),
    ("4", "深度研究"),
    ("8", "数据分析"),
    ("16", "设计"),
    ("20", "日常开发"),
]
SKILL_SHARE_POLL_TRIES = 4
SKILL_SHARE_POLL_GAP = 3.0
TEMPLATE_POLL_TRIES = 4
TEMPLATE_POLL_GAP = 2.0

# ---------------------------------------------------------------------------
# 逆向修复常量 (2026-09 实测校准)
# ---------------------------------------------------------------------------
# 这些任务上游只认桌面客户端的真实行为信号 (jump_url 均为 workbuddy:// 深链,
# 需要真实点击进入对应页面)。伪造 /v2/report 事件会被忽略或落到 heartbeat,
# 进度永远是 0/1, claim 必然返回 400 "task not completed"。诚实地跳过并给出深链。
DESKTOP_ONLY_TASKS = {
    "Buddy_App": "在桌面端左上角「发现应用」进入任意一个 Buddy 应用",
    "Buddy_App_QQ": "在桌面端「发现应用」进入「企鹅教师助手」",
}

# 夜猫子任务只在 23:00-08:00 上报才计数, 且每天 1 次、累计 3 天。
NIGHT_TASK_CODES = {"black_cat"}

def in_night_window():
    h = time.localtime().tm_hour
    return h >= 23 or h < 8

# 专家/团队事件必须彼此不同: 桌面端 appendGrowthEvent 按 (eventCode, id) 去重,
# 上游同样只按不同 id 累加进度 —— 重复发同一个 id 进度永远不动。
# 下列 id 已在 2026-09 实测中验证可推进任务进度。
EXPERT_ID_POOL = [
    ("ex_PZw8Gu81HfN4", "运维工程师"), ("ex_ROsDtJbzADFV", "产品经理"),
    ("ex_SMUnl0nJbPix", "UI设计师"), ("ex_ZTR062oVBOCW", "数据分析师"),
    ("ex_a3sSSFBy8qaC", "后端架构师"), ("ex_aG1kvKbq8lPx", "文案策划"),
    ("ex_al1vxtUOYQ10", "测试专家"), ("ex_cZfiyuET9UQP", "安全顾问"),
    ("ex_eggOvQuVP0hq", "算法工程师"), ("ex_hSwsQjkSKnkX", "前端工程师"),
    ("ex_mMbwwmFA9n9P", "项目管理专家"), ("ex_uAQE5POfk7Zh", "增长运营专家"),
    ("ex_uZzSAScSy7FZ", "行业研究员"), ("ex_LHywGrZOtG7G", "数据分析师"),
    ("ex_NX5C8GBciVed", "测试架构师"), ("ex_DdCsaoq4AtcO", "云端运维专家"),
    ("ex_KzqKQguubrNQ", "内容创作专家"), ("ex_2cvvUZQhDyeJ", "腾讯轻量云专家"),
]
# 团队 id 来自官方专家清单 expert_center.json (expertType=team, 共 53 个),
# 前 3 个已在 2026-09 实测验证可推进 Expert_team_use_3。
TEAM_ID_POOL = [
    ("CloudOpsTeam", "运维专家团队"), ("CloudContentTeam", "内容专家团队"),
    ("CloudDevTeam", "研发专家团队"), ("ProductStrategyTeam", "产品战略团队"),
    ("MarketingCampaignTeam", "营销活动团队"), ("SalesBattleTeam", "销售作战团队"),
    ("DesignEngineTeam", "设计引擎团队"), ("HrOperationsTeam", "人力运营团队"),
]


def fetch_growth_tasks(account, mp=False):
    """查询成长任务列表及当前状态。mp=True 时使用小程序专属标头查询。"""
    if getattr(account, "is_enterprise", lambda: False)():
        return []
    url = CHAT_BASE + "/v2/activity/growth/tasks"
    hdrs = account.headers("chat")
    if mp:
        hdrs.update(MP_HEADERS)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_tasks = (data.get("data") or {}).get("tasks") or []
            tasks = []
            specs = MP_TASK_SPECS if mp else TASK_SPECS
            for t in raw_tasks:
                code = t.get("task_code") or ""
                spec = specs.get(code, {})
                prog = t.get("progress") or {}
                tasks.append({
                    "task_code": code,
                    "name": t.get("title") or spec.get("name") or code,
                    "description": t.get("description") or t.get("task_desc") or "",
                    "jump_url": t.get("jump_url") or "",
                    "status": t.get("accept_status") or "not_accepted",
                    "task_type": t.get("task_type") or "",
                    "current": prog.get("current", 0),
                    "target": prog.get("target", spec.get("target", 1)),
                    "reward_credit": t.get("reward_credit") or spec.get("reward", 0),
                    "reward_energy": t.get("reward_energy", 0),
                    "unforgeable": bool(spec.get("unforgeable")),
                    "reason": spec.get("reason", ""),
                })
            return tasks
    except Exception as exc:
        return []


def fetch_growth_summary(account):
    """查询连续打卡、猫猫旅行与能量余额。"""
    headers = account.headers("chat")
    out = {"energy": 0, "streak_days": 0, "travel": {"state": "unknown"}}
    # 1. 能量
    try:
        req = urllib.request.Request(CHAT_BASE + "/v2/activity/growth/energy", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["energy"] = (d.get("data") or {}).get("balance", 0)
    except Exception as exc:
        _log(f"growth/energy query failed: {exc}")
    # 2. 连续打卡、连登档位、补签卡与抽奖
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            data = d.get("data") or {}
            st = data.get("streak") or {}
            out["streak_days"] = st.get("days", 0)
            out["makeup_cards"] = (data.get("makeup_cards") or {}).get("balance", 0)
            redemp = data.get("redemption_status") or {}
            out["tiers"] = redemp.get("tiers") or []
            out["tier_statuses"] = {
                "7d": redemp.get("tier_7d_status", "locked"),
                "14d": redemp.get("tier_14d_status", "locked"),
                "28d": redemp.get("tier_28d_status", "locked"),
            }
    except Exception as exc:
        _log(f"growth/streak query failed: {exc}")
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/lottery/summary", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["lottery_chances"] = (d.get("data") or {}).get("chances", 0)
    except Exception as exc:
        _log(f"growth/lottery query failed: {exc}")
    # 3. 猫猫旅行
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["travel"] = d.get("data") or {}
    except Exception as exc:
        _log(f"buddy/travel/status query failed: {exc}")
    return out


def fetch_streak_days(account):
    """只读查询账号当前连续打卡天数（GET /activity/growth/streak）。

    失败返回 None，不发写请求。供对话活跃上报后回显连登天数使用。
    """
    try:
        # 成长体系国内国际都有，端点跟着身分走：国际版是 www.workbuddy.ai。
        base = account.chat_base_url() if hasattr(account, "chat_base_url") else CHAT_BASE
        req = urllib.request.Request(base + "/activity/growth/streak",
                                     headers=account.headers("chat"))
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            st = (d.get("data") or {}).get("streak") or {}
            return st.get("days")
    except Exception as exc:
        _log(f"fetch_streak_days failed: {exc}")
        return None

def accept_tasks(account, codes, chunk=20, mp=False):
    """批量接取任务。

    上游按批返回 results, 单个任务可能 status=accepted / already_accepted /
    其它失败原因。过去这里只返回 bool 且吞掉异常, 接取失败时上层完全看不见,
    于是后续上报的事件全部作用在未接取的任务上 —— 进度永远 0, 领奖必然
    "task not completed", 表现就是"接取了一堆但一个都没点亮"。
    现在返回 {"ok": bool, "accepted": [...], "failed": [...], "msg": str}。
    """
    out = {"ok": True, "accepted": [], "failed": [], "msg": "", "reasons": []}
    if not codes:
        return out
    url = CHAT_BASE + "/v2/activity/growth/tasks/accept"
    for i in range(0, len(codes), max(1, chunk)):
        part = codes[i:i + max(1, chunk)]
        body = json.dumps({"task_codes": part}).encode("utf-8")
        hdrs = account.headers("chat")
        if mp:
            hdrs.update(MP_HEADERS)
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=hdrs)
        try:
            with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(300).decode("utf-8", "replace")
            except Exception:
                pass
            out["ok"] = False
            out["msg"] = "HTTP %s: %s" % (exc.code, detail[:200])
            out["failed"].extend(part)
            out["reasons"].append(out["msg"])
            _log(f"accept batch failed: {out['msg']}")
            continue
        except Exception as exc:
            out["ok"] = False
            out["msg"] = str(exc)[:200]
            out["failed"].extend(part)
            _log(f"accept batch failed: {exc}")
            continue
        if d.get("code") != 0:
            out["ok"] = False
            out["msg"] = d.get("msg") or ("code=%s" % d.get("code"))
            out["failed"].extend(part)
            out["reasons"].append(out["msg"])
            _log(f"accept rejected: {out['msg']}")
            continue
        results = (d.get("data") or {}).get("results") or []
        seen = set()
        for item in results:
            code = item.get("task_code")
            st = str(item.get("status") or "")
            seen.add(code)
            if st in ("accepted", "already_accepted"):
                out["accepted"].append(code)
            else:
                out["failed"].append(code)
                out["reasons"].append("%s: %s" % (code, item.get("message") or st))
                _log(f"task {code} accept status={st}")
        for code in part:
            if code not in seen:
                out["failed"].append(code)
    return out


def claim_task(account, code, mp=False):
    """领取任务奖励。支持小程序口径与 copilot.tencent.com -> www.workbuddy.cn 自动降级。"""
    url = f"{CHAT_BASE}/activity/growth/tasks/{code}/claim"
    hdrs = account.headers("chat")
    if mp:
        hdrs.update(MP_HEADERS)
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=hdrs)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            if d.get("code") == 0:
                data = d.get("data") or {}
                return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
            # 200 + 非0码 (典型: 400 task not completed —— 进度还没落账就来领奖)
            _log(f"task {code} claim rejected: {d.get('msg')}")
            return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or f"code={d.get('code')}"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        err_msg = f"HTTP {exc.code}"
        try:
            err_msg = json.loads(body).get("msg") or err_msg
        except Exception:
            pass
        if exc.code == 400:
            # 降级到 web 域领奖
            web_url = f"{WEB_BASE}/activity/growth/tasks/{code}/claim"
            web_hdrs = {
                "Authorization": "Bearer " + account.access_token,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": WEB_BASE,
                "Referer": f"{WEB_BASE}/profile/growth-center",
                "x-client-platform": "web",
                "User-Agent": WEB_UA,
                "X-User-Id": account.uid,
                "X-Domain": WEB_BASE,
            }
            try:
                req_web = urllib.request.Request(web_url, data=b"{}", method="POST", headers=web_hdrs)
                with _accounts.urlopen(req_web, timeout=15, proxy=account.proxy) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == 0:
                        data = d.get("data") or {}
                        return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
                    _log(f"task {code} web claim rejected: code={d.get('code')} msg={d.get('msg')}")
                    return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or err_msg}
            except Exception as exc2:
                _log(f"task {code} web claim failed: {exc2}")
        _log(f"task {code} claim failed: {err_msg}")
        return {"ok": False, "credit": 0, "energy": 0, "msg": err_msg}
    except Exception as exc:
        _log(f"task {code} claim failed: {exc}")
    return {"ok": False, "credit": 0, "energy": 0}


def extract_expert_from_task(task):
    """从任务 code 或 jump_url 中动态提取专家 ID 与名称。"""
    jump_url = str(task.get("jump_url") or "")
    if jump_url:
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(jump_url)
            query = urllib.parse.parse_qs(parsed.query)
            if "deeplink" in query and query["deeplink"]:
                inner = urllib.parse.urlparse(query["deeplink"][0])
                inner_q = urllib.parse.parse_qs(inner.query)
                if "expertId" in inner_q and inner_q["expertId"]:
                    eid = inner_q["expertId"][0].strip()
                    if _EXPERT_ID_RE.match(eid):
                        return (eid, task.get("name") or task.get("title") or "专家")
            if "expertId" in query and query["expertId"]:
                eid = query["expertId"][0].strip()
                if _EXPERT_ID_RE.match(eid):
                    return (eid, task.get("name") or task.get("title") or "专家")
        except Exception:
            pass
    code = task.get("task_code") or ""
    if code.startswith("expert_use_"):
        slug = code[len("expert_use_"):]
        return (f"ex_{slug}", task.get("name") or task.get("title") or slug)
    return None


def build_desktop_chat_events(account, idx=0, model_id="fast-model", model_name="fast-model"):
    """6 连桌面端成功对话事件链（点亮 RichMeow_Chat）。"""
    now = int(time.time() * 1000)
    cid = f"wb-richmeow-{now}-{idx}"
    rid = f"{cid}-req"
    mid = f"msg-{now}-{idx}"
    uid = account.uid
    nick = getattr(account, "nickname", "") or ""
    return [
        {"eventCode": "agent_task_created", "timestamp": now, "userId": uid, "userNickname": nick,
         "source": "LOCAL", "name": "working", "task_target": "local", "mode": "craft",
         "requestModelId": model_id, "requestModelName": model_name,
         "has_repo": False, "repo_type": "none", "workspace_type": "empty",
         "has_connector": False, "connector_types": [], "has_mention": False, "mention_types": [],
         "has_template": False, "action": "", "template_name": "", "has_expert": False,
         "expert_id": "", "expert_name": "", "expert_industry_id": "", "has_skill": False,
         "skill_names": [], "conversationId": cid, "messageId": mid, "buddyId": "", "buddyName": ""},
        {"eventCode": "chat_message_send", "timestamp": now + 20, "userId": uid, "userNickname": nick,
         "messageId": mid + "-ast", "historyCount": 0, "isContextTruncated": False,
         "currentStepCount": 1, "traceId": rid, "rootRequestId": rid, "parentConversationId": cid,
         "agentName": "cli", "agentType": "main"},
        {"eventCode": "chat_request_send", "timestamp": now + 40, "userId": uid, "userNickname": nick,
         "inputLength": 24, "isPlan": False, "isAutoExecuteTerminal": False, "isAutoModify": False,
         "codebaseEnable": False, "maxToken": 0, "maxSteps": 500, "temperature": 0,
         "maxRetries": 0, "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
         "codebaseId": "", "mentionContextCount": 0, "command": "", "recommendId": "",
         "skillId": "", "skillCount": 0, "totalCount": 0, "traceId": rid, "rootRequestId": rid,
         "parentConversationId": cid, "conversationId": cid, "messageId": mid,
         "agentName": "cli", "agentType": "main"},
        {"eventCode": "chat_message_response", "timestamp": now + 60, "userId": uid, "userNickname": nick,
         "messageId": mid + "-resp", "conversationId": cid, "requestId": rid, "agentName": "cli"},
        {"eventCode": "chat_request_response", "timestamp": now + 80, "userId": uid, "userNickname": nick,
         "conversationId": cid, "requestId": rid, "requestModelId": model_id,
         "requestModelName": model_name, "toolCallCount": 0, "inputToken": 6, "outputToken": 12,
         "duration": 50, "agentName": "cli"},
        {"eventCode": "agent_task_completed", "timestamp": now + 100, "userId": uid, "userNickname": nick,
         "conversationId": cid, "messageId": mid, "status": "success"},
    ]


def mp_base(uid, nick):
    now = int(time.time() * 1000)
    return {
        "timestamp": now, "ideType": "WorkBuddy_MP", "ideVersion": "2.4.0",
        "extName": "workbuddy-mp", "extVersion": "2.4.0", "product": "SaaS",
        "ideName": "wx_app_cloud", "platform": "mini_program", "source": "mini_program",
        "os": "android", "osVersion": "14", "arch": "arm64",
        "machineId": f"mp-{uid[:16] if uid else 'guest'}", "timezone": "Asia/Shanghai",
        "userId": uid, "userNickname": nick or "",
    }


def _post_mp_events(account, events, base, env_extra=None, headers=None):
    """Post mini-program events with an optional page context.

    Campaign tasks are keyed by the page that emitted the event, while the
    regular mini-program growth tasks are not. Keeping this envelope helper
    shared avoids two subtly different event shapes.
    """
    base_ev = mp_base(account.uid, getattr(account, "nickname", ""))
    if env_extra:
        base_ev.update(env_extra)
    arr = []
    for e in events:
        m = dict(base_ev)
        m.update(e)
        arr.append(m)
    body = json.dumps(arr).encode("utf-8")
    request_headers = dict(headers or account.headers("chat"))
    request_headers.update(MP_REPORT_HEADERS)
    request_headers["X-User-Id"] = account.uid
    url = base + "/v2/report"
    req = urllib.request.Request(
        url, data=body, method="POST", headers=request_headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception as exc:
        _log(f"report_mp_events failed: {exc}")
        return False


def report_mp_events(account, events, base=None):
    return _post_mp_events(account, events, base or CHAT_BASE)


def _report_desktop_events(account, events, base=None):
    """Post desktop-domain events to the desktop client's report endpoint.

    ``report_events`` uses the chat base (copilot.tencent.com by default).
    Desktop events use the same upstream contract, not the web console host;
    keeping the base parameter makes the endpoint testable and preserves the
    ``WB_CN_CHAT_UPSTREAM`` override.
    """
    envelope = wb_fingerprint.get_desktop_fingerprint(
        account.uid, getattr(account, "nickname", ""))
    envelope["ideVersion"] = wb_identity.DESKTOP_VERSION_CN
    envelope["extVersion"] = wb_identity.DESKTOP_VERSION_CN
    arr = []
    for event in events:
        item = dict(envelope)
        item.update(event)
        arr.append(item)
    headers = account.headers("chat")
    headers.update({
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": DESKTOP_UA,
        "X-Domain": "copilot.tencent.com",
        "X-Product": "SaaS",
        "X-Request-ID": wb_fingerprint.generate_request_id(account.uid),
        "X-User-Id": account.uid,
    })
    url = (base or CHAT_BASE).rstrip("/") + "/v2/report"
    req = urllib.request.Request(
        url, data=json.dumps(arr).encode("utf-8"),
        method="POST", headers=headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            # The desktop endpoint commonly returns an empty body. A 2xx
            # response is the transport contract; parsing JSON here would
            # incorrectly turn a successful report into a retryable failure.
            status = getattr(resp, "status", 200)
            resp.read()
            return 200 <= status < 300
    except Exception as exc:
        _log(f"report_desktop_events failed: {exc}")
        return False


def build_event(account, kind, idx=0, expert=None):
    """构造指定类型的真实规范事件数据。

    expert: (expert_id, expert_name) —— 专家/团队类事件必须每次使用不同的 id,
    上游按 (eventCode, id) 去重, 重复 id 不会推进任务进度。
    """
    now = int(time.time() * 1000)
    cid = f"wb-task-{now}-{idx}"
    rid = f"{cid}-req"
    uid = account.uid

    if kind == "canvas":
        return {"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
                "reportDelay": 0, "conversationId": cid, "requestId": rid,
                "source": "summon_keyword", "isCustomModel": False, "name": "",
                "inputLength": 12, "id": f"wbx-canvas-{now}", "cost": 0,
                "isSuccessful": True, "userId": uid}
    if kind == "template":
        return {"eventCode": "agent_task_created_with_template", "timestamp": now,
                "reportDelay": 0, "isCustomModel": True, "id": str(idx),
                "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}
    if kind in ("expert", "team", "lighthouse"):
        etype = "team" if kind == "team" else "agent"
        if expert:
            ex_id, name = expert
        elif kind == "lighthouse":
            ex_id, name = "ex_2cvvUZQhDyeJ", "腾讯轻量云专家"
        elif kind == "team":
            ex_id, name = "CloudOpsTeam", "运维专家团队"
        else:
            ex_id, name = "ContentCreator", "内容创作专家"
        return {"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                "type": "02-Engineering", "expertType": etype, "source": "builtin",
                "version": "1.0.2", "cost": 0, "characterCount": 12, "conversationId": cid,
                "requestId": rid, "messageId": rid, "requestModelId": "deepseek-v4-flash",
                "requestModelName": "DeepSeek V4 Flash", "userId": uid}
    if kind == "skill":
        return {"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
                "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}
    if kind == "automation":
        return {"eventCode": "automated_task_create_suc", "timestamp": now, "reportDelay": 0,
                "name": "每周工作整理", "type": "cron", "source": "manually",
                "modelId": "deepseek-v4-flash", "modelIsThinking": False,
                "conversationId": cid, "requestId": rid,
                "schedule": {"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
                "prompt": "每周五自动整理本周工作", "userId": uid}
    if kind == "library":
        return {"eventCode": "web_element_click", "timestamp": now, "reportDelay": 0,
                "pageURL": LIB_DOC_URL, "elementId": "library_doc_intro_click",
                "elementName": "WorkBuddy资料库介绍", "os": "Win32", "arch": "",
                "osVersion": "10.0", "userAgent": "Mozilla/5.0", "userId": uid}
    if kind == "playbook":
        cta = {"eventCode": "playbook_cta_click", "timestamp": now, "reportDelay": 0,
               "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
               "type": "document", "categoryId": "", "categoryName": "",
               "source": "discover", "position": 0, "conversationId": cid,
               "requestId": rid, "messageId": rid, "userId": uid}
        send = {"eventCode": "playbook_prompt_send", "timestamp": now + 50, "reportDelay": 0,
                "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
                "type": "document", "categoryId": "", "categoryName": "",
                "promptLength": 30, "isOfficial": 1, "source": "discover",
                "conversationId": cid, "requestId": rid, "messageId": rid,
                "skills": "", "skillNames": "", "userId": uid}
        return [cta, send]
    if kind == "skin":
        return {"eventCode": "appearance_skin_apply", "timestamp": now, "reportDelay": 0,
                "action": "apply", "source": "settings_close", "id": "theme-tkmw7j",
                "vipLevel": "free", "series": "craft", "type": "unknown",
                "name": "和平精英激战金秋", "userId": uid}
    if kind in ("chat", "glmchat", "cat"):
        m_id = "glm-5.2" if kind in ("glmchat", "cat") else "deepseek-v4-flash"
        m_nm = "GLM-5.2" if kind in ("glmchat", "cat") else "DeepSeek V4 Flash"
        mode = "night" if kind == "cat" else "craft"
        return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                "mode": mode, "conversationId": cid, "requestId": rid,
                "inputLength": 12, "requestModelId": m_id, "requestModelName": m_nm,
                "isPlan": False, "agentName": "default", "agentType": "conversation",
                "userId": uid}
    return {"eventCode": "heartbeat", "timestamp": now, "userId": uid}


def report_events(account, events, base=None):
    """向上游上报事件数组。

    默认发 copilot.tencent.com (chat 侧) —— 与桌面客户端真实上报地址一致
    (桌面 NetLog: POST https://copilot.tencent.com/v2/report), 2026-09 实测
    该侧事件会实时推进成长任务进度。
    """
    if base is None:
        base = CHAT_BASE
    url = base + "/v2/report"
    headers = account.headers("billing" if base == BILL_BASE else "chat")
    body = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception:
        return False


def _travel_status(account):
    req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status",
                                 headers=account.headers("chat"))
    with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
        d = json.loads(resp.read().decode("utf-8"))
        return d.get("data") or {}


def ensure_buddy(account, gap=1.0):
    """确保账号已领到 Buddy 实例：没有则完成并领取 first_buddy。

    first_buddy 是 auto 类任务，其奖励（reward_buddy）就是 Buddy 实例；
    single 任务与猫猫旅行都以它为前提。返回是否已就绪。
    """
    tasks = fetch_growth_tasks(account)
    fb = next((t for t in tasks if t["task_code"] == "first_buddy"), None)
    if fb is None:
        return False
    if fb["status"] == "claimed":
        return True
    _progress_one(account, fb, [], gap)
    tasks = fetch_growth_tasks(account)
    fb = next((t for t in tasks if t["task_code"] == "first_buddy"), None)
    return bool(fb and fb["status"] == "claimed")


def _travel_depart(account, lid):
    """派出旅行；返回 (data, err)。成功 data 为响应 data，失败 err 为上游 msg。"""
    body = json.dumps({"location_id": lid}).encode("utf-8")
    req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/depart",
                                 data=body, method="POST", headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") == 0:
            return res.get("data") or {}, None
        return None, res.get("msg") or ("code=%s" % res.get("code"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        try:
            msg = json.loads(detail).get("msg") or ("HTTP %s" % exc.code)
        except Exception:
            msg = "HTTP %s%s" % (exc.code, (": " + detail[:120]) if detail else "")
        return None, msg
    except Exception as exc:
        return None, str(exc)


def do_cat_travel(account):
    """检查并执行猫猫旅行 (领奖 / 派出)。"""
    if getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "action": "skip", "msg": "企业版账号无成长体系，跳过"}
    headers = account.headers("chat")
    # 1. 查询状态
    try:
        st = _travel_status(account)
    except Exception as exc:
        return {"ok": False, "msg": f"查询旅行状态失败: {exc}"}

    state = st.get("state")
    if state == "arrived":
        # 领奖 (官方前端: POST travel/claim body {})
        req_cl = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/claim", data=b"{}", method="POST", headers=headers)
        try:
            with _accounts.urlopen(req_cl, timeout=10, proxy=account.proxy) as resp:
                c_res = json.loads(resp.read().decode("utf-8"))
                credit = (c_res.get("data") or {}).get("reward_credit", 0)
                account.fetch_credits()
                return {"ok": True, "action": "claim", "credit": credit, "msg": f"旅行归来领奖成功！获得 {credit} 积分"}
        except Exception as e:
            return {"ok": False, "msg": f"领奖失败: {e}"}

    if state == "idle":
        if st.get("daily_limit_reached"):
            return {"ok": True, "action": "idle", "msg": "猫猫今日已完成旅行，明日 00:00 刷新"}
        # 派出旅行: 官方前端要求 body 携带 {location_id} (目的地清单在
        # travel/config) —— 空 body 会被拒 400 "invalid request"。
        lid = None
        try:
            req_cfg = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/config", headers=headers)
            with _accounts.urlopen(req_cfg, timeout=10, proxy=account.proxy) as resp:
                cfg = json.loads(resp.read().decode("utf-8"))
                locs = (cfg.get("data") or {}).get("locations") or []
            if locs:
                lid = locs[0].get("id")
        except Exception as exc:
            _log(f"buddy/travel/config query failed: {exc}")
        if lid is None:
            lid = 1
        # 状态里的 buddy_id 是"当前旅行实例"的 id，猫在家 (idle) 时恒为 0，
        # 不能拿它判断有没有 Buddy。以 depart 的 "no active buddy" 为准：
        # 先直接派，失败再补齐 first_buddy（其奖励即 Buddy 实例）重试一次。
        data, err = _travel_depart(account, lid)
        if data is None and "no active buddy" in (err or "").lower():
            if ensure_buddy(account):
                data, err = _travel_depart(account, lid)
            else:
                return {"ok": False,
                        "msg": "还没有 Buddy：需先完成并领取「领取一只 Buddy」任务（新建任务并发起对话）"}
        if data is not None:
            loc = (data.get("location") or {}).get("name") or ""
            return {"ok": True, "action": "depart", "msg": f"猫猫已出发前往「{loc}」，预计数小时后归来！"}
        return {"ok": False, "msg": f"派出旅行失败: {err}"}

    if state == "traveling":
        return {"ok": True, "action": "traveling", "msg": "猫猫正在旅行途中，请稍后再来查看！"}

    return {"ok": True, "action": state, "msg": f"当前状态: {state}"}


def _task_json_request(account, method, url, body=None, headers=None,
                       timeout=20):
    """Call one JSON endpoint and return ``(status, payload)``.

    Growth-task helpers deliberately do not use ``wb_accounts.http_json``:
    that helper raises on HTTP errors, while these callers need the status
    and body to distinguish "endpoint rejected us" from "task not issued".
    """
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = dict(headers or account.headers("chat"))
    request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(
        url, data=data, method=method, headers=request_headers)
    try:
        with _accounts.urlopen(req, timeout=timeout,
                               proxy=account.proxy) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(1000)
        except Exception:
            raw = b""
    except Exception as exc:
        _log(f"task request failed: {method} {url}: {exc}")
        return 0, {}
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (TypeError, ValueError):
        payload = {"msg": raw[:200].decode("utf-8", "replace")}
    return status, payload if isinstance(payload, dict) else {}


def _skill_share_market_skill(account):
    """Read the first shareable market skill, preferring the measured one."""
    status, payload = _task_json_request(
        account, "POST", SKILL_MARKET_URL,
        body={"page": 1, "page_size": 20})
    if status < 200 or status >= 300 or payload.get("code") not in (
            0, 200, "0", "200"):
        _log("skill market list rejected: HTTP %s %s"
             % (status, str(payload.get("msg") or "")[:120]))
        return None
    skills = (payload.get("data") or {}).get("skills") or []
    candidates = []
    for row in skills:
        if not isinstance(row, dict):
            continue
        skill_id = str(row.get("skill_id") or row.get("id") or "").strip()
        if not skill_id:
            continue
        name = str(row.get("display_name_zh") or row.get("name")
                   or row.get("display_name_en") or "").strip()
        if not name:
            continue
        candidates.append({
            "id": skill_id,
            "name": name,
            "description": str(
                row.get("description_zh") or row.get("description")
                or row.get("description_en") or "").strip()[:60],
        })
    if not candidates:
        return None
    return next((row for row in candidates
                 if row["id"] == SKILL_SHARE_PREFERRED_ID),
                candidates[0])


def _create_skill_share_channel(account, skill):
    """Create a real asset-share channel; return a normalized result."""
    skill_id = str(skill.get("id") or "").strip()
    if not skill_id:
        return {"ok": False, "msg": "市场技能 ID 为空"}
    body = {
        "asset_kind": "skill",
        "asset_source": "market",
        "asset_ref": json.dumps({"asset_id": skill_id},
                                separators=(",", ":")),
        "ways": ["link", "wxacode", "wxapath"],
        "extra": {
            "name": skill.get("name") or "技能",
            "description": skill.get("description") or "",
            "shareKey": skill_id,
            "marketplaceSource": "builtin-market",
        },
    }
    headers = account.headers("chat")
    headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": DESKTOP_UA,
        "X-User-Id": account.uid,
        "X-Domain": "copilot.tencent.com",
    })
    status, payload = _task_json_request(
        account, "POST", SKILL_SHARE_CHANNEL_URL, body=body,
        headers=headers, timeout=30)
    code = payload.get("code")
    if status < 200 or status >= 300 or code not in (0, 200, "0", "200"):
        return {"ok": False, "status": status, "code": code,
                "msg": str(payload.get("msg") or "创建分享渠道失败")[:160]}
    data = payload.get("data") or {}
    share_code = str(data.get("share_code") or "").strip()
    link = ""
    for way in data.get("ways") or []:
        if isinstance(way, dict) and way.get("share_way") == "link":
            link = str(way.get("resource_ref") or "").strip()
            break
    if not share_code or not link:
        return {"ok": False, "status": status,
                "msg": "分享接口缺少 share_code 或 link"}
    return {"ok": True, "share_code": share_code,
            "channel_id": str(data.get("channel_id") or "")}


def _skill_share_channel_events(skill_id, skill_name, share_code):
    common = {
        "skillId": skill_id,
        "skillName": skill_name,
        "skillType": "market",
    }
    now = int(time.time() * 1000)
    return [
        dict(common, eventCode="skill_share_channel_action",
             action="link", panelAction="click", panelType="clipboard",
             isSuccessful=True, cost=0, id=share_code, actionAt=now),
        dict(common, eventCode="skill_share_channel_action",
             action="link", panelAction="success", panelType="clipboard",
             isSuccessful=True, cost=0, id=share_code, actionAt=now),
    ]


def _refresh_growth_task(account, code):
    return next((x for x in fetch_growth_tasks(account)
                 if x.get("task_code") == code), None)


def _wait_for_growth_task(account, code, target, tries, gap):
    """Poll until the server confirms the target or the bounded wait expires."""
    fresh = None
    for i in range(max(1, tries)):
        if i:
            time.sleep(gap)
        fresh = _refresh_growth_task(account, code)
        if not fresh:
            continue
        if (fresh.get("status") in ("completed", "claimed")
                or fresh.get("current", 0) >= target):
            return fresh
    return fresh


def _finish_special_task(account, t, fresh, logs, gap, claim):
    """Claim only after server-side completion is visible."""
    name = t.get("name") or t["task_code"]
    target = t.get("target", 1)
    if not fresh or (fresh.get("status") not in ("completed", "claimed")
                     and fresh.get("current", 0) < target):
        current = fresh.get("current", 0) if fresh else t.get("current", 0)
        logs.append("? 任务 [%s] 进度 %s/%s 未确认，领奖顺延到下次运行"
                    % (name, current, target))
        return 0
    if fresh.get("status") == "claimed":
        logs.append("✓ 任务 [%s] 已领奖" % name)
        return 0
    if not claim:
        logs.append("⏸ 任务 [%s] 已达成，等待手动领取" % name)
        return 0
    res = claim_task(account, t["task_code"])
    if res.get("ok"):
        credit = res.get("credit", 0)
        logs.append("✓ 任务 [%s] 领奖成功: +%s 积分" % (name, credit))
        time.sleep(gap)
        return credit
    logs.append("! 任务 [%s] 进度已达成但领奖失败: %s"
                % (name, res.get("msg") or "未知原因"))
    return 0


def _run_skill_share_task(account, t, logs, gap, claim=True):
    """Create a real share channel, report its events, then verify progress."""
    name = t.get("name") or "分享技能给好友"
    skill = _skill_share_market_skill(account)
    if skill is None:
        logs.append("⏭ 任务 [%s] 市场未返回可分享技能，不伪造分享" % name)
        return 0
    channel = _create_skill_share_channel(account, skill)
    if not channel.get("ok"):
        logs.append("! 任务 [%s] 创建分享渠道失败: %s"
                    % (name, channel.get("msg") or "未知原因"))
        return 0
    events = _skill_share_channel_events(
        skill["id"], skill["name"], channel["share_code"])
    if not _report_desktop_events(account, events):
        logs.append("! 任务 [%s] 渠道已创建但事件上报失败，不领奖" % name)
        return 0
    logs.append("正在等待任务 [%s] 服务端进度落账..." % name)
    fresh = _wait_for_growth_task(
        account, t["task_code"], t.get("target", 1),
        SKILL_SHARE_POLL_TRIES, SKILL_SHARE_POLL_GAP)
    return _finish_special_task(account, t, fresh, logs, gap, claim)


def _fetch_template_scenes(account):
    """Fetch real scene IDs; the static table is only a transport fallback."""
    status, payload = _task_json_request(
        account, "GET", TEMPLATE_SCENES_URL)
    if 200 <= status < 300:
        rows = (payload.get("data") or {}).get("scenes") or []
        scenes = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            scenes.append((str(row["id"]), str(row.get("name") or "")))
        if scenes:
            return scenes
    return list(TEMPLATE_FALLBACK_SCENES)


def _template_events(scene_id, scene_name):
    """One real template-creation group advances ``template_5`` by one."""
    request_id = _new_message_id()
    conversation_id = _new_conversation_id()
    return [
        {"eventCode": "agent_task_created", "source": "CLOUD", "name": "",
         "mode": "craft", "requestModelId": "default", "action": scene_id,
         "has_template": True, "template_id": scene_id,
         "template_name": scene_name, "requestId": request_id,
         "conversationId": conversation_id},
        {"eventCode": "agent_task_created_with_template",
         "templateId": scene_id, "templateName": scene_name,
         "template_id": scene_id, "mode": "working", "isCustomModel": True,
         "id": scene_id, "name": scene_name, "requestId": request_id,
         "conversationId": conversation_id},
        {"eventCode": "template_used", "template_id": scene_id,
         "templateId": scene_id, "templateName": scene_name,
         "task_mode": "working", "id": scene_id, "name": scene_name,
         "source": "growth-center", "conversationId": conversation_id},
        {"eventCode": "playbook_prompt_send", "ext1": str(uuid.uuid4()),
         "requestId": request_id, "id": scene_id, "name": scene_name,
         "type": "other", "promptLength": 30, "isOfficial": 1,
         "source": "growth-center", "conversationId": conversation_id},
    ]


def _run_template_task(account, t, logs, gap, claim=True):
    """Advance only the missing template count using server scene IDs."""
    name = t.get("name") or "模板创建任务"
    target = int(t.get("target") or 5)
    current = int(t.get("current") or 0)
    scenes = _fetch_template_scenes(account)
    if not scenes:
        logs.append("⏭ 任务 [%s] 取不到模板场景，跳过" % name)
        return 0
    fresh = None
    while current < target:
        scene_id, scene_name = scenes[current % len(scenes)]
        before = current
        if not _report_desktop_events(
                account, _template_events(scene_id, scene_name)):
            logs.append("! 任务 [%s] 场景 %s 事件上报失败"
                        % (name, scene_name or scene_id))
            return 0
        fresh = _wait_for_growth_task(
            account, t["task_code"], target,
            TEMPLATE_POLL_TRIES, TEMPLATE_POLL_GAP)
        if not fresh:
            break
        next_current = int(fresh.get("current") or 0)
        if (fresh.get("status") in ("completed", "claimed")
                or next_current >= target):
            current = max(current, next_current)
            break
        # One report group must advance the server-side counter before the
        # next group is sent. If the counter did not move, leave the task for
        # the next run instead of spraying duplicate events.
        if next_current <= before:
            logs.append("任务 [%s] 服务端进度未增长，停止本轮补发" % name)
            break
        current = next_current
    logs.append("任务 [%s] 当前服务端进度 %s/%s" % (name, current, target))
    return _finish_special_task(account, t, fresh, logs, gap, claim)


def _progress_one(account, t, logs, gap, claim=True):
    """推进单个任务并返回本次到账积分。

    auto 类任务不需要接取（上游 "task does not require acceptance"），事件可
    直接点亮；single 类未接取时上报不计进度，跳过等接取成功。
    """
    code = t["task_code"]
    spec = TASK_SPECS.get(code)
    if not spec:
        dyn_expert = extract_expert_from_task(t)
        if dyn_expert:
            spec = {"kind": "expert", "target": t.get("target") or 1, "reward": 100,
                    "expert": dyn_expert, "name": t.get("name") or t.get("title") or code}
    if not spec or spec.get("unforgeable"):
        if spec and spec.get("reason"):
            logs.append(f"⏭ 任务 [{spec.get('name') or code}] 需真实操作完成: {spec['reason']}, 跳过")
        return 0

    status = t["status"]
    cur = t.get("current", 0)
    tgt = t.get("target", 1)

    if status == "claimed":
        return 0

    # 已完成的任务先领奖 —— 桌面端/夜间任务也可能被真实操作完成
    # (例如用户自己在桌面端用了一次, 或夜里调度器点亮了), 这类任务必须
    # 先结算, 不能因为"只能靠真实操作"就直接跳过丢掉奖励。
    if status == "completed" or cur >= tgt:
        if not claim:
            logs.append(f"⏸ 任务 [{spec['name']}] 已达成，等待手动领取")
            return 0
        res = claim_task(account, code)
        if res.get("ok"):
            cr = res.get("credit", 0)
            logs.append(f"✓ 任务 [{spec['name']}] 领奖成功: +{cr} 积分")
            time.sleep(gap)
            return cr
        logs.append(f"! 任务 [{spec['name']}] 领奖失败: {res.get('msg') or '未知原因'}")
        time.sleep(gap)
        return 0

    # 只认桌面端真实行为的任务: 伪造事件不会推进进度, 诚实跳过并给出深链。
    if code in DESKTOP_ONLY_TASKS:
        jump = t.get("jump_url") or "workbuddy://chat"
        logs.append(f"⏭ 任务 [{spec['name']}] 需真实操作完成: {DESKTOP_ONLY_TASKS[code]}"
                    f" (深链 {jump}), 跳过事件伪造")
        return 0

    # 夜猫子任务只在 23:00-08:00 计数 (每日 01:00 调度器也会自动执行)
    if code in NIGHT_TASK_CODES and not in_night_window():
        logs.append(f"🌙 任务 [{spec['name']}] 仅 23:00-08:00 上报计数, 当前不在窗口, 跳过"
                    f" (每日 01:00 调度器自动执行, 累计 3 天)")
        return 0

    if status == "not_accepted" and t.get("task_type") != "auto":
        # 接取没成功就上报是白费功夫: 上游只对已接取的任务累计进度。
        logs.append(f"⏭ 任务 [{spec['name']}] 仍未接取, 跳过 (先解决接取失败)")
        return 0

    kind = spec.get("kind")
    if kind == "skill_share":
        return _run_skill_share_task(account, t, logs, gap, claim=claim)
    if kind == "template":
        return _run_template_task(account, t, logs, gap, claim=claim)

    # 需点亮上报 —— 专家/团队事件必须使用互不相同的 id, 否则上游按
    # (eventCode, id) 去重, 进度永远不动。
    need = max(1, tgt - cur)
    logs.append(f"正在点亮任务 [{spec['name']}] (需上报 {need} 次)...")
    report_ok = True
    id_pool = None
    if kind in ("expert", "team"):
        id_pool = TEAM_ID_POOL if kind == "team" else EXPERT_ID_POOL
    fixed_expert = spec.get("expert")
    for i in range(need):
        expert = fixed_expert
        if expert is None and id_pool:
            pid, pnm = id_pool[(cur + i) % len(id_pool)]
            expert = (pid, pnm)
        if kind == "richmeow":
            ev_list = build_desktop_chat_events(account, idx=i)
        else:
            ev = build_event(account, kind, idx=i, expert=expert)
            ev_list = ev if isinstance(ev, list) else [ev]
        if not report_events(account, ev_list):
            report_ok = False
        if i < need - 1:
            time.sleep(gap)
    if not report_ok:
        logs.append(f"! 任务 [{spec['name']}] 部分事件上报失败 (上游拒绝), 继续尝试领奖")
    time.sleep(1.5)

    # 等上游把进度落账再领奖。进度通常 1-3 秒就可见, 因此先快查几次;
    # 只有确实在动才继续等, 免得每个卡住的任务都空等 20 秒 (整轮要几分钟)。
    prog = cur
    for attempt in range(6):
        fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == code), None)
        if fresh:
            prog = fresh.get("current", 0)
            if prog >= tgt or fresh.get("status") in ("completed", "claimed"):
                break
            if prog > cur:
                # 已经在涨了, 值得多等一会儿
                time.sleep(2.5)
                continue
        if attempt < 2:
            time.sleep(1.5)
        else:
            break
    if prog < tgt:
        logs.append(f"? 任务 [{spec['name']}] 已上报但进度 {prog}/{tgt} 未达成, 领奖顺延到下次运行")
        time.sleep(gap)
        return 0

    # 领奖
    if not claim:
        logs.append(f"⏸ 任务 [{spec['name']}] 已点亮，等待手动领取")
        time.sleep(gap)
        return 0
    res = claim_task(account, code)
    if res.get("ok"):
        cr = res.get("credit", 0)
        logs.append(f"✓ 任务 [{spec['name']}] 点亮并领奖成功: +{cr} 积分")
        time.sleep(gap)
        return cr
    logs.append(f"! 任务 [{spec['name']}] 进度已达 {prog}/{tgt} 但领奖失败: {res.get('msg') or '未知原因'}")
    time.sleep(gap)
    return 0


def do_buddy_open(account):
    """成长能量开 Buddy 盲盒（消耗能量获取宠物）。

    对齐 88lin/workbuddy-auto-signin 的开盒流程：在猫猫空闲/领奖后，将积累的能量
    自动开启 Buddy 盲盒（/v2/activity/growth/buddy/open），避免成长能量闲置。
    """
    if getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "action": "skip", "msg": "企业版账号无成长体系，跳过"}
    if not hasattr(account, "headers"):
        return {"ok": True, "action": "skip", "msg": "账号缺少 headers 方法，跳过"}
    headers = account.headers("chat")
    try:
        req_q = urllib.request.Request(CHAT_BASE + "/v2/activity/growth/buddy/quota", headers=headers)
        with _accounts.urlopen(req_q, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            quota_data = (d.get("data") or {}) if d.get("code") == 0 else {}
            affordable = int(quota_data.get("affordable") or 0)
            max_open = int(quota_data.get("max_open_count") or 1)
        if affordable <= 0:
            return {"ok": True, "action": "idle", "opened": 0, "msg": "成长能量不足以开启 Buddy 盲盒"}
        count = min(affordable, max(1, max_open))
        import uuid
        payload = {"count": count, "client_token": str(uuid.uuid4())}
        req_o = urllib.request.Request(
            CHAT_BASE + "/v2/activity/growth/buddy/open",
            data=json.dumps(payload).encode("utf-8"),
            method="POST", headers=headers
        )
        with _accounts.urlopen(req_o, timeout=15, proxy=account.proxy) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") == 0:
            data = res.get("data") or {}
            name = data.get("buddy") or data.get("name") or data.get("buddies") or "新 Buddy"
            if isinstance(name, list) and name:
                name = ", ".join(str(x) for x in name)
            return {"ok": True, "action": "open", "opened": count,
                    "name": str(name), "msg": f"开启 Buddy 盲盒 ×{count} 成功 ({name})"}
        return {"ok": False, "action": "error", "opened": 0,
                "msg": f"开盲盒失败: {res.get('msg') or res.get('code')}"}
    except Exception as exc:
        return {"ok": False, "action": "error", "opened": 0, "msg": f"Buddy 盲盒异常: {exc}"}


def run_miniprogram_tasks(account, gap=1.0, claim=True):
    """小程序链式任务自动化（Sequential_Tasks 系列与校园日活动）。

    参考并致谢 L0NE-6/WorkBuddy-Daily 的小程序埋点协议与任务链设计。
    """
    if account.realm != "cn" or getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "logs": [], "earned_credit": 0}
    logs = []
    mp_tasks = fetch_growth_tasks(account, mp=True)
    if not mp_tasks:
        return {"ok": True, "logs": logs, "earned_credit": 0}

    supported = [t for t in mp_tasks if t["task_code"] in MP_TASK_SPECS
                 or t["task_code"].startswith("Sequential_Tasks_")]
    if not supported:
        return {"ok": True, "logs": logs, "earned_credit": 0}

    logs.append(f"📱 发现 {len(supported)} 个小程序成长任务，开始执行自动化...")
    unaccepted = [t["task_code"] for t in supported if t["status"] == "not_accepted"]
    if unaccepted:
        acc = accept_tasks(account, unaccepted, mp=True)
        if acc.get("accepted"):
            logs.append(f"✓ 已接取小程序任务: {', '.join(acc['accepted'])}")
        time.sleep(gap)
        mp_tasks = fetch_growth_tasks(account, mp=True)

    total_earned = 0
    now = int(time.time() * 1000)

    for t in mp_tasks:
        code = t["task_code"]
        if code not in MP_TASK_SPECS and not code.startswith("Sequential_Tasks_"):
            continue
        status = t["status"]
        cur = t.get("current", 0)
        tgt = t.get("target", 1)
        name = t.get("name") or code

        if status == "claimed":
            continue

        if status == "completed" or cur >= tgt:
            if claim:
                res = claim_task(account, code, mp=True)
                if res.get("ok"):
                    cr = res.get("credit", 0)
                    total_earned += cr
                    logs.append(f"✓ 小程序任务 [{name}] 领奖成功: +{cr} 积分")
                else:
                    logs.append(f"! 小程序任务 [{name}] 领奖失败: {res.get('msg') or '未知原因'}")
            continue

        if status == "not_accepted":
            continue

        need = max(1, tgt - cur)
        events = []
        for i in range(need):
            cid = f"wb-mp-{now}-{code}-{i}"
            rid = f"req-{now}-{code}-{i}"
            if code in ("Sequential_Tasks_1", "Sequential_Tasks_3", "Sequential_Tasks_6", "school_season"):
                events.append({
                    "eventCode": "chat_request_send", "mode": "chat", "inputLength": 12,
                    "isPlan": False, "isAutoExecuteTerminal": False, "isAutoModify": False,
                    "codebaseEnable": False, "maxToken": 0, "maxSteps": 500, "temperature": 0,
                    "maxRetries": 0, "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
                    "codebaseId": "", "mentionContextCount": 0, "command": "",
                    "requestId": rid, "traceId": rid, "rootRequestId": rid,
                    "parentConversationId": cid, "conversationId": cid,
                    "messageId": f"msg-{now}-{i}", "agentName": "mp", "agentType": "main",
                    "codebuddy.session_id": cid, "codebuddy.conversation_request_id": rid,
                })
            elif code == "Sequential_Tasks_2":
                events.append({
                    "eventCode": "expert_actual_use", "id": "WorkspaceBuilder", "name": "WorkspaceBuilder",
                    "expertTitle": "工作台搭建师", "type": "send_message", "characterCount": 12,
                    "expertType": "agent", "extVersion": "2.2.8",
                })
            elif code == "Sequential_Tasks_4":
                events.append({
                    "eventCode": "automated_task_create_suc", "mode": "CLOUD",
                    "name": "每日任务提醒", "source": "manually", "skills": "",
                    "skillCount": 0, "scheduleType": "daily",
                })
            elif code == "Sequential_Tasks_5":
                events.append({
                    "eventCode": "chat_request_send", "mode": "chat", "inputLength": 12,
                    "isPlan": False, "isAutoExecuteTerminal": False, "isAutoModify": False,
                    "codebaseEnable": False, "maxToken": 0, "maxSteps": 500, "temperature": 0,
                    "maxRetries": 0, "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
                    "codebaseId": "", "mentionContextCount": 0, "command": "",
                    "requestId": rid, "traceId": rid, "rootRequestId": rid,
                    "parentConversationId": cid, "conversationId": cid,
                    "messageId": f"msg-{now}-{i}", "agentName": "mp", "agentType": "main",
                    "codebuddy.session_id": cid, "codebuddy.conversation_request_id": rid,
                    "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2",
                })

        if events:
            logs.append(f"正在点亮小程序任务 [{name}] (需上报 {need} 次)...")
            report_mp_events(account, events)
            time.sleep(1.5)
            if claim:
                res = claim_task(account, code, mp=True)
                if res.get("ok"):
                    cr = res.get("credit", 0)
                    total_earned += cr
                    logs.append(f"✓ 小程序任务 [{name}] 点亮并领奖成功: +{cr} 积分")
                else:
                    logs.append(f"? 小程序任务 [{name}] 已上报，领奖将在下轮重试")
            time.sleep(gap)

    return {"ok": True, "logs": logs, "earned_credit": total_earned}


def _campaign_headers(account):
    """Activity APIs use the mini-program identity, not the desktop profile."""
    headers = dict(MP_REPORT_HEADERS)
    headers["Authorization"] = account.headers("chat").get("Authorization", "")
    if account.uid:
        headers["X-User-Id"] = account.uid
    return headers


def _campaign_call(account, method, path, body=None):
    """Call a portal activity endpoint; return ``(status, payload)``."""
    data = None
    if method != "GET":
        data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        CAMPAIGN_BASE + path, data=data, method=method,
        headers=_campaign_headers(account))
    try:
        with _accounts.urlopen(req, timeout=20, proxy=account.proxy) as resp:
            raw = resp.read()
            return getattr(resp, "status", 200), (
                json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(500).decode("utf-8", "replace")
        except Exception:
            pass
        try:
            payload = json.loads(detail) if detail else {}
        except Exception:
            payload = {"msg": detail[:200]}
        return exc.code, payload
    except Exception as exc:
        _log(f"campaign request failed: {path}: {exc}")
        return 0, {}


def fetch_campaign_tasks(account, campaign, log=None):
    """Read activity task definitions and progress.

    Returns a ``{task_code: row}`` mapping. A failed request is deliberately
    distinguishable from "activity not issued" in the log: silently treating a
    401 or 500 as an ended campaign hides credential and upstream failures.
    """
    path = ("/portal/activity/tasks?activity_code="
            + urllib.parse.quote(str(campaign), safe=""))
    status, payload = _campaign_call(account, "GET", path)
    code = (payload or {}).get("code")
    if status != 200 or code not in (None, 0):
        if log:
            log("      ⚠️ 活动任务读取失败[%s]: HTTP %s %s"
                % (campaign, status, str(payload)[:80]))
        return {}
    data = (payload or {}).get("data") or {}
    progress = {p.get("task_code"): p
                for p in (data.get("progress") or [])
                if isinstance(p, dict)}
    rows = {}
    for task in data.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_code = task.get("task_code")
        if not task_code:
            continue
        prog = progress.get(task_code) or {}
        rows[task_code] = {
            "target": task.get("target_count") or 0,
            "progress": prog.get("progress") or 0,
            "completed_at": prog.get("completed_at"),
            "claimed_at": prog.get("claimed_at"),
            "need_view": task.get("need_view"),
            "credit": task.get("reward_credit") or 0,
        }
    return rows


def _campaign_view(account, campaign, task):
    path = ("/portal/activity/tasks/%s/viewed?activity_code=%s"
            % (urllib.parse.quote(str(task), safe=""),
               urllib.parse.quote(str(campaign), safe="")))
    status, payload = _campaign_call(account, "POST", path)
    return status == 200 and (payload or {}).get("code") in (None, 0)


def _campaign_claim(account, campaign, task, log, credit=0,
                    tries=None, gap=None):
    """Claim and verify ``claimed_at``; a successful POST alone is not enough."""
    path = ("/portal/activity/tasks/%s/claim?activity_code=%s"
            % (urllib.parse.quote(str(task), safe=""),
               urllib.parse.quote(str(campaign), safe="")))
    status, payload = _campaign_call(account, "POST", path)
    if not (status == 200 and (payload or {}).get("code") == 0):
        log("      ⚠️ 领奖[%s/%s]: HTTP %s %s"
            % (campaign, task, status, str(payload)[:80]))
        return False
    tries = CAMPAIGN_CLAIM_POLL if tries is None else tries
    gap = CAMPAIGN_CLAIM_GAP if gap is None else gap
    for i in range(max(1, tries)):
        if i:
            time.sleep(gap)
        row = fetch_campaign_tasks(account, campaign, log).get(task) or {}
        if row.get("claimed_at"):
            log("      🎁 领奖[%s/%s]: +%s 积分"
                % (campaign, task, credit))
            return True
    log("      ⚠️ 领奖[%s/%s]: 已提交但未回读到 claimed_at，下轮重试"
        % (campaign, task))
    return False


def _campaign_poll(account, campaign, task, target,
                   tries=None, gap=None, log=None):
    """Poll asynchronous activity progress and return the latest value."""
    current = 0
    for _ in range(tries if tries is not None else CAMPAIGN_POLL):
        time.sleep(gap if gap is not None else CAMPAIGN_GAP)
        row = fetch_campaign_tasks(account, campaign, log).get(task) or {}
        current = row.get("progress") or 0
        if row.get("completed_at") or current >= target:
            return max(current, target)
    return current


def _new_conversation_id():
    return str(uuid.uuid4())


def _new_message_id():
    return "%012x%s" % (int(time.time() * 1000), uuid.uuid4().hex[:20])


def _campaign_chat_event():
    conv = _new_conversation_id()
    rid = _new_message_id()
    return {
        "eventCode": "chat_request_send", "mode": "chat", "inputLength": 12,
        "isPlan": False, "isAutoExecuteTerminal": False, "isAutoModify": False,
        "codebaseEnable": False, "maxToken": 0, "maxSteps": 500,
        "temperature": 0, "maxRetries": 0, "mentionContexts": [],
        "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
        "mentionContextCount": 0, "command": "", "recommendId": "",
        "skillId": "", "skillCount": 0, "totalCount": 0,
        "requestId": rid, "traceId": rid, "rootRequestId": rid,
        "parentConversationId": conv, "conversationId": conv,
        "messageId": "msg-" + rid[-8:], "agentName": "mp",
        "agentType": "main", "codebuddy.session_id": conv,
        "codebuddy.conversation_request_id": rid,
    }


def _campaign_expert_events(expert_id, expert_name):
    conv = _new_conversation_id()
    rid = _new_message_id()
    category = MP_EXPERT_CATEGORY
    return [
        {"eventCode": "expert_summon_click", "id": expert_id,
         "name": expert_id, "expertTitle": expert_name, "type": category,
         "position": 0},
        {"eventCode": "expert_summoned", "id": expert_id,
         "name": expert_id, "expertTitle": expert_name},
        {"eventCode": "expert_actual_use", "id": expert_id,
         "name": expert_id, "expertTitle": expert_name, "type": category,
         "characterCount": 14, "expertType": "builtin"},
        {"eventCode": "chat_request_send", "inputLength": 14,
         "isPlan": False, "isAutoExecuteTerminal": False,
         "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
         "maxSteps": 500, "temperature": 0, "maxRetries": 0,
         "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
         "codebaseId": "", "mentionContextCount": 0, "command": "",
         "recommendId": "", "skillId": "", "skillCount": 0,
         "totalCount": 0, "traceId": rid, "rootRequestId": rid,
         "parentConversationId": conv, "conversationId": conv,
         "messageId": "msg-" + rid[-8:], "agentName": "mp",
         "agentType": "main", "expertId": expert_id,
         "expertName": expert_name, "codebuddy.session_id": conv,
         "codebuddy.conversation_request_id": rid},
    ]


def _campaign_skill_events(skill_id, skill_name):
    conv = _new_conversation_id()
    rid = _new_message_id()
    return [
        {"eventCode": "skill_info", "id": skill_name, "skillId": skill_id,
         "skillName": skill_name, "skillType": "market",
         "skillSource": "market", "position": 0,
         "conversationId": conv},
        {"eventCode": "chat_request_send", "inputLength": 14,
         "isPlan": False, "isAutoExecuteTerminal": False,
         "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
         "maxSteps": 500, "temperature": 0, "maxRetries": 0,
         "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
         "codebaseId": "", "mentionContextCount": 0, "command": "",
         "recommendId": "", "skillId": skill_id, "skillCount": 1,
         "totalCount": 0, "traceId": rid, "rootRequestId": rid,
         "parentConversationId": conv, "conversationId": conv,
         "messageId": "msg-" + rid[-8:], "agentName": "mp",
         "agentType": "main", "codebuddy.session_id": conv,
         "codebuddy.conversation_request_id": rid},
    ]


def _campaign_desktop_events(case):
    return [{
        "eventCode": "playbook_prompt_send",
        "id": case.get("event_id") or "",
        "name": case.get("name") or "",
        "type": case.get("type") or "other",
        "promptLength": case.get("prompt_length") or 0,
        "isOfficial": 1,
        "source": "discover",
        "query": "",
        "ext1": "discover",
        "requestId": _new_message_id(),
        "conversationId": _new_conversation_id(),
    }]


def _campaign_pick_expert(account):
    """Resolve a real expert; fall back to the stable mini-program expert."""
    body = json.dumps({
        "edition_mode": "all,domestic", "page": 1, "page_size": 20,
        "sort_by": "use_count", "sort_order": "desc",
        "categories": [MP_EXPERT_CATEGORY], "expert_type": "agent",
    }).encode("utf-8")
    req = urllib.request.Request(
        CAMPAIGN_BASE + "/v2/operation-platform/market/expert/list",
        data=body, method="POST", headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        experts = ((payload.get("data") or {}).get("experts") or [])
        for expert in experts:
            expert_id = expert.get("expert_id")
            if not expert_id:
                continue
            display = expert.get("display_name_zh") or {}
            name = ((display.get("zh") if isinstance(display, dict) else display)
                    or expert.get("name") or expert_id)
            return expert_id, name
    except Exception:
        pass
    return "WorkspaceBuilder", "工作台搭建师"


def _campaign_fire(account, spec, task, log):
    """Emit one campaign criterion; ``app`` is intentionally not forgeable."""
    kind = task.get("kind")
    page = spec.get("page")
    if kind == "app":
        log("      ⏳ %s: 需手机端 App 登录后与 AI 对话一次，脚本不伪造"
            % task.get("label"))
        return False
    if kind == "desktop":
        case = CAMPAIGN_CASES.get(task.get("case_id")) or {}
        if not case:
            log("      ⚠️ %s: 未内置案例 %s"
                % (task.get("label"), task.get("case_id")))
            return False
        return _report_desktop_events(account, _campaign_desktop_events(case))
    if kind == "chat":
        return _post_mp_events(
            account, [_campaign_chat_event()], CAMPAIGN_BASE, page,
            headers=_campaign_headers(account))
    if kind == "expert":
        expert_id = task.get("expert_id")
        expert_name = task.get("expert_name")
        if not expert_id:
            expert_id, expert_name = _campaign_pick_expert(account)
        if not expert_id:
            log("      ✗ %s: 取不到专家 id，跳过" % task.get("label"))
            return False
        return _post_mp_events(
            account, _campaign_expert_events(expert_id, expert_name),
            CAMPAIGN_BASE, page, headers=_campaign_headers(account))
    if kind == "skill":
        return _post_mp_events(
            account,
            _campaign_skill_events(task.get("skill_id"),
                                   task.get("skill_name")),
            CAMPAIGN_BASE, page, headers=_campaign_headers(account))
    return False


def run_campaign_tasks(account, claim=True):
    """Run issued campaign tasks, then claim and verify their receipts.

    Campaign definitions are separate from the growth-task list. The task
    criteria are asynchronous, so each event is followed by bounded polling;
    an event that has not landed yet is left for the next run rather than
    being sprayed repeatedly.
    """
    if account.realm != "cn" or getattr(account, "is_enterprise",
                                        lambda: False)():
        return {"ok": True, "logs": [], "earned_credit": 0}
    logs = []
    earned = 0
    available = 0
    failed = False
    for spec in CAMPAIGN_SPECS:
        campaign = spec["code"]
        read_errors = []
        rows = fetch_campaign_tasks(account, campaign, read_errors.append)
        if read_errors:
            failed = True
            logs.extend(read_errors)
        if not rows:
            logs.append("🎪 %s: 活动任务未下发/已结束或读取失败" % spec["label"])
            continue
        available += 1
        logs.append("🎪 %s（%s）" % (spec["label"], campaign))
        for task in spec.get("tasks") or []:
            code = task.get("task")
            row = rows.get(code) or {}
            if not row:
                logs.append("   ⏭ %s: 未下发/已下线" % task.get("label"))
                continue
            target = row.get("target") or task.get("times") or 1
            credit = row.get("credit") or task.get("credit") or 0
            if row.get("claimed_at"):
                logs.append("   ✅ %s: 已领奖（%s/%s）"
                            % (task.get("label"), target, target))
                continue
            if row.get("completed_at"):
                if claim and _campaign_claim(
                        account, campaign, code, logs.append, credit):
                    earned += credit
                elif claim:
                    failed = True
                elif not claim:
                    logs.append("   ⏸ %s: 已达成，等待手动领取"
                                % task.get("label"))
                continue
            if row.get("need_view", True):
                _campaign_view(account, campaign, code)
            current = row.get("progress") or 0
            kind = task.get("kind")
            if kind == "app":
                _campaign_fire(account, spec, task, logs.append)
                logs.append("   ⏳ %s: 尚未完成（%s/%s），真机完成后重跑会自动领奖"
                            % (task.get("label"), current, target))
                continue
            if kind == "desktop":
                if not _campaign_fire(account, spec, task, logs.append):
                    logs.append("   ⚠️ %s: 上报失败" % task.get("label"))
                    continue
                current = _campaign_poll(
                    account, campaign, code, target,
                    tries=max(2, CAMPAIGN_DESKTOP_WAIT // CAMPAIGN_DESKTOP_POLL),
                    gap=CAMPAIGN_DESKTOP_POLL, log=logs.append)
            else:
                for _ in range(max(1, target - current)):
                    if current >= target:
                        break
                    if not _campaign_fire(account, spec, task, logs.append):
                        break
                    current = _campaign_poll(
                        account, campaign, code, target, log=logs.append)
                if current < target:
                    current = _campaign_poll(
                        account, campaign, code, target,
                        tries=CAMPAIGN_SLOW_POLL, gap=CAMPAIGN_SLOW_GAP,
                        log=logs.append)
            row = fetch_campaign_tasks(
                account, campaign, logs.append).get(code) or {}
            credit = row.get("credit") or task.get("credit") or 0
            if not (row.get("completed_at") or current >= target):
                logs.append("   ⏳ %s: 判据延迟未落库（%s/%s），下轮自动补领"
                            % (task.get("label"), current, target))
                continue
            logs.append("   ✅ %s: completed %s/%s"
                        % (task.get("label"), target, target))
            if claim and _campaign_claim(
                    account, campaign, code, logs.append, credit):
                earned += credit
            elif claim:
                failed = True
            elif not claim:
                logs.append("   ⏸ %s: 已达成，等待手动领取"
                            % task.get("label"))
    if earned:
        logs.append("🎪 活动任务本轮入账 %s 积分" % earned)
    return {"ok": not failed, "logs": logs, "earned_credit": earned,
            "available": available, "failed": failed}


def run_growth_tasks(account, gap=1.0, claim=True):
    """执行一次性 / 活动成长任务，不含任何每日福利副作用。

    claim=False 只推进进度、不领奖，让用户自己挑时间领取（#507）。

    猫猫旅行、Buddy 盲盒、连登管家、抽奖与夜猫子都属于每日任务，由
    do_cat_travel() / do_buddy_open() / run_streak_bonus() / run_night_growth()
    分别执行。这里只处理成长任务清单里的一次性任务和活动任务，避免用户点
    「一次性任务」时顺带消耗每日额度。
    """
    if account.realm != "cn":
        return {"ok": False, "msg": "国际版不适用国内成长任务中心", "logs": []}
    if getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "msg": "企业版账号无成长体系，跳过", "logs": []}

    logs = []
    logs.append(f"开始为账号 {account.nickname or account.uid[:8]} 运行成长任务自动化...")
    total_earned = 0
    tasks = fetch_growth_tasks(account)
    growth_available = bool(tasks)
    if not growth_available:
        logs.append("未能获取到成长任务清单，继续尝试运营活动任务")

    # 0. auto 类任务（如 first_buddy）不需要接取，且可能是其他任务的前置：
    #    必须先把它们点亮并领奖（first_buddy 的奖励就是 Buddy 实例），否则
    #    single 任务接取会报 "prerequisite not met: first_buddy"。
    #    夜猫子是每日任务，不在这里处理。
    for t in tasks:
        if (t.get("task_type") == "auto"
                and t.get("task_code") not in NIGHT_TASK_CODES):
            total_earned += _progress_one(account, t, logs, gap, claim=claim)

    # 1. 批量接取未接任务（auto 类不接受接取，unforgeable 无法自动化）
    def _needs_accept(x):
        return (x["status"] == "not_accepted" and not x.get("unforgeable")
                and x.get("task_type") != "auto"
                and x.get("task_code") not in NIGHT_TASK_CODES)

    unaccepted = [t["task_code"] for t in tasks if _needs_accept(t)]
    if unaccepted:
        logs.append(f"发现 {len(unaccepted)} 个待接取任务，正在批量接取...")
        acc = accept_tasks(account, unaccepted)
        if acc.get("failed"):
            why = acc.get("msg") or ("; ".join(acc.get("reasons") or [])[:160])
            logs.append(f"! 接取未成功 {len(acc['failed'])} 个: {', '.join(acc['failed'][:5])}"
                        + (" ..." if len(acc["failed"]) > 5 else "")
                        + (f" ({why})" if why else ""))
        if acc.get("accepted"):
            logs.append(f"✓ 已接取 {len(acc['accepted'])} 个任务")
        time.sleep(gap)
        tasks = fetch_growth_tasks(account)
        # 复核一次: 上游偶尔会瞬时拒绝整个批次, 复查后仍处于未接取的再补一次,
        # 否则后面所有上报都作用在未接取的任务上 —— 进度全是 0。
        still = [t["task_code"] for t in tasks if _needs_accept(t)]
        if still:
            logs.append(f"仍有 {len(still)} 个未接取，重试接取一次...")
            retry = accept_tasks(account, still)
            if retry.get("accepted"):
                logs.append(f"✓ 重试接取成功 {len(retry['accepted'])} 个")
            time.sleep(gap)
            tasks = fetch_growth_tasks(account)
        if not tasks:
            # The activity portal is a separate contract. A temporary empty
            # growth list must not prevent campaign tasks from running.
            logs.append("! 接取后无法获取成长任务清单，继续尝试运营活动任务")
            tasks = []
        still_pending = [t["task_code"] for t in tasks if _needs_accept(t)]
        if still_pending:
            logs.append(f"! 仍有 {len(still_pending)} 个任务处于未接取状态，"
                        f"对未接取任务上报事件不会计入进度，本轮跳过这些任务")

    # 2. 处理每个任务（auto 已在第 0 步处理过）
    for t in tasks:
        if (t.get("task_type") == "auto"
                or t.get("task_code") in NIGHT_TASK_CODES):
            continue
        total_earned += _progress_one(account, t, logs, gap, claim=claim)

    # 2b. 运行微信小程序链式成长任务（Sequential_Tasks 系列）
    try:
        mp_res = run_miniprogram_tasks(account, gap=gap, claim=claim)
        total_earned += mp_res.get("earned_credit", 0)
        logs.extend(mp_res.get("logs", []))
    except Exception as exc:
        logs.append(f"! 小程序成长任务执行异常: {exc}")

    # 2c. 运营活动任务（星巴克协作 / 记者节福利）走独立的 portal 契约。
    campaign_available = 0
    campaign_ok = True
    try:
        campaign_res = run_campaign_tasks(account, claim=claim)
        total_earned += campaign_res.get("earned_credit", 0)
        campaign_available = campaign_res.get("available", 0)
        campaign_ok = campaign_res.get("ok") is not False
        logs.extend(campaign_res.get("logs") or [])
    except Exception as exc:
        campaign_ok = False
        logs.append(f"! 运营活动任务执行异常: {exc}")

    # 3. 刷新积分余额。每日福利不在这里执行。
    account.fetch_credits()
    logs.append(f"🎉 一次性 / 活动任务执行完毕！本次累计新增到账: +{total_earned} 积分，当前总剩余: {account.credits.get('remain', 0)} 积分")
    return {"ok": (growth_available or bool(campaign_available)) and campaign_ok,
        "logs": logs, "earned_credit": total_earned,
        "credits": account.credits}


def run_night_growth(account):
    """夜猫子任务 (black_cat): 每日 23:00-08:00 上报 1 次 GLM-5.2 夜间对话事件,
    每天计 1 次、累计 3 天后可领奖。白天调用会诚实跳过。"""
    if account.realm != "cn":
        return {"ok": False, "msg": "国际版不适用国内成长任务中心", "logs": [], "earned_credit": 0}
    if getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "msg": "企业版账号无成长体系，跳过", "logs": [], "earned_credit": 0}
    logs = []
    name = account.nickname or account.uid[:8]
    if not in_night_window():
        return {"ok": True, "earned_credit": 0, "logs": [
            f"[{name}] 当前不在 23:00-08:00 夜间窗口, 夜猫子任务跳过 (每日 01:00 自动执行)"]}
    tasks = fetch_growth_tasks(account)
    t = next((x for x in tasks if x["task_code"] == "black_cat"), None)
    if not t:
        return {"ok": False, "earned_credit": 0, "logs": [f"[{name}] 未获取到夜猫子任务清单"]}
    if t["status"] == "claimed":
        return {"ok": True, "earned_credit": 0, "logs": [f"[{name}] 夜猫子任务已领奖"]}
    if t["status"] == "not_accepted":
        # 上游只对已接取的任务累计进度；夜猫子任务也必须先接取。
        acc_res = accept_tasks(account, ["black_cat"])
        if not acc_res.get("accepted"):
            why = acc_res.get("msg") or ("; ".join(acc_res.get("reasons") or [])[:120])
            logs.append(f"[{name}] 夜猫子任务未接取, 今晚跳过 (先解决接取失败{': ' + why if why else ''})")
            return {"ok": False, "earned_credit": 0, "logs": logs}
        logs.append(f"[{name}] 夜猫子任务已接取")
    cur, tgt = t.get("current", 0), t.get("target", 3)
    if cur >= tgt:
        res = claim_task(account, "black_cat")
        cr = res.get("credit", 0) if res.get("ok") else 0
        account.fetch_credits()
        logs.append(f"[{name}] 夜猫子任务达标, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
        return {"ok": res.get("ok", False), "earned_credit": cr, "logs": logs}
    ev = build_event(account, "cat", idx=0)
    ok = report_events(account, [ev])
    logs.append(f"[{name}] 上报夜间 GLM-5.2 对话事件: {'成功' if ok else '失败'} (进度 {cur}/{tgt})")
    fresh = None
    for _ in range(4):
        time.sleep(4)
        fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == "black_cat"), None)
        if fresh and fresh.get("current", 0) > cur:
            break
    new_cur = fresh.get("current", cur) if fresh else cur
    if new_cur >= tgt:
        res = claim_task(account, "black_cat")
        cr = res.get("credit", 0) if res.get("ok") else 0
        logs.append(f"[{name}] 夜猫子任务完成 {new_cur}/{tgt}, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
    elif new_cur > cur:
        logs.append(f"[{name}] 今晚 +1 ({new_cur}/{tgt}), 明晚继续, 累计 3 天可领奖")
    else:
        logs.append(f"[{name}] 已上报但进度暂未变化 ({new_cur}/{tgt}), 明晚调度器会继续累计")
    account.fetch_credits()
    earned = 0
    for line in logs:
        m = re.search(r"\+(\d+) 积分", line)
        if m:
            earned = int(m.group(1))
    return {"ok": True, "earned_credit": earned, "logs": logs}

def run_streak_bonus(account):
    """连登管家：补签保连登 -> 礼包/补偿 -> 连登档位兑换 (7d/14d/28d) -> 自动抽奖闭环。

    幂等设计：未解锁/已领/无次数/企业版均安全静默跳过。
    """
    if account.realm != "cn":
        return {"ok": False, "msg": "连登管家仅限国内版账号"}
    if getattr(account, "enterprise_id", None):
        return {"ok": True, "msg": "企业版账号无成长体系，跳过"}

    headers = account.headers("chat")
    logs = []

    # 0. 检查昨日漏签并自动补签
    try:
        req_hm = urllib.request.Request(CHAT_BASE + "/activity/growth/heatmap", headers=headers)
        with _accounts.urlopen(req_hm, timeout=10, proxy=account.proxy) as resp:
            hm_data = json.loads(resp.read().decode("utf-8"))
            cells = (hm_data.get("data") or {}).get("cells", [])
            yesterday_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            y_cell = next((c for c in cells if c.get("date") == yesterday_str), None)
            if y_cell and y_cell.get("score", 0) == 0:
                # 查是否有补签卡
                req_st = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
                with _accounts.urlopen(req_st, timeout=10, proxy=account.proxy) as st_resp:
                    st_data = json.loads(st_resp.read().decode("utf-8"))
                    cards = ((st_data.get("data") or {}).get("makeup_cards") or {}).get("balance", 0)
                    if cards > 0:
                        body = json.dumps({"target_date": yesterday_str}).encode("utf-8")
                        req_use = urllib.request.Request(CHAT_BASE + "/activity/growth/makeup-cards/use",
                                                         data=body, method="POST", headers=headers)
                        with _accounts.urlopen(req_use, timeout=10, proxy=account.proxy) as use_resp:
                            logs.append(f"已消耗 1 张补签卡补签昨日 ({yesterday_str})")
    except Exception as exc:
        _log(f"makeup yesterday failed: {exc}")

    # 1. 尝试领新手礼包与补偿 (每号一次，无则业务错误静默)
    b_hdrs = account.headers("billing")
    for path, name in [("/billing/meter/claim-gift", "新手礼包"),
                       ("/billing/meter/claim-compensation", "活动补偿")]:
        try:
            req_g = urllib.request.Request(BILL_BASE + path, data=b"{}", method="POST", headers=b_hdrs)
            with _accounts.urlopen(req_g, timeout=10, proxy=account.proxy) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                if d.get("code") == 0:
                    cr = (d.get("data") or {}).get("credit", 0)
                    logs.append(f"领取{name} +{cr}积分")
        except Exception:
            pass

    # 2. 检查 7d/14d/28d 连登档位兑换
    try:
        req_st = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
        with _accounts.urlopen(req_st, timeout=10, proxy=account.proxy) as resp:
            st_data = json.loads(resp.read().decode("utf-8"))
            redemp = (st_data.get("data") or {}).get("redemption_status") or {}
            tiers = redemp.get("tiers", [])
            statuses = {
                "7d": redemp.get("tier_7d_status"),
                "14d": redemp.get("tier_14d_status"),
                "28d": redemp.get("tier_28d_status"),
            }
            import uuid
            for t in tiers:
                tier_name = t.get("tier")
                status = statuses.get(tier_name)
                if status in ("locked", "claimed"):
                    continue
                body = json.dumps({"tier": tier_name, "client_token": str(uuid.uuid4())}).encode("utf-8")
                req_rd = urllib.request.Request(CHAT_BASE + "/activity/growth/redeem",
                                                data=body, method="POST", headers=headers)
                try:
                    with _accounts.urlopen(req_rd, timeout=10, proxy=account.proxy) as rd_resp:
                        rd_json = json.loads(rd_resp.read().decode("utf-8"))
                        if rd_json.get("code") == 0:
                            logs.append(f"成功兑换连登 {tier_name} 档位")
                except Exception as exc:
                    _log(f"redeem {tier_name} failed: {exc}")
    except Exception as exc:
        _log(f"streak redeem check failed: {exc}")

    # 3. 自动抽奖闭环 (有 chances 则全抽完)
    drawn_count = 0
    try:
        req_lot = urllib.request.Request(CHAT_BASE + "/activity/growth/lottery/summary", headers=headers)
        with _accounts.urlopen(req_lot, timeout=10, proxy=account.proxy) as resp:
            lot_data = json.loads(resp.read().decode("utf-8"))
            chances = (lot_data.get("data") or {}).get("chances", 0)
            import uuid
            for _ in range(chances):
                body = json.dumps({"client_token": str(uuid.uuid4())}).encode("utf-8")
                req_draw = urllib.request.Request(CHAT_BASE + "/activity/growth/lottery/draw",
                                                  data=body, method="POST", headers=headers)
                try:
                    with _accounts.urlopen(req_draw, timeout=10, proxy=account.proxy) as draw_resp:
                        d_res = json.loads(draw_resp.read().decode("utf-8"))
                        if d_res.get("code") == 0:
                            drawn_count += 1
                except Exception as exc:
                    _log(f"lottery draw failed: {exc}")
                    break
                time.sleep(1.0)
            if drawn_count > 0:
                logs.append(f"完成成长抽奖 {drawn_count} 次")
    except Exception as exc:
        _log(f"lottery summary failed: {exc}")

    msg = "; ".join(logs) if logs else "连登状态正常（无需兑换/无抽奖次数）"
    return {"ok": True, "logs": logs, "msg": msg}
