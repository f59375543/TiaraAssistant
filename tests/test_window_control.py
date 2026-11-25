import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hka.web import _wait_for_window_close


@unittest.skipUnless(os.name == "nt", "Windows desktop host uses a redirected stdin pipe")
class WindowControlTests(unittest.TestCase):
    def test_child_can_inherit_stdin_while_window_is_open(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as control:
            monitor = threading.Thread(target=_wait_for_window_close, args=(control,), daemon=True)
            monitor.start()
            try:
                os.write(write_fd, b"partial")
                time.sleep(0.2)
                self.assertTrue(monitor.is_alive())
                child = subprocess.run([sys.executable, "-B", "-c", "print('ready')"],
                                       stdin=control, capture_output=True, timeout=4, check=True)
                self.assertEqual(child.stdout.strip(), b"ready")
            finally:
                os.close(write_fd)
                monitor.join(timeout=2)
            self.assertFalse(monitor.is_alive())
