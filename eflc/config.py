"""Settings and profile files.

User data lives in %APPDATA%\\EFLC:
  settings.json        pad calibration, HUD placement, port, active profile
  profiles\\*.json      mapping profiles (bundled ones are copied on first run)
  eflc.log
"""

import copy
import json
import logging
import os
import shutil

from .input.pad import canonical
from .core.profile import load_profile

log = logging.getLogger("eflc.config")

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_PROFILES = os.path.join(PKG_DIR, "profiles")


def user_dir():
    base = os.environ.get("EFLC_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "EFLC")
    os.makedirs(os.path.join(base, "profiles"), exist_ok=True)
    return base


def profiles_dir():
    return os.path.join(user_dir(), "profiles")


DEFAULT_SETTINGS = {
    "midi_port": "EFLC",
    "profile": "Default",
    "pad": {},
    "hud": {"corner": "bottom-right", "margin": 24, "opacity": 0.92, "scale": 1.0, "visible": True,
            "style": "card"},
    "tuning": {},            # manual adjustments from the settings view (overrides on top of the profile)
    "magnetic_start": True,
}


def load_settings():
    path = os.path.join(user_dir(), "settings.json")
    data = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
    except FileNotFoundError:
        pass
    except ValueError as e:
        log.error("settings.json is not valid JSON (%s); using defaults", e)
    return data


def save_settings(data):
    path = os.path.join(user_dir(), "settings.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _sha(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def install_bundled_profiles():
    """Install new bundled profiles and upgrade ones the user hasn't edited.

    A manifest (.bundled.json) remembers the hash of each file as installed;
    a user file that still matches it is safe to replace with a newer bundled
    version. Edited files are left alone.
    """
    dst = profiles_dir()
    manifest_path = os.path.join(dst, ".bundled.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    changed = False
    for fn in profile_files(BUNDLED_PROFILES):
        src, target = os.path.join(BUNDLED_PROFILES, fn), os.path.join(dst, fn)
        new_hash = _sha(src)
        if not os.path.exists(target):
            shutil.copy2(src, target)
            log.info("installed profile %s", fn)
        else:
            current = _sha(target)
            if current == new_hash:
                pass
            elif manifest.get(fn) == current:
                shutil.copy2(src, target)
                log.info("upgraded profile %s", fn)
            else:
                if manifest.get(fn) != current:
                    log.info("profile %s has your edits; keeping it (bundled version differs)", fn)
                continue
        if manifest.get(fn) != new_hash:
            manifest[fn] = new_hash
            changed = True
    if changed:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)


# ---------------------------------------------------------------- extends

def _input_key(b):
    parts = [canonical(p) for p in str(b.get("input", "")).split("+") if p.strip()]
    return (tuple(sorted(parts[:-1])), parts[-1] if parts else "", b.get("on", "press"))


def merge_profile(parent, child):
    out = copy.deepcopy(parent)
    out["name"] = child.get("name", out.get("name"))
    out.setdefault("timing", {}).update(child.get("timing") or {})
    out.setdefault("modifiers", {}).update(child.get("modifiers") or {})
    ctxs = out.setdefault("contexts", {})
    for cname, cspec in (child.get("contexts") or {}).items():
        base = ctxs.setdefault(cname, {})
        mine = cspec.get("bindings") or []
        overridden = {_input_key(b) for b in mine}
        base["bindings"] = [b for b in base.get("bindings") or [] if _input_key(b) not in overridden] + mine
        base.setdefault("axes", {}).update(cspec.get("axes") or {})
        base["sequences"] = (base.get("sequences") or []) + (cspec.get("sequences") or [])
        if "menu" in cspec:
            base["menu"] = cspec["menu"]
    return out


def _resolve(name, raw, stack=()):
    data = raw[name]
    parent = data.get("extends")
    if not parent:
        return data
    if parent in stack or parent not in raw:
        raise ValueError("profile %r extends unknown or circular profile %r" % (name, parent))
    return merge_profile(_resolve(parent, raw, stack + (name,)), data)


def profile_files(directory=None):
    """Profile file names in a directory (dotfiles like .bundled.json are metadata)."""
    directory = directory or profiles_dir()
    return sorted(fn for fn in os.listdir(directory) if fn.endswith(".json") and not fn.startswith("."))


def load_profiles(parse_combo, directory=None):
    """Returns ({name: Profile}, [error strings])."""
    directory = directory or profiles_dir()
    raw, errors = {}, []
    for fn in profile_files(directory):
        path = os.path.join(directory, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            name = str(data.get("name") or os.path.splitext(fn)[0])
            data["name"] = name
            raw[name] = data
        except (OSError, ValueError) as e:
            errors.append("%s: %s" % (fn, e))
    profiles = {}
    for name in raw:
        try:
            p = load_profile(_resolve(name, raw), parse_combo, source=name)
        except ValueError as e:
            errors.append(str(e))
            continue
        profiles[name] = p
        errors.extend("%s: %s" % (name, e) for e in p.errors)
    return profiles, errors


def profiles_signature(directory=None):
    """Cheap change detector for hot-reload."""
    directory = directory or profiles_dir()
    sig = []
    for fn in profile_files(directory):
        try:
            st = os.stat(os.path.join(directory, fn))
            sig.append((fn, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sig)
