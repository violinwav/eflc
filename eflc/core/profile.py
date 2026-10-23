"""Profile model: modifiers, per-context bindings, axes and sequences (cheat codes).

Profile JSON shape (see eflc/profiles/default.json):

{
  "name": "Default",
  "timing": {"hold_ms": 400, "double_ms": 250, "repeat_delay_ms": 350,
             "repeat_rate_ms": 70, "seq_window_ms": 1500},
  "modifiers": {"LT": {"as": "layer"}, "LB": {"as": "key", "key": "ctrl"}},
  "contexts": {
    "global": {
      "bindings": [{"input": "LT+X", "on": "press", "do": {...}, "label": "..."}],
      "axes": {"LS": {"do": "pointer", "speed": 1800}, "RT+RS": {"do": "scroll"}},
      "sequences": [{"seq": ["UP","UP","DOWN"], "mods": ["LT","RT"], "do": {...}}]
    },
    "pianoroll": {...}, "plugin:sytrus": {...}, "app:chrome.exe": {...}
  }
}
"""

from dataclasses import dataclass, field

from ..input.pad import AXES, BUTTONS, canonical
from .actions import parse_action

TRIGGERS = ("press", "release", "hold", "double", "repeat")
AXIS_KINDS = ("pointer", "scroll", "hscroll", "fl_jog", "midi_cc", "pinch")
WINDOW_CONTEXTS = ("global", "fl", "playlist", "pianoroll", "channelrack", "mixer", "browser",
                   "plugin", "popup")

CONTEXT_TITLES = {
    "global": "Global", "fl": "FL Studio", "playlist": "Playlist", "pianoroll": "Piano roll",
    "channelrack": "Channel rack", "mixer": "Mixer", "browser": "Browser", "plugin": "Plugin",
    "popup": "Menu",
}


def context_title(ctx):
    if ctx in CONTEXT_TITLES:
        return CONTEXT_TITLES[ctx]
    if ctx.startswith("plugin:"):
        return ctx[7:]
    if ctx.startswith("app:"):
        return ctx[4:]
    return ctx


@dataclass
class Timing:
    hold_ms: int = 400
    double_ms: int = 250
    repeat_delay_ms: int = 350
    repeat_rate_ms: int = 70
    seq_window_ms: int = 1500


@dataclass
class Modifier:
    button: str
    kind: str              # "layer" | "key"
    vks: tuple = ()        # keys held while a "key" modifier is down
    key_name: str = ""


@dataclass
class Binding:
    button: str
    mods: frozenset
    on: str
    action: object
    label: str
    context: str
    ms: int = None         # per-binding hold time override


@dataclass
class AxisBinding:
    stick: str
    mods: frozenset
    kind: str
    params: dict
    label: str
    context: str


@dataclass
class SequenceBinding:
    seq: tuple
    mods: frozenset
    action: object
    label: str
    context: str
    window_ms: int


@dataclass
class MenuSpec:
    label: str
    action: object = None
    detail: str = ""
    children: list = None     # submenu entries


@dataclass
class ContextMap:
    # (button, mods) -> {trigger: Binding}
    slots: dict = field(default_factory=dict)
    # (stick, mods) -> AxisBinding
    axes: dict = field(default_factory=dict)
    sequences: list = field(default_factory=list)
    menu: list = field(default_factory=list)   # [MenuSpec] for the action menu


@dataclass
class Profile:
    name: str
    timing: Timing
    modifiers: dict
    contexts: dict          # name -> ContextMap
    source: str = ""
    errors: list = field(default_factory=list)


def _parse_input(text, modifiers):
    parts = [canonical(p) for p in str(text).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty input")
    button, mods = parts[-1], frozenset(parts[:-1])
    if button not in BUTTONS:
        raise ValueError("unknown button %r" % button)
    for m in mods:
        if m not in modifiers:
            raise ValueError("%r is used as a modifier but not declared in \"modifiers\"" % m)
    return button, mods


def _parse_menu(entries, parse_combo, where, errors):
    out = []
    for i, m in enumerate(entries or []):
        try:
            label = str(m["label"])
            if "items" in m:
                out.append(MenuSpec(label, None, str(m.get("detail", "")),
                                    _parse_menu(m["items"], parse_combo, where + "/" + label, errors)))
            else:
                action = parse_action(m["do"], parse_combo)
                detail = m.get("detail")
                if detail is None and type(action).__name__ == "KeysAction":
                    detail = action.combo
                out.append(MenuSpec(label, action, str(detail or "")))
        except (KeyError, ValueError, TypeError) as e:
            errors.append("%s menu #%d: %s" % (where, i + 1, e))
    return out


def _parse_axis_key(text, modifiers):
    parts = [canonical(p) for p in str(text).split("+") if p.strip()]
    stick, mods = parts[-1], frozenset(parts[:-1])
    if stick not in AXES:
        raise ValueError("axes key must end in one of %s, got %r" % (AXES, text))
    for m in mods:
        if m not in modifiers:
            raise ValueError("%r is not a declared modifier" % m)
    return stick, mods


def _valid_context(name):
    return name in WINDOW_CONTEXTS or name.startswith("plugin:") or name.startswith("app:")


def load_profile(data, parse_combo, source=""):
    """Parse profile JSON. Invalid entries are skipped and reported in .errors."""
    errors = []
    name = str(data.get("name") or source or "Unnamed")
    t = data.get("timing") or {}
    timing = Timing(**{k: int(v) for k, v in t.items() if k in Timing.__dataclass_fields__})

    modifiers = {}
    for btn, spec in (data.get("modifiers") or {}).items():
        b = canonical(btn)
        try:
            if b not in BUTTONS:
                raise ValueError("unknown button %r" % btn)
            kind = spec.get("as", "layer")
            if kind == "layer":
                modifiers[b] = Modifier(b, "layer")
            elif kind == "key":
                key = str(spec["key"])
                modifiers[b] = Modifier(b, "key", tuple(parse_combo(key)), key)
            else:
                raise ValueError("modifier \"as\" must be layer or key")
        except (KeyError, ValueError) as e:
            errors.append("modifier %s: %s" % (btn, e))

    contexts = {}
    for cname, cspec in (data.get("contexts") or {}).items():
        cname = cname.lower()
        if not _valid_context(cname):
            errors.append("unknown context %r" % cname)
            continue
        cm = contexts.setdefault(cname, ContextMap())

        for i, b in enumerate(cspec.get("bindings") or []):
            where = "%s binding #%d (%s)" % (cname, i + 1, b.get("input", "?"))
            try:
                button, mods = _parse_input(b["input"], modifiers)
                on = b.get("on", "press")
                if on not in TRIGGERS:
                    raise ValueError("\"on\" must be one of %s" % (TRIGGERS,))
                action = parse_action(b["do"], parse_combo)
                label = b.get("label") or action.describe()
                slot = cm.slots.setdefault((button, mods), {})
                if on in slot:
                    raise ValueError("duplicate %s binding" % on)
                slot[on] = Binding(button, mods, on, action, label, cname, b.get("ms"))
            except (KeyError, ValueError, TypeError) as e:
                errors.append("%s: %s" % (where, e))

        for key, a in (cspec.get("axes") or {}).items():
            try:
                stick, mods = _parse_axis_key(key, modifiers)
                kind = a.get("do")
                if kind not in AXIS_KINDS:
                    raise ValueError("axis \"do\" must be one of %s" % (AXIS_KINDS,))
                if kind == "fl_jog" and "op" not in a:
                    raise ValueError("fl_jog needs \"op\"")
                if kind == "midi_cc":
                    ccs = [a[k] for k in ("cc", "cc_x", "cc_y") if k in a]
                    if not ccs or not all(0 <= int(c) <= 127 for c in ccs):
                        raise ValueError("midi_cc needs \"cc\" (or \"cc_x\"/\"cc_y\") 0..127")
                    if not 1 <= int(a.get("ch", 1)) <= 16:
                        raise ValueError("midi_cc ch must be 1..16")
                    if a.get("mode", "absolute") not in ("absolute", "relative"):
                        raise ValueError("midi_cc mode must be absolute or relative")
                params = {k: v for k, v in a.items() if k not in ("do", "label")}
                label = a.get("label") or kind
                cm.axes[(stick, mods)] = AxisBinding(stick, mods, kind, params, label, cname)
            except (ValueError, TypeError) as e:
                errors.append("%s axis %s: %s" % (cname, key, e))

        for i, s in enumerate(cspec.get("sequences") or []):
            try:
                seq = tuple(canonical(x) for x in s["seq"])
                for x in seq:
                    if x not in BUTTONS:
                        raise ValueError("unknown button %r" % x)
                if len(seq) < 2:
                    raise ValueError("a sequence needs at least 2 presses")
                mods = frozenset(canonical(m) for m in s.get("mods", []))
                for m in mods:
                    if m not in modifiers:
                        raise ValueError("%r is not a declared modifier" % m)
                action = parse_action(s["do"], parse_combo)
                label = s.get("label") or action.describe()
                cm.sequences.append(SequenceBinding(seq, mods, action, label, cname,
                                                    int(s.get("window_ms", timing.seq_window_ms))))
            except (KeyError, ValueError, TypeError) as e:
                errors.append("%s sequence #%d: %s" % (cname, i + 1, e))

        cm.menu = _parse_menu(cspec.get("menu"), parse_combo, cname, errors)

    return Profile(name, timing, modifiers, contexts, source, errors)
