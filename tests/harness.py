"""Virtual-clock harness: runs the WiFiManager supervisor generator without sleeping."""
import _setup  # noqa: F401

import machine
import network
from wifimanager import compat
import wifimanager.portal as portal_module
from wifimanager import WiFiManager
from _util import rmtree, tmpdir

_orig = (compat.ticks_ms, compat.sleep_ms_blocking, compat.run_for, portal_module.Portal)
W = network.WORLD


class StubPortal:
    """Stands in for the real portal in supervisor tests (the real one has its own tests)."""
    instances = []

    def __init__(self, mgr):
        self.mgr = mgr
        self.ap_ssid = "stub-ap"
        self.ap_ip = "192.168.4.1"
        self.last_activity = compat.ticks_ms()
        self.started = self.stopped = False
        self.n_clients = 0
        StubPortal.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def clients(self):
        return self.n_clients

    def touch(self):
        self.last_activity = compat.ticks_ms()


class Sim:
    def __init__(self):
        self.t = 100000
        compat.ticks_ms = lambda: self.t
        compat.sleep_ms_blocking = self.advance
        compat.run_for = self.advance
        portal_module.Portal = StubPortal
        StubPortal.instances = []
        W.reset()
        W.add_ap("Neighbor", "neighborpw", rssi=-90)   # scans are never empty (an empty scan = "scan failed")
        machine.Pin.levels.clear()
        self.dir = tmpdir()
        self.events = []

    def close(self):
        compat.ticks_ms, compat.sleep_ms_blocking, compat.run_for, portal_module.Portal = _orig
        rmtree(self.dir)

    def advance(self, ms):
        self.t += ms

    def path(self, name="wifi_config.json"):
        return self.dir + "/" + name

    def make(self, networks=None, **kw):
        kw.setdefault("config_file", self.path())
        kw.setdefault("log_level", "DEBUG")
        kw.setdefault("check_interval", 5)
        kw.setdefault("on_state", lambda old, new: self.events.append((self.t, new)))
        wm = WiFiManager(networks, **kw)
        wm.log.console = False
        return wm

    def run(self, wm, until=None, ms=600000):
        """Drive the supervisor on virtual time until `until()` is true or `ms` elapsed."""
        wm._boot()
        end = self.t + ms
        while self.t < end:
            step = wm._next()
            if step is None:
                return False
            wm._poll_button()
            self.t += step
            wm._kick = False
            if until is not None and until():
                return True
        return until is None

    def states(self):
        out = []
        for _, s in self.events:
            if not out or out[-1] != s:
                out.append(s)
        return out

    @property
    def portal(self):
        return StubPortal.instances[-1] if StubPortal.instances else None
