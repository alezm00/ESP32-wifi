"""Preview the configuration page on your PC (no ESP32 needed), with the MicroPython unix port.

    micropython tests/demo_portal.py [port]      then open http://127.0.0.1:8080/

A fake radio provides a few access points, so scanning, adding, editing and connecting all work.
"""
import _setup  # noqa: F401
import asyncio
import sys

import network
from _util import tmpdir
from wifimanager import WiFiManager

W = network.WORLD


async def main(port):
    W.reset()
    W.add_ap("HomeNet", "home-secret", rssi=-48, channel=6, delay_ms=1500)
    W.add_ap("Office-5G", "office-secret", rssi=-63, channel=36)
    W.add_ap("CoffeeShop", "", rssi=-71, channel=1)
    W.add_ap("Neighbour", "nope-nope-nope", rssi=-84, channel=11)
    W.ap_clients = [b"\x01" * 6]          # pretend a phone is attached to the setup network
    path = tmpdir() + "/wifi_config.json"
    wm = WiFiManager([("Office-5G", "wrong-password"), ("SecretSSID", "hidden-pass1", True)],
                     config_file=path, config_mode="always", portal_port=port, captive_portal=False,
                     hostname="esp32-demo", log_level="DEBUG")
    await wm.start(wait=False)
    print("Open http://127.0.0.1:%d/   (config stored in %s)" % (port, path))
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8080))
    except KeyboardInterrupt:
        pass
