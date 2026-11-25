import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hka.adb import AdbDevice


class EmulatorExitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.console = Path(directory.name) / "ldconsole.exe"
        self.console.touch()
        self.device = AdbDevice({"adb_path": "qa-adb", "package": "qa.game", "serial": "qa",
                                 "executable": str(self.console.with_name("dnplayer.exe"))})
        self.process = Mock(pid=8731)
        self.process.poll.return_value = None
        self.device._owned_process = self.process

    @patch("hka.adb.subprocess.run")
    def test_exact_gui_pid_and_index(self, run):
        run.return_value.stdout = (b"0,other,10,11,1,3333,8731\n"
                                   b"4,target,12,13,1,8731,1000,1920,1080,240\n")
        self.device.stop_emulator()
        self.assertEqual([call.args[0][1:] for call in run.call_args_list],
                         [["list2"], ["quit", "--index", "4"]])
        self.process.terminate.assert_not_called()
        self.process.wait.assert_called_once()

    @patch("hka.adb.subprocess.run")
    def test_unmatched_or_ambiguous_pid(self, run):
        for listing in (b"0,other,0,0,1,1234,8731", b"0,m,0,0,1,8731,2\n1,n,0,0,1,8731,3",
                        b"0,m,0,0,1,8731,2\n0,n,0,0,1,1234,3", b"invalid"):
            with self.subTest(listing=listing):
                run.reset_mock()
                self.process.reset_mock()
                run.return_value.stdout = listing
                self.device.stop_emulator()
                self.assertEqual([call.args[0][1:] for call in run.call_args_list], [["list2"]])
                self.process.terminate.assert_called_once()

    @patch("hka.adb.subprocess.run")
    def test_unowned_and_exit_during_query(self, run):
        self.device._owned_process = None
        self.device.stop_emulator()
        run.assert_not_called()
        self.device._owned_process = self.process
        self.process.poll.side_effect = [None, 0, 0]
        run.return_value.stdout = b"0,m,0,0,1,8731,4"
        self.device.stop_emulator()
        self.assertEqual([call.args[0][1:] for call in run.call_args_list], [["list2"]])
        self.process.terminate.assert_not_called()

    @patch("hka.adb.subprocess.run")
    def test_console_timeout_uses_only_owned_handle(self, run):
        run.side_effect = subprocess.TimeoutExpired("list2", 4)
        self.device.stop_emulator()
        run.assert_called_once()
        self.process.terminate.assert_called_once()
