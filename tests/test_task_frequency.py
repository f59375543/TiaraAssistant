import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from hka.cancellation import Cancelled
from hka.config import ConfigError, load_config, normalize_config, save_config, task_presets
from hka.defaults import DEFAULT_CONFIG
from hka.runner import TaskRunner
from hka.vision import VisionError


class TaskFrequencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.json"
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.cfg["runtime"]["daily_reset_at"] = "04:00"
        self.cfg["notifications"] = {"on_success": False, "on_error": False}
        self.cfg["tasks"] = [
            {"id": "daily", "name": "日", "enabled": True, "steps": [], "frequency": "once_per_day"},
            {"id": "repeatable", "name": "常", "enabled": True, "steps": [], "frequency": "every_run"},
        ]
        self.device = Mock(serial="test")
        self.device.size.return_value = (1920, 1080)
        self.runner = TaskRunner(self.cfg, self.path, device=self.device, log=lambda *_: None)

    def run_at(self, timestamp):
        with patch("hka.runner.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(timestamp)
            result = self.runner.run()
        return [item["id"] for item in result["tasks"]]

    def test_same_day_boundary_and_reset(self):
        self.assertEqual(self.run_at("2026-11-24T06:00"), ["daily", "repeatable"])
        self.assertEqual(self.run_at("2026-11-25T03:59"), ["repeatable"])
        self.assertEqual(self.run_at("2026-11-25T04:00"), ["daily", "repeatable"])
        self.assertEqual(self.runner.reset_today(), 2)
        self.assertEqual(self.run_at("2026-11-25T04:01"), ["daily", "repeatable"])
        self.assertEqual(self.run_at("2026-11-25T03:58"), ["repeatable"])

    def test_calendar_enabled_and_frequency_are_independent(self):
        for t in self.cfg["tasks"]:
            t["schedule"] = {"type": "dates", "dates": ["11-25"]}
        self.assertEqual(self.run_at("2026-11-24T12:00"), [])
        self.cfg["tasks"][0]["enabled"] = False
        self.assertEqual(self.run_at("2026-11-25T12:00"), ["repeatable"])
        self.assertEqual(self.run_at("2026-11-25T12:01"), ["repeatable"])

    def test_config_migration_and_reopen(self):
        ids = ["expeditions", "stamina_farm", "presence", "stamina_farm_after_presence"]
        self.cfg["tasks"] = [{"id": i, "name": i, "enabled": True, "schedule": {"type":"daily"},
                              "steps": []} for i in ids]
        self.cfg["tasks"][3]["steps"] = [{"type":"game_flow","flow":"stamina_farm",
                                         "options":{"max_runs":1, "use_double_ticket": True}}]
        normalize_config(self.cfg)
        self.assertEqual([t["frequency"] for t in self.cfg["tasks"]],
                         ["every_run", "once_per_day", "every_run", "every_run"])
        self.assertEqual(self.cfg["tasks"][3]["steps"][0]["options"]["max_runs"], 0)
        self.cfg["tasks"][0]["frequency"] = "once_per_day"
        self.cfg["tasks"][0]["enabled"] = False
        save_config(self.path, self.cfg)
        self.assertEqual(load_config(self.path)["tasks"], self.cfg["tasks"])
        self.cfg["tasks"][0]["frequency"] = "invalid"
        with self.assertRaises(ConfigError):
            save_config(self.path, self.cfg)

    def test_stock_launch_failure_stops_before_once_per_day_task(self):
        launch = next(task for task in task_presets() if task["id"] == "launch")
        self.assertTrue(launch.pop("required"))
        self.cfg["tasks"].insert(0, launch)
        normalize_config(self.cfg)
        self.device.screen.return_value = b""
        with patch.object(self.runner, "_steps", side_effect=RuntimeError("登录失败")) as steps:
            result = self.runner.run()
        steps.assert_called_once_with(launch["steps"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual([item["status"] for item in result["not_run"]], ["stopped_after_error"] * 2)
        self.assertEqual(self.runner.preview()[1]["status"], "pending")

    def test_stock_launch_migration_respects_explicit_and_custom_flows(self):
        launch = next(task for task in task_presets() if task["id"] == "launch")
        self.cfg["tasks"] = [launch]
        launch["required"] = False
        normalize_config(self.cfg)
        self.assertFalse(launch["required"])
        del launch["required"]
        launch["steps"] = [{"type": "sleep", "seconds": 1}]
        normalize_config(self.cfg)
        self.assertNotIn("required", launch)

    def test_task_ids_cannot_collide_on_windows(self):
        self.cfg["tasks"][1]["id"] = "DAILY"
        with self.assertRaisesRegex(ConfigError, "daily / DAILY"):
            save_config(self.path, self.cfg)
        self.assertFalse(self.path.exists())

    def test_force_bypasses_calendar_and_logs_daily_skip_reason(self):
        self.cfg["tasks"][0]["schedule"] = {"type": "dates", "dates": ["2000-01-01"]}
        self.runner.log = Mock()
        self.assertEqual(self.runner.run(task_ids={"daily"})["status"], "nothing_to_do")
        self.assertEqual(self.runner.run(task_ids={"daily"}, force=True)["status"], "success")
        result = self.runner.run(task_ids={"daily"}, force=True)
        self.assertEqual(result["status"], "nothing_to_do")
        item = next(item for item in result["not_run"] if item["id"] == "daily")
        self.assertEqual(item["status"], "completed")
        self.runner.log.assert_any_call(f"{item['name']}: {item['reason']}")

    def test_attempt_after_cancel_and_active_run_lock(self):
        t = self.cfg["tasks"][0]
        with self.runner._exclusive_run():
            with self.assertRaises(RuntimeError):
                self.runner.reset_today()
            with self.assertRaises(RuntimeError):
                self.runner.run()
        with patch.object(self.runner, "_steps", side_effect=Cancelled()):
            self.assertEqual(self.runner.run()["status"], "cancelled")
        self.assertEqual(self.runner.preview()[0]["status"], "attempted")
        self.assertFalse(self.runner.state_path.exists())
        self.assertEqual(self.runner.reset_today(), 1)
        self.assertEqual(self.runner.run()["tasks"][0]["id"], t["id"])

    def test_refuse_unreadable_state(self):
        self.runner.state_path.write_text("{}", encoding="utf-8")
        for value in ("not-json", json.dumps(["wrong"]), '{"today": "daily"}'):
            self.runner.state_path.write_text(value, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.runner.run()
            self.device.connect.assert_not_called()

    def test_optional_template_and_one_plan_per_run(self):
        self.cfg["tasks"][0]["steps"] = [{"type": "tap_image", "image": "missing.png", "on_missing": "continue"}]
        with patch.object(self.runner, "preview", wraps=self.runner.preview) as preview:
            self.assertEqual(self.runner.run()["status"], "success")
            self.assertEqual(preview.call_count, 1)
        self.device.screen.assert_not_called()
        with self.assertRaises(VisionError):
            self.runner._wait_image({"image": "required.png"})
        self.device.screen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
