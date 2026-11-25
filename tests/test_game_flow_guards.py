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

    def test_waits_for_hud_then_fights_and_settles(self):
        result = [hit("战斗结算", 800, 150), hit("确定", 800, 760), hit("再次挑战", 1000, 760)]
        stage_map = [hit("首页", 780, 100), hit("战斗", 950, 100)]
        flow = self.flow([[], [hit("HP 10/10", 20, 20)], result, stage_map, stage_map])
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


if __name__ == "__main__":
    unittest.main()
