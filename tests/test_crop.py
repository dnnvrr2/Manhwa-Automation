import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.ui.crop_dialog import apply_crop, lock_crop_rect


class CropTests(unittest.TestCase):
    def test_apply_crop_writes_the_requested_normalized_region(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            destination = root / "cache" / "crop.png"
            image = Image.new("RGB", (100, 80), "#000000")
            for x in range(50, 100):
                for y in range(40, 80):
                    image.putpixel((x, y), (255, 0, 0))
            image.save(source)
            result = apply_crop(str(source), (0.5, 0.5, 0.5, 0.5), str(destination))
            self.assertEqual(result, str(destination))
            self.assertTrue(destination.is_file())
            with Image.open(destination) as cropped:
                self.assertEqual(cropped.size, (50, 40))
                self.assertEqual(cropped.getpixel((25, 20)), (255, 0, 0))

    def test_lock_crop_rect_keeps_a_9_by_16_image_space_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wide.png"
            Image.new("RGB", (320, 120), "#000000").save(source)
            rect = lock_crop_rect(str(source), (.2, .15, .25, .5))
            x, y, width, height = rect
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + width, 1)
            self.assertLessEqual(y + height, 1)
            self.assertAlmostEqual((width * 320) / (height * 120), 9 / 16, places=6)


if __name__ == "__main__":
    unittest.main()
