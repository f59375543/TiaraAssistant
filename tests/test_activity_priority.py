"""Offline tests for choosing a visible B-difficulty activity card."""
import unittest
from unittest.mock import Mock, patch

from hka.game_flows import GameFlows
from hka.ocr import TextHit


def hit(text, x, y):
    return TextHit(text, 0.98, x, y, 80, 20)


def card(x, y, timer="剩余时间 2天"):
    return [hit("难度指数 B", x, y), hit(timer, x + 20, y + 25)]


class ActivityPriorityTests(unittest.TestCase):
    def assert_selected(self, hits, target):
        flow = GameFlows(Mock())
        flow._battle_picker = Mock()
        flow._click = Mock()
        flow._hits = Mock(side_effect=[hits, []])
        flow._tap = Mock()
        flow._swipe = Mock()
        flow._screen = Mock()
        flow._text = Mock(return_value=None)
        with patch("hka.game_flows.stage_labels", return_value=[hit("BONUS", 1000, 450)]):
            flow._activity()
        flow._tap.assert_called_once_with(max(300, target.x - 190), target.y + 115, 1.0)
        flow._swipe.assert_not_called()

    def test_lower_left_beats_higher_right_in_either_ocr_order(self):
        left = card(650, 560)
        right = card(1100, 270)
        for hits in (right + left, list(reversed(right + left))):
            with self.subTest(hits=hits):
                self.assert_selected(hits, left[0])

    def test_right_column_uses_uppermost_eligible_card(self):
        upper = card(1180, 385)
        lower = card(1080, 610)
        expired = card(1150, 215, "剩余时间 0天0小时")
        for hits in (lower + expired + upper, list(reversed(lower + expired + upper))):
            with self.subTest(hits=hits):
                self.assert_selected(hits, upper[0])

    def test_expired_or_excluded_left_activity_falls_back_to_right(self):
        right = card(1190, 490)
        for left in (card(640, 290, "剩余时间 0分钟"),
                     card(640, 290) + [hit("空想迷域", 650, 345)]):
            with self.subTest(left=left):
                self.assert_selected(left + right, right[0])

    def test_right_side_timer_does_not_qualify_left_difficulty_tag(self):
        left_tag = hit("难度指数 B", 760, 285)
        adjacent_timer = hit("剩余时间 3小时", 850, 295)
        right = card(1220, 565)
        self.assert_selected([left_tag, adjacent_timer] + right, right[0])


if __name__ == "__main__":
    unittest.main()
