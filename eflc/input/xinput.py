"""XInput backend (Xbox 360 / One / Series pads) via ctypes. No dependencies."""

import ctypes
from ctypes import wintypes

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167

BUTTON_MASKS = {
    "UP": 0x0001,
    "DOWN": 0x0002,
    "LEFT": 0x0004,
    "RIGHT": 0x0008,
    "START": 0x0010,
    "BACK": 0x0020,
    "LS": 0x0040,
    "RS": 0x0080,
    "LB": 0x0100,
    "RB": 0x0200,
    "GUIDE": 0x0400,
    "A": 0x1000,
    "B": 0x2000,
    "X": 0x4000,
    "Y": 0x8000,
}


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wintypes.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", wintypes.DWORD),
        ("Gamepad", XINPUT_GAMEPAD),
        ("_reserved", wintypes.DWORD),  # padding used by XInputGetStateEx
    ]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", wintypes.WORD), ("wRightMotorSpeed", wintypes.WORD)]


class RawPad:
    """One raw XInput sample, normalised to floats."""

    __slots__ = ("buttons", "lx", "ly", "rx", "ry", "lt", "rt", "touch")

    def __init__(self, buttons=frozenset(), lx=0.0, ly=0.0, rx=0.0, ry=0.0, lt=0.0, rt=0.0, touch=None):
        self.buttons = buttons
        self.lx, self.ly, self.rx, self.ry = lx, ly, rx, ry
        self.lt, self.rt = lt, rt
        self.touch = touch  # (x, y) in 0..1 while a finger is on the touchpad (SDL pads)


def _axis(v):
    return v / 32767.0 if v >= 0 else v / 32768.0


class XInputBackend:
    name = "XInput"

    def __init__(self, rescan_interval=1.0):
        self._dll = None
        for lib in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._dll = ctypes.WinDLL(lib)
                break
            except OSError:
                continue
        if self._dll is None:
            raise OSError("No XInput DLL found")
        # Ordinal 100 = XInputGetStateEx (includes the Guide button).
        try:
            self._get = self._dll[100]
        except (AttributeError, OSError):
            self._get = self._dll.XInputGetState
        self._get.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
        self._get.restype = wintypes.DWORD
        self._set = self._dll.XInputSetState
        self._set.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_VIBRATION)]
        self._set.restype = wintypes.DWORD
        self._state = XINPUT_STATE()
        self.index = None
        self._rescan_interval = rescan_interval
        self._next_scan = 0.0

    @property
    def connected(self):
        return self.index is not None

    def rumble(self, low, high):
        """Motor speeds 0..1 (left = heavy/low, right = light/high). Stays on until reset."""
        if self.index is None:
            return
        v = XINPUT_VIBRATION(int(max(0.0, min(1.0, low)) * 65535), int(max(0.0, min(1.0, high)) * 65535))
        self._set(self.index, ctypes.byref(v))

    def poll(self, now):
        """Return RawPad or None when no pad is connected."""
        if self.index is None:
            # Polling empty slots is slow, so only rescan periodically.
            if now < self._next_scan:
                return None
            self._next_scan = now + self._rescan_interval
            for i in range(4):
                if self._get(i, ctypes.byref(self._state)) == ERROR_SUCCESS:
                    self.index = i
                    break
            else:
                return None
        elif self._get(self.index, ctypes.byref(self._state)) != ERROR_SUCCESS:
            self.index = None
            return None
        g = self._state.Gamepad
        pressed = frozenset(n for n, m in BUTTON_MASKS.items() if g.wButtons & m)
        return RawPad(
            pressed,
            _axis(g.sThumbLX), _axis(g.sThumbLY),
            _axis(g.sThumbRX), _axis(g.sThumbRY),
            g.bLeftTrigger / 255.0, g.bRightTrigger / 255.0,
        )
