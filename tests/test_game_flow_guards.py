import unittest
from unittest.mock import Mock, call, patch

from hka.game_flows import GameFlows
from hka.ocr import TextHit


def hit(text, x=700, y=400):
    return TextHit(text, .99, x, y, 20, 20)


class GiftConfirmationTests(unittest.TestCase):
    def gift(self, confirmation):
        flow = GameFlows(Mock())
        flow._shop = Mock()
        flow._click = Mock()
        flow._tap = Mock()
        flow._hits = Mock(side_effect=[
            [hit("零时馈礼", 600, 300), hit("×0", 600, 480), hit("购买", 600, 540)],
            [hit("零时馈礼"), *confirmation],
        ])
        flow._text = Mock(return_value=hit("购买成功"))
        return flow

    def test_rejects_unverified_and_conflicting_prices(self):
        for labels in ([], ["非免费"], ["消耗水晶 60"], ["免费", "消耗水晶 60"],
                       ["免费次数已用完"], ["×0", "水晶", "60"]):
            with self.subTest(labels=labels):
                flow = self.gift([*(hit(label) for label in labels), hit("购买", 720, 550)])
                with self.assertRaisesRegex(RuntimeError, "最终价格"):
                    flow.free_gift({})
                flow._tap.assert_called_once_with(610, 550)

    def test_rejects_ambiguous_buttons(self):
        flow = self.gift([hit("免费"), hit("购买", 720, 550), hit("确认购买", 850, 550)])
        with self.assertRaisesRegex(RuntimeError, "唯一"):
            flow.free_gift({})
        flow._tap.assert_called_once_with(610, 550)

    def test_verified_free_gift_completes(self):
        for price in ("免费", "消耗水晶 0", "×0"):
            with self.subTest(price=price):
                flow = self.gift([hit(price), hit("购买", 720, 550)])
                flow.free_gift({})
                self.assertEqual(flow._tap.call_args_list,
                                 [call(610, 550), call(730, 560, 0.9), call(800, 555)])


class CombatEntryTests(unittest.TestCase):
    def flow(self, frames):
        runner = Mock()
        runner.cancel.is_set.return_value = False
        runner.device.foreground_package.return_value = runner.device.package
        flow = GameFlows(runner)
        flow._hits = Mock(side_effect=frames)
        flow._tap = Mock()
        flow._swipe = Mock()
        return flow

    def test_unknown_initial_screen_receives_no_inputs(self):
        flow = self.flow([[]] * 8)
        with self.assertRaisesRegex(RuntimeError, "未确认战斗画面"):
            flow._combat({})
        flow._tap.assert_not_called()
        flow._swipe.assert_not_called()

    def test_unknown_screen_after_combat_entry_suspends_inputs(self):
        flow = self.flow([[hit("HP 10/10", 20, 20)]] + [[hit("网络正在重连")]] * 4)
        with self.assertRaisesRegex(RuntimeError, "连接提示"):
            flow._combat({})
        flow._tap.assert_called_once_with(1480, 565, .1)
        flow._swipe.assert_not_called()

    def test_background_health_bar_does_not_override_reconnect_prompt(self):
        flow = self.flow([[hit("HP 10/10", 20, 20), hit("网络连接中断")]])
        with self.assertRaisesRegex(RuntimeError, "连接提示"):
            flow._combat({})
        flow._tap.assert_not_called()
        flow._swipe.assert_not_called()

    def test_waits_for_hud_then_fights_and_settles(self):
        result = [hit("战斗结算", 800, 150), hit("确定", 800, 760), hit("再次挑战", 1000, 760)]
        stage_map = [hit("首页", 780, 100), hit("战斗", 950, 100)]
        hud = [hit("HP 10/10", 20, 20)]
        flow = self.flow([[], hud, hud, result, stage_map, stage_map])
        flow._combat({})
        self.assertEqual(flow._tap.call_args_list, [call(1480, 565, .1), call(810, 770, 1.0)])
        flow._swipe.assert_called_once()


class BattlePickerTests(unittest.TestCase):
    def test_recognizes_tabs_on_one_frame_and_preserves_crop_fallback(self):
        tabs = [hit("活动", 80, 250), hit("挑战", 80, 330)]
        for full in (tabs, []):
            with self.subTest(full=full):
                runner = Mock()
                runner.cancel.is_set.return_value = False
                flow = GameFlows(runner)
                flow._home = Mock()
                flow._tap = Mock()
                flow._screen = Mock(return_value=b"picker")
                with patch("hka.game_flows.read_text", side_effect=[full, tabs]) as ocr:
                    flow._battle_picker()
                flow._screen.assert_called_once()
                self.assertEqual(ocr.call_count, 1 if full else 2)
                self.assertTrue(all(c.args[0] == b"picker" for c in ocr.call_args_list))


class LoginRewardTests(unittest.TestCase):
    def page(self, name):
        return [hit(name, 600, 270), hit("第一天", 410, 450), hit("第二天", 530, 450),
                hit("领取", 1100, 625)]

    def flow(self, frames):
        runner = Mock()
        runner.cancel.is_set.return_value = False
        flow = GameFlows(runner)
        flow._screen = Mock(return_value=b"screen")
        flow._hits = Mock(side_effect=frames)
        flow._navigation_card = Mock(return_value=None)
        flow._tap = Mock()
        return flow

    def test_unknown_frames_and_unrelated_notice_do_not_confirm(self):
        notice = [hit("提示 未领取 活动奖励 背包"), hit("确定", 705, 570)]
        flow = self.flow([self.page("每日签到"), notice] + [[]] * 38)
        with self.assertRaisesRegex(RuntimeError, "未确认领取结果"):
            flow.claim_login_rewards({})
        self.assertEqual(flow._tap.call_count, 2)
        self.assertFalse(any("已确认领取" in str(entry) for entry in flow.log.call_args_list))

    def test_confirms_next_reward_page_and_waits_for_stable_home(self):
        second = self.page("连续登录")
        home = [hit("首页", 790, 80), hit("战斗", 945, 80)]
        flow = self.flow([self.page("每日签到"), [], second, [], second, second,
                          home, [], home, home, home])
        with patch("hka.game_flows.find_image", return_value=(810, 85, 1.0)):
            self.assertEqual(flow.claim_login_rewards({}), {"claimed": 2, "pages": ["每日签到", "连续登录"]})
        self.assertEqual(flow._tap.call_count, 2)
        self.assertEqual(flow._screen.call_count, 11)

    def test_receipt_closes_before_return_home(self):
        home = [hit("首页", 790, 80), hit("战斗", 945, 80)]
        flow = self.flow([self.page("每日签到"), [hit("已领取以下奖励", 700, 60)], *[home] * 3])
        with patch("hka.game_flows.find_image", return_value=(810, 85, 1.0)):
            self.assertEqual(flow.claim_login_rewards({})["claimed"], 1)
        self.assertEqual(flow._tap.call_args_list, [call(1110, 635, 1), call(1450, 740, 1.0)])


class QuickBattleTests(unittest.TestCase):
    def dialog(self, count, after, buttons=()):
        return [hit("快捷战斗", 700, 340), hit("100", 710, 420), hit(str(after), 810, 420),
                hit(str(count), 730, 485), *buttons]

    def flow(self, frames):
        flow = GameFlows(Mock())
        flow._hits = Mock(side_effect=frames)
        flow._click = Mock()
        flow._tap = Mock()
        return flow

    def test_rechecks_all_values_before_accepting_late_button(self):
        button = hit("确定", 680, 560)
        for changed in ([button], self.dialog(1, 40, [button]),
                        self.dialog(1, 70, [button, hit("确定", 755, 560)])):
            with self.subTest(changed=changed):
                flow = self.flow([self.dialog(1, 70), changed])
                with self.assertRaises(RuntimeError):
                    flow._execute_quick("使魔的爱", 1)
                flow._tap.assert_not_called()

    def test_adjust_count_then_confirm_and_settle(self):
        button = hit("确定", 680, 560)
        result = [hit("战斗结算", 780, 160), hit("第1次快捷战斗结果", 700, 280), hit("确定", 850, 760)]
        stage_map = [hit("使魔的爱", 670, 300), hit("虚轴之庭", 1100, 300)]
        flow = self.flow([self.dialog(3, 10), self.dialog(2, 40), self.dialog(1, 70),
                          self.dialog(1, 70, [button]), result, stage_map])
        self.assertEqual(flow._execute_quick("使魔的爱", 1), 1)
        flow._click.assert_called_once_with("快捷战斗", (1040, 740, 1440, 870))
        self.assertEqual(flow._tap.call_args_list, [call(641, 492), call(641, 492),
                                                   call(690, 570), call(860, 770)])

    def test_quick_result_must_close_after_one_confirmation(self):
        button = hit("确定", 680, 560)
        result = [hit("战斗结算", 780, 160), hit("第1次快捷战斗结果", 700, 280), hit("确定", 850, 760)]
        flow = self.flow([self.dialog(1, 70, [button])] + [result] * 9)
        with self.assertRaisesRegex(RuntimeError, "未确认已离开结算页"):
            flow._execute_quick("使魔的爱", 1)
        self.assertEqual(flow._tap.call_count, 2)

    def test_compact_quick_result_recovers_before_background_home(self):
        home = [hit("首页", 790, 84), hit("战斗", 950, 84)]
        result = home + [TextHit("战斗结算", .99, 731, 146, 111, 35),
                         TextHit("【第1次快捷战斗结果】", .99, 722, 199, 207, 27),
                         TextHit("确定", .99, 949, 748, 70, 42)]
        flow = self.flow([self.dialog(1, 70, [hit("确定", 680, 560)]), result, result, home])
        self.assertEqual(flow._execute_quick("使魔的爱", 1), 1)
        self.assertEqual(flow._tap.call_args_list, [call(690, 570), call(984, 769)])

        flow = self.flow([result[:3], result, result, home, home])
        flow._screen = Mock(return_value=b"screen")
        flow._dismiss_navigation_card = Mock(return_value=False)
        with patch("hka.game_flows.find_image", return_value=(810, 85, 1.0)):
            flow._home()
        self.assertEqual(flow._tap.call_args_list, [call(984, 769, 1.0), call(800, 94, 2.5)])
        for invalid in (result[:-1], result[:3] + result[4:], result + [hit("确定", 820, 760)]):
            self.assertIsNone(flow._verified_combat_result_button(invalid))


if __name__ == "__main__":
    unittest.main()
