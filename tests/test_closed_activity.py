import unittest
from unittest.mock import Mock, call, patch

from hka.game_flows import GameFlows
from hka.ocr import TextHit
from hka.runner import NavigationError, SkipTask


def hit(text, x, y, width=60, height=40):
    return TextHit(text, .99, x, y, width, height)


class ClosedActivityTests(unittest.TestCase):
    # Normalized OCR coordinates from the November 27 failed run.
    picker = [hit("主线", 88, 213), hit("挑战", 87, 407), hit("首页", 788, 84)]
    title = hit("不在活动时间内", 656, 290, 284, 51)
    body = hit("不在活动时间内哦~开放时间详见公", 535, 370, 507, 39)
    confirm = hit("确定", 751, 529, 98, 57)
    dialog = picker + [title, body, hit("告。", 532, 407), confirm]
    dismiss = call(800, 557, .8)
    rift = hit("多元裂缝", 1200, 440)
    enter_rift = call(1230, 460, 1.0)

    def setUp(self):
        self.flow = GameFlows(Mock())
        self.flow._screen = Mock(return_value=b"offline frame")
        self.flow._tap = Mock()

    def test_ambiguous_or_unrelated_dialog_never_confirms(self):
        for items in ([self.title, self.confirm], [self.title, self.body],
                      self.dialog + [hit("取消", 900, 529)]):
            with self.subTest(items=items), self.assertRaises(NavigationError):
                self.flow._dismiss_closed_activity(items)
        self.assertFalse(self.flow._dismiss_closed_activity([self.body, self.confirm]))
        self.flow._tap.assert_not_called()

    def test_dialog_must_clear_and_picker_must_return_after_one_tap(self):
        for subsequent in (self.dialog, [], [hit("首页", 788, 84)]):
            with self.subTest(subsequent=subsequent):
                self.flow._tap.reset_mock()
                self.flow._hits = Mock(return_value=subsequent)
                with self.assertRaises(NavigationError):
                    self.flow._dismiss_closed_activity(self.dialog)
                self.assertEqual(self.flow._tap.call_args_list, [self.dismiss])

    def test_incidental_closed_activity_does_not_skip_named_rift(self):
        self.flow._battle_picker = Mock()
        self.flow._click = Mock()
        self.flow._swipe = Mock()
        stages = [hit("使魔的爱", 760, 350), hit("虚轴之庭", 1100, 530)]
        self.flow._hits = Mock(side_effect=[self.dialog, self.picker, self.picker, stages])
        self.flow._read_region = Mock(side_effect=[[self.rift], stages])
        self.flow._rift()
        self.assertEqual(self.flow._tap.call_args_list, [self.dismiss, self.enter_rift])
        self.flow._swipe.assert_not_called()

    def test_named_rift_closed_is_skipped_after_verified_return(self):
        self.flow._battle_picker = Mock()
        self.flow._click = Mock()
        self.flow._hits = Mock(side_effect=[self.picker, self.dialog, self.dialog, self.picker])
        self.flow._read_region = Mock(return_value=[self.rift])
        with self.assertRaises(SkipTask):
            self.flow._rift()
        self.assertEqual(self.flow._tap.call_args_list, [self.enter_rift, self.dismiss])
        self.flow._read_region.assert_called_once()

    def test_home_closes_dialog_before_tapping_visible_background_tab(self):
        self.flow._hits = Mock(side_effect=[self.dialog, self.picker, self.picker, self.picker])
        self.flow._dismiss_navigation_card = Mock(return_value=False)
        self.flow._leave_maze_roster = Mock(return_value=False)
        with patch("hka.game_flows.ImageFrame"), patch("hka.game_flows.find_image", return_value=(815, 96, 1)):
            self.flow._home()
        self.assertEqual(self.flow._tap.call_args_list, [self.dismiss, call(818, 104, 2.5)])
