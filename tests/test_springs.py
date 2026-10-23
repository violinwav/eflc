"""The overlay springs must stay stable at any frame time (an unstable one crashed Qt)."""

import math
import unittest

from eflc.hud.pointer_overlay import Spring


class SpringTests(unittest.TestCase):
    def test_stable_for_slow_and_uneven_frames(self):
        for freq, zeta in ((22, 0.85), (16, 0.68), (14, 0.68), (9, 0.9)):
            for dt in (0.004, 0.008, 0.0156, 0.033, 0.1, 0.5):
                s = Spring(0.0, freq=freq, zeta=zeta)
                s.target = 100.0
                peak = 0.0
                for _ in range(int(1.0 / dt) + 1):
                    peak = max(peak, abs(s.step(dt)))
                self.assertTrue(math.isfinite(s.x))
                self.assertLess(peak, 110.0, (freq, zeta, dt))
                self.assertAlmostEqual(s.x, 100.0, delta=1.0)

    def test_retargeting_mid_flight(self):
        s = Spring(0.0, freq=22, zeta=0.85)
        for i in range(400):
            s.target = 1000.0 if (i // 20) % 2 else -1000.0
            s.step(0.016)
            self.assertLess(abs(s.x), 1200.0)


class OverlayBoxTests(unittest.TestCase):
    """The highlight slides as a rigid box: it never spans more than one row."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_fast_row_changes_never_stretch_the_box(self):
        import eflc.hud.pointer_overlay as po
        from eflc.core.pointer import PointerView
        saved = po.phys_to_logical
        po.phys_to_logical = lambda x, y: (x, y, 1.0)
        try:
            w = po.PointerOverlay()
            tallest = 0.0
            for i in range(30):                       # a new 84 px row every 2 frames
                top = 100 + 84 * (i % 6)
                w.set_view(PointerView(500, top + 42, (450, top, 540, top + 84), "cell", True, False, True))
                for _ in range(2):
                    for s in w.box:
                        s.step(0.016)
                    l, t, r, b = w.rect_now()
                    tallest = max(tallest, b - t)
            self.assertLess(tallest, 84 + 2 * 1.5 + 1)   # row + highlight padding
        finally:
            po.phys_to_logical = saved


class UiMagnetDefaultTests(unittest.TestCase):
    def test_ui_targets_off_by_default(self):
        from eflc.vision.tracker import TargetTracker
        t = TargetTracker()
        self.assertFalse(t.ui_targets)
        self.assertEqual(t.elements(), [])


if __name__ == "__main__":
    unittest.main()
