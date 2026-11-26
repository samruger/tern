"""Paired phones: their public keys, and checking what they sign.

Each phone keeps two P-256 keys in its Keystore: an everyday one and a live one that needs a fingerprint.
The desk keeps only their public halves, in ~/.config/tern/app-devices.json (mode 600). A request is a JSON
payload and a signature over its exact text. Replays are stopped by the signed counter n, never by the
signature bytes (the same signature can be written more than one way).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import config as cfgmod
from . import sign, webauth

FILE = os.path.join(webauth.DIR, "app-devices.json")
MAX = 3                   # phones that may be paired at once
SKEW = 30.0               # seconds the phone's clock may differ from the desk's

_LOCK = threading.Lock()


class Refused(Exception):
    """A request the desk won't act on. Its text is shown on the phone as is."""


def words(everyday_b64: str, live_b64: str) -> str:
    """Six bytes of both public keys' hash, as 'ABCD EF01 2345', for you to compare on the desk and the phone."""
    h = hashlib.sha256(base64.b64decode(everyday_b64) + base64.b64decode(live_b64)).hexdigest()[:12].upper()
    return f"{h[:4]} {h[4:8]} {h[8:]}"


def needs_live(cmd: dict, cfg: "cfgmod.Config", armed: bool) -> bool:
    """True if the command needs the fingerprint key: arming live, or loosening a limit while live is armed."""
    if cmd.get("op") == "live":
        return cmd.get("on") is True
    if cmd.get("op") == "set" and armed:
        row = cfgmod.setting(str(cmd.get("key")))
        value = cmd.get("value")
        if row is None or not row.risk or isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        cur = cfgmod.get_setting(cfg, row.key)
        return cur is not None and (value - cur) * row.risk > 0
    return False


class Devices:
    def __init__(self, path: str = FILE):
        self.path = path
        self._mtime = None
        self._data: dict = {"secret": "", "phones": {}}

    def _load(self) -> None:
        """Re-read the file if it changed. Call with the lock held."""
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            with open(self.path) as fh:
                self._data = json.load(fh)
            self._mtime = mtime

    def _save(self) -> None:
        webauth._private_write(self.path, json.dumps(self._data))
        self._mtime = os.stat(self.path).st_mtime_ns

    def all(self) -> list[dict]:
        with _LOCK:
            self._load()
            return [{"id": i, "name": p["name"], "paired": p["paired"], "used": p["used"]}
                    for i, p in self._data["phones"].items()]

    def add(self, name: str, everyday_b64: str, live_b64: str) -> str:
        try:
            for k in (everyday_b64, live_b64):
                sign.p256_key(base64.b64decode(k, validate=True))
        except (ValueError, binascii.Error):
            raise Refused("that phone's keys aren't P-256 keys") from None
        with _LOCK:
            self._load()
            if len(self._data["phones"]) >= MAX:
                raise Refused(f"{MAX} phones are paired already: remove one first")
            i = secrets.token_hex(6)
            self._data["phones"][i] = {"name": name[:40], "everyday": everyday_b64, "live": live_b64,
                                       "paired": time.time(), "used": 0, "n": 0}
            self._save()
            return i

    def remove(self, id: str) -> bool:
        with _LOCK:
            self._load()
            if self._data["phones"].pop(id, None) is None:
                return False
            self._save()
            return True

    def _secret(self) -> bytes:
        """Call with the lock held."""
        self._load()
        if not self._data.get("secret"):
            self._data["secret"] = secrets.token_hex(32)
            self._save()
        return bytes.fromhex(self._data["secret"])

    def secret(self) -> bytes:
        """32 bytes that sign the app's session cookies. Made on first use."""
        with _LOCK:
            return self._secret()

    def check(self, payload: str, sig_b64: str, key: str, now: float, cfg=None, armed: bool = False) -> tuple[dict, dict]:
        """Verify one signed request. Returns (the phone's summary, its command), or raises Refused."""
        try:
            req = json.loads(payload)
        except ValueError:
            req = None
        if not (isinstance(req, dict) and isinstance(req.get("d"), str) and isinstance(req.get("cmd"), dict)
                and isinstance(req.get("n"), int) and not isinstance(req["n"], bool)
                and isinstance(req.get("ts"), (int, float)) and not isinstance(req["ts"], bool)):
            raise Refused("the request was malformed")
        with _LOCK:
            self._load()
            phone = self._data["phones"].get(req["d"])
            if phone is None:
                raise Refused("this phone isn't paired: pair it again from the desk")
            if key not in ("everyday", "live"):
                raise Refused("not signed by this phone's key")
            try:
                ok = sign.p256_verify(base64.b64decode(phone[key]), payload.encode(), base64.b64decode(sig_b64))
            except (ValueError, binascii.Error):
                ok = False
            if not ok:
                raise Refused("not signed by this phone's key")
            if abs(now - req["ts"]) > SKEW:
                raise Refused(f"the phone's clock is {abs(now - req['ts']):.0f} s off the desk's")
            if req["n"] <= phone["n"]:
                raise Refused("sent twice: refused")
            if key == "everyday" and cfg is not None and needs_live(req["cmd"], cfg, armed):
                raise Refused("live needs your fingerprint")
            phone["n"], phone["used"] = req["n"], now
            self._save()
            summary = {"id": req["d"], "name": phone["name"], "paired": phone["paired"], "used": now}
            return summary, req["cmd"]

    def _sig(self, msg: str) -> str:
        return hmac.new(self._secret(), msg.encode(), hashlib.sha256).hexdigest()

    def session_token(self, dev_id: str, now: float) -> str:
        """A cookie for the app's web view: the device, an expiry 30 days out, and an HMAC over both."""
        with _LOCK:
            msg = f"{dev_id}.{int(now + webauth.TTL)}"
            return f"{msg}.{self._sig(msg)}"

    def session_device(self, tok: str | None, now: float) -> str | None:
        """The device a cookie belongs to, or None if it is forged, expired or its phone was removed."""
        try:
            dev_id, exp, sig = (tok or "").split(".")
            with _LOCK:
                self._load()
                if (not hmac.compare_digest(self._sig(f"{dev_id}.{exp}"), sig) or int(exp) <= now
                        or dev_id not in self._data["phones"]):
                    return None
            return dev_id
        except (ValueError, TypeError):
            return None
