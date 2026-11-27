import unittest
from unittest.mock import Mock, call, patch

from hka.game_flows import GameFlows
from hka.ocr import TextHit
from hka.runner import SkipTask


def hit(text, x, y):
    return TextHit(text, 0.99, x, y, 20, 20)


class LoginMailReceiptTests(unittest.TestCase):
    daily = [hit("每日签到", 600, 270), hit("第一天", 410, 450), hit("第二天", 530, 450),
             hit("领取", 1100, 625)]
    announcement = [hit("活动公告", 172, 81), hit("祈愿公告", 506, 80), hit("系统公告", 842, 79)]
    home = [hit("首页", 790, 80), hit("战斗", 945, 80)]
    mailbox = [hit("体力领取", 1082, 152), hit("一键领取", 1396, 150)]
    dimmed = [hit("体力领取", 1082, 152), hit("键领取", 1396, 150)]

    def flow(self, frames):
        runner = Mock()
        runner.cancel.is_set.return_value = False
        flow = GameFlows(runner)
        flow._home = Mock()
        flow._tap = Mock()
        flow._screen = Mock(return_value=b"frame")
        flow._hits = Mock(side_effect=frames)
        return flow

    def test_dismisses_announcement_after_login_claim_then_verifies_return(self):
        flow = self.flow([self.daily, self.announcement, *[self.home] * 3])
        flow._navigation_card = Mock(side_effect=[("游戏公告", 1461, 95), None, None, None])
        with patch("hka.game_flows.find_image", return_value=(810, 85, 1.0)):
            self.assertEqual(flow.claim_login_rewards({}), {"claimed": 1, "pages": ["每日签到"]})
        self.assertEqual(flow._tap.call_args_list, [call(1110, 635, 1), call(1461, 95, 1.0)])

    def test_announcement_does_not_confirm_pending_claim(self):
        flow = self.flow([self.daily, self.announcement] + [[]] * 38)
        flow._dismiss_navigation_card = Mock(return_value=True)
        with self.assertRaisesRegex(RuntimeError, "未确认领取结果"):
            flow.claim_login_rewards({})
        flow._tap.assert_called_once_with(1110, 635, 1)

    def test_promotional_card_over_home_requires_fresh_verification(self):
        for remaining, confirmed in (([self.home] * 3, True), ([[]] * 38, False)):
            with self.subTest(confirmed=confirmed):
                flow = self.flow([self.daily, self.home, *remaining])
                promo = ("活动宣传卡片", 1296, 236)
                flow._navigation_card = Mock(side_effect=[promo, None, None, None])
                flow.dismiss_login_popups = Mock()
                with patch("hka.game_flows.find_image", return_value=(810, 85, 1.0)):
                    if confirmed:
                        self.assertEqual(flow.claim_login_rewards({})["pages"], ["每日签到"])
                        self.assertEqual(flow._screen.call_count, 5)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "未确认领取结果"):
                            flow.claim_login_rewards({})
                flow.dismiss_login_popups.assert_called_once_with({})
                flow._tap.assert_called_once_with(1110, 635, 1)

    def test_mail_receipt_then_stable_inbox(self):
        receipt = [*self.dimmed, hit("领取成功", 725, 257)]
        flow = self.flow([self.mailbox, receipt, *[self.mailbox] * 4])
        self.assertEqual(flow.claim_mail({}), {"claimed": True})
        self.assertEqual(flow._tap.call_args_list, [call(1406, 160, 0.2), call(1510, 740, 1.0)])

    def test_mail_receipt_requires_toolbar_and_centered_title(self):
        title = hit("领取成功", 725, 257)
        for hits in ([title], [title, self.dimmed[0]], [*self.dimmed, hit("领取成功", 400, 705)]):
            self.assertFalse(GameFlows._mail_claim_receipt(hits))

    def test_unrelated_notice_does_not_confirm_mail_claim(self):
        notice = [hit("提示 未领取 活动奖励 背包", 700, 400), hit("确定", 705, 570)]
        flow = self.flow([self.mailbox, notice, *[self.mailbox] * 4])
        with self.assertRaisesRegex(SkipTask, "未见本次附件领取回执"):
            flow.claim_mail({})

    def test_empty_mailbox_with_explicit_message(self):
        empty = self.mailbox + [hit("暂无邮件", 720, 480)]
        flow = self.flow([empty] * 5)
        with self.assertRaisesRegex(SkipTask, "没有可领取附件"):
            flow.claim_mail({})

    def test_login_title_alias_does_not_resubmit(self):
        sequence = [[hit(name, 600, 270), *self.daily[1:]]
                    for name in ["周年连登", "周年连续登陆", "周年连续登录"]]
        flow = self.flow(sequence + [self.home] * 3)
        flow._navigation_card = Mock(return_value=None)
        with patch("hka.game_flows.find_image", return_value=(810, 85, 1.0)):
            self.assertEqual(flow.claim_login_rewards({})["pages"], ["周年连续登录"])
        flow._tap.assert_called_once()

    def test_archived_empty_notice_acknowledged_once_then_inbox_verified(self):
        notice = self.dimmed + [hit("提示", 751, 289), hit("暂时没有消息", 528, 381), hit("确定", 747, 538)]
        flow = self.flow([self.mailbox, notice, notice, *[self.mailbox] * 4])
        with self.assertRaisesRegex(SkipTask, "没有可领取附件"):
            flow.claim_mail({})
        self.assertEqual(flow._tap.call_args_list, [call(1406, 160, 0.2), call(757, 548, 0.6)])
