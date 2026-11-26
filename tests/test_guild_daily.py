"""Offline checks for the guild contribution reward using simulated OCR frames."""
import unittest
from itertools import chain, repeat
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from hka.game_flows import GameFlows
from hka.ocr import TextHit
from hka.runner import SkipTask


def hit(text, x, y):
    return TextHit(text, .99, x, y, 40, 20)


def guild_page(status):
    return [hit("社团名", 650, 300), hit("成员数", 830, 400), hit(status, 1200, 540)]


REWARD = [hit("昨日社团贡献奖励", 700, 190), hit("领取", 680, 650)]
SUCCESS = [hit("社团宣言", 620, 260), hit("奖励领取成功", 700, 380), hit("确定", 760, 550)]
OPEN, CLAIM, CONFIRM = (1220, 550), (700, 660), (780, 560)


def simulate(*frames):
    flow = GameFlows(SimpleNamespace(device=Mock(), log=Mock(), sleep=Mock(), cancel=Event()))
    flow._home = Mock()
    flow._hits = Mock(side_effect=chain(frames, repeat(frames[-1])))
    flow._tap = Mock()
    return flow


class GuildDailyTests(unittest.TestCase):
    def assert_taps(self, flow, *expected):
        self.assertEqual([call.args[:2] for call in flow._tap.call_args_list], list(expected))

    def test_claimed_status_after_submit(self):
        for status in ("已领", "已领取"):
            with self.subTest(status=status):
                flow = simulate([], guild_page("可领"), REWARD, REWARD, guild_page(status))
                self.assertTrue(flow.claim_guild_daily({})["claimed"])
                self.assert_taps(flow, OPEN, CLAIM)

    def test_compact_reward_dialog_from_archived_capture(self):
        dialog = guild_page("可领") + [
            TextHit("昨日社团贡献奖励", .998, 661, 257, 342, 50),
            TextHit("领取", .999, 653, 576, 80, 45),
            TextHit("返回", .999, 933, 574, 81, 48),
        ]
        flow = simulate([], guild_page("可领"), dialog, guild_page("已领"))
        result = flow.claim_guild_daily({})
        self.assertIn("已领", result["verification"])
        self.assert_taps(flow, OPEN, (693, 598))

    def test_dialog_confirms_claim_with_generic_final_status(self):
        for status in ("不可领", "明日再来"):
            with self.subTest(status=status):
                flow = simulate([], guild_page("可领"), REWARD, SUCCESS, SUCCESS, guild_page(status))
                self.assertTrue(flow.claim_guild_daily({})["claimed"])
                self.assert_taps(flow, OPEN, CLAIM, CONFIRM)

    def test_generic_status_alone_does_not_confirm_claim(self):
        for status in ("不可领", "明日再来"):
            with self.subTest(status=status):
                flow = simulate([], guild_page("可领"), REWARD, guild_page(status))
                with self.assertRaises(RuntimeError):
                    flow.claim_guild_daily({})
                self.assert_taps(flow, OPEN, CLAIM)

    def test_waits_for_delayed_claimed_status(self):
        frames = [[], guild_page("可领"), REWARD, guild_page("不可领"), guild_page("已领取")]
        flow = simulate(*frames)
        self.assertTrue(flow.claim_guild_daily({})["claimed"])
        self.assertGreaterEqual(flow._hits.call_count, len(frames))
        self.assert_taps(flow, OPEN, CLAIM)

    def test_initial_terminal_status_skips_without_clicking(self):
        for status in ("已领", "已领取", "不可领", "明日再来"):
            with self.subTest(status=status):
                flow = simulate([], guild_page(status))
                with self.assertRaises(SkipTask):
                    flow.claim_guild_daily({})
                self.assert_taps(flow)

    def test_resumes_success_dialog_without_resubmitting(self):
        flow = simulate(SUCCESS, guild_page("不可领"))
        self.assertTrue(flow.claim_guild_daily({})["claimed"])
        self.assert_taps(flow, CONFIRM)

    def test_resumes_open_reward_dialog(self):
        flow = simulate(REWARD, guild_page("已领取"))
        self.assertTrue(flow.claim_guild_daily({})["claimed"])
        self.assert_taps(flow, CLAIM)
