"""SDL2 GameController backend: DualSense / DualShock 4 / Switch Pro / generic pads.

Gives the same RawPad as XInput plus the touchpad finger (RawPad.touch) and
rumble. SDL must be initialised and pumped on the thread that polls (the
engine thread), and needs background events enabled because EFLC never has
keyboard focus.
"""

import ctypes
import logging

from .xinput import RawPad

log = logging.getLogger("eflc.input")

_BUTTONS = None


def _load():
    global _BUTTONS
    import sdl2
    _BUTTONS = {
        sdl2.SDL_CONTROLLER_BUTTON_A: "A", sdl2.SDL_CONTROLLER_BUTTON_B: "B",
        sdl2.SDL_CONTROLLER_BUTTON_X: "X", sdl2.SDL_CONTROLLER_BUTTON_Y: "Y",
        sdl2.SDL_CONTROLLER_BUTTON_BACK: "BACK", sdl2.SDL_CONTROLLER_BUTTON_GUIDE: "GUIDE",
        sdl2.SDL_CONTROLLER_BUTTON_START: "START",
        sdl2.SDL_CONTROLLER_BUTTON_LEFTSTICK: "LS", sdl2.SDL_CONTROLLER_BUTTON_RIGHTSTICK: "RS",
        sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER: "LB", sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER: "RB",
        sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP: "UP", sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN: "DOWN",
        sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT: "LEFT", sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT: "RIGHT",
    }
    for attr, name in (("SDL_CONTROLLER_BUTTON_MISC1", "MISC"), ("SDL_CONTROLLER_BUTTON_TOUCHPAD", "TOUCHPAD"),
                       ("SDL_CONTROLLER_BUTTON_PADDLE1", "P1"), ("SDL_CONTROLLER_BUTTON_PADDLE2", "P2"),
                       ("SDL_CONTROLLER_BUTTON_PADDLE3", "P3"), ("SDL_CONTROLLER_BUTTON_PADDLE4", "P4")):
        if hasattr(sdl2, attr):
            _BUTTONS[getattr(sdl2, attr)] = name
    return sdl2


class SdlBackend:
    def __init__(self, rescan_interval=1.0):
        sdl2 = _load()
        self.sdl2 = sdl2
        for hint, val in ((sdl2.SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS, b"1"),
                          (b"SDL_JOYSTICK_HIDAPI_PS4_RUMBLE", b"1"),
                          (b"SDL_JOYSTICK_HIDAPI_PS5_RUMBLE", b"1")):
            sdl2.SDL_SetHint(hint, val)
        if sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_EVENTS) != 0:
            raise OSError("SDL init failed: %s" % sdl2.SDL_GetError().decode(errors="replace"))
        self.ctrl = None
        self.pad_name = ""
        self._next_scan = 0.0
        self._rescan = rescan_interval

    @property
    def name(self):
        return "SDL (%s)" % self.pad_name if self.pad_name else "SDL"

    def _open(self):
        s = self.sdl2
        for i in range(s.SDL_NumJoysticks()):
            if s.SDL_IsGameController(i):
                c = s.SDL_GameControllerOpen(i)
                if c:
                    self.ctrl = c
                    raw = s.SDL_GameControllerName(c)
                    self.pad_name = raw.decode(errors="replace") if raw else "controller"
                    log.info("SDL controller: %s", self.pad_name)
                    return True
        return False

    def poll(self, now):
        s = self.sdl2
        s.SDL_PumpEvents()
        if self.ctrl is not None and not s.SDL_GameControllerGetAttached(self.ctrl):
            s.SDL_GameControllerClose(self.ctrl)
            self.ctrl, self.pad_name = None, ""
        if self.ctrl is None:
            if now < self._next_scan:
                return None
            self._next_scan = now + self._rescan
            if not self._open():
                return None
        c = self.ctrl
        pressed = frozenset(name for b, name in _BUTTONS.items() if s.SDL_GameControllerGetButton(c, b))

        def axis(a):
            v = s.SDL_GameControllerGetAxis(c, a)
            return v / 32767.0 if v >= 0 else v / 32768.0

        touch = None
        try:
            if s.SDL_GameControllerGetNumTouchpads(c) > 0:
                st, x, y, pr = ctypes.c_uint8(), ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
                if s.SDL_GameControllerGetTouchpadFinger(c, 0, 0, ctypes.byref(st), ctypes.byref(x),
                                                        ctypes.byref(y), ctypes.byref(pr)) == 0 and st.value:
                    touch = (float(x.value), float(y.value))
        except AttributeError:
            pass
        return RawPad(
            pressed,
            axis(s.SDL_CONTROLLER_AXIS_LEFTX), -axis(s.SDL_CONTROLLER_AXIS_LEFTY),
            axis(s.SDL_CONTROLLER_AXIS_RIGHTX), -axis(s.SDL_CONTROLLER_AXIS_RIGHTY),
            max(0.0, axis(s.SDL_CONTROLLER_AXIS_TRIGGERLEFT)), max(0.0, axis(s.SDL_CONTROLLER_AXIS_TRIGGERRIGHT)),
            touch,
        )

    def rumble(self, low, high):
        if self.ctrl is not None:
            self.sdl2.SDL_GameControllerRumble(self.ctrl, int(max(0, min(1, low)) * 65535),
                                               int(max(0, min(1, high)) * 65535), 500)

    def close(self):
        if self.ctrl is not None:
            self.sdl2.SDL_GameControllerClose(self.ctrl)
        self.sdl2.SDL_Quit()
