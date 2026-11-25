from __future__ import annotations

import errno
import json
import os
import threading
from contextlib import contextmanager
from tempfile import TemporaryDirectory
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .adb import AdbDevice
from .cancellation import Cancelled
from .config import execution_day, schedule_matches, task_frequency
from .notify import send_notifications
from .inventory import EquipmentFull
from .vision import find_image, template_path, VisionError


class SkipTask(Exception):
    pass


class BreakRepeat(Exception):
    pass


class RunBusyError(RuntimeError):
    """Another process holds the execution lock for this installation."""


def load_latest_report(run_dir: Path) -> dict[str, Any] | None:
    for path in sorted(run_dir.glob("run_*.json"), reverse=True):
        try:
            report = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, RecursionError):
            continue
        if (isinstance(report, dict) and report.get("status") in ("success", "partial", "failed", "cancelled")
                and isinstance(report.get("tasks"), list) and isinstance(report.get("finished_at"), str)):
            return report
    return None


def _write_report(path: Path, result: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class TaskRunner:
    def __init__(self, config: dict[str, Any], config_path: Path, *, device: AdbDevice | None = None,
                 log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                 cancel: threading.Event | None = None):
        self.config = config
        self.config_path = config_path.resolve()
        self.base = self.config_path.parent
        self._device = device
        self.log = log
        self.cancel = cancel or threading.Event()
        self._sleep = sleep
        if isinstance(device, AdbDevice):
            device.cancel = self.cancel
        self.template_dir = (self.base / config.get("runtime", {}).get("template_dir", "templates")).resolve()
        self.run_dir = (self.base / config.get("runtime", {}).get("run_dir", "logs")).resolve()
        self.state_path = self.base / "state.json"
        self.today = execution_day(config.get("runtime", {}), datetime.now())
        self._screen_size: tuple[int, int] | None = None
        self._task_details: dict[str, Any] = {}

    @property
    def device(self):
        if self._device is None:
            self._device = AdbDevice(self.config["emulator"])
            self._device.cancel = self.cancel
        return self._device

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        self._check_cancel()
        if self._sleep is time.sleep:
            self.cancel.wait(max(0, seconds))
        else:
            self._sleep(seconds)
        self._check_cancel()

    def plan(self, *, task_ids: set[str] | None = None, force: bool = False) -> list[dict[str, Any]]:
        pending = {item["id"] for item in self.preview(task_ids=task_ids, force=force)
                   if item["status"] == "pending"}
        return [task for task in self.config["tasks"] if task["id"] in pending]

    def preview(self, *, task_ids: set[str] | None = None, force: bool = False) -> list[dict[str, str]]:
        state = self._load_state()
        done = set(state.get(self.today.isoformat(), []))
        items = []
        for task in self.config["tasks"]:
            schedule = task.get("schedule", {"type": "daily"})
            every_run = task_frequency(task) == "every_run"
            if task_ids and task["id"] not in task_ids:
                status, reason = "not_selected", "本次只运行指定任务"
            elif not task["enabled"] and not task_ids:
                status, reason = "disabled", "未启用"
            elif not force and not schedule_matches(schedule, self.today):
                status, reason = "outside_schedule", "今天不在执行日期内"
            elif not every_run and task["id"] in done:
                status, reason = "completed", "今天已完成，自动跳过"
            elif not every_run and self._daily_attempt(task["id"], self.today).exists():
                status, reason = "attempted", "今天已尝试，未确认完成；重置后可重试"
            else:
                status, reason = "pending", "每次运行" if every_run else "待执行"
            items.append({"id": task["id"], "name": task["name"], "status": status, "reason": reason})
        return items

    @contextmanager
    def exclusive_run(self):
        # The GUI, daemon, and standalone CLI may share an installation.
        # Closing this handle releases the OS lock even if the process exits.
        with (self.base / ".task-run.lock").open("a+b") as handle:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise RunBusyError("另一个流程正在运行或重置，请等待结束") from exc
                raise
            yield

    _exclusive_run = exclusive_run

    def run(self, *, task_ids: set[str] | None = None, force: bool = False) -> dict[str, Any]:
        with self.exclusive_run():
            result = self._run(task_ids=task_ids, force=force)
            if result["status"] == "nothing_to_do":
                return result
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            report_path = self.run_dir / f"run_{stamp}_{uuid4().hex[:12]}.json"
            _write_report(report_path, result)
        if result["status"] != "cancelled":
            self._notify(result)
            if result["notification_errors"]:
                try:
                    _write_report(report_path, result)
                except OSError as exc:
                    self.log(f"通知结果写入失败：{exc}")
        return result

    def _run(self, *, task_ids: set[str] | None, force: bool) -> dict[str, Any]:
        self.today = execution_day(self.config.get("runtime", {}), datetime.now())
        preview = self.preview(task_ids=task_ids, force=force)
        tasks = [task for task, item in zip(self.config["tasks"], preview) if item["status"] == "pending"]
        excluded = [item for item in preview if item["status"] != "pending"]
        result: dict[str, Any] = {"started_at": datetime.now().isoformat(timespec="seconds"), "tasks": [],
                                  "mode": "selected" if task_ids else "daily", "force": force,
                                  "not_run": excluded, "notification_errors": []}
        self.log("运行模式：指定任务测试" if task_ids and force else
                 "运行模式：指定任务" if task_ids else "运行模式：每日流程")
        if task_ids:
            for item in excluded:
                if item["id"] in task_ids:
                    self.log(f"{item['name']}: {item['reason']}")
        else:
            completed = [item["name"] for item in excluded if item["status"] == "completed"]
            if completed:
                self.log("今天已完成，自动跳过：" + "、".join(completed))
        self.log(f"本次待执行 {len(tasks)} 项；失败后{'停止' if self.config.get('runtime', {}).get('stop_on_error', False) else '继续后续任务'}")
        if not tasks:
            self.log("当前没有待执行任务")
            result["status"] = "nothing_to_do"
            result["finished_at"] = datetime.now().isoformat(timespec="seconds")
            return result
        self.run_dir.mkdir(parents=True, exist_ok=True)
        started = False
        try:
            try:
                self._check_cancel()
                self.device.connect()
            except Cancelled:
                raise
            except Exception:
                if not self.device.emulator_path:
                    raise
                self.log("ADB 尚未连接，正在启动模拟器")
                started = True
                self.device.start_emulator()
                self.device.wait_until_ready()
            if not self.device.installed():
                raise RuntimeError(f"游戏未安装: {self.device.package}")
            self._screen_size = self.device.size()
            self.log(f"已连接 {self.device.serial}，画面 {self._screen_size[0]}×{self._screen_size[1]}")
            for index, task in enumerate(tasks):
                self._check_cancel()
                self._task_details = {}
                try:
                    task_day = execution_day(self.config.get("runtime", {}), datetime.now())
                    schedule = task.get("schedule", {"type": "daily"})
                    if not force and not schedule_matches(schedule, task_day):
                        result["not_run"].append({"id": task["id"], "name": task["name"],
                                                  "status": "outside_schedule", "reason": "任务日已变化"})
                        continue
                    if task_frequency(task) == "once_per_day":
                        if not self._claim_daily_attempt(task["id"], task_day):
                            reason = "今天已执行，自动跳过"
                            self.log(f"{task['name']}: {reason}")
                            result["not_run"].append({"id": task["id"], "name": task["name"],
                                                      "status": "attempted", "reason": reason})
                            continue
                    self.log(f"开始: {task['name']}")
                    self._steps(task["steps"])
                    self._check_cancel()
                    status, message = "success", "完成"
                    self._mark_done(task["id"], task_day)
                except Cancelled:
                    result["cancelled"] = True
                    status, message = "cancelled", "执行中已停止，未确认完成"
                except SkipTask as exc:
                    status, message = "skipped", str(exc)
                except EquipmentFull as exc:
                    status, message = "failed", str(exc)
                    result['attention_required'] = {'code': 'equipment_full', 'message': message}
                    self.log('需要处理：' + message)
                    try:
                        result['attention_required']['screenshot'] = str(self._save_screenshot('equipment_full'))
                    except Exception as screenshot_error:
                        self.log(f'错误截图失败: {screenshot_error}')
                except Exception as exc:
                    status, message = "failed", str(exc)
                    try:
                        self._save_screenshot(task["id"] + "_error")
                    except Exception as screenshot_error:
                        self.log(f"错误截图失败: {screenshot_error}")
                result["tasks"].append({"id": task["id"], "name": task["name"], "status": status, "message": message})
                if self._task_details:
                    result["tasks"][-1]["details"] = dict(self._task_details)
                    bonus = self._task_details.get("stamina_farm")
                    if bonus and status == "success":
                        reason = (f"普通体力 {bonus['final_stamina']} 不足 {bonus['min_stamina']}" if
                                  bonus["stop_reason"] == "stamina_below_minimum" else "达到配置次数上限")
                        message = f"BONUS 完成 {bonus['runs']} 次；{reason}"
                        result["tasks"][-1]["message"] = message
                    extra = self._task_details.get("bonus_double_stamina")
                    quick = self._task_details.get("quick_battle")
                    if extra and status == "success":
                        message = "额外 BONUS 已完成 1 次"
                        if extra.get("double_spent"):
                            message += f"；兑换 {extra['double_spent']} 双倍体力，补充 {extra['restored_stamina']} 体力"
                        result["tasks"][-1]["message"] = message
                    elif quick and status == "success":
                        message = f"快捷战斗完成 {quick['battles']} 场（确认 {quick['operations']} 次）；剩余免费次数 {quick['remaining_free']}"
                        result["tasks"][-1]["message"] = message
                self.log(f"{task['name']}: {status} {message}")
                if status == "cancelled":
                    break
                if status == "failed" and (result.get('attention_required') or task.get("required", False) or self.config.get("runtime", {}).get("stop_on_error", False)):
                    reason = result['attention_required']['message'] if result.get('attention_required') else ("游戏启动或登录失败，无法继续" if task.get("required", False) else "前项失败，已按配置停止")
                    self.log(reason)
                    result["not_run"].extend({"id": item["id"], "name": item["name"],
                        "status": "stopped_after_error", "reason": reason}
                        for item in tasks[index + 1:])
                    break
                if status == "failed" and index + 1 < len(tasks):
                    self.log("当前任务失败，继续后续任务")
        except Cancelled:
            result["cancelled"] = True
        except Exception as exc:
            result["setup_error"] = str(exc)
            self.log(f"连接或启动失败: {exc}")
        finally:
            if started and (self.cancel.is_set() or self.config["emulator"].get("stop_after_run", False)):
                try:
                    self.device.stop_emulator()
                except Exception as exc:
                    result["cleanup_error"] = str(exc)
                    self.log(f"关闭模拟器失败：{exc}")
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        failed = bool(result.get("setup_error") or result.get("cleanup_error")) or any(item["status"] == "failed" for item in result["tasks"])
        result["status"] = "cancelled" if result.get("cancelled") else ("failed" if failed else "success")
        if result.get("cancelled"):
            recorded = {item["id"] for item in result["tasks"] + result["not_run"]}
            result["not_run"].extend({"id": task["id"], "name": task["name"],
                                      "status": "not_started", "reason": "运行已停止，本项尚未开始"}
                                     for task in tasks if task["id"] not in recorded)
            self.log("任务已停止")
        return result

    def _notify(self, result: dict[str, Any]) -> None:
        failed = result["status"] == "failed"
        lines = [f"运行时间: {result['started_at']}", f"结果: {result['status']}"]
        if result.get("setup_error"):
            lines.append(f"连接错误: {result['setup_error']}")
        if result.get("cleanup_error"):
            lines.append(f"关闭模拟器失败: {result['cleanup_error']}")
        lines += [f"{item['name']}: {item['status']} {item['message']}" for item in result["tasks"]]
        title = '装备仓库已满，需要处理' if result.get('attention_required') else "崩坏2任务执行" + ("失败" if failed else "完成")
        try:
            result["notification_errors"] = send_notifications(self.config, success=not failed, title=title,
                                                               body="\n".join(lines), cancel=self.cancel)
        except Exception as exc:
            result["notification_errors"] = [str(exc)]
        for error in result["notification_errors"]:
            self.log(f"通知发送失败: {error}")

    def _steps(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            if self.cancel.is_set():
                raise Cancelled()
            kind = step["type"]
            if kind == "launch_game":
                self.device.launch_game()
                self.sleep(float(step.get("wait", 15)))
            elif kind == "sleep":
                self.sleep(float(step["seconds"]))
            elif kind == "key":
                self.device.key(int(step.get("code", 4)))
            elif kind == "tap":
                width, height = self._size()
                self.device.tap(round(step["x"] * width), round(step["y"] * height))
                self.sleep(float(step.get("wait", 0.5)))
            elif kind == "swipe":
                width, height = self._size()
                self.device.swipe(round(step["x1"] * width), round(step["y1"] * height), round(step["x2"] * width), round(step["y2"] * height), int(step.get("duration_ms", 400)))
                self.sleep(float(step.get("wait", 0.5)))
            elif kind == "screenshot":
                self._save_screenshot(step.get("name", "manual"))
            elif kind == "game_flow":
                from .game_flows import run_game_flow
                details = run_game_flow(self, step)
                if details is not None:
                    self._task_details[step["flow"]] = details
            elif kind in {"wait_image", "tap_image", "if_image"}:
                found = self._wait_image(step)
                if kind == "if_image":
                    self._steps(step.get("then", []) if found else step.get("else", []))
                elif not found:
                    behavior = step.get("on_missing", "fail")
                    if behavior == "skip_task":
                        raise SkipTask(f"未找到 {step['image']}")
                    if behavior == "break_repeat":
                        raise BreakRepeat()
                    if behavior != "continue":
                        raise RuntimeError(f"未找到图像 {step['image']}")
                elif kind == "tap_image":
                    x, y, score = found
                    width, height = self._size()
                    x += round(float(step.get("offset_x", 0)) * width)
                    y += round(float(step.get("offset_y", 0)) * height)
                    self.log(f"找到 {step['image']}，匹配度 {score:.3f}")
                    self.device.tap(x, y)
                    self.sleep(float(step.get("wait", 0.5)))
            elif kind == "repeat":
                for _ in range(step["times"]):
                    try:
                        self._steps(step["steps"])
                    except BreakRepeat:
                        break

    def _wait_image(self, step: dict[str, Any]) -> tuple[int, int, float] | None:
        self._check_cancel()
        path = template_path(self.template_dir, step["image"])
        if not path.is_file():
            if step.get("on_missing", "fail") == "fail":
                raise VisionError(f"缺少截图模板: {path}")
            self.log(f"缺少可选模板 {path.name}，按步骤设置跳过")
            return None
        timeout = float(step.get("timeout", self.config.get("runtime", {}).get("image_timeout", 12)))
        deadline = time.monotonic() + timeout
        while True:
            if self.cancel.is_set():
                raise Cancelled()
            found = find_image(self.device.screen(), self.template_dir, step)
            if found or time.monotonic() >= deadline:
                return found
            self.sleep(min(0.6, max(0, deadline - time.monotonic())))

    def _size(self) -> tuple[int, int]:
        if self._screen_size is None:
            self._screen_size = self.device.size()
        return self._screen_size

    def _save_screenshot(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "_-") or "capture"
        path = self.run_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.device.screen())
        self.log(f"截图已保存: {path}")
        return path

    def _load_state(self) -> dict[str, list[str]]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"无法读取执行记录 {self.state_path}：{exc}") from exc
        if not isinstance(state, dict) or any(not isinstance(ids, list) or
                any(not isinstance(item, str) for item in ids) for ids in state.values()):
            raise RuntimeError(f"执行记录格式错误：{self.state_path}")
        return state

    def _daily_attempt(self, task_id: str, day: date) -> Path:
        return self.base / ".daily-attempts" / day.isoformat() / task_id

    def _claim_daily_attempt(self, task_id: str, day: date) -> bool:
        if task_id in self._load_state().get(day.isoformat(), []):
            return False
        marker = self._daily_attempt(task_id, day)
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            marker.touch(exist_ok=False)
        except FileExistsError:
            return False
        return True

    def reset_today(self) -> int:
        with self.exclusive_run():
            return self._reset_today()

    def _reset_today(self) -> int:
        today = self.today.isoformat()
        state = self._load_state()
        completed = set(state.pop(today, []))
        attempts = self.base / ".daily-attempts" / today
        if not attempts.is_dir():
            if completed:
                self._save_state(state)
            return len(completed)

        # Stage the attempt markers so a failed state write can restore them.
        with TemporaryDirectory(prefix=".reset-", dir=attempts.parent) as temporary:
            staged = Path(temporary) / today
            attempts.rename(staged)
            try:
                attempted = {marker.name for marker in staged.iterdir()}
                if completed:
                    self._save_state(state)
            except Exception:
                staged.rename(attempts)
                raise
        return len(completed | attempted)

    def _mark_done(self, task_id: str, day: date) -> None:
        state = self._load_state()
        today = day.isoformat()
        state.setdefault(today, [])
        if task_id in state[today]:
            return
        state[today].append(task_id)
        for day in list(state):
            if day < (date.fromordinal(self.today.toordinal() - 45)).isoformat():
                del state[day]
        self._save_state(state)

    def _save_state(self, state: dict[str, list[str]]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)
