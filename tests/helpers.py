from eflc import config
from eflc.core.engine import Engine
from eflc.core.profile import load_profile
from eflc.input.pad import PadState
from eflc.platform.win_output import parse_combo


class RecOut:
    """Records keyboard/mouse output instead of injecting it."""

    def __init__(self):
        self.log = []
        self.pos = [100.0, 100.0]

    def key_down(self, vks): self.log.append(("key_down", tuple(vks)))
    def key_up(self, vks): self.log.append(("key_up", tuple(vks)))
    def key_tap(self, vks): self.log.append(("key_tap", tuple(vks)))
    def type_text(self, t): self.log.append(("text", t))
    def release_all(self): self.log.append(("release_all",))
    def mouse_button(self, b, down): self.log.append(("mouse", b, down))
    def mouse_click(self, b): self.log.append(("click", b))
    def cursor_pos(self): return tuple(self.pos)
    def wheel(self, n, horizontal=False): self.log.append(("wheel", n * 120, horizontal))
    def wheel_delta(self, d, horizontal=False): self.log.append(("wheel", d, horizontal))

    def mouse_move_to(self, x, y):
        self.pos = [x, y]
        self.log.append(("move", x, y))


class RecHud:
    def __init__(self):
        self.models, self.toasts, self.commands = [], [], []
        self.pointers, self.modals = [], []

    def update(self, m): self.models.append(m)
    def toast(self, t): self.toasts.append(t)
    def command(self, what, on): self.commands.append((what, on))
    def pointer(self, v): self.pointers.append(v)
    def modal(self, v): self.modals.append(v)


class Rig:
    """Drives an Engine with a fake clock and fake pad."""

    def __init__(self, profiles=None, active="Default", fg="fl64.exe", focus="playlist", linked=True,
                 targets=None, windows=None, hotspots=None):
        if profiles is None:
            profiles, errors = config.load_profiles(parse_combo, config.BUNDLED_PROFILES)
            assert not errors, errors
        self.out, self.hud, self.fl = RecOut(), RecHud(), []
        self.haptics, self.midi = [], []
        self.replies = {}          # op -> (ok, value) answered to callbacks
        self.engine = Engine(profiles, active, self.out, self._fl, self.hud,
                             haptic=self.haptics.append, targets=targets,
                             midi=lambda s, a, b: self.midi.append((s, a, b)) or True,
                             windows=windows, hotspots=hotspots, parse_combo=parse_combo)
        self.t = 0.0
        self.fg = fg
        self.state = {"focus": focus}
        self.linked = linked
        self.pressed = set()
        self.axes = {}
        self.tick()

    def _fl(self, op, *args, callback=None):
        self.fl.append((op, *args))
        if not self.linked:
            return None
        if callback is not None and op in self.replies:
            reply = self.replies[op]
            callback(*(reply(*args) if callable(reply) else reply))
        return 1

    def tick(self, dt=0.005):
        self.t += dt
        pad = PadState(True, frozenset(self.pressed), dict(self.axes))
        self.engine.tick(self.t, pad, self.fg, self.state, self.linked)

    def wait(self, ms, step=5):
        for _ in range(int(ms / step)):
            self.tick(step / 1000.0)

    def down(self, *buttons):
        self.pressed.update(buttons)
        self.tick()

    def up(self, *buttons):
        self.pressed.difference_update(buttons)
        self.tick()

    def tap(self, *buttons):
        self.down(*buttons)
        self.up(*buttons)


def profile_from(data):
    return {data["name"]: load_profile(data, parse_combo, data["name"])}


VK_CTRL = 0x11
VK_SHIFT = 0x10
