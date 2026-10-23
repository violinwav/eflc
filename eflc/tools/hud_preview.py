"""Render HUD states to PNG without a controller or FL (design check / docs).

    python -m eflc.tools.hud_preview [out_dir]
"""

import os
import sys

from PySide6.QtWidgets import QApplication

from .. import config
from ..core.engine import Engine
from ..hud.overlay import AllShortcuts, HintCard
from ..input.pad import PadState
from ..platform.win_output import parse_combo


class _NullOut:
    def __getattr__(self, name):
        return lambda *a, **k: (0, 0) if name == "cursor_pos" else None


class _Capture:
    def __init__(self):
        self.model = None

    def update(self, m):
        self.model = m

    def toast(self, t):
        pass

    def command(self, what, on):
        pass

    def pointer(self, view):
        pass

    def modal(self, view):
        pass


def render(out_dir, profiles_dir=None):
    profiles, errors = config.load_profiles(parse_combo, profiles_dir or config.BUNDLED_PROFILES)
    if errors:
        print("profile issues:", *errors, sep="\n  ")
    hud = _Capture()
    eng = Engine(profiles, "Default", _NullOut(), lambda *a: 1, hud)
    states = [
        ("card_pianoroll", {"focus": "pianoroll", "channelName": "Sytrus - Lead"}, ()),
        ("card_pianoroll_LT", {"focus": "pianoroll", "channelName": "Sytrus - Lead"}, ("LT",)),
        ("card_mixer_RT", {"focus": "mixer", "mixerTrackName": "Insert 3 - Drums"}, ("RT",)),
        ("card_plugin", {"focus": "plugin", "plugin": "Serum"}, ()),
    ]
    os.makedirs(out_dir, exist_ok=True)
    t = 0.0
    paths = []
    for name, st, held in states:
        for pressed in (frozenset(), frozenset(held)):
            t += 0.01
            eng.tick(t, PadState(True, pressed, {}), "fl64.exe", st, True)
        card = HintCard()
        card.set_model(hud.model)
        path = os.path.join(out_dir, name + ".png")
        card.grab().save(path)
        paths.append(path)
        t += 0.01
        eng.tick(t, PadState(True, frozenset(), {}), "fl64.exe", st, True)
    eng.tick(t + 0.01, PadState(True, frozenset(), {}), "fl64.exe", {"focus": "pianoroll"}, True)
    allw = AllShortcuts()
    allw.set_model(hud.model)
    allw._relayout()
    path = os.path.join(out_dir, "all_shortcuts.png")
    allw.grab().save(path)
    paths.append(path)
    return paths


def _settle(widget):
    """Jump every spring to its target so a static grab shows the resting state."""
    from ..hud.pointer_overlay import Spring
    for v in vars(widget).values():
        for s in (v if isinstance(v, list) else [v]):
            if isinstance(s, Spring):
                s.snap(s.target)


def render_modals(out_dir, profiles_dir=None):
    from ..core.modals import KeyboardModal, ListModal, MenuItem, WheelItem, WheelModal
    from ..hud.modal_overlay import ModalOverlay
    from ..hud.pointer_overlay import PointerOverlay
    from ..core.pointer import PointerView

    profiles, _ = config.load_profiles(parse_combo, profiles_dir or config.BUNDLED_PROFILES)
    hud = _Capture()
    eng = Engine(profiles, "Default", _NullOut(), lambda *a, **k: 1, hud)
    eng.tick(0.0, PadState(True, frozenset(), {}), "fl64.exe", {"focus": "pianoroll"}, True)
    views = {
        "modal_menu": eng._context_menu(),
        "modal_params": ListModal(eng, "Serum", [
            MenuItem("Osc A level", kind="param", value=0.72, detail="72 %"),
            MenuItem("Osc A pan", kind="param", value=0.5, detail="C"),
            MenuItem("Filter cutoff", kind="param", value=0.38, detail="1.24 kHz"),
            MenuItem("Filter reso", kind="param", value=0.15, detail="15 %"),
            MenuItem("Env 1 attack", kind="param", value=0.05, detail="2 ms"),
            MenuItem("Env 1 release", kind="param", value=0.44, detail="380 ms")],
            subtitle="generator · 2623 parameters",
            hint="↑↓ choose · ←→ adjust · RS fine · X refresh · B close"),
        "modal_wheel": WheelModal(eng, "Windows", [
            WheelItem("Playlist", "playlist"), WheelItem("Piano roll", "pianoroll", active=True),
            WheelItem("Channel rack", "channelrack"), WheelItem("Mixer", "mixer"),
            WheelItem("Browser", "browser"), WheelItem("Serum", "plugin"), WheelItem("Pro-Q 3", "plugin")]),
        "modal_keyboard": KeyboardModal(eng),
    }
    views["modal_params"].selected = 2
    views["modal_wheel"].selected = 5
    views["modal_wheel"].aim = 5 * 2 * 3.14159 / 7
    kb = views["modal_keyboard"]
    kb.text, kb.row, kb.col = "serum", 2, 3
    paths = []
    for name, modal in views.items():
        eng.modals = [modal]
        w = ModalOverlay()
        w.set_view(modal.view())
        _settle(w)
        w.alpha.snap(1.0)
        w.slide.snap(0.0)
        path = os.path.join(out_dir, name + ".png")
        w.grab().save(path)
        paths.append(path)

    for name, view in (
            ("pointer_cell", PointerView(400, 300, (370, 290, 400, 310), "cell", True, False, True, "D#", "C minor", 1)),
            ("pointer_pressed", PointerView(400, 300, (370, 290, 400, 310), "cell", True, True, True, "D#", "", 1)),
            ("pointer_hotspot", PointerView(400, 300, (386, 286, 414, 314), "hotspot", True, False, True, "Cutoff")),
            ("pointer_free", PointerView(400, 300, None, "free", True, False, True))):
        w = PointerOverlay()
        w.set_view(view)
        _settle(w)
        w.alpha.snap(1.0)
        w.pulses = []
        w._frame()
        path = os.path.join(out_dir, name + ".png")
        w.grab().save(path)
        paths.append(path)
    return paths


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(config.user_dir(), "hud_preview")
    for p in render(out) + render_modals(out):
        print(p)
    del app


if __name__ == "__main__":
    main()
