"""Engine thread: pad polling, bridge IO, context tracking, haptics, hot-reload.

All engine state lives on this thread. The UI talks to it only through the
thread-safe request queue; the engine talks to the UI only through the HUD sink
(which must be thread-safe, e.g. Qt signals). Grid detection runs on its own
worker thread (vision.tracker) because a window capture takes ~25 ms.
"""

import ctypes
import logging
import os
import queue
import threading
import time

from . import config
from .bridge.midi_bridge import MidiBridge
from .core.engine import Engine
from .core.hotspots import HotspotStore
from .input.pad import Calibrator, PadConfig, PadReader, PadState

log = logging.getLogger("eflc.runtime")

TICK_HZ = 200

# kind -> (heavy motor, light motor, seconds). Short, crisp pulses read as
# "clicks"; the heavy motor is reserved for edges and confirmations.
HAPTICS = {
    "tick": (0.0, 0.34, 0.030),
    "select": (0.0, 0.26, 0.026),
    "press": (0.10, 0.30, 0.030),
    "confirm": (0.28, 0.40, 0.045),
    "open": (0.16, 0.24, 0.040),
    "edge": (0.36, 0.0, 0.045),
}


class Runtime(threading.Thread):
    def __init__(self, settings, hud, out=None, backend=None, fg=None, parse_combo=None, midi_link=None,
                 windows=None, tracker=None):
        super().__init__(name="eflc-engine", daemon=True)
        self.settings = settings
        self.hud = hud
        if out is None or parse_combo is None:
            from .platform.win_output import WinOutput, parse_combo as pc
            out = out or WinOutput()
            parse_combo = parse_combo or pc
        self.out = out
        self.parse_combo = parse_combo
        if fg is None:
            from .platform.win_window import ForegroundTracker
            fg = ForegroundTracker()
        self.fg = fg
        self._backend_arg = backend
        self.backend = None
        self.reader = None
        if midi_link is None:
            from .platform.win_midi import WinMidiLink
            midi_link = WinMidiLink(settings.get("midi_port", "EFLC"))
        self.bridge = MidiBridge(midi_link)
        if windows is None:
            from .platform import fl_windows as windows
        self.windows = windows
        self.hotspots = HotspotStore(os.path.join(config.user_dir(), "hotspots"))
        if tracker is None:
            from .vision.tracker import TargetTracker
            # "ui_magnet" (experimental, off): snap to menus/controls/other apps too
            tracker = TargetTracker(self.hotspots, ui_targets=bool(settings.get("ui_magnet", False)))
        self.tracker = tracker
        self._requests = queue.Queue()
        self._stop = threading.Event()
        self._calibrator = None
        self._profiles_sig = None
        self._next_reload_check = 0.0
        self._rumble_off = None
        self._capture = False
        self._capture_last = None
        self._monitor = False
        self._next_monitor = 0.0
        self.engine = None

    # ------------------------------------------------------------ requests (any thread)
    def request(self, kind, *args):
        self._requests.put((kind, args))

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ haptics (engine thread)
    def haptic(self, kind):
        strength = float(self.settings.get("haptics", 1.0))
        if strength <= 0 or self.backend is None or kind not in HAPTICS:
            return
        low, high, dur = HAPTICS[kind]
        try:
            self.backend.rumble(low * strength, high * strength)
        except Exception:
            return
        self._rumble_off = time.monotonic() + dur

    # ------------------------------------------------------------ thread body
    def run(self):
        winmm = None
        try:
            winmm = ctypes.WinDLL("winmm")
            winmm.timeBeginPeriod(1)  # 1 ms sleep granularity for a steady tick
        except OSError:
            pass
        try:
            self._setup()
            self._loop()
        except Exception:
            log.exception("engine thread crashed")
            self.hud.toast("EFLC engine crashed - see eflc.log")
        finally:
            try:
                self.out.release_all()
                if self.backend is not None:
                    self.backend.rumble(0, 0)
            except Exception:
                pass
            self.bridge.stop()
            try:
                self.tracker.stop()
            except Exception:
                pass
            if winmm is not None:
                winmm.timeEndPeriod(1)

    def _setup(self):
        # SDL must be initialised on the thread that polls it: this one.
        backend = self._backend_arg
        if backend is None:
            try:
                from .input.backends import AutoBackend
                backend = AutoBackend(use_sdl=bool(self.settings.get("sdl", True)))
            except OSError as e:
                log.error("no controller backend: %s", e)
        self.backend = backend
        if backend is not None:
            self.reader = PadReader(backend, PadConfig.from_dict(self.settings.get("pad") or {}))
        profiles, errors = self._load_profiles()
        self.engine = Engine(profiles, self.settings.get("profile"), self.out,
                             self.bridge.command, self.hud,
                             on_calibrate=lambda: self.request("calibrate"),
                             haptic=self.haptic, targets=self.tracker, midi=self.bridge.send_midi,
                             windows=self.windows, hotspots=self.hotspots, parse_combo=self.parse_combo)
        self.engine.apply_tuning(self.settings.get("tuning"))
        self.engine.pointer.magnetic = bool(self.settings.get("magnetic_start", True))
        self._report_errors(errors)
        try:
            self.bridge.start()
        except OSError as e:
            self.hud.toast(str(e))  # keeps retrying in poll()
        if self.reader is None:
            self.hud.toast("No controller support available on this system")
        if not self.tracker.is_alive():
            self.tracker.start()

    def _load_profiles(self):
        self._profiles_sig = config.profiles_signature()
        profiles, errors = config.load_profiles(self.parse_combo)
        if not profiles:
            errors.append("no valid profiles in %s; using the bundled default" % config.profiles_dir())
            profiles, more = config.load_profiles(self.parse_combo, config.BUNDLED_PROFILES)
            errors.extend(more)
        return profiles, errors

    def _report_errors(self, errors):
        for e in errors:
            log.warning("profile: %s", e)
        if errors:
            self.hud.toast("%d profile issue(s) - see eflc.log" % len(errors))

    def _loop(self):
        period = 1.0 / TICK_HZ
        idle = PadState(True, frozenset(), {})
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._drain_requests()

            if self.reader is not None:
                pad = self.reader.poll(t0)
            else:
                pad = PadState(False)

            if self._calibrator is not None and pad.connected:
                if self._calibrator.feed(t0, self.reader.last_raw):
                    self._finish_calibration()
                pad = idle  # ignore input while calibrating
            elif self._capture and pad.connected:
                key = pad.pressed
                if key != self._capture_last:
                    self._capture_last = key
                    self.hud.capture(sorted(key))
                pad = idle  # the mapping editor is listening: don't act on input

            self.bridge.poll()
            fg = self.fg.poll(t0)
            self.engine.tick(t0, pad, fg, self.bridge.state, self.bridge.connected)
            if self.bridge.last_error:
                self.hud.toast(self.bridge.last_error)
                self.bridge.last_error = None

            if self._monitor and t0 >= self._next_monitor:
                self._next_monitor = t0 + 1 / 30.0
                self._emit_monitor(pad)

            if self._rumble_off is not None and t0 >= self._rumble_off:
                self._rumble_off = None
                try:
                    self.backend.rumble(0, 0)
                except Exception:
                    pass

            if t0 >= self._next_reload_check:
                self._next_reload_check = t0 + 1.0
                if config.profiles_signature() != self._profiles_sig:
                    self._reload()

            spare = period - (time.monotonic() - t0)
            if spare > 0:
                time.sleep(spare)

    def _drain_requests(self):
        while True:
            try:
                kind, args = self._requests.get_nowait()
            except queue.Empty:
                return
            if kind == "calibrate":
                if self.reader is None:
                    self.hud.toast("No controller to calibrate")
                else:
                    self._calibrator = Calibrator()
                    self.hud.toast("Calibrating - leave both sticks untouched")
            elif kind == "reload":
                self._reload()
            elif kind == "profile":
                self.engine.switch_profile(args[0], "sticky")
                self._persist_profile()
            elif kind == "hud_model":
                self.engine._hud_key = None  # force a HUD refresh
            elif kind == "capture":
                self._capture = bool(args[0])
                self._capture_last = None
            elif kind == "pointer":
                self.engine.pointer_op(args[0])
            elif kind == "haptics":
                self.settings["haptics"] = float(args[0])
                config.save_settings(self.settings)
            elif kind == "settings":
                self._apply_settings(args[0])
            elif kind == "monitor":
                self._monitor = bool(args[0])
            elif kind == "test_haptic":
                self.haptic(args[0])

    # ------------------------------------------------------------ settings view
    def _apply_settings(self, patch):
        """Merge a settings patch (from the settings window), apply it live and save.

        This thread is the only writer of settings.json, so concurrent edits can't clash.
        """
        def merge(dst, src):  # deep: a deadzone change mustn't drop the stick's calibration
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = v
        merge(self.settings, patch)
        if "pad" in patch and self.reader is not None:
            self.reader.config = PadConfig.from_dict(self.settings.get("pad") or {})
        if "tuning" in patch:
            self.engine.apply_tuning(self.settings.get("tuning"))
        if "magnetic_start" in patch:
            self.engine.pointer.magnetic = bool(patch["magnetic_start"])
            self.engine._hud_key = None
        config.save_settings(self.settings)

    def _emit_monitor(self, pad):
        raw = self.reader.last_raw if self.reader is not None else None
        self.hud.monitor({
            "connected": bool(pad.connected and raw is not None),
            "backend": getattr(self.backend, "name", ""),
            "raw": {"LS": (raw.lx, raw.ly), "RS": (raw.rx, raw.ry), "LT": raw.lt, "RT": raw.rt} if raw else {},
            "shaped": dict(pad.axes),
            "pressed": sorted(pad.pressed),
            "fl": self.bridge.connected,
            "midi_open": bool(getattr(self.bridge.link, "is_open", False)),
        })

    def _reload(self):
        profiles, errors = self._load_profiles()
        self.engine.set_profiles(profiles)
        self._report_errors(errors)
        if not errors:
            self.hud.toast("Profiles reloaded")

    def _persist_profile(self):
        self.settings["profile"] = self.engine.profile.name
        config.save_settings(self.settings)

    def _finish_calibration(self):
        report = self._calibrator.apply(self.reader.config)
        self._calibrator = None
        self.settings["pad"] = self.reader.config.to_dict()
        config.save_settings(self.settings)
        self.hud.settings_changed(dict(self.settings))
        parts = ["%s dz %.2f" % (s, r[2]) for s, r in report.items()]
        log.info("calibration: %s", report)
        self.hud.toast("Sticks calibrated: " + ", ".join(parts))
