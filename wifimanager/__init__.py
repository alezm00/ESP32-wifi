"""wifimanager - plug-and-play WiFi connection manager for MicroPython on ESP32.

    from wifimanager import WiFiManager
    wm = WiFiManager(networks=[("MyHome", "my-password")])
    wm.connect()
"""
from .manager import (WiFiManager, __version__, IDLE, CONNECTING, CONNECTED, LOST, BACKOFF, PORTAL,
                      MODE_ON_FAIL, MODE_ALWAYS, MODE_BUTTON, MODE_NEVER)

__all__ = ["WiFiManager", "IDLE", "CONNECTING", "CONNECTED", "LOST", "BACKOFF", "PORTAL",
           "MODE_ON_FAIL", "MODE_ALWAYS", "MODE_BUTTON", "MODE_NEVER"]
