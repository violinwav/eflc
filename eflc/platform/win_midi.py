"""Minimal winmm SysEx link to one (loopMIDI) port, via ctypes.

The input side uses CALLBACK_NULL and polls buffer flags from the engine
thread, so there are no cross-thread callbacks to worry about.
"""

import ctypes
from ctypes import wintypes

winmm = ctypes.WinDLL("winmm")

CALLBACK_NULL = 0
MHDR_DONE = 0x1
MMSYSERR_NOERROR = 0
MMSYSERR_ALLOCATED = 4
DWORD_PTR = ctypes.c_size_t


class MIDIHDR(ctypes.Structure):
    pass


MIDIHDR._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", DWORD_PTR),
    ("dwFlags", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(MIDIHDR)),
    ("reserved", DWORD_PTR),
    ("dwOffset", wintypes.DWORD),
    ("dwReserved", DWORD_PTR * 8),
]


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("dwSupport", wintypes.DWORD)]


class MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("wTechnology", wintypes.WORD),
                ("wVoices", wintypes.WORD), ("wNotes", wintypes.WORD),
                ("wChannelMask", wintypes.WORD), ("dwSupport", wintypes.DWORD)]


HMIDI = wintypes.HANDLE
_HDR = ctypes.POINTER(MIDIHDR)
_SZ = ctypes.sizeof(MIDIHDR)
for _name, _args in {
    "midiInGetDevCapsW": [DWORD_PTR, ctypes.POINTER(MIDIINCAPSW), wintypes.UINT],
    "midiOutGetDevCapsW": [DWORD_PTR, ctypes.POINTER(MIDIOUTCAPSW), wintypes.UINT],
    "midiInOpen": [ctypes.POINTER(HMIDI), wintypes.UINT, DWORD_PTR, DWORD_PTR, wintypes.DWORD],
    "midiOutOpen": [ctypes.POINTER(HMIDI), wintypes.UINT, DWORD_PTR, DWORD_PTR, wintypes.DWORD],
    "midiInPrepareHeader": [HMIDI, _HDR, wintypes.UINT],
    "midiInUnprepareHeader": [HMIDI, _HDR, wintypes.UINT],
    "midiInAddBuffer": [HMIDI, _HDR, wintypes.UINT],
    "midiOutPrepareHeader": [HMIDI, _HDR, wintypes.UINT],
    "midiOutUnprepareHeader": [HMIDI, _HDR, wintypes.UINT],
    "midiOutLongMsg": [HMIDI, _HDR, wintypes.UINT],
    "midiOutShortMsg": [HMIDI, wintypes.DWORD],
    "midiInStart": [HMIDI], "midiInStop": [HMIDI], "midiInReset": [HMIDI], "midiInClose": [HMIDI],
    "midiOutReset": [HMIDI], "midiOutClose": [HMIDI],
}.items():
    getattr(winmm, _name).argtypes = _args
    getattr(winmm, _name).restype = wintypes.UINT


def list_ports():
    ins, outs = [], []
    for i in range(winmm.midiInGetNumDevs()):
        c = MIDIINCAPSW()
        if winmm.midiInGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c)) == 0:
            ins.append(c.szPname)
    for i in range(winmm.midiOutGetNumDevs()):
        c = MIDIOUTCAPSW()
        if winmm.midiOutGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c)) == 0:
            outs.append(c.szPname)
    return ins, outs


def _find(names, wanted):
    for i, n in enumerate(names):
        if n == wanted:
            return i
    for i, n in enumerate(names):
        if n.lower().startswith(wanted.lower()):
            return i
    return None


class MidiPortError(OSError):
    pass


class WinMidiLink:
    """SysEx in/out on one named port (e.g. the loopMIDI port 'EFLC')."""

    def __init__(self, name="EFLC", buffers=16, size=16384):
        self.name = name
        self._nbuf, self._size = buffers, size
        self._hin = HMIDI()
        self._hout = HMIDI()
        self._bufs, self._hdrs = [], []
        self._sending = []
        self._partial = b""
        self.is_open = False

    def open(self):
        ins, outs = list_ports()
        i, o = _find(ins, self.name), _find(outs, self.name)
        if i is None or o is None:
            raise MidiPortError("MIDI port %r not found (is loopMIDI running?)" % self.name)
        r = winmm.midiInOpen(ctypes.byref(self._hin), i, 0, 0, CALLBACK_NULL)
        if r:
            raise MidiPortError("can't open MIDI input %r (error %d%s)" % (
                self.name, r, ", in use by another app" if r == MMSYSERR_ALLOCATED else ""))
        r = winmm.midiOutOpen(ctypes.byref(self._hout), o, 0, 0, CALLBACK_NULL)
        if r:
            winmm.midiInClose(self._hin)
            raise MidiPortError("can't open MIDI output %r (error %d)" % (self.name, r))
        for _ in range(self._nbuf):
            buf = ctypes.create_string_buffer(self._size)
            hdr = MIDIHDR()
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = self._size
            self._bufs.append(buf)
            self._hdrs.append(hdr)
            self._queue(hdr)
        winmm.midiInStart(self._hin)
        self.is_open = True

    def _queue(self, hdr):
        hdr.dwBytesRecorded = 0
        hdr.dwFlags = 0
        winmm.midiInPrepareHeader(self._hin, ctypes.byref(hdr), _SZ)
        winmm.midiInAddBuffer(self._hin, ctypes.byref(hdr), _SZ)

    def receive(self):
        """Complete SysEx messages received since the last call."""
        out = []
        if not self.is_open:
            return out
        for buf, hdr in zip(self._bufs, self._hdrs):
            if not hdr.dwFlags & MHDR_DONE:
                continue
            data = ctypes.string_at(buf, hdr.dwBytesRecorded)
            winmm.midiInUnprepareHeader(self._hin, ctypes.byref(hdr), _SZ)
            self._queue(hdr)
            self._partial += data
            while True:
                start = self._partial.find(b"\xf0")
                if start < 0:
                    self._partial = b""
                    break
                end = self._partial.find(b"\xf7", start)
                if end < 0:
                    self._partial = self._partial[start:]
                    break
                out.append(self._partial[start:end + 1])
                self._partial = self._partial[end + 1:]
        return out

    def send(self, data):
        if not self.is_open:
            return False
        self._reap()
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        hdr = MIDIHDR()
        hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
        hdr.dwBufferLength = len(data)
        winmm.midiOutPrepareHeader(self._hout, ctypes.byref(hdr), _SZ)
        r = winmm.midiOutLongMsg(self._hout, ctypes.byref(hdr), _SZ)
        if r != MMSYSERR_NOERROR:
            winmm.midiOutUnprepareHeader(self._hout, ctypes.byref(hdr), _SZ)
            return False
        # The driver owns the buffer until MHDR_DONE: keep it alive until then.
        self._sending.append((buf, hdr))
        self._reap()
        return True

    def send_short(self, status, data1=0, data2=0):
        if not self.is_open:
            return False
        msg = (status & 0xFF) | ((data1 & 0x7F) << 8) | ((data2 & 0x7F) << 16)
        return winmm.midiOutShortMsg(self._hout, msg) == MMSYSERR_NOERROR

    def _reap(self):
        keep = []
        for buf, hdr in self._sending:
            if hdr.dwFlags & MHDR_DONE:
                winmm.midiOutUnprepareHeader(self._hout, ctypes.byref(hdr), _SZ)
            else:
                keep.append((buf, hdr))
        self._sending = keep

    def close(self):
        if not self.is_open:
            return
        self.is_open = False
        winmm.midiInStop(self._hin)
        winmm.midiInReset(self._hin)
        for hdr in self._hdrs:
            winmm.midiInUnprepareHeader(self._hin, ctypes.byref(hdr), _SZ)
        winmm.midiInClose(self._hin)
        winmm.midiOutReset(self._hout)  # returns pending output buffers as done
        self._reap()
        winmm.midiOutClose(self._hout)
        self._bufs, self._hdrs, self._sending = [], [], []
