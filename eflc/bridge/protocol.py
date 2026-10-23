"""EFLC SysEx framing, shared with fl_script/EFLC/device_EFLC.py (keep in sync).

    F0 7D 45 46 <dir> <ASCII JSON> F7
    7D = non-commercial manufacturer id, 45 46 = "EF"
    dir 01 = companion -> FL, 02 = FL -> companion

ensure_ascii JSON is 7-bit clean, so no extra SysEx encoding is needed.
"""

import json

HEADER = bytes([0xF0, 0x7D, 0x45, 0x46])
TO_FL = 0x01
FROM_FL = 0x02


def encode(direction, msg):
    payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return HEADER + bytes([direction]) + payload + b"\xf7"


def decode(data):
    """SysEx bytes -> (direction, msg), or None if it isn't an EFLC message."""
    data = bytes(data)
    if len(data) < 7 or not data.startswith(HEADER) or data[-1] != 0xF7:
        return None
    try:
        return data[4], json.loads(data[5:-1].decode("ascii"))
    except ValueError:
        return None
