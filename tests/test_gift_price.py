import unittest
from unittest.mock import Mock, call

from hka.game_flows import GameFlows
from hka.ocr import TextHit
from hka.runner import SkipTask


def hit(text, x, y, width=60, height=32):
    return TextHit(text, .99, x, y, width, height)


class GiftCancellationTests(unittest.TestCase):
    card = [hit("零时馈礼", 630, 300), hit("×0", 660, 620), hit("购买", 635, 680)]
    # Normalized OCR from the 2026-11-26 failure screenshot.
    dialog = [hit("零时馈礼", 710, 258), hit("获得礼品", 729, 334),
              hit("是否购买零时馈礼", 672, 492), hit("购买", 620, 554),
              hit("取消", 914, 554, 64, 37)]

    def flow(self, frames):
        flow = GameFlows(Mock())
        flow._shop = Mock()
        flow._click = Mock()
        flow._hits = Mock(side_effect=[self.card, *frames])
        flow._tap = Mock()
        return flow

    def test_unpriced_dialog_is_cancelled_and_verified(self):
        listing = [hit("吼美礼包", 30, 350), *self.card]
        flow = self.flow([self.dialog, self.dialog, listing])
        flow.runner.last_decision_frame = b"unpriced-dialog"
        def tap(x, y, wait=None):
            if (x, y) == (946, 572):
                flow.runner.last_decision_frame = b"listing"
        flow._tap.side_effect = tap
        with self.assertRaisesRegex(SkipTask, "已取消购买") as result:
            flow.free_gift({})
        self.assertEqual(result.exception.decision_frame, b"unpriced-dialog")
        self.assertEqual(flow._tap.call_args_list, [call(665, 696), call(946, 572, 0.9)])

    def test_cancel_does_not_count_as_verified_if_dialog_stays_open(self):
        flow = self.flow([self.dialog] * 4)
        with self.assertRaisesRegex(RuntimeError, "取消后未确认"):
            flow.free_gift({})
        self.assertEqual(flow._tap.call_args_list, [call(665, 696), call(946, 572, 0.9)])

    def test_missing_cancel_button_leaves_purchase_untouched(self):
        flow = self.flow([self.dialog[:-1]])
        with self.assertRaisesRegex(RuntimeError, "最终价格"):
            flow.free_gift({})
        flow._tap.assert_called_once_with(665, 696)
