"""池子空了必须说清「是谁、卡在哪一条」：unavailable_reason() + 503 报错的逐账号原因。

生产实测的背景：上游成片丢连接时，账号治理会把连续失败 3 次的账号熔断 30 分钟
（`note_unknown_failure` → `breaker_until`）；9 个国内账号被关掉几个之后，请求
会在 11ms 内回 503，报错却只列了「停用 / 冷却 / 过期 / 日限额」四种原因——恰好
少了熔断与降权，而看板上那些账号仍显示绿色「可用」。于是「明明有 9 个账号」和
「报没有可用账号」看起来自相矛盾，只能靠猜。

这里钉住三件事：每个账号的一句话原因、拼进报错的那段明细、以及没账号/全可用时
不硬编一个假原因。

    python tests/_test_pool_diagnostics.py
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolated_dirs import isolated_data_dirs    # noqa: E402

_TMP = isolated_data_dirs("wb-pool-diag-")

import wb_accounts as A        # noqa: E402
import wb_proxy as P           # noqa: E402


def account(uid="aaaa1111-bbbb-cccc-dddd-eeeeffff0000", realm="intl", **overrides):
    data = {"uid": uid, "accessToken": "x.y.z", "nickname": uid[:8],
            "enabled": True, "realm": realm}
    data.update(overrides)
    return A.Account(data)


class UnavailableReasonTests(unittest.TestCase):
    def test_healthy_account_has_no_reason(self):
        self.assertEqual(account().unavailable_reason(), "")

    def test_disabled_and_missing_token(self):
        self.assertEqual(account(enabled=False).unavailable_reason(), "已停用")
        self.assertEqual(account(accessToken="").unavailable_reason(), "无凭证")

    def test_each_penalty_names_itself(self):
        """四种惩罚各报各的——只报「冷却」会把人往 429 的方向带。"""
        now = time.time()
        cases = (
            ("breaker_until", "熔断"),
            ("degrade_until", "降权"),
            ("cooldown_until", "软限流冷却"),
            ("balance_until", "余额保护"),
        )
        for field, label in cases:
            acc = account()
            setattr(acc, field, now + 900)
            reason = acc.unavailable_reason(model="deepseek-v4.1-flash")
            self.assertIn(label, reason, field)
            # 只断言「带上了剩余时间」，不断言具体分钟数：_human_delta 是
            # int(秒/60)，而 Windows 的时钟精度是毫秒级、Linux 是纳秒级——
            # 同一份代码在两边会差一分钟，钉死分钟数就是在钉运行环境。
            self.assertIn("剩余", reason, field)

    def test_farthest_penalty_wins(self):
        """同时在熔断和软限流里时，报结束得更晚的那条（否则时间对不上）。"""
        now = time.time()
        acc = account()
        acc.breaker_until = now + 1800
        acc.cooldown_until = now + 120
        self.assertIn("熔断", acc.unavailable_reason(model="m"))

    def test_model_cooldown_is_model_scoped(self):
        """模型级冷却只在这个模型名下报出来，别的模型照常说没事。"""
        now = time.time()
        acc = account()
        acc.model_cooldowns["deepseek-v4.1-flash"] = now + 3600
        self.assertIn("模型 deepseek-v4.1-flash 冷却", acc.unavailable_reason(model="deepseek-v4.1-flash"))
        self.assertEqual(acc.unavailable_reason(model="glm-5.3"), "")
        self.assertEqual(acc.unavailable_reason(), "")

    def test_local_guards(self):
        """本地护栏同样要报得具体：这四种跟「上游罚你」是两回事。"""
        acc = account()
        acc.reserve_credits = 100
        acc.credits = {"remain": 50}
        self.assertEqual(acc.unavailable_reason(), "余额低于保留线")

        acc = account()
        acc.daily_token_limit = 1000
        acc.daily_tokens_today = 1000
        self.assertEqual(acc.unavailable_reason(), "今日 Token 额度用尽")

        acc = account()
        acc.free_models = ()                     # 免费模型不吃积分护栏
        acc.daily_credit_limit = 10
        acc.daily_credits_today = 10
        self.assertEqual(acc.unavailable_reason(model="glm-5.3"), "今日积分额度用尽")

        acc = account()
        acc.model_daily_token_limit = 100
        acc.model_daily_tokens = {"glm-5.3": 100}
        self.assertEqual(acc.unavailable_reason(model="glm-5.3"), "该模型今日额度用尽")

    def test_expired_token(self):
        acc = account()
        acc.expires_at = time.time() - 10
        self.assertEqual(acc.unavailable_reason(), "凭证已过期")

    def test_reason_matches_ready(self):
        """原因与 ready() 必须同进同出：报了原因就一定不可用，反之亦然。"""
        now = time.time()
        samples = [
            account(),
            account(enabled=False),
            account(accessToken=""),
        ]
        for field, _ in (("breaker_until", 0), ("degrade_until", 0),
                         ("cooldown_until", 0), ("balance_until", 0)):
            acc = account()
            setattr(acc, field, now + 600)
            samples.append(acc)
        model = "deepseek-v4.1-flash"
        for acc in samples:
            reason = acc.unavailable_reason(model=model)
            if reason:
                self.assertFalse(acc.ready(model=model), reason)
            else:
                self.assertTrue(acc.ready(model=model))


class PoolDetailTests(unittest.TestCase):
    """拼进 503 报错的那段明细。"""

    def setUp(self):
        self.original = P.POOL
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])

    def tearDown(self):
        P.POOL = self.original

    def test_lists_only_that_realm(self):
        P.POOL = self.pool
        self.pool.accounts = [account(uid="cn000001-0000-0000-0000-000000000000",
                                      realm="cn", enabled=False),
                              account(uid="intl0001-0000-0000-0000-000000000000", enabled=False)]
        detail = P.pool_unavailable_detail("cn", model="m")
        self.assertIn("cn000001", detail)
        self.assertNotIn("intl0001", detail)

    def test_empty_realm_says_so(self):
        """国际版一个账号都没有时要说清楚——这是「没有 intl 账号」那类报错的根。"""
        P.POOL = self.pool
        self.pool.accounts = [account(uid="cn000001-0000-0000-0000-000000000000",
                                      realm="cn", enabled=False)]
        self.assertEqual(P.pool_unavailable_detail("intl", model="m"), "；该区域没有任何账号")

    def test_all_ready_gives_no_detail(self):
        P.POOL = self.pool
        self.pool.accounts = [account()]
        self.assertEqual(P.pool_unavailable_detail("intl", model="m"), "")

    def test_no_pool(self):
        P.POOL = None
        self.assertEqual(P.pool_unavailable_detail("cn"), "")


class ErrorMessageTests(unittest.TestCase):
    """整条路：账号全不可用时，客户端看到的报错要带上明细。"""

    def setUp(self):
        self.original = P.POOL
        self.pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])
        self.pool.accounts = [account(uid="cn000001-0000-0000-0000-000000000000",
                                      realm="cn", enabled=False)]
        P.POOL = self.pool

    def tearDown(self):
        P.POOL = self.original

    def test_open_upstream_reports_each_account(self):
        with self.assertRaises(RuntimeError) as ctx:
            P.open_upstream({"model": "deepseek-v4.1-flash",
                             "messages": [{"role": "user", "content": "hi"}]},
                            target_realm="cn")
        message = str(ctx.exception)
        # 前缀保持原样：gateway_hint 靠 "no usable account for realm" 分类。
        self.assertIn("no usable account for realm 'cn'", message)
        self.assertIn("cn000001", message)
        self.assertIn("已停用", message)
        self.assertIn("no healthy account", P.gateway_hint(503, message))


if __name__ == "__main__":
    unittest.main(verbosity=2)
