"""Emulator peripheral: a temperature sensor that drifts around 30 C,
reporting as the Jacker's thermistor peripheral does (p<n>_temperature)."""
import math, time
from peripheral import IJ_Peripheral

class IJP_Emu_Temp(IJ_Peripheral):
    def __init__(self):
        super().__init__()
        self.identifier = 'Emulated Thermistor'
    def value(self):
        return round(30. + 2. * math.sin(time.monotonic() / 10.), 2)
    def handle_command(self, f=0, l=0, s=0, params=[]):
        if f == 2:
            return "F2 peripheral ok. temperature: %s" % (self.value(),)
        return super().handle_command(f, l, s, params)
    def get_status_info(self):
        return '%s_temperature: %s' % (self.short_identifier, self.value())
    @staticmethod
    def create(config_data, all_comms):
        return IJP_Emu_Temp()
