"""asyncio application: WiFi is supervised in the background, your tasks just keep running.

Copy the `wifimanager/` folder to /lib/ on the board, this file to /main.py.
"""
import asyncio

from wifimanager import WiFiManager

wm = WiFiManager(
    networks=[("MyHome", "my-home-password"), ("Office", "office-password")],  # default networks
    config_mode="on_fail",          # portal opens if no network works (or none is saved)
    ap_ssid="MyDevice-Setup",
    ap_password="12345678",         # protects the setup page (WPA2); None = open AP
    hostname="my-device",
    on_connect=lambda ssid, ip: print("ONLINE:", ssid, ip),
    on_disconnect=lambda reason: print("OFFLINE:", reason),
)


async def worker():
    while True:
        if wm.is_connected():
            print("doing network stuff, RSSI", wm.rssi())
        else:
            print("offline, buffering data...")
        await asyncio.sleep(10)


async def main():
    await wm.start()                 # first connection attempt, then supervision continues in the background
    await worker()


asyncio.run(main())
