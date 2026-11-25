"""Screen driven 崩坏学园2 flows for the 1600×900 landscape layout.

Every operation that can spend premium currency or stamina checks the visible
screen again immediately before its final confirmation. Unknown UI states stop.
"""
from __future__ import annotations

import io
import re
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from .ocr import TextHit, double_stamina, find_text, integer_after, read_stamina_hud, read_text, stage_labels, stamina
from .adb import AdbError, AdbTransportError
from .runner import SkipTask
from .vision import find_image
from .inventory import check_equipment_capacity
from .combat import weapon_controls


class LowBonusStamina(SkipTask):
    def __init__(self, balance: int):
        self.balance = balance
        super().__init__(f"普通体力 {balance} 不足 50，已停止 BONUS 刷取")


class GameFlows:
    def __init__(self, runner: Any):
        self.runner = runner
        self.device = runner.device
        self.log = runner.log
        self.sleep = runner.sleep

    def _check_cancel(self) -> None:
        if self.runner.cancel.is_set():
            from .runner import Cancelled
            raise Cancelled()

    def _tap(self, x: int, y: int, wait: float = 0.65) -> None:
        self._check_cancel()
        width, height = self.runner._size()
        self.device.tap(round(x * width / 1600), round(y * height / 900))
        self.sleep(wait)

    def _swipe(self, x1: int, y1: int, x2: int, y2: int,
               duration_ms: int = 450, wait: float = 0.55) -> None:
        self._check_cancel()
        width, height = self.runner._size()
        self.device.swipe(round(x1 * width / 1600), round(y1 * height / 900),
                          round(x2 * width / 1600), round(y2 * height / 900), duration_ms)
        self.sleep(wait)

    def _screen(self, *, recover_transport: bool = True) -> bytes:
        self._check_cancel()
        png = self.device.screen() if recover_transport else self.device.screen(recover_transport=False)
        with Image.open(io.BytesIO(png)) as screen:
            if abs(screen.width / screen.height - 1600 / 900) > 0.03:
                raise RuntimeError("游戏流程需要横屏 16:9 画面")
            if screen.size == (1600, 900):
                return png
            output = io.BytesIO()
            screen.convert("RGB").resize((1600, 900)).save(output, format="PNG", compress_level=1)
            return output.getvalue()

    def _hits(self, box: tuple[int, int, int, int] = (0, 0, 1600, 900),
              *, score: float = 0.75, recover_transport: bool = True,
              frame: bytes | None = None) -> list[TextHit]:
        self._check_cancel()
        png = frame if frame is not None else self._screen(recover_transport=recover_transport)
        full = read_text(png, min_score=score)
        check_equipment_capacity(full)
        if box == (0, 0, 1600, 900):
            return full
        return self._read_region(png, box, score)

    @staticmethod
    def _read_region(frame: bytes, box: tuple[int, int, int, int], score: float) -> list[TextHit]:
        left, top, right, bottom = box
        return read_text(frame, (left / 1600, top / 900, right / 1600, bottom / 900), min_score=score)

    def _text(self, needle: str, box: tuple[int, int, int, int] = (0, 0, 1600, 900)) -> TextHit | None:
        return find_text(self._hits(box), needle)

    def _click(self, needle: str, box: tuple[int, int, int, int] = (0, 0, 1600, 900),
               *, missing: str = "fail") -> bool:
        hit = None
        for _ in range(4):
            hit = self._text(needle, box)
            if hit is not None:
                break
            self.sleep(0.6)
        if hit is None:
            if missing == "skip":
                raise SkipTask(f"当前页面未找到“{needle}”")
            if missing == "continue":
                return False
            raise RuntimeError(f"当前页面未找到“{needle}”")
        self._tap(hit.x + hit.width // 2, hit.y + hit.height // 2)
        return True

    @staticmethod
    def _combat_result_confirmation(hits: list[TextHit]) -> TextHit | None:
        header = [hit for hit in hits if 680 <= hit.x <= 1000 and 120 <= hit.y <= 200]
        if not find_text(header, "战斗结算"):
            return None
        return next((hit for hit in hits if 700 <= hit.x <= 1170 and 700 <= hit.y <= 825
                     and hit.text.strip() == "确定"), None)

    def _verified_combat_result_button(self, hits: list[TextHit]) -> TextHit | None:
        confirm = self._combat_result_confirmation(hits)
        if not confirm:
            return None
        buttons = [hit for hit in hits if 700 <= hit.x <= 1170 and 730 <= hit.y <= 805]
        quick_result = any(680 <= hit.x <= 1120 and 210 <= hit.y <= 720
                           and re.search(r"第\s*\d+\s*次\s*快捷战斗结果", hit.text) for hit in hits)
        stats = [hit for hit in hits if 690 <= hit.x <= 1120 and 195 <= hit.y <= 310]
        scored_result = sum(find_text(stats, label) is not None
                            for label in ("挑战时间", "受击次数", "最大连击", "本次积分")) >= 3
        if quick_result or scored_result or find_text(buttons, "再次挑战"):
            return confirm
        return None

    def _dismiss_combat_result(self, hits: list[TextHit]) -> bool:
        confirm = self._verified_combat_result_button(hits)
        if confirm is None:
            return False
        self._tap(confirm.x + confirm.width // 2, confirm.y + confirm.height // 2, 1.0)
        self.log("已确认战斗结算，返回关卡地图")
        return True

    def _dismiss_navigation_notice(self, hits: list[TextHit]) -> bool:
        rewards = find_text(hits, "已领取以下奖励")
        if rewards and rewards.y < 150:
            self.log("关闭已领取奖励展示")
            self._tap(1450, 740, 1.0)
            return True
        dialog = [h for h in hits if 480 <= h.x <= 1100 and 240 <= h.y <= 650]
        words = "".join(h.text for h in dialog)
        # Only this informational notice is allowed to use its confirm button.
        # Never confirm an arbitrary dialog while recovering navigation.
        if all(word in words for word in ("提示", "未领取", "活动奖励", "背包")):
            confirm = next((h for h in dialog if h.text.strip() == "确定" and
                            650 <= h.x <= 900 and 535 <= h.y <= 640), None)
            if confirm:
                self.log("关闭活动奖励送达背包提示")
                self._tap(confirm.x + confirm.width // 2, confirm.y + confirm.height // 2, 1.0)
                return True
        return False

    def _dismiss_navigation_card(self, screenshot: bytes | None = None) -> bool:
        if screenshot is None:
            screenshot = self._screen()
        for image, region, label in (
                ("navigation_close.png", [0.65, 0.08, 0.95, 0.5], "活动宣传卡片"),
                ("navigation_announcement_close.png", [0.8, 0, 1, 0.25], "游戏公告")):
            close = find_image(screenshot, Path(__file__).parent / "assets", {
                "image": image, "region": region, "threshold": 0.94})
            if close:
                self.log(f"关闭遮挡导航的{label}")
                self._tap(close[0], close[1], 1.0)
                return True
        return False

    def _home(self) -> None:
        clicked_home = False
        for _ in range(8):
            screenshot = self._screen()
            hits = self._hits(frame=screenshot)
            modal = [h for h in hits if 300 <= h.x <= 1300 and 220 <= h.y <= 730]
            words = "".join(h.text for h in modal)
            if find_text(modal, "取消") and "体力" in words and any(
                    token in words for token in ("水晶", "钻石", "双倍体力", "将消耗")):
                self._cancel_exchange(modal, "返回首页前关闭体力兑换提示")
                continue
            if (self._dismiss_combat_result(hits) or self._dismiss_navigation_notice(hits)
                    or self._dismiss_navigation_card(screenshot)):
                continue
            hit = next((hit for hit in hits if "首页" in hit.text and
                        750 <= hit.x <= 890 and 70 <= hit.y < 130), None)
            if hit:
                active = find_image(screenshot, Path(__file__).parent / "assets", {
                    "image": "home_active.png", "region": [0.47, 0, 0.56, 0.15], "threshold": 0.94})
                # The task panel also highlights Home. A click must first
                # return to the character screen before callers open its menu.
                task_panel = len(self._task_tabs(hits)) >= 2
                if active and clicked_home and not task_panel:
                    return
                self._tap(hit.x + hit.width // 2, hit.y + hit.height // 2, 2.5)
                clicked_home = True
                continue
            # Full-screen chapters replace the top menu with a house icon.
            # It returns to the battle picker; then use its normal Home tab.
            home = find_image(screenshot, Path(__file__).parent / "assets", {
                "image": "navigation_home.png", "region": [0.075, 0.015, 0.16, 0.13],
                "threshold": 0.94})
            if home:
                self.log("通过章节首页图标恢复顶部导航")
                self._tap(home[0], home[1], 1.5)
                continue
            self.sleep(0.6)
        raise RuntimeError("未确认顶部首页入口或首页激活状态，已停止导航")

    def _shop(self) -> None:
        self._home()
        self._tap(1240, 68, 1.5)
        for _ in range(5):
            if self._text("礼包商店", (160, 145, 1440, 410)):
                return
            self.sleep(0.7)
        raise RuntimeError("未进入商店首页")

    def _startup_prompt(self, hits: list[TextHit]) -> str | None:
        words = "".join(re.sub(r"\s+", "", hit.text) for hit in hits)
        agreement = any(token in words for token in (
            "用户协议", "隐私政策", "隐私协议", "服务协议", "授权声明", "隐私保护提示", "权限申请"))
        update = any(token in words for token in (
            "资源更新", "版本更新", "游戏更新", "更新资源", "更新包", "新版本", "资源下载", "检测到新资源")) or (
            "资源包大小" in words and "下载" in words)
        if agreement:
            labels = {"同意", "同意并继续", "同意并进入", "同意并接受", "接受", "允许", "同意并授权"}
            kind = "agreement"
        elif update:
            labels = {"确定", "确认", "更新", "立即更新", "开始更新", "下载", "立即下载", "开始下载", "安装", "快给我下"}
            kind = "update"
        else:
            return None
        buttons = [hit for hit in hits if re.sub(r"\s+", "", hit.text) in labels]
        if len(buttons) == 1:
            button = buttons[0]
            self.log("检测到协议授权，自动同意" if agreement else "检测到游戏更新，自动确认更新")
            self._tap(button.x + button.width // 2, button.y + button.height // 2, 1)
            return kind
        if update and any(token in words for token in (
                "下载中", "正在下载", "更新中", "正在更新", "解压", "下载进度")):
            return "updating"
        return None

    def enter_game(self, options: dict[str, Any]) -> None:
        entered = False
        deadline = time.monotonic() + 90
        update_started = False
        while time.monotonic() < deadline:
            hits = self._hits()
            prompt = self._startup_prompt(hits)
            if prompt is not None:
                if prompt in {"update", "updating"} and not update_started:
                    self.log("等待游戏更新完成，最多等待 15 分钟")
                    deadline = time.monotonic() + 900
                    update_started = True
                self.sleep(1)
                continue
            if (any("首页" in hit.text and 750 <= hit.x <= 900 and hit.y < 135 for hit in hits)
                    or self._birthday_reward_stage(hits) or self._login_reward_page(hits)):
                self.log("已进入游戏")
                return
            if find_text(hits, "选择服务器"):
                previous = find_text(hits, "上次登录")
                if previous is None:
                    raise RuntimeError("没有上次登录服务器，请先在模拟器选择一次服务器")
                enter = next((hit for hit in hits if hit.text.strip() == "进入" and
                              hit.x > 1000 and 0 <= hit.y - previous.y < 150), None)
                if enter is None:
                    raise RuntimeError("未找到上次登录服务器的进入按钮")
                if not entered:
                    self._tap(enter.x + enter.width // 2, enter.y + enter.height // 2, 2)
                    entered = True
            elif find_text(hits, "验证码") or find_text(hits, "账号密码"):
                raise RuntimeError("游戏需要登录验证，请在模拟器完成后再运行")
            elif find_text(hits, "屏幕继续"):
                self._tap(800, 835, 2)
            self.sleep(1)
        raise RuntimeError("游戏更新或进入游戏超时，请检查模拟器中的更新、登录或加载提示")

    @staticmethod
    def _birthday_reward_stage(hits: list[TextHit]) -> str | None:
        message = "".join(re.sub(r"\s+", "", hit.text) for hit in hits
                          if 480 <= hit.x <= 1100 and 240 <= hit.y <= 350)
        if "有你的祝福" in message and "开心" in message:
            return "claim"
        titles = "".join(hit.text.upper() for hit in hits
                         if 750 <= hit.x <= 1550 and 180 <= hit.y <= 480)
        date = any(750 <= hit.x <= 1250 and 450 <= hit.y <= 600 and
                   re.fullmatch(r"\s*\d{1,2}\s*/\s*\d{1,2}\s*", hit.text) for hit in hits)
        if "HAPPY" in titles and "BIRTHDAY" in titles and date:
            return "wish"
        return None

    @staticmethod
    def _login_reward_page(hits: list[TextHit]) -> str | None:
        titles = [hit.text for hit in hits if 400 <= hit.x <= 1100 and 200 <= hit.y <= 340]
        days = [hit for hit in hits if 330 <= hit.x <= 1250 and 400 <= hit.y <= 550
                and re.fullmatch(r"第[一二三四五六七八九十\d]+天", hit.text.strip())]
        if len(days) < 2:
            return None
        for title in titles:
            if "每日签到" in title:
                return "每日签到"
            if any(token in title for token in ("连登", "连续登录", "连续登陆")):
                return title
        return None

    def claim_login_rewards(self, options: dict[str, Any]) -> dict[str, Any]:
        claimed: list[str] = []
        pending = None
        idle = 0
        waiting = 0
        birthday_actions: set[str] = set()
        for _ in range(40):
            self._check_cancel()
            hits = self._hits()
            if self._dismiss_navigation_notice(hits):
                if pending:
                    claimed.append(pending)
                    self.log(f"已确认领取：{pending}")
                    pending = None
                idle = 0
                continue
            birthday_stage = self._birthday_reward_stage(hits)
            page = "角色生日奖励" if birthday_stage else self._login_reward_page(hits)
            if pending and page != pending:
                claimed.append(pending)
                self.log(f"已确认领取：{pending}")
                pending = None
                waiting = 0
            if birthday_stage:
                idle = 0
                if birthday_stage in birthday_actions or page in claimed:
                    waiting += 1
                    if waiting >= 8:
                        raise RuntimeError("角色生日奖励操作后页面未变化，停止重复点击")
                    self.sleep(0.6)
                    continue
                label = "生日快乐" if birthday_stage == "wish" else "领取"
                buttons = [hit for hit in hits if hit.text.strip() == label and (
                    1000 <= hit.x <= 1350 and 600 <= hit.y <= 730 if birthday_stage == "wish"
                    else 650 <= hit.x <= 900 and 520 <= hit.y <= 620)]
                if len(buttons) != 1:
                    waiting += 1
                    if waiting >= 8:
                        raise RuntimeError(f"已识别角色生日奖励，但未确认{label}按钮")
                    self.sleep(0.6)
                    continue
                button = buttons[0]
                self.log("送上角色生日祝福" if birthday_stage == "wish" else "领取角色生日奖励")
                self._tap(button.x + button.width // 2, button.y + button.height // 2, 1)
                birthday_actions.add(birthday_stage)
                if birthday_stage == "claim":
                    pending = page
                waiting = 0
                continue
            if page is not None:
                idle = 0
                if pending or page in claimed:
                    waiting += 1
                    if waiting >= 8:
                        raise RuntimeError(f"{page}领取后页面未消失，停止重复点击")
                    self.sleep(0.6)
                    continue
                buttons = [hit for hit in hits if hit.text.strip() == "领取" and
                           1050 <= hit.x <= 1300 and 560 <= hit.y <= 700]
                if len(buttons) == 1:
                    button = buttons[0]
                    self.log(f"领取{page}奖励")
                    self._tap(button.x + button.width // 2, button.y + button.height // 2, 1)
                    pending = page
                    waiting = 0
                else:
                    waiting += 1
                    if waiting >= 8:
                        raise RuntimeError(f"已识别{page}，但未确认领取按钮")
                    self.sleep(0.6)
                continue
            idle += 1
            if idle >= 3:
                if "wish" in birthday_actions and "claim" not in birthday_actions:
                    raise RuntimeError("已送上生日祝福，但未确认生日奖励领取弹窗")
                self.log(f"签到、连登与角色生日奖励处理完成，共领取 {len(claimed)} 份")
                return {"claimed": len(claimed), "pages": claimed}
            self.sleep(0.6)
        raise RuntimeError("每日登录奖励处理超过检查上限")

    @staticmethod
    def _mailbox_page(hits: list[TextHit]) -> bool:
        header = [h for h in hits if 1020 <= h.x <= 1560 and 125 <= h.y <= 225]
        return find_text(header, "体力领取") is not None and find_text(header, "一键领取") is not None

    def claim_mail(self, options: dict[str, Any]) -> dict[str, Any]:
        self._home()
        for _ in range(8):
            hits = self._hits()
            if self._mailbox_page(hits):
                break
            messages = next((h for h in hits if h.text.strip() == "我的消息" and
                             650 <= h.x <= 1000 and 390 <= h.y <= 520), None)
            if messages:
                self._tap(messages.x + messages.width // 2, messages.y + messages.height // 2, 1)
                continue
            if self._dismiss_navigation_notice(hits) or self._dismiss_navigation_card():
                continue
            social = next((h for h in hits if h.text.strip() == "社交" and
                           1280 <= h.x <= 1450 and 40 <= h.y <= 140), None)
            if social:
                self._tap(social.x + social.width // 2, social.y + social.height // 2, 1.5)
            else:
                self.sleep(0.6)
        else:
            raise RuntimeError("未确认我的消息邮箱页面，未领取邮件")
        buttons = [h for h in hits if h.text.strip() == "一键领取" and
                   1320 <= h.x <= 1560 and 125 <= h.y <= 225]
        if len(buttons) != 1:
            raise RuntimeError("未确认邮箱的一键领取按钮，未领取邮件")
        button = buttons[0]
        self._tap(button.x + button.width // 2, button.y + button.height // 2, 0.2)
        claimed = False
        stable = 0
        for _ in range(12):
            hits = self._hits()
            if self._dismiss_navigation_notice(hits):
                claimed = True
                stable = 0
                continue
            # With no eligible attachments the game keeps displaying the inbox.
            # Confirm that page repeatedly, rather than interpreting any missing
            # receipt or an unrelated popup as an empty mailbox.
            stable = stable + 1 if self._mailbox_page(hits) else 0
            if stable >= 4:
                self._home()
                if claimed:
                    self.log("邮件附件领取完成")
                    return {"claimed": True}
                raise SkipTask("邮箱没有可领取附件")
            self.sleep(0.5)
        raise RuntimeError("邮件领取后未回到邮箱，未确认处理结果")

    @staticmethod
    def _guild_info_page(hits: list[TextHit]) -> bool:
        labels = [h for h in hits if 550 <= h.x <= 1350 and 250 <= h.y <= 500]
        return find_text(labels, "社团名") is not None and find_text(labels, "成员数") is not None

    @staticmethod
    def _guild_reward_dialog(hits: list[TextHit]) -> bool:
        title = "".join(h.text for h in hits if 500 <= h.x <= 1100 and 150 <= h.y <= 300)
        return "昨日社团" in title and "贡献奖励" in title

    @staticmethod
    def _guild_success_dialog(hits: list[TextHit]) -> bool:
        message = "".join(h.text for h in hits if 450 <= h.x <= 1100 and 300 <= h.y <= 480)
        return "奖励领取成功" in message and any(
            "社团宣言" in h.text or "活跃度" in h.text for h in hits)

    def claim_guild_daily(self, options: dict[str, Any]) -> dict[str, Any]:
        hits = self._hits()
        resuming = self._guild_success_dialog(hits)
        if not resuming:
            self._home()
            for _ in range(8):
                hits = self._hits()
                if self._guild_info_page(hits) or self._guild_reward_dialog(hits):
                    break
                if any(any(word in h.text for word in ("尚未加入社团", "未加入任何社团", "未加入社团"))
                       for h in hits if 200 <= h.x <= 1400 and 150 <= h.y <= 800):
                    self._home()
                    raise SkipTask("尚未加入社团，跳过社团每日奖励")
                entry = next((h for h in hits if h.text.strip() == "我的社团" and
                              700 <= h.x <= 1020 and 240 <= h.y <= 370), None)
                if entry:
                    self._tap(entry.x + entry.width // 2, entry.y + entry.height // 2, 1.5)
                    continue
                if self._dismiss_navigation_notice(hits) or self._dismiss_navigation_card():
                    continue
                social = next((h for h in hits if h.text.strip() == "社交" and
                               1280 <= h.x <= 1450 and 40 <= h.y <= 140), None)
                if social:
                    self._tap(social.x + social.width // 2, social.y + social.height // 2, 1.5)
                else:
                    self.sleep(0.6)
            else:
                raise RuntimeError("未确认我的社团信息页面，未领取社团奖励")

        opened = resuming
        submitted = resuming
        acknowledged = False
        for _ in range(16):
            self._check_cancel()
            if self._guild_reward_dialog(hits):
                if not submitted:
                    buttons = [h for h in hits if h.text.strip() == "领取" and
                               580 <= h.x <= 800 and 550 <= h.y <= 720]
                    if len(buttons) != 1:
                        raise RuntimeError("未确认社团贡献奖励弹窗的领取按钮")
                    button = buttons[0]
                    self.log("领取昨日社团贡献奖励")
                    self._tap(button.x + button.width // 2, button.y + button.height // 2, 0.6)
                    submitted = True
            elif submitted and self._guild_success_dialog(hits):
                if not acknowledged:
                    buttons = [h for h in hits if h.text.strip() == "确定" and
                               650 <= h.x <= 900 and 500 <= h.y <= 620]
                    if len(buttons) != 1:
                        raise RuntimeError("已收到社团奖励，但未确认成功提示的确定按钮")
                    button = buttons[0]
                    self._tap(button.x + button.width // 2, button.y + button.height // 2, 0.6)
                    acknowledged = True
            elif submitted and self._dismiss_navigation_notice(hits):
                pass
            elif self._guild_info_page(hits):
                status = [h for h in hits if 1100 <= h.x <= 1350 and 500 <= h.y <= 625]
                states = {h.text.strip() for h in status} & {"可领", "已领", "已领取", "不可领", "明日再来"}
                if len(states) == 1 and "可领" not in states:
                    label = next(iter(states))
                    if not submitted:
                        self._home()
                        raise SkipTask(f"社团每日奖励当前状态为「{label}」，跳过领取")
                    if acknowledged or label in {"已领", "已领取"}:
                        evidence = "社团弹窗显示「奖励领取成功」" if acknowledged else f"社团页显示「{label}」"
                        self.log(f"已确认昨日社团贡献奖励领取成功：{evidence}")
                        self._home()
                        return {"claimed": True, "verification": evidence}
                buttons = [h for h in status if h.text.strip() == "可领"]
                if states == {"可领"} and len(buttons) == 1 and not opened and not submitted:
                    button = buttons[0]
                    self._tap(button.x + button.width // 2, button.y + button.height // 2, 0.6)
                    opened = True
            self.sleep(0.5)
            hits = self._hits()
        if submitted:
            raise RuntimeError("已提交社团奖励领取，但未能核实领取结果，请查看失败截图")
        raise RuntimeError("未确认社团奖励可领取，未提交领取")

    def refresh_resonance(self, options: dict[str, Any]) -> None:
        self._shop()
        self._click("崩坏屋", (1000, 175, 1450, 410))
        self._click("共鸣屋", (0, 240, 225, 520))
        hits = self._hits((1010, 145, 1410, 235))
        remaining = integer_after(hits, "剩余次数")
        if remaining is None or remaining <= 0:
            raise SkipTask("共鸣屋免费刷新次数为零或无法识别，已跳过")
        self.log(f"共鸣屋剩余刷新次数 {remaining}")
        self._tap(1360, 190)
        dialog = self._hits((450, 220, 1150, 670))
        words = "".join(hit.text for hit in dialog)
        if not ("免费刷新" in words and "确定" in words):
            raise RuntimeError("无法核对刷新确认框，未确认")
        if re.search(r"(?:消耗|花费|水晶|共鸣)[^。]{0,12}\d", words):
            raise RuntimeError("刷新确认框疑似提示付费，未确认")
        hit = next((item for item in dialog if item.text.strip() == "确定" and item.y > 500), None)
        if hit is None:
            raise RuntimeError("未找到免费刷新确认按钮")
        self._tap(hit.x + hit.width // 2, hit.y + hit.height // 2, 1.0)
        after = integer_after(self._hits((1010, 145, 1410, 235)), "剩余次数")
        if after is None or after >= remaining:
            raise RuntimeError("刷新后剩余次数未减少，无法确认刷新成功")

    def free_gift(self, options: dict[str, Any]) -> None:
        self._shop()
        self._click("礼包商店", (420, 165, 860, 410))
        self._click("吼美礼包", (0, 245, 230, 550))
        card_hits: list[TextHit] = []
        for _ in range(8):
            card_hits = self._hits((230, 180, 1500, 820))
            title = find_text(card_hits, "零时馈礼")
            if title and title.y <= 380:
                break
            if title:
                # A title at the bottom belongs to a partly hidden card. Bring
                # its price and purchase button into view before checking it.
                self._swipe(900, 775, 900, 775 - min(475, title.y - 260))
                continue
            self._swipe(900, 775, 900, 300)
        title = find_text(card_hits, "零时馈礼")
        if title is None or title.y > 380:
            raise SkipTask("未找到零时馈礼")
        card_x = title.x
        card = [hit for hit in card_hits if abs(hit.x - card_x) < 250 and title.y <= hit.y < title.y + 440]
        price = next((hit for hit in card if re.fullmatch(r"[×xX*]\s*0", hit.text.strip())), None)
        buy = find_text(card, "购买")
        if price is None or buy is None:
            raise SkipTask("零时馈礼价格不是 0，或已售完")
        self.log("已确认零时馈礼价格为 0")
        self._tap(buy.x + buy.width // 2, buy.y + buy.height // 2)
        dialog = self._hits((500, 230, 1080, 630))
        if not find_text(dialog, "零时馈礼"):
            raise RuntimeError("购买确认框不是零时馈礼，未确认")
        # The card and the confirmation can show different prices. Require
        # an explicit free price on this frame and reject conflicting amounts.
        words = [re.sub(r"\s+", "", hit.text) for hit in dialog]
        zero_price = re.compile(
            r"(?:免费|免费购买|[×xX*]0|(?:价格|售价|消耗|花费|总价)[:：]?(?:水晶|钻石)?[×xX*]?0(?:水晶|钻石)?"
            r"|(?:购买|确认购买)[（(]?[×xX*]0[）)]?)")
        price_clue = re.compile(r"\d|付费|收费|支付|充值|免费|价格|售价|总价|消耗|花费|水晶|钻石|人民币|元|[￥¥]")
        price_labels = [word for word in words if price_clue.search(word)]
        if not price_labels or not all(zero_price.fullmatch(word) for word in price_labels):
            raise RuntimeError("无法确认零时馈礼最终价格为零，未提交购买")
        candidates = [hit for hit, word in zip(dialog, words) if hit.y > 490 and re.fullmatch(
            r"(?:购买|确认购买)(?:[（(]?[×xX*]0[）)]?)?|免费购买", word)]
        if len(candidates) != 1:
            raise RuntimeError("未确认唯一的零时馈礼购买按钮")
        hit = candidates[0]
        self._tap(hit.x + hit.width // 2, hit.y + hit.height // 2, 0.9)
        if not self._text("购买成功", (490, 220, 1110, 650)):
            raise RuntimeError("未看到零时馈礼购买成功提示")
        self._tap(800, 555)

    def _battle_picker(self) -> None:
        self._home()
        self._tap(955, 70, 1.5)
        home_retries = 0
        for _ in range(12):
            # The activity picker has three left-hand tabs. Its right-hand
            # artwork occupies the same coordinates as a stage map's reset
            # button, so a blind tap there may enter an unrelated activity.
            screenshot = self._screen()
            full = self._hits(frame=screenshot, score=0.6)
            tabs = [hit for hit in full if 35 <= hit.x < 185 and 175 <= hit.y < 490]
            if not self._picker_tabs_visible(tabs):
                tabs = self._read_region(screenshot, (35, 175, 185, 490), 0.6)
            if self._picker_tabs_visible(tabs):
                return
            hits = [hit for hit in full if hit.score >= 0.75]
            if self._dismiss_navigation_notice(hits) or self._dismiss_navigation_card(screenshot):
                continue
            reset = None
            for image in ("battle_reset.png", "battle_reset_compact.png"):
                reset = find_image(screenshot, Path(__file__).parent / "assets", {
                    "image": image, "region": [0.8, 0.16, 1, 0.28], "threshold": 0.94})
                if reset:
                    break
            if reset:
                self._tap(reset[0], reset[1], 0.8)
                continue
            home = find_image(screenshot, Path(__file__).parent / "assets", {
                "image": "home_active.png", "region": [0.47, 0, 0.56, 0.15], "threshold": 0.94})
            if home and home_retries < 3:
                battle = find_text(self._read_region(screenshot, (900, 0, 1020, 135), 0.75), "战斗")
                if battle:
                    home_retries += 1
                    self.log(f"仍停留首页，重新进入战斗页面（{home_retries}/3）")
                    self._tap(battle.x + battle.width // 2, battle.y + battle.height // 2, 1.5)
                    continue
            self.sleep(0.7)
        raise RuntimeError("未确认战斗关卡选择页面，已停止以避免误入其他活动")

    @staticmethod
    def _picker_tabs_visible(tabs: list[TextHit]) -> bool:
        return sum(find_text(tabs, label) is not None for label in ("主线", "活动", "挑战")) >= 2

    def _rift(self) -> None:
        self._battle_picker()
        self._click("活动", (35, 175, 185, 490))
        # The picker retains its scroll position. Search towards the top first,
        # then scan down with overlapping steps so an entry cannot be skipped.
        for start, end in ((350, 650), (740, 440)):
            previous = None
            for attempt in range(9):
                hits = self._hits((925, 180, 1535, 875), score=0.65)
                hit = find_text(hits, "多元裂缝")
                if hit:
                    if hit.y < 210:
                        self._swipe(1290, 350, 1290, 650, 650, 0.8)
                        continue
                    self._tap(hit.x + hit.width // 2, hit.y + hit.height // 2, 1.0)
                    for _ in range(6):
                        stages = self._hits((550, 150, 1535, 740), score=0.65)
                        if all(find_text(stages, name) is not None
                               for name in ("使魔的爱", "虚轴之庭")):
                            return
                        self.sleep(0.6)
                    raise RuntimeError("点击多元裂缝后未确认关卡地图，已停止导航")
                visible = [h for h in hits if not any(
                    label in h.text for label in ("重置时间", "剩余时间", "展示时间"))]
                unchanged = previous and visible and len(previous) == len(visible) and all(
                    a.text == b.text and abs(a.y - b.y) < 12
                    for a, b in zip(previous, visible))
                if unchanged or attempt == 8:
                    break
                previous = visible
                self._swipe(1290, start, 1290, end, 650, 0.8)
        raise RuntimeError("活动列表双向查找后仍未找到多元裂缝，请确认入口是否开放")

    def _open_quick_detail(self, stage: str) -> None:
        self._rift()
        if stage == "使魔的爱":
            hit = self._text(stage, (570, 160, 990, 740))
            if hit is None:
                raise RuntimeError("多元裂缝未找到使魔的爱入口")
            # The caption is above the clickable stage icon.
            self._tap(hit.x + hit.width // 2, hit.y + hit.height + 75, 1.2)
        else:
            self._click(stage, (570, 160, 1520, 740))
            for _ in range(6):
                hits = self._hits()
                header = [hit for hit in hits if 1290 <= hit.x <= 1580 and 150 <= hit.y <= 240]
                launch = [hit for hit in hits if 770 <= hit.x <= 965 and 575 <= hit.y <= 735]
                # Full-screen OCR keeps the title intact. The blue triangle
                # can be read as one label or as two stacked characters.
                has_launch = find_text(launch, "出击") or all(
                    any(hit.text.strip() == character for hit in launch) for character in ("出", "击")
                )
                if find_text(header, stage) and has_launch:
                    self._tap(850, 660)
                    return
                self.sleep(0.6)
            raise RuntimeError("未确认虚轴之庭地图，未点击出击")

    def quick_battle(self, options: dict[str, Any]) -> dict[str, Any]:
        stage = str(options.get("stage", "使魔的爱"))
        if stage not in {"使魔的爱", "虚轴之庭"}:
            raise ValueError("快捷战斗 stage 仅支持使魔的爱或虚轴之庭")
        operations = battles = 0
        expected_remaining = None
        for _ in range(12):
            self._open_quick_detail(stage)
            allowance = self._quick_allowance(stage, allow_zero=operations > 0)
            if expected_remaining is not None and allowance != expected_remaining:
                raise RuntimeError("免费次数未按确认的战斗次数减少，已停止后续快捷战斗")
            if allowance == 0:
                self.log(f"{stage}免费次数已用完，共确认 {operations} 次快捷战斗、完成 {battles} 场")
                return {"operations": operations, "battles": battles, "remaining_free": 0}
            count = self._execute_quick(stage, allowance)
            operations += 1
            battles += count
            expected_remaining = allowance - count
            if stage == "使魔的爱":
                return {"operations": operations, "battles": battles,
                        "remaining_free": expected_remaining}
        raise RuntimeError("虚轴之庭快捷战斗超过安全上限，已停止")

    def _execute_quick(self, stage: str, allowance: int) -> int:
        self._click("快捷战斗", (1040, 740, 1440, 870))
        previous_count = None
        for _ in range(3):
            dialog = self._hits((600, 300, 1010, 620))
            if not find_text(dialog, "快速战斗") and not find_text(dialog, "快捷战斗"):
                raise RuntimeError("无法核对快捷战斗确认框")
            counts = [int(hit.text) for hit in dialog if 465 <= hit.y <= 515 and
                      670 <= hit.x <= 810 and re.fullmatch(r"\d{1,2}", hit.text)]
            if len(counts) != 1 or not 1 <= counts[0] <= 3:
                raise RuntimeError("无法识别快捷战斗场数，未确认")
            if previous_count is not None and counts[0] != previous_count - 1:
                raise RuntimeError("快捷战斗场数调整未生效，未确认")
            if counts[0] <= allowance:
                break
            previous_count = counts[0]
            self._tap(641, 492)
        else:
            raise RuntimeError("快捷战斗次数超出免费次数，未确认")
        values = [int(hit.text) for hit in sorted(dialog, key=lambda hit: hit.x)
                  if 385 <= hit.y <= 455 and re.fullmatch(r"\d{1,4}", hit.text)]
        if len(values) != 2 or len(counts) != 1 or not 1 <= counts[0] <= allowance:
            raise RuntimeError("无法确认快捷战斗次数与体力变化，未确认")
        expected_cost = counts[0] * 30 if stage == "使魔的爱" else 0
        if values[0] < expected_cost or values[0] - values[1] != expected_cost:
            raise RuntimeError("快捷战斗体力消耗与预期不符，未确认")
        self._click("确定", (600, 535, 800, 640))
        for _ in range(12):
            self.sleep(0.6)
            # Detect the settlement and its confirmation on the same full frame.
            # Cropping the buttons alone can make the OCR miss the label.
            confirm = self._combat_result_confirmation(self._hits())
            if confirm:
                self._tap(confirm.x + confirm.width // 2, confirm.y + confirm.height // 2)
                return counts[0]
        raise RuntimeError("快捷战斗后未识别到结算")

    def _quick_allowance(self, stage: str, *, allow_zero: bool = False) -> int:
        label = "今日剩余次数" if stage == "使魔的爱" else "今日免费"
        for _ in range(8):
            hits = self._hits((735, 145, 1450, 850))
            if stage == "使魔的爱":
                detail = any(stage in hit.text and hit.y < 230 for hit in hits)
            else:
                # Axis details are titled with the rotating character's name,
                # not the map name. Require the detail controls and progress
                # labels; the dimmed map name behind the panel is unreliable.
                detail = all(find_text(hits, text) is not None for text in
                             ("进度", "已参与次数", "选择助战好友", "快捷战斗"))
            remaining = integer_after(hits, label) if detail else None
            if stage == "虚轴之庭" and remaining is None and self._axis_exhausted(hits):
                # Once the last free stage is complete, the game removes both
                # the free-count label and quick-battle button and shows a paid
                # 35-stamina replay instead. Never enter that replay.
                remaining = 0
            if remaining is not None:
                if remaining == 0 and not allow_zero:
                    raise SkipTask(f"{stage}{'今日次数' if stage == '使魔的爱' else '今日免费次数'}已用完")
                self.log(f"{stage}可用次数 {remaining}")
                return remaining
            self.sleep(0.6)
        raise RuntimeError(f"未确认{stage}详情页或无法识别{label}，未执行快捷战斗")

    @staticmethod
    def _axis_exhausted(hits: list[TextHit]) -> bool:
        participation = find_text(hits, "已参与次数")
        counts = re.fullmatch(r"已参与次数\s*[:：]?\s*(\d+)\s*/\s*(\d+)",
                              participation.text.strip()) if participation else None
        return (find_text(hits, "进度") is not None and
                find_text(hits, "选择助战好友") is not None and
                integer_after(hits, "消耗体力") == 35 and
                counts is not None and int(counts[1]) == int(counts[2]) > 0 and
                find_text(hits, "今日免费") is None and find_text(hits, "快捷战斗") is None)

    def _activity(self) -> None:
        self._battle_picker()
        self._click("活动", (35, 175, 185, 490))
        # The event artwork moves between releases; use the B difficulty tag and
        # its nearby remaining-time tag to identify a live activity card. Only
        # the right column scrolls, and it retains the previous task's position.
        midpoint = 800  # All OCR coordinates are normalized to 1600 x 900.

        def card_priority(tag: TextHit) -> tuple[int, int]:
            if tag.x < midpoint:
                return (0, 0)  # Stable sort preserves the existing order on the left.
            return (1, tag.y)

        for start, end in ((350, 650), (740, 440)):
            previous = None
            for attempt in range(9):
                hits = self._hits((180, 160, 1550, 875))
                difficulty = [h for h in hits if re.search(r"难度指数\s*[:：]?\s*B\b", h.text, re.I)
                              and 190 <= h.y <= 700]
                timers = [h for h in hits if "剩余时间" in h.text and
                          any(int(value) > 0 for value in re.findall(r"\d+", h.text.split("剩余时间", 1)[1]))]
                for tag in sorted(difficulty, key=card_priority):
                    same_column = lambda h: (h.x >= midpoint) == (tag.x >= midpoint)
                    if any("空想迷域" in h.text and same_column(h) and
                           -80 <= h.y - tag.y < 180 for h in hits):
                        continue
                    if any(same_column(timer) and abs(tag.x - timer.x) < 340 and
                           abs(tag.y - timer.y) < 65 for timer in timers):
                        self._tap(max(300, tag.x - 190), tag.y + 115, 1.0)
                        for _ in range(6):
                            if self._text("空想迷域"):
                                raise SkipTask("空想迷域不属于本次每日副本流程")
                            labels = stage_labels(self._screen())
                            if any(re.fullmatch(r"(?:\d+-\d+|BONUS)", h.text.strip(), re.I)
                                   for h in labels):
                                return
                            self.sleep(0.6)
                        self._home()
                        raise RuntimeError("点击活动后未确认日常关卡地图，已返回首页")
                signature = tuple((h.text, round(h.y / 30)) for h in hits if h.x >= 925)
                if signature and signature == previous:
                    break
                previous = signature
                if attempt < 8:
                    self._swipe(1290, start, 1290, end, 650, 0.8)
        raise SkipTask("未找到难度 B 且仍有剩余时间的活动")

    def _stage(self, name: str) -> None:
        for _ in range(6):
            hits = stage_labels(self._screen())
            hit = next((h for h in hits if re.sub(r"\s+", "", h.text).upper() == name.upper()), None)
            if hit:
                # Stage name is printed immediately above the stage icon.
                self._tap(hit.x + hit.width // 2, hit.y + 110, 1.2)
                if self._stage_detail_visible(name):
                    return
                self.sleep(0.8)
                continue
            page = self._hits()
            page_hit = next((h for h in page if 740 < h.x < 850 and h.y > 780 and
                             re.fullmatch(r"\d+\s*/\s*\d+", h.text)), None)
            if page_hit:
                current, total = map(int, re.findall(r"\d+", page_hit.text))
                stage_number = re.fullmatch(r"(\d+)-\d+", name)
                target_page = total if name == "BONUS" else int(stage_number.group(1)) if stage_number else 1
                if not 1 <= target_page <= total:
                    raise RuntimeError(f"关卡 {name} 所在页超出当前地图范围")
                if current < target_page:
                    self._tap(928, 828, 1.2)
                    continue
                if current > target_page:
                    self._tap(674, 828, 1.2)
                    continue
            self.sleep(0.6)
        raise RuntimeError(f"关卡地图未找到 {name}")

    def _stage_detail_visible(self, name: str) -> bool:
        detail = self._hits((735, 145, 1040, 265))
        return bool(find_text(detail, name) and find_text(detail, "最高得分"))

    def _stamina(self) -> int:
        return self._confirmed_stamina(doubled=False)

    def _confirmed_stamina(self, *, doubled: bool) -> int:
        previous = None
        ordinary_only = doubled
        parse = double_stamina if doubled else stamina
        label = "双倍体力" if doubled else "普通体力"
        for _ in range(20):
            hits = read_stamina_hud(self._screen())
            value = parse(hits)
            # A crossfade can briefly leave only "70" of "270" readable.
            # Missing labels or the other resource interrupt confirmation.
            if value is not None and value == previous:
                raw = " / ".join(h.text for h in hits)
                self.log(f"{label}复核：{value}（连续两帧一致；OCR：{raw}）")
                return value
            previous = value
            ordinary_only = ordinary_only and stamina(hits) is not None
            self.sleep(0.4)
        if ordinary_only:
            raise SkipTask("顶部持续只显示普通体力，已跳过额外 BONUS")
        raise RuntimeError(f"无法连续确认{label}，未使用本次识别结果（顶部可能正在切换显示）")

    def _exchange_snapshot(self) -> tuple[list[TextHit], bool]:
        frame = self._screen()
        hits = read_text(frame, (300 / 1600, 220 / 900, 1300 / 1600, 730 / 900))
        premium = bool(re.search(r"钻石|水晶|充值|人民币|￥|¥", "".join(h.text for h in hits)))
        premium = premium or find_image(frame, Path(__file__).parent / "assets", {
            "image": "premium_diamond.png", "region": [0.25, 0.25, 0.8, 0.75],
            "threshold": 0.92}) is not None
        return hits, premium

    def _cancel_exchange(self, hits: list[TextHit], reason: str) -> None:
        for _ in range(3):
            cancel = next((h for h in hits if h.text.strip() == "取消" and
                           800 <= h.x <= 1200 and 500 <= h.y <= 700), None)
            if cancel is not None:
                self._tap(cancel.x + cancel.width // 2, cancel.y + cancel.height // 2, 1.0)
            else:
                # Android Back closes an unknown dialog without approving a purchase.
                self._check_cancel()
                self.device.key(4)
                self.sleep(1.0)
            self.log(f"已取消体力兑换：{reason}")
            hits, premium = self._exchange_snapshot()
            if not premium and not any(h.text.strip() == "取消" for h in hits):
                return
            reason = "继续关闭后续体力兑换弹窗"
        raise RuntimeError("体力兑换弹窗未关闭，未确认任何购买")

    @staticmethod
    def _combat_hud_visible(hits: list[TextHit]) -> bool:
        hud = [hit for hit in hits if hit.x < 570 and hit.y < 55]
        if any(hit.x < 90 and re.fullmatch(r"HP\s*[:：]?\s*\d+\s*/\s*\d+", hit.text.strip(), re.I)
               for hit in hud):
            return True
        hp_label = any(hit.x < 90 and hit.text.strip().upper() == "HP" for hit in hud)
        hp_value = any(hit.x >= 80 and re.fullmatch(r"\d+\s*/\s*\d+", hit.text.strip()) for hit in hud)
        return hp_label and hp_value

    @staticmethod
    def _combat_map_visible(hits: list[TextHit]) -> bool:
        top = [hit for hit in hits if 70 <= hit.y < 130]
        footer = [hit for hit in hits if hit.y >= 775]
        page_number = any(740 <= hit.x < 850 and re.fullmatch(r"\d+\s*/\s*\d+", hit.text.strip())
                          for hit in footer)
        return bool((find_text(top, "首页") and find_text(top, "战斗")) or
                    (page_number and find_text(footer, "战况") and find_text(footer, "兑换")))

    def _check_combat_foreground(self, serial: str) -> None:
        if self.device.serial != serial:
            raise RuntimeError("ADB 设备地址已变化，已停止战斗输入")
        if self.device.foreground_package() != self.device.package:
            raise RuntimeError("游戏已离开前台或前台状态不明，已停止战斗输入，请重新启动游戏")

    def _combat(self, options: dict[str, Any]) -> None:
        combat = options.get("combat", {})
        weapons = weapon_controls(combat)
        max_cycles = int(combat.get("max_cycles", 24))
        movement_ms = int(combat.get("movement_ms", 6500))
        hold_ms = int(combat.get("hold_ms", 900))
        combat_mode = combat.get("mode", "move_shoot")
        self.log("战斗按键：" + "，".join(f"{slot}={weapon['mode']}" for slot, weapon in weapons.items()))
        serial = self.device.serial
        cycle = 0
        reconnects = 0
        hud_checks = 8
        once_sent: set[str] = set()
        settlement_seen = False
        confirmation_sent = False
        settlement_checks = 0
        map_frames = 0
        while True:
            self._check_cancel()
            try:
                self._check_combat_foreground(serial)
                hits = self._hits(recover_transport=False)
                if not settlement_seen and find_text(hits, "失败"):
                    raise RuntimeError("体力本战斗失败")
                showing_result = bool(find_text(hits, "战斗结算"))
                if settlement_seen or showing_result:
                    settlement_seen = True
                    settlement_checks += 1
                    if confirmation_sent:
                        result_button = any(700 <= hit.x <= 1170 and 700 <= hit.y <= 825 and
                                            hit.text.strip() in {"确定", "再次挑战"} for hit in hits)
                        if not showing_result and not result_button and self._combat_map_visible(hits):
                            map_frames += 1
                            if map_frames == 2:
                                self.log("已确认战斗结算，返回关卡地图")
                                return
                        else:
                            map_frames = 0
                    else:
                        confirm = self._verified_combat_result_button(hits)
                        if confirm:
                            self._check_combat_foreground(serial)
                            # A transport error can arrive after delivery. Do not tap again.
                            confirmation_sent = True
                            settlement_checks = 0
                            self._tap(confirm.x + confirm.width // 2, confirm.y + confirm.height // 2, 1.0)
                    if settlement_checks >= 8:
                        if confirmation_sent:
                            raise RuntimeError("结算确认已发送，但未确认返回关卡地图")
                        raise RuntimeError("已发现战斗结算，但未完整确认结算按钮")
                    self.sleep(0.6)
                    continue
                if hud_checks:
                    if not self._combat_hud_visible(hits):
                        hud_checks -= 1
                        if not hud_checks:
                            raise RuntimeError("未确认战斗画面，已停止战斗输入")
                        self.sleep(0.6)
                        continue
                    hud_checks = 0
                    self.log("已确认战斗画面，开始当前战斗输入")
                if cycle >= max_cycles:
                    raise RuntimeError("体力本战斗超时，已停止后续刷取")
                self._check_combat_foreground(serial)
                cycle += 1
                for slot, weapon in weapons.items():
                    mode = weapon.get("mode", "off")
                    if mode == "tap" or (mode == "once" and slot not in once_sent):
                        if mode == "once":
                            once_sent.add(slot)
                        self._tap(int(weapon["x"]), int(weapon["y"]), 0.1)
                        self.log(f"战斗循环 {cycle}/{max_cycles}：{slot} 点按已发送（{mode}）")
                    elif mode == "hold":
                        self._swipe(int(weapon["x"]), int(weapon["y"]), int(weapon["x"]), int(weapon["y"]), hold_ms, 0.1)
                        self.log(f"战斗循环 {cycle}/{max_cycles}：{slot} 长按 {hold_ms} 毫秒已发送")
                if combat_mode == "move_shoot":
                    self._swipe(210, 690, 345, 690, movement_ms, 0.5)
                else:
                    self.sleep(movement_ms / 1000)
            except AdbTransportError as exc:
                error = exc
                while reconnects < 2:
                    reconnects += 1
                    self.log(f"战斗中 ADB 连接中断，尝试重新连接（{reconnects}/2）：{error}")
                    self.sleep(1.5)
                    try:
                        self.device.connect(rediscover=False)
                    except AdbError as reconnect_error:
                        error = reconnect_error
                    else:
                        break
                else:
                    raise RuntimeError("战斗中 ADB 连接中断，已用完两次重连机会") from error
                hud_checks = 4
                map_frames = 0

    def _bonus_stamina(self) -> int:
        balance = self._stamina()
        if balance < 50:
            raise LowBonusStamina(balance)
        return balance

    def _bonus_cost(self) -> int:
        cost = integer_after(self._hits((760, 635, 1415, 730)), "消耗体力")
        if cost not in {50, 100}:
            raise RuntimeError("无法确认 BONUS 详情页消耗为 50 或 100，未开战")
        return cost

    def _prepare_bonus(self) -> None:
        balance = self._bonus_stamina()
        for _ in range(3):
            expected = 100 if balance >= 100 else 50
            if self._bonus_cost() != expected:
                self._tap(1375, 680)
            # Read the current HUD and displayed price from fresh frames even
            # when the ticket was already set correctly. Never retry a toggle
            # merely because the UI did not acknowledge it.
            balance = self._bonus_stamina()
            if self._bonus_cost() != expected:
                raise RuntimeError("双倍券开关与详情页消耗无法确认，未开战")
            if (balance >= 100) == (expected == 100):
                self.log(f"BONUS 开战核对：普通体力 {balance}，"
                         f"双倍券{'开启' if expected == 100 else '关闭'}，详情页消耗 {expected}")
                return
        raise RuntimeError("普通体力在用券阈值附近反复变化，未开战")

    def _run_stage(self, name: str, options: dict[str, Any], *, extra_bonus: bool = False) -> None:
        self._stage(name)
        # _stage returns only after confirming both the title and score label.
        if name == "BONUS":
            if extra_bonus and self._confirmed_stamina(doubled=True) < 50:
                raise SkipTask("当前双倍体力不足 50，已跳过额外 BONUS")
            self._prepare_bonus()
        else:
            detail_cost = integer_after(self._hits((760, 635, 1050, 730)), "消耗体力")
            if detail_cost is None or detail_cost < 0:
                raise RuntimeError(f"无法识别 {name} 消耗体力")
            if self._stamina() < detail_cost:
                raise SkipTask(f"体力不足以挑战 {name}")
        self._click("选择助战好友", (725, 745, 1090, 865))
        self._tap(530, 325)
        self._click("开战", (820, 765, 1240, 885))
        self.sleep(float(options.get("load_wait", 8)))
        self._combat(options)

    def bonus_double_stamina(self, options: dict[str, Any]) -> dict[str, Any]:
        self._home()
        self._bonus_stamina()
        balance = self._confirmed_stamina(doubled=True)
        if balance < 50:
            raise SkipTask(f"当前双倍体力 {balance} 不足 50，已跳过额外 BONUS")
        self._activity()
        self.log(f"当前双倍体力 {balance}，准备额外一次 BONUS")
        self._run_stage("BONUS", options, extra_bonus=True)
        try:
            final_stamina = self._stamina()
        except RuntimeError as exc:
            # The battle has already settled on the map. An optional balance
            # observation must not turn it back into a failed, retryable task.
            final_stamina = None
            self.log(f"额外 BONUS 已完成，结算后余额读取失败：{exc}")
        return {"runs": 1, "stop_reason": "daily_extra_completed", "initial_double_stamina": balance,
                "final_stamina": final_stamina, "max_runs": 1}

    def stamina_farm(self, options: dict[str, Any]) -> dict[str, Any]:
        self._activity()
        # First-clear detection uses the stage's displayed best score. A missing
        # value is ambiguous and stops rather than entering an unknown stage.
        if options.get("first_clear", "auto") in {True, "auto"}:
            self._bonus_stamina()
            for name in ("1-1", "1-2", "1-3", "2-1"):
                self._first_clear_stage(name, options)
            # The next stage can remain hidden until 2-1 is complete. Check the
            # map after clearing 2-1 rather than caching the initial stage list.
            for number in range(2, 9):
                name = f"2-{number}"
                if not self._optional_stage_visible(name):
                    break
                self._first_clear_stage(name, options)
            else:
                raise RuntimeError("第二页首通检查达到安全上限，未开始 BONUS")
        runs = 0
        limit = int(options.get("max_runs", 20))
        while limit == 0 or runs < limit:
            try:
                self._bonus_stamina()
                self._run_stage("BONUS", options)
            except LowBonusStamina as exc:
                if runs == 0:
                    raise
                self.log(f"BONUS 已完成 {runs} 次；{exc}")
                return {"runs": runs, "stop_reason": "stamina_below_minimum", "final_stamina": exc.balance,
                        "max_runs": limit, "min_stamina": 50}
            runs += 1
            self.log(f"第 {runs} 次 BONUS 已完成，继续检查普通体力")
        self.log(f"BONUS 已达到本次上限 {limit} 次")
        return {"runs": runs, "stop_reason": "max_runs_reached", "max_runs": limit,
                "min_stamina": 50}

    def _first_clear_stage(self, name: str, options: dict[str, Any]) -> None:
        self._stage(name)
        score = None
        for _ in range(5):
            detail = self._hits((745, 145, 1030, 265))
            if find_text(detail, name):
                score = integer_after(detail, "最高得分")
                if score is not None:
                    break
            self.sleep(0.5)
        if score is None:
            raise RuntimeError(f"无法识别 {name} 最高得分，未判断是否需首通")
        self._tap(70, 168, 1.2)
        self.log(f"{name} 最高得分 {score}，{'开始首通' if score == 0 else '已通关'}")
        if score == 0:
            self._run_stage(name, options)

    def _optional_stage_visible(self, name: str) -> bool:
        # New stage icons and their orange captions fade in separately after
        # settlement. Let the unlock animation finish before testing absence.
        self.sleep(2.0)
        missing_frames = 0
        for _ in range(5):
            frame = self._screen()
            labels = {re.sub(r"\s+", "", h.text).upper() for h in stage_labels(frame)}
            footer = read_text(frame, (630 / 1600, 780 / 900, 970 / 1600, 890 / 900), min_score=0.6)
            on_page_two = any(re.fullmatch(r"2\s*/\s*[2-6]", h.text.strip()) for h in footer)
            if on_page_two and any(label == "BONUS" or re.fullmatch(r"2-\d+", label) for label in labels):
                if name.upper() in labels:
                    self.log(f"第二页已出现 {name}，继续检查首通")
                    return True
                missing_frames += 1
                if missing_frames >= 2:
                    self.log(f"已重复检查第二页，当前未出现 {name}")
                    return False
            else:
                missing_frames = 0
            self.sleep(0.8)
        raise RuntimeError(f"未确认第二页地图，无法判断是否出现 {name}")

    @staticmethod
    def _task_tabs(hits: list[TextHit]) -> dict[str, TextHit]:
        sidebar = [hit for hit in hits if hit.x < 170 and 250 <= hit.y < 600]
        return {label: hit for label in ("存在感", "每周任务", "使魔探险")
                if (hit := find_text(sidebar, label)) is not None}

    def _tasks(self, label: str) -> None:
        tabs = self._task_tabs(self._hits())
        if len(tabs) < 2:
            self._home()
            self._tap(65, 205, 1.5)
            for _ in range(4):
                tabs = self._task_tabs(self._hits())
                if len(tabs) >= 2 and label in tabs:
                    break
                self.sleep(0.6)
        if len(tabs) < 2:
            raise RuntimeError("未进入首页任务页面")
        if (hit := tabs.get(label)) is None:
            raise RuntimeError(f"首页任务页面未识别到“{label}”页签")
        self._tap(hit.x + hit.width // 2, hit.y + hit.height // 2)

    def _scroll_expeditions(self) -> bool:
        with Image.open(io.BytesIO(self._screen())) as image:
            before = image.convert("RGB").crop((1080, 270, 1500, 850))
        self._swipe(1210, 770, 1210, 285)
        with Image.open(io.BytesIO(self._screen())) as image:
            after = image.convert("RGB").crop((1080, 270, 1500, 850))
        return max(ImageStat.Stat(ImageChops.difference(before, after)).mean) >= 2

    @staticmethod
    def _expedition_count(hits: list[TextHit]) -> int | None:
        count = integer_after(hits, "已接取的探险数")
        return count if count is not None else integer_after(hits, "已接收的探险数")

    def expeditions(self, options: dict[str, Any]) -> None:
        self._tasks("使魔探险")
        for _ in range(4):
            hits = self._hits()
            if self._expedition_count(hits) is not None or find_text(hits, "使魔探险将于"):
                break
            self.sleep(0.6)
        else:
            raise RuntimeError("未确认已进入使魔探险页面")
        claimed = 0
        stationary = 0
        for _ in range(60):
            hits = self._hits((1130, 250, 1480, 870))
            reward = find_text(hits, "点击领取奖励")
            if reward:
                accepted = self._expedition_count(self._hits())
                self._tap(reward.x + reward.width // 2, reward.y + reward.height // 2, 0.6)
                for _ in range(5):
                    after = self._hits()
                    if find_text(after, "获得奖励"):
                        self._tap(800, 740)
                        break
                    remaining = self._expedition_count(after)
                    if accepted is not None and remaining is not None and remaining < accepted:
                        break
                    self.sleep(0.4)
                else:
                    raise RuntimeError("未确认使魔探险奖励领取成功")
                claimed += 1
                stationary = 0
                continue
            stationary = 0 if self._scroll_expeditions() else stationary + 1
            if stationary >= 2:
                break
        else:
            raise RuntimeError("使魔探险领取扫描超过上限")
        self.log(f"使魔探险领取 {claimed} 份奖励")
        # Claiming scans down the whole list. Dispatch must begin at the top.
        for _ in range(6):
            self._swipe(1210, 300, 1210, 790)
        dispatched = 0
        stationary = 0
        for _ in range(60):
            hits = self._hits((1120, 270, 1490, 875))
            select = find_text(hits, "点击选择使魔")
            if select:
                self._tap(select.x + select.width // 2, select.y + select.height // 2)
                self._click("一键派遣", (860, 730, 1200, 865))
                success = self._text("成功率", (1200, 135, 1530, 285))
                if success is None:
                    raise RuntimeError("一键派遣后未显示成功率")
                self._click("出发", (1180, 730, 1510, 865))
                dispatched += 1
                stationary = 0
                continue
            stationary = 0 if self._scroll_expeditions() else stationary + 1
            if stationary >= 2:
                break
        else:
            raise RuntimeError("使魔探险派遣扫描超过上限")
        self.log(f"使魔探险派遣 {dispatched} 队")

    def claim_presence(self, options: dict[str, Any]) -> None:
        self._tasks("存在感")
        self._claim_task_rewards("存在感")

    def claim_weekly(self, options: dict[str, Any]) -> None:
        self._tasks("每周任务")
        # Tight crops cut through the decorative logo and can merge its two
        # lines with the adjacent stars. OCR the page, then restrict positions.
        header = [h for h in self._hits() if 280 <= h.x <= 570 and 180 <= h.y <= 320]
        if not (find_text(header, "每周") and find_text(header, "存在感")):
            raise RuntimeError("未确认每周任务奖励页面，未领取")
        self._claim_task_rewards("每周")

    def _claim_task_rewards(self, label: str) -> None:
        self._click("一键领取", (1150, 230, 1400, 320), missing="skip")
        claimed = False
        for _ in range(10):
            if self._text("已领取以下奖励"):
                claimed = True
                # The reward panel grows with its contents. Tap outside it and
                # allow the loading animation to finish before checking again.
                self._tap(1450, 740, 1.0)
                continue
            if claimed:
                self.log(f"已领取{label}奖励")
                return
            self.sleep(0.5)
        if claimed:
            raise RuntimeError(f"{label}奖励弹窗未关闭")
        raise SkipTask(f"{label}没有可领取奖励")


def run_game_flow(runner: Any, step: dict[str, Any]) -> dict[str, Any] | None:
    flows = GameFlows(runner)
    name = step["flow"]
    options = step.get("options", {})
    mapping = {
        "enter_game": flows.enter_game,
        "claim_login_rewards": flows.claim_login_rewards,
        "claim_mail": flows.claim_mail,
        "claim_guild_daily": flows.claim_guild_daily,
        "refresh_resonance": flows.refresh_resonance,
        "free_gift": flows.free_gift,
        "quick_battle": flows.quick_battle,
        "stamina_farm": flows.stamina_farm,
        "bonus_double_stamina": flows.bonus_double_stamina,
        "expeditions": flows.expeditions,
        "claim_presence": flows.claim_presence,
        "claim_weekly": flows.claim_weekly,
    }
    if name not in mapping:
        raise ValueError(f"未知游戏流程: {name}")
    return mapping[name](options)
