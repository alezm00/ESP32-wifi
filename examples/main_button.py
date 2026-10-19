"""Config mode on demand: hold the BOOT button (GPIO0) for 3 s while the program is running.

Do not hold BOOT while resetting/powering the board: that starts the ROM bootloader.
Use another GPIO (wired to GND when pressed) if you want "hold at power-up".
"""
import asyncio

from wifimanager import WiFiManager

wm = WiFiManager(
    networks=[("MyHome", "my-home-password")],
    config_mode="button",             # the portal opens only when the button is held
    button_pin=0,
    button_hold_ms=3000,
    ap_password="12345678",
    portal_timeout=300,               # close the page after 5 min without visitors
    log_file="/wifi.log",             # optional persistent log (rotates at 8 kB)
    on_portal=lambda opened: print("config portal", "OPEN" if opened else "closed"),
)


async def main():
    await wm.start()
    while True:
        await asyncio.sleep(1)


asyncio.run(main())
