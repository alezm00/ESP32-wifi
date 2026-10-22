"""Configuration portal: access point + captive DNS + tiny HTTP server + JSON API.

Imported lazily by WiFiManager, only when the portal is opened (saves RAM otherwise).
"""
import gc

import network

import asyncio
import json

from . import compat as C

_DIR = __file__.rsplit("/", 1)[0]

_MAX_BODY = 2048
_MAX_HEADERS = 3072

_REASONS = {200: "OK", 204: "No Content", 302: "Found", 400: "Bad Request", 404: "Not Found",
            405: "Method Not Allowed", 413: "Payload Too Large", 415: "Unsupported Media Type",
            431: "Request Header Fields Too Large", 500: "Internal Server Error",
            503: "Service Unavailable"}


class HttpError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _unquote(s):
    if "%" not in s and "+" not in s:
        return s
    s = s.replace("+", " ")
    parts = s.split("%")
    out = bytearray(parts[0].encode())
    for p in parts[1:]:
        try:
            out.append(int(p[:2], 16))
            out.extend(p[2:].encode())
        except ValueError:
            out.extend(b"%" + p.encode())
    return out.decode()


def _query(qs):
    out = {}
    for pair in qs.split("&"):
        if pair:
            k, _, v = pair.partition("=")
            out[_unquote(k)] = _unquote(v)
    return out


def _looks_like_ip(host):
    return host.replace(".", "").isdigit()


class Portal:
    def __init__(self, mgr):
        self.mgr = mgr
        self.log = mgr.log
        self.ap_ssid = mgr.ap_ssid
        self.ap_ip = "192.168.4.1"
        self.last_activity = C.ticks_ms()
        self.ap = None
        self._server = None
        self._dns = None
        self._tasks = []
        self._running = False

    # ---- lifecycle -----------------------------------------------------------------------
    def start(self):
        m = self.mgr
        ap = network.WLAN(network.WLAN.IF_AP)
        ap.active(True)
        if not self.ap_ssid:
            mac = m.wlan.config("mac")
            self.ap_ssid = "ESP32-Setup-%02X%02X" % (mac[4], mac[5])
        if m.ap_password:
            ap.config(ssid=self.ap_ssid, key=m.ap_password, security=network.WLAN.SEC_WPA2)
        else:
            ap.config(ssid=self.ap_ssid, security=network.WLAN.SEC_OPEN)
        # ifconfig(), not ipconfig(): only its 4th item sets the DNS server the AP's DHCP hands to
        # clients, and the captive portal needs that to be us
        ap.ifconfig((self.ap_ip, "255.255.255.0", self.ap_ip, self.ap_ip))
        self.ap = ap
        self._running = True
        self.last_activity = C.ticks_ms()
        if m.captive_portal:
            try:
                from .dns import DNSServer
                self._dns = DNSServer(self.ap_ip)
                self._dns.start()
                self._tasks.append(C.create_task(self._dns.serve()))
            except Exception as e:
                self.log.warning("captive DNS unavailable: %s", C.exc_text(e))
                self._dns = None
        self._tasks.append(C.create_task(self._serve()))

    def stop(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        if self._dns is not None:
            self._dns.stop()
            self._dns = None
        for t in self._tasks:
            try:
                t.cancel()
            except Exception:
                pass
        self._tasks = []
        try:
            self.ap.active(False)
        except Exception:
            pass

    def clients(self):
        try:
            return len(self.ap.status("stations"))
        except Exception:
            return 0

    def touch(self):
        self.last_activity = C.ticks_ms()

    async def _serve(self):
        try:
            self._server = await asyncio.start_server(self._client, "0.0.0.0", self.mgr.portal_port)
        except Exception as e:
            self.log.error("portal: cannot listen on port %d: %s", self.mgr.portal_port, C.exc_text(e))
            return
        if not self._running:  # stop() was called while we were starting
            self._server.close()

    # ---- HTTP ----------------------------------------------------------------------------
    async def _client(self, reader, writer):
        try:
            await asyncio.wait_for(self._handle(reader, writer), 15)
        except Exception as e:
            if not isinstance(e, (OSError, asyncio.TimeoutError)):
                self.log.debug("portal: %s", C.exc_text(e))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            gc.collect()

    async def _handle(self, reader, writer):
        self.touch()
        try:
            line = (await reader.readline()).decode().strip()
            parts = line.split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            headers = {}
            total = 0
            while True:
                h = await reader.readline()
                if not h or h in (b"\r\n", b"\n"):
                    break
                total += len(h)
                if total > _MAX_HEADERS:
                    raise HttpError(431)
                k, _, v = h.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            path, _, qs = target.partition("?")
            body = None
            if method == "POST":
                n = int(headers.get("content-length", "0") or 0)
                if n > _MAX_BODY:
                    raise HttpError(413)
                # read the body before any other refusal: closing with unread data sends a TCP reset
                raw = await reader.readexactly(n) if n else b""
                if "application/json" not in headers.get("content-type", ""):
                    raise HttpError(415, "Content-Type must be application/json")
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    raise HttpError(400, "invalid JSON")
            elif method not in ("GET", "HEAD"):
                raise HttpError(405)
            await self._route(writer, method, path, _query(qs), body, headers.get("host", ""))
        except HttpError as e:
            await self._json(writer, {"error": e.msg or _REASONS.get(e.code, "error")}, e.code)
        except ValueError as e:  # validation errors raised by the manager/store
            await self._json(writer, {"error": str(e)}, 400)

    async def _route(self, w, method, path, query, body, host):
        host = host.split(":")[0].lower()
        mgr = self.mgr
        local = (not host or _looks_like_ip(host) or host == "localhost"
                 or host.startswith(mgr.hostname.lower()))
        if not local and not path.startswith("/api/"):
            # captive-portal probes (generate_204, hotspot-detect.html, ncsi.txt ...) land here
            return await self._send(w, 302, "text/plain", b"", ["Location: http://%s/" % self.ap_ip])
        if path == "/" or path == "/index.html":
            return await self._file(w, _DIR + "/www/index.html", "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return await self._send(w, 204, "image/x-icon", b"")
        if not path.startswith("/api/"):
            if path in ("/generate_204", "/gen_204", "/hotspot-detect.html", "/ncsi.txt", "/connecttest.txt",
                        "/success.txt", "/canonical.html"):
                return await self._send(w, 302, "text/plain", b"", ["Location: http://%s/" % self.ap_ip])
            raise HttpError(404)

        route = path[5:]
        if method == "GET":
            if route == "status":
                return await self._json(w, mgr.status())
            if route == "networks":
                return await self._json(w, {"networks": mgr.networks_public(),
                                            "show_passwords": mgr.show_passwords})
            if route == "scan":
                return await self._scan(w, query.get("refresh") == "1")
            if route == "logs":
                n = int(query.get("n", "40") or 40)
                return await self._json(w, {"lines": mgr.log.tail(n)})
            raise HttpError(404)
        if method == "POST":
            if route == "networks/save":
                ssid = body.get("ssid")
                mgr.save_network(ssid, body.get("password"), body.get("hidden"), body.get("original_ssid"))
                mgr.log.info("portal: saved network '%s'", ssid)
                return await self._json(w, {"ok": True})
            if route == "networks/delete":
                ok = mgr.remove_network(body.get("ssid", ""))
                mgr.log.info("portal: deleted network '%s'", body.get("ssid"))
                return await self._json(w, {"ok": ok})
            if route == "connect":
                mgr.reconnect(body.get("ssid"))
                return await self._json(w, {"ok": True})
            if route == "exit":
                mgr.request_exit()
                return await self._json(w, {"ok": True})
            if route == "reboot":
                mgr.request_reboot()
                return await self._json(w, {"ok": True})
        raise HttpError(404)

    async def _scan(self, w, refresh):
        mgr = self.mgr
        error = None
        try:
            result = mgr.scan(force=refresh)
        except Exception as e:  # e.g. radio busy while a connection attempt is running
            error = "scan failed: %s" % e
            result = mgr._scan or []
        saved = {n["ssid"] for n in mgr.networks()}
        out = [dict(r, saved=r["ssid"] in saved) for r in result]
        await self._json(w, {"networks": out, "error": error})

    async def _json(self, w, obj, code=200):
        await self._send(w, code, "application/json", json.dumps(obj).encode())

    async def _send(self, w, code, ctype, body, extra=()):
        head = "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-store\r\nConnection: close\r\n" % (
            code, _REASONS.get(code, "OK"), ctype, len(body))
        for h in extra:
            head += h + "\r\n"
        w.write(head.encode() + b"\r\n")
        if body:
            w.write(body)
        await w.drain()

    async def _file(self, w, path, ctype):
        try:
            f = open(path, "rb")
        except OSError:
            raise HttpError(500, "web page missing: " + path)
        try:
            f.seek(0, 2)
            size = f.tell()
            f.seek(0)
            w.write(("HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-cache\r\n"
                     "Connection: close\r\n\r\n" % (ctype, size)).encode())
            buf = bytearray(1024)
            while True:
                n = f.readinto(buf)
                if not n:
                    break
                w.write(memoryview(buf)[:n])
                await w.drain()
        finally:
            f.close()
