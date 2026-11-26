from unittest import TestCase
from unittest.mock import Mock, call, patch

from hka.game_flows import GameFlows
from hka.ocr import TextHit


def hit(text, x, y):
    return TextHit(text, .99, x, y, 40, 20)


class MazeNavigationTests(TestCase):
    roster = [hit("空想迷域", 140, 8), hit("赋格等级：99/99", 535, 278),
              hit("赋格等级：99/99", 1243, 278)]
    picker = [hit("活动", 70, 250), hit("挑战", 70, 330)]
    home = [hit("首页", 795, 86), hit("战斗", 940, 86)]

    def flow(self, frames):
        flow = GameFlows(Mock())
        flow._screen = Mock(return_value=b"screen")
        flow._hits = Mock(side_effect=frames)
        flow._read_region = Mock(return_value=[])
        flow._tap = Mock()
        return flow

    def images(self, frame, assets, step):
        return {"maze_back.png": (82, 38, .99), "home_active.png": (820, 80, 1)}.get(step["image"])

    def test_picker_rechecks_after_leaving_roster(self):
        flow = self.flow([self.roster, self.picker])
        flow._home = Mock()
        with patch("hka.game_flows.find_image", side_effect=self.images):
            flow._battle_picker()
        self.assertEqual(flow._tap.call_args_list, [call(955, 70, 1.5), call(82, 38, 1.5)])

    def test_home_rechecks_home_tab_after_leaving_roster(self):
        flow = self.flow([self.roster, self.home, self.home])
        with patch("hka.game_flows.find_image", side_effect=self.images):
            flow._home()
        self.assertEqual(flow._tap.call_args_list, [call(82, 38, 1.5), call(815, 96, 2.5)])

    def test_back_requires_title_roster_and_matching_button(self):
        flow = self.flow([])
        with patch("hka.game_flows.find_image", side_effect=self.images):
            for hits in (self.roster[:1], self.roster[1:], [hit("空想迷域", 550, 300), *self.roster[1:]]):
                self.assertFalse(flow._leave_maze_roster(hits, Mock()))
        with patch("hka.game_flows.find_image", return_value=None):
            self.assertFalse(flow._leave_maze_roster(self.roster, Mock()))
        flow._tap.assert_not_called()
