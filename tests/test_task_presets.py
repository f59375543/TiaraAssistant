import copy
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hka.config import load_config, task_presets
from hka.defaults import DEFAULT_CONFIG
from hka.web import serve


class TaskPresetTests(unittest.TestCase):
    def test_normalized_presets_are_independent(self):
        first = task_presets()
        first[0]["steps"].clear()
        self.assertTrue(task_presets()[0]["steps"])
        self.assertTrue(DEFAULT_CONFIG["tasks"][0]["steps"])
        self.assertTrue(all(t["steps"] and t["frequency"] for t in task_presets()))
        festival = next(t for t in first if t["id"] == "festival_event")
        self.assertFalse(festival["enabled"])
        self.assertEqual(festival["schedule"]["type"], "range")

    def test_api_legacy_upgrade_delete_all_and_restore_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            legacy = copy.deepcopy(DEFAULT_CONFIG)
            legacy["tasks"] = [t for t in legacy["tasks"] if t["id"] != "guild_daily"]
            path.write_text(json.dumps(legacy), encoding="utf-8")
            ready = queue.Queue()
            read_fd, write_fd = os.pipe()
            with os.fdopen(read_fd) as control, os.fdopen(write_fd, "w") as signal:
                thread = threading.Thread(target=serve, args=(path, 0), kwargs={
                    "ready": ready.put, "open_browser": False, "control": control,
                }, daemon=True)
                thread.start()
                try:
                    address = ready.get(timeout=5)
                    def api(name, payload=None):
                        req = Request(address + "api/" + name)
                        if payload is not None:
                            req.data = json.dumps(payload).encode()
                            req.add_header("Content-Type", "application/json")
                        with urlopen(req, timeout=5) as response:
                            return json.load(response)

                    catalog = api("task-presets")
                    self.assertEqual(catalog, task_presets())
                    model = api("config")
                    ids = [t["id"] for t in model["tasks"]]
                    self.assertEqual(ids[ids.index("mail") + 1], "guild_daily")
                    model["tasks"] = [t for t in model["tasks"] if t["id"] != "guild_daily"]
                    self.assertTrue(api("config", model)["ok"])
                    self.assertEqual(load_config(path)["tasks"], model["tasks"])
                    model = api("config")
                    model["tasks"] = []
                    self.assertTrue(api("config", model)["ok"])
                    self.assertEqual(api("plan")["items"], [])
                    model = api("config")
                    self.assertEqual(model["tasks"], [])
                    model["tasks"].append(catalog[0])
                    self.assertTrue(api("config", model)["ok"])
                    self.assertEqual(load_config(path)["tasks"], [catalog[0]])
                finally:
                    signal.write("\n")
                    signal.flush()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
