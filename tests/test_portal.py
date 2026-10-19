"""Portal tests: real asyncio, real sockets on localhost, fake radio."""
import _setup  # noqa: F401
import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.request

import machine
import network
from wifimanager import WiFiManager, compat
from wifimanager.dns import DNSServer, build_reply

W = network.WORLD
PORT = 18080
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wifimanager", "www", "index.html")


def make_manager(directory, networks=None, **kw):
    kw.setdefault("config_mode", "on_fail")
    kw.setdefault("portal_port", PORT)
    kw.setdefault("captive_portal", False)
    kw.setdefault("log_level", "DEBUG")
    wm = WiFiManager(networks, config_file=os.path.join(directory, "wifi.json"), **kw)
    wm.log.console = False
    return wm


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        W.reset()
        W.ap_clients = [b"\x01" * 6]          # somebody is using the setup page: no background retries
        self.dir = tempfile.mkdtemp()
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
        shutil.rmtree(self.dir, ignore_errors=True)

    async def until(self, cond, timeout=10):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                self.fail("timed out waiting for condition")
            await asyncio.sleep(0.05)

    async def http(self, method, path, body=None, host="192.168.4.1", ctype="application/json", raw_body=None):
        r, w = await asyncio.open_connection("127.0.0.1", PORT)
        data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
        req = "%s %s HTTP/1.1\r\nHost: %s\r\n" % (method, path, host)
        if method == "POST":
            req += "Content-Type: %s\r\nContent-Length: %d\r\n" % (ctype, len(data))
        w.write(req.encode() + b"\r\n" + data)
        await w.drain()
        raw = await asyncio.wait_for(r.read(), 10)
        w.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = dict(l.split(": ", 1) for l in lines[1:])
        return int(lines[0].split()[1]), headers, payload

    async def api(self, method, path, body=None, **kw):
        status, headers, payload = await self.http(method, "/api/" + path, body, **kw)
        return status, json.loads(payload)

    # ---- pages -----------------------------------------------------------------------------
    async def test_serves_the_web_page(self):
        status, headers, body = await self.http("GET", "/")
        with open(INDEX, "rb") as f:
            self.assertEqual(body, f.read())
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(int(headers["Content-Length"]), len(body))

    async def test_status(self):
        status, j = await self.api("GET", "status")
        self.assertEqual(status, 200)
        self.assertEqual((j["state"], j["portal"], j["connected"]), ("portal", True, False))
        self.assertEqual(j["ap_ssid"], "ESP32-Setup-ABCD")
        self.assertEqual(j["ap_ip"], "192.168.4.1")
        self.assertEqual(W.ap_config["essid"], "ESP32-Setup-ABCD")
        self.assertEqual(W.ap_config["authmode"], network.AUTH_OPEN)

    # ---- captive portal ------------------------------------------------------------------
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
        with open(os.path.join(self.dir, "wifi.json")) as f:
            self.assertEqual(json.load(f)["networks"][0]["ssid"], "Home2")
        _, j = await self.api("POST", "networks/delete", {"ssid": "Home2"})
        self.assertTrue(j["ok"])
        self.assertEqual(self.wm.networks(), [])

    async def test_validation_errors_are_400(self):
        for body in ({"ssid": ""}, {"ssid": "x", "password": "short"}, {"ssid": "x" * 40},
                     {"ssid": "A", "original_ssid": "does-not-exist"}):
            status, j = await self.api("POST", "networks/save", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", j)

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

    async def test_scan_error_is_reported_with_cached_results(self):
        W.add_ap("Near", "nearpass12")
        await self.api("GET", "scan?refresh=1")
        W.scan_error = "Wifi Internal State Error"
        status, j = await self.api("GET", "scan?refresh=1")
        self.assertEqual(status, 200)
        self.assertIn("Wifi Internal State Error", j["error"])
        self.assertEqual(j["networks"][0]["ssid"], "Near")

    async def test_logs(self):
        _, j = await self.api("GET", "logs?n=5")
        self.assertTrue(0 < len(j["lines"]) <= 5)

    async def test_bad_requests(self):
        status, _, _ = await self.http("POST", "/api/networks/save", ctype="text/plain", raw_body=b'{"ssid":"x"}')
        self.assertEqual(status, 415)                       # blocks cross-site form posts
        status, _, _ = await self.http("POST", "/api/networks/save", raw_body=b"{nope")
        self.assertEqual(status, 400)
        status, _, _ = await self.http("POST", "/api/networks/save", raw_body=b"x" * 5000)
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
    async def test_connect_from_portal_then_portal_closes(self):
        W.add_ap("Home", "homepass12", delay_ms=300)
        await self.api("POST", "networks/save", {"ssid": "Home", "password": "homepass12"})
        status, j = await self.api("POST", "connect", {"ssid": "Home"})
        self.assertTrue(j["ok"])
        await self.until(lambda: self.wm.state == "connected", 15)
        self.assertEqual(self.wm.ssid, "Home")
        await self.until(lambda: not self.wm.portal_active, 5)   # lingers 0.6 s after success
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", PORT)     # port released

    async def test_exit_closes_portal_and_it_can_be_reopened(self):
        await self.api("POST", "exit", {})
        await self.until(lambda: not self.wm.portal_active)
        self.wm.start_portal()
        await self.until(lambda: self.wm.portal_active)
        await asyncio.sleep(0.2)
        status, _ = await self.api("GET", "status")
        self.assertEqual(status, 200)

    async def test_reboot_endpoint(self):
        resets = []
        self.wm._do_reset = lambda: resets.append(1) or setattr(self.wm, "_reset_at", None)
        await self.api("POST", "reboot", {})
        await self.until(lambda: resets, 6)

    async def test_async_callbacks(self):
        events = []

        async def on_portal(opened):
            events.append(opened)

        self.wm._cb_portal = on_portal
        await self.api("POST", "exit", {})
        await self.until(lambda: events == [False])


class DnsTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_udp_server(self):
        d = DNSServer("192.168.4.1", port=0)
        d.start()
        port = d._sock.getsockname()[1]
        task = asyncio.create_task(d.serve())

        def ask():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            s.sendto(self.QUERY, ("127.0.0.1", port))
            data = s.recvfrom(512)[0]
            s.close()
            return data

        data = await asyncio.get_running_loop().run_in_executor(None, ask)
        self.assertEqual(data[-4:], bytes([192, 168, 4, 1]))
        d.stop()
        await asyncio.wait_for(task, 2)


class BlockingPortalTest(unittest.TestCase):
    """Sync API: connect() serves the portal while it waits (real event loop pumped by compat.run_for)."""

    def test_connect_serves_portal_until_configured(self):
        W.reset()
        W.add_ap("Home", "homepass12", delay_ms=200)
        d = tempfile.mkdtemp()
        old = WiFiManager.PORTAL_LINGER_MS
        WiFiManager.PORTAL_LINGER_MS = 300
        wm = make_manager(d)
        result = {}

        def browser():
            base = "http://127.0.0.1:%d" % PORT
            for _ in range(100):
                try:
                    result["page"] = urllib.request.urlopen(base + "/", timeout=2).read()
                    break
                except Exception:
                    time.sleep(0.1)
            for path, body in (("/api/networks/save", {"ssid": "Home", "password": "homepass12"}),
                               ("/api/connect", {"ssid": "Home"})):
                req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                             {"Content-Type": "application/json"})
                result[path] = json.load(urllib.request.urlopen(req, timeout=5))

        t = threading.Thread(target=browser)
        t.start()
        try:
            ok = wm.connect(timeout=30)
            t.join(5)
            self.assertTrue(ok)
            self.assertIn(b"<html", result["page"])
            self.assertEqual(wm.ssid, "Home")
            for _ in range(60):                     # portal lingers briefly, then check() closes it
                wm.check()
                if not wm.portal_active:
                    break
                time.sleep(0.05)
            self.assertFalse(wm.portal_active)
        finally:
            wm.stop()
            compat.run_for(50)
            WiFiManager.PORTAL_LINGER_MS = old
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
