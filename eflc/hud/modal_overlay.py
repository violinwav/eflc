"""Modal HUD rendering: list menus, the window wheel and the on-screen keyboard.

Shared selection style ("tactile pill")
- The selection is a rounded pill that *travels* between items on a slightly
  under-damped spring, so a move reads as a physical slide with a soft settle.
- Activating flashes the pill brighter and dips it to 96 % scale for ~120 ms,
  like a key bottoming out; the pad gives a matching rumble.
- Lists scroll on their own spring, keeping the pill away from the edges.
- Panels slide + fade in on open and fade out on close.
"""

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen

from .overlay import BG, BORDER, LAYER_FG, MUTED, TEXT, Overlay
from .pointer_overlay import Spring

ACCENT = QColor(139, 124, 255)
ACCENT_SOFT = QColor(139, 124, 255, 70)
ROW_H = 34
LIST_W = 380
VISIBLE_ROWS = 11


def _alpha(c, a):
    c = QColor(c)
    c.setAlphaF(max(0.0, min(1.0, c.alphaF() * a)))
    return c


def draw_icon(p, kind, r, color):
    """Small vector icons for the window wheel."""
    p.save()
    pen = QPen(color, max(1.6, r.width() * 0.07))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    if kind == "playlist":
        for i, frac in enumerate((0.9, 0.55, 0.75)):
            yy = y + h * (0.25 + i * 0.25)
            p.drawLine(QPointF(x + w * 0.1, yy), QPointF(x + w * (0.1 + 0.8 * frac), yy))
    elif kind == "pianoroll":
        p.drawRoundedRect(QRectF(x + w * 0.12, y + h * 0.15, w * 0.76, h * 0.7), 3, 3)
        for i in range(1, 4):
            xx = x + w * (0.12 + 0.19 * i)
            p.drawLine(QPointF(xx, y + h * 0.15), QPointF(xx, y + h * 0.85))
        p.setBrush(color)
        for i in (1, 2):
            xx = x + w * (0.12 + 0.19 * i)
            p.drawRect(QRectF(xx - w * 0.05, y + h * 0.15, w * 0.1, h * 0.38))
    elif kind == "channelrack":
        for row in range(3):
            for col in range(4):
                filled = (row + col) % 3 == 0
                rr = QRectF(x + w * (0.12 + col * 0.2), y + h * (0.2 + row * 0.22), w * 0.14, h * 0.14)
                p.setBrush(color if filled else Qt.NoBrush)
                p.drawRoundedRect(rr, 2, 2)
    elif kind == "mixer":
        for i, k in enumerate((0.35, 0.65, 0.45)):
            xx = x + w * (0.25 + i * 0.25)
            p.drawLine(QPointF(xx, y + h * 0.15), QPointF(xx, y + h * 0.85))
            p.setBrush(color)
            p.drawRoundedRect(QRectF(xx - w * 0.08, y + h * k - h * 0.05, w * 0.16, h * 0.1), 2, 2)
    elif kind == "browser":
        path = QPainterPath()
        path.moveTo(x + w * 0.12, y + h * 0.3)
        path.lineTo(x + w * 0.4, y + h * 0.3)
        path.lineTo(x + w * 0.48, y + h * 0.4)
        path.lineTo(x + w * 0.88, y + h * 0.4)
        path.lineTo(x + w * 0.88, y + h * 0.8)
        path.lineTo(x + w * 0.12, y + h * 0.8)
        path.closeSubpath()
        p.drawPath(path)
    else:  # plugin: a knob
        c = QPointF(x + w / 2, y + h / 2)
        p.drawEllipse(c, w * 0.32, h * 0.32)
        p.drawLine(c, QPointF(c.x() + w * 0.18, c.y() - h * 0.18))
    p.restore()


class ModalOverlay(Overlay):
    def __init__(self, scale=1.0):
        super().__init__(scale)
        self.view = None
        self.kind = None
        self.alpha = Spring(0.0, freq=7, zeta=1.0)
        self.slide = Spring(0.0, freq=9, zeta=0.9)
        self.sel_y = Spring(0.0, freq=15, zeta=0.66)
        self.scroll = Spring(0.0, freq=10, zeta=0.95)
        self.flash = Spring(0.0, freq=9, zeta=1.0)
        self.wheel_angle = Spring(0.0, freq=13, zeta=0.7)
        self.key_rect = [Spring(0.0, freq=16, zeta=0.68) for _ in range(4)]
        self.last_activated = 0
        self.first = True
        self._t = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._frame)
        self.timer.start(8)

    # ------------------------------------------------------------ state
    def set_view(self, view):
        if view is None:
            self.alpha.target = 0.0
            self.slide.target = 24.0
            return
        if self.view is None or view.kind != self.kind or self.alpha.x < 0.05:
            self.first = True
        self.kind = view.kind
        if view.activated != self.last_activated:
            self.last_activated = view.activated
            self.flash.x, self.flash.v, self.flash.target = 1.0, 0.0, 0.0
        self.view = view
        self.alpha.target = 1.0
        self.slide.target = 0.0
        self._layout(view)
        if self.first:
            self.slide.snap(24.0)
            self.sel_y.snap(self.sel_y.target)
            self.scroll.snap(self.scroll.target)
            self.wheel_angle.snap(self.wheel_angle.target)
            for s in self.key_rect:
                s.snap(s.target)
            self.first = False
        if not self.isVisible():
            self.show()

    def _layout(self, v):
        s = self.s
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is None:
            return
        if v.kind == "list":
            n = len(v.items)
            rows = min(VISIBLE_ROWS, max(1, n))
            h = (86 + rows * ROW_H + 40) * s
            w = LIST_W * s
            self.resize(int(w), int(h))
            self.move(int(scr.right() - w - 48 * s), int(scr.center().y() - h / 2))
            self.sel_y.target = v.selected * ROW_H * s
            top = self.scroll.target
            view_h = rows * ROW_H * s
            y = v.selected * ROW_H * s
            margin = ROW_H * s * 1.5
            if y < top + margin:
                top = y - margin
            elif y + ROW_H * s > top + view_h - margin:
                top = y + ROW_H * s - view_h + margin
            self.scroll.target = max(0.0, min(top, max(0.0, n * ROW_H * s - view_h)))
        elif v.kind == "wheel":
            size = 420 * s
            self.resize(int(size), int(size))
            self.move(int(scr.center().x() - size / 2), int(scr.center().y() - size / 2))
            n = max(1, len(v.items))
            target = v.selected * 2 * math.pi / n
            cur = self.wheel_angle.target
            # shortest way round
            delta = (target - cur + math.pi) % (2 * math.pi) - math.pi
            self.wheel_angle.target = cur + delta
        else:  # keyboard
            unit = 46 * s
            w = 10 * unit + 40 * s
            h = 5 * unit + 120 * s
            self.resize(int(w), int(h))
            self.move(int(scr.center().x() - w / 2), int(scr.bottom() - h - 40 * s))
            r = self._key_geometry(v)
            if r is not None:
                for spring, val in zip(self.key_rect, (r.x(), r.y(), r.width(), r.height())):
                    spring.target = val

    def _key_geometry(self, v, index=None):
        s = self.s
        unit = 46 * s
        idx = v.selected if index is None else index
        i = 0
        y = 86 * s
        for row in v.keys:
            x = 20 * s
            for label, wd in row:
                if i == idx:
                    return QRectF(x + 3 * s, y + 3 * s, wd * unit - 6 * s, unit - 6 * s)
                x += wd * unit
                i += 1
            y += unit
        return None

    # ------------------------------------------------------------ animation
    def _frame(self):
        now = time.monotonic()
        dt = min(1 / 30.0, max(1 / 500.0, now - self._t))
        self._t = now
        if self.view is None:
            return
        for sp in (self.alpha, self.slide, self.sel_y, self.scroll, self.flash, self.wheel_angle, *self.key_rect):
            sp.step(dt)
        if self.alpha.target == 0.0 and self.alpha.x < 0.02:
            if self.isVisible():
                self.hide()
            self.view = None
            return
        self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, e):
        v = self.view
        if v is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        a = max(0.0, min(1.0, self.alpha.x))
        p.setOpacity(a)
        p.translate(self.slide.x * self.s, 0)
        if v.kind == "list":
            self._paint_list(p, v)
        elif v.kind == "wheel":
            self._paint_wheel(p, v)
        else:
            self._paint_keyboard(p, v)
        p.end()

    def _pill(self, p, rect, radius):
        """The shared selection pill."""
        flash = max(0.0, min(1.0, self.flash.x))
        k = 1.0 - 0.04 * flash
        c = rect.center()
        r = QRectF(c.x() - rect.width() * k / 2, c.y() - rect.height() * k / 2, rect.width() * k, rect.height() * k)
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        fill = QColor(ACCENT)
        fill.setAlphaF(0.24 + 0.36 * flash)
        p.fillPath(path, fill)
        p.setPen(QPen(_alpha(ACCENT, 0.9), 1.5))
        p.drawPath(path)
        p.setPen(QPen(QColor(255, 255, 255, int(50 + 60 * flash)), 1.0))
        inner = r.adjusted(2, 2, -2, -2)
        p.drawRoundedRect(inner, max(1.0, radius - 2), max(1.0, radius - 2))

    def _paint_list(self, p, v):
        s = self.s
        W, H = self.width(), self.height()
        self.panel(p, QRectF(0, 0, W, H), 16)
        pad = 18 * s
        p.setFont(self.mkfont(15, True))
        p.setPen(TEXT)
        fm = QFontMetricsF(self.mkfont(15, True))
        p.drawText(QRectF(pad, 14 * s, W - 2 * pad, 24 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(v.title, Qt.ElideRight, W - 2 * pad - 30 * s))
        if v.depth:
            for i in range(v.depth + 1):
                p.setBrush(LAYER_FG if i == v.depth else MUTED)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(W - pad - i * 10 * s, 26 * s), 2.6 * s, 2.6 * s)
        sub = v.subtitle or (u"Loading…" if v.loading else "")
        if sub:
            p.setFont(self.mkfont(11))
            p.setPen(MUTED)
            p.drawText(QRectF(pad, 38 * s, W - 2 * pad, 18 * s), Qt.AlignLeft | Qt.AlignVCenter, sub)
        top = 66 * s
        rows = min(VISIBLE_ROWS, max(1, len(v.items)))
        view_h = rows * ROW_H * s
        p.save()
        p.setClipRect(QRectF(0, top, W, view_h))
        off = top - self.scroll.x
        if v.items:
            self._pill(p, QRectF(10 * s, off + self.sel_y.x + 2 * s, W - 20 * s, ROW_H * s - 4 * s), 9 * s)
        for i, (label, detail, kind, value, icon) in enumerate(v.items):
            y = off + i * ROW_H * s
            if y + ROW_H * s < top or y > top + view_h:
                continue
            row = QRectF(pad + 4 * s, y, W - 2 * pad - 8 * s, ROW_H * s)
            if kind == "header":
                p.setFont(self.mkfont(11, True))
                p.setPen(LAYER_FG)
                p.drawText(row.adjusted(-6 * s, 6 * s, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, label.upper())
                continue
            sel = i == v.selected
            lf = self.mkfont(13, sel)
            p.setFont(lf)
            p.setPen(TEXT if kind != "info" else MUTED)
            dw = 0.0
            if detail:
                df = self.mkfont(11)
                dw = min(row.width() * 0.45, QFontMetricsF(df).horizontalAdvance(detail) + 4 * s)
            label_rect = QRectF(row.x(), y - (5 * s if kind == "param" else 0), row.width() - dw - 8 * s, ROW_H * s)
            p.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetricsF(lf).elidedText(label, Qt.ElideRight, label_rect.width()))
            if detail:
                p.setFont(self.mkfont(11))
                p.setPen(LAYER_FG if sel else MUTED)
                p.drawText(QRectF(row.right() - dw, y - (5 * s if kind == "param" else 0), dw, ROW_H * s),
                           Qt.AlignRight | Qt.AlignVCenter,
                           QFontMetricsF(self.mkfont(11)).elidedText(detail, Qt.ElideRight, dw))
            if kind == "param" and value is not None:
                bar = QRectF(row.x(), y + ROW_H * s - 11 * s, row.width(), 3 * s)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 28))
                p.drawRoundedRect(bar, 1.5 * s, 1.5 * s)
                p.setBrush(ACCENT if sel else QColor(170, 160, 255, 150))
                p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * max(0.0, min(1.0, value)), bar.height()),
                                  1.5 * s, 1.5 * s)
        p.restore()
        n = len(v.items)
        if n > rows:  # scroll thumb
            track = QRectF(W - 6 * s, top, 3 * s, view_h)
            th = max(18 * s, view_h * rows / n)
            ty = top + (view_h - th) * (self.scroll.x / max(1.0, n * ROW_H * s - view_h))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 50))
            p.drawRoundedRect(QRectF(track.x(), ty, track.width(), th), 1.5 * s, 1.5 * s)
        if v.hint:
            p.setPen(QPen(BORDER, 1))
            p.drawLine(int(pad), int(H - 34 * s), int(W - pad), int(H - 34 * s))
            p.setFont(self.mkfont(11))
            p.setPen(MUTED)
            p.drawText(QRectF(pad, H - 32 * s, W - 2 * pad, 26 * s), Qt.AlignLeft | Qt.AlignVCenter, v.hint)

    def _paint_wheel(self, p, v):
        s = self.s
        W = self.width()
        c = QPointF(W / 2, W / 2)
        R, r_in = 190 * s, 74 * s
        n = max(1, len(v.items))
        span = 2 * math.pi / n
        ring = QPainterPath()
        ring.addEllipse(c, R, R)
        hole = QPainterPath()
        hole.addEllipse(c, r_in, r_in)
        ring = ring.subtracted(hole)
        p.fillPath(ring, BG)
        p.setPen(QPen(BORDER, 1))
        p.drawPath(ring)

        # travelling selection wedge
        ang = self.wheel_angle.x
        flash = max(0.0, min(1.0, self.flash.x))
        wedge = QPainterPath()
        start_deg = 90 - math.degrees(ang + span / 2)
        outer = QRectF(c.x() - R + 6 * s, c.y() - R + 6 * s, 2 * (R - 6 * s), 2 * (R - 6 * s))
        inner = QRectF(c.x() - r_in - 4 * s, c.y() - r_in - 4 * s, 2 * (r_in + 4 * s), 2 * (r_in + 4 * s))
        wedge.arcMoveTo(outer, start_deg)
        wedge.arcTo(outer, start_deg, math.degrees(span) - 0.0)
        wedge.arcTo(inner, start_deg + math.degrees(span), -math.degrees(span))
        wedge.closeSubpath()
        fill = QColor(ACCENT)
        fill.setAlphaF(0.26 + 0.34 * flash)
        p.fillPath(wedge, fill)
        p.setPen(QPen(_alpha(ACCENT, 0.9), 1.5))
        p.drawPath(wedge)

        # items
        for i, (label, _, _, active, icon) in enumerate(v.items):
            mid = i * span
            rr = (R + r_in) / 2
            pos = QPointF(c.x() + rr * math.sin(mid), c.y() - rr * math.cos(mid))
            sel = i == v.selected
            col = TEXT if sel else QColor(200, 208, 214)
            isz = 30 * s if sel else 26 * s
            draw_icon(p, icon, QRectF(pos.x() - isz / 2, pos.y() - isz / 2 - 10 * s, isz, isz), col)
            f = self.mkfont(11, sel)
            p.setFont(f)
            p.setPen(col if sel else MUTED)
            fm = QFontMetricsF(f)
            text = fm.elidedText(label, Qt.ElideRight, 96 * s)
            p.drawText(QRectF(pos.x() - 50 * s, pos.y() + 10 * s, 100 * s, 16 * s), Qt.AlignCenter, text)
            if active:
                p.setPen(Qt.NoPen)
                p.setBrush(LAYER_FG)
                p.drawEllipse(QPointF(pos.x(), pos.y() + 30 * s), 2.5 * s, 2.5 * s)

        # centre: selected name + aim dot
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(14, 17, 20, 200))
        p.drawEllipse(c, r_in - 6 * s, r_in - 6 * s)
        if v.items:
            label = v.items[v.selected][0]
            f = self.mkfont(13, True)
            p.setFont(f)
            p.setPen(TEXT)
            p.drawText(QRectF(c.x() - r_in + 10 * s, c.y() - 16 * s, 2 * r_in - 20 * s, 20 * s), Qt.AlignCenter,
                       QFontMetricsF(f).elidedText(label, Qt.ElideRight, 2 * r_in - 24 * s))
        p.setFont(self.mkfont(10))
        p.setPen(MUTED)
        p.drawText(QRectF(c.x() - r_in, c.y() + 4 * s, 2 * r_in, 16 * s), Qt.AlignCenter, v.title)
        if v.aim is not None:
            d = QPointF(c.x() + (r_in - 14 * s) * math.sin(v.aim), c.y() - (r_in - 14 * s) * math.cos(v.aim))
            p.setBrush(ACCENT)
            p.drawEllipse(d, 4 * s, 4 * s)
        p.setFont(self.mkfont(11))
        p.setPen(MUTED)
        p.drawText(QRectF(0, W - 22 * s, W, 18 * s), Qt.AlignCenter, v.hint)

    def _paint_keyboard(self, p, v):
        s = self.s
        W, H = self.width(), self.height()
        self.panel(p, QRectF(0, 0, W, H), 16)
        unit = 46 * s
        # text preview field
        field = QRectF(20 * s, 18 * s, W - 40 * s, 50 * s)
        path = QPainterPath()
        path.addRoundedRect(field, 10 * s, 10 * s)
        p.fillPath(path, QColor(255, 255, 255, 14))
        p.setPen(QPen(BORDER, 1))
        p.drawPath(path)
        f = self.mkfont(16)
        p.setFont(f)
        p.setPen(TEXT if v.text else MUTED)
        shown = v.text or "Type into the focused field (e.g. plugin search)"
        p.drawText(field.adjusted(14 * s, 0, -14 * s, 0), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(f).elidedText(shown, Qt.ElideLeft, field.width() - 28 * s))
        if v.text:  # caret
            tw = min(QFontMetricsF(f).horizontalAdvance(v.text), field.width() - 30 * s)
            blink = (time.monotonic() * 2) % 2 < 1.2
            if blink:
                p.setPen(QPen(ACCENT, 2))
                x = field.x() + 14 * s + tw + 2 * s
                p.drawLine(QPointF(x, field.y() + 14 * s), QPointF(x, field.bottom() - 14 * s))
        # keys
        y = 86 * s
        i = 0
        for row in v.keys:
            x = 20 * s
            for label, wd in row:
                r = QRectF(x + 3 * s, y + 3 * s, wd * unit - 6 * s, unit - 6 * s)
                kp = QPainterPath()
                kp.addRoundedRect(r, 8 * s, 8 * s)
                special = len(label) > 1
                base = QColor(255, 255, 255, 22 if not special else 14)
                if label == "Shift" and v.shift:
                    base = QColor(139, 124, 255, 90)
                p.fillPath(kp, base)
                p.setPen(QPen(QColor(255, 255, 255, 24), 1))
                p.drawPath(kp)
                i += 1
                x += wd * unit
            y += unit
        # travelling key highlight (drawn between keycaps and labels)
        kx, ky, kw, kh = (sp.x for sp in self.key_rect)
        if kw > 1:
            self._pill(p, QRectF(kx, ky, kw, kh), 8 * s)
        y = 86 * s
        for row in v.keys:
            x = 20 * s
            for label, wd in row:
                r = QRectF(x + 3 * s, y + 3 * s, wd * unit - 6 * s, unit - 6 * s)
                special = len(label) > 1
                p.setFont(self.mkfont(12 if special else 16, not special))
                p.setPen(TEXT)
                p.drawText(r, Qt.AlignCenter, label)
                x += wd * unit
            y += unit
        p.setFont(self.mkfont(11))
        p.setPen(MUTED)
        p.drawText(QRectF(20 * s, H - 30 * s, W - 40 * s, 22 * s), Qt.AlignCenter, v.hint)
