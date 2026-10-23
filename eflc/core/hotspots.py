"""Taught plugin hotspots: named points on a plugin window.

Stored per plugin in %APPDATA%\\EFLC\\hotspots\\<plugin>.json as offsets from
the plugin window's top-left corner (physical pixels), so they follow the
window wherever it's moved.
"""

import json
import os
import re
import threading


def plugin_key(title):
    """Plugin window titles look like 'Serum\\ue5a0 (Lead)\\ue5ff'; the plugin is the first part.

    FL decorates titles with private-use icon glyphs (U+E000..U+F8FF): drop them first.
    """
    t = re.sub("[-]", "", title or "").strip()
    t = re.sub(r"\s*\(.*\)\s*$", "", t)
    return t or "Unknown"


class HotspotStore:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self._cache = {}
        self._lock = threading.Lock()

    def _path(self, key):
        safe = re.sub(r'[<>:"/\\|?*]', "_", key)
        return os.path.join(self.dir, safe + ".json")

    def spots(self, key):
        with self._lock:
            if key not in self._cache:
                try:
                    with open(self._path(key), "r", encoding="utf-8") as f:
                        self._cache[key] = json.load(f).get("spots", [])
                except (OSError, ValueError):
                    self._cache[key] = []
            return list(self._cache[key])

    def add(self, key, dx, dy, name=None):
        spots = self.spots(key)
        for s in spots:  # re-teaching the same spot just renames it
            if abs(s["x"] - dx) <= 6 and abs(s["y"] - dy) <= 6:
                if name:
                    s["name"] = name
                break
        else:
            spots.append({"name": name or "Spot %d" % (len(spots) + 1), "x": int(dx), "y": int(dy)})
        self._save(key, spots)
        return len(spots)

    def remove_near(self, key, dx, dy, radius=20):
        spots = self.spots(key)
        keep = [s for s in spots if (s["x"] - dx) ** 2 + (s["y"] - dy) ** 2 > radius ** 2]
        if len(keep) != len(spots):
            self._save(key, keep)
            return True
        return False

    def _save(self, key, spots):
        with self._lock:
            self._cache[key] = spots
            path = self._path(key)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"plugin": key, "spots": spots}, f, indent=2)
            os.replace(tmp, path)

    def screen_spots(self, form):
        """Hotspots of a form in screen coordinates: [(x, y, name)]."""
        ox, oy = form.rect.left, form.rect.top
        return [(ox + s["x"], oy + s["y"], s["name"]) for s in self.spots(plugin_key(form.title))]
