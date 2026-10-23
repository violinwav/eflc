# name=EFLC Bridge
# url=https://github.com/
# supportedDevices=EFLC
"""EFLC bridge: runs inside FL Studio's MIDI scripting runtime.

Talks to the EFLC companion app over the loopMIDI port "EFLC" using SysEx
(FL's script sandbox blocks sockets and file access):

    F0 7D 45 46 <dir> <ASCII JSON> F7     dir 01 = companion -> FL, 02 = FL -> companion
c
The companion polls ~12x/s; each poll is answered with the FL state when it
changed (or at least once a second as a keep-alive). Commands are executed from
a fixed whitelist through the official scripting API and answered with a
result. Everything runs inside FL's MIDI callbacks on FL's main thread.

Setup in FL (Options > MIDI settings): input "EFLC" with controller type
"EFLC Bridge", and output "EFLC" set to the same Port number as the input, so
device.midiOutSysex() can reach the companion.
"""

import json
import time
import traceback

import midi
import ui
import transport
import general
import channels
import patterns
import mixer
import device

try:
    import plugins
except Exception:
    plugins = None

PROTO = 2
HEADER = bytes([0xF0, 0x7D, 0x45, 0x46])
TO_FL = 0x01
FROM_FL = 0x02
KEEPALIVE = 1.0   # resend state at least this often (s) while polled

# Focus probe order: most specific first.
FOCUS_ORDER = [
    (midi.widPluginGenerator, "plugin"),
    (midi.widPluginEffect, "plugin"),
    (midi.widPlugin, "plugin"),
    (midi.widPianoRoll, "pianoroll"),
    (midi.widPlaylist, "playlist"),
    (midi.widChannelRack, "channelrack"),
    (midi.widMixer, "mixer"),
    (midi.widBrowser, "browser"),
]

WINDOWS = {
    "mixer": midi.widMixer,
    "channelrack": midi.widChannelRack,
    "playlist": midi.widPlaylist,
    "pianoroll": midi.widPianoRoll,
    "browser": midi.widBrowser,
}

NAV = ("up", "down", "left", "right", "enter", "escape", "next", "previous",
       "cut", "copy", "paste", "delete", "insert", "nextWindow")

_log_lines = 0


def _log(msg):
    """Diagnostics go to FL's Script output window (View > Script output)."""
    global _log_lines
    _log_lines += 1
    if _log_lines <= 200:
        print("EFLC: " + msg)


def _safe(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:
        return default


# ---------------------------------------------------------------- state

def read_state():
    focus = None
    focus_wid = None
    for wid, name in FOCUS_ORDER:
        if _safe(ui.getFocused, wid, default=False):
            focus, focus_wid = name, wid
            break

    st = {
        "focus": focus,
        "caption": _safe(ui.getFocusedFormCaption, default=""),
        "plugin": None,
        "playing": bool(_safe(transport.isPlaying, default=False)),
        "recording": bool(_safe(transport.isRecording, default=False)),
        "snap": _safe(ui.getSnapMode, default=None),
        "popup": bool(_safe(ui.isInPopupMenu, default=False)),
        "visible": {k: bool(_safe(ui.getVisible, w, default=False)) for k, w in WINDOWS.items()},
    }
    if focus == "plugin":
        st["plugin"] = _safe(ui.getFocusedPluginName, default="") or None
        st["pluginKind"] = "effect" if focus_wid == midi.widPluginEffect else "generator"

    ch = _safe(channels.selectedChannel, default=-1)
    st["channel"] = ch
    st["channelName"] = _safe(channels.getChannelName, ch, default="") if ch is not None and ch >= 0 else ""
    pat = _safe(patterns.patternNumber, default=-1)
    st["pattern"] = pat
    st["patternName"] = _safe(patterns.getPatternName, pat, default="") if pat is not None and pat >= 0 else ""
    trk = _safe(mixer.trackNumber, default=-1)
    st["mixerTrack"] = trk
    st["mixerTrackName"] = _safe(mixer.getTrackName, trk, default="") if trk is not None and trk >= 0 else ""
    return st


# ---------------------------------------------------------------- commands

def _wid(name):
    if name not in WINDOWS:
        raise ValueError("unknown window %r" % (name,))
    return WINDOWS[name]


def _valid(index, slot, use_global):
    try:
        return bool(plugins.isValid(index, slot, use_global))
    except TypeError:
        return bool(plugins.isValid(index, slot))


def _plugin_target():
    """(index, slot, use_global, kind) of the focused plugin window.

    Effects: getFocusedFormID() packs (track << 22) + (slot << 16).
    Generators: it is the global channel index. Falls back to the selected
    channel's plugin. Returns None if nothing usable is found.
    """
    if plugins is None:
        return None
    try:
        form = ui.getFocusedFormID()
        if ui.getFocused(midi.widPluginEffect):
            track, slot = form >> 22, (form >> 16) & 0x3F
            if _valid(track, slot, False):
                return (track, slot, False, "effect")
        if ui.getFocused(midi.widPluginGenerator) or ui.getFocused(midi.widPlugin):
            if _valid(form, -1, True):
                return (form, -1, True, "generator")
    except Exception:
        pass
    ch = _safe(channels.selectedChannel, default=-1)
    if ch is not None and ch >= 0 and _valid(ch, -1, False):
        return (ch, -1, False, "generator")
    return None


def _need_target():
    t = _plugin_target()
    if t is None:
        raise RuntimeError("no plugin focused")
    return t


def _call(fn, *args, use_global=False):
    """Plugin API calls take a trailing useGlobalIndex on newer API versions."""
    if use_global:
        return fn(*args, use_global)
    try:
        return fn(*args)
    except TypeError:
        return fn(*args, False)


def op_show(name):
    ui.showWindow(_wid(name))
    ui.setFocused(_wid(name))


def op_toggle(name):
    w = _wid(name)
    if ui.getVisible(w) and ui.getFocused(w):
        ui.hideWindow(w)
    else:
        ui.showWindow(w)
        ui.setFocused(w)


def op_nav(name, count=1):
    if name not in NAV:
        raise ValueError("unknown nav %r" % (name,))
    fn = getattr(ui, name)
    for _ in range(max(1, int(count))):
        fn()


def op_transport(name, value=1):
    code = getattr(midi, "FPT_" + str(name), None)
    if code is None:
        raise ValueError("unknown FPT command %r" % (name,))
    return transport.globalTransport(code, int(value))


def op_channel_jog(delta):
    n = channels.channelCount()
    if n <= 0:
        return
    cur = max(0, channels.selectedChannel())
    channels.selectOneChannel((cur + int(delta)) % n)


def op_pattern_jog(delta):
    cur = patterns.patternNumber()
    target = max(1, cur + int(delta))
    if target <= patterns.patternMax():
        patterns.jumpToPattern(target)


def op_mixer_jog(delta):
    n = mixer.trackCount()
    cur = mixer.trackNumber()
    mixer.setTrackNumber(max(0, min(n - 1, cur + int(delta))))


def op_preset(delta):
    index, slot, use_global, _ = _need_target()
    fn = plugins.nextPreset if int(delta) >= 0 else plugins.prevPreset
    for _ in range(max(1, abs(int(delta)))):
        _call(fn, index, slot, use_global=use_global)


def op_plugin_info():
    t = _plugin_target()
    if t is None:
        return {"valid": False}
    index, slot, use_global, kind = t
    return {
        "valid": True, "kind": kind, "index": index, "slot": slot, "global": use_global,
        "name": _safe(lambda: _call(plugins.getPluginName, index, slot, False, use_global=use_global), default=""),
        "params": _safe(lambda: _call(plugins.getParamCount, index, slot, use_global=use_global), default=0),
        "presets": _safe(lambda: _call(plugins.getPresetCount, index, slot, use_global=use_global), default=0),
        "formId": _safe(ui.getFocusedFormID, default=None),
    }


def _param_text(i, index, slot, use_global):
    fn = getattr(plugins, "getParamValueString", None)
    if fn is None:
        return ""
    try:
        return fn(i, index, slot, midi.PIM_None, use_global) if use_global else fn(i, index, slot)
    except Exception:
        return ""


def op_params(start=0, count=32):
    """Named parameters from `start`: {"total", "next", "items": [[i, name, value, text]]}."""
    index, slot, use_global, _ = _need_target()
    total = _call(plugins.getParamCount, index, slot, use_global=use_global)
    items, i, scanned = [], int(start), 0
    while i < total and len(items) < int(count) and scanned < 600:
        name = _safe(lambda: _call(plugins.getParamName, i, index, slot, use_global=use_global), default="")
        if name and name.strip() and not name.startswith("MIDI CC"):
            value = _safe(lambda: _call(plugins.getParamValue, i, index, slot, use_global=use_global), default=0.0)
            items.append([i, name.strip(), round(float(value), 4), _param_text(i, index, slot, use_global)])
        i += 1
        scanned += 1
    return {"total": total, "next": i if i < total else None, "items": items}


def op_set_param(i, value):
    index, slot, use_global, _ = _need_target()
    value = max(0.0, min(1.0, float(value)))
    try:
        plugins.setParamValue(value, int(i), index, slot, midi.PIM_None, use_global)
    except TypeError:
        plugins.setParamValue(value, int(i), index, slot)
    now = _safe(lambda: _call(plugins.getParamValue, int(i), index, slot, use_global=use_global), default=value)
    return [int(i), round(float(now), 4), _param_text(int(i), index, slot, use_global)]


def op_presets(limit=256):
    index, slot, use_global, _ = _need_target()
    n = _call(plugins.getPresetCount, index, slot, use_global=use_global)
    names = []
    for i in range(min(int(n), int(limit))):
        names.append(_safe(lambda: _call(plugins.getName, index, slot, midi.FPN_Preset, i,
                                          use_global=use_global), default="Preset %d" % (i + 1)))
    return names


def _channel(ch):
    ch = int(ch)
    return channels.selectedChannel() if ch < 0 else ch


def op_channel_note(ch, note, velocity):
    channels.midiNoteOn(_channel(ch), int(note), int(velocity))


def op_show_editor(ch=-1):
    """Open + focus a channel's plugin window (Sampler channels aren't plugins: no-op there)."""
    index = _channel(ch)
    channels.showEditor(index, 1)
    focus = getattr(channels, "focusEditor", None)
    if focus is not None:
        _safe(focus, index)


def op_mixer_slots():
    track = mixer.trackNumber()
    out = []
    for slot in range(10):
        if _valid(track, slot, False):
            out.append([slot, _safe(lambda: plugins.getPluginName(track, slot), default="Slot %d" % (slot + 1))])
    return {"track": track, "name": _safe(mixer.getTrackName, track, default=""), "slots": out}


def op_focus_slot(slot):
    track = mixer.trackNumber()
    fn = getattr(mixer, "focusEditor", None)
    if fn is None:
        raise RuntimeError("mixer.focusEditor not available in this FL version")
    fn(track, int(slot))


def op_api_info():
    mods = {"ui": ui, "plugins": plugins, "channels": channels, "mixer": mixer,
            "general": general, "transport": transport, "device": device, "patterns": patterns}
    return {k: sorted(n for n in dir(m) if not n.startswith("_")) for k, m in mods.items() if m is not None}


OPS = {
    "show": op_show,
    "hide": lambda name: ui.hideWindow(_wid(name)),
    "toggle": op_toggle,
    "nav": op_nav,
    "transport": op_transport,
    "play": lambda: transport.start(),
    "stop": lambda: transport.stop(),
    "record": lambda: transport.record(),
    "undo": lambda: general.undoUp(),
    "redo": lambda: general.undoDown(),
    "jog": lambda v: ui.jog(int(v)),
    "hzoom": lambda v: ui.horZoom(int(v)),
    "vzoom": lambda v: ui.verZoom(int(v)),
    "snap": lambda v: ui.snapMode(int(v)),
    "snapToggle": lambda: ui.snapOnOff(),
    "closePopup": lambda: ui.closeActivePopupMenu(),
    "channelJog": op_channel_jog,
    "patternJog": op_pattern_jog,
    "mixerJog": op_mixer_jog,
    "preset": op_preset,
    "pluginInfo": op_plugin_info,
    "params": op_params,
    "setParam": op_set_param,
    "presets": op_presets,
    "channelNote": op_channel_note,
    "showEditor": op_show_editor,
    "mixerSlots": op_mixer_slots,
    "focusSlot": op_focus_slot,
    "apiInfo": op_api_info,
    "state": lambda: read_state(),
}


# ---------------------------------------------------------------- link

def encode(msg):
    payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return HEADER + bytes([FROM_FL]) + payload + b"\xf7"


def decode(data):
    """SysEx bytes -> (direction, msg) or None if it isn't an EFLC message."""
    data = bytes(data)
    if len(data) < 7 or not data.startswith(HEADER) or data[-1] != 0xF7:
        return None
    try:
        return data[4], json.loads(data[5:-1].decode("ascii"))
    except Exception:
        return None


class Link:
    def __init__(self):
        self.hello_sent = False
        self.last_state = None
        self.last_state_t = 0.0
        self.warned_unassigned = False

    def send(self, msg):
        if not device.isAssigned():
            if not self.warned_unassigned:
                self.warned_unassigned = True
                _log("output not linked: in MIDI settings set the EFLC *output* Port "
                     "to the same number as the EFLC input Port")
            return
        device.midiOutSysex(encode(msg))

    def push_state(self, force=False):
        st = read_state()
        now = time.monotonic()
        if force or st != self.last_state or now - self.last_state_t >= KEEPALIVE:
            self.last_state = st
            self.last_state_t = now
            self.send({"t": "state", "s": st})

    def handle(self, msg):
        t = msg.get("t")
        if t == "poll":
            if not self.hello_sent or msg.get("hello"):
                if not self.hello_sent:
                    _log("companion found, link up")
                self.hello_sent = True
                self.send({"t": "hello", "proto": PROTO, "api": _safe(general.getVersion, default=0)})
                self.push_state(force=True)
            else:
                self.push_state()
        elif t == "cmd":
            self.run(msg)

    def run(self, msg):
        cid = msg.get("id")
        op = msg.get("op")
        args = msg.get("args") or []
        fn = OPS.get(op)
        if fn is None:
            self.send({"t": "result", "id": cid, "ok": False, "err": "unknown op %r" % (op,)})
            return
        try:
            value = fn(*args)
            self.send({"t": "result", "id": cid, "ok": True, "value": value if _jsonable(value) else None})
        except Exception as e:
            self.send({"t": "result", "id": cid, "ok": False, "err": "%s: %s" % (type(e).__name__, e)})
        self.push_state()


def _jsonable(v):
    return v is None or isinstance(v, (bool, int, float, str, list, dict))


link = Link()


# ---------------------------------------------------------------- FL callbacks

def OnInit():
    _log("bridge v%d loaded (api %s); waiting for the companion on the EFLC MIDI port"
         % (PROTO, _safe(general.getVersion, default="?")))
    if not device.isAssigned():
        _log("output not linked yet: set the EFLC output Port to match the EFLC input Port")


def OnDeInit():
    link.hello_sent = False


def OnSysEx(event):
    dm = decode(event.sysex)
    if dm is None:
        event.handled = False
        return
    event.handled = True
    direction, msg = dm
    if direction != TO_FL:
        return  # our own message echoed back by the loopback port
    try:
        link.handle(msg)
    except Exception:
        _log("error: " + traceback.format_exc().replace("\n", " | "))


def OnRefresh(flags):
    if link.hello_sent:
        try:
            link.push_state()
        except Exception:
            _log("error: " + traceback.format_exc().replace("\n", " | "))


def OnMidiMsg(event):
    event.handled = False
