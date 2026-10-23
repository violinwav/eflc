"""Keyboard and mouse injection via SendInput (works for FL and any other app)."""

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "lctrl": 0xA2, "rctrl": 0xA3, "lshift": 0xA0, "rshift": 0xA1, "lalt": 0xA4, "ralt": 0xA5,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "plus": 0xBB, "equals": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF, "backslash": 0xDC, "semicolon": 0xBA, "quote": 0xDE,
    "lbracket": 0xDB, "rbracket": 0xDD, "backtick": 0xC0, "menu": 0x5D, "apps": 0x5D,
    "numplus": 0x6B, "numminus": 0x6D, "nummul": 0x6A, "numdiv": 0x6F,
    "capslock": 0x14, "printscreen": 0x2C, "pause": 0x13,
}
for _i in range(1, 25):
    VK["f%d" % _i] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK[_c] = ord(_c.upper())
for _i in range(10):
    VK["num%d" % _i] = 0x60 + _i

EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0xA3, 0xA5, 0x6F, 0x5B, 0x5D}


def parse_combo(combo):
    """'ctrl+shift+s' -> [vk, vk, vk]. Raises ValueError on unknown keys."""
    keys = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part == "" and "++" in combo:
            part = "plus"
        if part not in VK:
            raise ValueError("unknown key %r in %r" % (part, combo))
        keys.append(VK[part])
    return keys


def _key_input(vk, up):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def _send(inputs):
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse_input(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    return inp


_MOUSE_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


class WinOutput:
    """Output sink used by the engine. A recording twin lives in tests."""

    def __init__(self):
        self._held_keys = {}  # vk -> refcount, so overlapping holds don't release early

    # keyboard ------------------------------------------------------------
    def key_down(self, vks):
        ins = []
        for vk in vks:
            n = self._held_keys.get(vk, 0)
            self._held_keys[vk] = n + 1
            if n == 0:
                ins.append(_key_input(vk, False))
        _send(ins)

    def key_up(self, vks):
        ins = []
        for vk in reversed(vks):
            n = self._held_keys.get(vk, 0)
            if n <= 1:
                self._held_keys.pop(vk, None)
                ins.append(_key_input(vk, True))
            else:
                self._held_keys[vk] = n - 1
        _send(ins)

    def key_tap(self, vks):
        # Modifiers that are already held by a key-modifier button stay down.
        down = [_key_input(vk, False) for vk in vks if vk not in self._held_keys]
        up = [_key_input(vk, True) for vk in reversed(vks) if vk not in self._held_keys]
        _send(down + up)

    def type_text(self, text):
        ins = []
        for ch in text:
            for up in (False, True):
                inp = INPUT(type=INPUT_KEYBOARD)
                inp.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
                ins.append(inp)
        _send(ins)

    def release_all(self):
        vks = list(self._held_keys)
        self._held_keys.clear()
        _send([_key_input(vk, True) for vk in vks])

    # mouse ---------------------------------------------------------------
    def mouse_button(self, button, down):
        d, u = _MOUSE_FLAGS[button]
        _send([_mouse_input(d if down else u)])

    def mouse_click(self, button):
        d, u = _MOUSE_FLAGS[button]
        _send([_mouse_input(d), _mouse_input(u)])

    def cursor_pos(self):
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def mouse_move_to(self, x, y):
        # Absolute move: immune to Windows pointer acceleration.
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 1)
        vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 1)
        x = min(max(x, vx), vx + vw)
        y = min(max(y, vy), vy + vh)
        nx = round((x - vx) * 65535 / vw)
        ny = round((y - vy) * 65535 / vh)
        _send([_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)])

    def wheel(self, notches, horizontal=False):
        self.wheel_delta(int(notches) * 120, horizontal)

    def wheel_delta(self, delta, horizontal=False):
        """Raw wheel delta (120 = one notch). Fractions scroll smoothly in apps that
        honour them (FL does, both directions); HWHEEL > 0 scrolls right."""
        flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        _send([_mouse_input(flag, data=int(delta))])
