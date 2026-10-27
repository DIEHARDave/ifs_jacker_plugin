"""Kiln OS ifs.py and this plugin against the IFS Jacker emulator.

Runs Klipper (Kiln's pinned version, batch mode, no hardware) with Kiln's
ifs.py and the plugin's extras, on the link of jacker_emu.py (the real
Jacker core with simulated IFS units) or of fake_ifs.py (one plain IFS),
and checks what Kiln does through Klipper's API socket.

    python3 test_kiln_jacker.py --klipper ~/kiln-klipper \\
        --kiln ~/kiln-os --jacker-src ~/ifs-jacker/src

Linux only (ptys). --klipper needs out/klipper.dict from a linux-process
build (make with CONFIG_MACH_LINUX=y) and a Python with Klipper's
requirements (--python).
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.dirname(HERE)
PLUGIN = os.path.dirname(EMU)
WORK = os.path.expanduser("~/kiln_ifs_test")
LINK = "/tmp/ifs"
CONTROL = 7790

results = []

def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        "  (%s)" % (detail,) if detail else ""), flush=True)
    return ok

class Api:
    """Klipper in batch mode, driven like its API: G-code goes into the
    input FIFO wrapped in TEST_RUN (kiln_test_probe.py), results come back
    in results.jsonl, status from status.json."""
    def __init__(self, writer):
        self.writer = writer
        self.next_id = 0
        self.lock = threading.Lock()
        self.results = os.path.join(WORK, "results.jsonl")
        self.status = os.path.join(WORK, "status.json")
        self.seen = 0
    def _result(self, rid, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(self.results):
                for line in open(self.results).read().splitlines():
                    r = json.loads(line)
                    if r['id'] == rid:
                        return r
            time.sleep(.05)
        raise TimeoutError("no result for TEST_RUN %s" % (rid,))
    def gcode(self, script, timeout=180.):
        """Run G-code lines in order, stopping at the first error; returns
        (ok, error message, output lines)."""
        out = []
        with self.lock:
            for line in [l for l in script.split("\n") if l.strip()]:
                self.next_id += 1
                rid = str(self.next_id)
                os.write(self.writer, ('TEST_RUN ID=%s CMD="%s"\n' % (
                    rid, line.replace('"', "'"))).encode())
                r = self._result(rid, timeout)
                out += r['output']
                if not r['ok']:
                    return False, r['error'], out
        time.sleep(.15)          # the next status.json
        return True, "", out
    def query(self, *objects):
        for i in range(20):
            try:
                st = json.load(open(self.status))['status']
                break
            except (OSError, ValueError):
                time.sleep(.05)
        return {o: st.get(o) for o in objects}

class Emulator:
    def __init__(self, args, kind, extra=()):
        if os.path.lexists(LINK):
            os.unlink(LINK)
        self.log = os.path.join(WORK, "%s.log" % (kind,))
        if kind == "jacker":
            cmd = [sys.executable, "-u", os.path.join(EMU, "jacker_emu.py"),
                   "--jacker-src", args.jacker_src, "--link", LINK,
                   "--control", str(CONTROL), "--speed", str(args.speed)]
        else:
            self.cmds = os.path.join(WORK, "stock_cmds.log")
            cmd = [sys.executable, "-u", os.path.join(EMU, "fake_ifs.py"),
                   "--link", LINK, "--speed", str(args.speed),
                   "--log", self.cmds]
        self.proc = subprocess.Popen(list(cmd) + list(extra), cwd=EMU,
                                     stdout=open(self.log, "w"),
                                     stderr=subprocess.STDOUT)
        for i in range(50):
            if os.path.exists(LINK):
                break
            time.sleep(.1)
        time.sleep(.5)
    def control(self, line):
        with socket.create_connection(("127.0.0.1", CONTROL)) as s:
            f = s.makefile("rw")
            f.write(line + "\n")
            f.flush()
            return f.readline().strip()
    def unit_log(self):
        """Commands each unit got since the last call: {'ifs1': [...]}."""
        out = {}
        for part in self.control("log").split(" | "):
            name, _, cmds = part.partition(": ")
            # Without the status polls
            out[name] = [c for c in cmds.split(", ") if c and c != "F13"]
        return out
    def stop(self):
        self.proc.terminate()
        self.proc.wait()

PROBE_OBJECTS = ("ifs, ifs_materials, kiln_ifsjacker, print_stats, idle_timeout, "
                 "temperature_sensor ifs_box, fan_generic rear")

class Klippy:
    def __init__(self, args, cfg_extra):
        cfg = open(os.path.join(HERE, "printer.cfg")).read().replace(
            "/tmp/kiln_ifs_test", WORK) + cfg_extra
        cfg += "\n[kiln_test_probe]\ndir: %s\nobjects: %s\n" % (
            WORK, PROBE_OBJECTS)
        self.cfg = os.path.join(WORK, "printer.cfg")
        open(self.cfg, "w").write(cfg)
        for name in ("results.jsonl", "status.json"):
            if os.path.exists(os.path.join(WORK, name)):
                os.unlink(os.path.join(WORK, name))
        self.fifo = os.path.join(WORK, "input.fifo")
        if os.path.exists(self.fifo):
            os.unlink(self.fifo)
        os.mkfifo(self.fifo)
        self.log = os.path.join(WORK, "klippy.log")
        k = os.path.expanduser(args.klipper)
        # Keep the batch input open (EOF ends klippy); klippy opens it
        # without blocking, so it must have a writer before it starts
        self.writer = os.open(self.fifo, os.O_RDWR)
        self.proc = subprocess.Popen(
            [args.python, os.path.join(k, "klippy", "klippy.py"), self.cfg,
             "-i", self.fifo, "-o", os.path.join(WORK, "out.serial"),
             "-d", os.path.join(k, "out", "klipper.dict"), "-l", self.log],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.api = Api(self.writer)
        status = os.path.join(WORK, "status.json")
        for i in range(100):
            if os.path.exists(status) or self.proc.poll() is not None:
                break
            time.sleep(.1)
        self.state = "ready" if os.path.exists(status) else "not ready"
    def stop(self):
        try:
            os.close(self.writer)
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

def wait_for(fn, timeout, step=.25):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()

def variables():
    text = open(os.path.join(WORK, "variables.cfg")).read()
    m = re.search(r"^ifs_materials = (.*)$", text, re.M)
    return eval(m.group(1)) if m else None

PLUGIN_CFG = """
[kiln_ifsjacker]

[temperature_sensor ifs_box]
sensor_type: ifs_jacker
peripheral: 0
min_temp: -50
max_temp: 120

[kiln_ifsjacker_fan rear]
peripheral: 1
"""

def run_jacker(args):
    print("\n== Kiln + IFS Jacker emulator: 2 IFS units, 8 channels ==")
    emu = Emulator(args, "jacker", ["--units", "2", "--loaded", "1-8",
                                    "--peripheral", "emu_temp",
                                    "--peripheral", "emu_pwm"])
    k = Klippy(args, PLUGIN_CFG)
    api = k.api
    try:
        check("klippy ready", k.state == "ready", k.state)
        api.gcode("SET_KINEMATIC_POSITION X=100 Y=100 Z=10")
        st = wait_for(lambda: (lambda s: s if s['ifs']['connected'] else
                               None)(api.query("ifs")), 15)['ifs']
        check("8 channels from the Jacker's channel_count",
              st['channel_count'] == 8 and st['board_channels'] == 8,
              "channel_count %s, board_channels %s" % (
                  st['channel_count'], st['board_channels']))
        check("loaded_channels 1-8",
              st['loaded_channels'] == list(range(1, 9)),
              st['loaded_channels'])
        check("tool map T0-T7", sorted(st['tool_map'], key=int)
              == [str(t) for t in range(8)], st['tool_map'])
        jk = wait_for(lambda: api.query("kiln_ifsjacker")["kiln_ifsjacker"]
                      if api.query("kiln_ifsjacker")["kiln_ifsjacker"]
                      ["present"] else None, 20)
        check("plugin detects the Jacker (Z2)", jk and jk['present']
              and jk['version'] == "3.2.1" and jk['units'] == 2,
              jk and jk['message'])
        names = [p.get('name') for p in (jk or {}).get('peripherals', [])]
        check("plugin reads peripheral names (Z3)",
              names == ["Emulated Thermistor", "Emulated PWM Output"], names)
        emu.unit_log()

        # A tool on the second unit
        ok, err, out = api.gcode("T5")
        st = api.query("ifs")['ifs']
        log = emu.unit_log()
        check("T5 loads channel 6", ok and st['channel'] == 6,
              err or "channel %s" % st['channel'])
        check("channel 6 = unit 2 channel 2 on the wire",
              "F24 C2" in log['ifs2'] and "F10 C2 L1000 S1200" in log['ifs2']
              and "F39 C2" in log['ifs2'] and "F23 C2" in log['ifs2'],
              "ifs2: %s" % (log['ifs2'],))
        check("Jacker parks unit 1 (F18) on the select",
              "F18" in log['ifs1'], "ifs1: %s" % (log['ifs1'],))

        # Activity on unit 2 while T6 swaps 6 -> 7
        seen = set()
        stop = threading.Event()
        def sample():
            while not stop.is_set():
                s = api.query("ifs")['ifs']
                seen.add((s['activity'], s['activity_channel'], s['state']))
                time.sleep(.05)
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        ok, err, out = api.gcode("T6")
        stop.set()
        th.join()
        acts = {(a, c) for a, c, s in seen if a in ("loading", "unloading")}
        check("T6 swaps 6 -> 7", ok and api.query("ifs")['ifs']['channel']
              == 7, err)
        check("activity names the right channels on unit 2",
              ("unloading", 6) in acts and ("loading", 7) in acts
              and not acts - {("unloading", 6), ("loading", 7)},
              sorted(acts))
        raw = sorted({s for a, c, s in seen if a in ("loading", "unloading")})
        print("      raw FFS_state seen during the swap: %s" % (raw,))

        # Tool map across units
        ok, err, out = api.gcode("IFS_MAP_TOOL TOOL=6 SLOT=3\nT6")
        check("T6 mapped to slot 3 (unit 1)", ok and
              api.query("ifs")['ifs']['channel'] == 3, err)
        ok, err, out = api.gcode("T8")
        check("T8 refused (8 channels)", not ok and "8 channels" in err, err)
        ok, err, out = api.gcode("IFS_MAP_TOOL TOOL=8 SLOT=1")
        check("IFS_MAP_TOOL TOOL=8 refused", not ok, err)
        ok, err, out = api.gcode("IFS_SET_MATERIAL SLOT=9 TYPE=PLA")
        check("SLOT=9 refused", not ok, err)
        emu.unit_log()

        # Lengths with decimals (tuned values) reach the units
        ok, err, out = api.gcode("IFS_RETRACT CHANNEL=5 LENGTH=152.3 "
                                 "SPEED=600.5")
        log = emu.unit_log()
        check("decimal L/S sent as integers through the Jacker",
              ok and "F11 C1 L152 S600" in log['ifs2'],
              err or "ifs2: %s" % (log['ifs2'],))

        # Slot materials for 8 slots
        ok, err, out = api.gcode("IFS_SET_MATERIAL SLOT=7 TYPE=PETG "
                                 "COLOR=FF0000")
        mats = variables()
        slots = api.query("ifs_materials")['ifs_materials']['slots']
        check("slot 7 material saved (8 entries in ifs_materials)",
              ok and mats and len(mats) == 8
              and mats[6]['type'] == "PETG", mats and len(mats))
        check("printer.ifs_materials has slots 1-8",
              sorted(slots, key=int) == [str(c) for c in range(1, 9)]
              and slots['7']['color'] == "#FF0000", list(slots))

        # New spool on channel 8 (unit 2 channel 4): pre-feed
        emu.control("remove 8")
        emu.control("insert 8")
        def prefed():
            s = api.query("ifs")['ifs']
            return not s['busy'] and 8 not in s['pending_insert_channels'] \
                and s['inserted'][7] is False
        seen8 = wait_for(lambda: 8 in api.query("ifs")['ifs']
                         ['pending_insert_channels'], 10)
        done = wait_for(prefed, 40)
        log = emu.unit_log()
        idle = api.query("idle_timeout")["idle_timeout"]
        check("new spool on channel 8 is pre-fed (unit 2 channel 4)",
              done and "F10 C4 L600 S1200" in log['ifs2']
              and "F23 C4" in log['ifs2'] and "F39 C4" in log['ifs2'],
              "Kiln saw the insert: %s; idle_timeout %s; ifs2: %s" % (
                  bool(seen8), idle and idle.get('state'),
                  [c for c in log['ifs2'] if c != "F21"]))

        # Eject channel 8
        ok, err, out = api.gcode("IFS_EJECT SLOT=8")
        time.sleep(2.)
        st = api.query("ifs")['ifs']
        check("IFS_EJECT SLOT=8 empties channel 8",
              ok and 8 not in st['loaded_channels'],
              err or st['loaded_channels'])

        # Peripherals: temperature sensor and fan
        temp = wait_for(lambda: (lambda t: t if t > 1. else None)(
            api.query("temperature_sensor ifs_box")
            ["temperature_sensor ifs_box"]["temperature"]), 15)
        check("temperature_sensor ifs_box reads the Jacker thermistor",
              temp and 27. < temp < 33., temp)
        ok, err, out = api.gcode("SET_FAN_SPEED FAN=rear SPEED=0.5")
        fan = api.query("fan_generic rear")["fan_generic rear"]
        check("SET_FAN_SPEED FAN=rear sets the Jacker PWM output",
              ok and abs(fan['speed'] - .5) < .01, err or fan)
        ok, err, out = api.gcode("IFS_JACKER_PERIPHERALS")
        check("IFS_JACKER_PERIPHERALS lists both", ok and any(
            "Emulated Thermistor" in o and "power=32768" in o for o in out),
            err or out)

        # Auto-refill across units: slot 2 runs out, slot 6 matches
        api.gcode("IFS_SET_MATERIAL SLOT=2 TYPE=PLA COLOR=00FF00\n"
                  "IFS_SET_MATERIAL SLOT=6 TYPE=PLA COLOR=00FF00\n"
                  "IFS_AUTO_REFILL ENABLE=1 MATCH=exact\nIFS_MAP_TOOL RESET=1")
        gdir = os.path.join(WORK, "gcodes")
        # Short sleeps, so the file lets go of the G-code lock between
        # lines as a print does
        open(os.path.join(gdir, "refill.gcode"), "w").write(
            "T1\n" + "TEST_SLEEP S=0.5\n" * 120 + "M117 done\n")
        api.gcode("SDCARD_PRINT_FILE FILENAME=refill.gcode")
        loaded2 = wait_for(lambda: api.query("ifs")['ifs']['channel'] == 2,
                           40)
        check("print loads slot 2 (T1)", loaded2)
        emu.control("remove 2")
        t0 = time.time()
        refilled = wait_for(lambda: "continued" in (
            api.query("ifs")['ifs']['refill'] or ""), 90)
        st = api.query("ifs", "print_stats")
        check("auto-refill: slot 2 ran out, print goes on with slot 6",
              refilled and st['ifs']['channel'] == 6
              and st['ifs']['tool_map']['1'] == 6
              and st['print_stats']['state'] == "printing",
              "%s; print %s; %.0f s" % (st['ifs']['refill'],
                                        st['print_stats']['state'],
                                        time.time() - t0))
        wait_for(lambda: api.query("print_stats")['print_stats']['state']
                 != "printing", 90)
    finally:
        k.stop()
        emu.stop()

def run_stock(args):
    print("\n== Kiln + one plain IFS (fake_ifs.py): unchanged behaviour ==")
    emu = Emulator(args, "stock")
    k = Klippy(args, PLUGIN_CFG)
    api = k.api
    try:
        api.gcode("SET_KINEMATIC_POSITION X=100 Y=100 Z=10")
        st = wait_for(lambda: (lambda s: s if s['ifs']['connected'] else
                               None)(api.query("ifs")), 15)['ifs']
        check("4 channels, no board count",
              st['channel_count'] == 4 and st['board_channels'] is None,
              st['channel_count'])
        check("tool map T0-T3", sorted(st['tool_map'], key=int)
              == ["0", "1", "2", "3"], st['tool_map'])
        ok, err, out = api.gcode("T4")
        check("T4 refused (4 channels)", not ok and "4 channels" in err, err)
        ok, err, out = api.gcode("IFS_RETRACT CHANNEL=1 LENGTH=152.3")
        time.sleep(6.)          # the plugin's detection window
        cmds = open(emu.cmds).read().split("\n")
        check("decimal lengths unchanged for a plain IFS",
              ok and "F11 C1 L152.3 S600" in cmds, err)
        check("plugin never sends Z2 to a plain IFS",
              not any(c.startswith("Z") for c in cmds),
              [c for c in cmds if c.startswith("Z")])
        jk = api.query("kiln_ifsjacker")["kiln_ifsjacker"]
        check("plugin: not present", not jk['present'], jk['message'])
        ok, err, out = api.gcode("T1")
        check("T1 swap works", ok and api.query("ifs")['ifs']['channel'] == 2,
              err)
    finally:
        k.stop()
        emu.stop()

def run_16(args):
    print("\n== Kiln + IFS Jacker emulator: 4 IFS units, 16 channels ==")
    emu = Emulator(args, "jacker", ["--units", "4", "--loaded", "1-16"])
    k = Klippy(args, PLUGIN_CFG)
    api = k.api
    try:
        api.gcode("SET_KINEMATIC_POSITION X=100 Y=100 Z=10")
        st = wait_for(lambda: (lambda s: s if s['ifs']['connected'] else
                               None)(api.query("ifs")), 15)['ifs']
        check("16 channels", st['channel_count'] == 16
              and st['loaded_channels'] == list(range(1, 17)),
              st['channel_count'])
        emu.unit_log()
        seen = set()
        stop = threading.Event()
        def sample():
            while not stop.is_set():
                s = api.query("ifs")['ifs']
                seen.add((s['activity'], s['activity_channel']))
                time.sleep(.05)
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        ok, err, out = api.gcode("T15")
        stop.set()
        th.join()
        log = emu.unit_log()
        check("T15 loads channel 16 = unit 4 channel 4",
              ok and api.query("ifs")['ifs']['channel'] == 16
              and "F24 C4" in log['ifs4'] and "F23 C4" in log['ifs4'],
              err or "ifs4: %s" % (log['ifs4'],))
        check("loading activity on channel 16", ("loading", 16) in seen,
              sorted(x for x in seen if x[1]))
        ok, err, out = api.gcode("T16")
        check("T16 has no command (16 channels max)",
              any("Unknown command" in o for o in out), out)
    finally:
        k.stop()
        emu.stop()

def run_no_count(args):
    print("\n== Jacker with include_channel_count_in_status=false ==")
    for channels, want in (("auto", 4), ("8", 8)):
        emu = Emulator(args, "jacker", ["--units", "2", "--loaded", "1-8",
                                        "--no-channel-count"])
        k = Klippy(args, "\n[ifs]\nchannels: %s\n" % (channels,))
        api = k.api
        try:
            api.gcode("SET_KINEMATIC_POSITION X=100 Y=100 Z=10")
            st = wait_for(lambda: (lambda s: s if s['ifs']['connected'] else
                                   None)(api.query("ifs")), 15)['ifs']
            check("channels: %s -> %d channels" % (channels, want),
                  st['channel_count'] == want, st['channel_count'])
            if want == 8:
                emu.unit_log()
                ok, err, out = api.gcode("IFS_RETRACT CHANNEL=6 LENGTH=40.7")
                log = emu.unit_log()
                check("channels: 8 still sends integer L/S",
                      ok and "F11 C2 L41 S600" in log['ifs2'],
                      err or log['ifs2'])
        finally:
            k.stop()
            emu.stop()

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--klipper", required=True)
    ap.add_argument("--kiln", required=True, help="kiln-os checkout")
    ap.add_argument("--jacker-src", required=True)
    ap.add_argument("--python", default=sys.executable,
                    help="Python with Klipper's requirements")
    ap.add_argument("--speed", type=float, default=20.)
    ap.add_argument("--only",
                    choices=("jacker", "sixteen", "stock", "nocount"))
    args = ap.parse_args()
    args.jacker_src = os.path.expanduser(args.jacker_src)
    extras = os.path.join(os.path.expanduser(args.klipper), "klippy",
                          "extras")
    links = {"ifs.py": os.path.join(os.path.expanduser(args.kiln), "klipper",
                                    "extras", "ifs.py"),
             "kiln_test_probe.py": os.path.join(HERE, "kiln_test_probe.py")}
    for name in ("kiln_ifsjacker.py", "kiln_ifsjacker_fan.py"):
        links[name] = os.path.join(PLUGIN, "extras", name)
    for name, target in links.items():
        path = os.path.join(extras, name)
        if os.path.lexists(path):
            os.unlink(path)
        os.symlink(target, path)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(os.path.join(WORK, "gcodes"))
    try:
        for name, fn in (("jacker", run_jacker), ("sixteen", run_16),
                         ("stock", run_stock), ("nocount", run_no_count)):
            if args.only in (None, name):
                open(os.path.join(WORK, "variables.cfg"), "w").close()
                fn(args)
    finally:
        for name in links:
            os.unlink(os.path.join(extras, name))
    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
