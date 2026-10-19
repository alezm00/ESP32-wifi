# wifimanager — plug-and-play WiFi manager for MicroPython on ESP32

One folder, no dependencies. It gives your project:

* **Automatic connection** to a list of saved networks (strongest signal first, hidden SSIDs last)
* **Retry** with per-network retries and exponential backoff (+ jitter)
* **Connection-loss detection** and recovery (the WiFi driver gets a head start, then the manager takes over)
* **Logs** to the console, to a RAM buffer shown in the web page, and optionally to a rotating file
* **Configuration page** served by the ESP32 itself (access point + captive portal): scan, add manually, edit, delete, "connect now"
* **Plain JSON config** in the flash (`/wifi_config.json`), editable by hand

Works with `asyncio` (background supervision) **and** with plain blocking scripts.

---

## 1. Install

Copy the `wifimanager/` folder to the board, into `/lib` (or next to `main.py`):

```bash
pip install mpremote
mpremote fs mkdir :lib
mpremote cp -r wifimanager :lib/
```

Or let `mip` fetch it from GitHub (it installs into `/lib/wifimanager/`):

```bash
mpremote mip install github:alezm00/ESP32-wifi
```

Other packages can list it as a dependency in their own `package.json`: `"deps": [["github:alezm00/ESP32-wifi", "main"]]`.

The folder contains `*.py` modules and `www/index.html` (the web page, streamed from flash, never loaded in RAM).
Everything else in this repository (`examples/`, `tests/`) is for you / development only.

Requirements: MicroPython ≥ 1.20 for ESP32 (works on ESP32, S2, S3, C3). Flash use ≈ 58 kB of `.py` + 12 kB web page
(≈ 23 kB of code when compiled with `mpy-cross`). RAM use was not measured on a board; the portal is imported only while config mode is open to keep normal running light.

## 2. Quick start

```python
# main.py
from wifimanager import WiFiManager

wm = WiFiManager(networks=[("MyHome", "my-password"), ("Office", "other-password")])
wm.connect()                 # blocks until connected (opens the config page if it cannot)

print("IP:", wm.ip)

while True:
    wm.check()               # detects loss + reconnects; call it often
    ...                      # your code
```

asyncio version:

```python
import asyncio
from wifimanager import WiFiManager

wm = WiFiManager(networks=[("MyHome", "my-password")])

async def main():
    await wm.start()         # first attempt, then supervision runs as a background task
    while True:
        print("connected" if wm.is_connected() else "offline")
        await asyncio.sleep(5)

asyncio.run(main())
```

See `examples/` for complete scripts (asyncio, blocking, button-triggered config mode).

## 3. How the configuration mode works

When the portal opens, the ESP32 creates its own WiFi access point (default name `ESP32-Setup-XXXX`, XXXX = end of the MAC).
Connect your phone/PC to it; a captive-portal popup opens the page by itself, otherwise browse to **http://192.168.4.1/**.

The page has:

| Section | What you can do |
|---|---|
| Status | state, current SSID, IP, signal, last error |
| Available networks | scan (button), see signal / security / "saved", tap **Add** or **Edit** |
| Saved networks | **Connect** now, **Edit**, **Delete** |
| Add / Edit form | SSID, password (show/hide), hidden flag, "no password (open network)" |
| Log | last log lines from the device |
| Exit / Reboot | leave config mode, or reboot the board |

When to open it is chosen with `config_mode` in the constructor:

| `config_mode` | Behaviour |
|---|---|
| `"on_fail"` (default) | opens when **no network is saved** or after `max_cycles` failed connection cycles. While open it still retries the saved networks every `portal_retry_interval` s (only if nobody is using the page), and closes itself when the connection succeeds. |
| `"always"` | the portal runs permanently next to the normal connection (AP+STA). Handy while developing. |
| `"button"` | only when `button_pin` is held for `button_hold_ms`. |
| `"never"` | only if your code calls `wm.start_portal()`. |

A held button (`button_pin=`) works in **every** mode. The portal closes after `portal_timeout` s without visitors
(`portal_timeout_action="retry"` keeps going, `"reboot"` restarts the board). It is never closed by timeout while nothing is saved.

> The ESP32 radio has one channel. When the ESP32 (re)connects to your router the setup network may jump channel and your phone can drop for a moment:
> reconnect to the setup network and reload the page. For this reason background retries wait while a client is attached to the setup network.

**Security:** protect the setup network with `ap_password` (WPA2, 8–63 chars). Saved passwords are never sent to the page unless
`show_passwords=True` (the edit form shows an empty field: leave it empty to keep the current password). The page API only accepts JSON `POST`s.
Passwords are stored in **plain text** in the JSON file, as requested; anybody with USB access to the board can read them.

## 4. Constructor reference

`WiFiManager(networks=None, *, ...)` — everything after `networks` is keyword-only.

**Networks / storage**

| Option | Default | Meaning |
|---|---|---|
| `networks` | `None` | default networks: list of `("ssid", "password")`, `("ssid", "password", hidden)`, `{"ssid":..,"password":..,"hidden":..}` or `"ssid"` (open) |
| `config_file` | `"/wifi_config.json"` | where the JSON is stored |
| `force_defaults` | `False` | `True`: the defaults overwrite saved entries with the same SSID at every boot |

Defaults are imported **once per SSID**: edits made in the portal are kept, networks you delete do not come back, and
networks you add to the list in a new firmware version are imported at the next boot.

**Config mode**

| Option | Default | Meaning |
|---|---|---|
| `config_mode` | `"on_fail"` | `"on_fail"`, `"always"`, `"button"`, `"never"` |
| `button_pin` / `button_hold_ms` / `button_active_low` | `None` / `3000` / `True` | button that opens the portal |
| `ap_ssid` / `ap_password` | `None` / `None` | setup network name (default `ESP32-Setup-XXXX`) and WPA2 password (`None` = open) |
| `portal_port` | `80` | HTTP port |
| `captive_portal` | `True` | answer all DNS queries with the AP address so phones open the page automatically |
| `portal_timeout` | `300` | seconds without visitors before closing (`0` = never) |
| `portal_timeout_action` | `"retry"` | or `"reboot"` |
| `portal_retry_interval` | `120` | seconds between background retries while the portal is open |
| `show_passwords` | `False` | show saved passwords in the page |

**Connection**

| Option | Default | Meaning |
|---|---|---|
| `hostname` | `"esp32"` | DHCP hostname |
| `connect_timeout` | `15` | seconds to wait for one connection attempt |
| `retries_per_network` | `2` | attempts per network per cycle (a wrong password is not retried) |
| `max_cycles` | `3` | failed cycles (all networks tried) before `on_fail` opens the portal |
| `driver_reconnects` | `3` | `wlan.config(reconnects=n)`: quick automatic re-associations done by the ESP-IDF driver |
| `lost_grace` | `10` | seconds the manager lets the driver recover before it takes over |
| `check_interval` | `5` | seconds between health checks while connected |
| `backoff_base` / `backoff_max` | `2` / `300` | wait between failed cycles: 2, 4, 8 … s, capped, ±20 % jitter |
| `reboot_after` | `0` | reboot after this many seconds offline (`0` = never; last resort) |
| `internet_check_host` | `None` | e.g. `"8.8.8.8:53"`: TCP probe to detect "associated but no internet" |
| `internet_check_interval` / `internet_check_failures` | `60` / `3` | probe period and failures before a forced reconnect |

**Logging**

| Option | Default | Meaning |
|---|---|---|
| `log_level` | `"INFO"` | `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"` |
| `log_file` | `None` | e.g. `"/wifi.log"`; rotates to `/wifi.log.1` at `log_max_size` bytes (default 8192). Flash wear: keep the level at INFO or above |
| `log_buffer` | `60` | lines kept in RAM for the web page and `wm.log.tail()` |

`wm.log.console = False` silences `print()`. Your app can log too: `wm.log.info("temperature %d", t)`. Passwords are never logged.

**Callbacks** (plain functions or `async def`): `on_connect(ssid, ip)`, `on_disconnect(reason)`, `on_portal(opened: bool)`, `on_state(old, new)`.

## 5. API

| Call | Description |
|---|---|
| `await wm.start(wait=True)` | asyncio: start supervision; with `wait` returns after the first attempt (`True` if connected) |
| `await wm.wait_connected(timeout=None)` | asyncio: wait until connected |
| `wm.connect(timeout=None)` | blocking: returns `True` when connected, `False` after `timeout` s. Serves the portal meanwhile |
| `wm.check()` | blocking style: one supervision step, returns the state. Call it every loop |
| `wm.stop(disconnect=False)` | stop supervising (and optionally switch the radio off) |
| `wm.start_portal()` / `wm.stop_portal()` | open / close config mode by code |
| `wm.reconnect(ssid=None)` | drop the connection and reconnect now (optionally to one network) |
| `wm.state` | `"idle"`, `"connecting"`, `"connected"`, `"lost"`, `"backoff"`, `"portal"` |
| `wm.is_connected()`, `wm.ssid`, `wm.ip`, `wm.rssi()`, `wm.wlan`, `wm.portal_active`, `wm.status()` | live information |
| `wm.networks()` | saved networks (list of dicts) |
| `wm.add_network(ssid, password="", hidden=False)` | add or replace |
| `wm.update_network(ssid, password=.., hidden=.., new_ssid=..)` | edit |
| `wm.remove_network(ssid)` | delete |
| `wm.scan(force=True)` | `[{ssid, rssi, channel, secure}]`, strongest first (blocks 2–4 s) |

Blocking mode and the portal: a portal opened by a failure, the button or `start_portal()` is served *inside* `connect()`/`check()`
and blocks your loop until it closes (your own code does not run during that time). In `"always"` mode `check()` serves it in short slices instead, so call `check()` often.
If you need your code to run while the portal is open, use the asyncio API.

### The JSON file

```json
{
 "version": 1,
 "networks": [
  {"ssid": "MyHome", "password": "my-password", "hidden": false},
  {"ssid": "Office", "password": "other-password", "hidden": false}
 ],
 "defaults_seen": ["MyHome", "Office"]
}
```

Writes are atomic (`.tmp` file, then rename). A power cut cannot lose the file: a leftover `.tmp` is recovered at boot, a corrupt file is moved to `.bad`.
Delete the file (`mpremote rm :wifi_config.json`) to start from the defaults again.

## 6. Reconnection and loss detection — how it works and why

MicroPython gives Python code **no WiFi event callback**, so loss is detected by polling `wlan.isconnected()` (cheap, no radio traffic).
The recipe implemented here is the one that is robust in practice:

1. **Poll** every `check_interval` s while connected (1 s while the portal is open).
2. **Let the driver try first.** Brief drops (router busy, one missed beacon) are fixed by ESP-IDF in a second or two using `reconnects=N`.
   The manager waits `lost_grace` s and only logs "recovered by the WiFi driver" if that worked: no scanning, no channel hopping, no churn.
3. **Take over.** Disconnect, **scan**, and try the saved networks that are visible, strongest first; hidden networks last (they do not show in scans).
   Each network gets `retries_per_network` attempts of `connect_timeout` s; a "wrong password" status stops retrying that network immediately.
   If the scan fails or is empty, all saved networks are tried blindly in saved order.
4. **Exponential backoff + jitter** between failed cycles (2 → 4 → 8 … ≤ 300 s). It saves power, does not hammer a router that is rebooting and
   avoids many devices retrying at the same instant after a power cut.
5. **Optional internet probe** (`internet_check_host`) — catches "connected to the AP but no uplink", which `isconnected()` cannot see.
6. **Fall back to config mode** (`on_fail`) after `max_cycles`, but keep retrying every `portal_retry_interval` s, so a device that booted before its router
   heals itself without anybody touching it.
7. **Reboot only as a last resort** (`reboot_after`): a reboot loses state and is rarely needed.

There is no roaming: once connected the manager stays on the current network until it is lost.

## 7. Function or class? Design notes

* **One class (`WiFiManager`)**, one public import. WiFi handling is *state*: radio handles, retry counters, a state machine, config, callbacks, a background task.
  A class keeps this in one place, lets you configure everything once in `__init__`, and keeps several-instance/testing possible. Loose functions would need module-level globals.
* **Small internal modules** (`config`, `log`, `portal`, `dns`, `compat`) keep each concern separate and let the RAM-hungry portal be **imported lazily**, only while config mode is open.
* **One supervisor, three drivers.** The whole retry/loss logic is a single generator that yields "sleep N ms". `await wm.start()`, `wm.connect()` and `wm.check()` are thin drivers
  of the same code, so blocking and asyncio users get identical behaviour.

## 8. Troubleshooting

| Symptom | Hint |
|---|---|
| Never connects, log says `wrong password` | edit the network in the portal; WPA2 passwords are 8–64 characters |
| `no saved network in range` | the SSID is not visible (2.4 GHz only! ESP32 cannot see 5 GHz); for a hidden SSID tick "Hidden network" |
| `config portal: cannot listen on port 80` | something else uses the port: set `portal_port=8080` |
| Phone says "no internet" on the setup network | normal; stay connected and open `http://192.168.4.1/` |
| The page stops answering during a scan | a scan blocks the radio for 2–4 s; wait and retry |
| Holding BOOT at power-up enters the bootloader | hold GPIO0 only **while running**, or use another pin |
| `ENOMEM` when the portal opens | free RAM before: `import gc; gc.collect()`, compile the library with `mpy-cross` or freeze it in the firmware |
| Need quieter logs | `log_level="WARNING"` and/or `wm.log.console = False` |

Use `log_level="DEBUG"` to see every state change and every attempt.

## 9. Development and tests (host, no board needed)

```bash
python3 -m unittest discover -s tests         # 60+ tests: config, supervisor on a virtual clock, portal over real sockets
python3 tests/demo_portal.py                  # preview the web page on http://127.0.0.1:8080/ with a fake radio
```

`tests/fakes/` contains a fake `network` / `machine`. `tests/mp_smoke.py` also runs on the **real MicroPython** (unix port):

```bash
MICROPYPATH=.frozen:tests/fakes:. micropython tests/mp_smoke.py
```

### On-device checklist (needs a board)

1. `mpremote cp -r wifimanager :lib/` and run `examples/main_async.py`.
2. Switch the router off: log shows `connection lost`, then retries with backoff; switch it on: it reconnects by itself.
3. Boot with an empty config (or wrong password): the `ESP32-Setup-XXXX` network appears, the page opens, add/edit a network and press **Connect**.
4. Reboot and check that `/wifi_config.json` kept your changes.

## 10. License

MIT — use it freely in your projects.
