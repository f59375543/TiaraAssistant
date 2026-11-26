import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hka.config import ConfigError, load_config, save_config, validate_config
from hka.defaults import DEFAULT_CONFIG, LEGACY_POPUP_STEPS
from hka.notify import send_notifications
from hka.web import WebState, config_revision, config_snapshot, save_config_snapshot


class ReleaseConfigTests(unittest.TestCase):
    def test_file_and_web_accept_utf8_bom_and_report_bad_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.json"
            path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8-sig")
            loaded = load_config(path)
            snapshot = config_snapshot(path)
            self.assertEqual(snapshot["tasks"], loaded["tasks"])
            self.assertEqual(snapshot["_revision"], config_revision(path))
            path.write_bytes(bytes([255, 0, 253]))
            for reader in (load_config, config_snapshot):
                with self.subTest(reader=reader.__name__), self.assertRaisesRegex(ConfigError, "UTF-8"):
                    reader(path)
            self.assertTrue(any("UTF-8" in message for message in WebState(path).logs))

    def test_invalid_enum_objects_raise_config_error_with_field(self):
        cases = [
            ({"type": []}, "type"),
            ({"type": "game_flow", "flow": {}}, "flow"),
            ({"type": "wait_image", "image": "a.png", "on_missing": []}, "on_missing"),
            ({"type": "game_flow", "flow": "quick_battle", "options": {"stage": {}}}, "stage"),
            ({"type": "game_flow", "flow": "stamina_farm",
              "options": {"combat": {"mode": []}}}, "combat.mode"),
            ({"type": "game_flow", "flow": "stamina_farm",
              "options": {"combat": {"weapons": {"A": {"mode": []}}}}}, "按键模式"),
        ]
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        for step, field in cases:
            cfg["tasks"][0]["steps"] = [step]
            with self.subTest(field=field), self.assertRaisesRegex(ConfigError, field):
                validate_config(cfg)
        cfg["tasks"][0]["schedule"] = {"type": []}
        with self.assertRaisesRegex(ConfigError, "schedule.type"):
            validate_config(cfg)

    def test_invalid_notification_switches_do_not_send_and_display_disabled(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["notifications"] = {"on_success": "false", "email": {"enabled": "false"},
                                "webhooks": [{"url": "https://example.test/mock"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            snapshot = config_snapshot(path)
            settings = snapshot["notifications"]
            self.assertIs(settings["on_success"], False)
            self.assertIs(settings["email"]["enabled"], False)
            snapshot["tasks"][0]["name"] = "改名测试"
            save_config_snapshot(path, snapshot)
            saved = load_config(path)["notifications"]
            self.assertIs(saved["on_success"], False)
            self.assertIs(saved["email"]["enabled"], False)
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch("hka.notify.urllib.request.urlopen", return_value=response) as post, patch(
                "hka.notify.send_email") as mail:
            errors = send_notifications(cfg, success=True, title="mock", body="mock")
            self.assertIn("on_success", errors[0])
            post.assert_not_called()
            cfg["notifications"]["on_success"] = True
            errors = send_notifications(cfg, success=True, title="mock", body="mock")
            self.assertIn("email.enabled", errors[0])
            post.assert_called_once()
            mail.assert_not_called()

    def test_legacy_popup_migration_only_without_customizations(self):
        source = copy.deepcopy(DEFAULT_CONFIG)
        task = next(t for t in source["tasks"] if t["id"] == "dismiss_popups")
        task["steps"] = copy.deepcopy(LEGACY_POPUP_STEPS)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.json"
            def loaded_steps():
                path.write_text(json.dumps(source), encoding="utf-8")
                return next(t for t in load_config(path)["tasks"] if t["id"] == "dismiss_popups")["steps"]
            self.assertEqual(loaded_steps(), [{"type": "game_flow", "flow": "dismiss_login_popups"}])
            templates = root / "templates"
            templates.mkdir()
            marker = templates / "popup_close.png"
            marker.touch()
            self.assertEqual(loaded_steps(), LEGACY_POPUP_STEPS)
            marker.unlink()
            task["steps"][0]["times"] = 3
            self.assertEqual(loaded_steps(), task["steps"])

    def test_load_wait_and_flags_must_be_valid_before_execution(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        step = next(t for t in cfg["tasks"] if t["id"] == "stamina_farm")["steps"][0]
        for value in (None, "10", True, float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "load_wait"):
                step["options"]["load_wait"] = value
                validate_config(cfg)
        step["options"]["load_wait"] = 0.5
        validate_config(cfg)
        for section, key in (("emulator", "stop_after_run"), ("runtime", "stop_on_error")):
            candidate = copy.deepcopy(cfg)
            candidate[section][key] = "false"
            with self.assertRaisesRegex(ConfigError, key):
                validate_config(candidate)

    def test_break_only_inside_repeat_including_conditional_branches(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        early_exit = {"type": "tap_image", "image": "x.png", "on_missing": "break_repeat"}
        branch = {"type": "if_image", "image": "y.png", "then": [early_exit]}
        cfg["tasks"][0]["steps"] = [branch]
        with self.assertRaisesRegex(ConfigError, "break_repeat"):
            validate_config(cfg)
        cfg["tasks"][0]["steps"] = [{"type": "repeat", "times": 2, "steps": [branch]}]
        validate_config(cfg)

    def test_bad_hook_is_rejected_at_save_and_isolated_at_send(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["notifications"]["webhooks"] = [42, {"url": "https://example.test/hook"}]
        cfg["notifications"]["email"]["enabled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with self.assertRaisesRegex(ValueError, "Webhook 1"):
                save_config(path, cfg)
            self.assertFalse(path.exists())
        reply = MagicMock()
        reply.__enter__.return_value.status = 200
        with patch("hka.notify.urllib.request.urlopen", return_value=reply) as post, patch("hka.notify.send_email") as mail:
            errors = send_notifications(cfg, success=True, title="运行", body="完成")
        self.assertEqual(len(errors), 1)
        self.assertIn("Webhook 1", errors[0])
        post.assert_called_once()
        mail.assert_called_once()
