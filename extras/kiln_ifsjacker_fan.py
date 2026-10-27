# An IFS Jacker PWM peripheral as a Klipper fan ([kiln_ifsjacker_fan name])
#
#   [kiln_ifsjacker_fan rear]
#   peripheral: 1
#
# SET_FAN_SPEED FAN=rear SPEED=0.5 sends Z5 C<peripheral> F3 L<0-65535>
# (the Jacker's pwm_output peripheral); printer['fan_generic rear'].speed is
# its reported power. Needs [kiln_ifsjacker].
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import re

FULL = 65535

class KilnIfsJackerFan:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.index = config.getint('peripheral', minval=0)
        self.jacker = self.printer.load_object(config, 'kiln_ifsjacker')
        self.printer.add_object("fan_generic " + self.name, self)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("SET_FAN_SPEED", "FAN", self.name,
                                   self.cmd_SET_FAN_SPEED,
                                   desc="Set the IFS Jacker fan's speed")
    def cmd_SET_FAN_SPEED(self, gcmd):
        speed = gcmd.get_float('SPEED', 0., minval=0., maxval=1.)
        self.jacker._need_present(gcmd, 3.0)
        reply = self.jacker._send(gcmd, "Z5 C%d F3 L%d" % (
            self.index, int(round(speed * FULL))))
        # 'Z5 ok. F3 peripheral ok. power: <n>' (no p<n>_ prefix here)
        m = re.search(r"power:\s*(\d+)", reply)
        if m:
            self.jacker._peripheral(self.index)['power'] = int(m.group(1))
    def get_status(self, eventtime):
        power = self.jacker.field(self.index, 'power')
        speed = power / FULL if isinstance(power, (int, float)) else 0.
        return {'speed': round(speed, 3), 'rpm': None}

def load_config_prefix(config):
    return KilnIfsJackerFan(config)
