"""A simulated Flashforge AD5X IFS unit (4 channels).

Behaviour follows Kiln OS docs/ifs-protocol.md (the commands, replies and
FFS_state values found there); move times are length / speed, divided by
`speed` to run faster than a real unit. Not a byte-exact copy of the stock
firmware: enough to drive Kiln's ifs.py through selects, loads, unloads,
purges, pre-feeds and ejects.

Used by jacker_emu.py (two or more units behind the real IFS Jacker core),
or on its own as one plain IFS on a pty:

    python3 fake_ifs.py --link /tmp/ifs
"""
import argparse
import os
import pty
import re
import select
import threading
import time
import tty

STATE_READY = 5
STRIDE = 11
FEEDING, RELEASING, EXITING = 10, 12, 14
STATE_FAULT = 0x7f

class FakeIfs:
    def __init__(self, name="ifs", speed=10., loaded=0b1111, reply_delay=.005):
        self.name = name
        self.speed = speed
        self.reply_delay = reply_delay
        self.lock = threading.RLock()
        self.state = STATE_READY
        self.chan = 0
        self.silk = loaded            # bit n = channel n+1 has filament
        self.insert = 0               # new-spool flags (F22/F23)
        self.stall = 0
        self.busy_until = 0.
        self.after = None             # state change when the move ends
        self.irun = {1: 16, 2: 16}
        self.log = []                 # commands received, for tests
    # Moves
    def _move(self, state, seconds, after=None):
        self.state = state
        self.busy_until = time.monotonic() + seconds / self.speed
        self.after = after
    def tick(self):
        with self.lock:
            if self.state not in (STATE_READY, STATE_FAULT) \
                    and time.monotonic() >= self.busy_until:
                after, self.after = self.after, None
                self.state = STATE_READY
                if after:
                    after()
    # Test controls (jacker_emu.py control port)
    def insert_spool(self, ch):
        with self.lock:
            self.silk |= 1 << (ch - 1)
            self.insert |= 1 << (ch - 1)
    def remove_spool(self, ch):
        with self.lock:
            self.silk &= ~(1 << (ch - 1))
    def fault(self, ch):
        with self.lock:
            self.state = STATE_FAULT
            self.stall |= 1 << (ch - 1)
    # Protocol
    def handle(self, line):
        """One command line -> reply text (no line ending), or None (the
        IFS stays silent on unknown commands)."""
        self.tick()
        m = re.search(r"F(\d+)", line)
        if not m:
            return None
        f = int(m.group(1))
        params = dict((k, float(v)) for k, v in
                      re.findall(r"\b([CLSTID])(-?\d+(?:\.\d+)?)", line))
        c = int(params.get('C', 0))
        with self.lock:
            self.log.append(line.strip())
            return self._handle(f, c, params)
    def _status(self):
        return ("F13 ok. FFS_state: %d silk_state: %d chan: %d "
                "ffs_channels_insert: %d stall_state: %d "
                "jinsi_GCONF: 000001c0 qiehuan_GCONF: 000001c0" % (
                    self.state, self.silk, self.chan, self.insert,
                    self.stall))
    def _channel_move(self, f, c):
        ch = c or self.chan
        if self.state != STATE_READY:
            return "F%d ok. FFS not ready." % (f,)
        if not ch:
            return "F%d ok. No channel selected." % (f,)
        if not 1 <= ch <= 4:
            return "F%d ok. FFS channel not exist." % (f,)
        return None, ch
    def _handle(self, f, c, p):
        if f == 13:
            return self._status()
        if f == 19:
            return "F19 ok. four color. version: 3.0.7"
        if f == 20:
            return "F20 ok."
        if f == 21:
            silk = [3800 if self.silk >> i & 1 else 200 for i in range(4)]
            return ("F21 ok.\r\n silk: %d %d %d %d \r\n stall: 0 0 0 0"
                    % tuple(silk))
        if f == 22:
            return "F22 ok. ffs_channels_insert: %d" % (self.insert,)
        if f == 14:
            st = [self.stall >> i & 1 for i in range(4)]
            return "F14 ok. stall: %d %d %d %d" % tuple(st)
        if f == 15:
            self.state = STATE_READY
            self.stall = 0
            self.after = None
            return "F15 ok."
        if f == 18:
            # Sent by the splitter to the units not being selected
            self._move(1, .3)
            return "F18 ok"
        if f == 24:
            if not 1 <= c <= 4:
                return "F24 ok. no chan."
            self.chan = c
            self._move(1, .5)
            return "F24 ok. chan %d." % (c,)
        if f == 23:
            if not 1 <= c <= 4:
                return "F23 ok. no chan."
            self.insert &= ~(1 << (c - 1))
            return "F23 ok. chan %d." % (c,)
        if f == 42:
            t, i = int(p.get('T', 0)), int(p.get('I', 0))
            self.irun[t] = max(0, min(31, i))
            return ("F42 ok.stepper_motor: %d stepper_motor_irun: %d"
                    % (t, self.irun.get(t, 0)))
        if f == 112:
            if self.state == STATE_FAULT:
                return "F112 ok. error."
            self.state = STATE_READY
            self.after = None
            return "F112 ok. yes."
        if f in (10, 11, 39):
            r = self._channel_move(f, c)
            if isinstance(r, str):
                return r
            ch = r[1]
            if f == 39:
                self._move(RELEASING + STRIDE * (ch - 1), .3)
                return "F39 ok. FFS channel %d release." % (ch,)
            length = abs(p.get('L', 100.))
            speed = p.get('S', 600.) or 600.
            seconds = length / speed * 60.
            if f == 10:
                self._move(FEEDING + STRIDE * (ch - 1), seconds)
                return "F10 ok. FFS channel %d feeding." % (ch,)
            # A long pull takes the strand out of the IFS (eject)
            after = None
            if length >= 500:
                def after(ch=ch):
                    self.silk &= ~(1 << (ch - 1))
            self._move(EXITING + STRIDE * (ch - 1), seconds, after)
            return "F11 ok. FFS channel %d exiting." % (ch,)
        return None

def serve_pty(ifs, link, log=None):
    """One FakeIfs on a pty, linked at `link`. Replies go without a line
    ending, as the real unit frames them by the pause after them. log: a
    file that gets every line received (also ones the IFS ignores)."""
    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(os.ttyname(slave), link)
    print("%s on %s -> %s" % (ifs.name, link, os.ttyname(slave)), flush=True)
    buf = b""
    while True:
        r, _, _ = select.select([master], [], [], .05)
        ifs.tick()
        if not r:
            continue
        buf += os.read(master, 4096)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.decode("ascii", "replace")
            if log is not None:
                log.write(line.strip() + "\n")
                log.flush()
            reply = ifs.handle(line)
            if reply is not None:
                time.sleep(ifs.reply_delay)
                os.write(master, reply.encode("ascii"))

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--link", default="/tmp/ifs")
    ap.add_argument("--speed", type=float, default=10.)
    ap.add_argument("--log", help="file for every line received")
    args = ap.parse_args()
    serve_pty(FakeIfs(speed=args.speed), args.link,
              open(args.log, "w") if args.log else None)
