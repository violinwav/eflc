"""Runs the real FL bridge script against stubbed FL modules and the real
companion bridge, connected by a fake loopback MIDI port (like loopMIDI: every
message reaches both ends, including the sender)."""

import importlib.util
import os
import sys
import types
import unittest

from eflc.bridge.midi_bridge import MidiBridge
from eflc.bridge.protocol import FROM_FL, TO_FL, decode, encode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "fl_script", "EFLC", "device_EFLC.py")
FL_LIB = r"C:\Program Files\Image-Line\FL Studio 2024\Shared\Python\Lib"


class Loop:
    def __init__(self):
        self.fl_inbox, self.app_inbox = [], []

    def send(self, data):
        self.fl_inbox.append(bytes(data))
        self.app_inbox.append(bytes(data))


class FakeLink:
    name = "EFLC"

    def __init__(self, loop):
        self.loop, self.is_open = loop, False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def receive(self):
        out, self.loop.app_inbox[:] = list(self.loop.app_inbox), []
        return out

    def send(self, data):
        self.loop.send(data)
        return True


class FakeFL:
    """Minimal stand-ins for FL's scripting modules, recording calls."""

    def __init__(self, midi, loop):
        self.calls = []
        self.focused = midi.widPianoRoll
        self.visible = {midi.widPianoRoll}
        self.midi = midi
        self.loop = loop
        self.assigned = True

    def modules(self):
        rec = self.calls
        fl = self

        def recorder(name):
            return lambda *a: rec.append((name, *a))

        ui = types.ModuleType("ui")
        ui.getFocused = lambda w: w == fl.focused
        ui.getFocusedFormCaption = lambda: "Piano roll - Pattern 1"
        ui.getFocusedPluginName = lambda: "Sytrus"
        ui.getFocusedFormID = lambda: 3
        ui.getSnapMode = lambda: 3
        ui.isInPopupMenu = lambda: False
        ui.getVisible = lambda w: w in fl.visible
        ui.setFocused = recorder("ui.setFocused")
        ui.hideWindow = recorder("ui.hideWindow")

        def show(w):
            rec.append(("ui.showWindow", w))
            fl.visible.add(w)
            fl.focused = w
        ui.showWindow = show
        for n in ("up", "down", "left", "right", "enter", "escape", "next", "previous",
                  "cut", "copy", "paste", "delete", "insert", "nextWindow", "jog",
                  "horZoom", "verZoom", "snapMode", "snapOnOff", "closeActivePopupMenu"):
            setattr(ui, n, recorder("ui." + n))

        transport = types.ModuleType("transport")
        transport.isPlaying = lambda: False
        transport.isRecording = lambda: False
        transport.start = recorder("transport.start")
        transport.stop = recorder("transport.stop")
        transport.record = recorder("transport.record")
        transport.globalTransport = lambda c, v: rec.append(("transport.globalTransport", c, v)) or 1

        general = types.ModuleType("general")
        general.getVersion = lambda: 37
        general.undoUp = recorder("general.undoUp")
        general.undoDown = recorder("general.undoDown")

        channels = types.ModuleType("channels")
        channels.selectedChannel = lambda: 2
        channels.getChannelName = lambda i: "Kick"
        channels.channelCount = lambda: 4
        channels.selectOneChannel = recorder("channels.selectOneChannel")

        patterns = types.ModuleType("patterns")
        patterns.patternNumber = lambda: 1
        patterns.getPatternName = lambda i: "Pattern 1"
        patterns.patternMax = lambda: 999
        patterns.jumpToPattern = recorder("patterns.jumpToPattern")

        mixer = types.ModuleType("mixer")
        mixer.trackNumber = lambda: 0
        mixer.getTrackName = lambda i: "Master"
        mixer.trackCount = lambda: 127
        mixer.setTrackNumber = recorder("mixer.setTrackNumber")

        plugins = types.ModuleType("plugins")
        plugins.nextPreset = recorder("plugins.nextPreset")
        plugins.prevPreset = recorder("plugins.prevPreset")

        device = types.ModuleType("device")
        device.isAssigned = lambda: fl.assigned
        device.midiOutSysex = lambda data: fl.loop.send(data)

        return {"midi": self.midi, "ui": ui, "transport": transport, "general": general,
                "channels": channels, "patterns": patterns, "mixer": mixer,
                "plugins": plugins, "device": device}


def load_midi():
    path = os.path.join(FL_LIB, "midi.py")
    if os.path.exists(path):
        spec = importlib.util.spec_from_file_location("midi", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    mod = types.ModuleType("midi")
    for i, n in enumerate(("widMixer", "widChannelRack", "widPlaylist", "widPianoRoll",
                           "widBrowser", "widPlugin", "widPluginEffect", "widPluginGenerator")):
        setattr(mod, n, i)
    mod.FPT_Save = 92
    return mod


class Event:
    def __init__(self, sysex):
        self.sysex = sysex
        self.handled = None


class BridgeRoundTrip(unittest.TestCase):
    def setUp(self):
        self.loop = Loop()
        self.fl = FakeFL(load_midi(), self.loop)
        mods = self.fl.modules()
        self._saved = {k: sys.modules.get(k) for k in mods}
        sys.modules.update(mods)
        spec = importlib.util.spec_from_file_location("device_EFLC_under_test", SCRIPT)
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        self.script.OnInit()
        self.bridge = MidiBridge(FakeLink(self.loop))
        self.bridge.start()
        self.events = []

    def tearDown(self):
        self.script.OnDeInit()
        self.bridge.stop()
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def pump(self, until, rounds=200):
        for _ in range(rounds):
            self.bridge._next_poll = 0.0  # don't wait for real time between polls
            self.bridge.poll()
            inbox, self.loop.fl_inbox[:] = list(self.loop.fl_inbox), []
            for data in inbox:
                ev = Event(data)
                self.script.OnSysEx(ev)
                self.events.append(ev)
            if until():
                return True
        return False

    def test_link_state_and_commands(self):
        self.assertTrue(self.pump(lambda: self.bridge.state), "no state received")
        self.assertTrue(self.bridge.connected)
        self.assertEqual(self.bridge.hello.get("api"), 37)
        self.assertEqual(self.bridge.state["focus"], "pianoroll")
        self.assertEqual(self.bridge.state["channelName"], "Kick")

        self.bridge.command("show", "mixer")
        self.assertTrue(self.pump(lambda: self.bridge.state.get("focus") == "mixer"))
        self.assertIn(("ui.showWindow", self.fl.midi.widMixer), self.fl.calls)

        self.bridge.command("transport", "Save")
        self.bridge.command("nav", "down", 2)
        self.bridge.command("channelJog", 3)
        self.assertTrue(self.pump(lambda: ("channels.selectOneChannel", 1) in self.fl.calls))
        self.assertIn(("transport.globalTransport", self.fl.midi.FPT_Save, 1), self.fl.calls)
        self.assertEqual(self.fl.calls.count(("ui.down",)), 2)

    def test_rejects_unknown_ops_and_bad_args(self):
        self.assertTrue(self.pump(lambda: self.bridge.connected))
        self.bridge.command("eval", "__import__('os')")
        self.assertTrue(self.pump(lambda: self.bridge.last_error))
        self.assertIn("unknown op", self.bridge.last_error)
        self.bridge.last_error = None
        self.bridge.command("show", "taskmanager")
        self.assertTrue(self.pump(lambda: self.bridge.last_error))
        self.assertIn("unknown window", self.bridge.last_error)

    def test_echoes_and_foreign_sysex_are_ignored(self):
        self.assertTrue(self.pump(lambda: self.bridge.connected))
        own = [e for e in self.events if decode(e.sysex)[0] == FROM_FL]
        self.assertTrue(own and all(e.handled for e in own))  # script saw and dropped its own echo
        foreign = Event(bytes([0xF0, 0x43, 0x10, 0x4C, 0x00, 0xF7]))
        self.script.OnSysEx(foreign)
        self.assertFalse(foreign.handled)  # left for FL / other scripts

    def test_unlinked_output_stays_silent(self):
        self.fl.assigned = False
        self.assertFalse(self.pump(lambda: self.bridge.connected, rounds=20))

    def test_companion_restart_gets_fresh_hello_and_state(self):
        self.assertTrue(self.pump(lambda: self.bridge.state))
        self.bridge = MidiBridge(FakeLink(self.loop))
        self.bridge.start()
        self.assertTrue(self.pump(lambda: self.bridge.state and self.bridge.hello))

    def test_protocol_is_7bit_clean(self):
        data = encode(TO_FL, {"t": "cmd", "args": ["Ünïcödé \u2191"]})
        self.assertTrue(all(b < 0x80 for b in data[1:-1]))
        self.assertEqual(decode(data)[1]["args"], ["Ünïcödé \u2191"])


@unittest.skipUnless(sys.platform == "win32", "winmm only")
class RealPortSmoke(unittest.TestCase):
    """Sends one inert SysEx through the real loopMIDI 'EFLC' port, if present."""

    def test_loopback(self):
        from eflc.platform.win_midi import WinMidiLink, list_ports
        ins, outs = list_ports()
        if "EFLC" not in ins or "EFLC" not in outs:
            self.skipTest("loopMIDI port EFLC not present")
        import time
        link = WinMidiLink("EFLC")
        link.open()
        try:
            probe = encode(0x7F, {"t": "probe", "n": 12345})  # dir 7F: ignored by both sides
            self.assertTrue(link.send(probe))
            got = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and probe not in got:
                got += link.receive()
                time.sleep(0.005)
            self.assertIn(probe, got)
        finally:
            link.close()


if __name__ == "__main__":
    unittest.main()
