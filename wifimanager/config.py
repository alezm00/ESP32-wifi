"""Plain-JSON storage of the known WiFi networks.

File format (human readable, edit it with `mpremote edit` if you like)::

    {
     "version": 1,
     "networks": [
      {"ssid": "Home", "password": "secret123", "hidden": false}
     ],
     "defaults_seen": ["Home"]
    }

`defaults_seen` remembers which SSIDs given to WiFiManager(networks=[...]) were already
imported once, so networks you delete or edit later are not resurrected at the next boot.
"""
import os

from .compat import json

VERSION = 1


def clean_ssid(ssid):
    if not isinstance(ssid, str):
        raise ValueError("SSID must be a string")
    if not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("SSID must be 1-32 bytes long")
    return ssid


def clean_password(password):
    if password is None:
        password = ""
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if password and not 8 <= len(password) <= 64:
        raise ValueError("password must be empty (open network) or 8-64 characters")
    return password


def parse_default(item):
    """Accept ("ssid", "pass"[, hidden]), {"ssid":..,"password":..,"hidden":..} or "ssid" (open)."""
    if isinstance(item, str):
        return {"ssid": item, "password": "", "hidden": False}
    if isinstance(item, dict):
        return {"ssid": item.get("ssid"), "password": item.get("password", ""),
                "hidden": bool(item.get("hidden", False))}
    item = tuple(item)
    if not 1 <= len(item) <= 3:
        raise ValueError("expected (ssid, password[, hidden])")
    return {"ssid": item[0], "password": item[1] if len(item) > 1 else "",
            "hidden": bool(item[2]) if len(item) > 2 else False}


class ConfigStore:
    def __init__(self, path, log):
        self.path = path
        self.log = log
        self._nets = []
        self._seen = []

    # ---- reading -------------------------------------------------------------------------
    def _exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    def load(self):
        path = self.path
        if not self._exists(path):
            if self._exists(path + ".tmp"):  # power was lost between "remove" and "rename"
                self.log.warning("config: recovering from %s.tmp", path)
                path = path + ".tmp"
            else:
                self._nets, self._seen = [], []
                return
        try:
            with open(path) as f:
                data = json.load(f)
            nets = []
            for n in data.get("networks", []):
                ssid = n.get("ssid")
                if isinstance(ssid, str) and ssid:
                    nets.append({"ssid": ssid, "password": n.get("password", "") or "",
                                 "hidden": bool(n.get("hidden", False))})
            self._nets = nets
            self._seen = [s for s in data.get("defaults_seen", []) if isinstance(s, str)]
        except Exception as e:
            self.log.error("config: %s is corrupt (%s), starting empty", path, e)
            try:
                if self._exists(self.path + ".bad"):
                    os.remove(self.path + ".bad")
                os.rename(path, self.path + ".bad")
            except OSError:
                pass
            self._nets, self._seen = [], []

    # ---- writing -------------------------------------------------------------------------
    def save(self):
        lines = [json.dumps(n) for n in self._nets]
        text = '{\n "version": %d,\n "networks": [\n' % VERSION
        text += ",\n".join("  " + l for l in lines)
        text += '\n ],\n "defaults_seen": %s\n}\n' % json.dumps(self._seen)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        try:
            os.remove(self.path)
        except OSError:
            pass
        os.rename(tmp, self.path)

    # ---- defaults given to WiFiManager(...) ----------------------------------------------
    def merge_defaults(self, defaults, force=False):
        changed = False
        for raw in defaults:
            try:
                d = parse_default(raw)
                clean_ssid(d["ssid"])
                clean_password(d["password"])
            except (ValueError, TypeError) as e:
                self.log.error("config: ignoring default network %r (%s)", raw, e)
                continue
            ssid = d["ssid"]
            idx = self._find(ssid)
            if force:
                if idx is None:
                    self._nets.append(d)
                    changed = True
                elif self._nets[idx] != d:
                    self._nets[idx] = d
                    changed = True
            elif ssid not in self._seen and idx is None:
                self._nets.append(d)
                changed = True
                self.log.info("config: imported default network '%s'", ssid)
            if ssid not in self._seen:
                self._seen.append(ssid)
                changed = True
        if changed:
            self.save()
        return changed

    # ---- queries / edits -----------------------------------------------------------------
    def _find(self, ssid):
        for i, n in enumerate(self._nets):
            if n["ssid"] == ssid:
                return i
        return None

    def networks(self):
        return [dict(n) for n in self._nets]

    def get(self, ssid):
        i = self._find(ssid)
        return dict(self._nets[i]) if i is not None else None

    def upsert(self, ssid, password=None, hidden=None, original=None):
        """Add a network, or edit `original` (default: the entry named `ssid`).

        `password=None` keeps the stored password, "" makes it an open network.
        """
        clean_ssid(ssid)
        if password is not None:
            clean_password(password)
        idx = self._find(original if original is not None else ssid)
        if original is not None and idx is None:
            raise ValueError("network '%s' not found" % original)
        if idx is not None and ssid != self._nets[idx]["ssid"] and self._find(ssid) is not None:
            raise ValueError("a network named '%s' already exists" % ssid)
        if idx is None:
            self._nets.append({"ssid": ssid, "password": password or "", "hidden": bool(hidden)})
        else:
            n = self._nets[idx]
            n["ssid"] = ssid
            if password is not None:
                n["password"] = password
            if hidden is not None:
                n["hidden"] = bool(hidden)
        if ssid not in self._seen:
            self._seen.append(ssid)
        self.save()

    def remove(self, ssid):
        idx = self._find(ssid)
        if idx is None:
            return False
        del self._nets[idx]
        self.save()
        return True
