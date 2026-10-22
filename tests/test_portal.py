"""Portal tests: real asyncio, real sockets on localhost, fake radio (MicroPython unix port)."""
import _setup  # noqa: F401
import asyncio
import json
import socket
import time
import unittest

import network
from _util import async_test, rmtree, tmpdir
from wifimanager import WiFiManager, compat
from wifimanager.dns import DNSServer, build_reply

W = network.WORLD
PORT = 18080
DNS_PORT = 15353
INDEX = _setup.ROOT + "/wifimanager/www/index.html"


def make_manager(directory, networks=None, **kw):
    kw.setdefault("config_mode", "on_fail")
    kw.setdefault("portal_port", PORT)
    kw.setdefault("captive_portal", False)
    kw.setdefault("log_level", "DEBUG")
    wm = WiFiManager(networks, config_file=directory + "/wifi.json", **kw)
    wm.log.console = False
    return wm


async def http(method, path, body=None, host="192.168.4.1", ctype="application/json", raw_body=None, length=None):
    r, w = await asyncio.open_connection("127.0.0.1", PORT)
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
    req = "%s %s HTTP/1.1\r\nHost: %s\r\n" % (method, path, host)
    if method == "POST":
        req += "Content-Type: %s\r\nContent-Length: %d\r\n" % (ctype, len(data) if length is None else length)
    w.write(req.encode() + b"\r\n" + data)
    await w.drain()
    raw = await asyncio.wait_for(r.read(-1), 10)
    w.close()
    await w.wait_closed()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return int(lines[0].split()[1]), headers, payload


class PortalTests(unittest.TestCase):
    async def asyncSetUp(self):
        W.reset()
        W.ap_clients = [b"\x01" * 6]          # somebody is using the setup page: no background retries
        self.dir = tmpdir()
        self.old_linger = WiFiManager.PORTAL_LINGER_MS
        WiFiManager.PORTAL_LINGER_MS = 600
        self.wm = make_manager(self.dir)
        await self.wm.start()
        await self.until(lambda: self.wm.portal_active)
        await asyncio.sleep(0.2)

    async def asyncTearDown(self):
        self.wm.stop()
        await asyncio.sleep(0.1)
        WiFiManager.PORTAL_LINGER_MS = self.old_linger
        rmtree(self.dir)

    async def until(self, cond, timeout=10):
        end = time.ticks_add(time.ticks_ms(), int(timeout * 1000))
        while not cond():
            if time.ticks_diff(end, time.ticks_ms()) < 0:
                self.fail("timed out waiting for condition")
            await asyncio.sleep_ms(50)

    async def http(self, *args, **kw):
        return await http(*args, **kw)

    async def api(self, method, path, body=None, **kw):
        status, headers, payload = await self.http(method, "/api/" + path, body, **kw)
        return status, json.loads(payload)

    # ---- pages -----------------------------------------------------------------------------
    @async_test
    async def test_serves_the_web_page(self):
        status, headers, body = await self.http("GET", "/")
        with open(INDEX, "rb") as f:
            self.assertEqual(body, f.read())
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(int(headers["Content-Length"]), len(body))

    @async_test
    async def test_status(self):
        status, j = await self.api("GET", "status")
        self.assertEqual(status, 200)
        self.assertEqual((j["state"], j["portal"], j["connected"]), ("portal", True, False))
        self.assertEqual(j["ap_ssid"], "ESP32-Setup-ABCD")
        self.assertEqual(j["ap_ip"], "192.168.4.1")
        self.assertEqual(W.ap_config["ssid"], "ESP32-Setup-ABCD")
        self.assertEqual(W.ap_config["security"], network.WLAN.SEC_OPEN)

    # ---- captive portal ------------------------------------------------------------------
    @async_test
    async def test_captive_probes_are_redirected(self):
        for host, path in (("connectivitycheck.gstatic.com", "/generate_204"),
                           ("captive.apple.com", "/hotspot-detect.html"),
                           ("www.msftconnecttest.com", "/connecttest.txt"),
                           ("anything.example", "/whatever")):
            status, headers, _ = await self.http("GET", path, host=host)
            self.assertEqual(status, 302, host)
            self.assertEqual(headers["Location"], "http://192.168.4.1/")
        status, _, _ = await self.http("GET", "/generate_204", host="192.168.4.1")
        self.assertEqual(status, 302)

    # ---- API: networks ----------------------------------------------------------------------
    @async_test
    async def test_add_edit_delete_networks(self):
        status, j = await self.api("POST", "networks/save", {"ssid": "Home", "password": "homepass12", "hidden": False})
        self.assertEqual((status, j), (200, {"ok": True}))
        self.assertEqual(self.wm.networks(), [{"ssid": "Home", "password": "homepass12", "hidden": False}])
        # password never leaves the device by default
        _, j = await self.api("GET", "networks")
        self.assertEqual(j["networks"], [{"ssid": "Home", "hidden": False, "has_password": True, "connected": False}])
        # edit: rename + hidden, password omitted -> kept
        await self.api("POST", "networks/save", {"ssid": "Home2", "hidden": True, "original_ssid": "Home"})
        self.assertEqual(self.wm.networks(), [{"ssid": "Home2", "password": "homepass12", "hidden": True}])
        # edit: explicit new password
        await self.api("POST", "networks/save", {"ssid": "Home2", "password": "another-pw1", "original_ssid": "Home2"})
        self.assertEqual(self.wm.networks()[0]["password"], "another-pw1")
        # edit: open network
        await self.api("POST", "networks/save", {"ssid": "Home2", "password": "", "original_ssid": "Home2"})
        self.assertEqual(self.wm.networks()[0]["password"], "")
        # persisted as plain JSON
        with open(self.dir + "/wifi.json") as f:
            self.assertEqual(json.load(f)["networks"][0]["ssid"], "Home2")
        _, j = await self.api("POST", "networks/delete", {"ssid": "Home2"})
        self.assertTrue(j["ok"])
        self.assertEqual(self.wm.networks(), [])

    @async_test
    async def test_validation_errors_are_400(self):
        for body in ({"ssid": ""}, {"ssid": "x", "password": "short"}, {"ssid": "x" * 40},
                     {"ssid": "A", "original_ssid": "does-not-exist"}):
            status, j = await self.api("POST", "networks/save", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", j)

    @async_test
    async def test_show_passwords_option(self):
        self.wm.stop()
        await asyncio.sleep(0.2)
        self.wm = make_manager(self.dir, [("Home", "homepass12")], show_passwords=True, config_mode="always")
        await self.wm.start(wait=False)
        await self.until(lambda: self.wm.portal_active)
        await asyncio.sleep(0.2)
        _, j = await self.api("GET", "networks")
        self.assertTrue(j["show_passwords"])
        self.assertEqual(j["networks"][0]["password"], "homepass12")

    # ---- API: scan, logs, bad requests -------------------------------------------------------
    @async_test
    async def test_scan_lists_saved_flag_and_sorts_by_signal(self):
        W.add_ap("Far", "farpass123", rssi=-85)
        W.add_ap("Near", "nearpass12", rssi=-40, channel=11)
        W.add_ap("Free", "", rssi=-60)
        self.wm.add_network("Near", "nearpass12")
        status, j = await self.api("GET", "scan?refresh=1")
        self.assertEqual([n["ssid"] for n in j["networks"]], ["Near", "Free", "Far"])
        near = j["networks"][0]
        self.assertEqual((near["saved"], near["secure"], near["channel"]), (True, True, 11))
        self.assertFalse(j["networks"][1]["secure"])
        calls = W.scan_calls
        await self.api("GET", "scan")              # cached
        self.assertEqual(W.scan_calls, calls)

    @async_test
    async def test_scan_error_is_reported_with_cached_results(self):
        W.add_ap("Near", "nearpass12")
        await self.api("GET", "scan?refresh=1")
        W.scan_error = "Wifi Internal State Error"
        status, j = await self.api("GET", "scan?refresh=1")
        self.assertEqual(status, 200)
        self.assertIn("Wifi Internal State Error", j["error"])
        self.assertEqual(j["networks"][0]["ssid"], "Near")

    @async_test
    async def test_logs(self):
        _, j = await self.api("GET", "logs?n=5")
        self.assertTrue(0 < len(j["lines"]) <= 5)

    @async_test
    async def test_bad_requests(self):
        status, _, _ = await self.http("POST", "/api/networks/save", ctype="text/plain", raw_body=b'{"ssid":"x"}')
        self.assertEqual(status, 415)                       # blocks cross-site form posts
        status, _, _ = await self.http("POST", "/api/networks/save", raw_body=b"{nope")
        self.assertEqual(status, 400)
        # announced too big: refused before the body is read (sending it would only earn a TCP reset)
        status, _, _ = await self.http("POST", "/api/networks/save", raw_body=b"", length=5000)
        self.assertEqual(status, 413)
        status, _, _ = await self.http("PUT", "/api/status")
        self.assertEqual(status, 405)
        status, _, _ = await self.http("GET", "/api/nothing")
        self.assertEqual(status, 404)
        status, _, _ = await self.http("GET", "/nothing.js")
        self.assertEqual(status, 404)
        status, _ = await self.api("GET", "status")         # server survived all that
        self.assertEqual(status, 200)

    # ---- API: actions ------------------------------------------------------------------------
    @async_test
    async def test_connect_from_portal_then_portal_closes(self):
        W.add_ap("Home", "homepass12", delay_ms=300)
        await self.api("POST", "networks/save", {"ssid": "Home", "password": "homepass12"})
        status, j = await self.api("POST", "connect", {"ssid": "Home"})
        self.assertTrue(j["ok"])
        await self.until(lambda: self.wm.state == "connected", 15)
        self.assertEqual(self.wm.ssid, "Home")
        await self.until(lambda: not self.wm.portal_active, 5)   # lingers 0.6 s after success
        await asyncio.sleep_ms(100)                             # the cancelled server task closes its socket
        with self.assertRaises(OSError):                         # port released (a non-blocking connect
            await self.http("GET", "/")                          # only fails at the first read)

    @async_test
    async def test_exit_closes_portal_and_it_can_be_reopened(self):
        await self.api("POST", "exit", {})
        await self.until(lambda: not self.wm.portal_active)
        self.wm.start_portal()
        await self.until(lambda: self.wm.portal_active)
        await asyncio.sleep(0.2)
        status, _ = await self.api("GET", "status")
        self.assertEqual(status, 200)

    @async_test
    async def test_reboot_endpoint(self):
        resets = []
        self.wm._do_reset = lambda: resets.append(1) or setattr(self.wm, "_reset_at", None)
        await self.api("POST", "reboot", {})
        await self.until(lambda: resets, 6)

    @async_test
    async def test_async_callbacks(self):
        events = []

        async def on_portal(opened):
            events.append(opened)

        self.wm._cb_portal = on_portal
        await self.api("POST", "exit", {})
        await self.until(lambda: events == [False])


class DnsTests(unittest.TestCase):
    QUERY = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
             b"\x07example\x03com\x00\x00\x01\x00\x01")

    def test_a_query(self):
        reply = build_reply(self.QUERY, bytes([192, 168, 4, 1]))
        self.assertEqual(reply[:2], b"\x12\x34")
        self.assertEqual(reply[2:4], b"\x81\x80")
        self.assertEqual(reply[6:8], b"\x00\x01")          # one answer
        self.assertEqual(reply[-4:], bytes([192, 168, 4, 1]))

    def test_aaaa_gets_empty_answer(self):
        q = self.QUERY[:-4] + b"\x00\x1c\x00\x01"
        reply = build_reply(q, bytes([192, 168, 4, 1]))
        self.assertEqual(reply[6:8], b"\x00\x00")

    def test_garbage_and_responses_are_ignored(self):
        self.assertIsNone(build_reply(b"short", b"\x00" * 4))
        self.assertIsNone(build_reply(self.QUERY[:2] + b"\x81\x80" + self.QUERY[4:], b"\x00" * 4))
        self.assertIsNone(build_reply(self.QUERY[:20], b"\x00" * 4))

    @async_test
    async def test_udp_server(self):
        d = DNSServer("192.168.4.1", port=DNS_PORT)
        d.start()
        task = asyncio.create_task(d.serve())
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        s.sendto(self.QUERY, socket.getaddrinfo("127.0.0.1", DNS_PORT)[0][-1])
        data = None
        for _ in range(100):
            try:
                data = s.recvfrom(512)[0]
                break
            except OSError:  # not answered yet
                await asyncio.sleep_ms(20)
        s.close()
        self.assertEqual(data[-4:], bytes([192, 168, 4, 1]))
        d.stop()
        await asyncio.wait_for(task, 2)


class BlockingPortalTest(unittest.TestCase):
    """Sync API: connect() serves the portal while it waits (real event loop pumped by compat.run_for)."""

    def test_connect_serves_portal_until_configured(self):
        W.reset()
        W.add_ap("Home", "homepass12", delay_ms=200)
        d = tmpdir()
        old = WiFiManager.PORTAL_LINGER_MS
        WiFiManager.PORTAL_LINGER_MS = 300
        wm = make_manager(d)
        result = {}

        async def browser():  # runs inside the event loop that connect() pumps
            for _ in range(100):
                try:
                    result["page"] = (await http("GET", "/"))[2]
                    break
                except OSError:
                    await asyncio.sleep_ms(100)
            for path, body in (("/api/networks/save", {"ssid": "Home", "password": "homepass12"}),
                               ("/api/connect", {"ssid": "Home"})):
                result[path] = json.loads((await http("POST", path, body))[2])

        asyncio.create_task(browser())
        try:
            ok = wm.connect(timeout=30)
            self.assertTrue(ok)
            self.assertTrue(b"<html" in result["page"])
            self.assertEqual(result["/api/connect"], {"ok": True})
            self.assertEqual(wm.ssid, "Home")
            for _ in range(60):                     # portal lingers briefly, then check() closes it
                wm.check()
                if not wm.portal_active:
                    break
                time.sleep_ms(50)
            self.assertFalse(wm.portal_active)
        finally:
            wm.stop()
            compat.run_for(50)
            WiFiManager.PORTAL_LINGER_MS = old
            rmtree(d)


if __name__ == "__main__":
    unittest.main()
