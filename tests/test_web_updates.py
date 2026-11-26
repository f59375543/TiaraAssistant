import json
import os
import queue
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hka.defaults import DEFAULT_CONFIG
from hka.web import StaleFrame, WebState, serve
from test_capture_api import screenshot


class WebUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "config.json"
        self.path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
        self.state = WebState(self.path)

    def test_status_sends_only_unread_lines_and_changed_report(self):
        self.state.log("first")
        first = self.state.status_delta({})
        cursor = {"stream": [first["stream"]], "cursor": [str(first["cursor"])],
                  "report": [first["report_version"]]}
        self.state.log("second")
        updated = self.state.status_delta(cursor)
        self.assertEqual(updated["logs"], ["second"])
        self.assertFalse(updated["logs_reset"])
        self.assertNotIn("report", updated)
        for index in range(301):
            self.state.log(str(index))
        overflow = self.state.status_delta(cursor)
        self.assertEqual(overflow["logs"], [str(i) for i in range(1,301)])
        self.assertTrue(overflow["logs_reset"])
        self.assertTrue(self.state.status_delta({"stream": ["previous"]})["logs_reset"])

    def test_plan_revision_observes_external_changes_and_daily_reset(self):
        config = json.loads(self.path.read_text(encoding="utf-8"))
        config["runtime"]["daily_reset_at"] = "06:00"
        self.path.write_text(json.dumps(config), encoding="utf-8")
        with patch("hka.web.datetime") as clock:
            clock.now.return_value = datetime(2026,11,26,5,59)
            before = self.state.plan_version()
            self.assertEqual(before, self.state.plan_version())
            clock.now.return_value = datetime(2026,11,26,6)
            reset = self.state.plan_version()
            self.assertNotEqual(reset, before)
            attempts = self.path.parent / ".daily-attempts" / "2026-11-26"
            attempts.mkdir(parents=True)
            (attempts / "mail").touch()
            attempted = self.state.plan_version()
            self.assertNotEqual(attempted, reset)
            (self.path.parent / "state.json").write_text('{"2026-11-26":["mail"]}')
            self.assertNotEqual(self.state.plan_version(), attempted)

    def test_bad_selection_never_starts_a_worker(self):
        for selection in ([], ["missing-task"], ["mail","missing-task"], "", [None]):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.state.start_run(selection, True)
            self.assertIsNone(self.state.worker)
            self.assertFalse(self.state.running)

    def test_http_legacy_null_selection_and_strict_force(self):
        reader, writer = os.pipe()
        ready = queue.Queue()
        with os.fdopen(reader, "rb") as control, os.fdopen(writer, "wb") as shutdown:
            service = threading.Thread(target=serve, args=(self.path, 0), kwargs={
                "ready": ready.put, "open_browser": False, "control": control})
            service.start()
            try:
                url = ready.get(timeout=5) + "api/run"
                called = threading.Event()
                def mock_run(**kwargs):
                    called.set()
                    return {"status":"nothing_to_do"}
                with patch("hka.web.AdbDevice"), patch("hka.web.TaskRunner.run", side_effect=mock_run) as run:
                    for invalid in ("false", 1, None):
                        request = Request(url, json.dumps({"tasks":None,"force":invalid}).encode(),
                                          headers={"Content-Type":"application/json"})
                        with self.assertRaises(HTTPError) as rejected:
                            urlopen(request, timeout=3)
                        with rejected.exception as response:
                            self.assertEqual(response.code, 400)
                            self.assertIn("force", json.load(response)["error"])
                    run.assert_not_called()
                    with urlopen(Request(url, b'{"tasks":null,"force":false}',
                                         headers={"Content-Type":"application/json"}), timeout=3) as response:
                        self.assertTrue(json.load(response)["ok"])
                    self.assertTrue(called.wait(3))
            finally:
                shutdown.write(b"\n")
                shutdown.flush()
                service.join(timeout=5)
                self.assertFalse(service.is_alive())
        run.assert_called_once_with(task_ids=None, force=False)

    @patch("hka.web.AdbDevice")
    def test_capture_in_progress_invalidates_waiting_manual_tap(self, factory):
        device = factory.return_value
        device.serial = "mock"
        device.size.return_value = (32,32)
        device.screen.return_value = screenshot("red")
        old = self.state.capture()
        capture_entered = threading.Event()
        capture_release = threading.Event()
        def delayed_screen():
            capture_entered.set()
            if not capture_release.wait(3):
                raise TimeoutError("capture never released")
            return screenshot("blue")
        device.screen.side_effect = delayed_screen
        errors = []
        def tap():
            try:
                self.state.tap(old.id, .5, .5)
            except Exception as exc:
                errors.append(exc)
        refresh = threading.Thread(target=self.state.capture)
        refresh.start()
        try:
            self.assertTrue(capture_entered.wait(3))
            click = threading.Thread(target=tap)
            click.start()
        finally:
            capture_release.set()
        refresh.join(3)
        click.join(3)
        self.assertEqual([type(exc) for exc in errors], [StaleFrame])
        device.tap.assert_not_called()

    @patch("hka.web.AdbDevice")
    def test_two_clicks_on_one_frame_deliver_only_one_input(self, factory):
        device = factory.return_value
        device.serial = "mock"
        device.size.return_value = (32, 32)
        device.screen.return_value = screenshot("red")
        frame = self.state.capture()
        gate = threading.Barrier(2)
        outcomes = []
        def click():
            gate.wait(timeout=3)
            try:
                self.state.tap(frame.id, .5, .5)
            except StaleFrame:
                outcomes.append("expired")
            else:
                outcomes.append("sent")
        clicks = [threading.Thread(target=click) for _ in range(2)]
        for thread in clicks:
            thread.start()
        for thread in clicks:
            thread.join(4)
        self.assertCountEqual(outcomes, ["expired", "sent"])
        device.tap.assert_called_once_with(16, 16)

    @patch("hka.web.AdbDevice")
    def test_close_cancels_worker_before_stopping_owned_emulator(self, factory):
        entered = threading.Event()
        finished = threading.Event()
        device = factory.return_value
        device.serial = "mock"
        device.size.return_value = (32, 32)
        device.screen.return_value = screenshot("blue")
        def run(*args, **kwargs):
            entered.set()
            if not self.state.cancel.wait(5):
                raise RuntimeError("worker did not receive cancellation")
            finished.set()
            return {"status": "cancelled"}
        device.owns_live_emulator.return_value = True
        device.stop_emulator.side_effect = lambda: self.assertTrue(finished.is_set())
        frame = self.state.capture()
        with patch("hka.web.TaskRunner.run", side_effect=run):
            self.state.start_run(None, False)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(RuntimeError, "运行中不能手动点击"):
                    self.state.tap(frame.id, .5, .5)
            finally:
                self.state.finish_close()
        self.assertEqual(self.state.result["status"], "cancelled")
        self.assertFalse(self.state.worker.is_alive())
        device.tap.assert_not_called()
        device.stop_emulator.assert_called_once()
