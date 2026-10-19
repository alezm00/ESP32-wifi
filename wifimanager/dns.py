"""Minimal captive-portal DNS: answers every A query with one IP so phones open the setup page."""
import socket

from . import compat as C


def build_reply(query, ip_bytes):
    """Return the DNS reply for `query` (bytes) or None when it is not a plain query."""
    if len(query) < 17 or query[2] & 0x80:  # too short, or already a response
        return None
    end = 12
    while end < len(query) and query[end] != 0:
        end += query[end] + 1
    qend = end + 5  # zero byte + QTYPE(2) + QCLASS(2)
    if qend > len(query):
        return None
    qtype = (query[end + 1] << 8) | query[end + 2]
    head = query[:2] + b"\x81\x80" + query[4:6]  # id, flags (response, recursion), QDCOUNT
    if qtype == 1:  # A
        return (head + b"\x00\x01\x00\x00\x00\x00" + query[12:qend]
                + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + ip_bytes)
    # AAAA & friends: empty answer, so clients fall back to IPv4 quickly
    return head + b"\x00\x00\x00\x00\x00\x00" + query[12:qend]


class DNSServer:
    def __init__(self, ip, port=53):
        self.ip_bytes = bytes(int(p) for p in ip.split("."))
        self.port = port
        self._sock = None
        self._running = False

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        s.bind(("0.0.0.0", self.port))
        s.setblocking(False)
        self._sock = s
        self._running = True

    async def serve(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(256)
            except OSError:  # nothing waiting (EAGAIN)
                await C.sleep_ms(40)
                continue
            try:
                reply = build_reply(data, self.ip_bytes)
                if reply:
                    self._sock.sendto(reply, addr)
            except Exception:
                pass

    def stop(self):
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
