import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from hka.adb import AdbDevice, AdbError, AdbTransportError
from hka.cli import main
from hka.defaults import DEFAULT_CONFIG
from hka.runner import NavigationError, RunBusyError, SkipTask, TaskRunner


class RunnerRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.json"
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["runtime"]["daily_reset_at"] = "04:00"
        config["tasks"] = [
            {"id": name, "name": name, "enabled": True, "frequency": "once_per_day", "steps": []}
            for name in ("first", "later")
        ]
        self.path.write_text(json.dumps(config), encoding="utf-8")
        self.device = Mock(serial="device")
        self.device.size.return_value = (1920, 1080)
        self.runner = TaskRunner(config, self.path, device=self.device, log=Mock(), sleep=Mock())
        config["tasks"][1]["steps"] = [{"type": "key", "code": 4}]

    def test_failed_navigation_saves_original_and_preserves_pending(self):
        def fail(steps):
            self.runner.last_decision_frame = b"decision-frame"
            self.runner._task_details["progress"] = {"confirmed": 1}
            raise NavigationError("地图无法确认")
        with patch.object(self.runner, "_steps", side_effect=fail):
            report = self.runner.run()
        task, = report["tasks"]
        self.assertGreaterEqual(task["duration_seconds"], 0)
        self.assertLessEqual(datetime.fromisoformat(task["started_at"]), datetime.fromisoformat(task["finished_at"]))
        self.assertEqual(task["details"]["progress"]["confirmed"], 1)
        self.assertEqual(task["screenshot_source"], "decision")
        self.assertEqual((self.runner.run_dir / task["screenshot"]).read_bytes(), b"decision-frame")
        self.device.screen.assert_not_called()
        self.assertEqual(self.runner.preview()[1]["status"], "pending")
        self.assertNotEqual(self.runner._save_screenshot("first_error", b"x").name, task["screenshot"])

    def test_independent_custom_task_runs_after_navigation_failure(self):
        self.runner.config["tasks"].append({"id": "pause", "name": "间隔", "enabled": True, "steps": [{"type": "sleep", "seconds": 0}]})
        with patch.object(self.runner, "_steps", side_effect=[NavigationError("导航失败"), None]):
            result = self.runner.run()
        self.assertEqual([(row["id"], row["status"]) for row in result["tasks"]], [("first", "failed"), ("pause", "success")])
        self.assertEqual([row["id"] for row in result["not_run"]], ["later"])

    def test_only_skip_with_evidence_saves_its_decision_frame(self):
        skips = [SkipTask("价格无法核实", decision_frame=b"dialog"), SkipTask("没有可领取奖励")]
        with patch.object(self.runner, "_steps", side_effect=skips):
            report = self.runner.run()
        saved, ordinary = report["tasks"]
        self.assertEqual((saved["status"], saved["screenshot_source"]), ("skipped", "decision"))
        self.assertEqual((self.runner.run_dir / saved["screenshot"]).read_bytes(), b"dialog")
        self.assertNotIn("screenshot", ordinary)
        self.device.screen.assert_not_called()

    def test_independent_runners_contend_on_same_installation(self):
        other = TaskRunner(self.runner.config, self.path, device=self.device, log=Mock())
        with self.runner.exclusive_run(), self.assertRaises(RunBusyError):
            other.run()
        self.device.connect.assert_not_called()
        with other.exclusive_run():
            pass

    def test_invalid_selection_cannot_start_device(self):
        for ids in (set(), {"absent"}, {"first", "absent"}):
            with self.assertRaises(RuntimeError):
                self.runner.run(task_ids=ids)
        self.device.connect.assert_not_called()

    def test_partial_steps_count_survives_failure(self):
        steps = [{"type": "repeat", "times": 2, "steps": [{"type": "sleep", "seconds": 0}]},
                 {"type": "key", "code": 4}]
        self.device.key.side_effect = AdbError("input unavailable")
        with self.assertRaises(AdbError):
            self.runner._steps(steps)
        self.assertEqual(self.runner._task_details["steps"], {"completed": 1, "total": 2})

    def test_replay_edges_match_manual_tap_and_swipe(self):
        self.runner._steps([{"type": "tap", "x": 1, "y": 1},
                            {"type": "swipe", "x1": 0, "y1": 0, "x2": 1, "y2": 1}])
        self.device.tap.assert_called_once_with(1919, 1079)
        self.device.swipe.assert_called_once_with(0, 0, 1919, 1079, 400)

    def test_cross_day_after_input_guards_against_second_submission(self):
        before = datetime(2026, 11, 26, 3, 59, 58)
        after = datetime(2026, 11, 26, 4, 0, 1)
        with patch("hka.runner.datetime", wraps=datetime) as clock:
            clock.now.return_value = before
            def cross(steps):
                self.runner.check_action_day()
                clock.now.return_value = after
                self.runner.finish_action_day()
            with patch.object(self.runner, "_steps", side_effect=cross):
                report = self.runner.run()
            self.assertEqual(report["tasks"][0]["status"], "failed")
            self.assertTrue(self.runner._daily_attempt("first", after.date()).exists())
            self.assertFalse(self.runner._daily_attempt("later", after.date()).exists())

    def test_wait_after_old_day_input_does_not_use_new_day_allowance(self):
        before = datetime(2026, 11, 26, 3, 59, 58)
        after = datetime(2026, 11, 26, 4, 0, 1)
        with patch("hka.runner.datetime", wraps=datetime) as clock:
            clock.now.return_value = before
            def cross(steps):
                self.runner._send_input(self.device.key, 4)
                clock.now.return_value = after
                self.runner._send_input(self.device.key, 4)
            with patch.object(self.runner, "_steps", side_effect=cross):
                self.assertEqual(self.runner.run()["tasks"][0]["status"], "failed")
        self.device.key.assert_called_once_with(4)
        self.assertFalse(self.runner._daily_attempt("first", after.date()).exists())

    def test_prunes_only_old_attempt_markers(self):
        old = self.runner.today - timedelta(days=46)
        marker = self.runner._daily_attempt("first", old)
        marker.parent.mkdir(parents=True)
        marker.touch()
        saved = marker.parent / "notes"
        saved.write_text("keep")
        self.runner._prune_attempts()
        self.assertFalse(marker.exists())
        self.assertTrue(saved.is_file())

    def test_doctor_exit_code_reports_missing_game(self):
        with patch("hka.cli.AdbDevice") as adb, redirect_stdout(io.StringIO()):
            adb.return_value.installed.return_value = False
            self.assertEqual(main(["--config", str(self.path), "doctor"]), 1)

    def test_daemon_survives_temporarily_unreadable_config(self):
        a = datetime(2026, 11, 25, 6)
        b = a + timedelta(days=1)
        with patch("hka.cli.datetime") as clock, patch("hka.cli.load_config") as load, patch("hka.cli.TaskRunner") as runner:
            clock.now.side_effect = [a, a.replace(hour=7), b, b.replace(hour=7)]
            load.side_effect = [OSError("temporarily unavailable"), {}]
            runner.return_value.run.side_effect = KeyboardInterrupt()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--config", str(self.path), "daemon", "--at", "07:00"]), 0)
        runner.return_value.run.assert_called_once()


class DeviceRecoveryTests(unittest.TestCase):
    def device(self, **settings):
        return AdbDevice({"adb_path": sys.executable, "serial": "emulator-5554", "package": "test", **settings})

    def test_invalid_png_uses_alternate_command_without_reconnect(self):
        device = self.device()
        with patch.object(device, "_run", side_effect=[b"bad", b"good"]) as run, patch.object(
                device, "_validate_screenshot", side_effect=[AdbError("truncated"), None]), patch.object(device, "connect") as conn:
            self.assertEqual(device.screen(), b"good")
            self.assertEqual(run.call_args_list[-1].args[:2], ("shell", "-T"))
            conn.assert_not_called()

    def test_no_recovery_during_combat_transport_failure(self):
        device = self.device()
        with patch.object(device, "_run", side_effect=AdbTransportError("offline")) as run:
            with self.assertRaises(AdbTransportError):
                device.screen(recover_transport=False)
        self.assertEqual(run.call_count, 1)

    def test_screenshot_shares_deadline_across_attempts(self):
        device = self.device()
        with patch("hka.adb.time.monotonic", side_effect=[0, 0, 0, 23, 23, 31]), patch.object(
                device, "_run", side_effect=AdbError("bad")) as run:
            with self.assertRaises(AdbError):
                device.screen()
        self.assertEqual([c.kwargs["timeout"] for c in run.call_args_list], [10, 7])

    def test_named_instance_query_failure_never_selects_unrelated_device(self):
        device = self.device(serial="auto", executable="MuMuManager.exe", arguments=["-v", "2"])
        with patch.object(device, "_execute", return_value=subprocess.CompletedProcess([], 1, b"", b"")), patch.object(device, "_detect_emulator") as detect:
            with self.assertRaises(AdbError):
                device.connect()
            detect.assert_not_called()

    def test_auto_multiple_devices_require_explicit_selection(self):
        device = self.device(serial="auto")
        listing = b"emulator-5554 device\nemulator-5556 device\n"
        with patch.object(device, "_execute", return_value=subprocess.CompletedProcess([], 0, listing, b"")) as execute:
            with self.assertRaisesRegex(AdbError, "多个"):
                device.connect()
            self.assertEqual(execute.call_count, 1)

    def test_missing_target_during_recovery_does_not_select_new_online_device(self):
        device = self.device(serial="auto")
        device.serial = "emulator-5554"
        def execute(command, timeout):
            error = b"error: device 'emulator-5554' not found"
            return subprocess.CompletedProcess(command, 1, b"", error)
        with patch.object(device, "_execute", side_effect=execute) as commands, patch.object(device, "_wait"):
            with self.assertRaises(AdbTransportError):
                device.screen()
        self.assertTrue(all(c.args[0][1:3] == ["-s", "emulator-5554"] for c in commands.call_args_list))
        self.assertEqual(device.serial, "emulator-5554")

    def test_cold_start_waits_for_package_manager(self):
        device = self.device()
        with patch.object(device, "connect"), patch.object(device, "_run", side_effect=["", "package:/system/framework/framework-res.apk"]) as run, patch.object(device, "_wait") as wait:
            device.wait_until_ready()
        self.assertEqual(run.call_count, 2)
        wait.assert_called_once()
