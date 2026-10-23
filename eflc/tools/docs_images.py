"""Render the README screenshots into docs/images (reproducible, no live screen grabs).

    python -m eflc.tools.docs_images [out_dir]

Everything is drawn by the real HUD code from the bundled profiles, composited
over the FL captures in tests/fixtures (which are redacted).
"""

import os
import sys
import time

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(ROOT, "tests", "fixtures")
BG = QColor(22, 24, 28)


def _fixture(name):
    q = QImage(os.path.join(FIX, name)).convertToFormat(QImage.Format_RGB32)
    w, h = q.width(), q.height()
    arr = np.frombuffer(q.constBits(), dtype=np.uint8, count=q.sizeInBytes()).reshape(h, q.bytesPerLine() // 4, 4)
    return q, arr[:, :w, 2::-1].copy()


def _settle(widget):
    from ..hud.pointer_overlay import Spring
    for v in vars(widget).values():
        for s in (v if isinstance(v, list) else [v]):
            if isinstance(s, Spring):
                s.snap(s.target)


def _canvas(w, h, color=BG):
    pm = QPixmap(w, h)
    pm.fill(color)
    return pm


def _render(widget, canvas, x, y):
    p = QPainter(canvas)
    widget.render(p, QPoint(int(x), int(y)))
    p.end()


def _caption(canvas, text, x, y, size=13, color=QColor(170, 178, 186)):
    p = QPainter(canvas)
    f = QFont()
    f.setFamilies(["Segoe UI"])
    f.setPixelSize(size)
    p.setFont(f)
    p.setPen(color)
    p.drawText(QRectF(x, y, 2000, size + 8), Qt.AlignLeft | Qt.AlignVCenter, text)
    p.end()


class _Hud:
    def __init__(self):
        self.model = None

    def update(self, m):
        self.model = m

    def __getattr__(self, name):
        return lambda *a, **k: None


class _Out:
    def __getattr__(self, name):
        return lambda *a, **k: (0, 0) if name == "cursor_pos" else None


def _engine():
    from .. import config
    from ..core.engine import Engine
    from ..platform.win_output import parse_combo
    profiles, _ = config.load_profiles(parse_combo, config.BUNDLED_PROFILES)
    hud = _Hud()
    return Engine(profiles, "Default", _Out(), lambda *a, **k: 1, hud), hud


def _model(eng, hud, state, held=(), t=[0.0]):
    from ..input.pad import PadState
    for pressed in (frozenset(), frozenset(held[:1]), frozenset(held)):
        t[0] += 0.01
        eng.tick(t[0], PadState(True, pressed, {}), "fl64.exe", state, True)
    model = hud.model
    t[0] += 0.01
    eng.tick(t[0], PadState(True, frozenset(), {}), "fl64.exe", state, True)
    return model


def _pointer_tile(grid, ix, iy, tool="draw", pressed=False, scale="", crop=(300, 150), slide=False):
    import eflc.hud.pointer_overlay as po
    from ..core.pointer import PointerView
    keep = po.phys_to_logical
    po.phys_to_logical = lambda x, y: (x, y, 1.0)
    try:
        rect = grid.cell_rect(ix, iy)
        l, t, r, b = rect
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        v = PointerView(l + 4, (t + b) / 2, rect, "cell", True, pressed, True,
                        names[grid.pitch[iy]] if grid.pitch else "", scale, 1 if slide else 0, tool)
        w = po.PointerOverlay()
        w.set_view(v)
        _settle(w)
        w.alpha.snap(1.0)
        w.press.snap(1.0 if pressed else 0.0)
        if slide:
            w.box[0].x -= (r - l) * 0.45
            w.pulses = [(time.monotonic() - 0.08, (l, t, r, b))]
        else:
            w.pulses = []
        w._frame()
        return w, v
    finally:
        po.phys_to_logical = keep


def hero(out, eng, hud):
    """Controller view over the piano roll, pointer snapped to a cell."""
    from ..hud.controller_view import ControllerCard
    from ..vision.grid import analyze
    q, arr = _fixture("pianoroll_908x726.png")
    grid = analyze(arr, "pianoroll", (109, 103, 868, 585), (9, 103, 109, 585), focus=(500, 300))
    canvas = QPixmap.fromImage(q).scaled(1100, int(726 * 1100 / 908), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    sx = 1100 / 908
    big = QPixmap.fromImage(q)
    w, v = _pointer_tile(grid, 9, 9, scale="C minor")
    p = QPainter(big)
    w.render(p, QPoint(w.x(), w.y()))
    p.end()
    canvas = big.scaled(1100, int(726 * sx), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    card = ControllerCard(scale=0.85)
    card.set_model(_model(eng, hud, {"focus": "pianoroll", "channelName": "Sytrus - Lead"}))
    _render(card, canvas, canvas.width() - card.width() - 16, canvas.height() - card.height() - 16)
    canvas.save(os.path.join(out, "hero.png"))


def hint_styles(out, eng, hud):
    from ..hud.controller_view import ControllerCard
    from ..hud.overlay import HintCard
    state = {"focus": "pianoroll", "channelName": "Sytrus - Lead"}
    base = _model(eng, hud, state)
    lt = _model(eng, hud, state, ("LT", "A"))
    for name, model in (("hint_card", base), ("hint_card_layer", lt)):
        c = HintCard()
        c.set_model(model)
        canvas = _canvas(c.width() + 32, c.height() + 32)
        _render(c, canvas, 16, 16)
        canvas.save(os.path.join(out, name + ".png"))
    for name, model in (("hint_controller", base), ("hint_controller_layer", lt),
                        ("hint_controller_rb", _model(eng, hud, {"focus": "mixer", "mixerTrackName": "Insert 3 - Drums"},
                                                      ("RB",)))):
        c = ControllerCard()
        c.set_model(model)
        canvas = _canvas(c.width() + 32, c.height() + 32)
        _render(c, canvas, 16, 16)
        canvas.save(os.path.join(out, name + ".png"))


def modals(out, eng, hud):
    from ..core.modals import KeyboardModal, ListModal, MenuItem, WheelItem, WheelModal
    from ..hud.modal_overlay import ModalOverlay
    _model(eng, hud, {"focus": "pianoroll"})
    views = {
        "menu": eng._context_menu(),
        "params": ListModal(eng, "Serum", [
            MenuItem("Osc A level", kind="param", value=0.72, detail="72 %"),
            MenuItem("Osc A pan", kind="param", value=0.5, detail="C"),
            MenuItem("Filter cutoff", kind="param", value=0.38, detail="1.24 kHz"),
            MenuItem("Filter reso", kind="param", value=0.15, detail="15 %"),
            MenuItem("Env 1 attack", kind="param", value=0.05, detail="2 ms"),
            MenuItem("Env 1 release", kind="param", value=0.44, detail="380 ms")],
            subtitle="generator · 2623 parameters",
            hint="↑↓ choose · ←→ adjust · RS fine · X refresh · B close"),
        "wheel": WheelModal(eng, "Windows", [
            WheelItem("Playlist", "playlist"), WheelItem("Piano roll", "pianoroll", active=True),
            WheelItem("Channel rack", "channelrack"), WheelItem("Mixer", "mixer"),
            WheelItem("Browser", "browser"), WheelItem("Serum", "plugin"), WheelItem("Pro-Q 3", "plugin")]),
        "keyboard": KeyboardModal(eng),
    }
    views["params"].selected = 2
    views["wheel"].selected = 5
    views["wheel"].aim = 5 * 2 * 3.14159 / 7
    kb = views["keyboard"]
    kb.text, kb.row, kb.col = "serum", 2, 3
    for name, modal in views.items():
        eng.modals = [modal]
        w = ModalOverlay()
        w.set_view(modal.view())
        _settle(w)
        w.alpha.snap(1.0)
        w.slide.snap(0.0)
        canvas = _canvas(w.width() + 32, w.height() + 32)
        _render(w, canvas, 16, 16)
        canvas.save(os.path.join(out, "modal_%s.png" % name))
    eng.modals = []


def pointer_tools(out):
    from ..vision.grid import analyze
    q, arr = _fixture("pianoroll_908x726.png")
    grid = analyze(arr, "pianoroll", (109, 103, 868, 585), (9, 103, 109, 585), focus=(500, 300))
    tiles = [("Draw", "draw", False), ("Paint", "paint", False), ("Delete", "delete", False),
             ("Mute", "mute", False), ("Slice", "slice", False), ("Select", "select", False),
             ("Pressed (drag)", "draw", True), ("Snapping", "draw", False)]
    tw, th = 220, 120
    canvas = _canvas(tw * 4 + 10, (th + 26) * 2 + 10)
    for i, (label, tool, pressed) in enumerate(tiles):
        ix, iy = 9, 9
        w, v = _pointer_tile(grid, ix, iy, tool, pressed, slide=(label == "Snapping"))
        cx, cy = (grid.xs[ix] + grid.xs[ix + 1]) / 2, (grid.ys[iy] + grid.ys[iy + 1]) / 2
        crop = QRect(int(cx) - tw // 2 + 20, int(cy) - th // 2, tw, th)
        bg = QPixmap.fromImage(q).copy(crop)
        p = QPainter(bg)
        w.render(p, QPoint(w.x() - crop.x(), w.y() - crop.y()))
        p.end()
        x, y = 5 + (i % 4) * tw, 5 + (i // 4) * (th + 26)
        p = QPainter(canvas)
        p.drawPixmap(x, y, bg)
        p.end()
        _caption(canvas, label, x + 6, y + th + 3)
    canvas.save(os.path.join(out, "pointer_tools.png"))


def vision(out):
    from ..vision.grid import NOTE_NAMES, analyze
    q, arr = _fixture("pianoroll_908x726.png")
    g = analyze(arr, "pianoroll", (109, 103, 868, 585), (9, 103, 109, 585), focus=(500, 300))
    pm = QPixmap.fromImage(q)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(139, 124, 255, 170), 1))
    for x in g.xs:
        p.drawLine(QPointF(x, g.ys[0]), QPointF(x, g.ys[-1]))
    p.setPen(QPen(QColor(63, 208, 176, 170), 1))
    for y in g.ys:
        p.drawLine(QPointF(g.xs[0], y), QPointF(g.xs[-1], y))
    f = QFont()
    f.setFamilies(["Segoe UI"])
    f.setPixelSize(11)
    f.setBold(True)
    p.setFont(f)
    for i in range(g.rows):
        y0, y1 = g.ys[i], g.ys[i + 1]
        p.setPen(QColor(255, 196, 92))
        p.drawText(QRectF(g.xs[0] + 4, y0, 30, y1 - y0), Qt.AlignLeft | Qt.AlignVCenter, NOTE_NAMES[g.pitch[i]])
    p.end()
    pm.save(os.path.join(out, "vision_pianoroll.png"))

    q, arr = _fixture("playlist_clips_1608x908.png")
    g = analyze(arr, "playlist", (408, 97, 1577, 908), focus=(700, 400), headers_local=(241, 97, 408, 908))
    pm = QPixmap.fromImage(q).copy(QRect(230, 60, 900, 640))
    p = QPainter(pm)
    p.translate(-230, -60)
    for i in range(g.rows):
        y0, y1 = g.ys[i], g.ys[i + 1]
        p.fillRect(QRectF(241, y0 + 1, 1577 - 241, y1 - y0 - 2),
                   QColor(63, 208, 176, 40) if i % 2 else QColor(139, 124, 255, 40))
    p.setPen(QPen(QColor(63, 208, 176), 2))
    for y in g.ys:
        p.drawLine(QPointF(241, y), QPointF(1577, y))
    p.end()
    pm.save(os.path.join(out, "vision_playlist.png"))


def settings_window(out):
    from ..editor.settings_window import SettingsWindow

    class FakeRuntime:
        settings = {"pad": {"sticks": {"LS": {"deadzone": 0.15, "center": [0.11, -0.06]},
                                       "RS": {"deadzone": 0.15, "center": [0.03, -0.03]}}},
                    "hud": {"style": "controller"}, "tuning": {"pointer_speed": 1.3}}

        def request(self, *a):
            pass

    w = SettingsWindow(FakeRuntime(), lambda hud: None)
    w.setAttribute(Qt.WA_DontShowOnScreen)
    w.show()
    snap = {"connected": True, "backend": "XInput", "fl": True, "midi_open": True, "pressed": ["LT", "A"],
            "raw": {"LS": (0.12, -0.05), "RS": (0.55, 0.42), "LT": 0.83, "RT": 0.2},
            "shaped": {"LS": (0.0, 0.0), "RS": (0.47, 0.36), "LT": 0.83, "RT": 0.2}}
    for i in range(30):
        s = dict(snap)
        s["raw"] = dict(snap["raw"], LS=(0.11 + 0.01 * ((i % 5) - 2), -0.06 + 0.008 * ((i % 3) - 1)))
        w.on_monitor(s)
    QApplication.processEvents()
    w.grab().save(os.path.join(out, "settings_controller.png"))
    w._pending = {}
    w.close()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "images")
    os.makedirs(out, exist_ok=True)
    eng, hud = _engine()
    hero(out, eng, hud)
    hint_styles(out, eng, hud)
    modals(out, eng, hud)
    pointer_tools(out)
    vision(out)
    settings_window(out)
    for fn in sorted(os.listdir(out)):
        print(os.path.join(out, fn))
    del app


if __name__ == "__main__":
    main()
