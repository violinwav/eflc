"""Controller-shaped hint view: the mapping drawn on a gamepad.

An alternative to the list card. A vector gamepad sits in the middle; every
control has a callout (left column for the left half of the pad, right column
for the right half) joined by a thin leader line. Callouts show what the
control does *right now*: context-aware and layer-aware, so holding LT / RT /
RB re-labels the whole pad. Pressed controls light up; controls without a
binding on the current layer dim out.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen

from .overlay import BAD, BORDER, CHIP, LAYER_BG, LAYER_FG, MUTED, OK, TEXT, Overlay

ACCENT = QColor(139, 124, 255)
BODY = QColor(31, 36, 42, 235)
BODY_EDGE = QColor(255, 255, 255, 40)
PART = QColor(52, 58, 66)
PART_EDGE = QColor(255, 255, 255, 30)
DIM = QColor(120, 132, 140)

GLYPH_TO_BUTTON = {"↑": "UP", "↓": "DOWN", "←": "LEFT", "→": "RIGHT"}
SECONDARY = {"release": "tap", "hold": "hold", "double": "2×"}

# Control anchors on the pad, as fractions of the pad drawing (w, h).
ANCHORS = {
    "LT": (0.26, 0.02), "LB": (0.25, 0.11), "BACK": (0.42, 0.36), "LS": (0.27, 0.38), "DPAD": (0.37, 0.64),
    "RT": (0.74, 0.02), "RB": (0.75, 0.11), "START": (0.58, 0.36), "Y": (0.73, 0.27),
    "X": (0.665, 0.38), "B": (0.795, 0.38), "A": (0.73, 0.49), "RS": (0.63, 0.64),
}
LEFT_COL = ("LT", "LB", "BACK", "LS", "DPAD")
RIGHT_COL = ("RT", "RB", "Y", "START", "X", "B", "A", "RS")
GLYPH = {"BACK": "View", "START": "Menu", "DPAD": "✛"}


# Which binding becomes the callout's main line: what a plain press does wins,
# then tap, double-tap, hold. The rest go on the muted second line.
PRIORITY = {"axis": 0, "press": 1, "repeat": 1, "mod": 2, "release": 3, "double": 4, "hold": 5}
ARROW = {"UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→"}


def mapping_from_model(model):
    """{control: {"main": str, "extra": [str], "layer": bool}} for the current layer."""
    per, dpad, collapsed = {}, {}, None
    for e in model.entries:
        inp, trig, label = e.input, e.trigger, e.label
        if trig == "seq":
            continue
        if len(inp) > 1 and all(ch in GLYPH_TO_BUTTON for ch in inp):   # collapsed D-pad row
            collapsed = label
        elif inp in GLYPH_TO_BUTTON:
            dpad[GLYPH_TO_BUTTON[inp]] = label
        elif inp in ANCHORS:
            per.setdefault(inp, []).append((trig, label))
    out = {}
    for key, items in per.items():
        items.sort(key=lambda it: PRIORITY.get(it[0], 9))
        main_t, main = items[0]
        extra = [SECONDARY[main_t]] if main_t in SECONDARY else []
        for t, label in items[1:]:
            prefix = "click" if main_t == "axis" and t in ("press", "repeat") else SECONDARY.get(t, "")
            extra.append("%s: %s" % (prefix, label) if prefix else label)
        out[key] = {"main": main, "extra": extra}
    if collapsed:
        out["DPAD"] = {"main": collapsed, "extra": []}
    elif dpad:
        line = lambda ks: " · ".join("%s %s" % (ARROW[k], dpad[k]) for k in ks if k in dpad)
        first, second = line(("UP", "DOWN")), line(("LEFT", "RIGHT"))
        out["DPAD"] = {"main": first or second, "extra": [second] if first and second else [], "raw": True}
    for b in getattr(model, "layers", ()):
        if b in ANCHORS and b not in out:
            out[b] = {"main": "", "extra": [], "layer": True}
    return out


class ControllerCard(Overlay):
    """Corner HUD that shows the live mapping on a gamepad drawing."""

    def __init__(self, scale=1.0, corner="bottom-right", margin=24):
        super().__init__(scale)
        self.corner, self.margin = corner, margin
        self.model = None
        self._map = {}

    def set_scale(self, scale):
        self.s = scale
        self._relayout()
        self.update()

    def set_model(self, model):
        self.model = model
        self._map = mapping_from_model(model) if model is not None else {}
        self._relayout()
        self.update()

    # ------------------------------------------------------------ geometry
    def _size(self):
        s = self.s
        return 640 * s, 300 * s

    def _relayout(self):
        w, h = self._size()
        self.resize(int(w), int(h))
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr is None:
            return
        mg = round(self.margin * self.s)
        x = scr.right() - self.width() - mg if "right" in self.corner else scr.left() + mg
        y = scr.bottom() - self.height() - mg if "bottom" in self.corner else scr.top() + mg
        self.move(x, y)

    def _pad_rect(self):
        s = self.s
        W, H = self.width(), self.height()
        pw, ph = 250 * s, 175 * s
        return QRectF((W - pw) / 2, 58 * s, pw, ph)

    def _anchor(self, key):
        r = self._pad_rect()
        fx, fy = ANCHORS[key]
        return QPointF(r.x() + fx * r.width(), r.y() + fy * r.height())

    # ------------------------------------------------------------ painting
    def paintEvent(self, e):
        m = self.model
        if m is None:
            return
        s = self.s
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.panel(p, QRectF(self.rect()), 14)
        W, H = self.width(), self.height()
        pad = 16 * s
        # header: context + layer
        p.setFont(self.mkfont(14, True))
        p.setPen(TEXT)
        p.drawText(QRectF(pad, 12 * s, W / 2, 22 * s), Qt.AlignLeft | Qt.AlignVCenter, m.title)
        if m.fl_info:
            p.setFont(self.mkfont(11))
            p.setPen(MUTED)
            tw = QFontMetricsF(self.mkfont(14, True)).horizontalAdvance(m.title)
            p.drawText(QRectF(pad + tw + 10 * s, 12 * s, W / 2, 22 * s), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetricsF(self.mkfont(11)).elidedText(m.fl_info, Qt.ElideRight, W / 2 - tw))
        if m.layer:
            label = " + ".join(m.layer) + " layer"
            f = self.mkfont(11, True)
            lw = QFontMetricsF(f).horizontalAdvance(label) + 18 * s
            r = QRectF(W - pad - lw, 13 * s, lw, 20 * s)
            path = QPainterPath()
            path.addRoundedRect(r, 10 * s, 10 * s)
            p.fillPath(path, LAYER_BG)
            p.setFont(f)
            p.setPen(LAYER_FG)
            p.drawText(r, Qt.AlignCenter, label)

        if not m.pad:
            p.setFont(self.mkfont(13))
            p.setPen(MUTED)
            p.drawText(QRectF(0, 0, W, H), Qt.AlignCenter, "Connect a controller")
            p.end()
            return

        self._draw_pad(p, m)
        self._draw_callouts(p, m, LEFT_COL, left=True)
        self._draw_callouts(p, m, RIGHT_COL, left=False)
        self._draw_status(p, m)
        p.end()

    def _lit(self, m, key):
        if key == "DPAD":
            return any(b in m.pressed for b in ("UP", "DOWN", "LEFT", "RIGHT"))
        return key in m.pressed or key in m.layer

    def _part_colors(self, m, key):
        if self._lit(m, key):
            return QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 210), QColor(220, 214, 255)
        has = bool(self._map.get(key, {}).get("main"))
        base = QColor(PART) if has else QColor(PART.red(), PART.green(), PART.blue(), 150)
        return base, PART_EDGE

    def _draw_pad(self, p, m):
        s = self.s
        r = self._pad_rect()
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        # triggers + bumpers (behind the body)
        for key, (fx0, fx1, fy0, fy1, rad) in {
                "LT": (0.17, 0.35, -0.04, 0.10, 6), "RT": (0.65, 0.83, -0.04, 0.10, 6),
                "LB": (0.12, 0.40, 0.06, 0.17, 7), "RB": (0.60, 0.88, 0.06, 0.17, 7)}.items():
            fill, edge = self._part_colors(m, key)
            pr = QRectF(x + fx0 * w, y + fy0 * h, (fx1 - fx0) * w, (fy1 - fy0) * h)
            path = QPainterPath()
            path.addRoundedRect(pr, rad * s, rad * s)
            p.fillPath(path, fill)
            p.setPen(QPen(edge, 1))
            p.drawPath(path)
        # body: rounded top + two grips
        body = QPainterPath()
        body.addRoundedRect(QRectF(x + 0.08 * w, y + 0.13 * h, 0.84 * w, 0.52 * h), 0.24 * h, 0.24 * h)
        for cx in (0.22, 0.78):
            grip = QPainterPath()
            grip.addEllipse(QPointF(x + cx * w, y + 0.66 * h), 0.15 * w, 0.32 * h)
            body = body.united(grip)
        p.fillPath(body, BODY)
        p.setPen(QPen(BODY_EDGE, 1.2))
        p.drawPath(body)
        # guide button
        g = QPointF(x + 0.5 * w, y + 0.22 * h)
        p.setPen(QPen(PART_EDGE, 1))
        p.setBrush(QColor(60, 66, 74))
        p.drawEllipse(g, 0.035 * w, 0.035 * w)
        # sticks
        for key in ("LS", "RS"):
            fill, edge = self._part_colors(m, key)
            c = self._anchor(key)
            p.setPen(QPen(QColor(0, 0, 0, 120), 2))
            p.setBrush(QColor(20, 23, 27))
            p.drawEllipse(c, 0.085 * w, 0.085 * w)
            p.setPen(QPen(edge, 1))
            p.setBrush(fill)
            p.drawEllipse(c, 0.058 * w, 0.058 * w)
        # d-pad
        fill, edge = self._part_colors(m, "DPAD")
        c = self._anchor("DPAD")
        a, t = 0.06 * w, 0.022 * w
        cross = QPainterPath()
        cross.addRoundedRect(QRectF(c.x() - a, c.y() - t, 2 * a, 2 * t), 2, 2)
        v = QPainterPath()
        v.addRoundedRect(QRectF(c.x() - t, c.y() - a, 2 * t, 2 * a), 2, 2)
        cross = cross.united(v)
        p.fillPath(cross, fill)
        p.setPen(QPen(edge, 1))
        p.drawPath(cross)
        for d, (dx, dy) in (("UP", (0, -1)), ("DOWN", (0, 1)), ("LEFT", (-1, 0)), ("RIGHT", (1, 0))):
            if d in m.pressed:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(220, 214, 255))
                p.drawEllipse(QPointF(c.x() + dx * a * 0.65, c.y() + dy * a * 0.65), t * 0.7, t * 0.7)
        # view / menu
        for key in ("BACK", "START"):
            fill, edge = self._part_colors(m, key)
            c = self._anchor(key)
            p.setPen(QPen(edge, 1))
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(c.x() - 0.03 * w, c.y() - 0.018 * w, 0.06 * w, 0.036 * w), 4, 4)
        # face buttons, in their Xbox colours
        for key in ("Y", "X", "B", "A"):
            c = self._anchor(key)
            col = QColor(CHIP[key])
            lit = self._lit(m, key)
            has = bool(self._map.get(key, {}).get("main"))
            if not has and not lit:
                col.setAlpha(110)
            p.setPen(QPen(QColor(255, 255, 255, 200) if lit else QColor(0, 0, 0, 90), 2 if lit else 1))
            p.setBrush(col.lighter(135) if lit else col)
            rr = 0.036 * w
            p.drawEllipse(c, rr, rr)
            p.setFont(self.mkfont(10, True))
            p.setPen(QColor(255, 255, 255))
            p.drawText(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), Qt.AlignCenter, key)

    def _draw_callouts(self, p, m, keys, left):
        s = self.s
        W = self.width()
        pr = self._pad_rect()
        col_w = pr.x() - 22 * s
        top, bottom = 50 * s, pr.bottom() + 12 * s
        n = len(keys)
        step = (bottom - top) / n
        for i, key in enumerate(keys):
            info = self._map.get(key, {})
            main = info.get("main") or ""
            extra = " · ".join(info.get("extra", []))
            cy = top + step * (i + 0.5)
            lit = self._lit(m, key)
            if not main and info.get("layer"):
                main_text, dim_main = ("layer held" if key in m.layer else "layer"), True
            else:
                main_text, dim_main = (main or "—"), not main
            # leader line: anchor -> elbow -> label edge
            a = self._anchor(key)
            edge_x = pr.x() - 10 * s if left else pr.right() + 10 * s
            line = QColor(ACCENT) if lit else QColor(255, 255, 255, 46 if main else 22)
            p.setPen(QPen(line, 1))
            path = QPainterPath(a)
            path.lineTo(QPointF(edge_x + (8 * s if left else -8 * s), cy))
            path.lineTo(QPointF(edge_x, cy))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(line)
            p.drawEllipse(a, 2.2 * s, 2.2 * s)
            # glyph chip next to the leader line, text running away from the pad
            glyph = GLYPH.get(key, key)
            ch = 18 * s
            cw = self.chip(None, 0, 0, glyph, ch)
            chip_y = cy - ch / 2 - (5 * s if extra else 0)
            if left:
                chip_x = edge_x - 4 * s - cw
                tx, tw = 10 * s, chip_x - 6 * s - 10 * s
                align = Qt.AlignRight | Qt.AlignVCenter
                elide = Qt.ElideLeft
            else:
                chip_x = edge_x + 6 * s
                tx = chip_x + cw + 6 * s
                tw = W - 10 * s - tx
                align = Qt.AlignLeft | Qt.AlignVCenter
                elide = Qt.ElideRight
            self.chip(p, chip_x, chip_y, glyph, ch)
            f = self.mkfont(12, lit)
            p.setFont(f)
            p.setPen(DIM if dim_main else TEXT)
            ty = cy - (13 * s if extra else 9 * s)
            p.drawText(QRectF(tx, ty, tw, 18 * s), align,
                       QFontMetricsF(f).elidedText(main_text, Qt.ElideRight if not left else elide, tw))
            if extra:
                fe = self.mkfont(10 if not info.get("raw") else 11)
                p.setFont(fe)
                p.setPen(MUTED if not info.get("raw") else TEXT)
                p.drawText(QRectF(tx, ty + 16 * s, tw, 14 * s), align,
                           QFontMetricsF(fe).elidedText(extra, Qt.ElideRight, tw))

    def _draw_status(self, p, m):
        s = self.s
        W, H = self.width(), self.height()
        y = H - 26 * s
        p.setPen(QPen(BORDER, 1))
        p.drawLine(int(16 * s), int(y - 6 * s), int(W - 16 * s), int(y - 6 * s))
        f = self.mkfont(11)
        p.setFont(f)
        fm = QFontMetricsF(f)
        cx = 16 * s
        for name, ok in (("Pad", m.pad), ("FL", m.fl)):
            p.setPen(Qt.NoPen)
            p.setBrush(OK if ok else BAD)
            p.drawEllipse(QRectF(cx, y + 9 * s - 3.5 * s, 7 * s, 7 * s))
            cx += 11 * s
            p.setPen(MUTED)
            p.drawText(QRectF(cx, y, 60 * s, 18 * s), Qt.AlignLeft | Qt.AlignVCenter, name)
            cx += fm.horizontalAdvance(name) + 12 * s
        label = m.profile + (" · " + m.pointer if m.pointer else "")
        p.drawText(QRectF(cx, y, W - 16 * s - cx, 18 * s), Qt.AlignRight | Qt.AlignVCenter,
                   fm.elidedText(label, Qt.ElideRight, W - 16 * s - cx))
