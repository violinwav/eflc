"""Selectable-element detection on real FL Studio 24 captures."""

import unittest

from eflc.vision.elements import detect_blobs, menu_items
from tests.test_grid import load


class MenuTests(unittest.TestCase):
    def test_file_menu_items(self):
        img = load("fl_menu_file.png")      # FL's FILE menu, 519 x 641
        items = menu_items(img)
        # 8 project items + Import/Export + 10 recent files + More/Revert/Exit; header skipped
        self.assertEqual(len(items), 23)
        heights = sorted(b - t for _, t, _, b in items)
        self.assertTrue(20 <= heights[len(heights) // 2] <= 28, heights)
        # none overlaps the "Recent projects" header band (y ~ 274..291)
        self.assertFalse(any(t < 288 and b > 278 for _, t, _, b in items))
        for l, t, r, b in items:
            self.assertGreater(r - l, 400)       # full-width rows


class BlobTests(unittest.TestCase):
    def test_browser_rows_and_icons(self):
        img = load("browser_300x908.png")
        rects = detect_blobs(img, (0, 0, 300, 600))
        self.assertGreaterEqual(len(rects), 15)
        for l, t, r, b in rects:
            self.assertLessEqual(r - l, 360)
            self.assertLessEqual(b - t, 120)
        # "Fruity Compressor" row (gear icon + name) is one element
        row = [rc for rc in rects if rc[1] <= 140 <= rc[3] and rc[0] < 100 and rc[2] > 250]
        self.assertTrue(row, rects)


if __name__ == "__main__":
    unittest.main()
