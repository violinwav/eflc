"""Settings view plumbing: engine tuning, settings merge, controller-view mapping."""

import os
import tempfile
import unittest

from eflc.core.engine import HudEntry, HudModel
from eflc.hud.controller_view import mapping_from_model
from tests.helpers import Rig
from tests.test_features import FakeTargets, piano_grid, stick


class TuningTests(unittest.TestCase):
    def test_pointer_speed_multiplier(self):
        moved = []
        for scale in (1.0, 2.0):
            r = Rig(fg="notepad.exe")
            r.engine.apply_tuning({"pointer_speed": scale})
            r.out.pos = [500.0, 500.0]
            r.tick()
            stick(r, 0.5, 0.0, ms=100)
            moved.append(r.out.pos[0] - 500)
        self.assertAlmostEqual(moved[1] / moved[0], 2.0, delta=0.15)

    def test_hold_time_override(self):
        r = Rig(focus="pianoroll")
        r.engine.apply_tuning({"hold_ms": 800})
        r.down("BACK")
        r.wait(450)
        self.assertEqual(r.hud.commands, [])           # profile's 400 ms no longer applies
        r.wait(400)
        self.assertEqual(r.hud.commands, [("all", True)])
        r.up("BACK")
        r.engine.apply_tuning({})                       # back to the profile value
        self.assertEqual(r.engine.timing.hold_ms, 400)

    def test_magnet_detent(self):
        r = Rig(focus="pianoroll", targets=FakeTargets(piano_grid()))
        r.engine.apply_tuning({"magnet_detent": 0.45})
        self.assertEqual(r.engine.pointer.detent, 0.45)


class SettingsMergeTests(unittest.TestCase):
    def test_patch_keeps_stick_calibration(self):
        os.environ["EFLC_HOME"] = tempfile.mkdtemp()
        from eflc.runtime import Runtime

        class Dummy:
            name = "x"
            is_open = False

            def __getattr__(self, n):
                return lambda *a, **k: None

        settings = {"pad": {"sticks": {"LS": {"deadzone": 0.15, "center": [0.11, -0.06]}}}, "hud": {"scale": 1.0}}
        rt = Runtime(settings, Dummy(), out=Dummy(), backend=Dummy(), fg=Dummy(), parse_combo=lambda c: [],
                     midi_link=Dummy(), windows=Dummy(), tracker=Dummy())
        rt._apply_settings({"pad": {"sticks": {"LS": {"deadzone": 0.3}}}, "hud": {"style": "controller"}})
        self.assertEqual(rt.settings["pad"]["sticks"]["LS"], {"deadzone": 0.3, "center": [0.11, -0.06]})
        self.assertEqual(rt.settings["hud"], {"scale": 1.0, "style": "controller"})


class ControllerViewMappingTests(unittest.TestCase):
    def model(self, entries, layers=("LT", "RT", "RB")):
        return HudModel(entries=[HudEntry(*e) for e in entries], layers=layers)

    def test_tap_beats_hold_and_extras_are_tagged(self):
        m = mapping_from_model(self.model([("Y", "hold", "Window wheel"), ("Y", "release", "Next window")]))
        self.assertEqual(m["Y"]["main"], "Next window")
        self.assertEqual(m["Y"]["extra"], ["tap", "hold: Window wheel"])

    def test_stick_axis_with_click(self):
        m = mapping_from_model(self.model([("LS", "press", "Keyboard"), ("LS", "axis", "Pointer")]))
        self.assertEqual(m["LS"]["main"], "Pointer")
        self.assertEqual(m["LS"]["extra"], ["click: Keyboard"])

    def test_dpad_collapsed_and_split(self):
        m = mapping_from_model(self.model([("↑↓←→", "repeat", "Navigate")]))
        self.assertEqual(m["DPAD"]["main"], "Navigate")
        m = mapping_from_model(self.model([("↑", "press", "Select all"), ("→", "press", "Duplicate")]))
        self.assertEqual(m["DPAD"]["main"], "↑ Select all")
        self.assertEqual(m["DPAD"]["extra"], ["→ Duplicate"])

    def test_layer_buttons_marked(self):
        m = mapping_from_model(self.model([("A", "press", "Click")]))
        self.assertTrue(m["LT"]["layer"])
        self.assertNotIn("LB", m)


if __name__ == "__main__":
    unittest.main()
