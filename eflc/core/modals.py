"""Modal HUD controllers: list menus, the window wheel, the on-screen keyboard.

While a modal is open it receives every button edge and the raw sticks; the
normal bindings are paused. Each modal exposes view() -> ModalView, a plain
snapshot the Qt HUD renders (and animates) on its own thread.

Conventions inside modals
  D-pad / left stick   move the selection (auto-repeat, faster the further you push)
  A                    activate          B   back / close
  LEFT / RIGHT         adjust a value (parameters, scale root)
  right stick X        fine, continuous adjust (parameters)
"""

import math
from dataclasses import dataclass, field

NAV = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}


@dataclass
class MenuItem:
    label: str
    action: object = None        # Action (profile-defined) to run
    call: object = None          # python callable(modal) for built-in features
    detail: str = ""             # right-aligned info: shortcut, value text
    kind: str = "action"         # action | submenu | param | header | info
    submenu: object = None       # callable(engine) -> Modal
    value: float = None          # 0..1 for "param" items (drawn as a bar)
    data: object = None
    keep_open: bool = False
    icon: str = ""


@dataclass
class ModalView:
    kind: str
    title: str
    items: tuple = ()            # ((label, detail, kind, value, icon), ...)
    selected: int = 0
    subtitle: str = ""
    hint: str = ""
    activated: int = 0           # bumps on activation: HUD plays the press animation
    loading: bool = False
    aim: float = None            # wheel: stick angle (radians, 0 = up, clockwise)
    keys: tuple = ()             # keyboard: rows of (label, width)
    shift: bool = False
    text: str = ""
    depth: int = 0


class Repeat:
    """Auto-repeat for held directions; stick deflection speeds it up."""

    def __init__(self, delay=0.30, rate=0.085, fastest=0.035):
        self.delay, self.rate, self.fastest = delay, rate, fastest
        self.held = {}

    def press(self, key, now):
        self.held[key] = now + self.delay

    def release(self, key):
        self.held.pop(key, None)

    def due(self, now, speed=1.0):
        out = []
        for key, t in list(self.held.items()):
            if now >= t:
                out.append(key)
                self.held[key] = now + max(self.fastest, self.rate / max(0.3, speed))
        return out


class Modal:
    kind = "modal"

    def __init__(self, engine, owner=None):
        self.e = engine
        self.owner = owner
        self.closed = False
        self.activated = 0
        self.repeat = Repeat()
        self.seen = set([owner]) if owner else set()
        self._stick_dirs = set()

    # engine -> modal
    def press(self, b, now):
        self.seen.add(b)
        if b in NAV:
            self.repeat.press(b, now)
            self.nav(*NAV[b])
        else:
            self.button(b, now)

    def release(self, b, now):
        if b not in self.seen:
            return
        self.repeat.release(b)
        if b == self.owner:
            self.owner_released(now)

    def tick(self, now, dt, pad):
        x, y = pad.axes.get("LS", (0.0, 0.0))
        self._stick_nav(x, y, now)
        for key in self.repeat.due(now, speed=1.0 + 1.5 * max(abs(x), abs(y))):
            if key in NAV:
                self.nav(*NAV[key])
            elif key.startswith("LS_"):
                self.nav(*NAV[key[3:]])

    def _stick_nav(self, x, y, now):
        want = set()
        if y > 0.55:
            want.add("LS_UP")
        elif y < -0.55:
            want.add("LS_DOWN")
        if x > 0.6:
            want.add("LS_RIGHT")
        elif x < -0.6:
            want.add("LS_LEFT")
        for k in want - self._stick_dirs:
            self.repeat.press(k, now)
            self.nav(*NAV[k[3:]])
        for k in self._stick_dirs - want:
            self.repeat.release(k)
        self._stick_dirs = want

    # overridables
    def nav(self, dx, dy):
        pass

    def button(self, b, now):
        if b == "B":
            self.close()

    def owner_released(self, now):
        pass

    def close(self):
        self.closed = True

    def view(self):
        raise NotImplementedError


class ListModal(Modal):
    kind = "list"

    def __init__(self, engine, title, items=(), subtitle="", hint="A select · B back",
                 loader=None, on_adjust=None, on_x=None, on_move=None, owner=None):
        super().__init__(engine, owner)
        self.title, self.subtitle, self.hint = title, subtitle, hint
        self.items = list(items)
        self.selected = self._first_selectable(0, 1)
        self.loading = loader is not None
        self.on_adjust = on_adjust      # callable(modal, item, delta, fine) for LEFT/RIGHT/RS
        self.on_x = on_x                # callable(modal, item)
        self.on_move = on_move          # callable(modal, old_index, new_index)
        self._fine_acc = 0.0
        if loader is not None:
            loader(self)

    def set_items(self, items, keep=True):
        sel = self.selected
        self.items = list(items)
        self.loading = False
        self.selected = min(sel, len(self.items) - 1) if keep and self.items else self._first_selectable(0, 1)
        if self.items and self.items[self.selected].kind == "header":
            self.selected = self._first_selectable(self.selected, 1)

    def _first_selectable(self, start, step):
        i = start
        while 0 <= i < len(self.items):
            if self.items[i].kind not in ("header", "info"):
                return i
            i += step
        return max(0, min(start, len(self.items) - 1))

    @property
    def item(self):
        return self.items[self.selected] if 0 <= self.selected < len(self.items) else None

    def nav(self, dx, dy):
        if dy:
            old = self.selected
            i = self.selected + dy
            while 0 <= i < len(self.items) and self.items[i].kind in ("header", "info"):
                i += dy
            if 0 <= i < len(self.items):
                self.selected = i
                self.e.haptic("select")
                if self.on_move:
                    self.on_move(self, old, i)
            else:
                self.e.haptic("edge")
        elif dx and self.on_adjust and self.item is not None:
            self.on_adjust(self, self.item, dx, False)

    def button(self, b, now):
        if b in ("A", "START"):
            self.activate()
        elif b == "B":
            self.close()
        elif b == "X" and self.on_x and self.item is not None:
            self.on_x(self, self.item)
        elif b in ("LB", "RB"):
            for _ in range(8):
                self.nav(0, -1 if b == "LB" else 1)

    def activate(self):
        it = self.item
        if it is None or it.kind in ("header", "info"):
            return
        self.activated += 1
        self.e.haptic("confirm")
        if it.kind == "submenu" and it.submenu is not None:
            self.e.push_modal(it.submenu(self.e))
            return
        if it.call is not None:
            it.call(self)
        if it.action is not None:
            # close first: the action may open a modal of its own (e.g. a submenu feature)
            if not it.keep_open:
                self.e.close_modals()
            self.e.run_action(it.action)
        elif not it.keep_open and it.call is None:
            self.e.close_modals()

    def tick(self, now, dt, pad):
        super().tick(now, dt, pad)
        rx = pad.axes.get("RS", (0.0, 0.0))[0]
        if self.on_adjust and self.item is not None and abs(rx) > 0.15:
            self._fine_acc += rx * dt
            if abs(self._fine_acc) >= 0.02:  # ~50 updates/s at full deflection
                self.on_adjust(self, self.item, self._fine_acc, True)
                self._fine_acc = 0.0

    def view(self):
        return ModalView(
            "list", self.title,
            tuple((it.label, it.detail, it.kind, it.value, it.icon) for it in self.items),
            self.selected, self.subtitle, self.hint, self.activated, self.loading,
            depth=len(self.e.modals) - 1)


@dataclass
class WheelItem:
    label: str
    icon: str
    action: object = None
    call: object = None
    active: bool = False


class WheelModal(Modal):
    """Radial picker: aim with either stick, release the opening button (or A) to pick."""
    kind = "wheel"

    def __init__(self, engine, title, items, owner=None):
        super().__init__(engine, owner)
        self.title = title
        self.items = list(items)
        self.selected = next((i for i, it in enumerate(self.items) if it.active), 0)
        self.aim = None
        self.touched = False

    def _sector(self, angle):
        n = len(self.items)
        return int(((angle + math.pi / n) % (2 * math.pi)) / (2 * math.pi / n)) % n

    def tick(self, now, dt, pad):
        best = None
        for stick in ("RS", "LS"):
            x, y = pad.axes.get(stick, (0.0, 0.0))
            m = math.hypot(x, y)
            if m > 0.45 and (best is None or m > best[0]):
                best = (m, x, y)
        if best is not None and self.items:
            _, x, y = best
            self.aim = math.atan2(x, y) % (2 * math.pi)   # 0 = up, clockwise
            sel = self._sector(self.aim)
            if sel != self.selected:
                self.selected = sel
                self.e.haptic("select")
            self.touched = True
        else:
            self.aim = None

    def nav(self, dx, dy):
        if not self.items:
            return
        step = dx if dx else dy
        self.selected = (self.selected + step) % len(self.items)
        self.touched = True
        self.e.haptic("select")

    def button(self, b, now):
        if b == "A":
            self.pick()
        elif b == "B":
            self.close()

    def owner_released(self, now):
        if self.touched:
            self.pick()
        else:
            self.close()

    def pick(self):
        if not self.items:
            self.close()
            return
        it = self.items[self.selected]
        self.activated += 1
        self.e.haptic("confirm")
        self.e.close_modals()
        if it.call is not None:
            it.call()
        if it.action is not None:
            self.e.run_action(it.action)

    def view(self):
        return ModalView("wheel", self.title,
                         tuple((it.label, "", "wheel", 1.0 if it.active else None, it.icon) for it in self.items),
                         self.selected, "", "Aim a stick · release to switch · B cancel",
                         self.activated, aim=self.aim)


KEY_ROWS = (
    tuple((c, 1) for c in "1234567890"),
    tuple((c, 1) for c in "qwertyuiop"),
    tuple((c, 1) for c in "asdfghjkl'"),
    tuple((c, 1) for c in "zxcvbnm,.-"),
    (("Shift", 2), ("Space", 4), ("⌫", 2), ("Enter", 2)),
)
SHIFTED = dict(zip("1234567890',.-", "!@#$%^&*()\"<>_"))


class KeyboardModal(Modal):
    """On-screen keyboard that types into whatever has focus (e.g. FL's plugin search)."""
    kind = "keyboard"

    def __init__(self, engine, owner=None):
        super().__init__(engine, owner)
        self.row, self.col = 1, 0
        self.shift = False
        self.text = ""

    def _row_len(self, r):
        return len(KEY_ROWS[r])

    def nav(self, dx, dy):
        if dy:
            # keep roughly the same horizontal position across rows of different widths
            frac = (self._x_center(self.row, self.col)) / self._row_width(self.row)
            self.row = (self.row + dy) % len(KEY_ROWS)
            self.col = self._col_at(self.row, frac * self._row_width(self.row))
        if dx:
            self.col = (self.col + dx) % self._row_len(self.row)
        self.e.haptic("select")

    def _row_width(self, r):
        return sum(w for _, w in KEY_ROWS[r])

    def _x_center(self, r, c):
        return sum(w for _, w in KEY_ROWS[r][:c]) + KEY_ROWS[r][c][1] / 2.0

    def _col_at(self, r, x):
        acc = 0
        for i, (_, w) in enumerate(KEY_ROWS[r]):
            if acc <= x < acc + w:
                return i
            acc += w
        return self._row_len(r) - 1

    def button(self, b, now):
        if b == "A":
            self.press_key(KEY_ROWS[self.row][self.col][0])
        elif b == "X":
            self.press_key("⌫")
        elif b == "Y":
            self.press_key("Space")
        elif b == "START":
            self.press_key("Enter")
            self.close()
        elif b in ("LB", "RB", "LS"):
            self.shift = not self.shift
            self.e.haptic("select")
        elif b == "B":
            self.close()

    def press_key(self, label):
        self.activated += 1
        self.e.haptic("confirm")
        out = self.e.out
        if label == "Shift":
            self.shift = not self.shift
        elif label == "Space":
            out.type_text(" ")
            self.text += " "
        elif label == "⌫":
            out.key_tap(self.e.parse_combo("backspace"))
            self.text = self.text[:-1]
        elif label == "Enter":
            out.key_tap(self.e.parse_combo("enter"))
            self.text = ""
        else:
            ch = (SHIFTED.get(label, label.upper()) if self.shift else label)
            out.type_text(ch)
            self.text += ch
            if self.shift:
                self.shift = False
        self.text = self.text[-40:]

    def view(self):
        rows = tuple(tuple((SHIFTED.get(k, k.upper()) if self.shift and len(k) == 1 else k, w) for k, w in row)
                     for row in KEY_ROWS)
        sel = sum(self._row_len(r) for r in range(self.row)) + self.col
        return ModalView("keyboard", "Keyboard", (), sel, "",
                         "A type · X ⌫ · Y space · LB shift · Start enter · B close",
                         self.activated, keys=rows, shift=self.shift, text=self.text)
