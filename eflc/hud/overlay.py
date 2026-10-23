"""HUD overlay windows: hint card, all-shortcuts panel, toasts.

All windows are frameless, always on top, click-through and never take focus,
so FL keeps keyboard focus while the HUD is visible.
"""

import ctypes

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED = 0x80000
WS_EX_NOACTIVATE = 0x8000000

BG = QColor(14, 17, 20, 228)
BORDER = QColor(255, 255, 255, 34)
TEXT = QColor(230, 237, 241)
MUTED = QColor(143, 160, 170)
LAYER_BG = QColor(59, 47, 107)
LAYER_FG = QColor(207, 200, 255)
OK = QColor(95, 200, 120)
BAD = QColor(220, 90, 90)
CHIP = {
    "A": QColor(47, 125, 79), "B": QColor(163, 58, 58),
    "X": QColor(44, 94, 158), "Y": QColor(155, 122, 28),
}
CHIP_DEFAULT = QColor(58, 66, 72)
ARROWS = set("←↑→↓")
TRIGGER_TAG = {"press": "", "repeat": "", "release": "tap", "hold": "hold",
               "double": "2×", "axis": "stick", "seq": "code", "mod": ""}


def _apply_noactivate(widget):
    try:
        hwnd = int(widget.winId())
        u32 = ctypes.windll.user32
        get = getattr(u32, "GetWindowLongPtrW", u32.GetWindowLongW)
        set_ = getattr(u32, "SetWindowLongPtrW", u32.SetWindowLongW)
        get.restype = ctypes.c_ssize_t
        set_.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        style = get(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
        set_(ctypes.c_void_p(hwnd), GWL_EXSTYLE,
             style | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
    except Exception:
        pass


class Overlay(QWidget):
    def __init__(self, scale=1.0):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.s = scale
        self._styled = False

    def showEvent(self, e):
        super().showEvent(e)
        if not self._styled:
            _apply_noactivate(self)
            self._styled = True

    def mkfont(self, px, bold=False):
        f = QFont()
        f.setFamilies(["Segoe UI", "Segoe UI Symbol"])
        f.setPixelSize(max(9, round(px * self.s)))
        f.setWeight(QFont.DemiBold if bold else QFont.Normal)
        return f

    def panel(self, p, rect, radius=12):
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius * self.s, radius * self.s)
        p.fillPath(path, BG)
        p.setPen(QPen(BORDER, 1))
        p.drawPath(path)

    def chip(self, p, x, y, text, h):
        """Draws a button glyph (p=None only measures); returns its width."""
        f = self.mkfont(11, True)
        if ARROWS.intersection(text):
            f.setFamilies(["Segoe UI Symbol", "Segoe UI"])  # monochrome arrows, not colour emoji
        w = max(h, QFontMetricsF(f).horizontalAdvance(text) + 10 * self.s)
        if p is None:
            return w
        color = CHIP.get(text.split(" ")[-1], CHIP_DEFAULT)
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
        p.fillPath(path, color)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(x, y, w, h), Qt.AlignCenter, text)
        return w

    def entry_chips(self, p, x, y, text, h):
        """'LT + X' -> two chips with a plus between them (p=None only measures)."""
        cx = x
        for i, part in enumerate(text.split(" + ")):
            if i:
                if p is not None:
                    p.setFont(self.mkfont(11))
                    p.setPen(MUTED)
                    p.drawText(QRectF(cx, y, 12 * self.s, h), Qt.AlignCenter, "+")
                cx += 12 * self.s
            cx += self.chip(p, cx, y, part, h) + 2 * self.s
        return cx - x

    def chip_column(self, entries, h, cap):
        """Shared label x-offset so labels line up across rows."""
        return min(cap, max((self.entry_chips(None, 0, 0, e.input, h) for e in entries), default=h))


class HintCard(Overlay):
    """Context-aware shortcut card (bottom corner)."""

    MAX_ROWS = 11

    def __init__(self, scale=1.0, corner="bottom-right", margin=24):
        super().__init__(scale)
        self.corner, self.margin = corner, margin
        self.model = None
        self.width_px = round(272 * scale)

    def set_model(self, model):
        self.model = model
        self._relayout()
        self.update()

    def set_scale(self, scale):
        self.s = scale
        self.width_px = round(272 * scale)
        self._relayout()
        self.update()

    def _rows(self):
        m = self.model
        if m is None:
            return []
        if not m.pad:
            return []
        return m.entries[: self.MAX_ROWS]

    def _relayout(self):
        s = self.s
        rows = self._rows()
        h = 14 * s + 20 * s                               # padding + title
        if self.model and self.model.fl_info:
            h += 16 * s
        h += 8 * s
        h += (len(rows) if rows else 1) * 26 * s
        if self.model and len(self.model.entries) > len(rows) and self.model.pad:
            h += 18 * s
        h += 12 * s + 18 * s + 12 * s                     # divider + status + padding
        self.resize(self.width_px, round(h))
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is None:
            return
        mg = round(self.margin * s)
        x = scr.right() - self.width() - mg if "right" in self.corner else scr.left() + mg
        y = scr.bottom() - self.height() - mg if "bottom" in self.corner else scr.top() + mg
        self.move(x, y)

    def paintEvent(self, e):
        m = self.model
        if m is None:
            return
        s = self.s
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.panel(p, QRectF(self.rect()))
        pad = 14 * s
        x0, y = pad, pad
        w = self.width() - 2 * pad

        # title + layer pill
        p.setFont(self.mkfont(13, True))
        p.setPen(TEXT)
        p.drawText(QRectF(x0, y, w, 20 * s), Qt.AlignLeft | Qt.AlignVCenter, m.title)
        if m.layer:
            label = " + ".join(m.layer) + " layer"
            f = self.mkfont(11, True)
            tw = QFontMetricsF(f).horizontalAdvance(label) + 16 * s
            r = QRectF(x0 + w - tw, y + 1 * s, tw, 18 * s)
            path = QPainterPath()
            path.addRoundedRect(r, 9 * s, 9 * s)
            p.fillPath(path, LAYER_BG)
            p.setFont(f)
            p.setPen(LAYER_FG)
            p.drawText(r, Qt.AlignCenter, label)
        y += 20 * s
        if m.fl_info:
            p.setFont(self.mkfont(11))
            p.setPen(MUTED)
            p.drawText(QRectF(x0, y, w, 16 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetricsF(self.mkfont(11)).elidedText(m.fl_info, Qt.ElideRight, w))
            y += 16 * s
        y += 8 * s

        rows = self._rows()
        ch = 20 * s
        if not m.pad:
            p.setFont(self.mkfont(12))
            p.setPen(MUTED)
            p.drawText(QRectF(x0, y, w, 26 * s), Qt.AlignLeft | Qt.AlignVCenter, "Connect an Xbox controller")
            y += 26 * s
        elif not rows:
            p.setFont(self.mkfont(12))
            p.setPen(MUTED)
            p.drawText(QRectF(x0, y, w, 26 * s), Qt.AlignLeft | Qt.AlignVCenter, "No bindings on this layer")
            y += 26 * s
        col = self.chip_column(rows, ch, w * 0.5)
        for ent in rows:
            cy = y + 3 * s
            cw = max(col, self.entry_chips(p, x0, cy, ent.input, ch))
            tag = TRIGGER_TAG.get(ent.trigger, ent.trigger)
            tag_w = 0
            if tag:
                f = self.mkfont(10)
                tag_w = QFontMetricsF(f).horizontalAdvance(tag) + 4 * s
                p.setFont(f)
                p.setPen(MUTED)
                p.drawText(QRectF(x0 + w - tag_w, y, tag_w, 26 * s), Qt.AlignRight | Qt.AlignVCenter, tag)
            lx = x0 + cw + 8 * s
            lw = w - (lx - x0) - tag_w - 4 * s
            f = self.mkfont(12)
            p.setFont(f)
            p.setPen(TEXT)
            p.drawText(QRectF(lx, y, lw, 26 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetricsF(f).elidedText(ent.label, Qt.ElideRight, lw))
            y += 26 * s
        if m.pad and len(m.entries) > len(rows):
            p.setFont(self.mkfont(11))
            p.setPen(MUTED)
            p.drawText(QRectF(x0, y, w, 18 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       "+%d more - hold View for all" % (len(m.entries) - len(rows)))
            y += 18 * s

        y += 6 * s
        p.setPen(QPen(BORDER, 1))
        p.drawLine(int(x0), int(y), int(x0 + w), int(y))
        y += 6 * s
        self._status(p, x0, y, w, m)
        p.end()

    def _status(self, p, x, y, w, m):
        s = self.s
        h = 18 * s
        f = self.mkfont(11)
        p.setFont(f)
        fm = QFontMetricsF(f)
        cx = x
        for name, ok in (("Pad", m.pad), ("FL", m.fl)):
            p.setBrush(OK if ok else BAD)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(cx, y + h / 2 - 3.5 * s, 7 * s, 7 * s))
            cx += 11 * s
            p.setPen(MUTED)
            p.drawText(QRectF(cx, y, 60 * s, h), Qt.AlignLeft | Qt.AlignVCenter, name)
            cx += fm.horizontalAdvance(name) + 12 * s
        label = m.profile + (" · " + m.pointer if getattr(m, "pointer", "") else "")
        prof = fm.elidedText(label, Qt.ElideRight, x + w - cx)
        p.drawText(QRectF(cx, y, x + w - cx, h), Qt.AlignRight | Qt.AlignVCenter, prof)


class AllShortcuts(Overlay):
    """Full shortcut list for the current context chain, shown while View is held."""

    def __init__(self, scale=1.0):
        super().__init__(scale)
        self.model = None
        self._cols = []

    def set_model(self, model):
        self.model = model
        if self.isVisible():
            self._relayout()
            self.update()

    def showEvent(self, e):
        self._relayout()
        super().showEvent(e)

    def _relayout(self):
        s = self.s
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is None or self.model is None:
            return
        row_h, head_h, col_w = 24 * s, 30 * s, 300 * s
        max_h = scr.height() * 0.8
        cols, cur, cur_h = [], [], 0.0
        for title, entries in self.model.groups:
            gh = head_h + len(entries) * row_h + 10 * s
            if cur and cur_h + gh > max_h:
                cols.append(cur)
                cur, cur_h = [], 0.0
            cur.append((title, entries))
            cur_h += gh
        if cur:
            cols.append(cur)
        self._cols = cols[:4]
        heights = [sum(head_h + len(e) * row_h + 10 * s for _, e in c) for c in self._cols] or [100]
        w = len(self._cols) * col_w + (len(self._cols) + 1) * 20 * s
        h = max(heights) + 70 * s
        self.resize(round(max(w, 320 * s)), round(min(h, scr.height() * 0.9)))
        self.move(scr.center().x() - self.width() // 2, scr.center().y() - self.height() // 2)

    def paintEvent(self, e):
        if self.model is None:
            return
        s = self.s
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.panel(p, QRectF(self.rect()), 16)
        p.setFont(self.mkfont(15, True))
        p.setPen(TEXT)
        p.drawText(QRectF(20 * s, 14 * s, self.width() - 40 * s, 28 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   "All shortcuts - %s" % self.model.profile)
        col_w, row_h, head_h = 300 * s, 24 * s, 30 * s
        x = 20 * s
        for col in self._cols:
            y = 56 * s
            for title, entries in col:
                p.setFont(self.mkfont(12, True))
                p.setPen(LAYER_FG)
                p.drawText(QRectF(x, y, col_w, head_h), Qt.AlignLeft | Qt.AlignVCenter, title)
                y += head_h
                col = self.chip_column(entries, 18 * s, col_w * 0.55)
                for ent in entries:
                    cw = max(col, self.entry_chips(p, x, y + 3 * s, ent.input, 18 * s))
                    tag = TRIGGER_TAG.get(ent.trigger, ent.trigger)
                    f = self.mkfont(12)
                    p.setFont(f)
                    p.setPen(TEXT)
                    lw = col_w - cw - 50 * s
                    p.drawText(QRectF(x + cw + 8 * s, y, lw, row_h), Qt.AlignLeft | Qt.AlignVCenter,
                               QFontMetricsF(f).elidedText(ent.label, Qt.ElideRight, lw))
                    if tag:
                        p.setFont(self.mkfont(10))
                        p.setPen(MUTED)
                        p.drawText(QRectF(x + col_w - 44 * s, y, 40 * s, row_h), Qt.AlignRight | Qt.AlignVCenter, tag)
                    y += row_h
                y += 10 * s
            x += col_w + 20 * s
        p.end()


class Toast(Overlay):
    def __init__(self, scale=1.0):
        super().__init__(scale)
        self.text = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_text(self, text, ms=2400):
        self.text = text
        f = self.mkfont(13)
        w = QFontMetricsF(f).horizontalAdvance(text) + 36 * self.s
        self.resize(round(w), round(38 * self.s))
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is not None:
            self.move(scr.center().x() - self.width() // 2, scr.top() + round(48 * self.s))
        self.update()
        self.show()
        self._timer.start(ms)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.panel(p, QRectF(self.rect()), 19)
        p.setFont(self.mkfont(13))
        p.setPen(TEXT)
        p.drawText(QRectF(self.rect()), Qt.AlignCenter, self.text)
        p.end()
