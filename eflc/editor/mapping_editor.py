"""Visual mapping editor: contexts, bindings, press-to-capture, typed action forms.

Edits the profile JSON files in %APPDATA%\\EFLC\\profiles directly; the engine
hot-reloads them within a second of saving. Axes, sequences and menus stay
editable through "Open JSON" (they're rarer and easier to read as text).
"""

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import config
from ..core.profile import CONTEXT_TITLES, TRIGGERS, context_title
from ..input.pad import BUTTONS, canonical

FL_OPS = ["show", "hide", "toggle", "nav", "transport", "play", "stop", "record", "undo", "redo", "jog",
          "hzoom", "vzoom", "snap", "snapToggle", "closePopup", "channelJog", "patternJog", "mixerJog",
          "preset", "showEditor", "focusSlot"]

# action type -> (label, [(key, kind, options/default)], build(values)->dict, parse(dict)->values)
ACTION_TYPES = [
    ("fl", "FL command", [("op", "combo", FL_OPS), ("args", "text", "")]),
    ("keys", "Keys", [("keys", "text", "ctrl+s"), ("hold", "check", False)]),
    ("mouse", "Mouse button", [("mouse", "combo", ["left", "right", "middle"]), ("hold", "check", True)]),
    ("text", "Type text", [("text", "text", "")]),
    ("wheel", "Window wheel", []),
    ("menu", "Menu", [("menu", "combo", ["context", "@params", "@presets", "@scale", "@slots", "@hotspots"])]),
    ("keyboard", "On-screen keyboard", []),
    ("pointer", "Pointer", [("pointer", "combo", ["toggle", "magnetic", "free", "step", "menu", "mainmenu",
                                                  "teach", "forget"]),
                            ("dx", "int", 0), ("dy", "int", 0)]),
    ("tool", "Editor tool", [("tool", "combo", ["slice", "mute", "delete", "select"])]),
    ("scale", "Scale lock toggle", []),
    ("profile", "Switch profile", [("profile", "combo", ["@next"]), ("mode", "combo", ["sticky", "revert"])]),
    ("midi_cc", "MIDI CC", [("cc", "int", 20), ("value", "int", 127), ("off", "int", 0), ("ch", "int", 1)]),
    ("midi_note", "MIDI note", [("note", "int", 36), ("vel", "int", 100), ("ch", "int", 1)]),
    ("pad", "Channel pad", [("channel", "text", "selected"), ("note", "int", 60), ("vel", "int", 100)]),
    ("hud", "HUD", [("hud", "combo", ["toggle", "all"])]),
    ("json", "Advanced (JSON)", [("json", "json", "{}")]),
]
TYPE_KEYS = [t[0] for t in ACTION_TYPES]


def action_type(do):
    if isinstance(do, list):
        return "json"
    for key in ("fl", "keys", "mouse", "text", "wheel", "menu", "pointer", "tool", "profile", "pad", "hud"):
        if key in do:
            return key
    if do.get("keyboard"):
        return "keyboard"
    if "scale" in do:
        return "scale"
    if do.get("midi") == "cc":
        return "midi_cc"
    if do.get("midi") == "note":
        return "midi_note"
    return "json"


def _parse_args(text):
    out = []
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        try:
            out.append(int(part))
        except ValueError:
            try:
                out.append(float(part))
            except ValueError:
                out.append(part)
    return out


def build_action(kind, v):
    if kind == "fl":
        d = {"fl": v["op"]}
        args = _parse_args(v["args"])
        if args:
            d["args"] = args
        return d
    if kind == "keys":
        d = {"keys": v["keys"]}
        if v["hold"]:
            d["hold"] = True
        return d
    if kind == "mouse":
        return {"mouse": v["mouse"], "hold": bool(v["hold"])}
    if kind == "text":
        return {"text": v["text"]}
    if kind == "wheel":
        return {"wheel": "windows"}
    if kind == "menu":
        return {"menu": v["menu"]}
    if kind == "keyboard":
        return {"keyboard": True}
    if kind == "pointer":
        d = {"pointer": v["pointer"]}
        if v["pointer"] == "step":
            d["dx"], d["dy"] = v["dx"], v["dy"]
        return d
    if kind == "tool":
        return {"tool": v["tool"]}
    if kind == "scale":
        return {"scale": "toggle"}
    if kind == "profile":
        return {"profile": v["profile"], "mode": v["mode"]}
    if kind == "midi_cc":
        return {"midi": "cc", "cc": v["cc"], "value": v["value"], "off": v["off"], "ch": v["ch"]}
    if kind == "midi_note":
        return {"midi": "note", "note": v["note"], "vel": v["vel"], "ch": v["ch"]}
    if kind == "pad":
        ch = v["channel"].strip()
        return {"pad": {"channel": int(ch) if ch.lstrip("-").isdigit() else "selected",
                        "note": v["note"], "vel": v["vel"]}}
    if kind == "hud":
        return {"hud": v["hud"]}
    return json.loads(v["json"])


def parse_action(kind, do):
    if kind == "fl":
        return {"op": do.get("fl"), "args": ", ".join(str(a) for a in do.get("args", []))}
    if kind == "keys":
        return {"keys": do.get("keys", ""), "hold": bool(do.get("hold"))}
    if kind == "mouse":
        return {"mouse": do.get("mouse", "left"), "hold": bool(do.get("hold"))}
    if kind == "text":
        return {"text": do.get("text", "")}
    if kind == "menu":
        return {"menu": do.get("menu", "context")}
    if kind == "pointer":
        return {"pointer": do.get("pointer"), "dx": do.get("dx", 0), "dy": do.get("dy", 0)}
    if kind == "tool":
        return {"tool": do.get("tool")}
    if kind == "profile":
        return {"profile": do.get("profile"), "mode": do.get("mode", "sticky")}
    if kind == "midi_cc":
        return {"cc": do.get("cc", 20), "value": do.get("value", 127), "off": do.get("off", 0) or 0,
                "ch": do.get("ch", 1)}
    if kind == "midi_note":
        return {"note": do.get("note", 36), "vel": do.get("vel", 100), "ch": do.get("ch", 1)}
    if kind == "pad":
        p = do.get("pad", {})
        return {"channel": str(p.get("channel", "selected")), "note": p.get("note", 60), "vel": p.get("vel", 100)}
    if kind == "hud":
        return {"hud": do.get("hud", "toggle")}
    return {"json": json.dumps(do, indent=2)}


def describe(do):
    kind = action_type(do)
    if kind == "fl":
        args = " ".join(str(a) for a in do.get("args", []))
        return ("FL: %s %s" % (do["fl"], args)).strip()
    if kind == "keys":
        return "Keys: %s%s" % (do["keys"], " (hold)" if do.get("hold") else "")
    if kind == "mouse":
        return "Mouse %s%s" % (do["mouse"], " drag" if do.get("hold") else " click")
    if kind in ("midi_cc", "midi_note"):
        return "MIDI %s" % (("CC %s" % do.get("cc")) if kind == "midi_cc" else ("note %s" % do.get("note")))
    label = dict((k, l) for k, l, _ in ACTION_TYPES).get(kind, "JSON")
    extra = do.get(kind) if isinstance(do, dict) and isinstance(do.get(kind), str) else ""
    return ("%s %s" % (label, extra)).strip()


class MappingEditor(QWidget):
    def __init__(self, runtime):
        super().__init__(None, Qt.Window)
        self.runtime = runtime
        self.setWindowTitle("EFLC mapping editor")
        self.resize(1180, 680)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.path = None
        self.data = None
        self.dirty = False
        self.capturing = False
        self._cap_main = None
        self._cap_mods = []
        self._cap_seen = set()
        self._build()
        self._load_profiles()

    # ------------------------------------------------------------ layout
    def _build(self):
        root = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Profile"))
        self.profile_box = QComboBox()
        self.profile_box.currentIndexChanged.connect(self._open_profile)
        lv.addWidget(self.profile_box)
        lv.addWidget(QLabel("Context"))
        self.contexts = QListWidget()
        self.contexts.currentItemChanged.connect(lambda *_: self._fill_table())
        lv.addWidget(self.contexts, 1)
        add_ctx = QPushButton("Add plugin / app context…")
        add_ctx.clicked.connect(self._add_context)
        lv.addWidget(add_ctx)
        split.addWidget(left)

        mid = QWidget()
        mv = QVBoxLayout(mid)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Input", "Trigger", "Action", "Label"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._select_row)
        mv.addWidget(self.table, 1)
        row = QHBoxLayout()
        new_btn = QPushButton("Add binding")
        new_btn.clicked.connect(self._new_binding)
        del_btn = QPushButton("Delete binding")
        del_btn.clicked.connect(self._delete_binding)
        json_btn = QPushButton("Open JSON")
        json_btn.clicked.connect(lambda: self.path and os.startfile(self.path))
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        row.addWidget(new_btn)
        row.addWidget(del_btn)
        row.addStretch(1)
        row.addWidget(json_btn)
        row.addWidget(self.save_btn)
        mv.addLayout(row)
        split.addWidget(mid)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(QLabel("<b>Edit binding</b>"))
        form = QFormLayout()
        cap_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("LT+X")
        self.capture_btn = QPushButton("Capture")
        self.capture_btn.setCheckable(True)
        self.capture_btn.toggled.connect(self._toggle_capture)
        cap_row.addWidget(self.input_edit, 1)
        cap_row.addWidget(self.capture_btn)
        form.addRow("Input", cap_row)
        self.capture_note = QLabel("")
        self.capture_note.setWordWrap(True)
        form.addRow("", self.capture_note)
        self.trigger_box = QComboBox()
        self.trigger_box.addItems(TRIGGERS)
        form.addRow("Fire on", self.trigger_box)
        self.ms_spin = QSpinBox()
        self.ms_spin.setRange(0, 3000)
        self.ms_spin.setSpecialValueText("profile default")
        self.ms_spin.setSuffix(" ms")
        form.addRow("Hold time", self.ms_spin)
        self.type_box = QComboBox()
        for key, label, _ in ACTION_TYPES:
            self.type_box.addItem(label, key)
        self.type_box.currentIndexChanged.connect(lambda *_: self._build_fields())
        form.addRow("Action", self.type_box)
        rv.addLayout(form)
        self.fields_host = QWidget()
        self.fields_form = QFormLayout(self.fields_host)
        rv.addWidget(self.fields_host)
        lf = QFormLayout()
        self.label_edit = QLineEdit()
        lf.addRow("HUD label", self.label_edit)
        rv.addLayout(lf)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply)
        rv.addWidget(apply_btn)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        rv.addWidget(self.status)
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([220, 560, 400])
        self.field_widgets = {}
        self._build_fields()

    # ------------------------------------------------------------ data
    def _load_profiles(self):
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        d = config.profiles_dir()
        for fn in config.profile_files(d):
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    name = json.load(f).get("name") or fn[:-5]
            except (OSError, ValueError):
                name = fn[:-5] + " (invalid JSON)"
            self.profile_box.addItem(name, os.path.join(d, fn))
        self.profile_box.blockSignals(False)
        if self.profile_box.count():
            active = getattr(self.runtime, "settings", {}).get("profile")
            index = max(0, self.profile_box.findText(active)) if active else 0
            self.profile_box.setCurrentIndex(index)
            self._open_profile(index)

    def _open_profile(self, index):
        if self.dirty and QMessageBox.question(self, "Unsaved changes", "Discard unsaved changes?") != QMessageBox.Yes:
            return
        path = self.profile_box.itemData(index)
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError) as e:
            self.status.setText("Can't read %s: %s" % (path, e))
            return
        self.path = path
        self.dirty = False
        self.contexts.clear()
        names = list(CONTEXT_TITLES)
        names += [c for c in (self.data.get("contexts") or {}) if c not in names]
        for c in names:
            it = QListWidgetItem(context_title(c) + ("" if c in CONTEXT_TITLES else "  (%s)" % c))
            it.setData(Qt.UserRole, c)
            n = len(((self.data.get("contexts") or {}).get(c) or {}).get("bindings") or [])
            if n:
                it.setText(it.text() + "   · %d" % n)
            self.contexts.addItem(it)
        self.contexts.setCurrentRow(0)
        ext = self.data.get("extends")
        self.status.setText("Extends %s: bindings here override it per button and trigger." % ext if ext else "")

    def type_fields(self, key):
        return next((f for k, _, f in ACTION_TYPES if k == key), None)

    def _ctx(self):
        it = self.contexts.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _bindings(self, create=False):
        c = self._ctx()
        if c is None or self.data is None:
            return []
        ctxs = self.data.setdefault("contexts", {}) if create else (self.data.get("contexts") or {})
        spec = ctxs.setdefault(c, {}) if create else (ctxs.get(c) or {})
        return spec.setdefault("bindings", []) if create else (spec.get("bindings") or [])

    def _fill_table(self):
        rows = self._bindings()
        self.table.setRowCount(len(rows))
        for i, b in enumerate(rows):
            vals = [b.get("input", ""), b.get("on", "press"), describe(b.get("do", {})), b.get("label", "")]
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(str(v)))
        if rows:
            self.table.selectRow(0)
        else:
            self._load_into_form({"input": "", "on": "press", "do": {"keys": ""}, "label": ""})

    def _select_row(self):
        r = self.table.currentRow()
        rows = self._bindings()
        if 0 <= r < len(rows):
            self._load_into_form(rows[r])

    def _load_into_form(self, b):
        self.input_edit.setText(b.get("input", ""))
        self.trigger_box.setCurrentText(b.get("on", "press"))
        self.ms_spin.setValue(int(b.get("ms") or 0))
        do = b.get("do", {})
        kind = action_type(do)
        self.type_box.blockSignals(True)
        self.type_box.setCurrentIndex(TYPE_KEYS.index(kind))
        self.type_box.blockSignals(False)
        self._build_fields(parse_action(kind, do))
        self.label_edit.setText(b.get("label", ""))
        self.capture_note.setText("")

    # ------------------------------------------------------------ action fields
    def _build_fields(self, values=None):
        while self.fields_form.rowCount():
            self.fields_form.removeRow(0)
        self.field_widgets = {}
        key = self.type_box.currentData()
        fields = self.type_fields(key) or []
        values = values or {}
        for name, kind, opt in fields:
            if kind == "combo":
                w = QComboBox()
                w.setEditable(True)
                options = list(opt)
                if key == "profile" and name == "profile":
                    options += [self.profile_box.itemText(i) for i in range(self.profile_box.count())]
                w.addItems(options)
                if name in values and values[name] is not None:
                    w.setCurrentText(str(values[name]))
            elif kind == "check":
                w = QCheckBox()
                w.setChecked(bool(values.get(name, opt)))
            elif kind == "int":
                w = QSpinBox()
                w.setRange(-128, 999)
                w.setValue(int(values.get(name, opt)))
            elif kind == "json":
                w = QPlainTextEdit()
                w.setPlainText(values.get(name, opt))
                w.setMinimumHeight(140)
            else:
                w = QLineEdit()
                w.setText(str(values.get(name, opt)))
            self.field_widgets[name] = (kind, w)
            self.fields_form.addRow(name.capitalize(), w)

    def _field_values(self):
        out = {}
        for name, (kind, w) in self.field_widgets.items():
            if kind == "combo":
                out[name] = w.currentText()
            elif kind == "check":
                out[name] = w.isChecked()
            elif kind == "int":
                out[name] = w.value()
            elif kind == "json":
                out[name] = w.toPlainText()
            else:
                out[name] = w.text()
        return out

    # ------------------------------------------------------------ editing
    def _form_binding(self):
        text = self.input_edit.text().strip()
        parts = [canonical(p) for p in text.split("+") if p.strip()]
        if not parts or parts[-1] not in BUTTONS:
            raise ValueError("Enter or capture an input, e.g. LT+X")
        mods = parts[:-1]
        declared = {canonical(m) for m in (self.data.get("modifiers") or {})}
        missing = [m for m in mods if m not in declared]
        if missing:
            raise ValueError("%s isn't a modifier in this profile. Add it under \"modifiers\" "
                             "(Open JSON) or use a declared one." % ", ".join(missing))
        b = {"input": "+".join(parts), "on": self.trigger_box.currentText(),
             "do": build_action(self.type_box.currentData(), self._field_values())}
        if self.ms_spin.value() and b["on"] == "hold":
            b["ms"] = self.ms_spin.value()
        if self.label_edit.text().strip():
            b["label"] = self.label_edit.text().strip()
        return b

    def _apply(self):
        try:
            b = self._form_binding()
        except (ValueError, json.JSONDecodeError) as e:
            self.status.setText(str(e))
            return
        rows = self._bindings(create=True)
        r = self.table.currentRow()
        clash = [i for i, x in enumerate(rows) if i != r and canonical_input(x.get("input", "")) ==
                 canonical_input(b["input"]) and x.get("on", "press") == b["on"]]
        if clash:
            self.status.setText("Another binding already uses %s on %s in this context." % (b["input"], b["on"]))
            return
        if 0 <= r < len(rows):
            rows[r] = b
        else:
            rows.append(b)
            r = len(rows) - 1
        self.dirty = True
        self._fill_table()
        self.table.selectRow(r)
        self.status.setText("Applied. Save to make it live.")

    def _new_binding(self):
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self._load_into_form({"input": "", "on": "press", "do": {"keys": ""}, "label": ""})
        self.capture_btn.setChecked(True)

    def _delete_binding(self):
        r = self.table.currentRow()
        rows = self._bindings()
        if 0 <= r < len(rows):
            del rows[r]
            self.dirty = True
            self._fill_table()

    def _add_context(self):
        text, ok = QInputDialog.getText(self, "Add context",
                                        "plugin:<plugin name> (e.g. plugin:serum) or app:<exe> (e.g. app:chrome.exe)")
        if not ok or not text.strip():
            return
        c = text.strip().lower()
        if not (c.startswith("plugin:") or c.startswith("app:")):
            self.status.setText("Contexts start with plugin: or app:")
            return
        self.data.setdefault("contexts", {}).setdefault(c, {"bindings": []})
        it = QListWidgetItem("%s  (%s)" % (context_title(c), c))
        it.setData(Qt.UserRole, c)
        self.contexts.addItem(it)
        self.contexts.setCurrentItem(it)
        self.dirty = True

    def _save(self):
        if self.path is None:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.dirty = False
        self.status.setText("Saved. EFLC reloads it within a second.")

    # ------------------------------------------------------------ capture
    def _toggle_capture(self, on):
        self.capturing = on
        self._cap_main, self._cap_mods, self._cap_seen = None, [], set()
        self.runtime.request("capture", on)
        self.capture_btn.setText("Listening…" if on else "Capture")
        self.capture_note.setText("Hold modifiers, press the button, then let go." if on else "")

    def on_captured(self, pressed):
        if not self.capturing:
            return
        now = set(pressed)
        new = now - self._cap_seen
        if new:
            main = sorted(new)[-1]
            self._cap_mods = sorted((now - {main}))
            self._cap_main = main
            self._cap_seen |= new
        if not now and self._cap_main:
            self.input_edit.setText("+".join(self._cap_mods + [self._cap_main]))
            self.capture_btn.setChecked(False)

    def closeEvent(self, e):
        if self.capturing:
            self.runtime.request("capture", False)
        if self.dirty and QMessageBox.question(self, "Unsaved changes", "Save before closing?") == QMessageBox.Yes:
            self._save()
        super().closeEvent(e)


def canonical_input(text):
    parts = [canonical(p) for p in str(text).split("+") if p.strip()]
    return (tuple(sorted(parts[:-1])), parts[-1] if parts else "")
