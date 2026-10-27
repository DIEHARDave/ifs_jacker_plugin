# Test-only Klipper module for test_kiln_jacker.py ([kiln_test_probe]).
#
# Klipper's batch mode (-i) has no API socket and exits on the first G-code
# error. This module gives the test what the API would:
#   - status: every `interval` s the status of `objects` goes to
#     <dir>/status.json (written whole, then renamed)
#   - TEST_RUN ID=<n> CMD="<g-code>": runs it and appends
#     {"id", "ok", "error", "output"} to <dir>/results.jsonl, so an error is
#     a result instead of the end of klippy
#   - idle_timeout reads Ready while no file prints (batch mode never idles)
#   - TEST_SLEEP S=<seconds>: a real-time wait (G4 only moves print time in
#     batch mode), for prints that must last while the test acts
import json
import os

class KilnTestProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.dir = config.get('dir')
        self.objects = [o.strip() for o in config.get('objects').split(',')]
        self.interval = config.getfloat('interval', .05)
        self.capture = None
        self.gcode.register_output_handler(self._output)
        self.gcode.register_command("TEST_RUN", self.cmd_TEST_RUN)
        self.gcode.register_command("TEST_SLEEP", self.cmd_TEST_SLEEP)
        self.printer.register_event_handler("klippy:ready", self._ready)
    def _ready(self):
        self.reactor.register_timer(self._dump, self.reactor.NOW)
    def _dump(self, eventtime):
        # Batch mode never lets the toolhead go idle, so idle_timeout says
        # "Printing" for ever and ifs.py holds back its pre-feed: say Ready
        # while no file prints
        idle = self.printer.lookup_object('idle_timeout', None)
        ps = self.printer.lookup_object('print_stats', None)
        if idle is not None and ps is not None and ps.get_status(
                eventtime)['state'] not in ("printing", "paused"):
            idle.state = "Ready"
        status = {}
        for name in self.objects:
            obj = self.printer.lookup_object(name, None)
            if obj is not None and hasattr(obj, 'get_status'):
                try:
                    status[name] = obj.get_status(eventtime)
                except Exception as e:
                    status[name] = {'_error': str(e)}
        path = os.path.join(self.dir, "status.json")
        with open(path + ".tmp", "w") as f:
            json.dump({'time': eventtime, 'status': status}, f, default=str)
        os.replace(path + ".tmp", path)
        return eventtime + self.interval
    def _output(self, msg):
        if self.capture is not None:
            self.capture.append(msg)
    def cmd_TEST_RUN(self, gcmd):
        rid = gcmd.get('ID')
        cmd = gcmd.get('CMD')
        prev, self.capture = self.capture, []
        ok, error = True, ""
        # An error reported in batch mode ends klippy (_respond_error)
        fileinput, self.gcode.is_fileinput = self.gcode.is_fileinput, False
        try:
            self.gcode.run_script_from_command(cmd)
        except Exception as e:
            ok, error = False, str(e)
        finally:
            self.gcode.is_fileinput = fileinput
        out, self.capture = self.capture, prev
        with open(os.path.join(self.dir, "results.jsonl"), "a") as f:
            f.write(json.dumps({'id': rid, 'ok': ok, 'error': error,
                                'output': out}) + "\n")
    def cmd_TEST_SLEEP(self, gcmd):
        seconds = gcmd.get_float('S', above=0.)
        self.reactor.pause(self.reactor.monotonic() + seconds)

def load_config(config):
    return KilnTestProbe(config)
