"""Foreground window / process tracking."""

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

# FL itself plus the bridge host for plugins running "bridged" or out-of-process.
FL_EXES = {"fl64.exe", "fl.exe", "fl64 (scaled).exe", "ilbridge.exe", "ilpluginhost.exe"}


def exe_for_pid(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


class ForegroundTracker:
    def __init__(self, interval=0.1, own_pid=None):
        self.interval = interval
        self.own_pid = own_pid if own_pid is not None else os.getpid()
        self._next = 0.0
        self._cache = {}
        self.exe = ""

    def poll(self, now):
        if now < self._next:
            return self.exe
        self._next = now + self.interval
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == self.own_pid:
            return self.exe  # our own windows never change the context
        exe = self._cache.get(pid.value)
        if exe is None:
            exe = exe_for_pid(pid.value)
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[pid.value] = exe
        self.exe = exe
        return exe

    @property
    def is_fl(self):
        return self.exe in FL_EXES
