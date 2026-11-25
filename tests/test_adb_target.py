import subprocess
import sys
import unittest
from unittest.mock import patch

from hka.adb import AdbDevice, AdbTransportError


class AdbTargetTests(unittest.TestCase):
    def test_explicit_address_is_pinned_when_another_device_is_online(self):
        for address in ("127.0.0.1:5555", "localhost:5555"):
            with self.subTest(address=address):
                device = AdbDevice({"adb_path": sys.executable, "serial": address, "package": "test"})

                def execute(command, timeout):
                    if command[-1] == "devices":
                        return subprocess.CompletedProcess(command, 0, b"emulator-5556\tdevice\n", b"")
                    if command[-1] == "get-state" and address in command:
                        return subprocess.CompletedProcess(command, 1, b"", b"error: device offline")
                    return subprocess.CompletedProcess(command, 0, b"device", b"")

                with patch.object(device, "_execute", side_effect=execute), patch.object(device, "_wait"):
                    with self.assertRaises(AdbTransportError):
                        device.connect()
                self.assertEqual(device.serial, address)

    def test_only_automatic_mode_uses_the_mumu_instance_address(self):
        for configured, expected in (("127.0.0.1:5555", "127.0.0.1:5555"),
                                     ("auto", "127.0.0.1:16384")):
            with self.subTest(configured=configured):
                device = AdbDevice({"adb_path": sys.executable, "serial": configured, "package": "test",
                                    "executable": "MuMuManager.exe", "arguments": ["-v", "0"]})

                def execute(command, timeout):
                    output = b'{"index": 0, "adb_port": 16384}' if "info" in command else b"device"
                    return subprocess.CompletedProcess(command, 0, output, b"")

                with patch("hka.adb.Path.is_file", return_value=True), patch.object(device, "_execute", side_effect=execute):
                    device.connect()
                self.assertEqual(device.serial, expected)

    def test_automatic_mode_detects_single_emulator_without_manager(self):
        device = AdbDevice({"adb_path": sys.executable, "serial": "自动", "package": "test"})

        def execute(command, timeout):
            output = b"emulator-5556\tdevice\n" if "devices" in command else b"device"
            return subprocess.CompletedProcess(command, 0, output, b"")

        with patch.object(device, "_execute", side_effect=execute):
            device.connect()
        self.assertEqual(device.serial, "emulator-5556")
