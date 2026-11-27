"""Offline regression for BONUS stage entry, ticket switching and accounting."""
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hka.game_flows import GameFlows, LowBonusStamina
from hka.ocr import TextHit, map_page
from hka.runner import SkipTask


class Scenario:
    def __init__(self, balance, initial_cost):
        self.balance = balance
        self.cost = initial_cost
        self.battles = []
        self.toggle_works = True
        runner = SimpleNamespace(device=Mock(), log=Mock(), sleep=Mock(), _task_details={})
        self.flow = GameFlows(runner)
        self.flow._activity = Mock()
        self.flow._home = Mock()
        self.flow._stage = Mock()
        self.flow._stamina = Mock(side_effect=lambda: self.balance)
        self.flow._confirmed_stamina = Mock(return_value=50)
        self.flow._hits = Mock(side_effect=self.detail)
        self.flow._tap = Mock(side_effect=self.tap)
        self.flow._click = Mock(side_effect=self.click)
        self.flow._combat = Mock()

    def detail(self, box):
        assert box == (760, 635, 1415, 730)
        return [TextHit(f"消耗体力 {self.cost}", .99, 808, 662, 116, 21)]

    def tap(self, x, y):
        if (x, y) == (1375, 680) and self.toggle_works:
            self.cost = 150 - self.cost

    def click(self, label, box):
        if label == "开战":
            self.battles.append(self.cost)
            self.balance -= self.cost
        else:
            assert label == "选择助战好友"

    def run(self, task):
        options = {"first_clear": False, "max_runs": 0 if task == 15 else 1,
                   "min_stamina": 200, "use_double_ticket": False}
        if task == 12:
            return self.flow.bonus_double_stamina(options)
        return self.flow.stamina_farm(options)


class BonusTest(unittest.TestCase):
    def test_low_stamina_after_rewards_skips_before_opening_battle_picker(self):
        case = Scenario(29, 50)
        del case.flow._activity
        with self.assertRaises(LowBonusStamina) as error:
            case.flow.stamina_farm({"first_clear": False})
        self.assertEqual(error.exception.balance, 29)
        case.flow._home.assert_called_once_with()
        case.flow._tap.assert_not_called()
        case.flow._click.assert_not_called()

    def test_map_page_reuses_legacy_footer_and_rejects_ambiguous_counters(self):
        png = io.BytesIO()
        Image.new("RGB", (1600, 900)).save(png, "PNG")
        first = TextHit("1/2", .95, 760, 810, 80, 40)
        second = TextHit("2/2", .95, 760, 810, 80, 40)
        chat = TextHit("1/2", .95, 645, 841, 60, 35)
        with patch("hka.ocr.read_text", return_value=[chat, second]), patch("hka.ocr._read_pixels") as blue:
            self.assertEqual(map_page(png.getvalue()), (2, 2))
            blue.assert_not_called()
        with patch("hka.ocr._read_pixels", return_value=[first, second]):
            self.assertIsNone(map_page(png.getvalue(), [chat]))
            self.assertIsNone(map_page(png.getvalue(), [first, second]))

    def test_optional_stage_requires_page_two_and_target_specific_ocr(self):
        flow = Scenario(100, 50).flow
        flow._screen = Mock(return_value=b"map")
        target = TextHit("2-3", .99, 1270, 472, 75, 45)
        with patch("hka.game_flows.stage_labels", return_value=[target]) as labels:
            with patch("hka.game_flows.map_page", side_effect=[(1, 2), None, (2, 2)]):
                self.assertTrue(flow._optional_stage_visible("2-3"))
            self.assertEqual(labels.call_count, 3)
            labels.assert_called_with(b"map", "2-3")
            with patch("hka.game_flows.map_page", side_effect=[(2, 2), None, (2, 2), (2, 2)]):
                self.assertFalse(flow._optional_stage_visible("2-4"))
            with patch("hka.game_flows.map_page", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "未确认第二页"):
                    flow._optional_stage_visible("2-4")

    def test_stage_entry_uses_recovered_target_caption_and_checks_detail(self):
        flow = Scenario(100, 50).flow
        del flow._stage
        flow._screen = Mock(return_value=b"map")
        flow._tap = Mock()
        flow._stage_detail_visible = Mock(return_value=True)
        target = TextHit("2-3", .99, 1270, 472, 74, 45)
        with patch("hka.game_flows.stage_labels", return_value=[target]) as labels:
            flow._stage("2-3")
            labels.assert_called_once_with(b"map", "2-3")
        flow._tap.assert_called_once_with(1307, 582, 1.2)
        flow._stage_detail_visible.assert_called_once_with("2-3")

    def test_boundaries_for_every_entry_and_initial_ticket_state(self):
        for task in (11, 12, 15):
            for balance in (49, 50, 51, 99, 100, 101):
                for initial_cost in (50, 100):
                    with self.subTest(task=task, balance=balance, initial=initial_cost):
                        case = Scenario(balance, initial_cost)
                        if balance < 50:
                            with self.assertRaises(LowBonusStamina):
                                case.run(task)
                            self.assertEqual(case.battles, [])
                            case.flow._stage.assert_not_called()
                        else:
                            self.assertEqual(case.run(task)["runs"], 1)
                            self.assertEqual(case.battles, [100 if balance >= 100 else 50])
                            case.flow._combat.assert_called_once()

    def test_unlimited_crosses_threshold_and_stops(self):
        case = Scenario(251, 50)
        result = case.run(15)
        self.assertEqual(case.battles, [100, 100, 50])
        self.assertEqual((result["runs"], result["final_stamina"]), (3, 1))

    def test_balance_changes_while_setting_ticket(self):
        case = Scenario(100, 100)
        case.flow._stamina.side_effect = [99, 100, 100]
        case.flow._run_stage("BONUS", {})
        self.assertEqual(case.battles, [100])

    def test_balance_drops_below_50_before_start(self):
        case = Scenario(70, 100)
        case.flow._stamina.side_effect = [70, 49]
        with self.assertRaises(LowBonusStamina):
            case.flow._run_stage("BONUS", {})
        case.flow._click.assert_not_called()

    def test_unacknowledged_toggle_and_unknown_cost_stop_before_start(self):
        for initial in (100, 75):
            with self.subTest(initial=initial):
                case = Scenario(80, initial)
                case.toggle_works = False
                with self.assertRaises(RuntimeError):
                    case.run(11)
                case.flow._click.assert_not_called()
                self.assertLessEqual(case.flow._tap.call_count, 1)

    def test_extra_balance_checked_again_at_detail(self):
        for readings in ([49], [100, 49]):
            case = Scenario(151, 50)
            case.flow._confirmed_stamina.side_effect = readings
            with self.assertRaises(SkipTask):
                case.run(12)
            case.flow._click.assert_not_called()

    def test_incomplete_battle_is_never_retried(self):
        case = Scenario(250, 50)
        case.flow._combat.side_effect = RuntimeError("unconfirmed map")
        with self.assertRaisesRegex(RuntimeError, "unconfirmed map"):
            case.run(15)
        self.assertEqual(case.battles, [100])

    def test_failed_second_battle_preserves_first_settlement(self):
        case = Scenario(250, 50)
        case.flow._combat.side_effect = [None, RuntimeError("未确认结算")]
        with self.assertRaisesRegex(RuntimeError, "未确认结算"):
            case.run(15)
        self.assertEqual(case.flow.runner._task_details["stamina_farm"]["runs"], 1)

    def test_extra_after_confirmed_settlement(self):
        case = Scenario(100, 50)
        case.flow._stamina.side_effect = [100, 100, 100, RuntimeError("HUD unavailable")]
        result = case.run(12)
        self.assertEqual(result["runs"], 1)
        self.assertIsNone(result["final_stamina"])
        self.assertEqual(case.battles, [100])

    def test_hidden_double_meter_can_only_skip_after_consistent_ordinary_frames(self):
        case = Scenario(100, 50)
        flow = case.flow
        del flow._confirmed_stamina
        flow._screen = Mock(return_value=b"frame")
        ordinary = [TextHit("体力", .99, 290, 80, 38, 18), TextHit("185/205", .99, 331, 80, 78, 18)]
        for sequence, expected_error in (([ordinary] * 20, SkipTask), ([[]] + [ordinary] * 19, RuntimeError)):
            with patch("hka.game_flows.read_stamina_hud", side_effect=sequence):
                with self.assertRaises(expected_error):
                    flow._confirmed_stamina(doubled=True)

    def test_home_reuses_each_frame(self):
        runner = SimpleNamespace(device=Mock(), log=Mock(), sleep=Mock(), cancel=Mock())
        runner.cancel.is_set.return_value = False
        flow = GameFlows(runner)
        flow._screen = Mock(return_value=b"one frame")
        flow._tap = Mock()
        home = TextHit("首页", .99, 780, 80, 42, 18)
        def match(frame, directory, options):
            self.assertEqual(frame.png, b"one frame")
            return (800, 90) if options["image"] == "home_active.png" else None
        with patch("hka.game_flows.find_image", match), patch("hka.game_flows.read_text", return_value=[home]):
            flow._home()
        self.assertEqual(flow._screen.call_count, 2)
        flow._tap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
