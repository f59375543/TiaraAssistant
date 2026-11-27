import io
import unittest
from pathlib import Path
from unittest.mock import Mock, call

from PIL import Image, ImageEnhance

from hka.game_flows import GameFlows
from hka.ocr import TextHit
from hka.runner import NavigationError


def hit(text, x, y):
    return TextHit(text, .99, x, y, 54, 30)


def screen(*parts, brightness=1):
    image = Image.new("RGB", (1600, 900), "black")
    for name, xy in parts:
        with Image.open(Path(__file__).resolve().parents[1] / "hka/assets" / name) as crop:
            image.paste(crop, xy)
    out = io.BytesIO()
    ImageEnhance.Brightness(image).enhance(brightness).save(out, format="PNG")
    return out.getvalue()


class HotSpringNavigationTests(unittest.TestCase):
    event = hit("揭秘狐妖汤", 1347, 173)
    guide = [event, hit("下一条", 1404, 730), hit("1/6", 1429, 667)]
    game_map = [event, hit("首页", 788, 82), hit("战斗", 932, 81)]
    picker = [hit("活动", 70, 250), hit("挑战", 70, 330)]
    back = ("tutorial_back.png", (43, 62))
    castle = ("battle_reset_compact.png", (1315, 177))
    battle = ("battle_active.png", (920, 26))

    def flow(self, frames, text):
        flow = GameFlows(Mock())
        flow._home = Mock()
        flow._tap = Mock()
        flow._screen = Mock(side_effect=frames)
        flow._hits = Mock(side_effect=text)
        flow._read_region = Mock(return_value=[])
        return flow

    def test_guide_closes_then_verified_battle_tab_returns_to_picker(self):
        flow = self.flow([screen(self.back), screen(self.battle, self.castle), b"picker"],
                         [self.guide, self.game_map, self.game_map, self.picker])
        flow._battle_picker()
        self.assertEqual(flow._tap.call_args_list, [call(955, 70, 1.5), call(77, 83, 1),
                                                    call(959, 96, 1.5)])

    def test_guide_needs_pagination_event_and_arrow(self):
        for labels, frame in ((self.guide[1:], screen(self.back)), (self.guide[:-1], screen(self.back)),
                              (self.guide, screen())):
            with self.subTest(labels=labels):
                flow = self.flow([frame], [labels])
                with self.assertRaises(NavigationError):
                    flow._battle_picker()
                flow._tap.assert_called_once_with(955, 70, 1.5)

    def test_stuck_guide_does_not_tap_back_twice(self):
        flow = self.flow([screen(self.back)], [self.guide] * 6)
        with self.assertRaises(NavigationError):
            flow._battle_picker()
        self.assertEqual(flow._tap.call_args_list, [call(955, 70, 1.5), call(77, 83, 1)])

    def test_castle_visible_beneath_modal_is_never_tapped(self):
        dark = screen(self.castle, brightness=.22)
        flow = self.flow([dark] * 12, [[]] * 12)
        with self.assertRaises(NavigationError):
            flow._battle_picker()
        flow._tap.assert_called_once_with(955, 70, 1.5)

    def test_ordinary_lit_castle_without_guide_remains_usable(self):
        flow = self.flow([screen(self.castle), b"picker"], [self.game_map, self.picker])
        flow._battle_picker()
        self.assertEqual(flow._tap.call_count, 2)
        self.assertAlmostEqual(flow._tap.call_args.args[0], 1344, delta=4)

    def test_castle_opening_guide_is_not_tapped_again(self):
        game_map = screen(self.battle, self.castle)
        flow = self.flow([game_map, screen(self.back)] + [game_map] * 10,
                         [self.game_map, self.guide] + [self.game_map] * 11)
        with self.assertRaises(NavigationError):
            flow._battle_picker()
        self.assertEqual([c.args[0] for c in flow._tap.call_args_list], [955, 1344, 77, 959])
