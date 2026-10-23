"""Backend selection: XInput for Xbox pads, SDL2 for everything else."""

import logging

log = logging.getLogger("eflc.input")


class AutoBackend:
    """Prefers an XInput pad; falls back to SDL (PlayStation, Switch, generic)."""

    def __init__(self, use_sdl=True):
        self.xinput = self.sdl = None
        try:
            from .xinput import XInputBackend
            self.xinput = XInputBackend()
        except OSError as e:
            log.warning("XInput unavailable: %s", e)
        if use_sdl:
            try:
                from .sdl_backend import SdlBackend
                self.sdl = SdlBackend()
            except Exception as e:  # missing pysdl2 / DLL: XInput only
                log.warning("SDL backend unavailable: %s", e)
        if self.xinput is None and self.sdl is None:
            raise OSError("no controller backend available")
        self.active = None

    @property
    def name(self):
        if self.active is self.sdl and self.sdl is not None:
            return self.sdl.name
        return "XInput"

    def poll(self, now):
        if self.xinput is not None:
            raw = self.xinput.poll(now)
            if raw is not None:
                self.active = self.xinput
                return raw
        if self.sdl is not None:
            raw = self.sdl.poll(now)
            if raw is not None:
                self.active = self.sdl
                return raw
        self.active = None
        return None

    def rumble(self, low, high):
        if self.active is not None:
            self.active.rumble(low, high)
