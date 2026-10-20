"""Fake `network` module (MicroPython v1.29 API) for the unix-port tests: a scriptable radio environment.

Use `WORLD` to define access points and to flip them up/down. All timing goes through
wifimanager.compat.ticks_ms(), so tests can run on a virtual clock.
"""
STAT_IDLE = 1000
STAT_CONNECTING = 1001
STAT_GOT_IP = 1010
STAT_WRONG_PASSWORD = 202
STAT_NO_AP_FOUND = 201
STAT_BEACON_TIMEOUT = 200
STAT_CONNECT_FAIL = 203
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
    IF_STA = 0
    IF_AP = 1
    SEC_OPEN = 0
    SEC_WPA2 = 3

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
        store = WORLD.ap_config if self.iface == WLAN.IF_AP else WORLD.sta_config
        if args:
            key = args[0]
            if key == "mac":
                return b"\x24\x0a\xc4\x12\xab\xcd"
            if key == "ssid":
                return self._target[0] if self._target else store.get("ssid", "")
            return store.get(key)
        for k in kw:
            if k not in ("ssid", "key", "security", "hidden", "channel", "reconnects", "max_clients"):
                raise ValueError("unknown config param")  # what the v1.29 driver does
        store.update(kw)

    def ifconfig(self, cfg=None):
        # only the AP uses it (it sets the DNS handed to DHCP clients)
        if cfg is None or self.iface != WLAN.IF_AP:
            raise TypeError("the library must use ipconfig() for reading / the station")
        self._ip = tuple(cfg)

    def ipconfig(self, key):
        if self.iface == WLAN.IF_AP:
            ip = self._ip if self._ip[0] != "0.0.0.0" else ("192.168.4.1", "255.255.255.0", "192.168.4.1")
        elif self.isconnected():
            ip = ("192.168.1.50", "255.255.255.0", "192.168.1.1")
        else:
            ip = ("0.0.0.0", "0.0.0.0", "0.0.0.0")
        if key == "addr4":
            return (ip[0], ip[1])
        if key == "gw4":
            return ip[2]
        raise ValueError("unexpected key")

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
        if self.iface == WLAN.IF_AP or not self._active or not self._target:
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
                            WLAN.SEC_WPA2 if ap["password"] else WLAN.SEC_OPEN, False))
        return out


def hostname(name=None):
    if name is None:
        return WORLD.hostname
    WORLD.hostname = name
