"""Selectable-element detection for the magnetic pointer outside the editor grids.

FL draws its own UI, so Windows UI Automation only sees window frames there.
Two vision detectors fill the gap:

  menu_items   FL popup menus (TQuickPopupMenuWindow) and other list-like
               windows: text bands -> evenly sized item rows; separators and
               section headers are skipped.
  detect_blobs compact controls (buttons, icons, knobs, step buttons, labels):
               edge map -> merge letters/shapes -> connected components.

Both take an RGB numpy image and return rects (left, top, right, bottom) in
image coordinates.
"""

import numpy as np

from .grid import luminance


def _runs(flags, max_gap=1):
    """Consecutive True runs, bridging gaps of up to max_gap rows."""
    runs, start, gap = [], None, 0
    for i, f in enumerate(flags):
        if f:
            if start is None:
                start = i
            gap = 0
            end = i
        elif start is not None:
            gap += 1
            if gap > max_gap:
                runs.append((start, end + 1))
                start, gap = None, 0
    if start is not None:
        runs.append((start, end + 1))
    return runs


def menu_items(img_rgb, margin=3, ink_threshold=45):
    """Item rows of a menu/list window. Returns [(l, t, r, b)] in image coords."""
    lum = luminance(img_rgb)
    h, w = lum.shape
    if h < 12 or w < 30:
        return []
    inner = lum[:, margin:w - margin]
    row_bg = np.median(inner, axis=1)
    menu_bg = float(np.median(row_bg))
    ink = np.abs(inner - row_bg[:, None]) > ink_threshold
    count = ink.sum(axis=1)
    width = inner.shape[1]
    text = (count >= 2) & (count < 0.6 * width)       # full-width ink rows are separators
    bands = [(a, b) for a, b in _runs(text, max_gap=2) if 5 <= b - a <= 48]
    # section headers sit on a different background (e.g. "Recent projects")
    bands = [(a, b) for a, b in bands if abs(float(np.median(row_bg[a:b])) - menu_bg) <= 10]
    if not bands:
        return []
    centres = np.array([(a + b) / 2.0 for a, b in bands])
    if len(centres) >= 2:
        d = np.diff(centres)
        pitch = float(np.median(d[d < 1.6 * d.min()]))
    else:
        a, b = bands[0]
        pitch = (b - a) * 2.2
    out = []
    for i, c in enumerate(centres):
        top = c - pitch / 2
        bottom = c + pitch / 2
        if i > 0:
            top = max(top, (centres[i - 1] + c) / 2)
        if i + 1 < len(centres):
            bottom = min(bottom, (centres[i + 1] + c) / 2)
        out.append((margin, int(round(max(0, top))), w - margin, int(round(min(h, bottom)))))
    return out


def _fill_holes(mask):
    """Linear-time hole filling: background patches not touching the border are holes."""
    from scipy import ndimage
    inv, _ = ndimage.label(~mask)
    border = np.unique(np.concatenate([inv[0], inv[-1], inv[:, 0], inv[:, -1]]))
    return mask | ((inv > 0) & ~np.isin(inv, border))


def detect_blobs(img_rgb, region=None, edge_threshold=20, min_size=(8, 8), max_size=(360, 120)):
    """Compact selectable-looking elements inside `region` (image coords)."""
    from scipy import ndimage
    l, t, r, b = region or (0, 0, img_rgb.shape[1], img_rgb.shape[0])
    lum = luminance(img_rgb[t:b, l:r])
    if lum.shape[0] < 8 or lum.shape[1] < 8:
        return []
    gx = np.abs(np.diff(lum, axis=1, prepend=lum[:, :1]))
    gy = np.abs(np.diff(lum, axis=0, prepend=lum[:1, :]))
    edges = np.maximum(gx, gy) > edge_threshold
    # merge letters into words and outlines into shapes, then fill shapes
    mask = ndimage.binary_dilation(edges, structure=np.ones((3, 5), dtype=bool))
    mask = _fill_holes(mask)
    labels, _ = ndimage.label(mask)
    out = []
    for sl in ndimage.find_objects(labels):
        if sl is None:
            continue
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        bw, bh = x1 - x0, y1 - y0
        if not (min_size[0] <= bw <= max_size[0] and min_size[1] <= bh <= max_size[1]):
            continue
        if bw > 12 * bh or bh > 12 * bw:      # lines and dividers aren't targets
            continue
        out.append((l + x0, t + y0, l + x1, t + y1))
    return _merge_lines(_drop_nested(out), max_w=max_size[0])


def _merge_lines(rects, text_h=24, max_w=360):
    """Join text-height boxes on one line (words -> label, icon + name -> row).

    Taller boxes (toolbar icons, knobs, panels) stay separate.
    """
    rects = sorted(rects, key=lambda r: (r[1], r[0]))
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            for i, m in enumerate(out):
                h1, h2 = r[3] - r[1], m[3] - m[1]
                if h1 > text_h or h2 > text_h:
                    continue
                overlap = min(r[3], m[3]) - max(r[1], m[1])
                gap = max(r[0], m[0]) - min(r[2], m[2])
                if overlap >= 0.6 * min(h1, h2) and gap <= max(8, 0.6 * min(h1, h2)) and \
                        max(r[2], m[2]) - min(r[0], m[0]) <= max_w:
                    out[i] = (min(r[0], m[0]), min(r[1], m[1]), max(r[2], m[2]), max(r[3], m[3]))
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


def _drop_nested(rects):
    """Keep the outer rect when one sits entirely inside another (button + its label)."""
    rects = sorted(rects, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]), reverse=True)
    keep = []
    for r in rects:
        if any(k[0] <= r[0] and k[1] <= r[1] and k[2] >= r[2] and k[3] >= r[3] for k in keep):
            continue
        keep.append(r)
    return keep
