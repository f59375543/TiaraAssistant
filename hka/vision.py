from __future__ import annotations

import io
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

from PIL import Image


class VisionError(RuntimeError):
    pass


_SOLID_TEMPLATE_ERROR = "模板为纯色，无法定位。请重新裁剪含有图案或文字的区域"


def template_path(template_dir: Path, name: str) -> Path:
    if not name or Path(name).name != name or Path(name).suffix.lower() != ".png":
        raise VisionError("模板名称必须是 templates 目录下的 PNG 文件名")
    return template_dir / name


def _decode_color(png: bytes):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise VisionError("缺少图像识别依赖，请运行 pip install -e .") from exc
    return cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR) if png else None


@dataclass
class ImageFrame:
    png: bytes

    @cached_property
    def pixels(self):
        image = _decode_color(self.png)
        if image is None:
            raise VisionError("无法解码截图")
        return image


@lru_cache(maxsize=32)
def _load_template(path: Path, version: tuple[int, int, int, int]):
    # OpenCV's Windows filename reader can fail for non-ASCII install paths.
    try:
        raw_template = path.read_bytes()
    except OSError as exc:
        raise VisionError(f"无法读取截图模板: {path}") from exc
    template = _decode_color(raw_template)
    if template is None:
        raise VisionError(f"无法读取模板: {path}")
    if not (template != template[0, 0]).any():
        raise VisionError(f"{path.name}: {_SOLID_TEMPLATE_ERROR}")
    return template


def find_image(screenshot: bytes | ImageFrame, template_dir: Path, step: dict[str, Any]) -> tuple[int, int, float] | None:
    path = template_path(template_dir, step["image"]).resolve()
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise VisionError(f"缺少截图模板: {path}") from exc
    except OSError as exc:
        raise VisionError(f"无法读取截图模板: {path}") from exc
    template = _load_template(path, (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    screen = screenshot.pixels if isinstance(screenshot, ImageFrame) else ImageFrame(screenshot).pixels
    import cv2
    height, width = screen.shape[:2]
    region = step.get("region", [0, 0, 1, 1])
    if not isinstance(region, list) or len(region) != 4 or any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in region):
        raise VisionError("region 必须是 [左, 上, 右, 下] 的比例坐标")
    x1, y1, x2, y2 = int(region[0] * width), int(region[1] * height), int(region[2] * width), int(region[3] * height)
    if x1 >= x2 or y1 >= y2:
        raise VisionError("region 范围无效")
    crop = screen[y1:y2, x1:x2]
    th, tw = template.shape[:2]
    if th > crop.shape[0] or tw > crop.shape[1]:
        return None
    score_map = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, point = cv2.minMaxLoc(score_map)
    threshold = float(step.get("threshold", 0.88))
    if not 0 <= threshold <= 1:
        raise VisionError("threshold 必须在 0 到 1 之间")
    if score < threshold:
        return None
    return x1 + point[0] + tw // 2, y1 + point[1] + th // 2, float(score)


def crop_template(screenshot: bytes, box: list[float], path: Path) -> tuple[int, int]:
    if len(box) != 4 or any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in box):
        raise VisionError("裁剪坐标必须在 0 到 1 之间")
    with Image.open(io.BytesIO(screenshot)) as image:
        width, height = image.size
        coords = (round(box[0] * width), round(box[1] * height), round(box[2] * width), round(box[3] * height))
        if coords[2] - coords[0] < 8 or coords[3] - coords[1] < 8:
            raise VisionError("模板区域至少需要 8×8 像素")
        cropped = image.crop(coords).convert("RGB")
        if all(low == high for low, high in cropped.getextrema()):
            raise VisionError(_SOLID_TEMPLATE_ERROR)
        path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(path)
        _load_template.cache_clear()
        return cropped.size
