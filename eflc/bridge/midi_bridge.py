"""Companion side of the FL link: SysEx over the loopMIDI port.

Polled from the engine thread; never blocks. FL's script sandbox blocks
sockets, so MIDI is the transport. The link object only needs
open()/receive()/send()/close(), which keeps this testable without winmm.
"""

import logging
import time

from .protocol import FROM_FL, TO_FL, decode, encode

log = logging.getLogger("eflc.bridge")

POLL_INTERVAL = 0.08   # companion -> FL polls (FL answers with state)
STALE_AFTER = 3.0      # no reply for this long -> FL is gone
REOPEN_INTERVAL = 2.0


class MidiBridge:
    def __init__(self, link):
        self.link = link
        self.port = getattr(link, "name", "?")
        self.state = {}
        self.hello = {}
        self.last_msg_t = 0.0
        self.last_error = None   # last failed command or port problem, for the HUD
        self._next_id = 1
        self._pending = {}
        self._next_poll = 0.0
        self._next_open = 0.0
        self._was_connected = False
        self._open_error = None

    def start(self):
        self._try_open(raise_errors=True)

    def stop(self):
        try:
            self.link.close()
        except Exception:
            log.exception("closing MIDI link")

    @property
    def connected(self):
        return (time.monotonic() - self.last_msg_t) < STALE_AFTER

    def _try_open(self, raise_errors=False):
        try:
            self.link.open()
        except OSError as e:
            if str(e) != self._open_error:
                self._open_error = str(e)
                log.warning("bridge: %s", e)
            if raise_errors:
                raise
            return False
        self._open_error = None
        log.info("bridge: MIDI port %r open", self.port)
        return True

    # ------------------------------------------------------------------
    def poll(self):
        now = time.monotonic()
        if not self.link.is_open:
            if now >= self._next_open:
                self._next_open = now + REOPEN_INTERVAL
                self._try_open()
            return
        for raw in self.link.receive():
            dm = decode(raw)
            if dm is None or dm[0] != FROM_FL:
                continue  # not ours, or our own echo from the loopback port
            self._handle(dm[1], now)
        connected = self.connected
        if connected != self._was_connected:
            self._was_connected = connected
            log.info("bridge: FL %s", "linked" if connected else "lost")
            if not connected:
                self.state = {}
                self._pending.clear()
        if now >= self._next_poll:
            self._next_poll = now + POLL_INTERVAL
            msg = {"t": "poll"}
            if not connected:
                msg["hello"] = True
            self.link.send(encode(TO_FL, msg))

    def command(self, op, *args, callback=None):
        """Send a whitelisted FL command. Returns the id, or None if FL is not linked.

        callback(ok, value_or_error) runs on the engine thread when FL answers.
        """
        if not self.connected or not self.link.is_open:
            return None
        cid = self._next_id
        self._next_id += 1
        self._pending[cid] = (op, callback)
        self.link.send(encode(TO_FL, {"t": "cmd", "id": cid, "op": op, "args": list(args)}))
        return cid

    def send_midi(self, status, data1=0, data2=0):
        """Plain MIDI to FL (mapping mode): lands on FL's EFLC input like a hardware controller."""
        if self.link.is_open and hasattr(self.link, "send_short"):
            self.link.send_short(status, data1, data2)
            return True
        return False

    def _handle(self, msg, now):
        self.last_msg_t = now
        t = msg.get("t")
        if t == "state":
            self.state = msg.get("s") or {}
        elif t == "hello":
            self.hello = msg
            log.info("bridge: hello %s", msg)
        elif t == "result":
            op, callback = self._pending.pop(msg.get("id"), ("?", None))
            if callback is not None:
                try:
                    callback(bool(msg.get("ok")), msg.get("value") if msg.get("ok") else msg.get("err"))
                except Exception:
                    log.exception("bridge: callback for %s failed", op)
            elif not msg.get("ok"):
                self.last_error = "%s failed: %s" % (op, msg.get("err"))
                log.warning("bridge: %s", self.last_error)
