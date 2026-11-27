"""Expedition rows in the normalized 1600 x 900 list view."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from .ocr import TextHit


@dataclass(frozen=True)
class ExpeditionCard:
    name: str
    state: Literal["selectable", "running", "unknown"]
    duration_seconds: int | None = None
    remaining_seconds: int | None = None
    select_button: TextHit | None = None


def _timer(label: str, hits: list[TextHit]) -> int | None:
    times = []
    for hit in hits:
        text = re.sub(r"\s+", "", hit.text).replace("：", ":")
        match = re.fullmatch(label + r":?(\d{1,2}):([0-5]\d):([0-5]\d)", text)
        if match:
            hours, minutes, seconds = map(int, match.groups())
            times.append(3600 * hours + 60 * minutes + seconds)
    return times[0] if len(times) == 1 else None


def parse_cards(hits: list[TextHit]) -> list[ExpeditionCard]:
    # Titles occupy the middle of the left header bar. Rank letters and
    # feature/reward labels lie outside it. Coordinates always move with
    # the title: a clipped card cannot borrow the next card's status.
    titles = [h for h in hits if 360 <= h.x <= 585 and h.x + h.width <= 790
              and 252 <= h.y and h.y + h.height <= 872 and re.search(r"[\u3400-\u9fff]", h.text)]
    cards = []
    for title in sorted(titles, key=lambda h: h.y):
        if any(h is not title and abs(h.y - title.y) < 45 for h in titles):
            continue
        name = re.sub(r"\s+", "", title.text)
        header = [h for h in hits if 1000 <= h.x <= 1260 and abs(h.y - title.y) <= 24]
        body = [h for h in hits if 80 <= h.y - title.y <= 150 and h.y + h.height <= 872]
        duration = _timer("需要时间", header)
        remaining = _timer("剩余时间", [h for h in body if 640 <= h.x <= 890])
        buttons = [h for h in body if 1150 <= h.x <= 1320 and h.text.strip() == "点击选择使魔"]
        if duration and len(buttons) == 1 and remaining is None:
            cards.append(ExpeditionCard(name, "selectable", duration_seconds=duration, select_button=buttons[0]))
        elif remaining is not None and duration is None and not buttons:
            cards.append(ExpeditionCard(name, "running", remaining_seconds=remaining))
        else:
            cards.append(ExpeditionCard(name, "unknown", duration_seconds=duration))
    return cards


def selected_card(cards: list[ExpeditionCard], button: TextHit) -> ExpeditionCard | None:
    """Match a button from the focused right-column OCR to its full-frame row."""
    if button.text.strip() != "点击选择使魔":
        return None
    matches = [card for card in cards if card.select_button is not None
               and abs(card.select_button.x - button.x) <= 35
               and abs(card.select_button.y - button.y) <= 25]
    if len(matches) != 1 or sum(card.name == matches[0].name for card in cards) != 1:
        return None
    return matches[0]


def freshly_running(cards: list[ExpeditionCard], previous: ExpeditionCard,
                    elapsed_seconds: float) -> ExpeditionCard | None:
    """Confirm the selected mission now carries a countdown consistent with this dispatch."""
    if previous.state != "selectable" or not previous.duration_seconds or elapsed_seconds < 0:
        return None
    matches = [card for card in cards if card.name == previous.name]
    if len(matches) != 1:
        return None
    current = matches[0]
    if current.state == "running" and current.remaining_seconds is not None:
        age = previous.duration_seconds - current.remaining_seconds
        if 0 <= age <= elapsed_seconds + 3:
            return current
    return None
