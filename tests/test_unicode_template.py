import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from hka import vision
from hka.vision import ImageFrame, VisionError, crop_template, find_image


class UnicodeTemplateTests(unittest.TestCase):
    def test_reuses_decodes_and_detects_replaced_template(self):
        image = Image.new("RGB", (20, 20), "red")
        image.putpixel((5, 5), (0, 255, 0))
        png = io.BytesIO()
        image.save(png, format="PNG")
        frame = ImageFrame(png.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "按钮.png"
            image.crop((2, 2, 12, 12)).save(path)
            step = {"image": path.name}
            with patch("hka.vision._decode_color", wraps=vision._decode_color) as decode:
                self.assertEqual(find_image(frame, directory, step)[:2], (7, 7))
                self.assertEqual(find_image(frame, directory, step)[:2], (7, 7))
                self.assertEqual(decode.call_count, 2)
                image.crop((0, 0, 16, 16)).save(path)
                self.assertEqual(find_image(frame, directory, step)[:2], (8, 8))
                self.assertEqual(decode.call_count, 3)

    def test_existing_solid_color_template_cannot_match_unrelated_screen(self):
        png = io.BytesIO()
        Image.new("RGB", (80, 60), "blue").save(png, format="PNG")
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            Image.new("RGB", (12, 10), "red").save(directory / "solid.png")
            with self.assertRaisesRegex(VisionError, "纯色"):
                find_image(png.getvalue(), directory, {"image": "solid.png", "threshold": 1.0})

    def test_solid_color_crop_preserves_existing_template(self):
        png = io.BytesIO()
        screen = Image.new("RGB", (20, 20), "red")
        screen.putpixel((0, 0), (255, 0, 1))
        screen.save(png, format="PNG")
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "button.png"
            crop_template(png.getvalue(), [0, 0, 1, 1], template)
            before = template.read_bytes()
            with self.assertRaisesRegex(VisionError, "纯色"):
                crop_template(png.getvalue(), [0.5, 0.5, 1, 1], template)
            self.assertEqual(template.read_bytes(), before)

    def test_match_template_under_chinese_install_path(self):
        tile = Image.new("RGB", (12, 10), "red")
        ImageDraw.Draw(tile).rectangle((1, 2, 5, 7), fill="blue")
        screen = Image.new("RGB", (80, 60), "white")
        screen.paste(tile, (23, 17))
        png = io.BytesIO()
        screen.save(png, format="PNG")
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "蒂亚拉小助手"
            directory.mkdir()
            tile.save(directory / "首页.png")
            match = find_image(png.getvalue(), directory, {"image": "首页.png"})
            self.assertIsNotNone(match)
            self.assertEqual(match[:2], (29, 22))
