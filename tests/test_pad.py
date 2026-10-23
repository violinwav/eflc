import unittest

from eflc.input.pad import Calibrator, PadConfig, PadReader, StickConfig, shape_stick
from eflc.input.xinput import RawPad


class FakeBackend:
    def __init__(self):
        self.raw = RawPad()

    def poll(self, now):
        return self.raw


class StickTests(unittest.TestCase):
    def test_deadzone_kills_drift(self):
        self.assertEqual(shape_stick(0.15, -0.1, StickConfig(deadzone=0.2)), (0.0, 0.0))

    def test_centre_offset_removes_bias(self):
        sc = StickConfig(deadzone=0.05, center=(0.3, 0.0))
        self.assertEqual(shape_stick(0.32, 0.0, sc), (0.0, 0.0))

    def test_full_deflection_reaches_one(self):
        x, y = shape_stick(1.0, 0.0, StickConfig())
        self.assertAlmostEqual(x, 1.0)
        self.assertEqual(y, 0.0)

    def test_trigger_hysteresis(self):
        be = FakeBackend()
        r = PadReader(be)
        be.raw = RawPad(lt=0.55)
        self.assertIn("LT", r.poll(0).pressed)
        be.raw = RawPad(lt=0.40)   # below press, above release: stays down
        self.assertIn("LT", r.poll(0).pressed)
        be.raw = RawPad(lt=0.30)
        self.assertNotIn("LT", r.poll(0).pressed)

    def test_stick_directions(self):
        be = FakeBackend()
        r = PadReader(be)
        be.raw = RawPad(ly=1.0)
        self.assertIn("LS_UP", r.poll(0).pressed)
        be.raw = RawPad(rx=-1.0)
        st = r.poll(0)
        self.assertIn("RS_LEFT", st.pressed)
        self.assertNotIn("LS_UP", st.pressed)

    def test_calibrator_sets_centre_and_deadzone(self):
        cfg = PadConfig()
        cal = Calibrator(duration=0.1)
        t = 0.0
        while not cal.feed(t, RawPad(lx=0.12, ly=-0.05 + (t * 0.1), rx=0.0, ry=0.0)):
            t += 0.01
        rep = cal.apply(cfg)
        self.assertAlmostEqual(cfg.sticks["LS"].center[0], 0.12, places=3)
        self.assertGreaterEqual(rep["LS"][2], 0.15)

    def test_config_roundtrip(self):
        cfg = PadConfig()
        cfg.sticks["LS"].deadzone = 0.27
        cfg2 = PadConfig.from_dict(cfg.to_dict())
        self.assertEqual(cfg2.sticks["LS"].deadzone, 0.27)


if __name__ == "__main__":
    unittest.main()
