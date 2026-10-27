"""Run the real IFS Jacker firmware core on a PC, with simulated IFS units.

The IFS Jacker (github.com/ninjamida/ifs-jacker) is MicroPython for an
RP2040. This loads its own core.py, peripheral.py and peripheral modules
from a checkout of that repository (not copied here: it is under the
PolyForm Noncommercial license), and swaps only the hardware for stand-ins:

- the printer UART -> a pty, linked at --link (point [ifs] serial: at it)
- each IFS UART -> a FakeIfs (fake_ifs.py)
- machine / uselect / time.ticks_* -> small shims

So the routing, F13 merging, channel renumbering and Z commands are the
Jacker's own code. A line-based control port (--control, localhost) lets a
test put spools in or take them out:

    insert <ch>   remove <ch>   fault <ch>   log   quit
    (ch = 1-16 across all units)

    python3 jacker_emu.py --jacker-src ~/ifs-jacker/src --units 2 \\
        --link /tmp/ifs --control 7790 --peripheral rng
"""
import argparse
import os
import pty
import select
import socket
import sys
import threading
import time
import traceback
import tty
import types

from fake_ifs import FakeIfs

# MicroPython shims, installed before the Jacker modules are imported
_t0 = time.monotonic()
time.ticks_ms = lambda: int((time.monotonic() - _t0) * 1000)
time.ticks_add = lambda a, b: a + b
time.ticks_diff = lambda a, b: a - b
sys.print_exception = lambda e, f: traceback.print_exception(e, file=f)

machine = types.ModuleType("machine")
machine.PWRON_RESET, machine.WDT_RESET, machine.SOFT_RESET = 1, 3, 5
machine.HARD_RESET, machine.DEEPSLEEP_RESET = 2, 4
machine.freq = lambda *a: None
machine.reset_cause = lambda: machine.PWRON_RESET
class _Hw:
    def __init__(self, *a, **kw):
        raise RuntimeError("no hardware in the emulator")
machine.UART = machine.Pin = machine.ADC = machine.PWM = machine.I2C = _Hw
machine.mem32 = {}
sys.modules["machine"] = machine

uselect = types.ModuleType("uselect")
uselect.POLLIN = 1
class _Poll:
    def register(self, *a):
        pass
    def poll(self, *a):
        return []                 # no console input: the pty is the printer
uselect.poll = _Poll
sys.modules["uselect"] = uselect

def load_jacker(src):
    sys.path.insert(0, os.path.abspath(src))
    import comm, core, peripheral
    return comm, core, peripheral

def make_comms(comm):
    class PtyComm(comm._IJ_Comm_Abstract):
        """The printer link: the Jacker's own framing (inter-character
        timeout) over a pty instead of a UART."""
        def __init__(self, link):
            super().__init__()
            self.master, slave = pty.openpty()
            tty.setraw(self.master)
            tty.setraw(slave)
            self.slave = slave
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(os.ttyname(slave), link)
            self.path = os.ttyname(slave)
        def _comm_send(self, data):
            os.write(self.master, data)
        def _comm_check_receive(self):
            return bool(select.select([self.master], [], [], 0)[0])
        def _comm_receive(self):
            return os.read(self.master, 4096)

    class FakeIfsComm(comm._IJ_Comm_Abstract):
        """One IFS unit: the Jacker writes a command, the FakeIfs answers
        after its reply delay (no line ending, as the real unit)."""
        def __init__(self, ifs):
            super().__init__()
            self.ifs = ifs
            self.pending = []         # (due time, bytes)
        def _comm_send(self, data):
            reply = self.ifs.handle(data.decode("ascii", "replace"))
            if reply is not None:
                self.pending.append((time.monotonic() + self.ifs.reply_delay,
                                     reply.encode("ascii")))
        def _comm_check_receive(self):
            self.ifs.tick()
            return bool(self.pending) and \
                self.pending[0][0] <= time.monotonic()
        def _comm_receive(self):
            return self.pending.pop(0)[1]
    return PtyComm, FakeIfsComm

def control_server(port, units, core_ref):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    def unit_of(ch):
        u, c = (ch - 1) // 4, (ch - 1) % 4 + 1
        if not 0 <= u < len(units):
            raise ValueError("channel %d: only %d units" % (ch, len(units)))
        return units[u], c
    def client(conn):
        f = conn.makefile("rw")
        for line in f:
            words = line.split()
            if not words:
                continue
            try:
                cmd = words[0]
                if cmd == "quit":
                    core_ref[0].terminate = True
                    out = "bye"
                elif cmd == "log":
                    out = " | ".join("%s: %s" % (u.name, ", ".join(u.log))
                                     for u in units)
                    for u in units:
                        u.log.clear()
                else:
                    ifs, c = unit_of(int(words[1]))
                    {"insert": ifs.insert_spool, "remove": ifs.remove_spool,
                     "fault": ifs.fault}[cmd](c)
                    out = "ok"
            except (KeyError, IndexError, ValueError) as e:
                out = "error: %s" % (e,)
            f.write(out + "\n")
            f.flush()
        conn.close()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=client, args=(conn,), daemon=True).start()

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jacker-src", required=True,
                    help="src/ of a github.com/ninjamida/ifs-jacker checkout")
    ap.add_argument("--units", type=int, default=2)
    ap.add_argument("--link", default="/tmp/ifs")
    ap.add_argument("--control", type=int, default=7790)
    ap.add_argument("--speed", type=float, default=10.,
                    help="how much faster than a real IFS moves run")
    ap.add_argument("--loaded", default="1-8",
                    help="channels with filament at start, e.g. 1-6,8")
    ap.add_argument("--peripheral", action="append", default=[],
                    help="Jacker peripheral type to add (emu_temp, emu_pwm, rng, dummy)")
    ap.add_argument("--no-channel-count", action="store_true",
                    help="Jacker setting include_channel_count_in_status=false")
    args = ap.parse_args()

    comm, core, peripheral = load_jacker(args.jacker_src)
    PtyComm, FakeIfsComm = make_comms(comm)

    loaded = set()
    for part in args.loaded.split(","):
        if "-" in part:
            a, b = part.split("-")
            loaded.update(range(int(a), int(b) + 1))
        elif part:
            loaded.add(int(part))
    units = []
    for u in range(args.units):
        mask = sum(1 << (c - 1 - 4 * u) for c in loaded if 0 <= c - 1 - 4 * u < 4)
        units.append(FakeIfs("ifs%d" % (u + 1,), speed=args.speed, loaded=mask))

    jcore = core.IJ_Core()
    manager = comm.IJ_Comm_Manager()
    jcore.comm = manager
    manager.core = jcore
    jcore.printer_comm = PtyComm(args.link)
    jcore.mmu_comms = [FakeIfsComm(u) for u in units]
    manager.comm_list = [jcore.printer_comm] + jcore.mmu_comms
    # As the shipped config.ini: [core] timeout=5, mmu_timeout default
    jcore.printer_connected_timeout = 5000
    jcore.mmu_response_timeout = 65
    jcore.include_channel_count_in_status = not args.no_channel_count
    for i, ptype in enumerate(args.peripheral):
        p = peripheral.load_peripheral(i, {"type": ptype}, manager.comm_list)
        p.initialize()
        jcore.peripherals.append(p)
    # Console: no per-poll chatter
    jcore.console.exclude_categories = ["silent"]
    threading.Thread(target=control_server, args=(args.control, units, [jcore]),
                     daemon=True).start()
    print("IFS Jacker emulator: %d units, %d channels, printer link %s -> %s, "
          "control port %d" % (len(units), 4 * len(units), args.link,
                               jcore.printer_comm.path, args.control),
          flush=True)

    # The Jacker's main loop spins; give the host CPU a break each turn
    update = manager.update
    def paced_update():
        update()
        time.sleep(.0005)
    manager.update = paced_update
    jcore.run()

if __name__ == "__main__":
    main()
