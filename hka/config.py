from __future__ import annotations

import json
import copy
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .combat import upgrade_combat_controls
from .defaults import DEFAULT_CONFIG, DEFAULT_LAUNCH_STEPS


class ConfigError(ValueError):
    pass


ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
STEP_TYPES = {"launch_game", "wait_image", "tap_image", "tap", "swipe", "key", "sleep", "screenshot", "repeat", "if_image", "game_flow"}


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc
    normalize_config(data)
    return data


def task_frequency(task: dict[str, Any]) -> str:
    if "frequency" in task:
        return task["frequency"]
    legacy = task.get("schedule", {}).get("type")
    if legacy in ("every_run", "once_per_day"):
        return legacy
    if task.get("always_run") or task["id"] in {"expeditions", "presence", "stamina_farm_after_presence"}:
        return "every_run"
    return "once_per_day"


def normalize_config(data: dict[str, Any], *, migrate_task_list: bool = True) -> None:
    upgrade_legacy_mail(data)
    if migrate_task_list and isinstance(data, dict) and not data.get("guild_daily_migrated"):
        upgrade_guild_daily(data)
    validate_config(data)
    runtime = data.setdefault("runtime", {})
    runtime.setdefault("close_tool_after_run", False)
    if runtime.get("run_dir") == "runs":
        runtime["run_dir"] = "logs"
    data["guild_daily_migrated"] = True
    for task in data["tasks"]:
        if task["id"] == "launch" and task["steps"] == DEFAULT_LAUNCH_STEPS:
            task.setdefault("required", True)
        task["frequency"] = task_frequency(task)
        schedule = task.setdefault("schedule", {"type": "daily"})
        if schedule["type"] in {"every_run", "once_per_day"}:
            schedule["type"] = "daily"
        task.pop("always_run", None)
        if task["id"] == "stamina_farm_after_presence" and task["name"] == "领取奖励后再刷一次 BONUS":
            task["name"] = "领取奖励后刷 BONUS"
        if task["id"] == "bonus_double_stamina" and task["name"] == "双倍体力额外 BONUS（每日一次）":
            task["name"] = "双倍体力额外 BONUS"
        normalize_bonus_steps(task["steps"], unlimited=task["id"] == "stamina_farm_after_presence")
    upgrade_combat_controls(data)


def task_presets() -> list[dict[str, Any]]:
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    normalize_config(defaults)
    return defaults["tasks"]


def normalize_bonus_steps(steps: list[dict], *, unlimited: bool) -> None:
    for step in steps:
        if step["type"] == "game_flow" and step["flow"] in {"stamina_farm", "bonus_double_stamina"}:
            options = step.setdefault("options", {})
            options.pop("min_stamina", None)
            options.pop("use_double_ticket", None)
            if unlimited and step["flow"] == "stamina_farm":
                options["max_runs"] = 0
        for branch in ("steps", "then", "else"):
            if branch in step and step["type"] in {"repeat", "if_image"}:
                normalize_bonus_steps(step[branch], unlimited=unlimited)


def upgrade_legacy_mail(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return
    images = ["social.png", "mail.png", "mail_claim.png", "back.png", "home_nav.png"]
    for task in data["tasks"]:
        if not isinstance(task, dict) or task.get("id") != "mail":
            continue
        steps = task.get("steps")
        if (isinstance(steps, list) and len(steps) == len(images) and
                all(isinstance(step, dict) and step.get("type") == "tap_image" and
                    step.get("image") == image and not any(key in step for key in ("offset_x", "offset_y"))
                    for step, image in zip(steps, images))):
            task["steps"] = [{"type": "game_flow", "flow": "claim_mail"}]


def upgrade_guild_daily(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return
    tasks = data["tasks"]
    ids = {task.get("id") for task in tasks if isinstance(task, dict) and isinstance(task.get("id"), str)}
    # Upgrade the standard daily workflow, preserving custom workflows and an
    # existing guild task's enabled state, position and authored steps.
    if any(key.casefold() == "guild_daily" for key in ids) or not {"launch", "signin", "mail", "resonance_refresh"} <= ids:
        return
    guild = next(task for task in DEFAULT_CONFIG["tasks"] if task["id"] == "guild_daily")
    position = next(i for i, task in enumerate(tasks) if isinstance(task, dict) and task.get("id") == "mail")
    tasks.insert(position + 1, copy.deepcopy(guild))


def save_config(path: str | Path, data: dict[str, Any]) -> None:
    data = copy.deepcopy(data)
    # A save explicitly replaces the task list, including intentional deletions.
    normalize_config(data, migrate_task_list=False)
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_config(data: Any) -> None:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConfigError("配置必须是 version=1 的 JSON 对象")
    emulator = data.get("emulator")
    if not isinstance(emulator, dict):
        raise ConfigError("缺少 emulator 配置")
    for field in ("adb_path", "serial", "package"):
        if not isinstance(emulator.get(field), str) or not emulator[field].strip():
            raise ConfigError(f"emulator.{field} 必须是非空字符串")
    if not isinstance(emulator.get("arguments", []), list) or not all(isinstance(a, str) for a in emulator.get("arguments", [])):
        raise ConfigError("emulator.arguments 必须是字符串数组")
    launch_delay = emulator.get("launch_delay", 10)
    if not isinstance(launch_delay, (int, float)) or not 0 <= launch_delay <= 300:
        raise ConfigError("emulator.launch_delay 必须在 0 到 300 秒之间")
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("runtime 必须是对象")
    if not isinstance(runtime.get("close_tool_after_run", False), bool):
        raise ConfigError("runtime.close_tool_after_run 必须是布尔值")
    image_timeout = runtime.get("image_timeout", 12)
    if not isinstance(image_timeout, (int, float)) or not 0 <= image_timeout <= 300:
        raise ConfigError("runtime.image_timeout 必须在 0 到 300 秒之间")
    if runtime.get("scheduler_enabled", False):
        when = runtime.get("daily_at", "")
        if not isinstance(when, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", when):
            raise ConfigError("runtime.daily_at 必须是 HH:MM")
    reset = runtime.get("daily_reset_at", "00:00")
    if not isinstance(reset, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reset):
        raise ConfigError("runtime.daily_reset_at 必须是 HH:MM")
    for key, default, low, high in (("retry_attempts", 0, 0, 5), ("retry_interval_minutes", 15, 1, 180)):
        value = runtime.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ConfigError(f"runtime.{key} 必须在 {low} 到 {high} 之间")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise ConfigError("tasks 必须是数组")
    ids = {}
    for index, task in enumerate(tasks):
        location = f"tasks[{index}]"
        if not isinstance(task, dict):
            raise ConfigError(f"{location} 必须是对象")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not ID_RE.fullmatch(task_id):
            raise ConfigError(f"{location}.id 只能包含英文字母、数字、_、-")
        key = task_id.casefold()
        if key in ids:
            raise ConfigError(f"任务 ID 重复（不区分大小写）: {ids[key]} / {task_id}")
        ids[key] = task_id
        if not isinstance(task.get("name"), str) or not isinstance(task.get("enabled"), bool):
            raise ConfigError(f"{location} 需要 name 和 enabled")
        if "frequency" in task and task["frequency"] not in ("every_run", "once_per_day"):
            raise ConfigError(f"{location}.frequency 必须是 every_run 或 once_per_day")
        for flag in ("always_run", "required"):
            if flag in task and not isinstance(task[flag], bool):
                raise ConfigError(f"{location}.{flag} 必须是布尔值")
        validate_schedule(task.get("schedule", {"type": "daily"}), location)
        validate_steps(task.get("steps"), location + ".steps")


def validate_schedule(schedule: Any, location: str) -> None:
    if not isinstance(schedule, dict) or schedule.get("type") not in {"every_run", "daily", "once_per_day", "weekdays", "dates", "range"}:
        raise ConfigError(f"{location}.schedule.type 必须是 every_run/daily/once_per_day/weekdays/dates/range")
    kind = schedule["type"]
    if kind == "weekdays":
        days = schedule.get("days")
        if not isinstance(days, list) or not days or any(type(day) is not int or day < 0 or day > 6 for day in days):
            raise ConfigError(f"{location}.schedule.days 必须是 0-6 的数组，周一为 0")
    if kind == "dates":
        dates = schedule.get("dates")
        if not isinstance(dates, list) or not dates or not all(_valid_date(value) for value in dates):
            raise ConfigError(f"{location}.schedule.dates 必须是 YYYY-MM-DD 或 MM-DD 数组")
    if kind == "range":
        try:
            start = date.fromisoformat(schedule["start"])
            end = date.fromisoformat(schedule["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{location}.schedule 范围日期格式应为 YYYY-MM-DD") from exc
        if start > end:
            raise ConfigError(f"{location}.schedule.start 不能晚于 end")


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if re.fullmatch(r"\d{2}-\d{2}", value):
            date.fromisoformat("2000-" + value)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
        else:
            return False
        return True
    except ValueError:
        return False


def schedule_matches(schedule: dict[str, Any], today: date) -> bool:
    kind = schedule["type"]
    if kind in {"daily", "every_run", "once_per_day"}:
        return True
    if kind == "weekdays":
        return today.weekday() in schedule["days"]
    if kind == "dates":
        return today.isoformat() in schedule["dates"] or today.strftime("%m-%d") in schedule["dates"]
    return date.fromisoformat(schedule["start"]) <= today <= date.fromisoformat(schedule["end"])


def execution_day(runtime: dict[str, Any], now: datetime) -> date:
    reset = runtime.get("daily_reset_at", "00:00")
    return (now - timedelta(days=1)).date() if now.strftime("%H:%M") < reset else now.date()


def validate_steps(steps: Any, location: str) -> None:
    if not isinstance(steps, list):
        raise ConfigError(f"{location} 必须是数组")
    for index, step in enumerate(steps):
        where = f"{location}[{index}]"
        if not isinstance(step, dict) or step.get("type") not in STEP_TYPES:
            raise ConfigError(f"{where}.type 无效")
        kind = step["type"]
        if kind == "game_flow":
            if step.get("flow") not in {"enter_game", "claim_login_rewards", "claim_mail", "claim_guild_daily", "refresh_resonance", "free_gift", "quick_battle", "stamina_farm", "expeditions", "claim_presence", "claim_weekly", "bonus_double_stamina"}:
                raise ConfigError(f"{where}.flow 无效")
            options = step.get("options", {})
            if not isinstance(options, dict):
                raise ConfigError(f"{where}.options 必须是对象")
            if step["flow"] == "quick_battle" and options.get("stage", "使魔的爱") not in {"使魔的爱", "虚轴之庭"}:
                raise ConfigError(f"{where}.options.stage 无效")
            if step["flow"] in {"stamina_farm", "bonus_double_stamina"}:
                max_runs = options.get("max_runs", 20)
                minimum_runs = 0 if step["flow"] == "stamina_farm" else 1
                if type(max_runs) is not int or not minimum_runs <= max_runs <= 30:
                    raise ConfigError(f"{where}.options.max_runs 必须在 {minimum_runs} 到 30 之间")
                if options.get("first_clear", "auto") not in (True, False, "auto"):
                    raise ConfigError(f"{where}.options.first_clear 必须是 auto 或布尔值")
                combat = options.get("combat", {})
                if not isinstance(combat, dict):
                    raise ConfigError(f"{where}.options.combat 必须是对象")
                if combat.get("mode", "move_shoot") not in {"move_shoot", "buttons"}:
                    raise ConfigError(f"{where}.options.combat.mode 无效")
                for name, default, low, high in (("max_cycles", 24, 1, 120), ("movement_ms", 6500, 100, 20000), ("hold_ms", 900, 100, 20000)):
                    if type(combat.get(name, default)) is not int or not low <= combat.get(name, default) <= high:
                        raise ConfigError(f"{where}.options.combat.{name} 必须在 {low} 到 {high} 之间")
                skills = combat.get("skills", [])
                if not isinstance(skills, list) or len(skills) > 3:
                    raise ConfigError(f"{where}.options.combat.skills 最多包含三个按键")
                weapons = combat.get("weapons", {})
                if not isinstance(weapons, dict) or set(weapons) - {"A", "B", "C"}:
                    raise ConfigError(f"{where}.options.combat.weapons 只能包含装备槽 A/B/C")
                controls = skills + list(weapons.values()) + ([combat["shoot"]] if "shoot" in combat else [])
                for control in controls:
                    if not isinstance(control, dict) or control.get("mode", "off") not in {"off", "tap", "hold", "once"}:
                        raise ConfigError(f"{where} 战斗按键模式无效")
                    if any(type(control.get(axis)) is not int or not 0 <= control[axis] <= limit for axis, limit in (("x", 1600), ("y", 900))):
                        raise ConfigError(f"{where} 战斗按键坐标无效")
        for field in ("wait", "timeout"):
            if field in step and (not isinstance(step[field], (int, float)) or not 0 <= step[field] <= 3600):
                raise ConfigError(f"{where}.{field} 必须在 0 到 3600 秒之间")
        if kind in {"wait_image", "tap_image", "if_image"} and not isinstance(step.get("image"), str):
            raise ConfigError(f"{where}.image 必须是模板文件名")
        if kind in {"wait_image", "tap_image", "if_image"}:
            image = step["image"]
            if Path(image).name != image or Path(image).suffix.lower() != ".png":
                raise ConfigError(f"{where}.image 必须是 PNG 文件名")
            threshold = step.get("threshold", 0.88)
            if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
                raise ConfigError(f"{where}.threshold 必须在 0 到 1 之间")
            region = step.get("region", [0, 0, 1, 1])
            if not isinstance(region, list) or len(region) != 4 or any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in region) or region[0] >= region[2] or region[1] >= region[3]:
                raise ConfigError(f"{where}.region 无效")
            if step.get("on_missing", "fail") not in {"fail", "continue", "skip_task", "break_repeat"}:
                raise ConfigError(f"{where}.on_missing 无效")
        if kind in {"tap", "swipe"}:
            keys = ("x", "y") if kind == "tap" else ("x1", "y1", "x2", "y2")
            if any(not isinstance(step.get(key), (int, float)) or not 0 <= step[key] <= 1 for key in keys):
                raise ConfigError(f"{where} 坐标必须是 0 到 1 的比例值")
        if kind == "repeat":
            if type(step.get("times")) is not int or not 1 <= step["times"] <= 100:
                raise ConfigError(f"{where}.times 必须在 1 到 100 之间")
            validate_steps(step.get("steps"), where + ".steps")
        if kind == "if_image":
            validate_steps(step.get("then", []), where + ".then")
            validate_steps(step.get("else", []), where + ".else")
        if kind == "sleep" and (not isinstance(step.get("seconds"), (int, float)) or not 0 <= step["seconds"] <= 3600):
            raise ConfigError(f"{where}.seconds 必须在 0 到 3600 之间")
