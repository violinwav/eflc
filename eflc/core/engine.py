"""The mapping engine: pad state + context in, actions out.

Pure logic (no Qt, no Win32 calls of its own) so it is unit-testable; outputs
go through injected sinks: `out` (keyboard/mouse), `fl` (bridge commands),
`hud` (overlay), `haptic`, `midi`; Win32 window helpers come in as `windows`.

Trigger semantics
  press    fires on press; held actions end on release
  release  fires on release, only if the button was not used as a modifier,
           did not reach its hold binding, and (if a double binding exists)
           no second tap followed in time
  hold     fires after hold_ms; held actions end on release
  double   fires on the second release within double_ms
  repeat   fires on press, then auto-repeats while held (menus, lists)
Modifiers ("layer" or "key") change which bindings match; a "key" modifier also
holds a real key (e.g. ctrl) so mouse and keyboard actions combine with it.
Lookup walks the context chain (most specific first); the first context that
binds a (button, modifiers) pair owns it entirely.

While a modal (menu, wheel, keyboard) is open it receives all input instead.
"""

from collections import deque
from dataclasses import dataclass, field

from ..input.pad import AXES, STICK_DIRS, STICKS
from ..vision.grid import NOTE_NAMES, SCALES, scale_set
from .actions import FuncAction, WaitAction
from .hotspots import plugin_key
from .modals import KeyboardModal, ListModal, MenuItem, WheelItem, WheelModal
from .pointer import Pointer
from .profile import context_title

FL_EXES = {"fl64.exe", "fl.exe", "fl64 (scaled).exe", "ilbridge.exe", "ilpluginhost.exe"}
EDITOR_KINDS = ("pianoroll", "playlist", "mixer", "browser", "channelrack", "editor")
MAIN_WINDOWS = (("Playlist", "playlist"), ("Piano roll", "pianoroll"), ("Channel rack", "channelrack"),
                ("Mixer", "mixer"), ("Browser", "browser"))
POPUP_GRACE = 8.0   # s we assume an FL menu is open after we clicked one

# FL's editor tool hotkeys, and the order RT + left/right cycles through them.
TOOL_KEYS = {"draw": "p", "paint": "b", "delete": "d", "mute": "t", "slice": "c",
             "select": "e", "slip": "s", "zoom": "z"}
TOOL_CYCLE = {"pianoroll": ("draw", "paint", "delete", "mute", "slice", "select"),
              "playlist": ("draw", "paint", "delete", "mute", "slip", "slice", "select")}
TOOL_NAMES = {"draw": "Draw", "paint": "Paint", "delete": "Delete", "mute": "Mute", "slice": "Slice",
              "select": "Select", "slip": "Slip", "zoom": "Zoom"}


def context_chain(fg_exe, fl_state, fl_linked, popup=False):
    chain = []
    if fg_exe in FL_EXES:
        if popup or (fl_linked and fl_state.get("popup")):
            chain.append("popup")
        focus = fl_state.get("focus") if fl_linked else None
        if focus == "plugin":
            name = (fl_state.get("plugin") or "").strip().lower()
            if name:
                chain.append("plugin:" + name)
            chain.append("plugin")
        elif focus:
            chain.append(focus)
        chain.append("fl")
    elif fg_exe:
        chain.append("app:" + fg_exe)
    chain.append("global")
    return tuple(chain)


@dataclass
class HudEntry:
    input: str       # display form, e.g. "LT + X"
    trigger: str     # press/release/hold/double/repeat/axis/seq
    label: str


@dataclass
class HudModel:
    title: str = ""
    layer: tuple = ()
    profile: str = ""
    pad: bool = False
    fl: bool = False
    fg_is_fl: bool = False
    entries: list = field(default_factory=list)
    groups: list = field(default_factory=list)   # [(title, [HudEntry])] for the all-shortcuts view
    fl_info: str = ""
    pointer: str = ""                            # "Magnetic" / "Free" (+ scale)
    pressed: tuple = ()                          # buttons currently down (controller view lights them)
    layers: tuple = ()                           # layer-modifier buttons of the profile


class _Held:
    __slots__ = ("button", "t", "slot", "mods", "started", "hold_fired", "double_second", "next_repeat")

    def __init__(self, button, t, slot, mods):
        self.button, self.t, self.slot, self.mods = button, t, slot, mods
        self.started = []
        self.hold_fired = False
        self.double_second = False
        self.next_repeat = None


class ActionContext:
    """What actions see. Bound to one engine."""

    def __init__(self, engine):
        self._e = engine
        self.out = engine.out

    def fl(self, op, *args):
        self._e.fl_command(op, *args)

    def hud(self, what, on):
        self._e.hud.command(what, on)

    def toast(self, text):
        self._e.hud.toast(text)

    def switch_profile(self, name, mode):
        self._e.switch_profile(name, mode)

    def schedule(self, steps):
        self._e.schedule(steps)

    def calibrate(self):
        self._e.on_calibrate()

    def mouse(self, button, phase):
        self._e.mouse(button, phase)

    def pointer_op(self, op, dx=0, dy=0):
        self._e.pointer_op(op, dx, dy)

    def tool(self, name):
        self._e.tool(name)

    def open_wheel(self, kind):
        self._e.open_wheel(kind)

    def open_menu(self, name):
        self._e.open_menu(name)

    def open_keyboard(self):
        self._e.push_modal(KeyboardModal(self._e, owner=None))

    def scale_op(self, op):
        self._e.scale_op(op)

    def tool_mode(self, mode):
        self._e.set_tool_mode(mode)

    def midi(self, status, d1, d2):
        self._e.send_midi(status, d1, d2)


class Engine:
    def __init__(self, profiles, active, out, fl_command, hud, on_calibrate=lambda: None,
                 haptic=None, targets=None, midi=None, windows=None, hotspots=None, parse_combo=None):
        self.out = out
        self._fl_command = fl_command
        self.hud = hud
        self.on_calibrate = on_calibrate
        self._haptic = haptic or (lambda kind: None)
        self._midi = midi
        self.windows = windows
        self.hotspots = hotspots
        self.parse_combo = parse_combo or (lambda combo: [])
        self.targets = targets
        self.pointer = Pointer(out, self.haptic, targets)
        self.ctx = ActionContext(self)
        self.profiles = {}
        self.profile = None
        self.set_profiles(profiles, active)

        self.prev_pressed = frozenset()
        self.held = {}             # button -> _Held
        self.mod_down = {}         # modifier button -> used flag
        self.pending_taps = {}     # button -> (deadline, Binding|None)
        self.seq_buf = deque(maxlen=16)
        self.macros = []           # [(due, [remaining steps])]
        self.modals = []           # open modal stack (top = last)
        self.chain = ("global",)
        self.revert = None         # (profile name, context head) for "revert" switches
        self._accum = {}           # analog accumulators
        self._pinching = False
        self._cc = {}            # (ch, cc) -> last sent / relative value
        self._last_t = None
        self._now = 0.0
        self._hud_key = None
        self._modal_key = None
        self._pointer_key = None
        self._current_button = None
        self.popup_until = 0.0
        self.scale_root, self.scale_name = 0, "Minor"
        self.fl_linked = False
        self.fl_state = {}
        self.fg_exe = ""
        self.pad_connected = False
        self.tuning = {}

    # ------------------------------------------------------------------ tuning (settings view)
    TIMING_KEYS = ("hold_ms", "double_ms", "repeat_delay_ms", "repeat_rate_ms")

    def apply_tuning(self, tuning):
        """Manual adjustments from settings.json "tuning", on top of the profile:
        pointer_speed / scroll_speed (multipliers), magnet_detent (0..0.45) and
        timing overrides (hold_ms, double_ms, repeat_delay_ms, repeat_rate_ms)."""
        self.tuning = dict(tuning or {})
        self.pointer.detent = float(self.tuning.get("magnet_detent", self.pointer.DEFAULT_DETENT))

    @property
    def timing(self):
        t = self.profile.timing
        over = {k: int(self.tuning[k]) for k in self.TIMING_KEYS if self.tuning.get(k)}
        if not over:
            return t
        from dataclasses import replace
        return replace(t, **over)

    # ------------------------------------------------------------------ profiles
    def set_profiles(self, profiles, active=None):
        self.profiles = dict(profiles)
        name = active or (self.profile.name if self.profile else None)
        if name not in self.profiles:
            name = next(iter(sorted(self.profiles)), None)
        if name is None:
            raise ValueError("no profiles loaded")
        if self.profile is not None:
            self._release_all()
        self.profile = self.profiles[name]
        self._hud_key = None

    def switch_profile(self, name, mode="sticky"):
        names = sorted(self.profiles)
        if name == "@next":
            i = names.index(self.profile.name) if self.profile.name in names else -1
            name = names[(i + 1) % len(names)]
        if name not in self.profiles:
            self.hud.toast("No profile named %s" % name)
            return
        if mode == "revert":
            if self.revert is None:
                self.revert = (self.profile.name, self.chain[0])
        else:
            self.revert = None
        self._release_all()
        self.profile = self.profiles[name]
        self._hud_key = None
        self.hud.toast("Profile: %s" % name)

    # ------------------------------------------------------------------ outputs
    def haptic(self, kind):
        self._haptic(kind)

    def fl_command(self, op, *args, callback=None):
        if callback is not None:
            cid = self._fl_command(op, *args, callback=callback)
        else:
            cid = self._fl_command(op, *args)
        if cid is None:
            self.hud.toast("FL bridge not linked")
            if callback is not None:
                callback(False, "FL bridge not linked")
        return cid

    def send_midi(self, status, d1, d2):
        if self._midi is None or not self._midi(status, d1, d2):
            self.hud.toast("MIDI port not open")

    def schedule(self, steps):
        self.macros.append((self._last_t or 0.0, list(steps)))

    def run_action(self, action):
        action.start(self.ctx)
        if action.held:
            action.end(self.ctx)

    def _start(self, binding, h=None):
        """Start an action; if h is given, end it when that button is released."""
        if binding is None:
            return
        self._current_button = h.button if h is not None else None
        binding.action.start(self.ctx)
        self._current_button = None
        if h is not None and binding.action.held:
            h.started.append(binding.action)
        elif binding.action.held:
            binding.action.end(self.ctx)

    def _instant(self, binding):
        if binding is None:
            return
        binding.action.start(self.ctx)
        if binding.action.held:
            binding.action.end(self.ctx)

    def _release_all(self):
        for h in self.held.values():
            for a in h.started:
                a.end(self.ctx)
        for b in self.mod_down:
            m = self.profile.modifiers.get(b)
            if m is not None and m.kind == "key":
                self.out.key_up(list(m.vks))
        self.held.clear()
        self.mod_down.clear()
        self.pending_taps.clear()
        self.seq_buf.clear()
        self._accum.clear()
        # Buttons still physically down stay in prev_pressed, so they must be
        # released and pressed again before they act under the new state.

    # ------------------------------------------------------------------ lookup
    def _layer_mods(self):
        return frozenset(self.mod_down)

    def lookup(self, button, mods):
        """First context in the chain that binds (button, mods) owns it."""
        prof = self.profile
        candidates = [mods]
        key_mods = frozenset(b for b in mods if prof.modifiers[b].kind == "key")
        if key_mods:
            candidates.append(mods - key_mods)
        for m in candidates:
            for c in self.chain:
                cm = prof.contexts.get(c)
                if cm is not None and (button, m) in cm.slots:
                    return cm.slots[(button, m)]
        return None

    def axis_for(self, stick, mods):
        prof = self.profile
        for m in (mods, frozenset()):
            for c in self.chain:
                cm = prof.contexts.get(c)
                if cm is not None and (stick, m) in cm.axes:
                    return cm.axes[(stick, m)]
        return None

    # ------------------------------------------------------------------ tick
    def tick(self, now, pad, fg_exe, fl_state, fl_linked):
        dt = 0.0 if self._last_t is None else min(0.05, now - self._last_t)
        self._last_t = self._now = now
        self.fg_exe, self.fl_state, self.fl_linked = fg_exe, fl_state, fl_linked

        chain = context_chain(fg_exe, fl_state, fl_linked, popup=now < self.popup_until)
        self.pointer.snap_mode = fl_state.get("snap") if fl_linked else None
        if chain != self.chain:
            self.chain = chain
            if self.revert and chain[0] != self.revert[1] and chain[0] != "popup":
                name, _ = self.revert
                self.revert = None
                self.switch_profile(name, "sticky")

        if self.targets is not None:
            p = self.pointer
            # Track where the pad pointer is heading (the virtual point), in any app,
            # but only while it's in use: the real mouse doesn't need targets.
            active = p.pressed > 0 or (now - p.last_active) < 3.0
            if p.magnetic and active and p.virtual is not None:
                self.targets.update_focus(*p.virtual)
            else:
                self.targets.update_focus(None, None)

        if not pad.connected:
            if self.pad_connected:
                self._release_all()
                self.close_modals()
                self.prev_pressed = frozenset()
            self.pad_connected = False
            self._update_hud()
            return
        self.pad_connected = True

        if self.modals:
            self._tick_modal(now, dt, pad)
            self._run_macros(now)
            self._update_hud()
            return

        mods = self._layer_mods()
        suppressed = set()
        for stick in STICKS:
            if self.axis_for(stick, mods) is not None:
                suppressed.update(stick + "_" + d for d in STICK_DIRS)
        pressed = frozenset(b for b in pad.pressed if b not in suppressed)

        self._timers(now)
        if self.mod_down and any(abs(c) > 0.3 for st in STICKS for c in pad.axes.get(st, (0.0, 0.0))):
            # moving a stick while holding a modifier (ctrl + scroll = zoom, shift + drag...)
            # counts as using it, so its tap action doesn't fire on release
            for k in self.mod_down:
                self.mod_down[k] = True
        for b in sorted(pressed - self.prev_pressed, key=self._press_order):
            self._on_press(b, now)
            if self.modals:      # a binding opened a modal: it owns the rest
                self.prev_pressed = pressed
                self._update_hud()
                return
        for b in self.prev_pressed - pressed:
            self._on_release(b, now)
        self.prev_pressed = pressed
        self._axes(now, dt, pad)
        self._run_macros(now)
        self._update_hud()

    def _press_order(self, b):
        # When a modifier and a button land in the same tick, the modifier wins.
        return (0 if b in self.profile.modifiers else 1, b)

    def _on_press(self, b, now):
        prof = self.profile
        others = self._layer_mods()
        is_mod = b in prof.modifiers
        if is_mod:
            m = prof.modifiers[b]
            if m.kind == "key":
                self.out.key_down(list(m.vks))
            for k in self.mod_down:
                self.mod_down[k] = True
            self.mod_down[b] = bool(others)
        else:
            for k in self.mod_down:
                self.mod_down[k] = True
            if self._check_sequences(b, others, now):
                self.held[b] = _Held(b, now, None, others)
                return

        slot = self.lookup(b, others)
        h = _Held(b, now, slot, others)
        self.held[b] = h
        if not slot:
            return
        pend = self.pending_taps.get(b)
        if pend is not None and "double" in slot:
            del self.pending_taps[b]
            h.double_second = True
        if "press" in slot:
            self._start(slot["press"], h)
        if "repeat" in slot:
            self._current_button = b
            self._instant(slot["repeat"])
            self._current_button = None
            h.next_repeat = now + self.timing.repeat_delay_ms / 1000.0

    def _on_release(self, b, now):
        prof = self.profile
        h = self.held.pop(b, None)
        used = False
        if b in self.mod_down:
            used = self.mod_down.pop(b)
            m = prof.modifiers.get(b)
            if m is not None and m.kind == "key":
                self.out.key_up(list(m.vks))
        if h is None:
            return
        for a in h.started:
            a.end(self.ctx)
        slot = h.slot
        if not slot or used or h.hold_fired:
            return
        if h.double_second:
            self._instant(slot["double"])
        elif "release" in slot:
            if "double" in slot:
                self.pending_taps[b] = (now + self.timing.double_ms / 1000.0, slot["release"])
            else:
                self._instant(slot["release"])
        elif "double" in slot:
            self.pending_taps[b] = (now + self.timing.double_ms / 1000.0, None)

    def _timers(self, now):
        t = self.timing
        for h in list(self.held.values()):
            slot = h.slot
            if not slot:
                continue
            hb = slot.get("hold")
            if hb is not None and not h.hold_fired and not h.double_second:
                ms = hb.ms if hb.ms is not None else t.hold_ms
                if now - h.t >= ms / 1000.0 and not self.mod_down.get(h.button, False):
                    h.hold_fired = True
                    self._start(hb, h)
                    if self.modals:
                        return
            if h.next_repeat is not None and "repeat" in slot:
                while now >= h.next_repeat:
                    self._instant(slot["repeat"])
                    h.next_repeat += t.repeat_rate_ms / 1000.0
        for b, (deadline, binding) in list(self.pending_taps.items()):
            if now >= deadline:
                del self.pending_taps[b]
                self._instant(binding)

    def _check_sequences(self, b, mods, now):
        self.seq_buf.append((b, now, mods))
        buf = list(self.seq_buf)
        for c in self.chain:
            cm = self.profile.contexts.get(c)
            if cm is None:
                continue
            for s in cm.sequences:
                n = len(s.seq)
                if len(buf) < n:
                    continue
                tail = buf[-n:]
                if (tuple(x[0] for x in tail) == s.seq
                        and all(x[2] == s.mods for x in tail)
                        and now - tail[0][1] <= s.window_ms / 1000.0):
                    self.seq_buf.clear()
                    self._instant(s)
                    return True
        return False

    def _run_macros(self, now):
        keep = []
        for due, steps in self.macros:
            while steps and now >= due:
                step = steps.pop(0)
                if isinstance(step, WaitAction):
                    due = now + step.ms / 1000.0
                else:
                    step.start(self.ctx)
                    if step.held:
                        step.end(self.ctx)
            if steps:
                keep.append((due, steps))
        self.macros = keep

    # ------------------------------------------------------------------ analog
    def _axes(self, now, dt, pad):
        mods = self._layer_mods()
        pointer_moved = False
        pointer_bound = False
        claimed = set()
        pinch = self.axis_for("LS", mods)
        if pinch is not None and pinch.kind == "pinch":
            self._pinch(pad, pinch.params, dt)
            claimed = {"LS", "RS"}           # both sticks belong to the pinch
        for stick in AXES:
            if stick in claimed:
                continue
            ab = self.axis_for(stick, mods)
            if ab is None or ab.kind == "pinch":
                continue
            p = ab.params
            raw = pad.axes.get(stick)
            if ab.kind == "pointer":
                pointer_bound = True
                x, y = raw if isinstance(raw, tuple) else (0.0, 0.0)
                if (x or y) and stick in STICKS:
                    self.pointer.move(dt, x, y, float(p.get("speed", 1800)) * float(self.tuning.get("pointer_speed", 1.0)))
                    pointer_moved = True
            elif ab.kind in ("scroll", "hscroll"):
                self._scroll(stick, raw, ab.kind, p, dt)
            elif ab.kind == "fl_jog":
                x, y = raw if isinstance(raw, tuple) else (0.0, 0.0)
                v = x if p.get("axis", "y") == "x" else y
                steps = self._accumulate(stick + "jog" + str(p["op"]), v * float(p.get("rate", 8)) * dt)
                for _ in range(abs(steps)):
                    self.fl_command(p["op"], 1 if steps > 0 else -1)
            elif ab.kind == "midi_cc":
                self._midi_cc(stick, raw, p, dt)
        if pointer_bound and not pointer_moved:
            self.pointer.idle()
        self._push_pointer(now)

    def _pinch(self, pad, p, dt):
        """Two-stick "pinch" zoom (bound as LT+RT+LS: {"do": "pinch"}).

        X: sticks apart (LS left, RS right) zoom in horizontally, together zoom out.
        Y: LS down + RS up zooms in vertically, LS up + RS down zooms out.
        Only the opposing component counts, so pushing both sticks the same way does nothing.
        """
        lx, ly = pad.axes.get("LS", (0.0, 0.0))
        rx, ry = pad.axes.get("RS", (0.0, 0.0))
        rate = float(p.get("rate", 10))
        for axis, v, op in (("h", (rx - lx) / 2.0, "hzoom"), ("v", (ry - ly) / 2.0, "vzoom")):
            if p.get("invert_" + axis):
                v = -v
            steps = self._accumulate("pinch" + axis, v * rate * dt)
            for _ in range(abs(steps)):
                self.fl_command(op, 1 if steps > 0 else -1)
        if abs(rx - lx) + abs(ry - ly) > 0.4 and not self._pinching:
            self.haptic("select")
        self._pinching = abs(rx - lx) + abs(ry - ly) > 0.4

    WHEEL_CHUNK = 24   # wheel units per event (1/5 notch): smooth, ~70 events/s at full tilt

    def _scroll(self, stick, raw, kind, p, dt):
        """Stick -> smooth wheel. "scroll" uses both axes (up/right scroll up/right),
        "hscroll" only X. Sends fractions of a notch so the view glides."""
        x, y = raw if isinstance(raw, tuple) else (0.0, 0.0)
        if kind == "hscroll":
            y = 0.0
        elif not p.get("horizontal", True):
            x = 0.0
        # axis lock: a mostly vertical push shouldn't drift sideways, and vice versa
        if abs(x) < 0.4 * abs(y):
            x = 0.0
        elif abs(y) < 0.4 * abs(x):
            y = 0.0
        if p.get("invert"):
            x, y = -x, -y
        # speed = notches per second at full tilt
        units = float(p.get("speed", 12)) * float(self.tuning.get("scroll_speed", 1.0)) * 120.0 * dt
        for axis, v in (("h", x), ("v", y)):
            key = stick + "wheel" + axis
            if v == 0.0:
                self._accum[key] = 0.0
                continue
            acc = self._accum.get(key, 0.0) + v * units
            if abs(acc) >= self.WHEEL_CHUNK:
                send = int(acc)
                acc -= send
                self.out.wheel_delta(send, horizontal=(axis == "h"))
            self._accum[key] = acc

    def _midi_cc(self, stick, raw, p, dt):
        if raw is None:
            return  # e.g. TOUCH while no finger is down: hold the last value
        if "cc" in p:
            pairs = [(int(p["cc"]), p.get("axis", "y"))]
        else:
            pairs = [(int(p[k]), k[-1]) for k in ("cc_x", "cc_y") if k in p]
        for cc, axis in pairs:
            if isinstance(raw, tuple):
                v = raw[0] if axis == "x" else raw[1]
                if stick == "TOUCH":
                    v = v * 2 - 1 if axis == "x" else 1 - v * 2  # 0..1 -> -1..1, up = +
            else:
                v = raw * 2 - 1  # triggers 0..1 -> -1..1
            self._send_cc(int(p.get("ch", 1)), cc, -v if p.get("invert") else v, p, dt)

    def _send_cc(self, ch, cc, v, p, dt):
        key = (ch, cc)
        if p.get("mode", "absolute") == "relative":
            cur = self._cc.get(key, float(p.get("start", 64)))
            cur = min(127.0, max(0.0, cur + v * float(p.get("rate", 90)) * dt))
            self._cc[key] = cur
            value = int(round(cur))
        else:
            value = int(round((v + 1) / 2 * 127))
        last_key = ("sent",) + key
        if self._cc.get(last_key) != value:
            self._cc[last_key] = value
            if self._midi is not None:
                self._midi(0xB0 | (ch - 1), cc, value)

    def _accumulate(self, key, delta):
        acc = self._accum.get(key, 0.0) + delta
        if delta == 0.0:
            acc = 0.0
        steps = int(acc)
        self._accum[key] = acc - steps
        return steps

    # ------------------------------------------------------------------ pointer
    def mouse(self, button, phase):
        """Clicks go through here so they land on the snapped target."""
        if self.pointer.cursor is None:
            self.pointer.idle()
        if phase == "click":
            self.out.mouse_click(button)
            self.haptic("confirm")
        elif phase == "down":
            self.out.mouse_button(button, True)
            self.pointer.set_pressed(True)
            self.haptic("press")
        else:
            self.out.mouse_button(button, False)
            self.pointer.set_pressed(False)

    def _push_pointer(self, now):
        view = self.pointer.view()
        if view is None:
            return
        key = (round(view.x), round(view.y), view.target, view.kind, view.magnetic, view.pressed,
               view.visible, view.label, view.scale, view.pulse, view.tool)
        if key != self._pointer_key:
            self._pointer_key = key
            self.hud.pointer(view)

    def pointer_op(self, op, dx=0, dy=0):
        p = self.pointer
        if op in ("toggle", "magnetic", "free"):
            want = {"toggle": not p.magnetic, "magnetic": True, "free": False}[op]
            if want != p.magnetic:
                p.toggle_magnetic()
            self.hud.toast("Magnetic pointer" if p.magnetic else "Free pointer")
            self.haptic("confirm")
            p.last_active = self._now
        elif op == "step":
            p.step(dx, dy)
        elif op in ("menu", "mainmenu"):
            self._click_menu(op)
        elif op in ("teach", "forget"):
            self._teach(op)
        self._push_pointer(self._now)

    def _click_at(self, x, y, restore=True):
        before = self.out.cursor_pos()
        self.out.mouse_move_to(int(x), int(y))
        self.out.mouse_click("left")
        if restore:
            self.out.mouse_move_to(*before)

    def _click_menu(self, op):
        w = self.windows
        if w is None:
            self.hud.toast("Window access unavailable")
            return
        if op == "mainmenu":
            r = w.main_menu_rect()
            if r is None:
                self.hud.toast("FL main menu not found")
                return
            self._click_at(r.left + r.height * 0.6, (r.top + r.bottom) / 2, restore=False)
        else:
            form = self._focused_form()
            cap = w.caption_rect(form) if form else None
            if cap is None:
                self.hud.toast("No FL window to open a menu for")
                return
            self._click_at(cap.left + cap.height * 0.45, (cap.top + cap.bottom) / 2, restore=False)
        self.popup_until = self._now + POPUP_GRACE
        self.pointer.cursor = None   # re-sync: we moved the cursor
        self.haptic("confirm")

    def _focused_form(self):
        w = self.windows
        cur = self.out.cursor_pos()
        form = w.form_at(*cur)
        if form is not None:
            return form
        caption = (self.fl_state or {}).get("caption") or ""
        for f in w.forms():
            if caption and f.title.startswith(caption.split(" - ")[0]):
                return f
        return None

    def _teach(self, op):
        w, store = self.windows, self.hotspots
        if w is None or store is None:
            self.hud.toast("Hotspots unavailable")
            return
        x, y = self.out.cursor_pos()
        form = w.form_at(x, y)
        if form is None or form.kind in EDITOR_KINDS:
            self.hud.toast("Point at a plugin window first")
            self.haptic("edge")
            return
        key = plugin_key(form.title)
        dx, dy = x - form.rect.left, y - form.rect.top
        if op == "teach":
            n = store.add(key, dx, dy)
            self.hud.toast("Hotspot %d saved · %s" % (n, key))
        else:
            if store.remove_near(key, dx, dy):
                self.hud.toast("Hotspot removed · %s" % key)
            else:
                self.hud.toast("No hotspot here")
        if self.targets is not None and hasattr(self.targets, "invalidate"):
            self.targets.invalidate()
        self.haptic("confirm")

    def tool(self, name):
        target = self.pointer.current_grid_target()
        if target is None:
            self.hud.toast("Point at the Playlist or Piano roll grid")
            self.haptic("edge")
            return
        grid, ix, iy = target
        cx, cy = grid.center(ix, iy)
        l, t, r, b = grid.cell_rect(ix, iy)
        tap = lambda combo: FuncAction(lambda: self.out.key_tap(self.parse_combo(combo)))
        move = lambda x, y: FuncAction(lambda: self.out.mouse_move_to(int(x), int(y)))
        down = FuncAction(lambda: self.out.mouse_button("left", True))
        up = FuncAction(lambda: self.out.mouse_button("left", False))
        click = FuncAction(lambda: self.out.mouse_click("left"))
        wait = lambda ms: WaitAction(ms)
        if name == "delete":
            steps = [move(cx, cy), FuncAction(lambda: self.out.mouse_click("right"))]
        elif name == "mute":
            steps = [tap("t"), wait(30), move(cx, cy), click, wait(30), tap("p")]
        elif name == "select":
            steps = [tap("e"), wait(30), move(cx, cy), click, wait(30), tap("p")]
        else:  # slice: drag a vertical cut along the cell's left grid line
            x = l + 1
            steps = [tap("c"), wait(30), move(x, t - 2), wait(15), down, wait(30),
                     move(x, b + (b - t)), wait(30), up, wait(30), tap("p"), move(cx, cy)]
        self.schedule(steps)
        self.haptic("confirm")

    # ------------------------------------------------------------------ editor tools
    def set_tool_mode(self, mode):
        editor = self.chain[0] if self.chain and self.chain[0] in TOOL_CYCLE else "pianoroll"
        cycle = TOOL_CYCLE[editor]
        if mode in ("next", "prev"):
            cur = self.pointer.tool if self.pointer.tool in cycle else cycle[0]
            mode = cycle[(cycle.index(cur) + (1 if mode == "next" else -1)) % len(cycle)]
        if mode not in TOOL_KEYS:
            return
        self.out.key_tap(self.parse_combo(TOOL_KEYS[mode]))
        self.pointer.tool = mode
        self.pointer.last_active = self._now
        self.hud.toast("%s tool" % TOOL_NAMES[mode])
        self.haptic("select")
        self._push_pointer(self._now)

    # ------------------------------------------------------------------ scale
    def scale_op(self, op):
        p = self.pointer
        if p.scale is None:
            p.set_scale(scale_set(self.scale_root, self.scale_name),
                        "%s %s" % (NOTE_NAMES[self.scale_root], self.scale_name.lower()))
            self.hud.toast("Scale lock: %s" % p.scale_name)
        else:
            p.set_scale(None, "")
            self.hud.toast("Scale lock off")
        self.haptic("confirm")

    # ------------------------------------------------------------------ modals
    def push_modal(self, modal):
        if not self.modals:
            self._suspend_bindings()
        self.modals.append(modal)
        self.haptic("open")
        self._modal_key = None

    def close_modals(self):
        if self.modals:
            self.modals.clear()
            self._modal_key = None
            self.hud.modal(None)

    def _suspend_bindings(self):
        """Opening a modal ends held actions so nothing stays stuck underneath."""
        for h in self.held.values():
            for a in h.started:
                a.end(self.ctx)
            h.started = []
        for b in self.mod_down:
            m = self.profile.modifiers.get(b)
            if m is not None and m.kind == "key":
                self.out.key_up(list(m.vks))
        self.held.clear()
        self.mod_down.clear()
        self.pending_taps.clear()

    def _tick_modal(self, now, dt, pad):
        top = self.modals[-1]
        pressed = pad.pressed
        for b in sorted(pressed - self.prev_pressed):
            top.press(b, now)
            if top.closed or self.modals and self.modals[-1] is not top:
                break
        for b in self.prev_pressed - pressed:
            if self.modals and self.modals[-1] is top and not top.closed:
                top.release(b, now)
        self.prev_pressed = pressed
        if self.modals and self.modals[-1] is top and not top.closed:
            top.tick(now, dt, pad)
        while self.modals and self.modals[-1].closed:
            self.modals.pop()
        if not self.modals:
            self._modal_key = None
            self.hud.modal(None)
            return
        view = self.modals[-1].view()
        if view != self._modal_key:
            self._modal_key = view
            self.hud.modal(view)

    def open_wheel(self, kind):
        state = self.fl_state if self.fl_linked else {}
        focus = state.get("focus")
        items = []
        for label, wid in MAIN_WINDOWS:
            act = FuncAction(lambda w=wid: self.fl_command("show", w), label)
            items.append(WheelItem(label, wid, action=act, active=(focus == wid)))
        if self.windows is not None:
            try:
                forms = self.windows.forms()
            except Exception:
                forms = []
            for f in forms:
                if f.kind in EDITOR_KINDS or not f.title:
                    continue
                items.append(WheelItem(plugin_key(f.title), "plugin", call=lambda f=f: self._focus_form(f)))
                if len(items) >= 10:
                    break
        self.push_modal(WheelModal(self, "Windows", items, owner=self._current_button))

    def _focus_form(self, form):
        cap = self.windows.caption_rect(form) if self.windows else None
        if cap is None:
            return
        self._click_at(cap.left + cap.width * 0.4, (cap.top + cap.bottom) / 2)

    def open_menu(self, name):
        if name == "context":
            self.push_modal(self._context_menu())
        elif name == "@params":
            self.push_modal(self._params_menu())
        elif name == "@presets":
            self.push_modal(self._presets_menu())
        elif name == "@scale":
            self.push_modal(self._scale_menu())
        elif name == "@slots":
            self.push_modal(self._slots_menu())
        elif name == "@hotspots":
            self.push_modal(self._hotspots_menu())
        else:
            cm = self.profile.contexts.get(name)
            if cm is None or not cm.menu:
                self.hud.toast("No menu for %s" % name)
                return
            self.push_modal(ListModal(self, context_title(name), self._menu_items(cm.menu)))

    def _menu_items(self, specs):
        items = []
        for s in specs:
            if s.children is not None:
                items.append(MenuItem(s.label, kind="submenu", detail="›",
                                      submenu=lambda e, s=s: ListModal(e, s.label, e._menu_items(s.children))))
            else:
                items.append(MenuItem(s.label, action=s.action, detail=s.detail))
        return items

    def _context_menu(self):
        items = []
        sections = [(c, self.profile.contexts[c].menu) for c in self.chain
                    if c in self.profile.contexts and self.profile.contexts[c].menu]
        for c, specs in sections:
            if len(sections) > 1:
                items.append(MenuItem(context_title(c), kind="header"))
            items.extend(self._menu_items(specs))
        if not items:
            items = [MenuItem("No actions for this window", kind="info")]
        title = context_title(self.chain[0]) if self.chain[0] != "popup" else "Menu"
        return ListModal(self, title, items, subtitle="Actions")

    def _params_menu(self):
        hint = "↑↓ choose · ←→ adjust · RS fine · X refresh · B close"

        def load(m):
            m.items = []
            m.loading = True

            def got_info(ok, info):
                if not ok or not info or not info.get("valid"):
                    m.set_items([MenuItem("Focus a plugin window first", kind="info")])
                    m.subtitle = str(info) if not ok else ""
                    return
                m.title = info.get("name") or "Plugin"
                m.subtitle = "%s · %d parameters" % (info.get("kind", ""), info.get("params", 0))
                fetch(0)

            def fetch(start):
                self.fl_command("params", start, 32, callback=got_page)

            def got_page(ok, page):
                if not ok:
                    m.set_items(m.items or [MenuItem("Couldn't read parameters", kind="info", detail=str(page))])
                    return
                for i, name, value, text in page.get("items", []):
                    m.items.append(MenuItem(name, kind="param", value=value, detail=text or "%d%%" % round(value * 100),
                                            data=i, keep_open=True))
                m.set_items(m.items)
                m.loading = page.get("next") is not None and len(m.items) < 512
                if m.loading:
                    fetch(page["next"])

            self.fl_command("pluginInfo", callback=got_info)

        def adjust(m, item, delta, fine):
            if item.kind != "param":
                return
            step = delta * 0.6 if fine else 0.01 * delta
            item.value = min(1.0, max(0.0, (item.value or 0.0) + step))
            item.detail = "%d%%" % round(item.value * 100)

            def done(ok, res, item=item):
                if ok and res:
                    _, value, text = res
                    item.value = value
                    item.detail = text or "%d%%" % round(value * 100)
            self.fl_command("setParam", item.data, item.value, callback=done)
            if not fine:
                self.haptic("select")

        return ListModal(self, "Parameters", hint=hint, loader=load, on_adjust=adjust,
                         on_x=lambda m, it: load(m))

    def _presets_menu(self):
        def load(m):
            def got(ok, names):
                if not ok or not names:
                    m.set_items([MenuItem("No presets available", kind="info", detail="" if ok else str(names))])
                    return
                m.set_items([MenuItem(n, kind="action", keep_open=False) for n in names], keep=False)
            self.fl_command("presets", callback=got)

        def moved(m, old, new):
            self.fl_command("preset", new - old)   # audition while scrolling

        return ListModal(self, "Presets", subtitle="Scrolling steps the plugin's preset",
                         hint="↑↓ audition · A keep · B close", loader=load, on_move=moved)

    def _scale_menu(self):
        def details(m):
            m.subtitle = "Root %s · ←→ to change" % NOTE_NAMES[self.scale_root]
            for it in m.items:
                if it.data:
                    it.detail = "%s%s" % (NOTE_NAMES[self.scale_root],
                                          "  ✓" if self.pointer.scale_name == "%s %s" % (
                                              NOTE_NAMES[self.scale_root], it.data.lower()) else "")

        def pick(m):
            it = m.item
            if it.data is None:
                self.pointer.set_scale(None, "")
                self.hud.toast("Scale lock off")
            else:
                self.scale_name = it.data
                self.pointer.set_scale(scale_set(self.scale_root, it.data),
                                       "%s %s" % (NOTE_NAMES[self.scale_root], it.data.lower()))
                self.hud.toast("Scale lock: %s" % self.pointer.scale_name)
            self.close_modals()

        def adjust(m, item, delta, fine):
            if fine:
                return
            self.scale_root = (self.scale_root + int(delta)) % 12
            self.haptic("select")
            details(m)

        items = [MenuItem("Off", call=pick, data=None)] + [MenuItem(n, call=pick, data=n) for n in SCALES]
        m = ListModal(self, "Scale lock", items, hint="A apply · ←→ root · B close",
                      on_adjust=adjust)
        if self.scale_name in SCALES:
            m.selected = 1 + list(SCALES).index(self.scale_name)
        details(m)
        return m

    def _slots_menu(self):
        def load(m):
            def got(ok, res):
                if not ok:
                    m.set_items([MenuItem("Couldn't read the mixer", kind="info", detail=str(res))])
                    return
                m.title = "Insert %s" % res.get("track") if not res.get("name") else res["name"]
                slots = res.get("slots") or []
                if not slots:
                    m.set_items([MenuItem("No effects on this track", kind="info")])
                    return
                m.set_items([MenuItem(name, detail="slot %d" % (slot + 1),
                                      action=FuncAction(lambda s=slot: self.fl_command("focusSlot", s)))
                             for slot, name in slots], keep=False)
            self.fl_command("mixerSlots", callback=got)
        return ListModal(self, "Mixer effects", loader=load)

    def _hotspots_menu(self):
        w, store = self.windows, self.hotspots
        if w is None or store is None:
            return ListModal(self, "Hotspots", [MenuItem("Hotspots unavailable", kind="info")])
        form = w.form_at(*self.out.cursor_pos())
        if form is None or form.kind in EDITOR_KINDS:
            return ListModal(self, "Hotspots", [MenuItem("Point at a plugin window first", kind="info")])
        key = plugin_key(form.title)
        spots = store.spots(key)

        def go(m):
            s = m.item.data
            self.out.mouse_move_to(form.rect.left + s["x"], form.rect.top + s["y"])
            self.pointer.cursor = None
            self.close_modals()

        def forget(m, item):
            s = item.data
            store.remove_near(key, s["x"], s["y"], radius=2)
            m.set_items([it for it in m.items if it is not item] or [MenuItem("No hotspots left", kind="info")])
            if self.targets is not None and hasattr(self.targets, "invalidate"):
                self.targets.invalidate()

        items = [MenuItem(s["name"], call=go, data=s, detail="%d, %d" % (s["x"], s["y"])) for s in spots] or \
            [MenuItem("None yet: point at a control and teach it", kind="info")]
        return ListModal(self, "Hotspots · %s" % key, items, hint="A go · X forget · B close",
                         on_x=forget)

    # ------------------------------------------------------------------ HUD
    def _update_hud(self):
        mods = self._layer_mods()
        key = (self.chain, mods, self.profile.name, self.pad_connected, self.fl_linked,
               self.fg_exe in FL_EXES, self._fl_info(), self._pointer_label(), self.prev_pressed)
        if key == self._hud_key:
            return
        self._hud_key = key
        self.hud.update(self.build_hud_model(mods))

    def _pointer_label(self):
        p = self.pointer
        s = "Magnetic" if p.magnetic else "Free"
        if self.chain and self.chain[0] in TOOL_CYCLE:
            s += " · %s" % TOOL_NAMES.get(p.tool, p.tool)
        return s + (" · %s" % p.scale_name if p.scale is not None else "")

    def _fl_info(self):
        s = self.fl_state if self.fl_linked else {}
        if not s:
            return ""
        focus = s.get("focus")
        if focus == "plugin":
            return s.get("plugin") or ""
        if focus == "channelrack" or focus == "pianoroll":
            return s.get("channelName") or ""
        if focus == "mixer":
            return s.get("mixerTrackName") or ""
        if focus == "playlist":
            return s.get("patternName") or ""
        return ""

    def build_hud_model(self, mods=None):
        mods = self._layer_mods() if mods is None else mods
        entries = self._layer_entries(mods)
        key_mods = frozenset(b for b in mods if self.profile.modifiers[b].kind == "key")
        if not entries and key_mods:
            # e.g. LB held as ctrl with no LB+ bindings: show what still works.
            entries = self._layer_entries(mods - key_mods)
        return HudModel(
            title=context_title(self.chain[0]),
            layer=tuple(sorted(mods)),
            profile=self.profile.name,
            pad=self.pad_connected,
            fl=self.fl_linked,
            fg_is_fl=self.fg_exe in FL_EXES,
            entries=entries,
            groups=self._all_groups(),
            fl_info=self._fl_info(),
            pointer=self._pointer_label(),
            pressed=tuple(sorted(self.prev_pressed)),
            layers=tuple(sorted(b for b, m in self.profile.modifiers.items() if m.kind == "layer")),
        )

    def _layer_entries(self, mods):
        prof = self.profile
        seen = set()
        entries = []
        for c in self.chain:
            cm = prof.contexts.get(c)
            if cm is None:
                continue
            for (button, bmods), slot in cm.slots.items():
                if bmods != mods or (button, bmods) in seen:
                    continue
                seen.add((button, bmods))
                for on, bnd in slot.items():
                    entries.append(HudEntry(_fmt_input(button, ()), on, bnd.label))
            for (stick, amods), ab in cm.axes.items():
                if amods == mods and ("axis", stick) not in seen:
                    seen.add(("axis", stick))
                    entries.append(HudEntry(stick, "axis", ab.label))
            for s in cm.sequences:
                if s.mods == mods:
                    entries.append(HudEntry(" ".join(_glyph(x) for x in s.seq), "seq", s.label))
        if not mods:  # key modifiers: show what they hold (e.g. LB "Shift (hold)")
            for b, m in prof.modifiers.items():
                if m.kind == "key":
                    entries.append(HudEntry(_fmt_input(b, ()), "mod", "%s (hold)" % m.key_name.capitalize()))
        entries.sort(key=_entry_order)
        return _collapse_dpad(entries)

    def _all_groups(self):
        prof = self.profile
        groups = []
        for c in self.chain:
            cm = prof.contexts.get(c)
            if cm is None:
                continue
            g = []
            for (button, bmods), slot in cm.slots.items():
                for on, bnd in slot.items():
                    g.append(HudEntry(_fmt_input(button, bmods), on, bnd.label))
            for (stick, amods), ab in cm.axes.items():
                g.append(HudEntry(_fmt_input(stick, amods), "axis", ab.label))
            for s in cm.sequences:
                g.append(HudEntry(" + ".join([*sorted(s.mods), " ".join(_glyph(x) for x in s.seq)]), "seq", s.label))
            if g:
                g.sort(key=_entry_order)
                groups.append((context_title(c), _collapse_dpad(g)))
        return groups


_ORDER = {b: i for i, b in enumerate((
    "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "UP", "DOWN", "LEFT", "RIGHT",
    "START", "BACK", "LS", "RS", "GUIDE"))}

GLYPHS = {"UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→"}


def _glyph(b):
    if "_" in b:
        stick, d = b.split("_", 1)
        return stick + GLYPHS.get(d, d)
    return GLYPHS.get(b, b)


def _fmt_input(button, mods):
    return " + ".join([*sorted(mods), _glyph(button)])


_DPAD = [GLYPHS[d] for d in ("UP", "DOWN", "LEFT", "RIGHT")]


def _collapse_dpad(entries):
    """Four D-pad rows with the same trigger and a shared role -> one row."""
    found = {e.input: e for e in entries if e.input in _DPAD}
    if len(found) < 4 or len({e.trigger for e in found.values()}) != 1:
        return entries
    labels = [found[g].label.lower() for g in _DPAD]
    if labels == ["up", "down", "left", "right"]:
        label = "Navigate"
    elif all(l.startswith("step") for l in labels):
        label = "Step pointer"
    else:
        return entries
    out, done = [], False
    for e in entries:
        if e.input in found and found[e.input] is e:
            if not done:
                out.append(HudEntry("".join(_DPAD), e.trigger, label))
                done = True
            continue
        out.append(e)
    return out


def _entry_order(e):
    base = e.input.split(" + ")[-1]
    inv = {v: k for k, v in GLYPHS.items()}
    return (e.input.count("+"), _ORDER.get(inv.get(base, base), 50), e.input, e.trigger)
