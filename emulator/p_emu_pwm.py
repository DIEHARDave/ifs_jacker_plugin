"""Emulator peripheral: a PWM output (fan), answering as the Jacker's
pwm_output peripheral does (F3 L<0-65535> sets it, p<n>_power)."""
from peripheral import IJ_Peripheral

class IJP_Emu_Pwm(IJ_Peripheral):
    def __init__(self):
        super().__init__()
        self.identifier = 'Emulated PWM Output'
        self.power = 0
    def handle_command(self, f=0, l=0, s=0, params=[]):
        if f == 2:
            return "F2 peripheral ok. power: %d frequency: 25000" % (self.power,)
        if f == 3:
            self.power = max(0, min(65535, l))
            return "F3 peripheral ok. power: %d" % (self.power,)
        return super().handle_command(f, l, s, params)
    def get_status_info(self):
        return '%s_power: %d' % (self.short_identifier, self.power)
    @staticmethod
    def create(config_data, all_comms):
        return IJP_Emu_Pwm()
