"""Windows UI Automation: the interactive element under a point, in any app.

Used for everything that isn't drawn by FL itself (FL only exposes window
frames): Windows menus, dialogs (including FL's file dialogs), browsers,
Explorer, settings... Must be created and used on one thread (the tracker).
"""

import ctypes
from ctypes import wintypes

# UIA control type ids that are worth snapping to
INTERACTIVE = {
    50000,  # Button
    50002,  # CheckBox
    50003,  # ComboBox
    50004,  # Edit
    50005,  # Hyperlink
    50007,  # ListItem
    50011,  # MenuItem
    50013,  # RadioButton
    50015,  # Slider
    50016,  # Spinner
    50019,  # TabItem
    50024,  # TreeItem
    50029,  # DataItem
    50031,  # SplitButton
    50035,  # HeaderItem
}


class Uia:
    def __init__(self):
        import comtypes
        import comtypes.client
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        self._comtypes = comtypes
        self.mod = comtypes.client.GetModule("UIAutomationCore.dll")
        self.uia = comtypes.client.CreateObject(self.mod.CUIAutomation, interface=self.mod.IUIAutomation)
        self.walker = self.uia.ControlViewWalker

    def element_at(self, x, y, max_size=(900, 220)):
        """(left, top, right, bottom, name) of the interactive element at (x, y), or None."""
        try:
            el = self.uia.ElementFromPoint(self.mod.tagPOINT(int(x), int(y)))
        except Exception:
            return None
        for _ in range(4):  # a Text inside a Button: walk up to the Button
            if not el:
                return None
            try:
                ct = el.CurrentControlType
                if ct in INTERACTIVE:
                    r = el.CurrentBoundingRectangle
                    w, h = r.right - r.left, r.bottom - r.top
                    if 4 <= w <= max_size[0] and 4 <= h <= max_size[1] and r.left <= x < r.right and r.top <= y < r.bottom:
                        return (r.left, r.top, r.right, r.bottom, el.CurrentName or "")
                    return None
                el = self.walker.GetParentElement(el)
            except Exception:
                return None
        return None

    def close(self):
        try:
            self._comtypes.CoUninitialize()
        except Exception:
            pass
