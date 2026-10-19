"""Smoke test that also runs on the real MicroPython interpreter (unix port) with the fake radio.

    MICROPYPATH=.frozen:tests/fakes:. micropython tests/mp_smoke.py  (from the repository root)
    PYTHONPATH=tests/fakes:. python3 tests/mp_smoke.py              (same test on CPython)
"""
import json
import os
import sys

import network
from wifimanager import WiFiManager, compat

W = network.WORLD
TMP = os.getenv("WM_TMP") or "/tmp"
CFG = TMP + "/wm_smoke.json"
PORT = 18081
failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def cleanup():
    for suffix in ("", ".tmp", ".bad"):
        try:
            os.remove(CFG + suffix)
        except OSError:
            pass


# ---- part 1: supervisor on a virtual clock --------------------------------------------------
clock = [100000]
compat.ticks_ms = lambda: clock[0]


def drive(wm, until, limit_ms):
    wm._boot()
    end = clock[0] + limit_ms
    while clock[0] < end:
        step = wm._next()
        clock[0] += step
        wm._kick = False
        if until():
            return True
    return False


def virtual_part():
    cleanup()
    W.reset()
    W.add_ap("Weak", "weakpass1", rssi=-80)
    W.add_ap("Strong", "strongpass1", rssi=-45)
    events = []
    wm = WiFiManager([("Weak", "weakpass1"), ("Strong", "strongpass1"), ("Hidden", "hiddenpass1", True)],
                     config_file=CFG, config_mode="never", lost_grace=10, log_level="INFO",
                     on_connect=lambda s, ip: events.append(("up", s, ip)),
                     on_disconnect=lambda r: events.append(("down", r)))
    wm.log.console = False
    check("connects to the strongest network", drive(wm, lambda: wm.state == "connected", 60000) and wm.ssid == "Strong")
    check("on_connect fired", events == [("up", "Strong", "192.168.1.50")])
    n = len(W.connect_calls)

    W.set_up("Strong", False)                      # short drop: the driver recovers
    drive(wm, lambda: False, 4000)
    W.set_up("Strong", True)
    check("driver recovery", drive(wm, lambda: wm.state == "connected" and len(events) == 3, 30000)
          and len(W.connect_calls) == n)

    W.set_up("Strong", False)                      # long outage: manager takes over, fails over to "Weak"
    drive(wm, lambda: False, 90000)
    check("fails over to the other saved network", wm.state == "connected" and wm.ssid == "Weak")
    W.set_up("Weak", False)                        # now nothing is left: lost -> backoff
    drive(wm, lambda: False, 90000)
    check("loss detected, backing off", wm.state in ("backoff", "connecting", "lost") and not wm.is_connected())
    W.set_up("Strong", True)
    check("reconnects when a network is back", drive(wm, lambda: wm.state == "connected" and wm.ssid == "Strong", 300000))
    check("config file is plain JSON", json.loads(open(CFG).read())["networks"][0]["password"] == "weakpass1")

    wm2 = WiFiManager(None, config_file=CFG, config_mode="never")
    wm2.log.console = False
    check("config reload", [x["ssid"] for x in wm2.networks()] == ["Weak", "Strong", "Hidden"])
    wm2.update_network("Weak", new_ssid="Weak2", password="newpass123")
    wm2.remove_network("Hidden")
    check("edit/remove", [x["ssid"] for x in wm2.networks()] == ["Weak2", "Strong"])


# ---- part 2: real event loop, real sockets, the portal --------------------------------------
async def http(method, path, body=None, host="192.168.4.1"):
    import asyncio
    r, w = await asyncio.open_connection("127.0.0.1", PORT)
    data = json.dumps(body).encode() if body is not None else b""
    req = "%s %s HTTP/1.1\r\nHost: %s\r\n" % (method, path, host)
    if method == "POST":
        req += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(data)
    w.write(req.encode() + b"\r\n" + data)
    await w.drain()
    raw = await r.read(-1)
    w.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), head, payload


async def portal_part():
    import asyncio
    # real clock again
    import time
    compat.ticks_ms = time.ticks_ms if hasattr(time, "ticks_ms") else (lambda: int(time.monotonic() * 1000))
    cleanup()
    W.reset()
    W.add_ap("Home", "homepass12", delay_ms=300, rssi=-50)
    WiFiManager.PORTAL_LINGER_MS = 500
    states = []
    wm = WiFiManager(None, config_file=CFG, portal_port=PORT, captive_portal=False, log_level="INFO",
                     on_state=lambda o, n: states.append(n))
    wm.log.console = False
    W.ap_clients = [b"\x01" * 6]
    await wm.start()
    await compat.sleep_ms(300)
    check("portal opened (no saved network)", wm.portal_active and wm.state == "portal")

    status, head, body = await http("GET", "/")
    check("serves index.html", status == 200 and b"<html" in body and len(body) > 5000)
    status, head, body = await http("GET", "/generate_204", host="connectivitycheck.gstatic.com")
    check("captive redirect", status == 302 and b"Location: http://192.168.4.1/" in head)
    status, _, body = await http("POST", "/api/networks/save", {"ssid": "Home", "password": "homepass12"})
    check("save network", status == 200)
    status, _, body = await http("GET", "/api/networks")
    check("list hides password", json.loads(body)["networks"][0] == {
        "ssid": "Home", "hidden": False, "has_password": True, "connected": False})
    status, _, body = await http("POST", "/api/networks/save", {"ssid": "x", "password": "short"})
    check("validation error is 400", status == 400)
    status, _, body = await http("GET", "/api/scan?refresh=1")
    check("scan", status == 200 and json.loads(body)["networks"][0]["ssid"] in ("Home", "Neighbor"))
    await http("POST", "/api/connect", {"ssid": "Home"})
    for _ in range(100):
        if wm.state == "connected" and not wm.portal_active:
            break
        await compat.sleep_ms(100)
    check("connected via portal request", wm.state == "connected" and wm.ssid == "Home")
    check("portal closed after success", not wm.portal_active)
    check("state sequence", states[:2] == ["portal", "connecting"] or "connected" in states)
    wm.stop()
    await compat.sleep_ms(100)


virtual_part()
import asyncio  # noqa: E402

asyncio.run(portal_part())
cleanup()
print("---")
print("FAILED: %s" % failures if failures else "ALL PASSED")
sys.exit(1 if failures else 0)
