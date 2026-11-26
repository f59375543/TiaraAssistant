import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from hka.cancellation import Cancelled
from hka.cli import main
from hka.defaults import DEFAULT_CONFIG
from hka.runner import RunBusyError, TaskRunner, load_latest_report


class RunReportingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["tasks"] = [
            {"id": name, "name": name, "enabled": True, "frequency": "once_per_day", "steps": []}
            for name in ("first", "second", "third")
        ]
        device = Mock(serial="test")
        device.size.return_value = (100, 100)
        self.runner = TaskRunner(self.config, self.path, device=device, log=Mock())

    def test_interrupted_tasks_are_accounted_for(self):
        with patch.object(self.runner, "_steps", side_effect=[None, Cancelled()]):
            result = self.runner.run()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual([(i["id"], i["status"]) for i in result["tasks"]],
                         [("first", "success"), ("second", "cancelled")])
        self.assertEqual([(i["id"], i["status"]) for i in result["not_run"]], [("third", "not_started")])
        self.assertEqual([i["status"] for i in self.runner.preview()], ["completed", "attempted", "pending"])
        self.assertEqual(load_latest_report(self.runner.run_dir), result)

    def test_stop_before_connection_marks_all_tasks_unstarted(self):
        self.runner.cancel.set()
        result = self.runner.run()
        self.assertEqual(result["tasks"], [])
        self.assertEqual([i["status"] for i in result["not_run"]], ["not_started"] * 3)
        self.runner.device.connect.assert_not_called()

    def test_notification_sees_durable_report_and_no_execution_lock(self):
        def deliver(*_args, **_kwargs):
            self.assertEqual(load_latest_report(self.runner.run_dir)["status"], "success")
            with self.runner.exclusive_run():
                return ["timeout"]

        with patch("hka.runner.send_notifications", side_effect=deliver):
            result = self.runner.run()
        self.assertEqual(result["notification_errors"], ["timeout"])
        self.assertEqual(load_latest_report(self.runner.run_dir), result)
        self.assertEqual(len(list(self.runner.run_dir.glob("run_*.json"))), 1)
        self.assertEqual(self.runner.run()["status"], "nothing_to_do")
        self.assertEqual(load_latest_report(self.runner.run_dir), result)

    def test_stop_during_delivery_skips_remaining_destinations(self):
        self.config["notifications"] = {
            "webhooks": [{"url": "https://first.invalid"}, {"url": "https://second.invalid"}],
            "email": {"enabled": True},
        }
        def stop_during_request(*_args, **_kwargs):
            self.runner.cancel.set()
            raise TimeoutError("timeout")

        with (patch("hka.notify.urllib.request.urlopen", side_effect=stop_during_request) as request,
              patch("hka.notify.send_email") as email):
            result = self.runner.run()
        request.assert_called_once()
        email.assert_not_called()
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["notification_errors"]), 2)
        self.assertEqual(load_latest_report(self.runner.run_dir), result)

    def test_read_previous_legacy_report_among_invalid_newer_files(self):
        folder = self.runner.run_dir
        self.assertIsNone(load_latest_report(folder))
        folder.mkdir()
        old = {"finished_at": "2026-01-01T20:02:11", "status": "failed", "tasks": []}
        (folder / "run_20260101_200211_000123.json").write_text(json.dumps(old))
        (folder / "run_20260102.json").write_text("{}")
        (folder / "run_20260103.json").write_text("not json")
        self.assertEqual(load_latest_report(folder), old)

        for fields in ({"tasks": [None]}, {"tasks": [{"id": "first", "status": []}]},
                       {"not_run": "invalid"}, {"not_run": [None]}):
            with self.subTest(fields=fields):
                (folder / "run_20260104.json").write_text(json.dumps({**old, **fields}))
                self.assertEqual(load_latest_report(folder), old)

    def test_daemon_continues_after_busy_run(self):
        self.path.write_text(json.dumps(self.config))
        starts = [datetime(2026, 1, day, 7, 29) for day in (5, 6)]
        due = [stamp.replace(minute=30) for stamp in starts]
        with patch("hka.cli.datetime") as clock, patch("hka.cli.TaskRunner") as runner, redirect_stdout(io.StringIO()):
            clock.now.side_effect = [starts[0], due[0], starts[1], due[1]]
            runner.return_value.run.side_effect = [RunBusyError("busy"), KeyboardInterrupt()]
            self.assertEqual(main(["--config", str(self.path), "daemon", "--at", "07:30"]), 0)
        self.assertEqual(runner.return_value.run.call_count, 2)
