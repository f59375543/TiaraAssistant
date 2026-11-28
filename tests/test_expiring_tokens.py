import unittest
from unittest.mock import Mock, call

from hka.game_flows import GameFlows
from hka.ocr import TextHit
from hka.runner import NavigationError


def hit(text, x, y, width=60, height=40):
    return TextHit(text, .99, x, y, width, height)


class ExpiringTokenNoticeTests(unittest.TestCase):
    # Normalized OCR coordinates from the 2026-11-28 resonance refresh failure.
    title = hit("以下活动代币即将销毁，请尽快使用", 548, 236, 500, 39)
    confirm = hit("确认", 753, 600, 92, 56)
    background = [hit("首页", 789, 85, 58, 34),
                  hit("前往", 1080, 550, 80, 44)]
    dialog = [title, *background, confirm]

    def setUp(self):
        self.flow = GameFlows(Mock())
        self.flow._tap = Mock()
        self.flow._hits = Mock(return_value=self.background)

    def test_clicks_unique_confirmation_and_verifies_dialog_closed(self):
        self.assertTrue(self.flow._dismiss_navigation_notice(self.dialog))
        self.flow._tap.assert_called_once_with(799, 628, 1.0)
        self.flow._hits.assert_called_once_with()

    def test_unrelated_confirmation_dialog_is_not_clicked(self):
        unrelated = [hit("是否购买零时馈礼", 650, 300),
                     hit("确认", 753, 600, 92, 56)]
        self.assertFalse(self.flow._dismiss_navigation_notice(unrelated))
        self.flow._tap.assert_not_called()
        self.flow._hits.assert_not_called()

    def test_ambiguous_or_cancelled_notice_stops_without_input(self):
        cancel = hit("取消", 900, 600, 92, 56)
        cases = (
            [self.title, *self.background],
            [self.title, *self.background, self.confirm,
             hit("确认", 910, 600, 92, 56)],
            [self.title, *self.background, self.confirm, cancel],
        )
        for dialog in cases:
            with self.subTest(dialog=dialog), self.assertRaisesRegex(
                    NavigationError, "确认按钮未唯一识别"):
                self.flow._dismiss_navigation_notice(dialog)
        self.flow._tap.assert_not_called()

    def test_keeps_waiting_but_never_reclicks_if_notice_remains(self):
        self.flow._hits = Mock(return_value=self.dialog)
        with self.assertRaisesRegex(NavigationError, "销毁提示仍未关闭"):
            self.flow._dismiss_navigation_notice(self.dialog)
        self.assertEqual(self.flow._tap.call_args_list, [call(799, 628, 1.0)])
        self.assertEqual(self.flow._hits.call_count, 4)


if __name__ == "__main__":
    unittest.main()
