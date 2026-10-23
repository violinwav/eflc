"""Controller pointer: free or magnetic.

The stick drives a *virtual* point. In magnetic mode the real cursor sits on
the centre of the target the virtual point is over (an editor grid cell, or a
taught plugin hotspot) and jumps to the next target once the virtual point
pushes past the edge plus a small detent margin; each jump is a haptic tick.
When the stick is released the virtual point recentres on the target, which is
what makes it feel like notches. Outside any target the pointer is free.

Pure logic: the cursor sink (`out`), haptics and the target provider are
injected, so this is unit-testable. Coordinates are physical screen pixels.
"""

import math
import time
from dataclasses import dataclass

from ..vision.grid import NOTE_NAMES

DETENT = 0.18          # fraction of a cell the virtual point must overshoot to switch
HOTSPOT_RADIUS = 38    # px: hotspots capture the pointer within this distance
VISIBLE_FOR = 2.5      # s after the last pad movement the overlay stays visible
SNAP_NONE = 3          # midi.Snap_None: FL places events exactly where you click


@dataclass
class PointerView:
    x: float
    y: float
    target: tuple = None       # (left, top, right, bottom) highlight, or None
    kind: str = "free"         # "free" | "cell" | "hotspot"
    magnetic: bool = True
    pressed: bool = False
    visible: bool = False
    label: str = ""            # note name / hotspot name next to the target
    scale: str = ""            # active scale lock, e.g. "C minor"
    pulse: int = 0             # increments on every snap (overlay plays a pulse)
    tool: str = "draw"         # FL editor tool the pad selected (overlay style)


class NoTargets:
    def update_focus(self, x, y):
        pass

    def grid(self):
        return None

    def hotspots(self):
        return []

    def elements(self):
        return []


def _is_wide(rect):
    l, t, r, b = rect[:4]
    return (r - l) > 3 * (b - t) and (r - l) > 60


class Pointer:
    DEFAULT_DETENT = DETENT

    def __init__(self, out, haptic=lambda kind: None, targets=None, clock=time.monotonic):
        self.out = out
        self.haptic = haptic
        self.targets = targets or NoTargets()
        self.clock = clock
        self.magnetic = True
        self.scale = None          # frozenset of pitch classes, or None
        self.scale_name = ""
        self.virtual = None        # [x, y]
        self.cursor = None         # last position we set
        self.target_key = None
        self.target_rect = None
        self.kind = "free"
        self.label = ""
        self.pressed = 0           # pad-held mouse buttons
        self.pulse = 0
        self.last_active = -1e9
        self._last_view = None
        self.snap_mode = None      # FL's snap setting (ui.getSnapMode) for click placement
        self.tool = "draw"
        self.detent = DETENT       # magnet strength (settings: "magnet_detent")

    # ------------------------------------------------------------ inputs
    def _sync(self):
        """Adopt the real cursor if the user moved the mouse themselves."""
        real = self.out.cursor_pos()
        if self.cursor is None or self.virtual is None or \
                abs(real[0] - self.cursor[0]) > 2 or abs(real[1] - self.cursor[1]) > 2:
            self.virtual = [float(real[0]), float(real[1])]
            self.cursor = (int(real[0]), int(real[1]))
            if self.target_key is not None:
                self.target_key = None
                self.target_rect = None
                self.kind = "free"
            return True
        return False

    def move(self, dt, vx, vy, speed):
        """Stick input (vx, vy in -1..1, y up) for dt seconds."""
        self._sync()
        self.last_active = self.clock()
        grid = self.targets.grid() if self.magnetic else None
        if grid is not None and self._in_grid(grid, self.virtual):
            # Cell-sized speed keeps targeting controllable at any zoom level.
            cw = (grid.xs[-1] - grid.xs[0]) / max(1, grid.cols)
            ch = (grid.ys[-1] - grid.ys[0]) / max(1, grid.rows)
            speed = min(speed, max(cw, ch) * 14 + 250)
        elif self.kind == "ui" and self.target_rect is not None:
            # Over small controls / menu rows: a touch slower, so each one is easy to land on.
            l, t, r, b = self.target_rect
            speed = min(speed, max(300.0, min(r - l, b - t) * 16 + 280))
        self.virtual[0] += vx * speed * dt
        self.virtual[1] -= vy * speed * dt
        self._resolve()

    def idle(self):
        """No stick input: recentre the virtual point on the current target."""
        if self._sync():
            return  # the real mouse moved: follow it, never snap it back

        if self.target_key is not None and self.target_rect is not None and not self.pressed:
            l, t, r, b = self.target_rect
            if self.kind == "ui" and _is_wide(self.target_rect):
                self.virtual[1] = (t + b) / 2.0       # rows: keep the horizontal position
            else:
                self.virtual = [(l + r) / 2.0, (t + b) / 2.0]

    def step(self, dx, dy):
        """D-pad: move exactly one cell (or to the nearest hotspot in that direction)."""
        self._sync()
        self.last_active = self.clock()
        grid = self.targets.grid() if self.magnetic else None
        if grid is not None and self._in_grid(grid, self.virtual):
            cell = grid.nearest_cell(self.virtual[0], self.virtual[1], self.scale)
            if cell is None:
                return
            nx, ny = grid.step(cell[0], cell[1], dx, -dy, self.scale)
            if (nx, ny) == cell:
                self.haptic("edge")
                return
            self.virtual = list(grid.center(nx, ny))
            self._resolve()
            return
        spots = self.targets.hotspots() if self.magnetic else []
        if not spots and self.magnetic:
            spots = [((e[0] + e[2]) / 2.0, (e[1] + e[3]) / 2.0, e[4] if len(e) > 4 else "")
                     for e in self._elements()]
        if spots:
            best = None
            cx, cy = self.virtual
            for (hx, hy, name) in spots:
                ddx, ddy = hx - cx, cy - hy  # screen y grows downward; dy is "up"
                along = ddx * dx + ddy * dy
                if along <= 4:
                    continue
                across = abs(ddx * dy - ddy * dx)
                score = along + across * 2.5
                if best is None or score < best[0]:
                    best = (score, hx, hy)
            if best is None:
                self.haptic("edge")
                return
            self.virtual = [float(best[1]), float(best[2])]
            self._resolve()
            return
        # free pointer: nudge a few pixels
        self.virtual[0] += dx * 4
        self.virtual[1] -= dy * 4
        self._resolve()

    def set_pressed(self, down):
        self.pressed = max(0, self.pressed + (1 if down else -1))
        self.last_active = self.clock()

    def toggle_magnetic(self):
        self.magnetic = not self.magnetic
        self.target_key = None
        self.target_rect = None
        self.kind = "free"
        return self.magnetic

    def set_scale(self, pcs, name):
        self.scale = frozenset(pcs) if pcs else None
        self.scale_name = name if pcs else ""

    def current_grid_target(self):
        """(grid, ix, iy) of the cell under the pointer, for editor tools."""
        if self.virtual is None:
            self._sync()
        grid = self.targets.grid()
        if grid is None:
            return None
        cell = grid.nearest_cell(self.virtual[0], self.virtual[1], self.scale if self.magnetic else None)
        return (grid,) + cell if cell else None

    # ------------------------------------------------------------ resolution
    @staticmethod
    def _in_grid(grid, p, margin=0):
        l, t, r, b = grid.rect
        return l - margin <= p[0] < r + margin and t - margin <= p[1] < b + margin

    def _resolve(self, quiet=False):
        key, rect, kind, label, pos = None, None, "free", "", None
        free_drag = self.pressed and self.kind in ("hotspot", "ui")
        if self.magnetic and not free_drag:
            grid = self.targets.grid()
            if grid is not None and self._in_grid(grid, self.virtual):
                cell = self._grid_cell(grid)
                if cell is not None:
                    ix, iy = cell
                    rect = grid.cell_rect(ix, iy)
                    pos = self.click_point(rect)
                    # keyed by screen position: re-analysis of an unchanged grid isn't a "snap"
                    key = ("cell", int(round(pos[0])), int(round(pos[1])))
                    kind = "cell"
                    if grid.pitch is not None:
                        label = NOTE_NAMES[grid.pitch[iy]]
            else:
                best = None
                for (hx, hy, name) in self.targets.hotspots():
                    d = math.hypot(hx - self.virtual[0], hy - self.virtual[1])
                    if d <= HOTSPOT_RADIUS and (best is None or d < best[0]):
                        best = (d, hx, hy, name)
                if best is not None:
                    _, hx, hy, name = best
                    key = ("spot", hx, hy)
                    rect = (hx - 14, hy - 14, hx + 14, hy + 14)
                    pos = (hx, hy)
                    kind = "hotspot"
                    label = name
                else:
                    el = self._element()
                    if el is not None:
                        l, t, r, b = el[:4]
                        rect = (l, t, r, b)
                        key = ("ui", int(l), int(t), int(r), int(b))
                        if _is_wide(rect):  # rows: snap vertically, glide horizontally
                            pos = (min(max(self.virtual[0], l + 8), r - 8), (t + b) / 2.0)
                        else:
                            pos = ((l + r) / 2.0, (t + b) / 2.0)
                        kind = "ui"
                        label = el[4] if len(el) > 4 else ""
        if pos is None:
            pos = tuple(self.virtual)
        if key != self.target_key:
            if key is not None and not quiet:
                self.haptic("tick")
                self.pulse += 1
            self.target_key = key
        self.target_rect = rect
        self.kind = kind
        self.label = label
        new = (int(round(pos[0])), int(round(pos[1])))
        if new != self.cursor:
            self.out.mouse_move_to(*new)
            self.cursor = new

    def _elements(self):
        fn = getattr(self.targets, "elements", None)
        return fn() if fn is not None else []

    def _element(self):
        """Selectable element under the virtual point (smallest wins), with a detent."""
        vx, vy = self.virtual
        els = self._elements()
        if self.target_key is not None and self.target_key[0] == "ui" and self.target_rect:
            l, t, r, b = self.target_rect
            m = max(4.0, 0.15 * min(r - l, b - t))
            if l - m <= vx < r + m and t - m <= vy < b + m:
                inner = [e for e in els if e[0] <= vx < e[2] and e[1] <= vy < e[3]
                         and tuple(e[:4]) != tuple(self.target_rect)]
                # stay unless the point is clearly inside a different element
                if not inner or (l <= vx < r and t <= vy < b):
                    return (l, t, r, b, self.label)
        best = None
        for e in els:
            l, t, r, b = e[:4]
            if l <= vx < r and t <= vy < b:
                area = (r - l) * (b - t)
                if best is None or area < best[0]:
                    best = (area, e)
        if best is not None:
            return best[1]
        near = None
        for e in els:  # a small magnetic halo around elements
            l, t, r, b = e[:4]
            dx = max(l - vx, 0, vx - r)
            dy = max(t - vy, 0, vy - b)
            d = math.hypot(dx, dy)
            if d <= 6 and (near is None or d < near[0]):
                near = (d, e)
        return near[1] if near else None

    def click_point(self, rect):
        """Where the cursor sits inside a grid cell so FL places events at the cell start.

        FL snaps a click to the *nearest* grid line (or not at all with snap
        off), so clicking the centre lands on the next line. Just inside the
        left line resolves to this cell's start for every snap mode, and a few
        pixels in keeps clear of the clip-trim handle on the clip's left edge.
        """
        l, t, r, b = rect
        if self.snap_mode == SNAP_NONE:
            x = math.ceil(l)
        else:
            x = l + max(2.0, min((r - l) * 0.25, 8.0))
        return (x, (t + b) / 2.0)

    def _grid_cell(self, grid):
        """Nearest allowed cell, with a detent so borders don't flicker."""
        vx, vy = self.virtual
        if self.target_key is not None and self.target_key[0] == "cell" and self.target_rect:
            l, t, r, b = self.target_rect
            mx, my = (r - l) * self.detent, (b - t) * self.detent
            if l - mx <= vx < r + mx and t - my <= vy < b + my:
                cur = grid.cell_at((l + r) / 2.0, (t + b) / 2.0)
                if cur is not None and grid.allowed_row(cur[1], self.scale):
                    return cur
        return grid.nearest_cell(vx, vy, self.scale)

    # ------------------------------------------------------------ view
    def view(self):
        if self.cursor is None:
            return None
        visible = self.pressed > 0 or (self.clock() - self.last_active) < VISIBLE_FOR
        return PointerView(
            float(self.cursor[0]), float(self.cursor[1]), self.target_rect, self.kind, self.magnetic,
            self.pressed > 0, visible, self.label,
            self.scale_name if self.kind == "cell" else "", self.pulse, self.tool)
