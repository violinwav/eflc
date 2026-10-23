"""The EFLC pointer: a reticle that springs after the cursor and morphs into a
highlight around whatever it's snapped to (grid cell, hotspot).

Design
- Free:      a soft 24 px ring around the cursor tip.
- Magnetic:  the ring stretches into a rounded highlight hugging the cell;
             moving to the next cell slides the highlight with a slightly
             under-damped spring (a hint of overshoot reads as a detent).
- Snap:      every snap emits a thin pulse ring that expands and fades
             (paired with a rumble tick on the pad).
- Press:     holding a click squashes the highlight a few percent and
             brightens the fill, like pressing a key down.
- Labels:    a small chip shows the note name (piano roll) or hotspot name.
The overlay is a small window that moves with the pointer instead of a
full-screen layer, so each frame repaints only a few hundred pixels.
"""

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen

from .overlay import Overlay

ACCENT = {"cell": QColor(139, 124, 255), "hotspot": QColor(63, 208, 176), "free": QColor(230, 237, 241),
          "ui": QColor(139, 124, 255)}
# Editor tool -> highlight colour (the shape decorations are drawn in paintEvent).
TOOL_COLORS = {"draw": QColor(139, 124, 255), "paint": QColor(88, 166, 255), "delete": QColor(255, 104, 104),
               "mute": QColor(255, 190, 80), "slice": QColor(63, 208, 176), "select": QColor(230, 237, 241),
               "slip": QColor(240, 120, 200), "zoom": QColor(170, 178, 186)}
TOOL_NAMES = {"paint": "Paint", "delete": "Delete", "mute": "Mute", "slice": "Slice", "select": "Select",
              "slip": "Slip", "zoom": "Zoom"}
OFF_MAGNET = QColor(255, 196, 92)
CHIP_BG = QColor(14, 17, 20, 225)
TEXT = QColor(235, 240, 244)
MUTED = QColor(160, 172, 180)
MARGIN = 46


def phys_to_logical(x, y):
    """Physical screen px -> Qt logical coords (Windows: per-screen DPR, native origin)."""
    for scr in QGuiApplication.screens():
        g = scr.geometry()
        dpr = scr.devicePixelRatio()
        ox, oy = g.x(), g.y()
        if ox <= x < ox + g.width() * dpr and oy <= y < oy + g.height() * dpr:
            return ox + (x - ox) / dpr, oy + (y - oy) / dpr, dpr
    scr = QGuiApplication.primaryScreen()
    dpr = scr.devicePixelRatio() if scr else 1.0
    return x / dpr, y / dpr, dpr


class Spring:
    """Damped spring toward a target; zeta < 1 overshoots slightly.

    Integrated with implicit (backward) Euler in <= 4 ms substeps: stable for
    any frame time. (Explicit integration at these stiffnesses blows up as soon
    as a frame takes longer than a few ms, which crashed the overlay.)
    """

    MAX_SUBSTEP = 0.004

    def __init__(self, value=0.0, freq=13.0, zeta=0.72):
        self.x, self.v, self.target = value, 0.0, value
        self.w, self.z = 2 * math.pi * freq, zeta

    def step(self, dt):
        if not (dt > 0):
            return self.x
        n = max(1, int(math.ceil(dt / self.MAX_SUBSTEP)))
        h = dt / n
        w2, c = self.w * self.w, 2 * self.z * self.w
        for _ in range(n):
            self.v = (self.v + h * w2 * (self.target - self.x)) / (1 + h * c + h * h * w2)
            self.x += h * self.v
        if not (math.isfinite(self.x) and math.isfinite(self.v)):
            self.snap(self.target)
        return self.x

    def snap(self, value):
        self.x = self.target = value
        self.v = 0.0

    @property
    def settled(self):
        return abs(self.target - self.x) < 0.05 and abs(self.v) < 0.5


class PointerOverlay(Overlay):
    def __init__(self, scale=1.0):
        super().__init__(scale)
        self.view = None
        self.cx, self.cy = Spring(freq=22, zeta=0.85), Spring(freq=22, zeta=0.85)
        # Highlight = centre (slight overshoot: a soft detent) + size (critically
        # damped: never overshoots). Springing the four edges separately let the
        # box stretch across neighbouring rows during fast moves.
        self.box = [Spring(freq=14, zeta=0.7), Spring(freq=14, zeta=0.7),
                    Spring(freq=16, zeta=1.0), Spring(freq=16, zeta=1.0)]   # cx, cy, w, h
        self.alpha = Spring(0.0, freq=6, zeta=1.0)
        self.press = Spring(0.0, freq=18, zeta=0.6)
        self.pulses = []   # [(start_time, rect)]
        self.last_pulse = 0
        self.initialised = False
        self.dpr = 1.0
        self._t = time.monotonic()
        self._origin = (0.0, 0.0)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._frame)
        self.timer.start(8)

    def rect_now(self):
        cx, cy, w, h = (s.x for s in self.box)
        w, h = max(2.0, w), max(2.0, h)
        return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2

    def set_view(self, view):
        if view is None:
            self.alpha.target = 0.0
            return
        x, y, self.dpr = phys_to_logical(view.x, view.y)
        if not self.initialised:
            self.cx.snap(x)
            self.cy.snap(y)
            for s, v in zip(self.box, (x, y, 24.0, 24.0)):
                s.snap(v)
            self.initialised = True
        self.cx.target, self.cy.target = x, y
        if view.target:
            l, t, _ = phys_to_logical(view.target[0], view.target[1])
            r, b, _ = phys_to_logical(view.target[2], view.target[3])
            pad = 1.5
            targets = (l - pad, t - pad, r + pad, b + pad)
        else:
            rr = 12.0
            targets = (x - rr, y - rr, x + rr, y + rr)
        tl, tt, tr, tb = targets
        for s, v in zip(self.box, ((tl + tr) / 2, (tt + tb) / 2, tr - tl, tb - tt)):
            s.target = v
        self.alpha.target = 1.0 if view.visible else 0.0
        self.press.target = 1.0 if view.pressed else 0.0
        if view.pulse != self.last_pulse:
            self.last_pulse = view.pulse
            self.pulses.append((time.monotonic(), targets))
            self.pulses = self.pulses[-4:]
        self.view = view

    # ------------------------------------------------------------ animation
    def _frame(self):
        now = time.monotonic()
        dt = min(1 / 30.0, max(1 / 500.0, now - self._t))
        self._t = now
        if self.view is None:
            return
        for s in (self.cx, self.cy, self.alpha, self.press, *self.box):
            s.step(dt)
        self.pulses = [p for p in self.pulses if now - p[0] < 0.32]
        if self.alpha.x < 0.02 and self.alpha.target == 0.0:
            if self.isVisible():
                self.hide()
            return
        l, t, r, b = self.rect_now()
        chip_w = 160 if self.view.label or self.view.scale or self.view.tool != "draw" else 0
        left = min(l, self.cx.x) - MARGIN
        top = min(t, self.cy.x) - MARGIN
        right = max(r, self.cx.x) + MARGIN + chip_w
        bottom = max(b, self.cy.x) + MARGIN
        # Safety net: never let a bad value turn into a gigantic window.
        if not all(math.isfinite(v) for v in (left, top, right, bottom)) or \
                right - left > 2400 or bottom - top > 1600:
            for s, v in zip(self.box, (self.cx.target, self.cy.target, 24.0, 24.0)):
                s.snap(v)
            self.cx.snap(self.cx.target)
            self.cy.snap(self.cy.target)
            return
        geo = (int(left), int(top), int(right - left), int(bottom - top))
        if (self.x(), self.y(), self.width(), self.height()) != geo:
            self.setGeometry(*geo)
        self._origin = (geo[0], geo[1])
        if not self.isVisible():
            self.show()
        self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, e):
        v = self.view
        if v is None:
            return
        ox, oy = self._origin
        a = max(0.0, min(1.0, self.alpha.x))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(-ox, -oy)
        accent = QColor(ACCENT.get(v.kind, ACCENT["free"]))
        tool = v.tool if v.kind == "cell" else "draw"
        if v.kind == "cell":
            accent = QColor(TOOL_COLORS.get(tool, accent))
        if not v.magnetic:
            accent = QColor(OFF_MAGNET)
        l, t, r, b = self.rect_now()
        w, h = max(2.0, r - l), max(2.0, b - t)
        press = max(0.0, min(1.0, self.press.x))
        squash = 1.0 - 0.08 * press
        cxr, cyr = (l + r) / 2, (t + b) / 2
        w, h = w * squash, h * squash
        rect = QRectF(cxr - w / 2, cyr - h / 2, w, h)
        small = v.target is None
        radius = min(w, h) / 2 if small else min(7.0, min(w, h) / 2.4)

        # pulses (expanding fading outline)
        now = time.monotonic()
        for start, (pl, pt, pr, pb) in self.pulses:
            k = (now - start) / 0.32
            grow = 3 + 12 * k
            c = QColor(accent)
            c.setAlphaF(0.55 * (1 - k) * a)
            p.setPen(QPen(c, 1.5))
            p.setBrush(Qt.NoBrush)
            pr_ = QRectF(pl - grow, pt - grow, (pr - pl) + 2 * grow, (pb - pt) + 2 * grow)
            p.drawRoundedRect(pr_, radius + grow, radius + grow)

        # soft shadow ring for contrast on light and dark UI
        shadow = QColor(0, 0, 0, int(110 * a))
        p.setPen(QPen(shadow, 4.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, radius, radius)

        fill = QColor(accent)
        fill.setAlphaF((0.16 + 0.22 * press) * a if not small else 0.10 * a)
        if tool == "mute":
            fill.setAlphaF(fill.alphaF() * 0.5)
        edge = QColor(accent)
        edge.setAlphaF(0.95 * a)
        pen = QPen(edge, 2.0)
        if tool == "select":
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(fill)
        p.drawRoundedRect(rect, radius, radius)
        if not small:  # inner highlight line: a little bevel for "keycap" depth
            inner = QColor(255, 255, 255, int(55 * a * (1 - press * 0.6)))
            p.setPen(QPen(inner, 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(2.5, 2.5, -2.5, -2.5), max(1.0, radius - 2), max(1.0, radius - 2))
        if not small and v.kind == "cell":
            self._tool_marks(p, tool, rect, radius, edge, a)

        # tip dot that trails the cursor
        dot = QPointF(self.cx.x, self.cy.x)
        p.setPen(QPen(QColor(0, 0, 0, int(160 * a)), 1.5))
        p.setBrush(QColor(255, 255, 255, int(235 * a)))
        p.drawEllipse(dot, 2.6, 2.6)

        # label chip (note name / hotspot name / tool / scale)
        text = v.label
        sub = " · ".join(s for s in (TOOL_NAMES.get(tool, "") if v.kind == "cell" else "", v.scale) if s)
        if text or sub:
            f = self.mkfont(12, True)
            fs = self.mkfont(10)
            tw = QFontMetricsF(f).horizontalAdvance(text) if text else 0
            sw = QFontMetricsF(fs).horizontalAdvance(sub) if sub else 0
            cw = max(tw, sw) + 16
            ch = 22 if not (text and sub) else 34
            chip = QRectF(r + 8, cyr - ch / 2, cw, ch)
            path = QPainterPath()
            path.addRoundedRect(chip, 8, 8)
            bg = QColor(CHIP_BG)
            bg.setAlphaF(CHIP_BG.alphaF() * a)
            p.fillPath(path, bg)
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), int(160 * a)), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            y = chip.top() + 4
            if text:
                p.setFont(f)
                c = QColor(TEXT)
                c.setAlphaF(a)
                p.setPen(c)
                p.drawText(QRectF(chip.left() + 8, y, cw - 16, 16), Qt.AlignLeft | Qt.AlignVCenter, text)
                y += 15
            if sub:
                p.setFont(fs)
                c = QColor(MUTED)
                c.setAlphaF(a)
                p.setPen(c)
                p.drawText(QRectF(chip.left() + 8, y, cw - 16, 14), Qt.AlignLeft | Qt.AlignVCenter, sub)
        p.end()

    @staticmethod
    def _tool_marks(p, tool, rect, radius, edge, a):
        """Small shape cues so the active FL tool reads at a glance."""
        if tool == "slice":       # the cut: a bright line on the cell's start, past both edges
            pen = QPen(edge, 2.0)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            x = rect.left() + 1.5
            p.drawLine(QPointF(x, rect.top() - 7), QPointF(x, rect.bottom() + 7))
        elif tool == "delete":    # a small cross in the corner
            s = min(6.0, rect.height() * 0.3)
            c = QPointF(rect.right() - s - 3, rect.top() + s + 3)
            p.setPen(QPen(edge, 1.6))
            p.drawLine(QPointF(c.x() - s / 2, c.y() - s / 2), QPointF(c.x() + s / 2, c.y() + s / 2))
            p.drawLine(QPointF(c.x() - s / 2, c.y() + s / 2), QPointF(c.x() + s / 2, c.y() - s / 2))
        elif tool == "mute":      # struck through
            p.setPen(QPen(edge, 1.4))
            p.drawLine(QPointF(rect.left() + 3, rect.bottom() - 3), QPointF(rect.right() - 3, rect.top() + 3))
        elif tool == "paint":     # a second outline: "repeats"
            c = QColor(edge)
            c.setAlphaF(0.45 * a)
            p.setPen(QPen(c, 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(-4, -4, 4, 4), radius + 4, radius + 4)
