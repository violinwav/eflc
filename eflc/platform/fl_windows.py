"""FL Studio window introspection via Win32: forms, editor layout, capture.

FL's windows are Delphi forms (class names ending in "Form") parented to the
main window "TFruityLoopsMainForm"; detached plugin windows are top-level
windows of the same process. Editors (Playlist, Piano roll) are both
TEventEditForm; their child controls outline the grid:

    TWPControl  (corner panel)   -> grid top    = corner.bottom
    TQuickScroller (h-scroller)  -> grid left   = scroller.left, keyboard right edge
    TNumBar (right zoom bar)     -> grid right  = numbar.left, grid bottom = numbar.bottom

All coordinates are physical screen pixels (the process is per-monitor DPI aware).
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumProc, wintypes.LPARAM]
user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

GA_PARENT = 1
PW_RENDERFULLCONTENT = 2
MAIN_CLASS = "TFruityLoopsMainForm"
# Inner wrapper forms (e.g. the plugin UI host inside a TPluginForm): never "the" form.
WRAPPER_CLASSES = {"TWPControlForm"}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


@dataclass
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def contains(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom

    def offset(self, dx, dy):
        return Rect(self.left + dx, self.top + dy, self.right + dx, self.bottom + dy)

    def as_tuple(self):
        return (self.left, self.top, self.right, self.bottom)


@dataclass
class Control:
    hwnd: int
    cls: str
    title: str
    rect: Rect
    visible: bool


@dataclass
class Form:
    hwnd: int
    cls: str
    title: str
    rect: Rect
    children: list = field(default_factory=list)   # direct child Controls

    @property
    def kind(self):
        t = self.title.lower()
        if self.cls == "TEventEditForm":
            if t.startswith("piano roll"):
                return "pianoroll"
            if t.startswith("playlist"):
                return "playlist"
            return "editor"
        return {"TFXForm": "mixer", "TSampleListForm": "browser",
                "TStepSeqForm": "channelrack"}.get(self.cls, "plugin" if "plugin" in self.cls.lower() else "other")

    def child(self, cls, pick=None):
        found = [c for c in self.children if c.cls == cls and c.visible and c.rect.width > 0 and c.rect.height > 0]
        if not found:
            return None
        return pick(found) if pick else found[0]


def _text(fn, h):
    buf = ctypes.create_unicode_buffer(256)
    fn(h, buf, 256)
    return buf.value


def _rect(h):
    r = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return Rect(r.left, r.top, r.right, r.bottom)


def control(h):
    return Control(int(h), _text(user32.GetClassNameW, h), _text(user32.GetWindowTextW, h),
                   _rect(h), bool(user32.IsWindowVisible(h)))


def find_main():
    return user32.FindWindowW(MAIN_CLASS, None)


def _children(parent):
    out = []
    user32.EnumChildWindows(parent, EnumProc(lambda h, _: out.append(h) or True), 0)
    return out


def _form(h, all_children=None):
    c = control(h)
    kids = []
    for k in (all_children if all_children is not None else _children(h)):
        if user32.GetParent(k) == h:
            kids.append(control(k))
    return Form(c.hwnd, c.cls, c.title, c.rect, kids)


def fl_pid():
    main = find_main()
    if not main:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(main, ctypes.byref(pid))
    return pid.value


def forms(visible_only=True):
    """All FL forms: docked ones under the main window plus detached top-level ones."""
    main = find_main()
    if not main:
        return []
    pid = fl_pid()
    handles = [h for h in _children(main)]
    tops = []

    def on_top(h, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and h != main:
            tops.append(h)
        return True

    user32.EnumWindows(EnumProc(on_top), 0)
    out = []
    for h in handles + tops:
        cls = _text(user32.GetClassNameW, h)
        if not cls.endswith("Form") or cls == MAIN_CLASS or cls in WRAPPER_CLASSES:
            continue
        if visible_only and not user32.IsWindowVisible(h):
            continue
        f = _form(h)
        if f.rect.width > 0 and f.rect.height > 0:
            out.append(f)
    return out


def form_at(x, y):
    """The FL form under a screen point, or None."""
    h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    pid = fl_pid()
    while h:
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value != pid:
            return None
        cls = _text(user32.GetClassNameW, h)
        if cls == MAIN_CLASS:
            return None
        if cls.endswith("Form") and cls not in WRAPPER_CLASSES:
            return _form(h)
        h = user32.GetAncestor(h, GA_PARENT)
    return None


GW_HWNDPREV = 3
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]


def occluders(form):
    """Rects of visible sibling FL forms stacked above `form` (they cover parts of it)."""
    out = []
    h = user32.GetWindow(form.hwnd, GW_HWNDPREV)
    guard = 0
    while h and guard < 200:
        guard += 1
        cls = _text(user32.GetClassNameW, h)
        if user32.IsWindowVisible(h) and cls.endswith("Form") and cls not in WRAPPER_CLASSES:
            r = _rect(h)
            if r.width > 0 and r.height > 0:
                out.append(r)
        h = user32.GetWindow(h, GW_HWNDPREV)
    return out


GA_ROOT = 2


def root_at(x, y):
    """Top-level window under a screen point (any process) as a Control, or None."""
    h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not h:
        return None
    root = user32.GetAncestor(h, GA_ROOT)
    return control(root) if root else None


def main_menu_rect():
    main = find_main()
    if not main:
        return None
    for h in _children(main):
        if _text(user32.GetClassNameW, h) == "TNewMenu" and user32.IsWindowVisible(h):
            return _rect(h)
    return None


def caption_rect(form):
    c = form.child("TNewCaption")
    return c.rect if c else None


@dataclass
class EditorLayout:
    kind: str
    grid: Rect           # note/clip area, screen coords
    keyboard: Rect = None  # piano roll keyboard strip
    headers: Rect = None   # playlist track-header column (never covered by clips)


def editor_layout(form):
    """Grid (and keyboard) rectangles of a Playlist / Piano roll form, or None."""
    if form.kind not in ("pianoroll", "playlist"):
        return None
    scroller = form.child("TQuickScroller", pick=lambda cs: min(cs, key=lambda c: c.rect.top))
    if scroller is None:
        return None
    corners = [c for c in form.children if c.cls == "TWPControl" and c.visible
               and abs(c.rect.top - scroller.rect.top) <= 4 and c.rect.height > 0]
    numbars = [c for c in form.children if c.cls == "TNumBar" and c.visible
               and c.rect.left >= scroller.rect.right - 8 and c.rect.height > 0]
    if not corners or not numbars:
        return None
    corner = corners[0]
    numbar = min(numbars, key=lambda c: c.rect.top)
    grid = Rect(scroller.rect.left, corner.rect.bottom, numbar.rect.left, numbar.rect.bottom)
    if grid.width < 40 or grid.height < 40:
        return None
    keyboard = headers = None
    if form.kind == "pianoroll":
        keyboard = Rect(corner.rect.left, grid.top, scroller.rect.left, grid.bottom)
    elif scroller.rect.left - corner.rect.left >= 40:
        headers = Rect(corner.rect.left, grid.top, scroller.rect.left, grid.bottom)
    return EditorLayout(form.kind, grid, keyboard, headers)


def capture(form):
    """PrintWindow capture of a form -> numpy uint8 array (h, w, 3) RGB, or None."""
    import numpy as np
    w, h = form.rect.width, form.rect.height
    if w <= 0 or h <= 0 or not user32.IsWindow(form.hwnd):
        return None
    sdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(form.hwnd, mdc, PW_RENDERFULLCONTENT):
            return None
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = np.empty((h, w, 4), dtype=np.uint8)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf.ctypes.data, ctypes.byref(bi), 0)
        return buf[:, :, 2::-1]  # BGRA -> RGB view
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, sdc)
