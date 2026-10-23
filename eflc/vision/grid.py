"""Grid detection for FL's Playlist and Piano roll from a window capture.

Grid lines run the full height/width of the grid, so the per-column median
(or per-row median) of the luminance isolates them from notes and clips. Lines
may be brighter (Piano roll) or darker (Playlist) than the background, so we
use the absolute deviation. Spacing can be fractional (31.5 px), so lines are
modelled as offset + k * spacing and fitted with least squares.

Piano roll pitch: black keys on the keyboard strip are exactly one row tall,
which tells us which rows are black keys; the 2-3 pattern fixes the pitch class.
"""

from dataclasses import dataclass, field

import numpy as np

BLACK_KEYS = {1, 3, 6, 8, 10}   # pitch classes (C = 0)


@dataclass
class Lines:
    offset: float
    spacing: float
    confidence: float

    def positions(self, lo, hi):
        """Line coordinates within [lo, hi]."""
        k0 = int(np.ceil((lo - self.offset) / self.spacing))
        k1 = int(np.floor((hi - self.offset) / self.spacing))
        return [self.offset + k * self.spacing for k in range(k0, k1 + 1)]


def luminance(rgb):
    return rgb.astype(np.float32).mean(axis=2)


def periodic_lines(profile, min_period, max_period):
    """Find evenly spaced lines in a 1D profile. Returns Lines or None."""
    p = np.asarray(profile, dtype=np.float32)
    n = len(p)
    if n < min_period * 3:
        return None
    # Deviation from a *local* median: thin grid lines stand out, while broad
    # blocks (clips, notes, black-key row shading) follow the baseline and vanish.
    half = 4
    padded = np.pad(p, half, mode="edge")
    baseline = np.median(np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1), axis=1)
    dev = np.abs(p - baseline)
    noise = max(2.0, float(np.percentile(dev, 60)))
    sig = np.clip(dev - noise, 0, None)
    if not sig.any():
        return None

    # Candidate peaks: local maxima of the deviation signal.
    peaks = [i for i in range(1, n - 1) if sig[i] > 0 and sig[i] >= sig[i - 1] and sig[i] >= sig[i + 1]]
    if len(peaks) < 3:
        return None

    # Autocorrelation picks the fundamental spacing (smallest strong period).
    max_period = min(max_period, n // 2)
    lags = np.arange(min_period, max_period + 1)
    # Slight blur: 1 px lines at fractional spacing (31/32/31/32...) would
    # otherwise correlate better at two periods than at one.
    smooth = np.convolve(sig, np.array([1.0, 2.0, 1.0]) / 4.0, mode="same")
    s0 = smooth - smooth.mean()
    ac = np.array([float(np.dot(s0[:-lag], s0[lag:])) / (n - lag) for lag in lags])
    if ac.max() <= 0:
        return None
    strong = np.where(ac >= 0.55 * ac.max())[0]
    i = int(strong[0])
    # climb to the local maximum of that first strong lobe
    while i + 1 < len(ac) and ac[i + 1] >= ac[i]:
        i += 1
    period = float(lags[i])
    if 0 < i < len(ac) - 1:  # parabolic refinement for fractional spacing
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        den = a - 2 * b + c
        if den != 0:
            period += 0.5 * (a - c) / den

    # Phase: the offset whose comb hits the most signal.
    best_phase, best_score = 0.0, -1.0
    for ph in np.arange(0, period, 0.5):
        idx = np.round(np.arange(ph, n, period)).astype(int)
        idx = idx[idx < n]
        lo = np.clip(idx - 1, 0, n - 1)
        hi = np.clip(idx + 1, 0, n - 1)
        score = float(np.maximum(np.maximum(sig[lo], sig[idx]), sig[hi]).sum())
        if score > best_score:
            best_phase, best_score = float(ph), score

    # Least-squares refinement on peaks that sit near the comb.
    ks, xs = [], []
    for pk in peaks:
        k = round((pk - best_phase) / period)
        if abs(best_phase + k * period - pk) <= max(1.5, period * 0.08):
            ks.append(k)
            xs.append(pk)
    if len(ks) < 3:
        return None
    ks, xs = np.array(ks, dtype=np.float64), np.array(xs, dtype=np.float64)
    if ks.max() == ks.min():
        return None
    spacing, offset = np.polyfit(ks, xs, 1)
    expected = len(Lines(offset, spacing, 0).positions(1, n - 2))
    confidence = min(1.0, len(set(ks.tolist())) / max(1, expected))
    return Lines(float(offset), float(spacing), float(confidence))


@dataclass
class Grid:
    """Detected editor grid in screen coordinates."""
    kind: str
    rect: tuple                      # (left, top, right, bottom)
    xs: list                         # vertical line x positions (cell boundaries)
    ys: list                         # horizontal line y positions (row boundaries)
    pitch: list = field(default=None)   # pitch class per row (len(ys) - 1), piano roll only

    @property
    def cols(self):
        return len(self.xs) - 1

    @property
    def rows(self):
        return len(self.ys) - 1

    def _index(self, lines, v):
        if len(lines) < 2 or v < lines[0] or v >= lines[-1]:
            return None
        i = int(np.searchsorted(lines, v, side="right")) - 1
        return min(max(i, 0), len(lines) - 2)

    def cell_at(self, x, y):
        ix, iy = self._index(self.xs, x), self._index(self.ys, y)
        if ix is None or iy is None:
            return None
        return ix, iy

    def cell_rect(self, ix, iy):
        return (self.xs[ix], self.ys[iy], self.xs[ix + 1], self.ys[iy + 1])

    def center(self, ix, iy):
        l, t, r, b = self.cell_rect(ix, iy)
        return ((l + r) / 2.0, (t + b) / 2.0)

    def allowed_row(self, iy, scale):
        return scale is None or self.pitch is None or self.pitch[iy] in scale

    def nearest_cell(self, x, y, scale=None):
        """Cell nearest to a point, honouring a scale (set of pitch classes)."""
        l, t, r, b = self.rect
        x = min(max(x, self.xs[0]), self.xs[-1] - 0.01)
        y = min(max(y, self.ys[0]), self.ys[-1] - 0.01)
        cell = self.cell_at(x, y)
        if cell is None:
            return None
        ix, iy = cell
        if not self.allowed_row(iy, scale):
            best = None
            for d in range(1, 13):
                for cand in (iy - d, iy + d):
                    if 0 <= cand < self.rows and self.allowed_row(cand, scale):
                        cy = self.center(ix, cand)[1]
                        if best is None or abs(cy - y) < abs(self.center(ix, best)[1] - y):
                            best = cand
                if best is not None:
                    break
            if best is None:
                return None
            iy = best
        return ix, iy

    def step(self, ix, iy, dx, dy, scale=None):
        nx = min(max(ix + dx, 0), self.cols - 1)
        ny = iy
        if dy:
            cand = iy
            while True:
                cand += dy
                if not 0 <= cand < self.rows:
                    break
                if self.allowed_row(cand, scale):
                    ny = cand
                    break
        return nx, ny


def clip_window(window, focus, avoid):
    """Shrink (l, t, r, b) so it excludes every `avoid` rect but still contains focus.

    For each overlapping rect, cut the side that keeps the most area. Returns
    None if an avoid rect covers the focus point itself.
    """
    l, t, r, b = window
    fx, fy = focus
    for al, at, ar, ab in avoid:
        if ar <= l or al >= r or ab <= t or at >= b:
            continue
        if al <= fx < ar and at <= fy < ab:
            return None
        options = []
        if al > fx:
            options.append((l, t, al, b))
        if ar <= fx:
            options.append((ar, t, r, b))
        if at > fy:
            options.append((l, t, r, at))
        if ab <= fy:
            options.append((l, ab, r, b))
        l, t, r, b = max(options, key=lambda o: (o[2] - o[0]) * (o[3] - o[1]))
    return l, t, r, b


def track_rows(img_rgb, headers_local, top, bottom):
    """Playlist track boundaries (image y) from the track-header column, or None.

    Clips never draw over the headers, so this is far more reliable than the
    grid's own separator lines (which clips hide). Each header is its own
    panel with a dark separator row underneath, found individually, so tracks
    of different heights work too. Returns [top, b1, b2, ..., bottom].
    """
    hl, ht, hr, hb = headers_local
    strip = luminance(img_rgb[top:bottom, hl + 4:hr - 4])
    if strip.shape[0] < 30 or strip.shape[1] < 20:
        return None
    dark = np.percentile(strip, 10, axis=1)          # a separator darkens the whole row
    n = len(dark)
    half = 6
    padded = np.pad(dark, half, mode="edge")
    base = np.median(np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1), axis=1)
    cand = (dark <= 0.55 * base) & (base >= 12)
    lines = []
    i = 0
    while i < n:
        if cand[i]:
            j = i
            while j + 1 < n and cand[j + 1]:
                j += 1
            if j - i + 1 <= 4:                      # thin: a separator, not text or a button
                lines.append(top + (i + j) / 2.0 + 0.5)
            i = j + 1
        else:
            i += 1
    if len(lines) < 2:
        return None
    gaps = np.diff(lines)
    if gaps.min() < 14:
        return None
    typical = float(np.median(gaps))
    ys = list(lines)
    if ys[0] - top >= 0.15 * typical:               # partially scrolled first track
        ys.insert(0, float(top))
    if bottom - ys[-1] >= 0.15 * typical:           # last (possibly partial) track
        ys.append(float(bottom))
    return ys


def analyze(img_rgb, kind, grid_local, keyboard_local=None, origin=(0, 0), focus=None,
            reach=(420, 260), avoid=(), headers_local=None):
    """Detect the grid in a form capture.

    grid_local / keyboard_local are (left, top, right, bottom) in image coords;
    origin is the form's screen position. With focus=(x, y) (image coords) the
    profiles come from a window around that point only, so windows overlapping
    other parts of the editor don't disturb detection; the fitted lines are then
    extended across the whole grid. Returns Grid (screen coords) or None.
    """
    gl, gt, gr, gb = grid_local
    al, at, ar, ab = gl, gt, gr, gb
    if focus is not None:
        fx, fy = focus
        rx, ry = reach
        al, ar = max(gl, int(fx - rx)), min(gr, int(fx + rx))
        at, ab = max(gt, int(fy - ry)), min(gb, int(fy + ry))
        if avoid:
            clipped = clip_window((al, at, ar, ab), focus, avoid)
            if clipped is None:
                return None
            al, at, ar, ab = (int(v) for v in clipped)
    lum = luminance(img_rgb[at:ab, al:ar])
    if lum.shape[0] < 20 or lum.shape[1] < 20:
        return None
    col_profile = np.median(lum[::2, :], axis=0)
    vx = periodic_lines(col_profile, 4, 400)
    if vx is None or vx.confidence < 0.5:
        return None
    ox, oy = origin
    # Playlist tracks: from the header column when possible (clips hide grid lines).
    header_rows = track_rows(img_rgb, headers_local, gt, gb) if kind == "playlist" and headers_local else None
    if header_rows is not None:
        ys = [oy + y for y in header_rows]
    else:
        row_profile = np.median(lum[:, ::2], axis=1)
        vy = periodic_lines(row_profile, 5, 80) if kind == "pianoroll" else periodic_lines(row_profile, 16, 400)
        if vy is None or vy.confidence < 0.5:
            return None
        vy = Lines(vy.offset + (at - gt), vy.spacing, vy.confidence)
        ys = [gt + oy + v for v in vy.positions(0, gb - gt - 1)]
    # profile coords are relative to the analysed window: shift to grid coords
    vx = Lines(vx.offset + (al - gl), vx.spacing, vx.confidence)
    xs = [gl + ox + v for v in vx.positions(0, gr - gl - 1)]
    if len(xs) < 2 or len(ys) < 2:
        return None
    pitch = None
    if kind == "pianoroll" and keyboard_local is not None:
        pitch = _pitch_classes(img_rgb, keyboard_local, [y - oy for y in ys])
    return Grid(kind, (gl + ox, gt + oy, gr + ox, gb + oy), xs, ys, pitch)


def _pitch_classes(img_rgb, keyboard_local, ys_local):
    """Pitch class per row from the keyboard strip, or None."""
    kl, kt, kr, kb = keyboard_local
    if kr - kl < 10:
        return None
    x = kl + max(3, int((kr - kl) * 0.2))   # inside the black-key length
    column = luminance(img_rgb[:, x:x + 3]).mean(axis=1)
    rows = len(ys_local) - 1
    dark = []
    for i in range(rows):
        a, b = int(ys_local[i]), int(ys_local[i + 1])
        mid = column[a + max(1, (b - a) // 4): b - max(1, (b - a) // 4)]
        dark.append(float(mid.mean()) if len(mid) else float(column[(a + b) // 2]))
    if not dark:
        return None
    lo, hi = min(dark), max(dark)
    if hi - lo < 40:
        return None
    thresh = (lo + hi) / 2
    black = [v < thresh for v in dark]
    # Row 0 is the highest pitch; pitch class decreases by one per row.
    best, best_score = None, -1
    for top_pc in range(12):
        score = sum((((top_pc - i) % 12) in BLACK_KEYS) == black[i] for i in range(rows))
        if score > best_score:
            best, best_score = top_pc, score
    if best_score < rows * 0.9:
        return None
    return [(best - i) % 12 for i in range(rows)]


SCALES = {
    "Major": (0, 2, 4, 5, 7, 9, 11),
    "Minor": (0, 2, 3, 5, 7, 8, 10),
    "Harmonic minor": (0, 2, 3, 5, 7, 8, 11),
    "Melodic minor": (0, 2, 3, 5, 7, 9, 11),
    "Dorian": (0, 2, 3, 5, 7, 9, 10),
    "Phrygian": (0, 1, 3, 5, 7, 8, 10),
    "Lydian": (0, 2, 4, 6, 7, 9, 11),
    "Mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "Major pentatonic": (0, 2, 4, 7, 9),
    "Minor pentatonic": (0, 3, 5, 7, 10),
    "Blues": (0, 3, 5, 6, 7, 10),
    "Chromatic": tuple(range(12)),
}
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def scale_set(root, name):
    return frozenset((root + i) % 12 for i in SCALES[name])
