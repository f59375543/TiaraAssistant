import io
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from hka.defaults import DEFAULT_CONFIG
from hka.web import StaleFrame, WebState, serve


def screenshot(color):
    image = Image.new("RGB", (32, 32), color)
    image.putpixel((1, 1), (255, 255, 255))
    png = io.BytesIO()
    image.save(png, format="PNG")
    return png.getvalue()


class CaptureApiTests(unittest.TestCase):
    def test_manual_input_requires_same_frame_device_and_viewport(self):
        with tempfile.TemporaryDirectory() as directory, patch("hka.web.AdbDevice") as factory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
            state = WebState(path)
            device = factory.return_value
            device.serial = "first"
            device.screen.return_value = screenshot("red")
            device.size.return_value = (32, 32)
            frame = state.capture()
            with self.assertRaises(StaleFrame):
                state.tap(None, 0.5, 0.5)
            device.serial = "second"
            with self.assertRaises(StaleFrame):
                state.tap(frame.id, 0.5, 0.5)
            device.serial = "first"
            device.size.return_value = (64, 32)
            with self.assertRaises(StaleFrame):
                state.tap(frame.id, 0.5, 0.5)
            device.tap.assert_not_called()
            device.size.return_value = (32, 32)
            state.tap(frame.id, 1, 1)
            device.tap.assert_called_once_with(31, 31)
            with self.assertRaises(StaleFrame):
                state.tap(frame.id, 1, 1)

    def test_only_current_frame_can_save_a_template(self):
        with tempfile.TemporaryDirectory() as directory, patch("hka.web.AdbDevice") as device:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
            device.return_value.screen.side_effect = [screenshot("red"), screenshot("blue")]
            ready = queue.Queue()
            reader, writer = os.pipe()
            with os.fdopen(reader, "rb") as control, os.fdopen(writer, "wb") as stop:
                server = threading.Thread(target=serve, args=(path, 0), kwargs={
                    "ready": ready.put, "open_browser": False, "control": control,
                }, daemon=True)
                server.start()
                try:
                    address = ready.get(timeout=5)
                    ids = []
                    for _ in range(2):
                        with urlopen(address + "api/screenshot", timeout=5) as response:
                            ids.append(response.headers["X-Screenshot-Id"])
                            response.read()
                    self.assertTrue(all(ids))
                    self.assertNotEqual(*ids)
                    payload = {"name": "button.png", "box": [0, 0, .5, .5]}
                    target = path.parent / "templates" / "button.png"

                    def save():
                        req = Request(address + "api/template", data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json"})
                        with urlopen(req, timeout=5) as response:
                            return json.load(response)

                    for frame_id in (None, ids[0]):
                        if frame_id:
                            payload["screenshot_id"] = frame_id
                        with self.assertRaises(HTTPError) as error:
                            save()
                        self.assertEqual(error.exception.code, 409)
                        error.exception.close()
                        self.assertFalse(target.exists())
                    payload["screenshot_id"] = ids[1]
                    self.assertEqual(save()["size"], [16, 16])
                    with Image.open(target) as image:
                        self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))
                finally:
                    stop.write(b"\n")
                    stop.flush()
                    server.join(timeout=5)
                    self.assertFalse(server.is_alive())
