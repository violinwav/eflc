"""Settings view for manual adjustments.

Everything applies live and is saved to %APPDATA%\\EFLC\\settings.json through
the engine thread (the single writer). Tuning is stored as overrides on top of
the profile, so bundled profiles keep upgrading. Items marked "restart" are read
at start-up only.
"""

import os
import sys

from PySide6.QtCore import QPointF, QProcess, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QRadioButton, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from .. import config

PAD_DEFAULTS = {"deadzone": 0.20, "outer": 0.95, "curve": 1.6}
TIMING_DEFAULTS = {"hold_ms": 400, "double_ms": 250, "repeat_delay_ms": 350, "repeat_rate_ms": 70}
HUD_DEFAULTS = {"style": "card", "corner": "bottom-right", "scale": 1.0, "opacity": 0.92}


class ValueSlider(QWidget):
    """Slider over a float range with a live value label."""

    def __init__(self, lo, hi, step, value, fmt="{:.2f}", on_change=None):
        super().__init__()
        self.lo, self.step, self.fmt = lo, step, fmt
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, int(round((hi - lo) / step)))
        self.label = QLabel()
        self.label.setMinimumWidth(64)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.label)
        self.on_change = on_change
        self.set_value(value)
        self.slider.valueChanged.connect(self._changed)

    def value(self):
        return round(self.lo + self.slider.value() * self.step, 4)

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round((float(v) - self.lo) / self.step)))
        self.slider.blockSignals(False)
        self.label.setText(self.fmt.format(self.value()))

    def _changed(self, _):
        self.label.setText(self.fmt.format(self.value()))
        if self.on_change:
            self.on_change(self.value())


class StickView(QWidget):
    """Live stick plot: full range, outer edge, deadzone (around the calibrated
    centre), the raw reading (grey) and what EFLC uses after shaping (accent)."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.setMinimumSize(170, 170)
        self.raw = (0.0, 0.0)
        self.shaped = (0.0, 0.0)
        self.cfg = dict(PAD_DEFAULTS, center=(0.0, 0.0))
        self.trail = []

    def update_values(self, raw, shaped, cfg):
        self.raw, self.shaped, self.cfg = raw, shaped, cfg
        self.trail = (self.trail + [raw])[-40:]
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 10
        c = QPointF(self.width() / 2, self.height() / 2)
        R = side / 2
        pal = self.palette()
        fg = pal.windowText().color()
        p.setPen(QPen(QColor(fg.red(), fg.green(), fg.blue(), 70), 1))
        p.setBrush(QColor(fg.red(), fg.green(), fg.blue(), 12))
        p.drawEllipse(c, R, R)
        p.drawLine(QPointF(c.x() - R, c.y()), QPointF(c.x() + R, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - R), QPointF(c.x(), c.y() + R))
        # outer edge
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(120, 160, 255, 140), 1, Qt.DashLine))
        p.drawEllipse(c, R * self.cfg["outer"], R * self.cfg["outer"])
        # deadzone around the calibrated centre
        cx, cy = self.cfg.get("center", (0.0, 0.0))
        dz_c = QPointF(c.x() + cx * R, c.y() - cy * R)
        p.setPen(QPen(QColor(255, 170, 60, 200), 1.2))
        p.setBrush(QColor(255, 170, 60, 40))
        p.drawEllipse(dz_c, R * self.cfg["deadzone"], R * self.cfg["deadzone"])
        # raw trail + raw dot
        for i, (x, y) in enumerate(self.trail):
            a = int(15 + 80 * i / max(1, len(self.trail)))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(160, 160, 160, a))
            p.drawEllipse(QPointF(c.x() + x * R, c.y() - y * R), 1.8, 1.8)
        rx, ry = self.raw
        p.setBrush(QColor(170, 170, 170))
        p.drawEllipse(QPointF(c.x() + rx * R, c.y() - ry * R), 4, 4)
        # shaped output
        sx, sy = self.shaped
        p.setPen(QPen(QColor(139, 124, 255), 2))
        p.drawLine(c, QPointF(c.x() + sx * R, c.y() - sy * R))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(139, 124, 255))
        p.drawEllipse(QPointF(c.x() + sx * R, c.y() - sy * R), 5, 5)
        p.setPen(fg)
        p.drawText(QRectF(4, 2, 60, 18), Qt.AlignLeft | Qt.AlignVCenter, self.name)
        p.end()


class TriggerBar(QWidget):
    def __init__(self, name):
        super().__init__()
        self.name, self.value, self.press, self.release = name, 0.0, 0.5, 0.35
        self.setMinimumHeight(22)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width() - 40, self.height() - 8
        x0, y0 = 36, 4
        fg = self.palette().windowText().color()
        p.setPen(fg)
        p.drawText(QRectF(0, 0, 32, self.height()), Qt.AlignLeft | Qt.AlignVCenter, self.name)
        p.setPen(QPen(QColor(fg.red(), fg.green(), fg.blue(), 70), 1))
        p.setBrush(QColor(fg.red(), fg.green(), fg.blue(), 14))
        p.drawRoundedRect(QRectF(x0, y0, w, h), 4, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(139, 124, 255) if self.value >= self.press else QColor(150, 150, 150))
        p.drawRoundedRect(QRectF(x0, y0, w * max(0.0, min(1.0, self.value)), h), 4, 4)
        for v, col in ((self.press, QColor(255, 170, 60)), (self.release, QColor(120, 160, 255))):
            p.setPen(QPen(col, 2))
            p.drawLine(QPointF(x0 + w * v, y0 - 2), QPointF(x0 + w * v, y0 + h + 2))
        p.end()


class SettingsWindow(QWidget):
    def __init__(self, runtime, apply_hud):
        super().__init__(None, Qt.Window)
        self.runtime = runtime
        self.apply_hud = apply_hud          # callable(hud_settings) on the Qt side
        self.setWindowTitle("EFLC settings")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(760, 640)
        self.s = config.load_settings()
        self.s.update({k: v for k, v in runtime.settings.items()})
        self._pending = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        self._build()
        self.runtime.request("monitor", True)

    # ------------------------------------------------------------ plumbing
    def _patch(self, patch):
        """Queue a settings patch; sent to the engine thread 120 ms after the last change."""
        def merge(dst, src):
            for k, v in src.items():
                if isinstance(v, dict):
                    merge(dst.setdefault(k, {}), v)
                else:
                    dst[k] = v
        merge(self._pending, patch)
        merge(self.s, patch)
        if "hud" in patch:
            self.apply_hud(dict(self.s.get("hud", {})))
        self._timer.start(120)

    def _flush(self):
        if self._pending:
            self.runtime.request("settings", self._pending)
            self._pending = {}

    def closeEvent(self, e):
        self._flush()
        self.runtime.request("monitor", False)
        super().closeEvent(e)

    def _pad(self, stick):
        return (self.s.get("pad") or {}).get("sticks", {}).get(stick, {})

    # ------------------------------------------------------------ layout
    def _build(self):
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._controller_tab(), "Controller")
        self.tabs.addTab(self._feel_tab(), "Pointer and feel")
        self.tabs.addTab(self._timing_tab(), "Timing")
        self.tabs.addTab(self._hud_tab(), "HUD")
        self.tabs.addTab(self._connection_tab(), "Connection")
        row = QHBoxLayout()
        self.note = QLabel("Changes apply immediately and are saved.")
        row.addWidget(self.note, 1)
        reset = QPushButton("Reset this tab")
        reset.clicked.connect(self._reset_tab)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(reset)
        row.addWidget(close)
        root.addLayout(row)

    def _controller_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.status = QLabel("Waiting for the controller…")
        lay.addWidget(self.status)
        grid = QGridLayout()
        self.stick_views, self.stick_sliders = {}, {}
        for col, stick in enumerate(("LS", "RS")):
            box = QGroupBox("Left stick" if stick == "LS" else "Right stick")
            v = QVBoxLayout(box)
            view = StickView(stick)
            v.addWidget(view, 1)
            self.stick_views[stick] = view
            form = QFormLayout()
            cfg = {**PAD_DEFAULTS, **self._pad(stick)}
            sl = {
                "deadzone": ValueSlider(0.05, 0.50, 0.01, cfg["deadzone"],
                                        on_change=lambda v, s=stick: self._stick(s, "deadzone", v)),
                "outer": ValueSlider(0.70, 1.00, 0.01, cfg["outer"],
                                     on_change=lambda v, s=stick: self._stick(s, "outer", v)),
                "curve": ValueSlider(1.0, 3.0, 0.05, cfg["curve"],
                                     on_change=lambda v, s=stick: self._stick(s, "curve", v)),
            }
            form.addRow("Deadzone", sl["deadzone"])
            form.addRow("Full tilt at", sl["outer"])
            form.addRow("Response curve", sl["curve"])
            v.addLayout(form)
            hint = QLabel("Grey: raw reading · orange: deadzone · violet: what EFLC uses.\n"
                          "Curve 1 = linear; higher = finer control near the centre.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: gray")
            v.addWidget(hint)
            self.stick_sliders[stick] = sl
            grid.addWidget(box, 0, col)
        lay.addLayout(grid, 1)
        row = QHBoxLayout()
        cal = QPushButton("Calibrate sticks (hands off for 1.5 s)")
        cal.clicked.connect(lambda: self.runtime.request("calibrate"))
        recentre = QPushButton("Forget calibration")
        recentre.clicked.connect(self._forget_centres)
        row.addWidget(cal)
        row.addWidget(recentre)
        row.addStretch(1)
        lay.addLayout(row)
        trig = QGroupBox("Triggers and stick directions")
        tf = QFormLayout(trig)
        pad = self.s.get("pad") or {}
        self.trig_bars = {"LT": TriggerBar("LT"), "RT": TriggerBar("RT")}
        tf.addRow(self.trig_bars["LT"])
        tf.addRow(self.trig_bars["RT"])
        self.trig_press = ValueSlider(0.10, 0.95, 0.01, pad.get("trigger_press", 0.5),
                                      on_change=lambda v: self._pad_value("trigger_press", v))
        self.trig_release = ValueSlider(0.05, 0.90, 0.01, pad.get("trigger_release", 0.35),
                                        on_change=lambda v: self._pad_value("trigger_release", v))
        self.dir_press = ValueSlider(0.30, 0.95, 0.01, pad.get("dir_press", 0.6),
                                     on_change=lambda v: self._pad_value("dir_press", v))
        tf.addRow("Trigger counts as pressed at", self.trig_press)
        tf.addRow("…and released below", self.trig_release)
        tf.addRow("Stick direction counts as a press at", self.dir_press)
        self.pressed_label = QLabel("Pressed: —")
        tf.addRow(self.pressed_label)
        lay.addWidget(trig)
        return w

    def _feel_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        t = self.s.get("tuning") or {}
        self.pointer_speed = ValueSlider(0.3, 2.5, 0.05, t.get("pointer_speed", 1.0), "{:.2f}×",
                                         lambda v: self._patch({"tuning": {"pointer_speed": v}}))
        self.scroll_speed = ValueSlider(0.3, 3.0, 0.05, t.get("scroll_speed", 1.0), "{:.2f}×",
                                        lambda v: self._patch({"tuning": {"scroll_speed": v}}))
        self.detent = ValueSlider(0.0, 0.45, 0.01, t.get("magnet_detent", 0.18), "{:.2f}",
                                  lambda v: self._patch({"tuning": {"magnet_detent": v}}))
        form.addRow("Pointer speed", self.pointer_speed)
        form.addRow("Scroll speed", self.scroll_speed)
        form.addRow("Magnet strength", self.detent)
        note = QLabel("How far past a grid cell you push before the pointer jumps to the next one.")
        note.setStyleSheet("color: gray")
        form.addRow("", note)
        self.magnetic = QCheckBox("Start with the magnetic pointer on")
        self.magnetic.setChecked(bool(self.s.get("magnetic_start", True)))
        self.magnetic.toggled.connect(lambda v: self._patch({"magnetic_start": v}))
        form.addRow(self.magnetic)
        hrow = QWidget()
        hl = QHBoxLayout(hrow)
        hl.setContentsMargins(0, 0, 0, 0)
        self.haptics = ValueSlider(0.0, 1.0, 0.05, self.s.get("haptics", 1.0), "{:.0%}",
                                   lambda v: self._patch({"haptics": v}))
        test = QPushButton("Test")
        test.clicked.connect(lambda: (self._flush(), self.runtime.request("test_haptic", "confirm")))
        hl.addWidget(self.haptics, 1)
        hl.addWidget(test)
        form.addRow("Rumble strength", hrow)
        self.ui_magnet = QCheckBox("Experimental: magnet on menus, buttons and other apps (restart)")
        self.ui_magnet.setChecked(bool(self.s.get("ui_magnet", False)))
        self.ui_magnet.toggled.connect(lambda v: self._patch({"ui_magnet": v}))
        form.addRow(self.ui_magnet)
        return w

    def _timing_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        t = self.s.get("tuning") or {}
        self.timing = {}
        for key, label, lo, hi, step in (("hold_ms", "Hold time", 150, 1000, 10),
                                         ("double_ms", "Double-tap window", 120, 500, 10),
                                         ("repeat_delay_ms", "Repeat starts after", 150, 800, 10),
                                         ("repeat_rate_ms", "Repeat every", 30, 200, 5)):
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            sl = ValueSlider(lo, hi, step, t.get(key) or TIMING_DEFAULTS[key], "{:.0f} ms",
                             lambda v, k=key: self._patch({"tuning": {k: int(v)}}))
            use_profile = QCheckBox("profile default")
            use_profile.setChecked(not t.get(key))
            sl.setEnabled(bool(t.get(key)))

            def toggled(on, k=key, s=sl):
                s.setEnabled(not on)
                self._patch({"tuning": {k: 0 if on else int(s.value())}})
            use_profile.toggled.connect(toggled)
            hl.addWidget(sl, 1)
            hl.addWidget(use_profile)
            form.addRow(label, row)
            self.timing[key] = (sl, use_profile)
        note = QLabel("Per-binding hold times in a profile (\"ms\") still win over these.")
        note.setStyleSheet("color: gray")
        form.addRow(note)
        return w

    def _hud_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        hud = {**HUD_DEFAULTS, **(self.s.get("hud") or {})}
        style_row = QWidget()
        sr = QHBoxLayout(style_row)
        sr.setContentsMargins(0, 0, 0, 0)
        self.style_group = QButtonGroup(self)
        for key, label in (("card", "List card"), ("controller", "Controller diagram")):
            rb = QRadioButton(label)
            rb.setChecked(hud["style"] == key)
            rb.toggled.connect(lambda on, k=key: on and self._patch({"hud": {"style": k}}))
            self.style_group.addButton(rb)
            sr.addWidget(rb)
        sr.addStretch(1)
        form.addRow("Hint style", style_row)
        self.corner = QComboBox()
        corners = ["bottom-right", "bottom-left", "top-right", "top-left"]
        self.corner.addItems(corners)
        self.corner.setCurrentText(hud["corner"])
        self.corner.currentTextChanged.connect(lambda v: self._patch({"hud": {"corner": v}}))
        form.addRow("Position", self.corner)
        self.hud_scale = ValueSlider(0.7, 1.6, 0.05, hud["scale"], "{:.2f}×",
                                     lambda v: self._patch({"hud": {"scale": v}}))
        self.hud_opacity = ValueSlider(0.4, 1.0, 0.02, hud["opacity"], "{:.0%}",
                                       lambda v: self._patch({"hud": {"opacity": v}}))
        form.addRow("Size", self.hud_scale)
        form.addRow("Opacity", self.hud_opacity)
        self.hud_visible = QCheckBox("Show hints (View tap toggles them too)")
        self.hud_visible.setChecked(bool(hud.get("visible", True)))
        self.hud_visible.toggled.connect(lambda v: self._patch({"hud": {"visible": v}}))
        form.addRow(self.hud_visible)
        return w

    def _connection_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.link_label = QLabel("—")
        self.backend_label = QLabel("—")
        form.addRow("FL Studio", self.link_label)
        form.addRow("Controller", self.backend_label)
        self.midi_port = QLineEdit(self.s.get("midi_port", "EFLC"))
        self.midi_port.editingFinished.connect(lambda: self._patch({"midi_port": self.midi_port.text().strip()}))
        form.addRow("loopMIDI port (restart)", self.midi_port)
        self.sdl = QCheckBox("PlayStation / generic pads via SDL (restart)")
        self.sdl.setChecked(bool(self.s.get("sdl", True)))
        self.sdl.toggled.connect(lambda v: self._patch({"sdl": v}))
        form.addRow(self.sdl)
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        for label, fn in (("Profiles folder", lambda: os.startfile(config.profiles_dir())),
                          ("Open log", lambda: os.startfile(os.path.join(config.user_dir(), "eflc.log"))),
                          ("Restart EFLC", self._restart)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            hl.addWidget(b)
        hl.addStretch(1)
        form.addRow(row)
        return w

    # ------------------------------------------------------------ actions
    def _stick(self, stick, key, value):
        self._patch({"pad": {"sticks": {stick: {key: value}}}})

    def _pad_value(self, key, value):
        patch = {key: value}
        if key == "trigger_press":
            patch["trigger_release"] = min(self.trig_release.value(), value - 0.05)
            self.trig_release.set_value(patch["trigger_release"])
        if key == "dir_press":
            patch["dir_release"] = max(0.2, value - 0.2)
        self._patch({"pad": patch})

    def _forget_centres(self):
        self._patch({"pad": {"sticks": {s: {"center": [0.0, 0.0]} for s in ("LS", "RS")}}})

    def _reset_tab(self):
        tab = self.tabs.tabText(self.tabs.currentIndex())
        if tab == "Controller":
            for stick, sl in self.stick_sliders.items():
                for k, v in PAD_DEFAULTS.items():
                    sl[k].set_value(v)
                self._patch({"pad": {"sticks": {stick: dict(PAD_DEFAULTS)}}})
            self.trig_press.set_value(0.5)
            self.trig_release.set_value(0.35)
            self.dir_press.set_value(0.6)
            self._patch({"pad": {"trigger_press": 0.5, "trigger_release": 0.35, "dir_press": 0.6, "dir_release": 0.4}})
        elif tab == "Pointer and feel":
            for sl, v in ((self.pointer_speed, 1.0), (self.scroll_speed, 1.0), (self.detent, 0.18), (self.haptics, 1.0)):
                sl.set_value(v)
            self._patch({"tuning": {"pointer_speed": 1.0, "scroll_speed": 1.0, "magnet_detent": 0.18}, "haptics": 1.0})
        elif tab == "Timing":
            for key, (sl, cb) in self.timing.items():
                cb.setChecked(True)
        elif tab == "HUD":
            self.corner.setCurrentText(HUD_DEFAULTS["corner"])
            self.hud_scale.set_value(1.0)
            self.hud_opacity.set_value(0.92)
            self._patch({"hud": {"scale": 1.0, "opacity": 0.92}})

    def _restart(self):
        self._flush()
        QProcess.startDetached(sys.executable, ["-m", "eflc"], os.getcwd())
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(300, QApplication.instance().quit)

    # ------------------------------------------------------------ live data
    def on_monitor(self, snap):
        self.status.setText("Controller: %s" % (snap.get("backend") or "connected") if snap.get("connected")
                            else "No controller connected")
        self.link_label.setText("linked" if snap.get("fl") else
                                ("MIDI port open, waiting for FL" if snap.get("midi_open") else "MIDI port not open"))
        self.backend_label.setText(snap.get("backend") or "none")
        raw, shaped = snap.get("raw") or {}, snap.get("shaped") or {}
        for stick, view in self.stick_views.items():
            cfg = {**PAD_DEFAULTS, "center": (0.0, 0.0), **self._pad(stick)}
            cfg["center"] = tuple(cfg.get("center", (0.0, 0.0)))
            view.update_values(raw.get(stick, (0.0, 0.0)), shaped.get(stick, (0.0, 0.0)), cfg)
        pad = self.s.get("pad") or {}
        for t, bar in self.trig_bars.items():
            bar.value = float(raw.get(t, 0.0))
            bar.press = float(pad.get("trigger_press", 0.5))
            bar.release = float(pad.get("trigger_release", 0.35))
            bar.update()
        self.pressed_label.setText("Pressed: " + (" ".join(snap.get("pressed") or []) or "—"))

    def on_settings_changed(self, settings):
        """Settings changed elsewhere (e.g. calibration finished): refresh centres."""
        self.s["pad"] = settings.get("pad", self.s.get("pad"))
        for stick, sl in self.stick_sliders.items():
            sl["deadzone"].set_value(self._pad(stick).get("deadzone", PAD_DEFAULTS["deadzone"]))
