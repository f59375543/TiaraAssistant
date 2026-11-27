import io
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch
from PIL import Image

from hka.game_flows import GameFlows
from hka.ocr import TextHit
from hka.runner import SkipTask


def hit(text, x, y):
    return TextHit(text, .99, x, y, 40, 20)


class TaskOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.flow = GameFlows(Mock())
        self.flow.runner._task_details = {}
        self.flow._tap = Mock()
        self.flow._click = Mock()
        self.flow._tasks = Mock()

    def test_existing_detail_for_zero_score(self):
        self.flow._stage = Mock()
        self.flow._hits = Mock(return_value=[hit("1-1", 790, 160), hit("最高得分 0", 800, 220)])
        self.flow._start_stage_from_detail = Mock()
        self.flow._first_clear_stage("1-1", {})
        self.flow._stage.assert_called_once_with("1-1")
        self.flow._start_stage_from_detail.assert_called_once_with("1-1", {})
        self.flow._tap.assert_not_called()

    def test_activity_skips_excluded_map_and_reads_new_list(self):
        listing = [hit("难度指数 B", 650, 300), hit("剩余时间 1天", 660, 320),
                   hit("难度指数 B", 1170, 400), hit("剩余时间 3天", 1190, 430)]
        self.flow._battle_picker = Mock()
        self.flow._screen = Mock(return_value=b"map")
        self.flow._hits = Mock(side_effect=[listing, [hit("空想迷域", 130, 20)], listing, []])
        with patch("hka.game_flows.stage_labels", return_value=[hit("BONUS", 500, 500)]):
            self.flow._activity()
        self.assertEqual([c.args[:2] for c in self.flow._tap.call_args_list], [(460, 415), (980, 515)])
        self.assertEqual(self.flow._battle_picker.call_count, 2)

    def test_unchanged_list_ignores_countdown(self):
        self.flow._battle_picker = Mock()
        self.flow._swipe = Mock()
        self.flow._hits = Mock(side_effect=[[hit("难度指数 C", 1200, 400), hit(f"剩余时间 {n}秒", 1210, 430)]
                                           for n in (30, 29, 28, 27)])
        with self.assertRaises(SkipTask):
            self.flow._activity()
        self.assertEqual(self.flow._swipe.call_count, 2)

    def test_presence_refuses_weekly_page(self):
        weekly = [hit("每周", 311, 212), hit("当前存在感,720", 1140, 200), hit("一键领取", 1237, 256)]
        self.flow._hits = Mock(return_value=weekly)
        with self.assertRaisesRegex(RuntimeError, "未确认存在感奖励页"):
            self.flow.claim_presence({})
        self.flow._tap.assert_not_called()

    def test_unverified_result_after_rewards_is_not_reported_as_empty(self):
        daily = [hit("每日", 312, 208), hit("当前存在感,170/150", 1151, 201), hit("一键领取", 1236, 254)]
        self.flow._hits = Mock(side_effect=[daily] + [[]] * 10)
        with self.assertRaisesRegex(RuntimeError, "未能核实"):
            self.flow.claim_presence({})
        self.flow._tap.assert_called_once()

    def test_centered_presence_receipt_closes_once_and_waits_for_return(self):
        daily = [hit("每日", 312, 208), hit("当前存在感,170/150", 1151, 201), hit("一键领取", 1236, 254)]
        receipt = [TextHit("已领取以下奖励", .99, 668, 162, 282, 44),
                   TextHit("(点击空白处关闭)", .99, 692, 573, 254, 43)]
        self.flow._hits = Mock(side_effect=[daily, daily + receipt, daily + receipt, daily, [], daily, daily])
        self.flow.claim_presence({})
        self.assertEqual(self.flow._tap.call_args_list, [call(1256, 264), call(819, 594, 1.0)])
        self.assertEqual(self.flow._hits.call_count, 7)

    def test_receipt_must_clear_even_if_daily_page_is_visible_behind_it(self):
        daily = [hit("每日", 312, 208), hit("当前存在感,170/150", 1151, 201), hit("一键领取", 1236, 254)]
        receipt = [hit("已领取以下奖励", 668, 162), hit("（点击空白处关闭）", 692, 573)]
        self.flow._hits = Mock(side_effect=[daily] + [daily + receipt] * 10)
        with self.assertRaisesRegex(RuntimeError, "未能核实"):
            self.flow.claim_presence({})
        self.assertEqual(self.flow._tap.call_args_list, [call(1256, 264), call(712, 583, 1.0),
                                                         call(1450, 740, 1.0)])

    def test_persistent_compact_receipt_retries_backdrop_then_verifies_page(self):
        daily = [hit("每日", 312, 208), hit("当前存在感,20/150", 1151, 201), hit("一键领取", 1236, 254)]
        receipt = [TextHit("已领取以下奖励", .99, 668, 161, 281, 44),
                   TextHit("(点击空白处关闭)", .99, 694, 577, 251, 37)]
        self.flow._hits = Mock(side_effect=[daily, receipt, [], receipt, receipt, receipt, daily, daily])
        self.flow.claim_presence({})
        self.assertEqual(self.flow._tap.call_args_list, [call(1256, 264), call(819, 595, 1.0),
                                                         call(1450, 740, 1.0)])
        self.assertEqual(self.flow._hits.call_count, 8)

    def test_unrelated_notices_never_trigger_centered_receipt_dismissal(self):
        title = hit("已领取以下奖励", 668, 162)
        close = hit("（点击空白处关闭）", 692, 573)
        for frame in ([title], [close], [hit("预览已领取以下奖励", 668, 162), close],
                      [title, hit("点击空白处关闭", 120, 573)], [title, hit("确定", 692, 573)]):
            with self.subTest(frame=frame):
                self.assertFalse(self.flow._dismiss_navigation_notice(frame))
                self.flow._tap.assert_not_called()

    def test_legacy_top_receipt_keeps_its_blank_area(self):
        self.assertTrue(self.flow._dismiss_navigation_notice([hit("【已领取以下奖励】", 690, 60)]))
        self.flow._tap.assert_called_once_with(1450, 740, 1.0)

    def test_navigation_waits_for_the_same_receipt_to_clear(self):
        receipt = [hit("已领取以下奖励", 668, 162), hit("（点击空白处关闭）", 692, 573)]
        for frame, expected in ((receipt, True), (receipt, True), ([], False), (receipt, True)):
            self.assertEqual(self.flow._dismiss_navigation_notice(frame), expected)
        self.assertEqual(self.flow._tap.call_args_list, [call(712, 583, 1.0)] * 2)

    def test_dispatch_requires_increased_count(self):
        self.flow.runner.last_decision_frame = b"team"
        self.flow.runner._save_screenshot.side_effect = [Path("list.png"), Path("team.png")]
        self.flow._screen = Mock(return_value=b"frame")
        self.flow._scroll_expeditions = Mock(return_value=False)
        self.flow._read_region = Mock(side_effect=[[], [], [hit("点击选择使魔", 1250, 380)]])
        self.flow._hits = Mock(return_value=[hit("已接取的探险数2/10", 308, 176)])
        self.flow._text = Mock(return_value=hit("成功率", 1250, 175))
        with self.assertRaisesRegex(RuntimeError, "出发前 2/10，出发后最后读数 2/10"):
            self.flow.expeditions({})
        self.assertEqual([c.args[0] for c in self.flow._click.call_args_list], ["一键派遣", "出发"])
        evidence = self.flow.runner._task_details["expedition_dispatch"]
        self.assertEqual((evidence["before"], evidence["after"], evidence["success_rate_text"]),
                         ("2/10", "2/10", "成功率"))
        self.assertEqual((evidence["list_screenshot"], evidence["team_screenshot"]), ("list.png", "team.png"))
        self.assertEqual(self.flow.runner._save_screenshot.call_args_list,
                         [call("expeditions_list", b"frame"), call("expeditions_team", b"team")])

    def test_dispatch_count_reads_only_a_complete_header_fraction(self):
        label = hit("已接取的探险数", 308, 176)
        for words in ([label, hit("/10", 540, 176)], [label, hit("11/10", 540, 176)],
                      [label, hit("3/10", 580, 370)]):
            self.assertIsNone(self.flow._expedition_count(words))
        self.assertEqual(self.flow._expedition_count([label, hit("3／10", 521, 169)]), (3, 10))
        self.flow._read_region = Mock(return_value=[label, hit("3/10", 521, 169)])
        self.assertEqual(self.flow._dispatch_count(b"frame", [label, hit("/10", 540, 176)]), (3, 10))
        self.flow._read_region.assert_called_once_with(b"frame", (260, 156, 680, 233), 0.75)

    def test_dispatch_verifies_increment_with_same_limit_and_stops_when_full(self):
        near_cap = [hit("已接取的探险数9/10", 308, 176)]
        full = [hit("已接取的探险数10/10", 308, 176)]
        self.flow._screen = Mock(return_value=b"frame")
        self.flow._scroll_expeditions = Mock(return_value=False)
        self.flow._read_region = Mock(side_effect=[[], [], [hit("点击选择使魔", 1250, 380)]])
        self.flow._hits = Mock(side_effect=[near_cap] * 5 + [[hit("已接取的探险数10/12", 308, 176)], full, full])
        self.flow._text = Mock(return_value=hit("成功率80%", 1250, 175))
        self.assertEqual(self.flow.expeditions({}), {"claimed": 0, "dispatched": 1})
        self.assertEqual(self.flow._hits.call_count, 8)
        self.assertEqual(self.flow.runner._task_details["expedition_dispatch"],
                         {"before": "9/10", "after": "10/10", "success_rate_text": "成功率80%",
                          "verification": "count_increased"})

    def test_popup_results_report_actual_count(self):
        png = io.BytesIO()
        Image.new("L", (1600, 900)).save(png, "PNG")
        self.flow._screen = Mock(return_value=png.getvalue())
        for closed in (0, 2, 7):
            with self.subTest(closed=closed):
                cards = [(f"卡片{n % 2}", 1300, 120) for n in range(closed)]
                self.flow._tap.reset_mock()
                self.flow._navigation_card = Mock(side_effect=[c for card in cards for c in (card, card)] + [None, None])
                result = self.flow.dismiss_login_popups({})
                self.assertEqual(result["closed"], closed)
                self.assertEqual(self.flow._tap.call_count, closed)
                self.flow.log.assert_called_with(result["message"])

    def test_stage_title_requires_exact_id(self):
        self.assertTrue(GameFlows._matches_stage_title([hit("关卡 1-1", 790, 160)], "1-1"))
        self.assertFalse(GameFlows._matches_stage_title([hit("关卡 1-10", 790, 160)], "1-1"))

    def test_paid_refresh_never_confirms(self):
        self.flow._shop = Mock()
        for text in ("免费刷新次数已用完", "免费刷新 剩余0次免费刷新", "免费刷新 ×60", "免费刷新 消耗水晶",
                     "免费刷新次数已达上限，是否使用金币刷新？"):
            with self.subTest(text=text):
                self.flow._tap.reset_mock()
                self.flow._hits = Mock(side_effect=[
                    [hit("剩余次数 1", 1100, 190)],
                    [hit(text, 710, 350), hit("确定", 710, 550)]])
                with self.assertRaisesRegex(RuntimeError, "未确认本次为免费"):
                    self.flow.refresh_resonance({})
                self.flow._tap.assert_called_once_with(1360, 190)

    def test_confirmed_free_refresh_decrements_once(self):
        self.flow._shop = Mock()
        self.flow._hits = Mock(side_effect=[[hit("剩余次数 2", 1100, 190)],
                                            [hit("是否使用1次免费刷新", 710, 350), hit("确定", 710, 550)],
                                            [hit("剩余次数 1", 1100, 190)]])
        self.flow.refresh_resonance({})
        self.assertEqual(self.flow._tap.call_count, 2)

    def test_last_free_refresh_switches_to_paid_price_without_another_submission(self):
        self.flow._shop = Mock()
        paid = [hit("消耗1零时之种", 1065, 158), hit("15小时49分后刷新", 1066, 197)]
        dialog = [hit("你确定要进行免费刷新吗？", 528, 382), hit("确定", 615, 540)]
        self.flow._hits = Mock(side_effect=[[hit("剩余次数1次", 1090, 164)], dialog, [], paid, paid])
        self.flow.refresh_resonance({})
        self.assertEqual(self.flow._tap.call_args_list, [call(1360, 190), call(635, 550, 1.0)])
        evidence = self.flow.runner._task_details["refresh_resonance"]
        self.assertEqual((evidence["before"], evidence["after"], evidence["after_label"]),
                         (1, 0, "消耗1零时之种"))
        self.flow._tap.reset_mock()
        self.flow._hits = Mock(return_value=paid)
        with self.assertRaisesRegex(SkipTask, "次数已用尽"):
            self.flow.refresh_resonance({})
        self.flow._tap.assert_not_called()

    def test_unknown_or_inconsistent_refresh_result_stops_after_one_submission(self):
        self.flow._shop = Mock()
        paid = [hit("消耗1零时之种", 1065, 158)]
        dialog = [hit("你确定要进行免费刷新吗？", 528, 382), hit("确定", 615, 540)]
        for before, after in [(2, [paid] * 4), (1, [paid, [], paid, []]),
                              (1, [[hit("15小时49分后刷新", 1066, 197)]] * 4)]:
            with self.subTest(before=before, after=after):
                self.flow._tap.reset_mock()
                self.flow._hits = Mock(side_effect=[[hit(f"剩余次数{before}次", 1090, 164)], dialog, *after])
                with self.assertRaisesRegex(RuntimeError, "无法确认刷新成功"):
                    self.flow.refresh_resonance({})
                self.assertEqual(self.flow._tap.call_count, 2)

    def test_refresh_dialog_excludes_background_shop_labels(self):
        self.flow._shop = Mock()
        frame_hits = [hit("提示", 752, 291), hit("你确定要进行免费刷新吗？", 528, 382),
                      hit("尼禄", 1102, 490), hit("确定", 615, 540), hit("取消", 883, 541),
                      hit("x6000", 540, 617)]
        counts = iter((3, 2))

        def read_box(box):
            if box == (1010, 145, 1410, 235):
                return [hit(f"剩余次数 {next(counts)}次", 1100, 170)]
            left, top, right, bottom = box
            return [item for item in frame_hits if left <= item.x < right and top <= item.y < bottom]

        self.flow._hits = Mock(side_effect=read_box)
        self.flow.refresh_resonance({})
        self.assertEqual(self.flow._tap.call_args_list, [call(1360, 190), call(635, 550, 1.0)])
