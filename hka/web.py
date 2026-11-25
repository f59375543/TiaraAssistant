from __future__ import annotations

import json
import errno
import copy
import hashlib
import io
import os
import socket
import threading
import time
import webbrowser
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image

from .adb import AdbDevice
from .config import ConfigError, load_config, save_config, normalize_config, task_presets
from .runner import TaskRunner, load_latest_report
from .vision import crop_template, template_path
from .mail_credentials import protect_secret
from .notify import email_options, send_email


UI_DIR = Path(__file__).parent / "ui"
UI_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/ui/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/ui/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def server_bind(self):
        if os.name == "nt":
            # Windows SO_REUSEADDR allows unrelated instances to share requests.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def config_revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_snapshot(path: Path) -> dict:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    normalize_config(data)
    email = data.get('notifications', {}).get('email', {})
    email['has_authorization_code'] = bool(email.pop('authorization_code_encrypted', ''))
    email.pop('authorization_code', None)
    data["_revision"] = hashlib.sha256(raw).hexdigest()
    return data


def save_config_snapshot(path: Path, data: dict) -> str:
    value = copy.deepcopy(data)
    if value.pop("_revision", None) != config_revision(path):
        raise ConfigError("配置已更新，请刷新页面后再保存，避免旧页面覆盖新流程")
    value.setdefault("runtime", {})["scheduler_enabled"] = False
    email = value.setdefault('notifications', {}).setdefault('email', {})
    current = load_config(path).get('notifications', {}).get('email', {})
    secret = email.pop('authorization_code', '').strip()
    email.pop('has_authorization_code', None)
    email.pop('authorization_code_encrypted', None)
    if secret:
        email['authorization_code_encrypted'] = protect_secret(secret)
    elif all(email.get(key, '') == current.get(key, '') for key in ('qq_number', 'username', 'host', 'provider')):
        if current.get('authorization_code_encrypted'):
            email['authorization_code_encrypted'] = current['authorization_code_encrypted']
    if email.get('enabled'):
        email_options(email)
    save_config(path, value)
    return config_revision(path)


@dataclass(frozen=True)
class CapturedFrame:
    id: str
    png: bytes
    serial: str
    size: tuple[int, int]
    emulator: dict
    manual_input_allowed: bool


class StaleFrame(ValueError):
    pass


class WebState:
    def __init__(self, config_path: Path, request_window_close=None):
        self.config_path = config_path
        self.request_window_close = request_window_close
        self.lock = threading.Lock()
        self.device_lock = threading.Lock()
        self.running = False
        self.closing = threading.Event()
        self.worker = None
        self.owned_devices = []
        self.cancel = threading.Event()
        self.logs: list[str] = []
        self.result: dict | None = None
        self.last_result: dict | None = None
        try:
            runtime = load_config(config_path).get("runtime", {})
            self.last_result = load_latest_report(config_path.parent / runtime.get("run_dir", "logs"))
        except (ConfigError, OSError, TypeError) as exc:
            self.log(f"无法载入上次运行记录：{exc}")
        self.last_screenshot: CapturedFrame | None = None

    def capture(self) -> CapturedFrame:
        with self.device_lock:
            with self.lock:
                manual_input_allowed = not self.running
            emulator = load_config(self.config_path)["emulator"]
            device = AdbDevice(emulator)
            device.cancel = self.closing
            device.connect()
            png = device.screen()
            with Image.open(io.BytesIO(png)) as image:
                frame = CapturedFrame(uuid.uuid4().hex, png, device.serial, image.size,
                                      emulator, manual_input_allowed)
            with self.lock:
                self.last_screenshot = frame
            return frame

    def frame_for(self, frame_id: str | None) -> CapturedFrame:
        with self.lock:
            frame = self.last_screenshot
        if frame is None or frame.id != frame_id:
            raise StaleFrame("截图已失效，请重新获取模拟器截图")
        return frame

    def tap(self, frame_id: str | None, x: float, y: float) -> None:
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError("点击坐标必须在画面内")
        with self.device_lock:
            with self.lock:
                if self.running:
                    raise RuntimeError("运行中不能手动点击")
            frame = self.frame_for(frame_id)
            if not frame.manual_input_allowed:
                raise StaleFrame("这是运行中的截图，请重新获取模拟器截图")
            config = load_config(self.config_path)
            if config["emulator"] != frame.emulator:
                raise StaleFrame("设备设置已更改，请重新获取模拟器截图")
            runner = TaskRunner(config, self.config_path)
            with runner.exclusive_run():
                device = AdbDevice(config["emulator"])
                device.cancel = self.closing
                device.connect()
                if device.serial != frame.serial or device.size() != frame.size:
                    raise StaleFrame("设备或画面尺寸已变化，请重新获取模拟器截图")
                with self.lock:
                    self.last_screenshot = None
                width, height = frame.size
                device.tap(min(width - 1, round(x * width)), min(height - 1, round(y * height)))

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            if len(self.logs) > 300:
                del self.logs[:-300]

    def start_run(self, tasks: list[str] | None, force: bool) -> None:
        with self.lock:
            if self.closing.is_set():
                raise RuntimeError("助手正在关闭")
            if self.running:
                raise RuntimeError("任务正在运行")
            self.running = True
            self.last_screenshot = None
            self.cancel = threading.Event()
            self.logs = []
            self.result = None

        def worker() -> None:
            device = None
            close_after_run = False
            try:
                config = load_config(self.config_path)
                close_after_run = config.get("runtime", {}).get("close_tool_after_run", False)
                device = AdbDevice(config["emulator"])
                result = TaskRunner(config, self.config_path, log=self.log, cancel=self.cancel, device=device).run(
                    task_ids=set(tasks) if tasks else None, force=force)
                with self.lock:
                    self.result = result
                    if result.get("status") != "nothing_to_do":
                        self.last_result = result
            except Exception as exc:
                self.log(f"运行失败: {exc}")
                with self.lock:
                    self.result = {"status": "failed", "setup_error": str(exc)}
                    self.last_result = self.result
            finally:
                with self.lock:
                    self.running = False
                    self.last_screenshot = None
                    self.owned_devices = [item for item in self.owned_devices if item.owns_live_emulator()]
                    if device and device.owns_live_emulator():
                        self.owned_devices.append(device)
                    auto_close = (self.request_window_close is not None and close_after_run
                                  and not self.cancel.is_set() and not self.closing.is_set()
                                  and self.result is not None and self.result["status"] != "cancelled")
                    if auto_close:
                        self.closing.set()
                        # The runner already applied the user's emulator exit preference.
                        self.owned_devices.clear()
                if auto_close:
                    print(f"运行结束后关闭工具：{self.result['status']} {self.result.get('setup_error', '')}", flush=True)
                    self.request_window_close()

        with self.lock:
            if self.closing.is_set():
                self.running = False
                raise RuntimeError("助手正在关闭")
            self.worker = threading.Thread(target=worker, name="HKA task")
            self.worker.start()

    def begin_close(self):
        with self.lock:
            self.closing.set()
            self.cancel.set()

    def finish_close(self):
        self.begin_close()
        if self.worker:
            self.worker.join()
        for device in self.owned_devices:
            device.stop_emulator()



def _wait_for_window_close(control) -> None:
    if os.name == "nt":
        import _winapi
        import msvcrt

        try:
            handle = msvcrt.get_osfhandle(control.fileno())
        except (AttributeError, OSError, ValueError):
            handle = None
        if handle is not None and _winapi.GetFileType(handle) == _winapi.FILE_TYPE_PIPE:
            # A pending ReadFile on inherited stdin can stall OCR imports and child processes.
            # Peek first so the single reader only reads bytes already in the pipe.
            try:
                while True:
                    available, _ = _winapi.PeekNamedPipe(handle, 0)
                    if available:
                        data, _ = _winapi.ReadFile(handle, min(available, 4096))
                        if b"\n" in data:
                            return
                    else:
                        time.sleep(0.1)
            except BrokenPipeError:
                return  # The desktop host closed its end of the pipe.
    control.readline()


def serve(config_path: Path, port: int = 8765, *, fallback_port: bool = False,
          ready_path: Path | None = None, ready=None, control=None, open_browser=True,
          request_window_close=None) -> None:
    state = WebState(config_path, request_window_close)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: object, status: int = 200) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _bytes(self, payload: bytes, mime: str, *, screenshot_id: str | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            if screenshot_id is not None:
                self.send_header("X-Screenshot-Id", screenshot_id)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            try:
                if route in UI_FILES:
                    filename, mime = UI_FILES[route]
                    return self._bytes((UI_DIR / filename).read_bytes(), mime)
                if route == "/api/config":
                    return self._json(config_snapshot(config_path))
                if route == "/api/task-presets":
                    return self._json(task_presets())
                if route == "/api/plan":
                    runner = TaskRunner(load_config(config_path), config_path)
                    return self._json({"date": runner.today.isoformat(), "items": runner.preview()})
                if route == "/api/status":
                    with state.lock:
                        snapshot = {"running": state.running, "logs": list(state.logs),
                                    "result": state.result, "last_result": state.last_result}
                    return self._json(snapshot)
                if route == "/api/screenshot":
                    frame = state.capture()
                    return self._bytes(frame.png, "image/png", screenshot_id=frame.id)
                if route == "/api/templates":
                    config = load_config(config_path)
                    directory = config_path.parent / config.get("runtime", {}).get("template_dir", "templates")
                    return self._json(sorted(p.name for p in directory.glob("*.png")) if directory.is_dir() else [])
                self._json({"error": "未找到页面"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            try:
                if state.closing.is_set():
                    raise RuntimeError("助手正在关闭")
                origin = self.headers.get("Origin")
                if origin and origin != f"http://127.0.0.1:{self.server.server_port}" and origin != f"http://localhost:{self.server.server_port}":
                    return self._json({"error": "来源与本地助手不符"}, 403)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 2_000_000:
                    raise ValueError("请求太大")
                data = json.loads(self.rfile.read(length) or b"{}")
                if route == "/api/config":
                    with state.device_lock, state.lock:
                        if state.running:
                            raise RuntimeError("运行中不能修改配置")
                        revision = save_config_snapshot(config_path, data)
                    return self._json({"ok": True, "revision": revision})
                if route == "/api/doctor":
                    config = load_config(config_path)
                    device = AdbDevice(config["emulator"])
                    device.cancel = state.closing
                    device.connect()
                    return self._json({"serial": device.serial, "package": device.package, "installed": device.installed(), "size": device.size()})
                if route == '/api/test-email':
                    email = load_config(config_path).get('notifications', {}).get('email', {})
                    send_email(email, title='蒂亚拉小助手 测试邮件', body='邮件通知配置成功。装备仓库已满或任务失败时，将按通知设置发送提醒。')
                    return self._json({'ok': True})
                if route == "/api/reset-today":
                    with state.lock:
                        if state.running:
                            raise RuntimeError("任务运行中，请先等待流程结束")
                        runner = TaskRunner(load_config(config_path), config_path)
                        if data.get("date") != runner.today.isoformat():
                            raise ValueError("执行日期已变化，请刷新今日状态后再重置")
                        reset_count = runner.reset_today()
                    return self._json({"ok": True, "reset_count": reset_count})
                if route == "/api/run":
                    with state.device_lock:
                        state.start_run(data.get("tasks"), bool(data.get("force", False)))
                    return self._json({"ok": True})
                if route == "/api/stop":
                    state.cancel.set()
                    state.log("收到停止请求，正在中断当前任务")
                    return self._json({"ok": True})
                if route == "/api/tap":
                    state.tap(data.get("screenshot_id"), float(data["x"]), float(data["y"]))
                    return self._json({"ok": True})
                if route == "/api/template":
                    config = load_config(config_path)
                    directory = (config_path.parent / config.get("runtime", {}).get("template_dir", "templates")).resolve()
                    path = template_path(directory, data["name"])
                    frame = state.frame_for(data.get("screenshot_id"))
                    size = crop_template(frame.png, data["box"], path)
                    return self._json({"ok": True, "name": path.name, "size": size})
                self._json({"error": "未找到接口"}, 404)
            except StaleFrame as exc:
                self._json({"error": str(exc)}, 409)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        if not fallback_port or not (exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048):
            raise
        server = LocalServer(("127.0.0.1", 0), Handler)
        print(f"端口 {port} 已占用，已自动选择空闲端口")
    address = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        if ready_path is not None:
            ready_path.write_text(address, encoding="utf-8")
        print(f"配置界面: {address}")
        if ready:
            ready(address)
        if control is not None:
            def watch_window():
                try:
                    _wait_for_window_close(control)
                finally:
                    state.begin_close()
                    server.shutdown()
            threading.Thread(target=watch_window, name="HKA window", daemon=True).start()
        if open_browser and os.getenv("HKA_NO_BROWSER") != "1":
            webbrowser.open(address)
        server.serve_forever(poll_interval=0.1)
    finally:
        state.begin_close()
        server.server_close()
        state.finish_close()
        if ready_path is not None:
            ready_path.unlink(missing_ok=True)
