import io
import unittest
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
        self.assertEqual(self.flow._tap.call_count, 2)

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
        self.flow._screen = Mock(return_value=b"frame")
        self.flow._scroll_expeditions = Mock(return_value=False)
        self.flow._read_region = Mock(side_effect=[[], [], [hit("点击选择使魔", 1250, 380)]])
        self.flow._hits = Mock(return_value=[hit("已接取的探险数2/10", 308, 176)])
        self.flow._text = Mock(return_value=hit("成功率", 1250, 175))
        with self.assertRaisesRegex(RuntimeError, "未确认探险数增加"):
            self.flow.expeditions({})
        self.assertEqual([c.args[0] for c in self.flow._click.call_args_list], ["一键派遣", "出发"])

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
