"""Live controller monitor - check stick drift and deadzones.

    python -m eflc.tools.padmon            show raw vs. processed values
    python -m eflc.tools.padmon --calibrate  measure rest position and save it

Leave the sticks untouched to see drift: the raw column moves, the processed
column should stay at 0.00 once the deadzone is right.
"""

import sys
import time

from .. import config
from ..input.pad import Calibrator, PadConfig, PadReader
from ..input.xinput import XInputBackend


def main():
    settings = config.load_settings()
    reader = PadReader(XInputBackend(), PadConfig.from_dict(settings.get("pad") or {}))
    if "--calibrate" in sys.argv:
        cal = Calibrator(duration=2.0)
        print("Calibrating: don't touch the sticks for 2 seconds...")
        while True:
            now = time.monotonic()
            reader.poll(now)
            if reader.last_raw is None:
                print("No XInput controller found.")
                return
            if cal.feed(now, reader.last_raw):
                break
            time.sleep(0.005)
        for stick, (cx, cy, dz) in cal.apply(reader.config).items():
            print("%s  centre (%+.3f, %+.3f)  deadzone %.3f" % (stick, cx, cy, dz))
        settings["pad"] = reader.config.to_dict()
        config.save_settings(settings)
        print("Saved to", config.user_dir())
        return

    print("Ctrl+C to quit. Deadzones:", {k: v.deadzone for k, v in reader.config.sticks.items()})
    try:
        while True:
            st = reader.poll(time.monotonic())
            raw = reader.last_raw
            if raw is None:
                line = "no controller"
            else:
                lx, ly = st.axes["LS"]
                rx, ry = st.axes["RS"]
                line = ("LS raw %+.3f %+.3f -> %+.2f %+.2f | RS raw %+.3f %+.3f -> %+.2f %+.2f | "
                        "LT %.2f RT %.2f | %s" % (raw.lx, raw.ly, lx, ly, raw.rx, raw.ry, rx, ry,
                                                  raw.lt, raw.rt, " ".join(sorted(st.pressed)) or "-"))
            sys.stdout.write("\r" + line.ljust(150))
            sys.stdout.flush()
            time.sleep(0.03)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
