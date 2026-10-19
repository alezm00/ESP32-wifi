"""Plain blocking script, no asyncio in your code.

connect() blocks until connected (serving the config page while it waits if needed).
check() in the main loop detects loss and reconnects.
"""
import time

from wifimanager import WiFiManager

wm = WiFiManager(networks=[("MyHome", "my-home-password")], config_mode="on_fail")

wm.connect()                          # or wm.connect(timeout=60) -> False if still offline

while True:
    wm.check()                        # cheap: does nothing unless a check is due. Call it often.
    if wm.is_connected():
        print("hello from", wm.ip)
    time.sleep(1)
