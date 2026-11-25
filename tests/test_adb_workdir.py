import os
from pathlib import Path
import sys
import threading
import unittest

from hka.adb import AdbDevice


@unittest.skipUnless(os.name == "nt", "Windows directory locking")
class AdbWorkingDirectoryTests(unittest.TestCase):
    def test_adb_child_and_its_server_inherit_external_workdir(self):
        # Use Python as the client and its child to observe the actual inherited
        # working directory without touching the machine's shared ADB server.
        device = AdbDevice({"adb_path": sys.executable, "serial": "test", "package": "test"})
        probe = ("import os, subprocess, sys\n"
                 "print(os.getcwd(), flush=True)\n"
                 "subprocess.run([sys.executable, '-c', 'import os; print(os.getcwd())'], check=True)")
        for cancel in (None, threading.Event()):
            with self.subTest(cancellable=cancel is not None):
                device.cancel = cancel
                result = device._execute([device.adb_path, "-B", "-c", probe], timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                paths = [Path(line) for line in result.stdout.decode().splitlines()]
                self.assertEqual(paths, [Path(os.environ["SystemRoot"])] * 2)
