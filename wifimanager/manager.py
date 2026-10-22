"""WiFiManager: connection supervision, retry/backoff, loss detection, config-portal control.

The whole supervision logic lives in ONE generator (`_main`) that yields "sleep this many
milliseconds". Three thin drivers run it:

* asyncio  : `await wm.start()`      (background task, portal served by the same event loop)
* blocking : `wm.connect()`          (sleeps with time.sleep_ms, pumps the loop while the portal is open)
* polling  : `wm.check()` in your own while-loop
"""
import gc
import random

import network

from . import compat as C
from .compat import ticks_add, ticks_diff
from .config import ConfigStore, clean_password, clean_ssid
from .log import Logger

__version__ = "2.0.0"

# supervisor states (what `wm.state` returns)
IDLE = "idle"
CONNECTING = "connecting"
CONNECTED = "connected"
LOST = "lost"
BACKOFF = "backoff"
PORTAL = "portal"

# config_mode values
MODE_ON_FAIL = "on_fail"
MODE_ALWAYS = "always"
MODE_BUTTON = "button"
MODE_NEVER = "never"
_MODES = (MODE_ON_FAIL, MODE_ALWAYS, MODE_BUTTON, MODE_NEVER)

_STATUS = {network.STAT_IDLE: "idle", network.STAT_CONNECTING: "connecting",
           network.STAT_WRONG_PASSWORD: "wrong password", network.STAT_NO_AP_FOUND: "no ap found",
           network.STAT_CONNECT_FAIL: "connect fail", network.STAT_GOT_IP: "got ip",
           network.STAT_BEACON_TIMEOUT: "beacon timeout", network.STAT_HANDSHAKE_TIMEOUT: "handshake timeout"}


def status_text(code):
    return _STATUS.get(code, "status %s" % code)


class WiFiManager:
    IDLE, CONNECTING, CONNECTED, LOST, BACKOFF, PORTAL = IDLE, CONNECTING, CONNECTED, LOST, BACKOFF, PORTAL
    MODE_ON_FAIL, MODE_ALWAYS, MODE_BUTTON, MODE_NEVER = MODE_ON_FAIL, MODE_ALWAYS, MODE_BUTTON, MODE_NEVER

    PORTAL_LINGER_MS = 10000  # keep the portal open this long after a successful connection

    def __init__(self, networks=None, *,
                 config_file="/wifi_config.json", force_defaults=False,
                 config_mode=MODE_ON_FAIL,
                 button_pin=None, button_hold_ms=3000, button_active_low=True,
                 ap_ssid=None, ap_password=None, portal_port=80, captive_portal=True,
                 portal_timeout=300, portal_timeout_action="retry", portal_retry_interval=120,
                 show_passwords=False,
                 hostname="esp32", connect_timeout=15, retries_per_network=2, max_cycles=3,
                 driver_reconnects=3, lost_grace=10, check_interval=5,
                 backoff_base=2, backoff_max=300, reboot_after=0,
                 internet_check_host=None, internet_check_interval=60, internet_check_failures=3,
                 log_level="INFO", log_file=None, log_max_size=8192, log_buffer=60,
                 on_connect=None, on_disconnect=None, on_portal=None, on_state=None):
        if config_mode not in _MODES:
            raise ValueError("config_mode must be one of %s" % (_MODES,))
        if portal_timeout_action not in ("retry", "reboot"):
            raise ValueError("portal_timeout_action must be 'retry' or 'reboot'")
        if ap_password:
            if not 8 <= len(ap_password) <= 63:
                raise ValueError("ap_password must be 8-63 characters (or empty for an open AP)")
        if config_mode == MODE_BUTTON and button_pin is None:
            raise ValueError("config_mode='button' needs button_pin")

        self.config_mode = config_mode
        self.button_pin = button_pin
        self.button_hold_ms = button_hold_ms
        self.button_active_low = button_active_low
        self.ap_ssid = ap_ssid
        self.ap_password = ap_password or None
        self.portal_port = portal_port
        self.captive_portal = captive_portal
        self.portal_timeout = portal_timeout
        self.portal_timeout_action = portal_timeout_action
        self.portal_retry_interval = portal_retry_interval
        self.show_passwords = show_passwords
        self.hostname = hostname
        self.connect_timeout = connect_timeout
        self.retries_per_network = max(1, retries_per_network)
        self.max_cycles = max(1, max_cycles)
        self.driver_reconnects = driver_reconnects
        self.lost_grace = lost_grace
        self.check_interval = max(1, check_interval)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.reboot_after = reboot_after
        self.internet_check_host = internet_check_host
        self.internet_check_interval = internet_check_interval
        self.internet_check_failures = max(1, internet_check_failures)

        self._cb_connect = on_connect
        self._cb_disconnect = on_disconnect
        self._cb_portal = on_portal
        self._cb_state = on_state

        self.log = Logger(log_level, log_buffer, log_file, log_max_size)
        self._store = ConfigStore(config_file, self.log)
        self._store.load()
        if networks:
            try:
                self._store.merge_defaults(networks, force_defaults)
            except Exception as e:
                self.log.error("could not store default networks: %s", C.exc_text(e))

        self.wlan = network.WLAN(network.WLAN.IF_STA)

        # supervisor state
        self._sta = IDLE
        self._pub = IDLE
        self._ssid = None
        self._gen = None
        self._booted = False
        self._running = False
        self._async = False
        self._task = None
        self._btn_task = None
        self._kick = False
        self._wake = 0
        self._first_done = False
        self._failed_cycles = 0
        self._next_try = None
        self._offline_since = None
        self._connect_req = None
        self._last_error = ""
        self._inet_fail = 0
        self._inet_next = 0
        self._reset_at = None
        self._nonet_handled = False
        # portal
        self._portal = None
        self._portal_req = False
        self._portal_close_at = None
        # scan cache
        self._scan = None
        self._scan_ts = 0
        # button
        self._btn = None
        self._btn_since = None
        self._btn_fired = False

    # =====================================================================================
    # public: state
    # =====================================================================================
    @property
    def state(self):
        return self._pub

    @property
    def ssid(self):
        return self._ssid if self.wlan.isconnected() else None

    @property
    def ip(self):
        return self.wlan.ipconfig("addr4")[0] if self.wlan.isconnected() else None

    @property
    def portal_active(self):
        return self._portal is not None

    def is_connected(self):
        return bool(self.wlan.isconnected())

    def rssi(self):
        if not self.wlan.isconnected():
            return None
        try:
            return self.wlan.status("rssi")
        except Exception:
            return None

    def status(self):
        w = self.wlan
        up = bool(w.isconnected())
        d = {"state": self._pub, "connected": up, "ssid": self._ssid if up else None,
             "ip": w.ipconfig("addr4")[0] if up else None, "rssi": self.rssi(), "mode": self.config_mode,
             "hostname": self.hostname, "version": __version__,
             "failed_cycles": self._failed_cycles, "last_error": self._last_error,
             "portal": self._portal is not None}
        if self._portal is not None:
            d["ap_ssid"] = self._portal.ap_ssid
            d["ap_ip"] = self._portal.ap_ip
            d["clients"] = self._portal.clients()
        return d

    # =====================================================================================
    # public: networks stored in flash
    # =====================================================================================
    def networks(self):
        """Saved networks as a list of dicts (ssid, password, hidden)."""
        return self._store.networks()

    def add_network(self, ssid, password="", hidden=False):
        self._store.upsert(ssid, password, hidden)
        self._kick = True

    def update_network(self, ssid, **fields):
        """Edit a saved network. Fields: password, hidden, new_ssid (rename)."""
        extra = [k for k in fields if k not in ("password", "hidden", "new_ssid")]
        if extra:
            raise TypeError("unknown field(s): %s" % ", ".join(extra))
        self._store.upsert(fields.get("new_ssid", ssid), fields.get("password"), fields.get("hidden"),
                           original=ssid)
        self._kick = True

    def remove_network(self, ssid):
        return self._store.remove(ssid)

    def networks_public(self):
        """Saved networks for the web page (passwords only when show_passwords=True)."""
        cur = self.ssid
        out = []
        for n in self._store.networks():
            item = {"ssid": n["ssid"], "hidden": n["hidden"], "has_password": bool(n["password"]),
                    "connected": n["ssid"] == cur}
            if self.show_passwords:
                item["password"] = n["password"]
            out.append(item)
        return out

    def scan(self, force=True):
        """Scan for networks. Returns [{ssid, rssi, channel, secure}] strongest first.

        Blocks for 2-4 s. With force=False a result younger than 10 s is reused.
        """
        now = C.ticks_ms()
        if not force and self._scan is not None and ticks_diff(now, self._scan_ts) < 10000:
            return self._scan
        best = {}
        for t in self.wlan.scan():
            try:
                ssid = t[0].decode()
            except Exception:
                continue
            if not ssid:
                continue
            if ssid not in best or t[3] > best[ssid]["rssi"]:
                best[ssid] = {"ssid": ssid, "rssi": t[3], "channel": t[2], "secure": t[4] != 0}
        out = sorted(best.values(), key=lambda r: r["rssi"], reverse=True)
        self._scan = out
        self._scan_ts = C.ticks_ms()
        return out

    # =====================================================================================
    # public: control
    # =====================================================================================
    def start_portal(self):
        """Open the configuration portal (sync mode: it is served inside the next connect()/check())."""
        self._portal_req = True
        self._portal_close_at = None
        self._kick = True

    def stop_portal(self):
        self._portal_req = False
        self._kick = True

    def reconnect(self, ssid=None):
        """Drop the current connection and run a connect cycle now (optionally only to `ssid`)."""
        self._connect_req = ssid or True
        self._kick = True

    def stop(self, disconnect=False):
        """Stop supervising. With disconnect=True the WiFi radio is switched off too."""
        self._running = False
        self._kick = True
        self._close_portal()
        if disconnect:
            try:
                self.wlan.disconnect()
                self.wlan.active(False)
            except Exception:
                pass
        self._booted = False
        self._gen = None
        self._task = None
        self._set_sta(IDLE)

    # ---- called by the web portal ------------------------------------------------------
    def save_network(self, ssid, password=None, hidden=None, original=None):
        self._store.upsert(ssid, password, hidden, original=original)
        self._kick = True

    def request_exit(self):
        self._portal_req = False
        self._nonet_handled = True
        self._kick = True

    def request_reboot(self):
        self._reset_at = ticks_add(C.ticks_ms(), 1500)
        self._kick = True

    # =====================================================================================
    # driver: asyncio
    # =====================================================================================
    async def start(self, wait=True):
        """Start background supervision. With wait=True returns after the first connect attempt."""
        if self._task is not None:
            return self.is_connected()
        self._async = True
        self._boot()
        self._task = C.create_task(self._run_async())
        if self._btn is not None:
            self._btn_task = C.create_task(self._button_loop())
        if wait:
            while self._running and not self._first_done:
                await C.sleep_ms(100)
        return self.is_connected()

    async def wait_connected(self, timeout=None):
        end = ticks_add(C.ticks_ms(), int(timeout * 1000)) if timeout else None
        while not self.wlan.isconnected():
            if end is not None and ticks_diff(end, C.ticks_ms()) <= 0:
                return False
            await C.sleep_ms(200)
        return True

    async def _run_async(self):
        while self._running:
            ms = self._next()
            if ms is None:
                break
            end = ticks_add(C.ticks_ms(), ms)
            while self._running and not self._kick:
                rem = ticks_diff(end, C.ticks_ms())
                if rem <= 0:
                    break
                await C.sleep_ms(min(rem, 100))
            self._kick = False

    async def _button_loop(self):
        while self._running:
            self._poll_button()
            await C.sleep_ms(100)

    # =====================================================================================
    # driver: blocking / polling
    # =====================================================================================
    def connect(self, timeout=None):
        """Blocking connect. Returns True once connected, False when `timeout` seconds elapsed.

        While the config portal is open this call serves it (and returns when WiFi is connected).
        """
        if self._async:
            return self.is_connected()
        self._boot()
        end = ticks_add(C.ticks_ms(), int(timeout * 1000)) if timeout else None
        while True:
            ms = self._next()
            if ms is None:
                return False
            if self._sta == CONNECTED:
                self._wake = ticks_add(C.ticks_ms(), ms)
                return True
            if end is not None and ticks_diff(end, C.ticks_ms()) <= 0:
                self._wake = ticks_add(C.ticks_ms(), ms)
                return False
            self._sleep_sync(ms)

    def check(self):
        """One supervision step for your own loop. Cheap when nothing is due; returns `wm.state`.

        Call it often (every loop iteration). A config portal opened by a failure, the button or
        start_portal() is served *inside* this call until it closes (mode "always" is only pumped briefly).
        """
        if self._async:
            return self._pub
        self._boot()
        self._poll_button()
        if self._portal is not None and self.config_mode == MODE_ALWAYS:
            C.run_for(10)
        now = C.ticks_ms()
        if self._kick or ticks_diff(now, self._wake) >= 0:
            self._kick = False
            ms = self._next()
            if ms is not None:
                self._wake = ticks_add(now, ms)
        while self._portal is not None and self.config_mode != MODE_ALWAYS and self._running:
            ms = self._next()
            if ms is None:
                break
            if self._portal is None:  # it just closed: hand control back to the caller
                self._wake = ticks_add(C.ticks_ms(), ms)
                break
            self._sleep_sync(ms)
        return self._pub

    def _sleep_sync(self, ms):
        end = ticks_add(C.ticks_ms(), ms)
        while not self._kick:
            rem = ticks_diff(end, C.ticks_ms())
            if rem <= 0:
                break
            step = min(rem, 100)
            self._poll_button()
            if self._portal is not None:
                C.run_for(step)
            else:
                C.sleep_ms_blocking(step)
        self._kick = False

    # =====================================================================================
    # boot / helpers
    # =====================================================================================
    def _next(self):
        try:
            return next(self._gen)
        except StopIteration:
            self._running = False
            return None

    def _boot(self):
        if self._booted:
            return
        self._booted = True
        self._running = True
        w = self.wlan
        w.active(True)
        network.hostname(self.hostname)
        if self.driver_reconnects is not None:
            w.config(reconnects=self.driver_reconnects)
        if self.button_pin is not None and self._btn is None:
            self._init_button()
        self._offline_since = C.ticks_ms()
        self._gen = self._main()
        self.log.info("WiFiManager %s started (mode=%s, %d saved network(s))",
                      __version__, self.config_mode, len(self._store.networks()))

    def _fire(self, cb, *args):
        if cb is None:
            return
        try:
            r = cb(*args)
            if C.is_coroutine(r):
                if self._async:
                    C.create_task(r)
                else:
                    C.run_until_complete(r)
        except Exception as e:
            self.log.error("callback error: %s", C.exc_text(e))

    def _set_sta(self, new):
        if new != self._sta:
            self._sta = new
            self._notify()

    def _notify(self):
        new = PORTAL if (self._portal is not None and self._sta != CONNECTED) else self._sta
        old = self._pub
        if new != old:
            self._pub = new
            self.log.debug("state %s -> %s", old, new)
            self._fire(self._cb_state, old, new)

    def _do_reset(self):
        self.log.warning("rebooting")
        import machine
        machine.reset()

    # ---- button ---------------------------------------------------------------------------
    def _init_button(self):
        from machine import Pin
        pull = Pin.PULL_UP if self.button_active_low else Pin.PULL_DOWN
        try:
            self._btn = Pin(self.button_pin, Pin.IN, pull)
        except Exception:
            self._btn = Pin(self.button_pin, Pin.IN)  # input-only pins have no internal pull

    def _poll_button(self):
        b = self._btn
        if b is None:
            return
        pressed = (b.value() == 0) if self.button_active_low else (b.value() == 1)
        if not pressed:
            self._btn_since = None
            self._btn_fired = False
            return
        now = C.ticks_ms()
        if self._btn_since is None:
            self._btn_since = now
        elif not self._btn_fired and ticks_diff(now, self._btn_since) >= self.button_hold_ms:
            self._btn_fired = True
            self.log.info("button held: opening config portal")
            self.start_portal()

    # ---- portal ---------------------------------------------------------------------------
    def _open_portal(self):
        if self._portal is not None:
            return
        gc.collect()
        try:
            from .portal import Portal
            p = Portal(self)
            p.start()
        except Exception as e:
            self.log.error("could not start config portal: %s", C.exc_text(e))
            self._portal_req = False
            return
        self._portal = p
        self.log.info("config portal open: connect to '%s' and browse http://%s/", p.ap_ssid, p.ap_ip)
        self._notify()
        self._fire(self._cb_portal, True)

    def _close_portal(self):
        p = self._portal
        if p is None:
            return
        self._portal = None
        self._portal_close_at = None
        self._next_try = None
        self._failed_cycles = 0  # whatever closed it (exit, timeout, success): start counting afresh
        try:
            p.stop()
        except Exception as e:
            self.log.error("error closing portal: %s", C.exc_text(e))
        gc.collect()
        self.log.info("config portal closed")
        self._notify()
        self._fire(self._cb_portal, False)

    def _service(self):
        """Reconcile requests (portal open/close, reboot) - runs at every supervisor wake-up."""
        now = C.ticks_ms()
        if self._reset_at is not None and ticks_diff(now, self._reset_at) >= 0:
            self._do_reset()
        if self._portal_close_at is not None and ticks_diff(now, self._portal_close_at) >= 0:
            self._portal_close_at = None
            if self.config_mode != MODE_ALWAYS:
                self._portal_req = False
        p = self._portal
        if (p is not None and self.portal_timeout and self.config_mode != MODE_ALWAYS
                and (self._sta == CONNECTED or self._store.networks())):
            if p.clients() > 0:
                p.touch()
            elif ticks_diff(now, p.last_activity) >= self.portal_timeout * 1000:
                self.log.warning("portal idle for %ds: closing", self.portal_timeout)
                self._portal_req = False
                if self._sta != CONNECTED and self.portal_timeout_action == "reboot":
                    self._do_reset()
        if self._portal_req and self._portal is None:
            self._open_portal()
        elif not self._portal_req and self._portal is not None:
            self._close_portal()

    # =====================================================================================
    # the supervisor (generator: every `yield n` means "wait n milliseconds")
    # =====================================================================================
    def _main(self):
        if self.config_mode == MODE_ALWAYS:
            self._portal_req = True
        try:
            if self.wlan.isconnected():  # e.g. after a soft reboot the radio can still be associated
                try:
                    ssid = self.wlan.config("ssid")
                except Exception:
                    ssid = None
                self._on_connected(ssid)
        except Exception as e:
            self.log.error("supervisor error: %s", C.exc_text(e))
        while self._running:
            try:
                self._service()
                if self._sta == CONNECTED:
                    yield from self._connected()
                else:
                    yield from self._offline()
            except Exception as e:
                self.log.error("supervisor error: %s", C.exc_text(e))
                yield 2000

    def _connected(self):
        # poll faster while a portal is open: its linger/idle timers are evaluated in _service()
        yield 1000 if (self._portal is not None or self._reset_at is not None) else self.check_interval * 1000
        if not self._running:
            return
        if self._connect_req is not None:
            self.log.info("reconnect requested: dropping current connection")
            self._fire(self._cb_disconnect, "reconnect requested")
            self.wlan.disconnect()
            self._offline_since = C.ticks_ms()
            self._set_sta(IDLE)
            return
        if not self.wlan.isconnected():
            yield from self._handle_loss()
            return
        # associated: optionally verify that the internet is really reachable
        if self.internet_check_host and ticks_diff(C.ticks_ms(), self._inet_next) >= 0:
            self._inet_next = ticks_add(C.ticks_ms(), self.internet_check_interval * 1000)
            if self._internet_ok():
                self._inet_fail = 0
            else:
                self._inet_fail += 1
                self.log.warning("internet check failed (%d/%d)", self._inet_fail, self.internet_check_failures)
                if self._inet_fail >= self.internet_check_failures:
                    self._inet_fail = 0
                    self.log.warning("no internet: forcing reconnect")
                    self._fire(self._cb_disconnect, "no internet")
                    self.wlan.disconnect()
                    self._offline_since = C.ticks_ms()
                    self._set_sta(LOST)

    def _handle_loss(self):
        reason = status_text(self.wlan.status())
        self._offline_since = C.ticks_ms()
        self._set_sta(LOST)
        self.log.warning("connection to '%s' lost (%s)", self._ssid, reason)
        self._fire(self._cb_disconnect, reason)
        # the ESP-IDF driver retries on its own (see `driver_reconnects`): give it a head start
        t0 = C.ticks_ms()
        while ticks_diff(C.ticks_ms(), t0) < self.lost_grace * 1000:
            yield 500
            if self.wlan.isconnected():
                self.log.info("connection recovered by the WiFi driver")
                self._on_connected(self._ssid)
                return
            if self._connect_req is not None or not self._running:
                return
        self.log.info("driver did not recover in %ds: taking over", self.lost_grace)

    def _offline(self):
        if not self._store.networks() and self._connect_req is None:
            if not self._nonet_handled:
                self._nonet_handled = True
                self.log.warning("no WiFi network saved")
                if self.config_mode in (MODE_ON_FAIL, MODE_ALWAYS):
                    self._portal_req = True
                    self._service()
            self._set_sta(IDLE)
            self._first_done = True
            yield 1000 if self._portal is not None else self.check_interval * 1000
            return
        self._nonet_handled = False
        req = self._connect_req
        p = self._portal
        if req is None:
            # don't yank the radio away from somebody who is using the setup page
            if p is not None and p.clients() > 0:
                yield 1000
                return
            if self._next_try is not None and ticks_diff(C.ticks_ms(), self._next_try) < 0:
                yield 1000
                return
        self._connect_req = None
        self._next_try = None
        if req is not None and p is not None:
            yield 1500  # let the HTTP answer reach the browser before the radio changes channel
        ok = yield from self._connect_cycle(req)
        if ok:
            self._on_connected()
            return
        self._failed_cycles += 1
        self._first_done = True
        self._set_sta(BACKOFF)
        self._check_reboot()
        if (self.config_mode == MODE_ON_FAIL and self._failed_cycles >= self.max_cycles
                and not self._portal_req and self._portal is None):
            self.log.warning("%d connection cycle(s) failed: opening config portal", self._failed_cycles)
            self._portal_req = True
        if (self._portal_req or self._portal is not None) and self.config_mode != MODE_ALWAYS:
            delay = self.portal_retry_interval * 1000
        else:
            delay = self._backoff_ms()
        self.log.info("next attempt in %ds", delay // 1000)
        self._next_try = ticks_add(C.ticks_ms(), delay)
        yield 500

    def _backoff_ms(self):
        secs = min(self.backoff_max, self.backoff_base * (2 ** min(self._failed_cycles - 1, 16)))
        return int(secs * 1000 * (80 + random.getrandbits(6) % 41) / 100)  # +-20 % jitter

    def _check_reboot(self):
        if not self.reboot_after or self._offline_since is None:
            return
        if ticks_diff(C.ticks_ms(), self._offline_since) < self.reboot_after * 1000:
            return
        if self._portal is not None and self._portal.clients() > 0:
            return
        self.log.error("offline for more than %ds: rebooting", self.reboot_after)
        self._do_reset()

    def _order(self, nets):
        """Visible saved networks strongest first, then hidden ones in saved order."""
        seen = None
        try:
            found = self.scan(force=True)
            if found:
                seen = {r["ssid"]: r["rssi"] for r in found}
        except Exception as e:
            self.log.warning("scan failed (%s): trying every saved network", C.exc_text(e))
        if seen is None:
            return list(nets)
        visible = [n for n in nets if n["ssid"] in seen]
        visible.sort(key=lambda n: seen[n["ssid"]], reverse=True)
        hidden = [n for n in nets if n["ssid"] not in seen and n["hidden"]]
        for n in nets:
            if n["ssid"] not in seen and not n["hidden"]:
                self.log.debug("'%s' not in range", n["ssid"])
        return visible + hidden

    def _connect_cycle(self, only=None):
        self._set_sta(CONNECTING)
        nets = self._store.networks()
        if only is not None and only is not True:
            nets = [n for n in nets if n["ssid"] == only]
        order = self._order(nets)
        if not order:
            self.log.warning("no saved network in range")
            self._last_error = "no saved network in range"
            return False
        for net in order:
            for attempt in range(self.retries_per_network):
                if not self._running:
                    return False
                self.log.info("connecting to '%s' (try %d/%d)", net["ssid"], attempt + 1, self.retries_per_network)
                ok = yield from self._attempt(net)
                if ok:
                    return True
                if self._last_error == status_text(network.STAT_WRONG_PASSWORD):
                    break  # retrying with the same password is pointless
                yield 500
        return False

    def _attempt(self, net):
        w = self.wlan
        try:
            w.disconnect()
        except Exception:
            pass
        yield 100
        try:
            if net["password"]:
                w.connect(net["ssid"], net["password"])
            else:
                w.connect(net["ssid"])
        except Exception as e:
            self._last_error = C.exc_text(e)
            self.log.error("connect('%s') failed: %s", net["ssid"], self._last_error)
            return False
        start = C.ticks_ms()
        while True:
            yield 250
            if not self._running:
                return False
            if w.isconnected():
                self._ssid = net["ssid"]
                return True
            st = w.status()
            if st == network.STAT_WRONG_PASSWORD:
                self._last_error = status_text(st)
                self.log.error("'%s': wrong password", net["ssid"])
                break
            if ticks_diff(C.ticks_ms(), start) >= self.connect_timeout * 1000:
                self._last_error = status_text(st) if st != network.STAT_CONNECTING else "timeout"
                self.log.warning("'%s': %s", net["ssid"], self._last_error)
                break
        try:
            w.disconnect()
        except Exception:
            pass
        return False

    def _on_connected(self, ssid=None):
        now = C.ticks_ms()
        if ssid is not None:
            self._ssid = ssid
        self._failed_cycles = 0
        self._next_try = None
        self._offline_since = None
        self._inet_fail = 0
        self._inet_next = ticks_add(now, self.internet_check_interval * 1000)
        self._last_error = ""
        self._first_done = True
        self._set_sta(CONNECTED)
        ip = self.wlan.ipconfig("addr4")[0]
        self.log.info("connected to '%s' ip=%s rssi=%s", self._ssid, ip, self.rssi())
        if self._portal is not None and self.config_mode != MODE_ALWAYS:
            self._portal_close_at = ticks_add(now, self.PORTAL_LINGER_MS)
        self._fire(self._cb_connect, self._ssid, ip)

    def _internet_ok(self):
        import socket
        host = self.internet_check_host
        if isinstance(host, str):
            name, _, port = host.partition(":")
            host = (name, int(port) if port else 53)
        s = None
        try:
            addr = socket.getaddrinfo(host[0], host[1])[0][-1]
            s = socket.socket()
            s.settimeout(3)
            s.connect(addr)
            return True
        except Exception:
            return False
        finally:
            if s is not None:
                s.close()
