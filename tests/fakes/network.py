"""Fake `network` module for host tests: a scriptable radio environment.

Use `WORLD` to define access points and to flip them up/down. All timing goes through
wifimanager.compat.ticks_ms(), so tests can run on a virtual clock.
"""
STA_IF = 0
AP_IF = 1
AUTH_OPEN = 0
AUTH_WPA2_PSK = 3
STAT_IDLE = 1000
STAT_CONNECTING = 1001
STAT_GOT_IP = 1010
STAT_WRONG_PASSWORD = 202
STAT_NO_AP_FOUND = 201
STAT_BEACON_TIMEOUT = 200
STAT_ASSOC_FAIL = 203
STAT_HANDSHAKE_TIMEOUT = 204


def _now():
    from wifimanager import compat
    return compat.ticks_ms()


class World:
    def __init__(self):
        self.reset()

    def reset(self):
        self.aps = {}
        self.scan_error = None
        self.ap_clients = []
        self.hostname = None
        self.driver_give_up_ms = 8000   # the fake ESP-IDF driver stops retrying after this long
        self.connect_calls = []   # (ssid, password) of every WLAN.connect()
        self.scan_calls = 0
        self.ap_config = {}
        self.sta_config = {}

    def add_ap(self, ssid, password="", rssi=-60, delay_ms=1500, hidden=False, channel=6):
        self.aps[ssid] = {"password": password, "rssi": rssi, "delay": delay_ms, "hidden": hidden,
                          "channel": channel, "up": True, "up_since": _now(), "down_at": None}

    def set_up(self, ssid, up):
        ap = self.aps[ssid]
        if up and not ap["up"]:
            ap["up_since"] = _now()
        if not up and ap["up"]:
            ap["down_at"] = _now()
        ap["up"] = up


WORLD = World()


class WLAN:
    def __init__(self, iface):
        self.iface = iface
        self._active = False
        self._target = None      # (ssid, password, t_connect)
        self._ip = ("0.0.0.0", "0.0.0.0", "0.0.0.0", "0.0.0.0")

    def active(self, flag=None):
        if flag is None:
            return self._active
        self._active = bool(flag)
        if not flag:
            self._target = None

    def config(self, *args, **kw):
        store = WORLD.ap_config if self.iface == AP_IF else WORLD.sta_config
        if args:
            key = args[0]
            if key == "mac":
                return b"\x24\x0a\xc4\x12\xab\xcd"
            if key == "essid":
                return self._target[0] if self._target else store.get("essid", "")
            return store.get(key)
        store.update(kw)

    def ifconfig(self, cfg=None):
        if cfg is not None:
            self._ip = tuple(cfg)
            return
        if self.iface == AP_IF:
            return self._ip if self._ip[0] != "0.0.0.0" else ("192.168.4.1", "255.255.255.0", "192.168.4.1", "8.8.8.8")
        if self.isconnected():
            return ("192.168.1.50", "255.255.255.0", "192.168.1.1", "192.168.1.1")
        return ("0.0.0.0", "0.0.0.0", "0.0.0.0", "0.0.0.0")

    def connect(self, ssid, password=""):
        WORLD.connect_calls.append((ssid, password))
        self._target = (ssid, password, _now())

    def disconnect(self):
        self._target = None

    def _ap(self):
        if not self._target:
            return None
        return WORLD.aps.get(self._target[0])

    def isconnected(self):
        if self.iface == AP_IF or not self._active or not self._target:
            return False
        ap = self._ap()
        if ap and not ap["up"] and _now() - ap["down_at"] > WORLD.driver_give_up_ms:
            self._target = None
            return False
        if not ap or not ap["up"] or ap["password"] != self._target[1]:
            return False
        started = max(self._target[2], ap["up_since"])
        return _now() - started >= ap["delay"]

    def status(self, what=None):
        if what == "stations":
            return list(WORLD.ap_clients)
        if what == "rssi":
            ap = self._ap()
            if not ap:
                raise OSError("not connected")
            return ap["rssi"]
        if self.isconnected():
            return STAT_GOT_IP
        if not self._target:
            return STAT_IDLE
        ap = self._ap()
        waited = _now() - self._target[2]
        if ap is None or not ap["up"]:
            return STAT_NO_AP_FOUND if waited > 2000 else STAT_CONNECTING
        if ap["password"] != self._target[1]:
            return STAT_WRONG_PASSWORD if waited > 500 else STAT_CONNECTING
        return STAT_CONNECTING

    def scan(self):
        WORLD.scan_calls += 1
        if WORLD.scan_error:
            raise OSError(WORLD.scan_error)
        out = []
        for ssid, ap in WORLD.aps.items():
            if ap["up"] and not ap["hidden"]:
                out.append((ssid.encode(), b"\x00" * 6, ap["channel"], ap["rssi"],
                            AUTH_WPA2_PSK if ap["password"] else AUTH_OPEN, False))
        return out


def hostname(name=None):
    if name is None:
        return WORLD.hostname
    WORLD.hostname = name
