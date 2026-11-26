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
from .runner import NavigationError, SkipTask
from .vision import ImageFrame, find_image
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
        self._navigation_receipt_open = False

    def _check_cancel(self) -> None:
        if self.runner.cancel.is_set():
            from .runner import Cancelled
            raise Cancelled()

    def _tap(self, x: int, y: int, wait: float = 0.65) -> None:
        self._check_cancel()
        width, height = self.runner._size()
        self.runner.check_action_day()
        self.runner.last_decision_frame = None
        try:
            self.device.tap(min(width - 1, max(0, round(x * width / 1600))),
                            min(height - 1, max(0, round(y * height / 900))))
        finally:
            self.runner.finish_action_day()
        self.sleep(wait)

    def _swipe(self, x1: int, y1: int, x2: int, y2: int,
               duration_ms: int = 450, wait: float = 0.55) -> None:
        self._check_cancel()
        width, height = self.runner._size()
        self.runner.check_action_day()
        self.runner.last_decision_frame = None
        try:
            self.device.swipe(min(width - 1, max(0, round(x1 * width / 1600))),
                              min(height - 1, max(0, round(y1 * height / 900))),
                              min(width - 1, max(0, round(x2 * width / 1600))),
                              min(height - 1, max(0, round(y2 * height / 900))), duration_ms)
        finally:
            self.runner.finish_action_day()
        self.sleep(wait)

    def _screen(self, *, recover_transport: bool = True) -> bytes:
        self._check_cancel()
        self.runner.last_decision_frame = None
        png = self.device.screen() if recover_transport else self.device.screen(recover_transport=False)
        self.runner.last_decision_frame = png
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

    @staticmethod
    def _reward_receipt_close_point(hits: list[TextHit]) -> tuple[int, int] | None:
        rewards = find_text(hits, "已领取以下奖励")
        if rewards is None:
            return None
        if rewards.y < 150:
            return 1450, 740
        # Compact receipts place the title lower and label their own dismissal area.
        titles = [h for h in hits if re.sub(r"\s+", "", h.text) == "已领取以下奖励"]
        for title in titles:
            center_x = title.x + title.width // 2
            if not (600 <= center_x <= 1000 and 150 <= title.y <= 320):
                continue
            for hint in hits:
                x = hint.x + hint.width // 2
                if (450 <= hint.y <= 800 and abs(x - center_x) <= 100
                        and re.sub(r"\s+", "", hint.text).strip("()（）") == "点击空白处关闭"):
                    return x, hint.y + hint.height // 2
        return None

    def _dismiss_navigation_notice(self, hits: list[TextHit]) -> bool:
        close = self._reward_receipt_close_point(hits)
        if close is not None:
            if self._navigation_receipt_open:
                self.sleep(0.5)
            else:
                self._navigation_receipt_open = True
                self.log("关闭已领取奖励展示")
                self._tap(*close, 1.0)
            return True
        self._navigation_receipt_open = False
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

    @staticmethod
    def _navigation_card(frame: ImageFrame) -> tuple[str, int, int] | None:
        for image, region, label in (
                ("navigation_close.png", [0.65, 0.08, 0.95, 0.5], "活动宣传卡片"),
                ("navigation_announcement_close.png", [0.8, 0, 1, 0.25], "游戏公告")):
            close = find_image(frame, Path(__file__).parent / "assets", {
                "image": image, "region": region, "threshold": 0.94})
            if close:
                return label, close[0], close[1]
        return None

    def _close_navigation_card(self, card: tuple[str, int, int]) -> None:
        label, x, y = card
        self.log(f"关闭遮挡导航的{label}")
        self._tap(x, y, 1.0)

    def _dismiss_navigation_card(self, frame: ImageFrame | None = None) -> bool:
        card = self._navigation_card(frame or ImageFrame(self._screen()))
        if card is None:
            return False
        self._close_navigation_card(card)
        return True

    def _leave_maze_roster(self, hits: list[TextHit], frame: ImageFrame) -> bool:
        title = any(h.text.strip() == "空想迷域" and 110 <= h.x <= 180 and h.y < 60
                    for h in hits)
        ranks = [h for h in hits if "赋格等级" in h.text and 150 <= h.y < 800]
        if not title or len(ranks) < 2:
            return False
        back = find_image(frame, Path(__file__).parent / "assets", {
            "image": "maze_back.png", "region": [0.02, 0, 0.09, 0.1], "threshold": 0.94})
        if back is None:
            return False
        self.log("识别到空想迷域角色列表，点击返回并重新确认导航页面")
        self._tap(back[0], back[1], 1.5)
        return True

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
            frame = ImageFrame(screenshot)
            if (self._dismiss_combat_result(hits) or self._dismiss_navigation_notice(hits)
                    or self._dismiss_navigation_card(frame) or self._leave_maze_roster(hits, frame)):
                continue
            hit = next((hit for hit in hits if "首页" in hit.text and
                        750 <= hit.x <= 890 and 70 <= hit.y < 130), None)
            if hit:
                active = find_image(frame, Path(__file__).parent / "assets", {
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
            home = find_image(frame, Path(__file__).parent / "assets", {
                "image": "navigation_home.png", "region": [0.075, 0.015, 0.16, 0.13],
                "threshold": 0.94})
            if home:
                self.log("通过章节首页图标恢复顶部导航")
                self._tap(home[0], home[1], 1.5)
                continue
            self.sleep(0.6)
        raise NavigationError("未确认顶部首页入口或首页激活状态，已停止导航")

    def dismiss_login_popups(self, options: dict[str, Any]) -> dict[str, Any]:
        closed = 0
        absent = 0
        previous = None
        pending_change = None
        for _ in range(50):
            frame = ImageFrame(self._screen())
            card = self._navigation_card(frame)
            if card is None:
                absent += 1
                if absent == 2:
                    closed += int(previous is not None)
                    message = f"已关闭 {closed} 个登录弹窗" if closed else "已检查，未发现登录弹窗"
                    self.log(message)
                    return {"closed": closed, "message": message}
                self.sleep(0.4)
                continue
            absent = 0
            with Image.open(io.BytesIO(frame.png)) as image:
                body = image.crop((430, 240, 1170, 620)).convert("L").resize((32, 18))
            if previous is not None:
                last_label, last_body = previous
                if card[0] == last_label:
                    difference = ImageStat.Stat(ImageChops.difference(last_body, body)).mean[0]
                    if difference < 22:
                        pending_change = None
                        self.sleep(0.4)
                        continue
                    if pending_change is None or ImageStat.Stat(ImageChops.difference(pending_change, body)).mean[0] >= 8:
                        pending_change = body
                        self.sleep(0.4)
                        continue
                closed += 1
                if closed >= 7:
                    raise NavigationError("已关闭 7 个登录弹窗，仍有卡片遮挡")
            self._close_navigation_card(card)
            previous = card[0], body
            pending_change = None
        raise NavigationError(f"已确认关闭 {closed} 个登录弹窗，剩余卡片状态未确认")

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
                return re.sub(r"连登|连续登[录陆]", "连续登录", re.sub(r"\s+", "", title))
        return None

    def claim_login_rewards(self, options: dict[str, Any]) -> dict[str, Any]:
        claimed: list[str] = []
        pending = None
        home_frames = 0
        next_reward_page = None
        waiting = 0
        birthday_actions: set[str] = set()
        for _ in range(40):
            self._check_cancel()
            screenshot = self._screen()
            hits = self._hits(frame=screenshot)
            if self._dismiss_navigation_notice(hits):
                if pending and self._reward_receipt_close_point(hits) is not None:
                    claimed.append(pending)
                    self.log(f"已确认领取：{pending}")
                    pending = None
                home_frames = 0
                next_reward_page = None
                continue
            birthday_stage = self._birthday_reward_stage(hits)
            page = "角色生日奖励" if birthday_stage else self._login_reward_page(hits)
            if pending and page is not None and page != pending:
                if next_reward_page != page:
                    next_reward_page = page
                    home_frames = 0
                    self.sleep(0.6)
                    continue
                claimed.append(pending)
                self.log(f"已确认领取：{pending}")
                pending = None
                waiting = 0
            next_reward_page = None
            if page is not None:
                home_frames = 0
            if birthday_stage:
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
            top = [h for h in hits if 70 <= h.y < 130]
            announcement_tabs = {h.text.strip() for h in top if 120 <= h.x <= 1000}
            if len(announcement_tabs & {"活动公告", "祈愿公告", "系统公告"}) >= 2:
                # The bulletin can follow a claim. Only the subsequent stable
                # home or reward page confirms the pending login reward.
                home_frames = 0
                if not self._dismiss_navigation_card(ImageFrame(screenshot)):
                    self.sleep(0.6)
                continue
            home_tab = any(h.text.strip() == "首页" and 750 <= h.x <= 890 for h in top)
            home_visible = home_tab and find_text(top, "战斗") and find_image(
                screenshot, Path(__file__).parent / "assets",
                {"image": "home_active.png", "region": [0.47, 0, 0.56, 0.15], "threshold": 0.94})
            home_frames = home_frames + 1 if home_visible else 0
            if home_frames >= 3:
                if "wish" in birthday_actions and "claim" not in birthday_actions:
                    raise RuntimeError("已送上生日祝福，但未确认生日奖励领取弹窗")
                if pending:
                    claimed.append(pending)
                    self.log(f"已确认领取：{pending}")
                self.log(f"签到、连登与角色生日奖励处理完成，共领取 {len(claimed)} 份")
                return {"claimed": len(claimed), "pages": claimed}
            self.sleep(0.6)
        if pending:
            raise RuntimeError(f"已提交{pending}领取，但未确认领取结果")
        raise RuntimeError("每日登录奖励处理超过检查上限，未确认返回首页")

    @staticmethod
    def _mailbox_page(hits: list[TextHit]) -> bool:
        header = [h for h in hits if 1020 <= h.x <= 1560 and 125 <= h.y <= 225]
        return find_text(header, "体力领取") is not None and find_text(header, "一键领取") is not None

    @staticmethod
    def _mail_toolbar_visible(hits: list[TextHit]) -> bool:
        # The receipt dims the toolbar, and the OCR may drop the leading 一.
        return (any(h.text.strip() == "体力领取" and 1020 <= h.x <= 1240 and 125 <= h.y <= 225
                        for h in hits)
                and any(h.text.strip() in {"一键领取", "键领取"} and 1320 <= h.x <= 1560
                        and 125 <= h.y <= 225 for h in hits))

    @classmethod
    def _mail_claim_receipt(cls, hits: list[TextHit]) -> bool:
        return cls._mail_toolbar_visible(hits) and any(
            h.text.strip() == "领取成功" and 690 <= h.x <= 790 and 240 <= h.y <= 300 for h in hits)

    @classmethod
    def _empty_mail_notice(cls, hits: list[TextHit]) -> bool:
        return (cls._mail_toolbar_visible(hits)
                and any(h.text.strip() == "提示" and 700 <= h.x <= 810 and 250 <= h.y <= 330 for h in hits)
                and any(h.text.strip() == "暂时没有消息" and 495 <= h.x <= 890 and 345 <= h.y <= 435 for h in hits))

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
        empty_notice_closed = False
        stable = 0
        for _ in range(12):
            hits = self._hits()
            if self._empty_mail_notice(hits):
                stable = 0
                confirms = [h for h in hits if h.text.strip() == "确定" and 680 <= h.x <= 870 and 490 <= h.y <= 610]
                if len(confirms) == 1 and not empty_notice_closed:
                    confirm = confirms[0]
                    self._tap(confirm.x + confirm.width // 2, confirm.y + confirm.height // 2, 0.6)
                    empty_notice_closed = True
                else:
                    self.sleep(0.5)
                continue
            if self._mail_claim_receipt(hits):
                self.log("关闭邮箱领取成功弹窗")
                self._tap(1510, 740, 1.0)
                claimed = True
                stable = 0
                continue
            if self._dismiss_navigation_notice(hits):
                claimed = claimed or self._reward_receipt_close_point(hits) is not None
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
                if empty_notice_closed or any(find_text(hits, text) for text in ("暂无邮件", "没有邮件", "没有可领取附件")):
                    raise SkipTask("邮箱没有可领取附件")
                raise SkipTask("邮箱已稳定返回，未见本次附件领取回执")
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
        if not resuming and not self._guild_reward_dialog(hits):
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
        body = "".join(h.text for h in dialog if h.text.strip() not in {"提示", "确认", "确定", "取消"})
        body = re.sub(r"[\s？?。！!]", "", body)
        if not re.fullmatch(r"(?:是否|确定|确认)?(?:使用|进行|消耗)?(?:(?:一|1)次)?免费刷新(?:一次|商店|商品|共鸣屋)?(?:吗|么)?", body):
            raise RuntimeError("未确认本次为免费刷新，未提交")
        buttons = [item for item in dialog if item.text.strip() == "确定" and item.y > 500]
        if len(buttons) != 1:
            raise RuntimeError("未找到唯一的免费刷新确认按钮")
        hit = buttons[0]
        self._tap(hit.x + hit.width // 2, hit.y + hit.height // 2, 1.0)
        after = integer_after(self._hits((1010, 145, 1410, 235)), "剩余次数")
        if after != remaining - 1:
            raise RuntimeError("刷新后剩余次数未准确减少一次，无法确认刷新成功")

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
        if not price_labels and {"零时馈礼", "是否购买零时馈礼", "购买"} <= set(words):
            cancel = [hit for hit, word in zip(dialog, words)
                      if word == "取消" and 850 < hit.x < 1040 and 530 < hit.y < 610]
            if len(cancel) == 1:
                button = cancel[0]
                self._tap(button.x + button.width // 2, button.y + button.height // 2, 0.9)
                for _ in range(3):
                    shop = self._hits()
                    item_visible = any(hit.text == "零时馈礼" and abs(hit.x - title.x) < 60
                                       and abs(hit.y - title.y) < 60 for hit in shop)
                    if (item_visible and find_text(shop, "吼美礼包")
                            and not find_text(shop, "是否购买") and not find_text(shop, "取消")):
                        raise SkipTask("零时馈礼确认框未读到最终价格，已取消购买")
                    self.sleep(0.4)
                raise RuntimeError("零时馈礼未提交购买，取消后未确认已返回商品列表")
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
            frame = ImageFrame(screenshot)
            if (self._dismiss_navigation_notice(hits) or self._dismiss_navigation_card(frame)
                    or self._leave_maze_roster(hits, frame)):
                continue
            reset = None
            for image in ("battle_reset.png", "battle_reset_compact.png"):
                reset = find_image(frame, Path(__file__).parent / "assets", {
                    "image": image, "region": [0.8, 0.16, 1, 0.28], "threshold": 0.94})
                if reset:
                    break
            if reset:
                self._tap(reset[0], reset[1], 0.8)
                continue
            home = find_image(frame, Path(__file__).parent / "assets", {
                "image": "home_active.png", "region": [0.47, 0, 0.56, 0.15], "threshold": 0.94})
            if home and home_retries < 3:
                battle = find_text(self._read_region(screenshot, (900, 0, 1020, 135), 0.75), "战斗")
                if battle:
                    home_retries += 1
                    self.log(f"仍停留首页，重新进入战斗页面（{home_retries}/3）")
                    self._tap(battle.x + battle.width // 2, battle.y + battle.height // 2, 1.5)
                    continue
            self.sleep(0.7)
        raise NavigationError("未确认战斗关卡选择页面，已停止以避免误入其他活动")

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
        self._open_quick_detail(stage)
        allowance = self._quick_allowance(stage)
        for _ in range(12):
            count = self._execute_quick(stage, allowance)
            self._open_quick_detail(stage)
            remaining = self._quick_allowance(stage, allow_zero=True)
            if remaining != allowance - count:
                raise RuntimeError("免费次数未按确认的战斗次数减少，已停止后续快捷战斗")
            operations += 1
            battles += count
            progress = {"operations": operations, "battles": battles, "remaining_free": remaining}
            self.runner._task_details["quick_battle"] = progress
            if stage == "使魔的爱" or remaining == 0:
                return progress
            allowance = remaining
        raise RuntimeError("虚轴之庭快捷战斗超过安全上限，已停止")

    def _execute_quick(self, stage: str, allowance: int) -> int:
        self._click("快捷战斗", (1040, 740, 1440, 870))
        expected_count = None
        missing_button_frames = 0
        while missing_button_frames < 4:
            dialog = self._hits((600, 300, 1010, 640))
            if not find_text(dialog, "快速战斗") and not find_text(dialog, "快捷战斗"):
                raise RuntimeError("无法核对快捷战斗确认框")
            counts = [int(hit.text) for hit in dialog if 465 <= hit.y <= 515 and
                      670 <= hit.x <= 810 and re.fullmatch(r"\d{1,2}", hit.text)]
            if len(counts) != 1 or not 1 <= counts[0] <= 3:
                raise RuntimeError("无法识别快捷战斗场数，未确认")
            count = counts[0]
            if expected_count is not None and count != expected_count:
                raise RuntimeError("快捷战斗场数调整未生效，未确认")
            if count > allowance:
                if count == 1:
                    raise RuntimeError("快捷战斗次数超出免费次数，未确认")
                expected_count = count - 1
                self._tap(641, 492)
                continue
            values = [int(hit.text) for hit in sorted(dialog, key=lambda hit: hit.x)
                      if 385 <= hit.y <= 455 and re.fullmatch(r"\d{1,4}", hit.text)]
            if len(values) != 2:
                raise RuntimeError("无法确认快捷战斗体力变化，未确认")
            expected_cost = count * 30 if stage == "使魔的爱" else 0
            if values[0] < expected_cost or values[0] - values[1] != expected_cost:
                raise RuntimeError("快捷战斗体力消耗与预期不符，未确认")
            buttons = [hit for hit in dialog if hit.text.strip() == "确定" and
                       600 <= hit.x < 800 and 535 <= hit.y < 640]
            if len(buttons) > 1:
                raise RuntimeError("快捷战斗确认按钮不唯一，未确认")
            if buttons:
                button = buttons[0]
                self._tap(button.x + button.width // 2, button.y + button.height // 2)
                break
            expected_count = count
            missing_button_frames += 1
            self.sleep(0.6)
        else:
            raise RuntimeError("未找到快捷战斗确认按钮，未确认")
        for _ in range(12):
            self.sleep(0.6)
            # Detect the settlement and its confirmation on the same full frame.
            # Cropping the buttons alone can make the OCR miss the label.
            hits = self._hits()
            confirm = self._verified_combat_result_button(hits)
            if confirm:
                self._tap(confirm.x + confirm.width // 2, confirm.y + confirm.height // 2)
                for _ in range(8):
                    current = self._hits()
                    if (not find_text(current, "战斗结算")
                            and (self._combat_map_visible(current) or find_text(current, "选择助战好友")
                                 or (find_text(current, "使魔的爱") and find_text(current, "虚轴之庭")))):
                        return count
                    self.sleep(0.5)
                raise RuntimeError("快捷结算已确认一次，但未确认已离开结算页")
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

        excluded = set()
        for start, end in ((350, 650), (740, 440)):
            previous = None
            for attempt in range(9):
                hits = self._hits((180, 160, 1550, 875))
                difficulty = [h for h in hits if re.search(r"难度指数\s*[:：]?\s*B\b", h.text, re.I)
                              and 190 <= h.y <= 700]
                timers = [h for h in hits if "剩余时间" in h.text and
                          any(int(value) > 0 for value in re.findall(r"\d+", h.text.split("剩余时间", 1)[1]))]
                signature = tuple(sorted((h.text, round(h.y / 30)) for h in hits if h.x >= 925
                                         and not any(word in h.text for word in ("剩余时间", "重置时间", "展示时间"))))
                reopened = False
                for tag in sorted(difficulty, key=card_priority):
                    key = (signature, round(tag.x / 30), round(tag.y / 30))
                    if key in excluded:
                        continue
                    same_column = lambda h: (h.x >= midpoint) == (tag.x >= midpoint)
                    if any("空想迷域" in h.text and same_column(h) and
                           -80 <= h.y - tag.y < 180 for h in hits):
                        continue
                    if any(same_column(timer) and abs(tag.x - timer.x) < 340 and
                           abs(tag.y - timer.y) < 65 for timer in timers):
                        self._tap(max(300, tag.x - 190), tag.y + 115, 1.0)
                        for _ in range(6):
                            frame = self._screen()
                            full = self._hits(frame=frame)
                            if self._picker_tabs_visible([h for h in full if h.x < 185 and 175 <= h.y < 490]):
                                self.sleep(0.6)
                                continue
                            if find_text(full, "空想迷域"):
                                excluded.add(key)
                                self._battle_picker()
                                self._click("活动", (35, 175, 185, 490))
                                reopened = True
                                break
                            labels = stage_labels(frame)
                            if any(re.fullmatch(r"(?:\d+-\d+|BONUS)", h.text.strip(), re.I)
                                   for h in labels):
                                return
                            self.sleep(0.6)
                        if reopened:
                            break
                        self._home()
                        raise RuntimeError("点击活动后未确认日常关卡地图，已返回首页")
                if reopened:
                    previous = None
                    continue
                if signature and signature == previous:
                    break
                previous = signature
                if attempt < 8:
                    self._swipe(1290, start, 1290, end, 650, 0.8)
        raise SkipTask("未找到难度 B 且仍有剩余时间的活动")

    def _stage(self, name: str) -> None:
        for _ in range(6):
            frame = self._screen()
            hits = stage_labels(frame)
            hit = next((h for h in hits if re.sub(r"\s+", "", h.text).upper() == name.upper()), None)
            if hit:
                # Stage name is printed immediately above the stage icon.
                self._tap(hit.x + hit.width // 2, hit.y + 110, 1.2)
                if self._stage_detail_visible(name):
                    return
                self.sleep(0.8)
                continue
            page = self._hits(frame=frame)
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
        return self._matches_stage_title(detail, name) and find_text(detail, "最高得分") is not None

    @staticmethod
    def _matches_stage_title(detail: list[TextHit], name: str) -> bool:
        titles = {stage for hit in detail for stage in
                  re.findall(r"(?<![A-Z0-9-])(?:\d+-\d+|BONUS)(?![A-Z0-9-])",
                             re.sub(r"\s+", "", hit.text.upper()))}
        return titles == {name.upper()}

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
                self.runner.check_action_day()
                self.runner.last_decision_frame = None
                try:
                    self.device.key(4)
                finally:
                    self.runner.finish_action_day()
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
        pending_frame = None
        while True:
            self._check_cancel()
            try:
                self._check_combat_foreground(serial)
                hits = pending_frame if pending_frame is not None else self._hits(recover_transport=False)
                pending_frame = None
                if self._blocking_game_dialog(hits):
                    raise NavigationError("战斗中出现暂停或连接提示，已停止输入")
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
                if not self._combat_hud_visible(hits):
                    hud_checks -= 1
                    if not hud_checks:
                        raise RuntimeError("未确认战斗画面，已停止战斗输入")
                    self.sleep(0.6)
                    continue
                if cycle == 0:
                    self.log("已确认战斗画面，开始当前战斗输入")
                hud_checks = 4
                if cycle >= max_cycles:
                    raise RuntimeError("体力本战斗超时，已停止后续刷取")
                self._check_combat_foreground(serial)
                cycle += 1
                sent = False
                for slot, weapon in weapons.items():
                    mode = weapon.get("mode", "off")
                    if mode == "off" or (mode == "once" and slot in once_sent):
                        continue
                    if sent:
                        pending_frame = self._interrupted_combat_frame(serial)
                        if pending_frame is not None:
                            break
                    if mode == "tap" or (mode == "once" and slot not in once_sent):
                        if mode == "once":
                            once_sent.add(slot)
                        self._tap(int(weapon["x"]), int(weapon["y"]), 0.1)
                        self.log(f"战斗循环 {cycle}/{max_cycles}：{slot} 点按已发送（{mode}）")
                    elif mode == "hold":
                        self._swipe(int(weapon["x"]), int(weapon["y"]), int(weapon["x"]), int(weapon["y"]), hold_ms, 0.1)
                        self.log(f"战斗循环 {cycle}/{max_cycles}：{slot} 长按 {hold_ms} 毫秒已发送")
                    sent = True
                if pending_frame is not None:
                    continue
                if combat_mode == "move_shoot":
                    if sent:
                        pending_frame = self._interrupted_combat_frame(serial)
                        if pending_frame is not None:
                            continue
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

    @staticmethod
    def _blocking_game_dialog(hits: list[TextHit]) -> bool:
        center = "".join(h.text for h in hits if 320 <= h.x <= 1250 and 140 <= h.y <= 785)
        return any(word in center for word in ("网络连接中断", "重新连接", "网络异常", "连接超时",
                                                "游戏暂停", "继续游戏", "退出战斗", "重新登录", "网络正在重连"))

    def _interrupted_combat_frame(self, serial: str) -> list[TextHit] | None:
        self._check_combat_foreground(serial)
        hits = self._hits(recover_transport=False)
        if (self._blocking_game_dialog(hits) or not self._combat_hud_visible(hits)
                or find_text(hits, "战斗结算") or find_text(hits, "失败")):
            return hits
        return None

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
        self._start_stage_from_detail(name, options, extra_bonus=extra_bonus)

    def _start_stage_from_detail(self, name: str, options: dict[str, Any], *, extra_bonus: bool = False) -> None:
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
        self.runner._task_details["bonus_double_stamina"] = {
            "runs": 1, "initial_double_stamina": balance, "max_runs": 1}
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
            self.runner._task_details["stamina_farm"] = {"runs": runs, "max_runs": limit,
                                                        "min_stamina": 50}
            self.log(f"第 {runs} 次 BONUS 已完成，继续检查普通体力")
        self.log(f"BONUS 已达到本次上限 {limit} 次")
        return {"runs": runs, "stop_reason": "max_runs_reached", "max_runs": limit,
                "min_stamina": 50}

    def _first_clear_stage(self, name: str, options: dict[str, Any]) -> None:
        self._stage(name)
        score = None
        for _ in range(5):
            detail = self._hits((745, 145, 1030, 265))
            if self._matches_stage_title(detail, name):
                score = integer_after(detail, "最高得分")
                if score is not None:
                    break
            self.sleep(0.5)
        if score is None:
            raise RuntimeError(f"无法识别 {name} 最高得分，未判断是否需首通")
        self.log(f"{name} 最高得分 {score}，{'开始首通' if score == 0 else '已通关'}")
        if score == 0:
            self._start_stage_from_detail(name, options)
        else:
            self._tap(70, 168, 1.2)

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

    def _scroll_expeditions(self, frame: bytes, *, towards_top: bool = False) -> bool:
        with Image.open(io.BytesIO(frame)) as image:
            before = image.convert("RGB").crop((1080, 270, 1500, 850))
        start, end = (300, 790) if towards_top else (770, 285)
        self._swipe(1210, start, 1210, end)
        with Image.open(io.BytesIO(self._screen())) as image:
            after = image.convert("RGB").crop((1080, 270, 1500, 850))
        return max(ImageStat.Stat(ImageChops.difference(before, after)).mean) >= 2

    @staticmethod
    def _expedition_count(hits: list[TextHit]) -> int | None:
        count = integer_after(hits, "已接取的探险数")
        return count if count is not None else integer_after(hits, "已接收的探险数")

    def _expedition_page(self) -> tuple[bytes, list[TextHit]]:
        frame = self._screen()
        page = self._hits(frame=frame)
        if self._blocking_game_dialog(page) or (
                self._expedition_count(page) is None and not find_text(page, "使魔探险将于")):
            raise NavigationError("未确认使魔探险列表，已停止滚动和派遣")
        return frame, page

    def expeditions(self, options: dict[str, Any]) -> dict[str, int]:
        self._tasks("使魔探险")
        for _ in range(4):
            hits = self._hits()
            if self._expedition_count(hits) is not None or find_text(hits, "使魔探险将于"):
                break
            self.sleep(0.6)
        else:
            raise RuntimeError("未确认已进入使魔探险页面")
        claimed = 0
        progress = {"claimed": 0, "dispatched": 0}
        self.runner._task_details["expeditions"] = progress
        stationary = 0
        for _ in range(60):
            frame, page = self._expedition_page()
            hits = self._read_region(frame, (1130, 250, 1480, 870), 0.75)
            reward = find_text(hits, "点击领取奖励")
            if reward:
                accepted = self._expedition_count(page)
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
                progress["claimed"] = claimed
                stationary = 0
                continue
            stationary = 0 if self._scroll_expeditions(frame) else stationary + 1
            if stationary >= 2:
                break
        else:
            raise RuntimeError("使魔探险领取扫描超过上限")
        self.log(f"使魔探险领取 {claimed} 份奖励")
        # Claiming scans down the whole list. Dispatch must begin at the top.
        for _ in range(60):
            frame, _ = self._expedition_page()
            if not self._scroll_expeditions(frame, towards_top=True):
                break
        else:
            raise RuntimeError("使魔探险列表未确认已返回顶部")
        dispatched = 0
        stationary = 0
        for _ in range(60):
            frame, page = self._expedition_page()
            hits = self._read_region(frame, (1120, 270, 1490, 875), 0.75)
            select = find_text(hits, "点击选择使魔")
            if select:
                accepted = self._expedition_count(page)
                if accepted is None:
                    raise RuntimeError("无法确认当前已接取的探险数，未派遣")
                self._tap(select.x + select.width // 2, select.y + select.height // 2)
                self._click("一键派遣", (860, 730, 1200, 865))
                success = self._text("成功率", (1200, 135, 1530, 285))
                if success is None:
                    raise RuntimeError("一键派遣后未显示成功率")
                self._click("出发", (1180, 730, 1510, 865))
                for _ in range(6):
                    if self._expedition_count(self._hits()) == accepted + 1:
                        break
                    self.sleep(0.5)
                else:
                    raise RuntimeError("已点击出发，未确认探险数增加，已停止后续派遣")
                dispatched += 1
                progress["dispatched"] = dispatched
                stationary = 0
                continue
            stationary = 0 if self._scroll_expeditions(frame) else stationary + 1
            if stationary >= 2:
                break
        else:
            raise RuntimeError("使魔探险派遣扫描超过上限")
        self.log(f"使魔探险派遣 {dispatched} 队")
        return progress

    def claim_presence(self, options: dict[str, Any]) -> None:
        self._tasks("存在感")
        self._claim_task_rewards("存在感")

    def claim_weekly(self, options: dict[str, Any]) -> None:
        self._tasks("每周任务")
        self._claim_task_rewards("每周")

    @staticmethod
    def _reward_page(hits: list[TextHit], label: str) -> bool:
        if find_text(hits, "已领取以下奖励") or find_text(hits, "点击空白处关闭"):
            return False
        period = "每日" if label == "存在感" else "每周"
        title = [h for h in hits if 280 <= h.x <= 570 and 180 <= h.y <= 320]
        current = [h for h in hits if 1080 <= h.x <= 1380 and 160 <= h.y < 240]
        return bool(find_text(title, period) and find_text(current, "当前存在感"))

    def _claim_task_rewards(self, label: str) -> None:
        for _ in range(5):
            hits = self._hits()
            buttons = [h for h in hits if h.text.strip() == "一键领取"
                       and 1150 <= h.x <= 1400 and 230 <= h.y <= 320]
            if self._reward_page(hits, label) and len(buttons) == 1:
                button = buttons[0]
                self._tap(button.x + button.width // 2, button.y + button.height // 2)
                break
            self.sleep(0.5)
        else:
            raise RuntimeError(f"未确认{label}奖励页及其领取按钮，未提交领取")
        claimed = False
        stable = 0
        for _ in range(10):
            hits = self._hits()
            close = self._reward_receipt_close_point(hits)
            if close is not None:
                stable = 0
                if not claimed:
                    claimed = True
                    self._tap(*close, 1.0)
                else:
                    self.sleep(0.5)
                continue
            stable = stable + 1 if self._reward_page(hits, label) else 0
            if claimed and stable >= 2:
                self.log(f"已领取{label}奖励")
                return
            self.sleep(0.5)
        if not claimed and stable >= 4:
            raise SkipTask(f"{label}奖励页保持稳定，未见本次领取回执")
        raise RuntimeError(f"{label}奖励领取后未能核实返回奖励页")


def run_game_flow(runner: Any, step: dict[str, Any]) -> dict[str, Any] | None:
    flows = GameFlows(runner)
    name = step["flow"]
    options = step.get("options", {})
    mapping = {
        "enter_game": flows.enter_game,
        "dismiss_login_popups": flows.dismiss_login_popups,
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
