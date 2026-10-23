"""Actions a binding can run.

Every action has start(ctx) and end(ctx). The engine calls end() when the
button that started the action is released, so "held" behaviour (drag with the
mouse, hold ctrl, show an overlay while held) falls out naturally. Instant
triggers (release, double, repeat) call start() and end() back to back.

ctx is the engine's ActionContext: ctx.out (keyboard/mouse), ctx.fl(op, *args),
ctx.hud(what, on), ctx.toast(text), ctx.switch_profile(name, mode),
ctx.schedule(steps), ctx.calibrate().
"""


class Action:
    held = False  # True if end() does something meaningful

    def start(self, ctx):
        raise NotImplementedError

    def end(self, ctx):
        pass

    def describe(self):
        return type(self).__name__


class FlAction(Action):
    """Whitelisted FL bridge command, e.g. {"fl": "show", "args": ["mixer"]}."""

    def __init__(self, op, args):
        self.op, self.args = op, list(args)

    def start(self, ctx):
        ctx.fl(self.op, *self.args)

    def describe(self):
        a = " ".join(str(x) for x in self.args)
        return ("%s %s" % (self.op, a)).strip()


class KeysAction(Action):
    """Key chord, e.g. {"keys": "ctrl+s"}; with "hold": true it stays down while held."""

    def __init__(self, combo, vks, hold=False):
        self.combo, self.vks, self.held = combo, vks, hold

    def start(self, ctx):
        if self.held:
            ctx.out.key_down(self.vks)
        else:
            ctx.out.key_tap(self.vks)

    def end(self, ctx):
        if self.held:
            ctx.out.key_up(self.vks)

    def describe(self):
        return self.combo


class TextAction(Action):
    def __init__(self, text):
        self.text = text

    def start(self, ctx):
        ctx.out.type_text(self.text)

    def describe(self):
        return "type %r" % self.text


class MouseAction(Action):
    """{"mouse": "left"} clicks; with "hold": true the button follows the pad button (drag)."""

    def __init__(self, button, hold=False):
        if button not in ("left", "right", "middle"):
            raise ValueError("mouse button must be left, right or middle")
        self.button, self.held = button, hold

    def start(self, ctx):
        # via the engine, so the click lands on the magnetic target and the pointer knows
        ctx.mouse(self.button, "down" if self.held else "click")

    def end(self, ctx):
        if self.held:
            ctx.mouse(self.button, "up")

    def describe(self):
        return "%s %s" % (self.button, "drag" if self.held else "click")


class HudAction(Action):
    """{"hud": "toggle"} hides/shows the hint card; {"hud": "all"} shows every shortcut while held."""

    KINDS = ("toggle", "all")

    def __init__(self, what):
        if what not in self.KINDS:
            raise ValueError("hud must be one of %s" % (self.KINDS,))
        self.what = what
        self.held = what == "all"

    def start(self, ctx):
        ctx.hud(self.what, True)

    def end(self, ctx):
        if self.held:
            ctx.hud(self.what, False)

    def describe(self):
        return {"toggle": "Toggle hints", "all": "All shortcuts"}[self.what]


class ProfileAction(Action):
    """{"profile": "Mixing", "mode": "sticky" | "revert"}; name "@next" cycles."""

    def __init__(self, name, mode="sticky"):
        if mode not in ("sticky", "revert"):
            raise ValueError("profile mode must be sticky or revert")
        self.name, self.mode = name, mode

    def start(self, ctx):
        ctx.switch_profile(self.name, self.mode)

    def describe(self):
        return "Next profile" if self.name == "@next" else "Profile: %s" % self.name


class WaitAction(Action):
    def __init__(self, ms):
        self.ms = max(0, int(ms))

    def start(self, ctx):
        pass  # handled by the macro scheduler

    def describe(self):
        return "wait %dms" % self.ms


class MacroAction(Action):
    """A list of actions run in order; {"wait": ms} steps delay without blocking."""

    def __init__(self, steps):
        self.steps = steps

    def start(self, ctx):
        ctx.schedule(self.steps)

    def describe(self):
        return " > ".join(s.describe() for s in self.steps if not isinstance(s, WaitAction))


class CalibrateAction(Action):
    def start(self, ctx):
        ctx.calibrate()

    def describe(self):
        return "Calibrate sticks"


class FuncAction(Action):
    """Internal: wraps a python callable (used by macros the engine builds itself)."""

    def __init__(self, fn, label="", end=None):
        self.fn, self.label, self._end = fn, label, end
        self.held = end is not None

    def start(self, ctx):
        self.fn()

    def end(self, ctx):
        if self._end is not None:
            self._end()

    def describe(self):
        return self.label


class PointerAction(Action):
    """{"pointer": "toggle" | "magnetic" | "free" | "step" | "menu" | "mainmenu" | "teach" | "forget"}"""

    OPS = ("toggle", "magnetic", "free", "step", "menu", "mainmenu", "teach", "forget")

    def __init__(self, op, dx=0, dy=0):
        if op not in self.OPS:
            raise ValueError("pointer must be one of %s" % (self.OPS,))
        self.op, self.dx, self.dy = op, int(dx), int(dy)

    def start(self, ctx):
        ctx.pointer_op(self.op, self.dx, self.dy)

    def describe(self):
        return {"toggle": "Magnetic / free pointer", "magnetic": "Magnetic pointer", "free": "Free pointer",
                "step": "Step pointer", "menu": "Window menu", "mainmenu": "Main menu",
                "teach": "Teach hotspot here", "forget": "Forget hotspot here"}[self.op]


class ToolAction(Action):
    """Editor tool at the pointer: {"tool": "slice" | "mute" | "delete" | "select"}."""

    TOOLS = ("slice", "mute", "delete", "select")

    def __init__(self, tool):
        if tool not in self.TOOLS:
            raise ValueError("tool must be one of %s" % (self.TOOLS,))
        self.tool = tool

    def start(self, ctx):
        ctx.tool(self.tool)

    def describe(self):
        return {"slice": "Slice at pointer", "mute": "Mute at pointer",
                "delete": "Delete at pointer", "select": "Select at pointer"}[self.tool]


class ToolModeAction(Action):
    """Switch FL's editor tool: {"tool_mode": "draw" | "paint" | "delete" | "mute" | "slice" |
    "select" | "slip" | "zoom" | "next" | "prev"}. The pointer restyles to match."""

    MODES = ("draw", "paint", "delete", "mute", "slice", "select", "slip", "zoom", "next", "prev")

    def __init__(self, mode):
        if mode not in self.MODES:
            raise ValueError("tool_mode must be one of %s" % (self.MODES,))
        self.mode = mode

    def start(self, ctx):
        ctx.tool_mode(self.mode)

    def describe(self):
        return {"next": "Next tool", "prev": "Previous tool"}.get(self.mode, self.mode.capitalize() + " tool")


class WheelAction(Action):
    """{"wheel": "windows"}: hold the button, aim, release to switch."""

    def __init__(self, kind):
        self.kind = kind

    def start(self, ctx):
        ctx.open_wheel(self.kind)

    def describe(self):
        return "Window wheel"


class MenuAction(Action):
    """{"menu": "context" | "<profile menu>" | "@params" | "@presets" | "@scale" | "@slots" | "@hotspots"}"""

    def __init__(self, name):
        self.name = name

    def start(self, ctx):
        ctx.open_menu(self.name)

    def describe(self):
        return {"context": "Action menu", "@params": "Plugin parameters", "@presets": "Presets",
                "@scale": "Scale lock", "@slots": "Mixer effects", "@hotspots": "Hotspots"}.get(
            self.name, "Menu: %s" % self.name)


class KeyboardAction(Action):
    def start(self, ctx):
        ctx.open_keyboard()

    def describe(self):
        return "On-screen keyboard"


class ScaleAction(Action):
    """{"scale": "toggle"} turns scale lock on/off (last picked scale)."""

    def __init__(self, op):
        self.op = op

    def start(self, ctx):
        ctx.scale_op(self.op)

    def describe(self):
        return "Scale lock on / off"


class MidiAction(Action):
    """Mapping mode: plain MIDI to FL over the EFLC port.

    {"midi": "cc", "cc": 20, "value": 127, "off": 0, "ch": 1}   held: value on press, off on release
    {"midi": "note", "note": 36, "vel": 100, "ch": 1}           held: note on / note off
    """

    def __init__(self, kind, number, value=127, off=0, ch=1):
        if kind not in ("cc", "note"):
            raise ValueError("midi must be cc or note")
        if not (1 <= int(ch) <= 16):
            raise ValueError("ch must be 1..16")
        self.kind, self.number, self.value, self.off, self.ch = kind, int(number), int(value), off, int(ch)
        self.held = off is not None

    def start(self, ctx):
        if self.kind == "cc":
            ctx.midi(0xB0 | (self.ch - 1), self.number, self.value)
        else:
            ctx.midi(0x90 | (self.ch - 1), self.number, self.value)

    def end(self, ctx):
        if self.off is None:
            return
        if self.kind == "cc":
            ctx.midi(0xB0 | (self.ch - 1), self.number, int(self.off))
        else:
            ctx.midi(0x80 | (self.ch - 1), self.number, 0)

    def describe(self):
        return ("CC %d" % self.number) if self.kind == "cc" else ("Note %d" % self.number)


class PadAction(Action):
    """{"pad": {"channel": "selected" | n, "note": 60, "vel": 100}}: play a channel-rack sound."""

    held = True

    def __init__(self, channel, note, vel):
        self.channel = -1 if channel in (None, "selected") else int(channel)
        self.note, self.vel = int(note), int(vel)

    def start(self, ctx):
        ctx.fl("channelNote", self.channel, self.note, self.vel)

    def end(self, ctx):
        ctx.fl("channelNote", self.channel, self.note, 0)

    def describe(self):
        return "Pad %s %d" % ("sel" if self.channel < 0 else "ch%d" % self.channel, self.note)


def parse_action(spec, parse_combo):
    """Build an Action from its JSON form. parse_combo maps 'ctrl+s' -> key codes."""
    if isinstance(spec, list):
        return MacroAction([parse_action(s, parse_combo) for s in spec])
    if not isinstance(spec, dict):
        raise ValueError("action must be an object or a list, got %r" % (spec,))
    if "fl" in spec:
        args = spec.get("args", [])
        if not isinstance(args, list):
            args = [args]
        return FlAction(str(spec["fl"]), args)
    if "keys" in spec:
        combo = str(spec["keys"])
        return KeysAction(combo, parse_combo(combo), bool(spec.get("hold", False)))
    if "text" in spec:
        return TextAction(str(spec["text"]))
    if "mouse" in spec:
        return MouseAction(str(spec["mouse"]), bool(spec.get("hold", False)))
    if "hud" in spec:
        return HudAction(str(spec["hud"]))
    if "profile" in spec:
        return ProfileAction(str(spec["profile"]), str(spec.get("mode", "sticky")))
    if "wait" in spec:
        return WaitAction(spec["wait"])
    if spec.get("calibrate"):
        return CalibrateAction()
    if "pointer" in spec:
        return PointerAction(str(spec["pointer"]), spec.get("dx", 0), spec.get("dy", 0))
    if "tool_mode" in spec:
        return ToolModeAction(str(spec["tool_mode"]))
    if "tool" in spec:
        return ToolAction(str(spec["tool"]))
    if "wheel" in spec:
        return WheelAction(str(spec["wheel"]))
    if "menu" in spec:
        return MenuAction(str(spec["menu"]))
    if spec.get("keyboard"):
        return KeyboardAction()
    if "scale" in spec:
        return ScaleAction(str(spec["scale"]))
    if "midi" in spec:
        kind = str(spec["midi"])
        number = spec.get("cc" if kind == "cc" else "note")
        if number is None:
            raise ValueError("midi %s needs %r" % (kind, "cc" if kind == "cc" else "note"))
        value = spec.get("value" if kind == "cc" else "vel", 127 if kind == "cc" else 100)
        return MidiAction(kind, number, value, spec.get("off", 0), spec.get("ch", 1))
    if "pad" in spec:
        p = spec["pad"]
        return PadAction(p.get("channel", "selected"), p.get("note", 60), p.get("vel", 100))
    raise ValueError("unknown action %r" % (spec,))
