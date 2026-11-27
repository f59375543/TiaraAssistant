import time
import unittest
from unittest.mock import Mock

from hka.expedition_cards import parse_cards
from hka.game_flows import GameFlows
from hka.ocr import TextHit


def hit(text, x, y, width):
    return TextHit(text, .99, x, y, width, 40)


def header(count="3/10"):
    return [hit("已接取的探险数" + count, 308, 176, 302)]


def selectable(name, y, duration="08:00:00"):
    return [hit(name, 440, y, 178), hit("需要时间：" + duration, 1112, y + 5, 310),
            hit("点击选择使魔", 1208, y + 113, 208)]


def running(name, y, remaining):
    return [hit(name, 440, y, 178), hit("剩余时间：" + remaining, 729, y + 112, 312)]


target_name = "终焉的坚持"
next_name = "捉拿啾啾"
# Visible rows in the archived before/after screens. Only the target's title
# fits at the bottom after departure, and the visible timer belongs to another row.
before_rows = header() + selectable(target_name, 280) + selectable(next_name, 546, "06:00:00")
after_rows = (header() + selectable(next_name, 280, "06:00:00")
              + running("水晶的微光", 548, "09:05:02") + [hit(target_name, 440, 812, 178)])


class ExpeditionVerificationTests(unittest.TestCase):
    def test_other_card_still_selectable_target_or_changed_limit_cannot_confirm(self):
        target = parse_cards(before_rows)[0]
        for rows in (after_rows, before_rows, header("3/12") + running(target_name, 280, "07:59:59"),
                     header() + running(target_name, 280, "02:58:30")):
            with self.subTest(rows=rows):
                flow = GameFlows(Mock())
                flow._rewind_expeditions = Mock()
                flow._expedition_page = Mock(return_value=(b"list", rows))
                flow._scroll_expeditions = Mock(return_value=False)
                self.assertFalse(flow._find_dispatched_expedition(target, (3, 10), {}, time.monotonic()))

    def test_match_below_viewport_requires_scroll_to_its_own_timer(self):
        target = parse_cards(before_rows)[0]
        flow = GameFlows(Mock())
        flow._rewind_expeditions = Mock()
        revealed = header() + running(target_name, 350, "07:59:59")
        flow._expedition_page = Mock(side_effect=[(b"top", after_rows), (b"lower", revealed)])
        flow._scroll_expeditions = Mock(return_value=True)
        evidence = {}
        self.assertTrue(flow._find_dispatched_expedition(target, (3, 10), evidence, time.monotonic()))
        self.assertEqual(evidence["verification"], "target_timer")
        self.assertEqual(evidence["remaining_seconds"], 28799)
        flow._scroll_expeditions.assert_called_once_with(b"top")

    def test_dispatch_resumes_at_top_only_after_target_is_verified(self):
        for verified in (True, False):
            with self.subTest(verified=verified):
                flow = GameFlows(Mock())
                flow.runner._task_details = {}
                flow.runner.cancel.is_set.return_value = False
                flow._tasks = Mock()
                lower = False
                submitted = []
                selected = None

                def rows():
                    if not submitted:
                        return before_rows
                    if len(submitted) == 2:
                        return header("4/10")
                    if not lower:
                        return after_rows
                    return header() + running(target_name if verified else "水晶的微光", 350, "07:59:59")

                def scroll(frame, *, towards_top=False):
                    nonlocal lower
                    if towards_top:
                        moved, lower = lower, False
                        return moved
                    if submitted and not lower:
                        lower = True
                        return True
                    return False

                def read_region(frame, box, score):
                    x1, y1, x2, y2 = box
                    return [h for h in rows() if x1 <= h.x < x2 and y1 <= h.y < y2]

                def select(x, y, *args):
                    nonlocal selected
                    cards = [c for c in parse_cards(rows()) if c.select_button
                             and abs(c.select_button.y + c.select_button.height // 2 - y) < 5]
                    self.assertEqual(len(cards), 1)
                    selected = cards[0].name

                def click(label, *args):
                    if label == "出发":
                        self.assertNotIn(selected, submitted)
                        submitted.append(selected)
                    return True

                flow._screen = Mock(return_value=b"list")
                flow._hits = Mock(side_effect=lambda *a, **kw: rows())
                flow._read_region = read_region
                flow._scroll_expeditions = scroll
                flow._tap = select
                flow._click = click
                flow._text = Mock(return_value=hit("成功率100%", 1224, 156, 180))
                if verified:
                    self.assertEqual(flow.expeditions({}), {"claimed": 0, "dispatched": 2})
                    self.assertEqual(submitted, [target_name, next_name])
                else:
                    with self.assertRaises(RuntimeError):
                        flow.expeditions({})
                    self.assertEqual(submitted, [target_name])
                    self.assertEqual(flow.runner._task_details["expeditions"]["dispatched"], 0)
