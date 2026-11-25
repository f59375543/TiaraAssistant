"""Offline regression for BONUS stage entry, ticket switching and accounting."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hka.game_flows import GameFlows, LowBonusStamina
from hka.ocr import TextHit
from hka.runner import SkipTask


class Scenario:
    def __init__(self, balance, initial_cost):
        self.balance = balance
        self.cost = initial_cost
        self.battles = []
        self.toggle_works = True
        runner = SimpleNamespace(device=Mock(), log=Mock(), sleep=Mock())
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
            self.assertEqual(frame, b"one frame")
            return (800, 90) if options["image"] == "home_active.png" else None
        with patch("hka.game_flows.find_image", match), patch("hka.game_flows.read_text", return_value=[home]):
            flow._home()
        self.assertEqual(flow._screen.call_count, 2)
        flow._tap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
