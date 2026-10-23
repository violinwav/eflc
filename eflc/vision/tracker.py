"""Background target tracking for the magnetic pointer.

The engine thread reports where the pointer is heading (cheap); this worker
works out what's under it and publishes snap targets:

  FL editor grid (Playlist / Piano roll) -> Grid cells            (vision)
  plugin windows                         -> taught hotspots
and, only with the experimental "ui_magnet" setting (off by default):
  FL popup menu (TQuickPopupMenuWindow)  -> menu item rows        (vision)
  other FL areas                         -> controls              (vision)
  anything else (other apps, dialogs)    -> UI Automation element (accessibility)

Captures take ~20 ms and UIA calls a few ms, so none of this runs on the engine
thread. Results are published by plain attribute assignment (atomic in CPython).
"""

import logging
import threading
import time

log = logging.getLogger("eflc.vision")

REFRESH = 0.35        # s between re-analyses while the pointer stays in the same place
MOVED_FAR = 180       # px: re-analyse sooner once the pointer leaves the analysed window
BLOB_REACH = (300, 210)
MENU_CLASS = "TQuickPopupMenuWindow"


class TargetTracker(threading.Thread):
    def __init__(self, hotspot_store=None, ui_targets=False):
        super().__init__(name="eflc-vision", daemon=True)
        self.hotspot_store = hotspot_store
        self.ui_targets = ui_targets   # experimental: magnet on menus / controls / other apps
        self._focus = None
        self._grid = None
        self._spots = []
        self._elements = []      # [(l, t, r, b, label)] in screen coords
        self._form_title = ""
        self._stop = threading.Event()
        self._last = (None, None, 0.0)       # grid: (hwnd, focus, time)
        self._blob_key = (None, None, 0.0)   # blobs/menus: (hwnd, centre, time)
        self._uia = None
        self.enabled = True
        self.last_error = None

    # ---------------------------------------------------- engine-thread API
    def update_focus(self, x, y):
        self._focus = None if x is None else (int(x), int(y))

    def grid(self):
        g, f = self._grid, self._focus
        if g is None or f is None:
            return None
        l, t, r, b = g.rect
        return g if (l - 30 <= f[0] < r + 30 and t - 30 <= f[1] < b + 30) else None

    def hotspots(self):
        return self._spots

    def elements(self):
        return self._elements

    @property
    def form_title(self):
        return self._form_title

    def invalidate(self):
        self._last = (None, None, 0.0)
        self._blob_key = (None, None, 0.0)

    def stop(self):
        self._stop.set()

    # ---------------------------------------------------- worker
    def run(self):
        from ..platform import fl_windows as flw
        from .grid import analyze
        if self.ui_targets:
            try:
                from ..platform.uia import Uia
                self._uia = Uia()
            except Exception as e:
                log.warning("UI Automation unavailable: %s", e)
        try:
            while not self._stop.is_set():
                focus = self._focus
                if focus is None or not self.enabled:
                    self._grid, self._spots, self._elements = None, [], []
                    self._stop.wait(0.1)
                    continue
                try:
                    pause = self._track(focus, flw, analyze)
                except Exception as e:
                    if repr(e) != self.last_error:
                        self.last_error = repr(e)
                        log.exception("target tracking failed")
                    self._grid, self._elements = None, []
                    pause = 0.2
                self._stop.wait(pause)
        finally:
            if self._uia is not None:
                self._uia.close()

    def _track(self, focus, flw, analyze):
        x, y = focus
        if self.ui_targets:
            root = flw.root_at(x, y)
            if root is not None and root.cls == MENU_CLASS:
                self._grid, self._spots = None, []
                self._menu(root, flw)
                return 0.04
        form = flw.form_at(x, y)
        if form is None:
            self._grid, self._spots, self._form_title = None, [], ""
            if self.ui_targets:
                self._uia_element(x, y)
            else:
                self._elements = []
            return 0.02 if self.ui_targets else 0.1
        self._form_title = form.title
        layout = flw.editor_layout(form)
        if layout is not None and layout.grid.contains(x, y):
            self._spots, self._elements = [], []
            self._editor(form, layout, focus, flw, analyze)
            return 0.05
        self._grid = None
        self._spots = self.hotspot_store.screen_spots(form) if self.hotspot_store else []
        if self.ui_targets:
            self._blobs(form, layout, focus, flw)
        else:
            self._elements = []
        return 0.05

    def _menu(self, root, flw):
        hwnd, _, t = self._blob_key
        if hwnd == root.hwnd and time.monotonic() - t < 0.4:
            return
        from .elements import menu_items
        img = flw.capture(root)
        if img is None:
            self._elements = []
            return
        ox, oy = root.rect.left, root.rect.top
        self._elements = [(ox + l, oy + t_, ox + r, oy + b, "") for l, t_, r, b in menu_items(img)]
        self._blob_key = (root.hwnd, None, time.monotonic())

    def _uia_element(self, x, y):
        if self._uia is None:
            self._elements = []
            return
        # keep the current element while the point is still inside it (no flicker)
        for e in self._elements:
            if e[0] <= x < e[2] and e[1] <= y < e[3] and time.monotonic() - self._blob_key[2] < 0.3:
                return
        el = self._uia.element_at(x, y)
        self._elements = [el] if el else []
        self._blob_key = (None, None, time.monotonic())

    def _blobs(self, form, layout, focus, flw):
        from .elements import detect_blobs
        x, y = focus
        hwnd, centre, t = self._blob_key
        if hwnd == form.hwnd and centre is not None and time.monotonic() - t < 0.5 and \
                abs(centre[0] - x) < BLOB_REACH[0] / 2 and abs(centre[1] - y) < BLOB_REACH[1] / 2:
            return
        img = flw.capture(form)
        if img is None:
            self._elements = []
            return
        ox, oy = form.rect.left, form.rect.top
        rx, ry = BLOB_REACH
        h, w = img.shape[:2]
        region = (max(0, x - ox - rx), max(0, y - oy - ry), min(w, x - ox + rx), min(h, y - oy + ry))
        rects = detect_blobs(img, region)
        avoid = flw.occluders(form)
        if layout is not None:
            avoid.append(layout.grid)          # editor grids have their own (exact) cells
        out = []
        for l, t_, r, b in rects:
            sl, st, sr, sb = ox + l, oy + t_, ox + r, oy + b
            if any(a.left < sr and sl < a.right and a.top < sb and st < a.bottom for a in avoid):
                continue
            out.append((sl, st, sr, sb, ""))
        self._elements = out
        self._blob_key = (form.hwnd, (x, y), time.monotonic())

    def _editor(self, form, layout, focus, flw, analyze):
        x, y = focus
        hwnd, last_focus, last_t = self._last
        now = time.monotonic()
        fresh = (hwnd == form.hwnd and self._grid is not None and now - last_t < REFRESH
                 and last_focus is not None
                 and abs(last_focus[0] - x) < MOVED_FAR and abs(last_focus[1] - y) < MOVED_FAR)
        if fresh:
            return
        img = flw.capture(form)
        if img is None:
            self._grid = None
            return
        ox, oy = form.rect.left, form.rect.top
        g = layout.grid.offset(-ox, -oy).as_tuple()
        kb = layout.keyboard.offset(-ox, -oy).as_tuple() if layout.keyboard else None
        hd = layout.headers.offset(-ox, -oy).as_tuple() if layout.headers else None
        avoid = [r.offset(-ox, -oy).as_tuple() for r in flw.occluders(form)]
        self._grid = analyze(img, layout.kind, g, kb, origin=(ox, oy), focus=(x - ox, y - oy), avoid=avoid,
                             headers_local=hd)
        self._last = (form.hwnd, focus, now)
