"""Qt application: HUD windows, tray menu, wiring to the engine thread."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import __version__, config
from .hud.controller_view import ControllerCard
from .hud.modal_overlay import ModalOverlay
from .hud.overlay import AllShortcuts, HintCard, Toast
from .hud.pointer_overlay import PointerOverlay
from .runtime import Runtime

log = logging.getLogger("eflc")


class HudSink(QObject):
    """Thread-safe HUD interface handed to the engine (signals cross threads)."""

    model_changed = Signal(object)
    toasted = Signal(str)
    commanded = Signal(str, bool)
    pointer_changed = Signal(object)
    modal_changed = Signal(object)
    captured = Signal(object)
    monitored = Signal(object)
    settings_updated = Signal(object)

    def update(self, model):
        self.model_changed.emit(model)

    def toast(self, text):
        self.toasted.emit(text)

    def command(self, what, on):
        self.commanded.emit(what, on)

    def pointer(self, view):
        self.pointer_changed.emit(view)

    def modal(self, view):
        self.modal_changed.emit(view)

    def capture(self, pressed):
        self.captured.emit(pressed)

    def monitor(self, snapshot):
        self.monitored.emit(snapshot)

    def settings_changed(self, settings):
        self.settings_updated.emit(settings)


def _tray_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    body = QPainterPath()
    body.addRoundedRect(QRectF(4, 16, 56, 34), 17, 17)
    p.fillPath(body, QColor(83, 74, 183))
    p.setBrush(QColor(255, 255, 255))
    p.setPen(Qt.NoPen)
    p.drawRect(QRectF(15, 30, 12, 4))
    p.drawRect(QRectF(19, 26, 4, 12))
    p.drawEllipse(QRectF(40, 25, 7, 7))
    p.drawEllipse(QRectF(46, 32, 7, 7))
    p.end()
    return QIcon(pm)


class EflcApp(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.settings = config.load_settings()
        hud_cfg = self.settings["hud"]
        scale = float(hud_cfg.get("scale", 1.0))
        corner, margin = hud_cfg.get("corner", "bottom-right"), int(hud_cfg.get("margin", 24))

        self.card = HintCard(scale, corner, margin)
        self.controller = ControllerCard(scale, corner, margin)
        self.style = hud_cfg.get("style", "card")
        self.all = AllShortcuts(scale)
        self.all.setWindowOpacity(0.96)
        self.toast = Toast(scale)
        self.pointer = PointerOverlay(scale)
        self.modal = ModalOverlay(scale)
        self.hints_visible = bool(hud_cfg.get("visible", True))
        self.modal_open = False
        self.last_model = None
        self.editor = None
        self.settings_window = None
        for w in (self.card, self.controller):
            w.setWindowOpacity(float(hud_cfg.get("opacity", 0.92)))

        self.sink = HudSink()
        self.sink.model_changed.connect(self.on_model, Qt.QueuedConnection)
        self.sink.toasted.connect(self.toast.show_text, Qt.QueuedConnection)
        self.sink.commanded.connect(self.on_command, Qt.QueuedConnection)
        self.sink.pointer_changed.connect(self.pointer.set_view, Qt.QueuedConnection)
        self.sink.modal_changed.connect(self.on_modal, Qt.QueuedConnection)
        self.sink.captured.connect(self.on_captured, Qt.QueuedConnection)
        self.sink.monitored.connect(self.on_monitor, Qt.QueuedConnection)
        self.sink.settings_updated.connect(self.on_settings_updated, Qt.QueuedConnection)

        self.runtime = Runtime(self.settings, self.sink)
        self._build_tray()
        app.aboutToQuit.connect(self.shutdown)
        self.runtime.start()
        self._show_hint()

    # ------------------------------------------------------------ hint widgets
    @property
    def hint(self):
        return self.controller if self.style == "controller" else self.card

    def _show_hint(self):
        want = self.hints_visible and not self.modal_open
        for w in (self.card, self.controller):
            w.setVisible(want and w is self.hint and self.last_model is not None)

    def apply_hud(self, hud):
        """Live HUD changes from the settings window (Qt thread)."""
        scale = float(hud.get("scale", 1.0))
        for w in (self.card, self.controller):
            w.corner = hud.get("corner", w.corner)
            w.setWindowOpacity(float(hud.get("opacity", 0.92)))
            w.set_scale(scale)
        self.style = hud.get("style", self.style)
        self.hints_visible = bool(hud.get("visible", self.hints_visible))
        self.act_hints.setChecked(self.hints_visible)
        for act in self.style_group.actions():
            act.setChecked(act.data() == self.style)
        self._show_hint()

    def set_style(self, style):
        self.style = style
        self.runtime.request("settings", {"hud": {"style": style}})
        self._show_hint()

    # ------------------------------------------------------------ HUD slots
    def on_model(self, model):
        if self.last_model is None or self.last_model.profile != model.profile:
            self._refresh_profile_menu(model.profile)
        first = self.last_model is None
        self.last_model = model
        self.card.set_model(model)
        self.controller.set_model(model)
        self.all.set_model(model)
        if first:
            self._show_hint()

    def on_command(self, what, on):
        if what == "toggle":
            self.set_hints_visible(not self.hints_visible)
        elif what == "all":
            if on and self.last_model is not None:
                self.all.set_model(self.last_model)
                self.all.show()
            else:
                self.all.hide()

    def on_modal(self, view):
        self.modal.set_view(view)
        open_now = view is not None
        if open_now != self.modal_open:
            self.modal_open = open_now
            # the modal panel carries its own hints; keep the screen calm
            self._show_hint()

    def on_captured(self, pressed):
        if self.editor is not None:
            self.editor.on_captured(pressed)

    def on_monitor(self, snapshot):
        if self.settings_window is not None:
            self.settings_window.on_monitor(snapshot)

    def on_settings_updated(self, settings):
        if self.settings_window is not None:
            self.settings_window.on_settings_changed(settings)

    def set_hints_visible(self, visible):
        self.hints_visible = visible
        self._show_hint()
        self.act_hints.setChecked(visible)
        self.runtime.request("settings", {"hud": {"visible": visible}})

    def open_editor(self):
        from .editor.mapping_editor import MappingEditor
        if self.editor is None:
            self.editor = MappingEditor(self.runtime)
            self.editor.destroyed.connect(lambda *_: setattr(self, "editor", None))
        self.editor.show()
        self.editor.raise_()
        self.editor.activateWindow()

    def open_settings(self):
        from .editor.settings_window import SettingsWindow
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self.runtime, self.apply_hud)
            self.settings_window.destroyed.connect(lambda *_: setattr(self, "settings_window", None))
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def set_haptics(self, value):
        self.runtime.request("haptics", value)
        for act in self.haptic_group.actions():
            act.setChecked(abs(act.data() - value) < 1e-6)

    # ------------------------------------------------------------ tray
    def _build_tray(self):
        self.tray = QSystemTrayIcon(_tray_icon(), self.app)
        self.tray.setToolTip("EFLC %s" % __version__)
        self.tray.activated.connect(lambda reason: reason == QSystemTrayIcon.DoubleClick and self.open_settings())
        menu = QMenu()
        menu.addAction("Settings…", self.open_settings)
        menu.addAction("Mapping editor…", self.open_editor)
        self.profile_menu = menu.addMenu("Profile")
        self.profile_group = QActionGroup(self.profile_menu)
        menu.addSeparator()
        self.act_hints = QAction("Show hints", menu, checkable=True, checked=self.hints_visible)
        self.act_hints.toggled.connect(lambda v: v != self.hints_visible and self.set_hints_visible(v))
        menu.addAction(self.act_hints)
        style = menu.addMenu("Hint style")
        self.style_group = QActionGroup(style)
        for label, key in (("List card", "card"), ("Controller diagram", "controller")):
            act = QAction(label, style, checkable=True, checked=(self.style == key))
            act.setData(key)
            act.triggered.connect(lambda _=False, k=key: self.set_style(k))
            self.style_group.addAction(act)
            style.addAction(act)
        menu.addAction("Magnetic / free pointer", lambda: self.runtime.request("pointer", "toggle"))
        haptics = menu.addMenu("Rumble feedback")
        self.haptic_group = QActionGroup(haptics)
        current = float(self.settings.get("haptics", 1.0))
        for label, value in (("Off", 0.0), ("Subtle", 0.5), ("Full", 1.0)):
            act = QAction(label, haptics, checkable=True, checked=abs(current - value) < 1e-6)
            act.setData(value)
            act.triggered.connect(lambda _=False, v=value: self.set_haptics(v))
            self.haptic_group.addAction(act)
            haptics.addAction(act)
        menu.addAction("Calibrate sticks (hands off)", lambda: self.runtime.request("calibrate"))
        menu.addAction("Reload profiles", lambda: self.runtime.request("reload"))
        menu.addSeparator()
        menu.addAction("Quit EFLC", self.app.quit)
        self.tray.setContextMenu(menu)
        self._menu = menu
        self.tray.show()

    def _refresh_profile_menu(self, active):
        self.profile_menu.clear()
        for a in self.profile_group.actions():
            self.profile_group.removeAction(a)
        names = [os.path.splitext(f)[0] for f in config.profile_files()]
        engine = self.runtime.engine
        if engine is not None:
            names = sorted(engine.profiles)
        for name in names:
            act = QAction(name, self.profile_menu, checkable=True, checked=(name == active))
            act.triggered.connect(lambda _=False, n=name: self.runtime.request("profile", n))
            self.profile_group.addAction(act)
            self.profile_menu.addAction(act)

    def shutdown(self):
        self.runtime.stop()
        self.runtime.join(timeout=2.0)
        self.tray.hide()


def setup_logging():
    path = os.path.join(config.user_dir(), "eflc.log")
    handlers = [RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    setup_logging()
    config.install_bundled_profiles()
    log.info("EFLC %s starting", __version__)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("EFLC")
    _ = EflcApp(app)
    sys.exit(app.exec())
