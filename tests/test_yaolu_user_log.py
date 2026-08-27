import unittest
from types import SimpleNamespace

from app.ui.pages._impl import YaoluPage


class YaoluUserLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = SimpleNamespace(
            _current_round=2,
            _enter_ok_count=1,
            _daily_ok_count=1,
            _risk_constraint_from_ui=lambda: 50,
        )

    def format(self, phase, message, *, ok=True, detail=None):
        return YaoluPage._format_yaolu_user_log(
            self.page,
            phase,
            message,
            ok=ok,
            detail=detail or {},
        )

    def test_suppresses_countdowns_and_routine_flow(self) -> None:
        self.assertIsNone(
            self.format(
                "status",
                "跨图/切图后等待稳定，剩余 12s",
            )
        )
        self.assertIsNone(self.format("gate", "门控通过: 已组队且是队长"))
        self.assertIsNone(self.format("path_open", "寻路/打开 九层妖楼暗道"))
        self.assertIsNone(self.format("wait_return", "等待退出妖楼回到福州"))
        self.assertIsNone(self.format("recover", "恢复结果 ok=True"))
        self.assertIsNone(
            self.format(
                "path_open",
                "未确认验证码弹窗，就地再开入口 1/2",
                ok=False,
                detail={"reason": "no_captcha_dialog_retry"},
            )
        )

    def test_keeps_compact_round_submit_and_success_milestones(self) -> None:
        self.assertEqual(
            self.format(
                "round",
                "第 2 轮 开始",
                detail={"daily_ok": 1, "daily_limit": 30},
            ),
            "第2轮开始 · 今日1/30 · 风控50%",
        )
        self.assertEqual(
            self.format(
                "captcha",
                "已提交 动物=蜘蛛 pos=[1, 3] conf=0.917182",
            ),
            "第2轮 · 验证码已提交 · 蜘蛛 · 置信度92%",
        )
        self.assertEqual(
            self.format(
                "enter_ok",
                "进入成功 scene=1529",
                detail={"daily_ok": 2, "daily_limit": 30},
            ),
            "第2轮进入成功 · 成功×1 · 今日2/30",
        )

    def test_keeps_only_terminal_failures(self) -> None:
        self.assertEqual(
            self.format(
                "path_open",
                "内存未确认验证码弹窗（交互未成功/CD/服务器拒绝）",
                ok=False,
                detail={"reason": "no_captcha_dialog"},
            ),
            "第2轮 · 入口未弹验证码，已恢复并稍后重试",
        )
        self.assertEqual(
            self.format(
                "captcha",
                "确认未生效，未提交：确认按钮未生效",
                ok=False,
            ),
            "第2轮 · 确认按钮未生效，已恢复并稍后重试",
        )
        self.assertEqual(
            self.format(
                "enter_fail",
                "答案错误，请大侠重新来过",
                ok=False,
            ),
            "第2轮 · 验证码答案错误，稍后重试",
        )


if __name__ == "__main__":
    unittest.main()
