# IFS Jacker support for Kiln OS ([kiln_ifsjacker])
#
# The IFS Jacker (github.com/ninjamida/ifs-jacker) chains two to four IFS
# units on the printer's IFS link. Kiln's ifs.py drives its channels itself
# (channels: auto reads the channel_count the Jacker adds to F13); this
# plugin adds what is Jacker-only: its identity and firmware version (Z2),
# its peripherals (temperature sensors, fans, ...: Z3, Z4, Z5 and the
# p<n>_<field> values it adds to F13) and the "ifs_jacker" temperature
# sensor type. Commands and status names follow ninjamida's Z-Mod plugin
# (github.com/ninjamida/ifs_jacker_plugin); no code from it is used.
#
# Kiln OS loads a plugin's extras/*.py while the plugin is enabled.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import re

# Jacker firmware: 3.0 has the F13 peripheral fields, 3.2 Z7
MIN_VERSION = 3.0
# Z2 tries before giving up until the IFS link reconnects
DETECT_TRIES = 3
DETECT_INTERVAL = 5.
# 'p<n>_<field>: <value>' in F13 and Z4 replies
PERIPHERAL_RE = re.compile(r'(?:\s|^)p(\d+)_(\S+?):\s*(\S+)')

def parse_value(text):
    for conv in (int, float):
        try:
            return conv(text)
        except ValueError:
            pass
    return text.strip('"')

def parse_z2(reply):
    """Z2 reply -> dict (software, version, author, mmu_count,
    channel_count, peripheral_count, ...)."""
    info = {}
    for key, quoted, plain in re.findall(
            r'([a-z_]+):\s*(?:"([^"]*)"|(\S+))', reply):
        info[key] = quoted if quoted else parse_value(plain)
    return info

def version_number(text):
    """'3.2.1' -> 3.2 (major.minor), 0. if unreadable."""
    try:
        return float('.'.join(str(text).split('.')[:2]))
    except ValueError:
        return 0.

class KilnIfsJacker:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.ifs = None
        self.info = {}
        self.present = False
        self.message = ""
        self.tries = 0
        self.peripherals = []     # per index: {'name': .., field: value}
        self.seen_reply = None
        self.printer.register_event_handler("klippy:ready", self._ready)
        for cmd in ("IFS_JACKER_STATUS", "IFS_JACKER_PERIPHERALS",
                    "IFS_JACKER_PERIPHERAL", "IFS_JACKER_CHECK"):
            self.gcode.register_command(cmd, getattr(self, "cmd_" + cmd),
                                        desc=getattr(self, "cmd_%s_help" % cmd))
        # A Jacker peripheral as a Klipper temperature sensor:
        #   [temperature_sensor ifs_box] sensor_type: ifs_jacker
        #   peripheral: 0  [field: temperature]
        pheaters = self.printer.load_object(config, 'heaters')
        pheaters.add_sensor_factory(
            'ifs_jacker', lambda cfg: JackerTemperature(cfg, self))
    def _ready(self):
        self.ifs = self.printer.lookup_object('ifs', None)
        if self.ifs is None:
            self.message = "no [ifs] section"
            return
        if not hasattr(self.ifs, 'board_channels'):
            # Kiln's ifs.py before multi-unit support: 4 channels only
            self.message = ("this Kiln OS drives 4 IFS channels only; the "
                            "IFS Jacker needs ifs.py with the channels "
                            "option (see README)")
            logging.warning("kiln_ifsjacker: %s", self.message)
            return
        self.reactor.register_timer(self._watch, self.reactor.NOW)
    # Detection
    def _link_up(self):
        return self.ifs.fd is not None and self.ifs.board_channels is not None
    def _watch(self, eventtime):
        """Detect the Jacker once its link is up and reports channel_count
        (a plain IFS doesn't, so it is never sent Z2); forget it when the
        link drops. Then follow the peripheral fields of each F13."""
        if not self._link_up():
            if self.present or self.tries:
                logging.info("kiln_ifsjacker: IFS link down or no "
                             "channel_count, Jacker state cleared")
            self.present = False
            self.tries = 0
            self.info = {}
            return eventtime + DETECT_INTERVAL
        if not self.present and self.tries < DETECT_TRIES:
            if not self.ifs.busy and not self.ifs.mutex.test():
                self.tries += 1
                self._detect()
            return eventtime + DETECT_INTERVAL
        if self.present:
            self._follow_status()
        return eventtime + 1.
    def _request(self, cmd):
        """One command over Kiln's IFS link (its lock; no controller reset
        on failure: these commands don't move anything)."""
        with self.ifs.mutex:
            reply = self.ifs._request(cmd)
            # An F13 from before this command is older than its reply (a
            # Z5 that set a fan): don't let it overwrite the new values
            self.seen_reply = self.ifs.last_reply
            return reply
    def _detect(self):
        try:
            reply = self._request("Z2")
        except Exception as e:
            self.message = "no Z2 reply (%s)" % (e,)
            logging.info("kiln_ifsjacker: %s", self.message)
            return False
        info = parse_z2(reply)
        if not info.get('software', '').startswith("IFS Jacker"):
            self.message = "Z2: not an IFS Jacker (%s)" % (reply,)
            return False
        self.info = info
        self.present = True
        self.tries = 0
        version = version_number(info.get('version'))
        self.message = "IFS Jacker %s: %s units, %s channels" % (
            info.get('version'), info.get('mmu_count', '?'),
            info.get('channel_count', '?'))
        if version < MIN_VERSION:
            self.message += ("; firmware %s is too old for peripherals "
                             "(3.0 or newer)" % (info.get('version'),))
        logging.info("kiln_ifsjacker: %s", self.message)
        count = info.get('peripheral_count') or 0
        self.peripherals = [{} for i in range(count)]
        if count and version >= MIN_VERSION:
            self._read_names()
        return True
    def _read_names(self):
        try:
            reply = self._request("Z3")
        except Exception as e:
            logging.info("kiln_ifsjacker: Z3 failed: %s", e)
            return
        for idx, name in re.findall(r'peripheral_(\d+):\s*"([^"]*)"', reply):
            self._peripheral(int(idx))['name'] = name
    def _peripheral(self, idx):
        while len(self.peripherals) <= idx:
            self.peripherals.append({})
        return self.peripherals[idx]
    def _take_fields(self, reply):
        for idx, field, value in PERIPHERAL_RE.findall(reply):
            self._peripheral(int(idx))[field] = parse_value(value)
    def _follow_status(self):
        reply = self.ifs.last_reply
        if reply and reply is not self.seen_reply:
            self.seen_reply = reply
            self._take_fields(reply)
    def field(self, idx, name):
        if not self.present or idx >= len(self.peripherals):
            return None
        return self.peripherals[idx].get(name)
    def get_status(self, eventtime):
        return {
            'present': self.present,
            'version': self.info.get('version'),
            'units': self.info.get('mmu_count'),
            'channel_count': self.info.get('channel_count'),
            'peripherals': [dict(p) for p in self.peripherals],
            'message': self.message,
        }
    # Commands
    def _need_present(self, gcmd, min_version=0.):
        if self.ifs is None or self.ifs.fd is None:
            raise gcmd.error("IFS Jacker: the IFS link is down")
        if not self.present:
            raise gcmd.error("IFS Jacker: not detected (%s)"
                             % (self.message or "no channel_count in F13"))
        if version_number(self.info.get('version')) < min_version:
            raise gcmd.error("IFS Jacker: needs firmware %.1f or newer"
                             % (min_version,))
    def _send(self, gcmd, cmd):
        try:
            return self._request(cmd)
        except Exception as e:
            raise gcmd.error("IFS Jacker: %s: %s" % (cmd, e))
    cmd_IFS_JACKER_CHECK_help = "Detect the IFS Jacker again (Z2)"
    def cmd_IFS_JACKER_CHECK(self, gcmd):
        if self.ifs is None or not self._link_up():
            raise gcmd.error("IFS Jacker: the IFS link is down or reports "
                             "no channel_count")
        self.present = False
        self.tries = DETECT_TRIES     # the watch timer leaves it to us
        self._detect()
        gcmd.respond_info("IFS Jacker: " + self.message)
    cmd_IFS_JACKER_STATUS_help = "Show the IFS Jacker's Z2 information"
    def cmd_IFS_JACKER_STATUS(self, gcmd):
        self._need_present(gcmd)
        gcmd.respond_info(self._send(gcmd, "Z2"))
    cmd_IFS_JACKER_PERIPHERALS_help = ("List the IFS Jacker's peripherals and "
                                       "their values (Z3, Z4)")
    def cmd_IFS_JACKER_PERIPHERALS(self, gcmd):
        self._need_present(gcmd, MIN_VERSION)
        self._read_names()
        self._take_fields(self._send(gcmd, "Z4"))
        lines = ["p%d %s: %s" % (i, p.get('name', '?'), " ".join(
            "%s=%s" % (k, v) for k, v in sorted(p.items()) if k != 'name')
            or "-") for i, p in enumerate(self.peripherals)]
        gcmd.respond_info("\n".join(lines) or "IFS Jacker: no peripherals")
    cmd_IFS_JACKER_PERIPHERAL_help = ("Send a command to an IFS Jacker "
                                      "peripheral (Z5): PERIPHERAL= COMMAND= "
                                      "[PARAM1=] [PARAM2=]")
    def cmd_IFS_JACKER_PERIPHERAL(self, gcmd):
        self._need_present(gcmd, MIN_VERSION)
        idx = gcmd.get_int('PERIPHERAL', minval=0)
        command = gcmd.get_int('COMMAND', 2, minval=1)
        p1 = gcmd.get_int('PARAM1', 0)
        p2 = gcmd.get_int('PARAM2', 0)
        reply = self._send(gcmd, "Z5 C%d F%d L%d S%d" % (idx, command, p1, p2))
        self._take_fields(reply)
        gcmd.respond_info(reply)

class JackerTemperature:
    """sensor_type: ifs_jacker: a peripheral's value (field, default
    temperature) times factor, read from the F13 replies Kiln's status
    poll gets (every [ifs] status_interval)."""
    def __init__(self, config, jacker):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.jacker = jacker
        self.index = config.getint('peripheral', minval=0)
        self.field = config.get('field', 'temperature')
        self.factor = config.getfloat('factor', 1.)
        self.report_time = config.getfloat('report_time', 1., above=0.)
        self.temp = 0.
        self.min_temp = self.max_temp = None
        self._callback = None
        self.printer.register_event_handler("klippy:ready", self._ready)
    def _ready(self):
        self.reactor.register_timer(self._sample, self.reactor.NOW)
    def _sample(self, eventtime):
        value = self.jacker.field(self.index, self.field)
        if isinstance(value, (int, float)):
            self.temp = float(value) * self.factor
        mcu = self.printer.lookup_object('mcu')
        if self._callback is not None:
            self._callback(mcu.estimated_print_time(eventtime), self.temp)
        return eventtime + self.report_time
    def setup_minmax(self, min_temp, max_temp):
        self.min_temp, self.max_temp = min_temp, max_temp
    def setup_callback(self, cb):
        self._callback = cb
    def get_report_time_delta(self):
        return self.report_time

def load_config(config):
    return KilnIfsJacker(config)
