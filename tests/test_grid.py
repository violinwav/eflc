"""Grid detection against real FL Studio 24 captures (tests/fixtures)."""

import os
import unittest

import numpy as np

from eflc.vision.grid import analyze, periodic_lines, scale_set

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    from PySide6.QtGui import QImage
    img = QImage(os.path.join(FIX, name)).convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    arr = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    arr = arr.reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :]
    return arr[:, :, 2::-1].copy()  # BGRA -> RGB


class PeriodicTests(unittest.TestCase):
    def test_fractional_spacing(self):
        prof = np.full(800, 17.0)
        for k in range(25):
            prof[int(round(5 + k * 31.5))] = 41
        prof[110] = 66  # playhead line: an outlier
        lines = periodic_lines(prof, 4, 400)
        self.assertAlmostEqual(lines.spacing, 31.5, delta=0.15)
        self.assertAlmostEqual(lines.offset, 5, delta=1.0)

    def test_dark_lines(self):
        prof = np.full(600, 17.0)
        prof[3::18] = 1
        lines = periodic_lines(prof, 4, 400)
        self.assertAlmostEqual(lines.spacing, 18, delta=0.1)

    def test_broad_block_does_not_hide_thin_lines(self):
        # live Playlist case: a dark clip at the top, track lines every 84 px
        prof = np.full(520, 17.0)
        prof[0:22] = 2
        prof[106::84] = 0
        lines = periodic_lines(prof, 16, 400)
        self.assertAlmostEqual(lines.spacing, 84, delta=0.5)

    def test_clip_window_avoids_overlapping_windows(self):
        from eflc.vision.grid import clip_window
        self.assertEqual(clip_window((0, 0, 1000, 800), (100, 400), [(600, 50, 1200, 700)]), (0, 0, 600, 800))
        self.assertIsNone(clip_window((0, 0, 1000, 800), (700, 400), [(600, 50, 1200, 700)]))


class PianoRollFixture(unittest.TestCase):
    """908x726 Piano roll: grid (109,103)-(868,585), keyboard x 9..109, rows 20px."""

    @classmethod
    def setUpClass(cls):
        cls.img = load("pianoroll_908x726.png")
        cls.grid = analyze(cls.img, "pianoroll", (109, 103, 868, 585), (9, 103, 109, 585),
                           origin=(1000, 200), focus=(500, 300))

    def test_rows_and_columns(self):
        g = self.grid
        self.assertIsNotNone(g)
        self.assertAlmostEqual(g.ys[1] - g.ys[0], 20, delta=0.3)
        self.assertAlmostEqual(g.xs[1] - g.xs[0], 31.5, delta=0.3)
        self.assertAlmostEqual((g.ys[0] - 200 - 103) % 20, 8, delta=1.0)  # row lines at 8 + 20k
        self.assertEqual(g.rect, (1109, 303, 1868, 785))

    def test_pitch_classes(self):
        g = self.grid
        self.assertIsNotNone(g.pitch)
        # "C7" label row: keyboard y 503..523 in the capture -> that row is C
        c_row = g.cell_at(1300, 200 + 513)[1]
        self.assertEqual(g.pitch[c_row], 0)
        self.assertEqual(g.pitch[c_row - 1], 1)   # one row up = C#
        self.assertEqual(g.pitch[c_row + 1], 11)  # one row down = B

    def test_scale_snap(self):
        g = self.grid
        cmaj = scale_set(0, "Major")
        c_row = g.cell_at(1300, 713)[1]
        cs_y = g.center(5, c_row - 1)[1]           # C# isn't in C major
        ix, iy = g.nearest_cell(1300, cs_y, cmaj)
        self.assertIn(g.pitch[iy], cmaj)
        nx, ny = g.step(ix, c_row, 0, -1, cmaj)     # up from C -> D (skips C#)
        self.assertEqual(g.pitch[ny], 2)


class PlaylistFixture(unittest.TestCase):
    """1608x908 Playlist: grid from x=408, y=97; 18px steps, 84px tracks."""

    def test_unoccluded_region(self):
        img = load("playlist_1608x908.png")
        g = analyze(img, "playlist", (408, 97, 1577, 908), origin=(0, 0), focus=(520, 600),
                    reach=(110, 300))
        self.assertIsNotNone(g)
        self.assertAlmostEqual(g.xs[1] - g.xs[0], 18, delta=0.3)
        self.assertAlmostEqual(g.ys[1] - g.ys[0], 84, delta=1.0)


class PlaylistTrackRows(unittest.TestCase):
    """Live capture with clips covering track separators: rows come from the headers."""

    def test_rows_never_merge_into_multiple_tracks(self):
        img = load("playlist_clips_1608x908.png")
        heights = set()
        for fx in range(448, 1537, 90):
            for fy in range(137, 868, 70):
                g = analyze(img, "playlist", (408, 97, 1577, 908), focus=(fx, fy),
                            headers_local=(241, 97, 408, 908))
                self.assertIsNotNone(g, (fx, fy))
                heights.update(round(b - a) for a, b in zip(g.ys[:-1], g.ys[1:-1]))
        self.assertTrue(heights and heights <= {83, 84}, heights)   # never 2-3 tracks merged

    def test_custom_track_heights(self):
        from eflc.vision.grid import track_rows
        img = load("playlist_clips_1608x908.png").copy()
        # make track 4 (348..432) twice as tall by inserting 84 rows of its interior
        # (no separator) just above its bottom separator
        tall = np.concatenate([img[:428], img[352:428], img[352:360], img[428:-84]])
        rows = track_rows(tall, (241, 97, 408, 908), 97, 908)
        gaps = [round(b - a) for a, b in zip(rows, rows[1:-1])]
        self.assertIn(168, gaps)
        self.assertEqual(gaps.count(84) + gaps.count(83) + gaps.count(168), len(gaps))


if __name__ == "__main__":
    unittest.main()
