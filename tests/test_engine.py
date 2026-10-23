import unittest

from eflc import config
from eflc.core.engine import context_chain
from eflc.platform.win_output import parse_combo
from tests.helpers import VK_CTRL, VK_SHIFT, Rig, profile_from


class PluginNameTests(unittest.TestCase):
    def test_fl_title_decorations_are_stripped(self):
        from eflc.core.hotspots import plugin_key
        self.assertEqual(plugin_key("Fruity soft clipper (Master)"), "Fruity soft clipper")
        self.assertEqual(plugin_key("Serum (Lead)"), "Serum")
        self.assertEqual(plugin_key(""), "Unknown")


class ContextTests(unittest.TestCase):
    def test_chain_for_fl_plugin(self):
        chain = context_chain("fl64.exe", {"focus": "plugin", "plugin": "Sytrus"}, True)
        self.assertEqual(chain, ("plugin:sytrus", "plugin", "fl", "global"))

    def test_chain_for_other_app(self):
        self.assertEqual(context_chain("chrome.exe", {}, True), ("app:chrome.exe", "global"))

    def test_chain_without_bridge_still_knows_fl(self):
        self.assertEqual(context_chain("fl64.exe", {"focus": "mixer"}, False), ("fl", "global"))


class DefaultProfileTests(unittest.TestCase):
    def test_bundled_profiles_load_cleanly(self):
        profiles, errors = config.load_profiles(parse_combo, config.BUNDLED_PROFILES)
        self.assertEqual(errors, [])
        self.assertEqual(set(profiles), {"Default", "Mixing", "Automation"})

    def test_mouse_hold_follows_button(self):
        r = Rig()
        r.down("A")
        self.assertEqual(r.out.log[-1], ("mouse", "left", True))
        r.up("A")
        self.assertEqual(r.out.log[-1], ("mouse", "left", False))

    def test_layer_chord_fires_and_suppresses_tap(self):
        r = Rig()
        r.down("LT")
        r.tap("Y")
        r.up("LT")
        self.assertEqual(r.fl, [("show", "playlist")])

    def test_rb_tap_steps_forward_and_never_holds_ctrl(self):
        r = Rig(focus="mixer")
        r.tap("RB")
        self.assertEqual(r.out.log, [])                     # a layer, not a held key
        self.assertEqual(r.fl, [("mixerJog", 1)])

    def test_rb_layer_ctrl_shortcuts(self):
        ctrl = lambda k: ("key_tap", (VK_CTRL, ord(k)))
        r = Rig(focus="pianoroll")
        r.down("RB")
        for button, key in (("Y", "C"), ("X", "X"), ("A", "V"), ("UP", "A"), ("DOWN", "D"), ("RIGHT", "B")):
            r.tap(button)
            self.assertEqual(r.out.log[-1], ctrl(key), button)
        r.tap("B")
        r.up("RB")
        self.assertEqual(r.fl, [("undo",)])                  # FL's multi-step undo, no RB tap
        r = Rig(fg="notepad.exe")                             # other apps: plain Ctrl shortcuts
        r.down("RB")
        r.tap("B")
        r.tap("LEFT")
        r.up("RB")
        self.assertEqual([e for e in r.out.log if e[0] == "key_tap"], [ctrl("Z"), ctrl("Y")])

    def test_rt_rb_steps_back(self):
        r = Rig(focus="mixer")
        r.down("RT")
        r.tap("RB")
        r.up("RT")
        self.assertEqual(r.fl, [("mixerJog", -1)])

    def test_lb_is_plain_shift(self):
        for focus in ("mixer", "playlist", "channelrack", "plugin"):
            r = Rig(focus=focus)
            r.tap("LB")
            self.assertEqual(r.out.log, [("key_down", (VK_SHIFT,)), ("key_up", (VK_SHIFT,))], focus)
            self.assertEqual(r.fl, [], focus)

    def test_shift_drag(self):
        r = Rig(focus="playlist")
        r.down("LB")
        r.down("A")
        r.up("A")
        r.up("LB")
        log = r.out.log
        self.assertLess(log.index(("key_down", (VK_SHIFT,))), log.index(("mouse", "left", True)))
        self.assertLess(log.index(("mouse", "left", False)), log.index(("key_up", (VK_SHIFT,))))
        self.assertEqual(r.fl, [])

    def test_rb_with_stick_skips_tap(self):
        r = Rig(focus="mixer")
        r.down("RB")
        r.axes = {"RS": (0.0, 0.8)}           # ctrl + wheel (zoom) while held
        r.wait(100)
        r.axes = {}
        r.up("RB")
        self.assertEqual(r.fl, [])
        self.assertTrue(any(e[0] == "wheel" for e in r.out.log))

    def test_hint_card_lists_held_keys(self):
        r = Rig(focus="mixer")
        labels = {(e.input, e.label) for e in r.hud.models[-1].entries}
        self.assertIn(("LB", "Shift (hold)"), labels)
        self.assertNotIn(("RB", "Ctrl (hold)"), labels)
        r.down("RB")                                          # holding RB shows its layer
        self.assertIn("Copy", [e.label for e in r.hud.models[-1].entries])

    def test_hold_vs_tap_on_same_button(self):
        r = Rig()
        r.tap("BACK")
        self.assertEqual(r.hud.commands, [("toggle", True)])
        r.hud.commands.clear()
        r.down("BACK")
        r.wait(450)
        self.assertEqual(r.hud.commands, [("all", True)])
        r.up("BACK")
        self.assertEqual(r.hud.commands, [("all", True), ("all", False)])

    def test_repeat(self):
        r = Rig(focus="browser")
        r.down("DOWN")
        self.assertEqual(r.fl, [("nav", "down")])
        r.wait(340)
        self.assertEqual(len(r.fl), 1)
        r.wait(100)  # ~445ms held: repeats at 350 and 420ms
        self.assertEqual(len(r.fl), 3)
        r.up("DOWN")
        r.wait(200)
        self.assertEqual(len(r.fl), 3)

    def test_sequence_cheat_code(self):
        r = Rig()
        r.down("LT", "RT")
        for b in ("UP", "UP", "DOWN", "DOWN"):
            r.tap(b)
        r.up("LT", "RT")
        self.assertIn(("transport", "Save"), r.fl)

    def test_context_override_and_fallback(self):
        r = Rig(focus="mixer")
        r.tap("START")
        self.assertEqual(r.fl[-1], ("play",))       # from "fl"
        r.state = {"focus": "browser"}
        r.tick()
        r.tap("START")
        self.assertEqual(r.fl[-1], ("nav", "enter"))  # browser overrides
        r.fg = "notepad.exe"
        r.tick()
        r.tap("START")
        self.assertEqual(r.out.log[-1], ("key_tap", (0x0D,)))  # global enter

    def test_plugin_specific_context(self):
        r = Rig(focus="plugin")
        r.state = {"focus": "plugin", "plugin": "Sytrus"}
        r.tick()
        r.tap("RB")
        self.assertEqual(r.fl[-1], ("preset", 1))

    def test_not_linked_toasts(self):
        r = Rig(linked=False)
        r.tap("START")
        self.assertIn("FL bridge not linked", r.hud.toasts)

    def test_profile_extends(self):
        r = Rig(active="Mixing", focus="mixer")
        r.tap("RIGHT")
        self.assertEqual(r.fl[-1], ("mixerJog", 1))
        r.down("LT")
        r.tap("A")
        r.up("LT")
        self.assertEqual(r.fl[-1], ("show", "pianoroll"))  # inherited from Default

    def test_pointer_axis_moves_and_suppresses_stick_dirs(self):
        r = Rig(fg="notepad.exe")
        r.axes = {"LS": (1.0, 0.0), "RS": (0.0, 0.0)}
        r.pressed.add("LS_RIGHT")  # would be "RIGHT"-ish virtual button, must be ignored
        r.wait(100)
        moves = [e for e in r.out.log if e[0] == "move"]
        self.assertTrue(moves)
        self.assertGreater(moves[-1][1], 100 + 1600 * 0.08)
        self.assertNotIn("LS_RIGHT", r.engine.prev_pressed)

    def wheel(self, r, horizontal):
        return [e[1] for e in r.out.log if e[0] == "wheel" and e[2] == horizontal]

    def test_scroll_axis(self):
        r = Rig(fg="notepad.exe")
        r.axes = {"LS": (0.0, 0.0), "RS": (0.0, 1.0)}
        r.wait(500)
        v = self.wheel(r, False)
        self.assertTrue(5 * 120 <= sum(v) <= 8 * 120, sum(v))    # ~14 notches/s for 0.5 s
        self.assertTrue(all(0 < d < 120 for d in v))             # smooth: fractions of a notch
        self.assertEqual(self.wheel(r, True), [])

    def test_right_stick_x_scrolls_horizontally(self):
        r = Rig(fg="notepad.exe")
        r.axes = {"RS": (1.0, 0.0)}
        r.wait(300)
        h = self.wheel(r, True)
        self.assertGreater(sum(h), 3 * 120)                      # right = scroll right (+)
        self.assertEqual(self.wheel(r, False), [])
        r.out.log.clear()
        r.axes = {"RS": (-1.0, 0.0)}
        r.wait(100)
        self.assertLess(sum(self.wheel(r, True)), 0)             # left = scroll left

    def test_scroll_axis_lock_and_diagonal(self):
        r = Rig(fg="notepad.exe")
        r.axes = {"RS": (0.2, 0.9)}                               # mostly up: no sideways drift
        r.wait(300)
        self.assertEqual(self.wheel(r, True), [])
        r.out.log.clear()
        r.axes = {"RS": (0.7, 0.7)}                               # real diagonal: both
        r.wait(300)
        self.assertGreater(sum(self.wheel(r, True)), 0)
        self.assertGreater(sum(self.wheel(r, False)), 0)

    def test_pad_disconnect_releases_held(self):
        r = Rig()
        r.down("A")
        from eflc.input.pad import PadState
        r.engine.tick(r.t + 0.01, PadState(False), r.fg, r.state, True)
        self.assertEqual(r.out.log[-1], ("mouse", "left", False))

    def test_hud_model_reflects_layer(self):
        r = Rig(focus="pianoroll")
        self.assertEqual(r.hud.models[-1].title, "Piano roll")
        r.down("LT")
        m = r.hud.models[-1]
        self.assertEqual(m.layer, ("LT",))
        self.assertIn("Mixer", [e.label for e in m.entries])


class TriggerTests(unittest.TestCase):
    PROFILE = {
        "name": "T",
        "modifiers": {"LT": {"as": "layer"}},
        "contexts": {"global": {"bindings": [
            {"input": "A", "on": "release", "do": {"keys": "a"}},
            {"input": "A", "on": "double", "do": {"keys": "b"}},
            {"input": "X", "on": "press", "do": {"profile": "U", "mode": "revert"}},
            {"input": "LT", "on": "release", "do": {"keys": "c"}},
        ]}},
    }
    OTHER = {"name": "U", "contexts": {"global": {"bindings": [
        {"input": "A", "on": "press", "do": {"keys": "z"}}]}}}

    def rig(self):
        profiles = {**profile_from(self.PROFILE), **profile_from(self.OTHER)}
        return Rig(profiles, active="T", fg="notepad.exe")

    def taps(self, r):
        return [e[1] for e in r.out.log if e[0] == "key_tap"]

    def test_single_tap_waits_for_double_window(self):
        r = self.rig()
        r.tap("A")
        self.assertEqual(self.taps(r), [])
        r.wait(300)
        self.assertEqual(self.taps(r), [(ord("A"),)])

    def test_double_tap(self):
        r = self.rig()
        r.tap("A")
        r.wait(100)
        r.tap("A")
        r.wait(300)
        self.assertEqual(self.taps(r), [(ord("B"),)])

    def test_layer_button_tap_only_when_alone(self):
        r = self.rig()
        r.tap("LT")
        self.assertEqual(self.taps(r), [(ord("C"),)])

    def test_revert_profile_on_context_change(self):
        r = self.rig()
        r.tap("X")
        self.assertEqual(r.engine.profile.name, "U")
        r.fg = "chrome.exe"
        r.tick()
        self.assertEqual(r.engine.profile.name, "T")

    def test_bad_entries_are_reported_not_fatal(self):
        bad = {"name": "Bad", "contexts": {"global": {"bindings": [
            {"input": "LT+A", "do": {"keys": "a"}},
            {"input": "Q", "do": {"keys": "a"}},
            {"input": "A", "do": {"keys": "nosuchkey"}},
            {"input": "B", "do": {"keys": "b"}},
        ]}, "nowhere": {}}}
        p = profile_from(bad)["Bad"]
        self.assertEqual(len(p.errors), 4)
        self.assertIn(("B", frozenset()), p.contexts["global"].slots)


if __name__ == "__main__":
    unittest.main()
