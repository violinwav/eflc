"""Magnetic pointer, modals (wheel / menus / keyboard), tools, MIDI mapping."""

import unittest

from eflc.input.pad import PadState
from eflc.vision.grid import Grid, scale_set
from tests.helpers import Rig, profile_from


def piano_grid():
    # 10 columns of 30 px from x=100, 24 rows of 20 px from y=100; row 0 = B (top)
    xs = [100 + 30 * i for i in range(11)]
    ys = [100 + 20 * i for i in range(25)]
    pitch = [(11 - i) % 12 for i in range(24)]
    return Grid("pianoroll", (100, 100, 400, 580), xs, ys, pitch)


class FakeTargets:
    def __init__(self, grid=None, spots=(), elements=()):
        self._grid, self._spots, self._elements = grid, list(spots), list(elements)
        self.focus = None

    def elements(self):
        return self._elements

    def update_focus(self, x, y):
        self.focus = None if x is None else (x, y)

    def grid(self):
        return self._grid

    def hotspots(self):
        return self._spots

    def invalidate(self):
        pass


def stick(r, x, y, ms=60):
    r.axes = {"LS": (x, y), "RS": (0.0, 0.0)}
    r.wait(ms)
    r.axes = {"LS": (0.0, 0.0), "RS": (0.0, 0.0)}
    r.tick()


class PointerTests(unittest.TestCase):
    def rig(self, **kw):
        r = Rig(focus="pianoroll", targets=FakeTargets(piano_grid()), **kw)
        r.out.pos = [205.0, 210.0]   # inside cell (3, 5)
        r.tick()
        return r

    def test_snaps_to_cell_centres_with_ticks(self):
        r = self.rig()
        stick(r, 1.0, 0.0, ms=120)
        x, y = r.out.pos
        self.assertIn((x - 100) % 30, (7, 8))  # just inside the cell's left line
        self.assertEqual((y - 100) % 20, 10)
        self.assertIn("tick", r.haptics)
        v = r.hud.pointers[-1]
        self.assertEqual(v.kind, "cell")
        self.assertEqual(v.target[2] - v.target[0], 30)

    def test_dpad_steps_one_cell(self):
        r = self.rig()
        stick(r, 0.0, 0.0)
        r.tap("RIGHT")
        self.assertEqual(r.out.pos, [228, 210])
        r.tap("DOWN")
        self.assertEqual(r.out.pos, [228, 230])

    def test_scale_lock_skips_rows(self):
        r = self.rig()
        r.engine.pointer.set_scale(scale_set(0, "Major"), "C major")
        g = piano_grid()
        r.out.pos = list(g.center(2, 11))     # row 11 = C
        r.tick()
        r.tap("UP")                            # C -> D (C# skipped)
        _, y = r.out.pos
        self.assertEqual(g.pitch[g.cell_at(165, y)[1]], 2)

    def test_real_mouse_takes_over(self):
        r = self.rig()
        stick(r, 1.0, 0.0)
        r.out.pos = [333.0, 444.0]             # user grabs the mouse
        r.wait(20)
        self.assertEqual(r.out.pos, [333.0, 444.0])

    def test_free_mode(self):
        r = self.rig()
        r.engine.pointer_op("free")
        stick(r, 1.0, 0.0, ms=50)
        self.assertNotEqual((r.out.pos[0] - 100) % 30, 15)
        self.assertIsNone(r.hud.pointers[-1].target)

    def test_click_lands_on_target(self):
        r = self.rig()
        stick(r, 1.0, 0.0, ms=80)
        r.down("A")
        self.assertEqual(r.out.log[-1], ("mouse", "left", True))
        self.assertIn((r.out.pos[0] - 100) % 30, (7, 8))
        r.up("A")

    def test_snap_off_clicks_exactly_on_the_line(self):
        r = self.rig()
        r.state = {"focus": "pianoroll", "snap": 3}   # midi.Snap_None
        r.tick()
        stick(r, 1.0, 0.0, ms=80)
        self.assertEqual((r.out.pos[0] - 100) % 30, 0)

    def test_rt_layer_switches_tools(self):
        r = self.rig()
        r.down("RT")
        r.tap("UP")
        self.assertEqual(r.out.log[-1], ("key_tap", (ord("C"),)))
        self.assertEqual(r.engine.pointer.tool, "slice")
        r.tap("RIGHT")                                 # slice -> select
        self.assertEqual(r.engine.pointer.tool, "select")
        r.tap("RIGHT")                                 # wraps to draw
        self.assertEqual(r.engine.pointer.tool, "draw")
        r.up("RT")
        self.assertIn("Draw", r.hud.models[-1].pointer)
        stick(r, 1.0, 0.0)
        self.assertEqual(r.hud.pointers[-1].tool, "draw")

    def test_playlist_cycle_includes_slip(self):
        r = Rig(focus="playlist", targets=FakeTargets(piano_grid()))
        r.engine.pointer.tool = "mute"
        r.down("RT")
        r.tap("RIGHT")
        r.up("RT")
        self.assertEqual(r.engine.pointer.tool, "slip")

    def test_hotspots(self):
        t = FakeTargets(None, [(500, 500, "Cutoff"), (560, 500, "Reso")])
        r = Rig(focus="plugin", targets=t)
        r.state = {"focus": "plugin", "plugin": "Serum"}
        r.out.pos = [490.0, 505.0]
        r.tick()
        stick(r, 0.2, 0.0, ms=30)
        self.assertEqual(r.out.pos, [500, 500])
        self.assertEqual(r.hud.pointers[-1].label, "Cutoff")
        r.tap("RIGHT")
        self.assertEqual(r.out.pos, [560, 500])


class UiElementTests(unittest.TestCase):
    """Magnetism outside the editors: menu rows, buttons, anything selectable."""

    MENU = [(100, 100 + 25 * i, 420, 125 + 25 * i, "Item %d" % i) for i in range(8)]
    BUTTONS = [(600 + 40 * i, 300, 632 + 40 * i, 332, "B%d" % i) for i in range(4)]

    def rig(self, elements, pos, fg="notepad.exe"):
        r = Rig(fg=fg, targets=FakeTargets(elements=elements))
        r.out.pos = list(pos)
        r.tick()
        return r

    def test_menu_rows_snap_vertically_and_glide_horizontally(self):
        r = self.rig(self.MENU, (180, 112))
        stick(r, 0.0, -0.6, ms=150)              # down through a few items
        x, y = r.out.pos
        self.assertIn((y - 100) % 25, (12, 13))    # row centre (12.5, rounded)
        self.assertGreater((y - 100) // 25, 0)
        self.assertEqual(x, 180)                    # x untouched
        ticks = r.haptics.count("tick")
        stick(r, 0.6, 0.0, ms=120)                 # sideways inside the row: glides, no new snaps
        self.assertGreater(r.out.pos[0], 180)
        self.assertEqual(r.haptics.count("tick"), ticks)
        self.assertEqual(r.hud.pointers[-1].kind, "ui")

    def test_buttons_snap_to_centre_with_detent(self):
        r = self.rig(self.BUTTONS, (612, 314))
        stick(r, 0.05, 0.0, ms=20)
        self.assertEqual(r.out.pos, [616, 316])     # centre of B0
        stick(r, 0.6, 0.0, ms=60)                   # push into the next button
        self.assertEqual(r.out.pos, [656, 316])
        self.assertEqual(r.hud.pointers[-1].label, "B1")
        r.tap("RIGHT")                              # (global D-pad = arrow keys here)
        self.assertEqual(r.out.log[-1][0], "key_tap")

    def test_free_between_elements(self):
        r = self.rig(self.BUTTONS, (612, 314))
        stick(r, 0.0, -1.0, ms=150)                 # well below the row of buttons
        self.assertIsNone(r.hud.pointers[-1].target)
        self.assertEqual(r.hud.pointers[-1].kind, "free")

    def test_step_jumps_between_elements(self):
        t = FakeTargets(elements=self.BUTTONS)
        r = Rig(focus="plugin", targets=t)
        r.state = {"focus": "plugin", "plugin": "X"}
        r.out.pos = [616.0, 316.0]
        r.tick()
        stick(r, 0.05, 0.0, ms=20)
        r.tap("RIGHT")                              # plugin context: D-pad steps the pointer
        self.assertEqual(r.out.pos, [656, 316])


class ModalTests(unittest.TestCase):
    def test_wheel_hold_aim_release(self):
        r = Rig(focus="pianoroll")
        r.down("Y")
        r.wait(260)                              # hold opens the wheel
        self.assertTrue(r.engine.modals)
        self.assertEqual(r.hud.modals[-1].kind, "wheel")
        r.axes = {"RS": (0.0, -1.0), "LS": (0.0, 0.0)}   # aim down
        r.tick()
        r.axes = {}
        r.up("Y")                                # release picks
        self.assertFalse(r.engine.modals)
        self.assertEqual(r.fl[-1][0], "show")
        self.assertNotIn(("nav", "nextWindow"), r.fl)   # the tap action didn't fire too

    def test_wheel_tap_still_next_window(self):
        r = Rig(focus="pianoroll")
        r.tap("Y")
        self.assertIn(("nav", "nextWindow"), r.fl)
        self.assertFalse(r.engine.modals)

    def test_context_menu_and_submenu(self):
        r = Rig(focus="pianoroll")
        r.tap("RS")
        m = r.hud.modals[-1]
        self.assertEqual(m.kind, "list")
        labels = [it[0] for it in m.items]
        self.assertIn("Tools", labels)
        self.assertIn("Piano roll", labels)       # section header from the context
        # walk to "Tools" and open it
        while r.hud.modals[-1].items[r.hud.modals[-1].selected][0] != "Tools":
            r.tap("DOWN")
        r.tap("A")
        self.assertEqual(r.hud.modals[-1].title, "Tools")
        self.assertEqual(r.hud.modals[-1].depth, 1)
        r.tap("A")                                # Draw -> key "p"
        self.assertEqual(r.out.log[-1], ("key_tap", (ord("P"),)))
        self.assertFalse(r.engine.modals)

    def test_modal_pauses_bindings_and_b_closes(self):
        r = Rig(focus="pianoroll")
        r.tap("RS")
        r.tap("START")    # in a list: activate, not play
        self.assertNotIn(("play",), r.fl)
        r.tap("RS")
        r.tap("B")
        self.assertFalse(r.engine.modals)
        self.assertIsNone(r.hud.modals[-1])

    def test_params_menu_loads_and_adjusts(self):
        r = Rig(focus="plugin")
        r.state = {"focus": "plugin", "plugin": "Serum"}
        r.replies["pluginInfo"] = (True, {"valid": True, "name": "Serum", "kind": "generator", "params": 3})
        r.replies["params"] = (True, {"total": 3, "next": None,
                                      "items": [[0, "Cutoff", 0.5, "50 %"], [2, "Reso", 0.1, "10 %"]]})
        r.replies["setParam"] = lambda i, v: (True, [i, v, "%d %%" % round(v * 100)])
        r.engine.open_menu("@params")
        r.tick()
        m = r.hud.modals[-1]
        self.assertEqual(m.title, "Serum")
        self.assertEqual([it[0] for it in m.items], ["Cutoff", "Reso"])
        r.tap("RIGHT")
        self.assertEqual(r.fl[-1][0], "setParam")
        self.assertAlmostEqual(r.fl[-1][2], 0.51)
        self.assertEqual(r.hud.modals[-1].items[0][1], "51 %")

    def test_keyboard_types(self):
        r = Rig(focus="browser")
        r.tap("LS")                        # opens keyboard (row 1 col 0 = "q")
        self.assertEqual(r.hud.modals[-1].kind, "keyboard")
        r.tap("A")
        r.tap("RIGHT")
        r.tap("A")
        r.tap("Y")
        texts = [e[1] for e in r.out.log if e[0] == "text"]
        self.assertEqual(texts, ["q", "w", " "])
        self.assertEqual(r.hud.modals[-1].text, "qw ")
        r.tap("B")
        self.assertFalse(r.engine.modals)


class ToolAndContextTests(unittest.TestCase):
    def test_slice_macro(self):
        r = Rig(focus="pianoroll", targets=FakeTargets(piano_grid()))
        r.out.pos = [205.0, 210.0]
        r.tick()
        r.tap("LS")                         # slice at pointer
        r.wait(300)
        log = r.out.log
        taps = [e[1] for e in log if e[0] == "key_tap"]
        self.assertEqual(taps[0], (ord("C"),))
        self.assertEqual(taps[-1], (ord("P"),))
        downs = [i for i, e in enumerate(log) if e == ("mouse", "left", True) or e[:2] == ("mouse", "left")]
        self.assertTrue(downs)

    def test_tool_outside_editor_warns(self):
        r = Rig(focus="pianoroll", targets=FakeTargets(None))
        r.tap("LS")
        self.assertIn("Point at the Playlist or Piano roll grid", r.hud.toasts)

    def test_popup_context(self):
        r = Rig(focus="mixer")
        r.state = {"focus": "mixer", "popup": True}
        r.tick()
        self.assertEqual(r.engine.chain[0], "popup")
        r.tap("A")
        self.assertEqual(r.out.log[-1], ("key_tap", (0x0D,)))


class PinchZoomTests(unittest.TestCase):
    def pinch(self, ls, rs, ms=400):
        r = Rig(focus="pianoroll")
        r.down("LT", "RT")
        r.axes = {"LS": ls, "RS": rs}
        r.wait(ms)
        r.axes = {}
        r.up("LT", "RT")
        return [c for c in r.fl if c[0] in ("hzoom", "vzoom")], r

    def test_apart_zooms_in_horizontally(self):
        calls, r = self.pinch((-1.0, 0.0), (1.0, 0.0))
        self.assertTrue(calls)
        self.assertTrue(all(c == ("hzoom", 1) for c in calls), calls)
        self.assertEqual(r.out.pos, [100.0, 100.0])   # pointer didn't move

    def test_together_zooms_out_horizontally(self):
        calls, _ = self.pinch((1.0, 0.0), (-1.0, 0.0))
        self.assertTrue(calls and all(c == ("hzoom", -1) for c in calls), calls)

    def test_vertical(self):
        calls, _ = self.pinch((0.0, -1.0), (0.0, 1.0))      # L down, R up
        self.assertTrue(calls and all(c == ("vzoom", 1) for c in calls), calls)
        calls, _ = self.pinch((0.0, 1.0), (0.0, -1.0))      # L up, R down
        self.assertTrue(calls and all(c == ("vzoom", -1) for c in calls), calls)

    def test_same_direction_does_nothing(self):
        calls, _ = self.pinch((1.0, 0.0), (1.0, 0.0))
        self.assertEqual(calls, [])


class MidiTests(unittest.TestCase):
    def test_automation_profile(self):
        r = Rig(active="Automation", focus="playlist")
        r.down("A")
        self.assertEqual(r.midi[-1], (0x99, 36, 110))   # note on, ch 10
        r.up("A")
        self.assertEqual(r.midi[-1], (0x89, 36, 0))
        r.axes = {"LT": 1.0}
        r.tick()
        self.assertEqual(r.midi[-1], (0xB0, 25, 127))
        r.axes = {"LS": (0.0, 1.0), "LT": 0.0}
        r.wait(300)                       # relative: rises from 64 while pushed
        cc22 = [m for m in r.midi if m[1] == 22]
        self.assertGreater(cc22[-1][2], 80)
        r.axes = {"LS": (0.0, 0.0)}
        last = cc22[-1][2]
        r.wait(100)                       # ...and holds when released
        self.assertEqual([m for m in r.midi if m[1] == 22][-1][2], last)

    def test_channel_pad(self):
        r = Rig(active="Automation", focus="playlist")
        r.down("RIGHT")
        self.assertEqual(r.fl[-1], ("channelNote", 1, 60, 100))
        r.up("RIGHT")
        self.assertEqual(r.fl[-1], ("channelNote", 1, 60, 0))


if __name__ == "__main__":
    unittest.main()
