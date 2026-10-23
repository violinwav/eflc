"""Turns raw samples into a clean PadState: drift-compensated sticks, digital
triggers and stick directions with hysteresis."""

import math
from dataclasses import dataclass, field

STICK_DIRS = ("UP", "DOWN", "LEFT", "RIGHT")

# PlayStation / generic names accepted in profiles, mapped to canonical names.
ALIASES = {
    "CROSS": "A", "CIRCLE": "B", "SQUARE": "X", "TRIANGLE": "Y",
    "L1": "LB", "R1": "RB", "L2": "LT", "R2": "RT", "L3": "LS", "R3": "RS",
    "SHARE": "BACK", "CREATE": "BACK", "VIEW": "BACK", "SELECT": "BACK",
    "OPTIONS": "START", "MENU": "START", "PS": "GUIDE", "HOME": "GUIDE",
    "DPAD_UP": "UP", "DPAD_DOWN": "DOWN", "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT",
}

BUTTONS = (
    "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "LS", "RS", "BACK", "START", "GUIDE",
    "UP", "DOWN", "LEFT", "RIGHT",
    *("LS_" + d for d in STICK_DIRS), *("RS_" + d for d in STICK_DIRS),
    "TOUCHPAD", "MISC", "P1", "P2", "P3", "P4",   # SDL pads (DualSense, Elite paddles, ...)
)
STICKS = ("LS", "RS")
ANALOG_TRIGGERS = ("LT", "RT")
AXES = STICKS + ANALOG_TRIGGERS + ("TOUCH",)   # TOUCH = touchpad finger (0..1, 0..1)


def canonical(name):
    n = name.strip().upper()
    return ALIASES.get(n, n)


@dataclass
class StickConfig:
    deadzone: float = 0.20      # radial; raise this if your pad drifts
    outer: float = 0.95         # values beyond this count as full deflection
    curve: float = 1.6          # >1 = finer control near the centre
    center: tuple = (0.0, 0.0)  # rest offset measured by calibration


@dataclass
class PadConfig:
    sticks: dict = field(default_factory=lambda: {"LS": StickConfig(), "RS": StickConfig()})
    trigger_press: float = 0.50
    trigger_release: float = 0.35
    dir_press: float = 0.60     # stick-as-dpad thresholds (after deadzone)
    dir_release: float = 0.40

    @classmethod
    def from_dict(cls, d):
        cfg = cls()
        for name, sd in (d.get("sticks") or {}).items():
            sc = StickConfig()
            for k in ("deadzone", "outer", "curve"):
                if k in sd:
                    setattr(sc, k, float(sd[k]))
            if "center" in sd:
                sc.center = (float(sd["center"][0]), float(sd["center"][1]))
            cfg.sticks[name] = sc
        for k in ("trigger_press", "trigger_release", "dir_press", "dir_release"):
            if k in d:
                setattr(cfg, k, float(d[k]))
        return cfg

    def to_dict(self):
        return {
            "sticks": {
                n: {"deadzone": round(s.deadzone, 3), "outer": s.outer, "curve": s.curve,
                    "center": [round(s.center[0], 4), round(s.center[1], 4)]}
                for n, s in self.sticks.items()
            },
            "trigger_press": self.trigger_press,
            "trigger_release": self.trigger_release,
            "dir_press": self.dir_press,
            "dir_release": self.dir_release,
        }


def shape_stick(x, y, sc):
    """Centre-correct, radial deadzone, rescale, response curve. Returns (x, y)."""
    x -= sc.center[0]
    y -= sc.center[1]
    mag = math.hypot(x, y)
    if mag <= sc.deadzone or mag == 0.0:
        return 0.0, 0.0
    span = max(1e-6, sc.outer - sc.deadzone)
    norm = min(1.0, (mag - sc.deadzone) / span) ** sc.curve
    return x / mag * norm, y / mag * norm


@dataclass
class PadState:
    connected: bool = False
    pressed: frozenset = frozenset()
    axes: dict = field(default_factory=dict)  # LS/RS -> (x, y), LT/RT -> 0..1


class PadReader:
    def __init__(self, backend, config=None):
        self.backend = backend
        self.config = config or PadConfig()
        self._latched = set()  # hysteresis memory for analog->digital
        self.last_raw = None

    def poll(self, now):
        raw = self.backend.poll(now)
        self.last_raw = raw
        if raw is None:
            self._latched.clear()
            return PadState(False)
        return self.process(raw)

    def process(self, raw):
        cfg = self.config
        pressed = set(raw.buttons)
        axes = {}
        for stick, (x, y) in (("LS", (raw.lx, raw.ly)), ("RS", (raw.rx, raw.ry))):
            sx, sy = shape_stick(x, y, cfg.sticks[stick])
            axes[stick] = (sx, sy)
            for d, v in (("UP", sy), ("DOWN", -sy), ("RIGHT", sx), ("LEFT", -sx)):
                self._digital(stick + "_" + d, v, cfg.dir_press, cfg.dir_release, pressed)
        for trig, v in (("LT", raw.lt), ("RT", raw.rt)):
            axes[trig] = v
            self._digital(trig, v, cfg.trigger_press, cfg.trigger_release, pressed)
        if raw.touch is not None:
            axes["TOUCH"] = raw.touch
        return PadState(True, frozenset(pressed), axes)

    def _digital(self, name, value, on, off, pressed):
        if name in self._latched:
            if value < off:
                self._latched.discard(name)
        elif value >= on:
            self._latched.add(name)
        if name in self._latched:
            pressed.add(name)


class Calibrator:
    """Samples the sticks at rest and derives centre offsets and deadzones."""

    def __init__(self, duration=1.5):
        self.duration = duration
        self.samples = {"LS": [], "RS": []}
        self.start = None

    def feed(self, now, raw):
        if self.start is None:
            self.start = now
        if raw is not None:
            self.samples["LS"].append((raw.lx, raw.ly))
            self.samples["RS"].append((raw.rx, raw.ry))
        return now - self.start >= self.duration

    def apply(self, config):
        report = {}
        for stick, pts in self.samples.items():
            if not pts:
                continue
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            spread = max(math.hypot(p[0] - cx, p[1] - cy) for p in pts)
            sc = config.sticks[stick]
            sc.center = (cx, cy)
            # Keep at least the default, add margin over measured jitter.
            sc.deadzone = round(min(0.45, max(0.15, spread * 1.5 + 0.08)), 3)
            report[stick] = (cx, cy, sc.deadzone)
        return report
