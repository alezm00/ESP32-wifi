import harness  # noqa: F401  (sets up sys.path, fakes, virtual clock)
import unittest

import machine
from _util import exists
from harness import Sim, W

from wifimanager import WiFiManager, compat


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.sim = Sim()

    def tearDown(self):
        self.sim.close()

    def connected(self, wm):
        return lambda: wm.state == "connected" and wm.is_connected()

    # ---- choosing a network -------------------------------------------------------------
    def test_connects_to_strongest_visible_network(self):
        W.add_ap("Weak", "weakpass1", rssi=-82)
        W.add_ap("Strong", "strongpass1", rssi=-48)
        wm = self.sim.make([("Weak", "weakpass1"), ("Strong", "strongpass1")])
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        self.assertEqual(wm.ssid, "Strong")
        self.assertEqual(W.connect_calls[0][0], "Strong")
        self.assertEqual(W.hostname, "esp32")

    def test_hidden_networks_are_tried_last(self):
        W.add_ap("Visible", "rightpass1", rssi=-70)
        W.add_ap("Secret", "secretpass1", rssi=-30, hidden=True)
        wm = self.sim.make([{"ssid": "Secret", "password": "secretpass1", "hidden": True},
                            ("Visible", "WRONGpass1")], retries_per_network=1)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 120000))
        self.assertEqual([c[0] for c in W.connect_calls], ["Visible", "Secret"])

    def test_wrong_password_is_not_retried_then_next_network(self):
        W.add_ap("A", "rightpass1", rssi=-40)
        W.add_ap("B", "otherpass1", rssi=-60)
        wm = self.sim.make([("A", "WRONGpass1"), ("B", "otherpass1")], retries_per_network=3)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 120000))
        self.assertEqual([c[0] for c in W.connect_calls], ["A", "B"])
        self.assertEqual(wm.ssid, "B")

    def test_scan_failure_falls_back_to_saved_order(self):
        W.add_ap("A", "passwordA", rssi=-40)
        W.scan_error = "Wifi Internal Error"
        wm = self.sim.make([("A", "passwordA")])
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))

    def test_open_network_connects_without_password(self):
        W.add_ap("Free", "")
        wm = self.sim.make(["Free"])
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        self.assertEqual(W.connect_calls[-1], ("Free", ""))

    # ---- retry / backoff ----------------------------------------------------------------
    def test_backoff_grows_exponentially_and_is_capped(self):
        wm = self.sim.make([("Gone", "password1")], config_mode="never", backoff_max=100)
        seen = {}
        wm._boot()
        for _ in range(400):
            self.sim.t += wm._next()
            if wm._failed_cycles and wm._failed_cycles not in seen and wm._next_try is not None:
                seen[wm._failed_cycles] = (wm._next_try - self.sim.t) / 1000
        for n in range(1, 9):
            expect = min(100, 2 * 2 ** (n - 1))
            self.assertGreaterEqual(seen[n] + 0.6, expect * 0.8, "cycle %d" % n)   # +0.5 s: yield granularity
            self.assertLessEqual(seen[n] - 0.1, expect * 1.2, "cycle %d" % n)

    def test_connects_when_network_appears_later(self):
        wm = self.sim.make([("Late", "latepass1")], config_mode="never")
        W.add_ap("Late", "latepass1")
        W.set_up("Late", False)
        self.assertFalse(self.sim.run(wm, self.connected(wm), 40000))
        W.set_up("Late", True)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        self.assertIn("backoff", self.sim.states())

    def test_failed_attempt_uses_connect_timeout(self):
        W.add_ap("Slow", "slowpass12", delay_ms=60000)
        wm = self.sim.make([("Slow", "slowpass12")], connect_timeout=5, retries_per_network=2, config_mode="never")
        t0 = self.sim.t
        self.sim.run(wm, lambda: wm._failed_cycles == 1, 120000)
        self.assertEqual(len(W.connect_calls), 2)
        self.assertTrue(10000 <= self.sim.t - t0 <= 14000, self.sim.t - t0)

    # ---- loss detection -----------------------------------------------------------------
    def test_short_drop_is_recovered_by_the_driver(self):
        W.add_ap("Home", "homepass12")
        calls = []
        wm = self.sim.make([("Home", "homepass12")], on_connect=lambda s, ip: calls.append((s, ip)),
                           on_disconnect=lambda r: calls.append(("lost", r)))
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        n_connects = len(W.connect_calls)
        W.set_up("Home", False)
        self.sim.run(wm, ms=4000)
        W.set_up("Home", True)
        self.assertTrue(self.sim.run(wm, lambda: wm.state == "connected" and len(calls) >= 3, 30000))
        self.assertEqual(len(W.connect_calls), n_connects)           # the manager never touched the radio
        self.assertEqual([c[0] for c in calls], ["Home", "lost", "Home"])
        self.assertIn("recovered by the WiFi driver", "\n".join(wm.log.tail()))
        self.assertEqual(calls[0][1], "192.168.1.50")

    def test_long_outage_manager_takes_over_and_reconnects(self):
        W.add_ap("Home", "homepass12")
        wm = self.sim.make([("Home", "homepass12")], config_mode="never", lost_grace=10)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        n_connects = len(W.connect_calls)
        W.set_up("Home", False)
        self.sim.run(wm, ms=90000)
        self.assertFalse(wm.is_connected())
        states = self.sim.states()
        self.assertIn("lost", states)
        self.assertIn("backoff", states)
        W.set_up("Home", True)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 120000))
        self.assertTrue(len(W.connect_calls) > n_connects)
        self.assertIn("taking over", "\n".join(wm.log.tail()))

    def test_internet_check_forces_reconnect(self):
        W.add_ap("Home", "homepass12")
        reasons = []
        wm = self.sim.make([("Home", "homepass12")], internet_check_host="8.8.8.8:53",
                           internet_check_interval=10, internet_check_failures=2, on_disconnect=reasons.append)
        wm._internet_ok = lambda: False
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        n = len(W.connect_calls)
        self.sim.run(wm, lambda: reasons, 120000)
        self.assertEqual(reasons, ["no internet"])
        wm._internet_ok = lambda: True
        self.assertTrue(self.sim.run(wm, lambda: len(W.connect_calls) > n and self.connected(wm)(), 120000))

    def test_reboot_after_offline_too_long(self):
        wm = self.sim.make([("Gone", "password1")], config_mode="never", reboot_after=60)
        with self.assertRaises(machine.ResetCalled):
            self.sim.run(wm, ms=300000)
        self.assertGreaterEqual(self.sim.t - 100000, 60000)

    def test_already_connected_at_boot(self):
        W.add_ap("Home", "homepass12", delay_ms=0)
        wm = self.sim.make([("Home", "homepass12")])
        wm.wlan.active(True)
        wm.wlan.connect("Home", "homepass12")        # radio survived a soft reboot
        self.assertTrue(self.sim.run(wm, self.connected(wm), 5000))
        self.assertEqual(len(W.connect_calls), 1)

    # ---- config portal policy -----------------------------------------------------------
    def test_on_fail_opens_portal_after_max_cycles(self):
        wm = self.sim.make([("Gone", "password1")], config_mode="on_fail", max_cycles=3)
        self.assertTrue(self.sim.run(wm, lambda: self.sim.portal is not None, 120000))
        self.assertEqual(wm._failed_cycles, 3)
        self.assertEqual(wm.state, "portal")
        self.assertTrue(wm.portal_active)

    def test_never_and_button_modes_do_not_open_portal_on_failure(self):
        for mode, extra in (("never", {}), ("button", {"button_pin": 0})):
            wm = self.sim.make([("Gone", "password1")], config_mode=mode, max_cycles=2, **extra)
            self.sim.run(wm, ms=300000)
            self.assertIsNone(self.sim.portal, mode)
            self.assertTrue(wm._failed_cycles > 2)

    def test_always_mode_opens_portal_at_start_and_keeps_it(self):
        W.add_ap("Home", "homepass12")
        wm = self.sim.make([("Home", "homepass12")], config_mode="always", portal_timeout=5)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        self.sim.run(wm, ms=120000)
        self.assertTrue(wm.portal_active)
        self.assertFalse(self.sim.portal.stopped)
        self.assertEqual(wm.state, "connected")

    def test_no_saved_network_opens_portal_unless_never(self):
        wm = self.sim.make(None, config_mode="on_fail")
        self.sim.run(wm, ms=5000)
        self.assertTrue(wm.portal_active)
        self.assertEqual(wm.state, "portal")
        self.sim.close(); self.sim = Sim()
        wm = self.sim.make(None, config_mode="never")
        self.sim.run(wm, ms=5000)
        self.assertFalse(wm.portal_active)

    def test_portal_keeps_retrying_and_closes_after_success(self):
        wm = self.sim.make([("Late", "latepass1")], max_cycles=1, portal_retry_interval=20, portal_timeout=0)
        W.add_ap("Late", "latepass1")
        W.set_up("Late", False)
        self.assertTrue(self.sim.run(wm, lambda: wm.portal_active, 60000))
        W.set_up("Late", True)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        self.assertTrue(self.sim.run(wm, lambda: not wm.portal_active, 30000))
        self.assertTrue(self.sim.portal.stopped)

    def test_portal_retry_waits_while_a_client_uses_the_page(self):
        W.add_ap("Late", "latepass1")
        W.set_up("Late", False)
        wm = self.sim.make([("Late", "latepass1")], max_cycles=1, portal_retry_interval=10, portal_timeout=0)
        self.assertTrue(self.sim.run(wm, lambda: wm.portal_active, 60000))
        self.sim.portal.n_clients = 1
        W.set_up("Late", True)
        n = len(W.connect_calls)
        self.sim.run(wm, ms=100000)
        self.assertEqual(len(W.connect_calls), n)
        self.sim.portal.n_clients = 0
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))

    def test_portal_idle_timeout_then_reopens_after_more_failures(self):
        wm = self.sim.make([("Gone", "password1")], max_cycles=1, portal_timeout=60, portal_retry_interval=1000)
        self.assertTrue(self.sim.run(wm, lambda: wm.portal_active, 60000))
        self.assertTrue(self.sim.run(wm, lambda: not wm.portal_active, 120000))
        self.assertTrue(self.sim.run(wm, lambda: wm.portal_active, 120000))
        self.assertEqual(len(__import__("harness").StubPortal.instances), 2)

    def test_portal_timeout_does_not_close_portal_when_nothing_saved(self):
        wm = self.sim.make(None, portal_timeout=30)
        self.sim.run(wm, ms=200000)
        self.assertTrue(wm.portal_active)

    def test_exit_request_closes_portal_and_does_not_reopen_without_networks(self):
        wm = self.sim.make(None)
        self.sim.run(wm, ms=3000)
        self.assertTrue(wm.portal_active)
        wm.request_exit()
        self.sim.run(wm, ms=20000)
        self.assertFalse(wm.portal_active)

    def test_exit_resets_failed_cycles(self):
        wm = self.sim.make([("Gone", "password1")], config_mode="on_fail", max_cycles=3)
        self.assertTrue(self.sim.run(wm, lambda: wm.portal_active, 120000))
        wm.request_exit()
        self.assertTrue(self.sim.run(wm, lambda: not wm.portal_active, 20000))
        self.assertLessEqual(wm._failed_cycles, 1)       # closing resets it; the retry that follows may fail once
        # the portal must not come back after the first (or second) failed cycle ...
        for expected in (1, 2):
            self.assertTrue(self.sim.run(wm, lambda: wm._failed_cycles >= expected, 120000))
            self.assertFalse(wm.portal_active, "reopened after %d failed cycle(s)" % expected)
        # ... only after max_cycles new failures
        self.assertTrue(self.sim.run(wm, lambda: wm.portal_active, 120000))
        self.assertEqual(wm._failed_cycles, 3)

    def test_button_hold_opens_portal(self):
        W.add_ap("Home", "homepass12")
        wm = self.sim.make([("Home", "homepass12")], config_mode="never", button_pin=0, button_hold_ms=3000)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        machine.Pin.levels[0] = 0
        self.sim.run(wm, ms=20000)
        self.assertTrue(wm.portal_active)
        self.assertEqual(wm.state, "connected")        # portal open next to a healthy connection

    def test_start_and_stop_portal_manually(self):
        W.add_ap("Home", "homepass12")
        wm = self.sim.make([("Home", "homepass12")], config_mode="never", portal_timeout=0)
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        wm.start_portal()
        self.sim.run(wm, ms=10000)
        self.assertTrue(wm.portal_active)
        wm.stop_portal()
        self.sim.run(wm, ms=10000)
        self.assertFalse(wm.portal_active)

    # ---- networks API / switching ---------------------------------------------------------
    def test_network_api_persists(self):
        wm = self.sim.make([("Home", "homepass12")])
        wm.add_network("Office", "officepw12", hidden=True)
        wm.update_network("Home", password="newpass123", new_ssid="Home2")
        wm.remove_network("Office")
        wm2 = self.sim.make(None)
        self.assertEqual(wm2.networks(), [{"ssid": "Home2", "password": "newpass123", "hidden": False}])
        with self.assertRaises(TypeError):
            wm.update_network("Home2", colour="red")

    def test_public_networks_hide_passwords_by_default(self):
        wm = self.sim.make([("Home", "homepass12")])
        self.assertFalse("password" in wm.networks_public()[0])
        self.assertTrue(wm.networks_public()[0]["has_password"])
        wm = self.sim.make(None, show_passwords=True)
        self.assertEqual(wm.networks_public()[0]["password"], "homepass12")

    def test_reconnect_switches_network(self):
        W.add_ap("A", "passwordA", rssi=-40)
        W.add_ap("B", "passwordB", rssi=-70)
        wm = self.sim.make([("A", "passwordA"), ("B", "passwordB")])
        self.assertTrue(self.sim.run(wm, self.connected(wm), 60000))
        self.assertEqual(wm.ssid, "A")
        wm.reconnect("B")
        self.assertTrue(self.sim.run(wm, lambda: wm.is_connected() and wm.ssid == "B", 60000))

    def test_constructor_validation(self):
        for kw in ({"config_mode": "bogus"}, {"ap_password": "short"}, {"config_mode": "button"},
                   {"portal_timeout_action": "explode"}):
            with self.assertRaises(ValueError, msg=kw):
                WiFiManager(**kw)

    def test_log_file_rotates_and_never_contains_passwords(self):
        W.add_ap("Home", "homepass12")
        wm = self.sim.make([("Home", "homepass12")], log_file=self.sim.path("wifi.log"), log_max_size=300)
        wm.log.console = False
        self.sim.run(wm, self.connected(wm), 60000)
        for i in range(30):
            wm.log.info("filler line number %d", i)
        self.assertTrue(exists(self.sim.path("wifi.log.1")))
        self.assertFalse("homepass12" in "\n".join(wm.log.tail()))

    # ---- blocking / polling API ---------------------------------------------------------
    def test_blocking_connect(self):
        W.add_ap("Home", "homepass12")
        wm = self.sim.make([("Home", "homepass12")])
        self.assertTrue(wm.connect(timeout=60))
        self.assertEqual(wm.state, "connected")

    def test_blocking_connect_times_out(self):
        wm = self.sim.make([("Gone", "password1")], config_mode="never")
        t0 = self.sim.t
        self.assertFalse(wm.connect(timeout=20))
        self.assertGreaterEqual(self.sim.t - t0, 20000)

    def test_check_detects_loss_and_recovers(self):
        W.add_ap("Home", "homepass12")
        wm = self.sim.make([("Home", "homepass12")], config_mode="never", lost_grace=5)
        self.assertTrue(wm.connect(timeout=60))
        W.set_up("Home", False)
        seen = set()
        for _ in range(200):                  # your main loop: do work, call check(), sleep
            self.sim.advance(500)
            seen.add(wm.check())
        self.assertTrue({"lost", "backoff"} <= seen, seen)
        W.set_up("Home", True)
        for _ in range(400):
            self.sim.advance(500)
            wm.check()
        self.assertEqual(wm.state, "connected")

    def test_check_serves_the_portal_inside_the_call_until_it_closes(self):
        wm = self.sim.make([("Home", "homepass12")], config_mode="on_fail", max_cycles=1, portal_timeout=40)
        seen_portal = False
        for _ in range(200):                      # Home is not in range -> portal after the first failed cycle
            self.sim.advance(1000)
            before = self.sim.t
            wm.check()
            if self.sim.portal is not None and self.sim.portal.stopped:
                seen_portal = True
                self.assertGreaterEqual(self.sim.t - before, 30000)   # check() itself waited for the portal
                break
        self.assertTrue(seen_portal)

    def test_empty_scan_tries_saved_networks_anyway(self):
        W.aps.clear()                               # nothing visible at all (scan result is empty)
        W.add_ap("Home", "homepass12", rssi=-60)
        W.set_up("Home", False)
        wm = self.sim.make([("Home", "homepass12")], config_mode="never", connect_timeout=3, retries_per_network=1)
        self.sim.run(wm, ms=10000)
        self.assertTrue(W.connect_calls)            # blind attempt although the scan found nothing

    def test_coroutine_callbacks_run_in_blocking_mode(self):
        W.add_ap("Home", "homepass12")
        got = []

        async def on_connect(ssid, ip):
            got.append((ssid, ip))

        wm = self.sim.make([("Home", "homepass12")], on_connect=on_connect)
        self.assertTrue(wm.connect(timeout=60))
        self.assertEqual(got, [("Home", "192.168.1.50")])


if __name__ == "__main__":
    unittest.main()
