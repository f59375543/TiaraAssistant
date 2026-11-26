from __future__ import annotations

import os
import io
import json
import re
import shutil
import subprocess
import time
import threading
from pathlib import Path
from .cancellation import Cancelled


class AdbError(RuntimeError):
    pass


class AdbTransportError(AdbError):
    pass


class AdbInputDenied(AdbError):
    """Android denied input injection; other task inputs would fail too."""


def _background_options() -> dict:
    # pythonw otherwise gives each console helper its own visible window.
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class AdbDevice:
    def __init__(self, config: dict):
        self.adb_path = config.get("adb_path", "builtin").strip().strip('"')
        bundled_adb = Path(__file__).resolve().parents[1] / "platform-tools" / "adb.exe"
        if self.adb_path.lower() in {"builtin", "内置"}:
            if not bundled_adb.is_file():
                raise AdbError("内置 ADB 缺失，请重新解压完整免安装包，或指定 adb.exe 的完整路径")
            self.adb_path = str(bundled_adb)
        elif self.adb_path.lower() in {"adb", "adb.exe"} and bundled_adb.is_file():
            self.adb_path = str(bundled_adb)
        elif Path(self.adb_path).is_dir():
            executable = Path(self.adb_path) / "adb.exe"
            if not executable.is_file():
                raise AdbError(f"ADB 文件夹中没有 adb.exe: {self.adb_path}")
            self.adb_path = str(executable.resolve())
        if os.name == "nt" and not os.path.isabs(self.adb_path):
            resolved = shutil.which(self.adb_path)
            if resolved:
                self.adb_path = os.path.abspath(resolved)
        self.serial = config["serial"].strip()
        self._auto_serial = self.serial.lower() in {"auto", "自动"}
        self.package = config["package"]
        self.emulator_path = config.get("executable", "")
        self.emulator_args = config.get("arguments", [])
        self.launch_delay = float(config.get("launch_delay", 10))
        self._owned_process: subprocess.Popen | None = None
        self.cancel: threading.Event | None = None
        self._screenshot_command = ("exec-out", "screencap", "-p")

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def _wait(self, seconds: float) -> None:
        self._check_cancel()
        if self.cancel is None:
            time.sleep(seconds)
        else:
            self.cancel.wait(max(0, seconds))
        self._check_cancel()

    def _execute(self, command: list[str], timeout: float):
        self._check_cancel()
        options = _background_options()
        if os.name == "nt" and command[0] == self.adb_path:
            # ADB may start a shared daemon that outlives the assistant.
            # Give it a stable working directory outside the portable installation.
            options["cwd"] = os.environ["SystemRoot"]
        if self.cancel is None:
            return subprocess.run(command, capture_output=True, timeout=timeout, check=False,
                                  **options)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   **options)
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._check_cancel()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    self._check_cancel()
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def _execution_error(self, exc: Exception) -> AdbError:
        if isinstance(exc, PermissionError):
            return AdbError(f"ADB 无法执行: {self.adb_path}。请确认路径指向 adb.exe，完整解压安装包，并检查文件是否被安全软件阻止。原始错误: {exc}")
        return AdbError(f"ADB 执行失败: {self.adb_path}: {exc}")

    def _run(self, *args: str, timeout: float = 30, binary: bool = False) -> str | bytes:
        command = [self.adb_path, "-s", self.serial, *args]
        try:
            result = self._execute(command, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._execution_error(exc) from exc
        if result.returncode:
            message = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            if message.lower() in {"error: closed", "error: device offline", "error: no devices/emulators found",
                                    f"error: device '{self.serial.lower()}' not found"}:
                raise AdbTransportError(f"ADB 返回错误: {message}")
            raise AdbError(f"ADB 返回错误: {message}")
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace").strip()

    def start_emulator(self) -> None:
        self._check_cancel()
        if not self.emulator_path:
            return
        path = Path(self.emulator_path).expanduser()
        if not path.is_file():
            raise AdbError(f"模拟器程序不存在: {path}")
        self._owned_process = subprocess.Popen([str(path), *self.emulator_args], cwd=str(path.parent),
                                                **_background_options())
        self._wait(self.launch_delay)

    def owns_live_emulator(self) -> bool:
        return self._owned_process is not None and self._owned_process.poll() is None

    def stop_emulator(self) -> None:
        process = self._owned_process
        if process is None or process.poll() is not None:
            return
        player = Path(self.emulator_path).expanduser().resolve()
        console = player.with_name("ldconsole.exe")
        if player.name.lower() == "dnplayer.exe" and console.is_file():
            try:
                listed = subprocess.run([str(console), "list2"], cwd=player.parent, check=True,
                                        capture_output=True, timeout=4, **_background_options())
                index = self._ldplayer_index(listed.stdout, process.pid)
                if index is not None and process.poll() is None:
                    subprocess.run([str(console), "quit", "--index", str(index)], cwd=player.parent,
                                   check=True, capture_output=True, timeout=8, **_background_options())
                    process.wait(timeout=10)
                    return
            except (OSError, subprocess.SubprocessError):
                pass  # The owned process handle is still safe for the fallback.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    @staticmethod
    def _ldplayer_index(listing: bytes, process_id: int) -> int | None:
        # Old list2 has 7 fields; newer versions append width, height and DPI.
        # Reject malformed or ambiguous listings instead of guessing an index.
        instances = {}
        for row in listing.splitlines():
            if not row.strip():
                continue
            fields = [field.strip() for field in row.split(b",")]
            if len(fields) not in (7, 10) or not fields[0].isdigit() or fields[4] not in (b"0", b"1"):
                return None
            try:
                index, pid = int(fields[0]), int(fields[5])
            except ValueError:
                return None
            if index in instances:
                return None
            instances[index] = pid
        matches = [index for index, pid in instances.items() if pid == process_id]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _budget(limit: float, deadline: float | None) -> float:
        if deadline is None:
            return limit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdbError("设备操作已达到总时限")
        return min(limit, remaining)

    def connect(self, *, rediscover: bool = True, deadline: float | None = None) -> None:
        if rediscover and self._auto_serial:
            self._refresh_mumu_address(deadline)
            if self.serial.lower() in {"auto", "自动"}:
                self.serial = self._detect_emulator(deadline)
        connect_message = ""
        if ":" in self.serial:
            try:
                result = self._execute([self.adb_path, "connect", self.serial], self._budget(15, deadline))
                connect_message = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise self._execution_error(exc) from exc
        for attempt in range(4):
            self._check_cancel()
            try:
                state = self._run("get-state", timeout=self._budget(5, deadline))
                if state == "device":
                    return
                if state == "offline":
                    raise AdbTransportError(f"设备未就绪: {self.serial} ({state})")
                raise AdbError(f"设备未就绪: {self.serial} ({state})")
            except AdbError as exc:
                if attempt == 3:
                    detail = f"；连接响应: {connect_message}" if connect_message else ""
                    raise type(exc)(f"无法连接设备 {self.serial}: {exc}{detail}") from exc
                # A connect response can precede transport readiness, especially
                # after recovering a closed screenshot stream.
                self._wait(self._budget(0.5 * (attempt + 1), deadline))

    def _refresh_mumu_address(self, deadline: float | None = None) -> None:
        # MuMu can keep a stale ADB transport alive after its window has exited,
        # then assign a different port on launch. Only resolve an explicitly
        # configured instance; never guess between other connected emulators.
        executable = Path(self.emulator_path)
        if executable.name.lower() != "mumumanager.exe":
            return
        if not (self.serial.lower() in {"auto", "自动"} or
                self.serial.startswith(("127.0.0.1:", "localhost:", "emulator-"))):
            return
        index = None
        for position, arg in enumerate(self.emulator_args):
            if arg in {"-v", "--vmindex"} and position + 1 < len(self.emulator_args):
                index = self.emulator_args[position + 1]
                break
            if arg.startswith("--vmindex="):
                index = arg.split("=", 1)[1]
                break
        if index is None and not any(arg in {"-v", "--vmindex"} for arg in self.emulator_args):
            return
        if index is None or not index.isdigit():
            raise AdbError("MuMu 实例编号无效")
        try:
            result = self._execute([str(executable), "info", "-v", index], self._budget(10, deadline))
            if result.returncode:
                raise AdbError(f"无法确认 MuMu 实例 {index}，未选择其他设备")
            info = json.loads(result.stdout.decode("utf-8-sig"))
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise AdbError(f"无法读取 MuMu 实例 {index} 的设备信息") from exc
        if not isinstance(info, dict) or str(info.get("index")) != index:
            raise AdbError(f"MuMu 返回的实例与配置 {index} 不符")
        if info.get("is_process_started") is False or info.get("is_android_started") is False:
            raise AdbTransportError(f"MuMu 实例 {index} 尚未启动完成")
        port = info.get("adb_port")
        host = info.get("adb_host_ip", "127.0.0.1")
        if type(port) is int and 0 < port < 65536 and host in {"127.0.0.1", "localhost"}:
            self.serial = f"{host}:{port}"
        else:
            raise AdbError(f"MuMu 实例 {index} 未提供有效的本地 ADB 地址")

    def _detect_emulator(self, deadline: float | None = None) -> str:
        try:
            result = self._execute([self.adb_path, "devices"], self._budget(15, deadline))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._execution_error(exc) from exc
        if result.returncode:
            raise AdbError(result.stderr.decode("utf-8", errors="replace").strip())
        devices = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "device" and (
                    fields[0].startswith(("127.0.0.1:", "localhost:", "emulator-"))):
                devices.append(fields[0])
        if len(devices) == 1:
            return devices[0]
        if devices:
            raise AdbError("检测到多个模拟器，请填写要使用的设备地址: " + "、".join(devices))
        raise AdbTransportError("未检测到已连接的模拟器，请开启模拟器 ADB 调试，并填写模拟器设置中显示的实际 ADB 地址")

    def wait_until_ready(self, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.connect(deadline=deadline)
                if self._package_manager_ready(deadline):
                    return
                last_error = AdbError("Android 包管理器尚未就绪")
            except AdbError as exc:
                last_error = exc
            self._wait(min(2, max(0, deadline - time.monotonic())))
        raise AdbError(f"模拟器启动超时: {last_error}")

    def _package_manager_ready(self, deadline: float | None = None) -> bool:
        output = self._run("shell", "pm", "path", "android", timeout=self._budget(5, deadline))
        return any(line.startswith("package:") for line in output.splitlines())

    def screen(self, *, recover_transport: bool = True) -> bytes:
        errors = []
        transport_failed = False
        fallback = ("shell", "-T", "screencap", "-p")
        alternative = fallback if self._screenshot_command[0] == "exec-out" else ("exec-out", "screencap", "-p")
        deadline = time.monotonic() + 30
        reset_transport = False
        for command in (self._screenshot_command, alternative, self._screenshot_command):
            self._check_cancel()
            if time.monotonic() >= deadline:
                break
            try:
                if reset_transport:
                    # Reset this device's transport only, keeping other emulators
                    # and tools using the shared ADB server connected.
                    if ":" in self.serial:
                        self._execute([self.adb_path, "disconnect", self.serial], self._budget(5, deadline))
                    else:
                        self._run("reconnect", timeout=self._budget(5, deadline))
                    self.connect(rediscover=False, deadline=deadline)
                    reset_transport = False
                data = self._run(*command, timeout=self._budget(10, deadline), binary=True)
                self._validate_screenshot(data)
                self._screenshot_command = command
                return data
            except (AdbError, OSError, subprocess.TimeoutExpired) as exc:
                if isinstance(exc, AdbTransportError):
                    if not recover_transport:
                        raise
                    transport_failed = True
                    reset_transport = True
                errors.append(str(exc))
        error_type = AdbTransportError if transport_failed else AdbError
        raise error_type(f"模拟器截图失败（{self.serial}），限定次数和时间内未恢复。"
                       "请确认对应模拟器已启动并能正常显示画面，必要时重启该实例后重试。详情: "
                       + "；".join(errors))

    @staticmethod
    def _validate_screenshot(data: bytes) -> None:
        from PIL import Image
        try:
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("不是 PNG 数据")
            with Image.open(io.BytesIO(data)) as screenshot:
                screenshot.verify()
        except (OSError, ValueError, SyntaxError) as exc:
            raise AdbError(f"模拟器截图无效或不完整: {exc}") from exc

    def size(self) -> tuple[int, int]:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(self.screen())) as image:
            return image.size

    def tap(self, x: int, y: int) -> None:
        self._input("点按", "tap", str(x), str(y))

    def foreground_package(self) -> str | None:
        output = self._run("shell", "dumpsys", "activity", "activities")
        match = re.search(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[:=]\s*ActivityRecord\{[^\n]*?\bu\d+\s+([\w.]+)/", output)
        return match.group(1) if match else None

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> None:
        self._input("滑动/长按", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def key(self, code: int) -> None:
        self._input("按键", "keyevent", str(code))

    def _input(self, action: str, *args: str) -> None:
        try:
            self._run("shell", "input", *args)
        except AdbError as exc:
            if "INJECT_EVENTS" not in str(exc):
                raise
            # Never replay an input that may already have partially executed.
            raise AdbInputDenied(
                f"模拟器拒绝{action}输入（INJECT_EVENTS），本轮已停止。"
                "请重启模拟器后重试；如果仍失败，请检查模拟器的 ADB 输入授权。"
                f"原始错误: {exc}"
            ) from exc

    def launch_game(self) -> None:
        intent = ("-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER")
        errors = []
        # Some emulator images omit monkey. Resolve the installed launcher rather
        # than hardcoding an Activity, which can differ between game channels.
        for resolver in (("cmd", "package"), ("pm",)):
            try:
                output = self._run("shell", *resolver, "resolve-activity", "--brief",
                                   *intent, "-p", self.package)
                component = next((line.strip() for line in reversed(output.splitlines())
                                  if re.fullmatch(re.escape(self.package) + r"/[\w.$]+", line.strip())), None)
                if component is None:
                    errors.append(f"{' '.join(resolver)}: {output}")
                    continue
                output = self._run("shell", "am", "start", *intent, "-n", component)
                # Android tools may print a failure while returning exit code 0.
                if re.search(r"(?im)^\s*(?:error\b|exception\b|securityexception\b)", output):
                    raise AdbError(output)
                return
            except AdbError as exc:
                errors.append(str(exc))
        try:
            output = self._run("shell", "monkey", "-p", self.package,
                               "-c", "android.intent.category.LAUNCHER", "1")
            if re.search(r"(?i)monkey aborted|no activities found|inaccessible or not found", output):
                raise AdbError(output)
        except AdbError as exc:
            errors.append(str(exc))
            raise AdbError(f"无法启动游戏 {self.package}，请确认游戏已安装且包名与渠道一致。"
                           + " 启动详情: " + "；".join(errors)) from exc

    def installed(self) -> bool:
        output = self._run("shell", "pm", "path", self.package)
        if any(line.startswith("package:") for line in output.splitlines()):
            return True
        if not self._package_manager_ready():
            raise AdbError("Android 包管理器尚未就绪，无法确认游戏安装状态")
        return False
