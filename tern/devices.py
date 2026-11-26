"""Paired phones: their public keys, and checking what they sign.

Each phone keeps two P-256 keys in its Keystore: an everyday one and a live one that needs a fingerprint.
The desk keeps only their public halves, in ~/.config/tern/app-devices.json (mode 600). A request is a JSON
payload and a signature over its exact text. Replays are stopped by the signed counter n, never by the
signature bytes (the same signature can be written more than one way).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
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

_LOCK = threading.RLock()


class Refused(Exception):
    """A request the desk won't act on. Its text is shown on the phone as is."""


def words(everyday_b64: str, live_b64: str) -> str:
    """Six bytes of both public keys' hash, as 'ABCD EF01 2345', for you to compare on the desk and the phone."""
    h = hashlib.sha256(base64.b64decode(everyday_b64) + base64.b64decode(live_b64)).hexdigest()[:12].upper()
    return f"{h[:4]} {h[4:8]} {h[8:]}"


def needs_live(cmd: dict, cfg: "cfgmod.Config", armed: bool) -> bool:
    """True if the command needs the fingerprint key: arming live, or loosening a limit while live is armed."""
    if cmd.get("op") == "live":
        return bool(cmd.get("on"))                # the engine arms on any truthy value
    if cmd.get("op") == "variant_on" and armed:   # the desktop types "live" for turning on the armed live variant too
        return bool(cmd.get("on")) and cmd.get("variant") == cfg.live.variant
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
        self._depth = 0

    @contextlib.contextmanager
    def _locked(self):
        """Hold this process's lock and a file lock, so the desk and tern-web never overwrite each other."""
        with _LOCK:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._depth = 1
                self._load(force=True)            # another process may have changed the file
                yield
            finally:
                self._depth = 0
                os.close(fd)                      # closing releases the file lock

    def _load(self, force: bool = False) -> None:
        """Re-read the file if it changed. Call with the lock held."""
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            self._data, self._mtime = {"secret": "", "phones": {}}, None
            return
        if force or mtime != self._mtime:
            with open(self.path) as fh:
                self._data = json.load(fh)
            self._mtime = mtime

    def _save(self) -> None:
        webauth._private_write(self.path, json.dumps(self._data))
        self._mtime = os.stat(self.path).st_mtime_ns

    def all(self) -> list[dict]:
        with self._locked():
            return [{"id": i, "name": p["name"], "paired": p["paired"], "used": p["used"]}
                    for i, p in self._data["phones"].items()]

    def add(self, name: str, everyday_b64: str, live_b64: str) -> str:
        try:
            for k in (everyday_b64, live_b64):
                sign.p256_key(base64.b64decode(k, validate=True))
        except (ValueError, binascii.Error):
            raise Refused("that phone's keys aren't P-256 keys") from None
        with self._locked():
            if len(self._data["phones"]) >= MAX:
                raise Refused(f"{MAX} phones are paired already: remove one first")
            i = secrets.token_hex(6)
            self._data["phones"][i] = {"name": name[:40], "everyday": everyday_b64, "live": live_b64,
                                       "paired": time.time(), "used": 0, "n": 0}
            self._save()
            return i

    def remove(self, id: str) -> bool:
        with self._locked():
            if self._data["phones"].pop(id, None) is None:
                return False
            self._save()
            return True

    def _secret(self) -> bytes:
        """Call with the lock held."""
        if not self._data.get("secret"):
            self._data["secret"] = secrets.token_hex(32)
            self._save()
        return bytes.fromhex(self._data["secret"])

    def secret(self) -> bytes:
        """32 bytes that sign the app's session cookies. Made on first use."""
        with self._locked():
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
        with self._locked():
            phone = self._data["phones"].get(req["d"])
            if phone is None:
                raise Refused("this phone isn't paired: pair it again from the desk")
            if key not in ("everyday", "live"):
                raise Refused("not signed by this phone's key")
            pub = phone[key]
        try:                                      # slow: done outside the file lock
            ok = sign.p256_verify(base64.b64decode(pub), payload.encode(), base64.b64decode(sig_b64))
        except (ValueError, binascii.Error):
            ok = False
        if not ok:
            raise Refused("not signed by this phone's key")
        if abs(now - req["ts"]) > SKEW:
            raise Refused(f"the phone's clock is {abs(now - req['ts']):.0f} s off the desk's")
        if req["cmd"].get("op") in ("live", "variant_on") and not isinstance(req["cmd"].get("on"), bool):
            raise Refused("the request was malformed")
        if key == "everyday" and cfg is not None and needs_live(req["cmd"], cfg, armed):
            raise Refused("live needs your fingerprint")
        with self._locked():                      # the file was just re-read: the phone may be gone or have moved on
            phone = self._data["phones"].get(req["d"])
            if phone is None or phone[key] != pub:
                raise Refused("this phone isn't paired: pair it again from the desk")
            if req["n"] <= phone["n"]:
                raise Refused("sent twice: refused")
            phone["n"], phone["used"] = req["n"], now
            self._save()
            return ({"id": req["d"], "name": phone["name"], "paired": phone["paired"], "used": now}, req["cmd"])

    def _sig(self, msg: str) -> str:
        return hmac.new(self._secret(), msg.encode(), hashlib.sha256).hexdigest()

    def session_token(self, dev_id: str, now: float) -> str:
        """A cookie for the app's web view: the device, an expiry 30 days out, and an HMAC over both."""
        with self._locked():
            msg = f"{dev_id}.{int(now + webauth.TTL)}"
            return f"{msg}.{self._sig(msg)}"

    def session_device(self, tok: str | None, now: float) -> str | None:
        """The device a cookie belongs to, or None if it is forged, expired or its phone was removed."""
        try:
            dev_id, exp, sig = (tok or "").split(".")
            with self._locked():
                if (not hmac.compare_digest(self._sig(f"{dev_id}.{exp}"), sig) or int(exp) <= now
                        or dev_id not in self._data["phones"]):
                    return None
            return dev_id
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------- pairing
# The desk shows a 6-digit code; the phone sends it with its two public keys, signed by its everyday key. The
# desk shows the keys' words and you say yes on the desk. State is in the tmpfs folder (app-pair.json), shared
# by the screen and tern-web, under a file lock.

CODE_TTL = 300.0          # a pairing code lasts 5 minutes
WRONG_MAX = 5             # wrong codes before pairing is shut for LOCKOUT seconds
LOCKOUT = 600.0
NAME_MAX = 24


@contextlib.contextmanager
def _pair(live: str):
    """The pairing state, loaded; saved (0600, atomically) when the block ends, even in a refusal, if it changed."""
    path = os.path.join(live, "app-pair.json")
    fd = os.open(os.path.join(live, "app-pair.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with open(path) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        if not isinstance(st, dict):
            st = {}
        was = json.dumps(st, sort_keys=True)

        def save() -> None:
            if json.dumps(st, sort_keys=True) != was:          # the screen looks twice a second: most looks change nothing
                webauth._private_write(path, json.dumps(st))
        try:
            yield st
        except Refused:
            save()                                # a refusal still counts (wrong codes)
            raise
        save()
    finally:
        os.close(fd)


def _clean(name: str) -> str:
    return "".join(c for c in str(name) if c.isprintable())[:NAME_MAX].strip() or "phone"


def new_code(live: str, now: float) -> str:
    """A fresh 6-digit code for 5 minutes. It replaces any earlier code and any offer waiting."""
    code = f"{secrets.randbelow(10 ** 6):06d}"
    with _pair(live) as st:
        st.update(code=code, until=now + CODE_TTL, wrong=0)
        st.pop("offer", None)
    return code


def offer(live: str, code: str, name: str, everyday_b64: str, live_b64: str, sig_b64: str, now: float) -> str:
    """A phone asks to be paired. Returns an offer id to poll with offer_state, or raises Refused."""
    sent, name = name, _clean(name)           # the phone signed the name as it sent it
    with _pair(live) as st:
        if st.get("locked", 0) > now:
            raise Refused("too many wrong codes: wait 10 min")
        o = st.get("offer")
        if o and o["state"] == "waiting" and o["until"] > now:
            raise Refused("another phone is waiting to be paired")
        if not st.get("code") or st.get("until", 0) <= now:
            raise Refused("no pairing code on the desk: open settings, APP, pair a phone")
        if not (isinstance(code, str) and hmac.compare_digest(code.encode(), st["code"].encode())):
            st["wrong"] = st.get("wrong", 0) + 1
            if st["wrong"] >= WRONG_MAX:
                st.update(locked=now + LOCKOUT, wrong=0)
                st.pop("code", None)
                raise Refused("too many wrong codes: wait 10 min")
            raise Refused("wrong code")
        if everyday_b64 == live_b64:
            raise Refused("the phone sent one key for both: not paired")
        try:
            ok = sign.p256_verify(base64.b64decode(everyday_b64, validate=True),
                                  f"pair\n{code}\n{sent}\n{everyday_b64}\n{live_b64}".encode(),
                                  base64.b64decode(sig_b64, validate=True))
            sign.p256_key(base64.b64decode(live_b64, validate=True))
        except (ValueError, binascii.Error, TypeError):
            ok = False
        if not ok:
            raise Refused("bad signature: the phone didn't sign its own keys")
        oid = secrets.token_hex(8)
        st["offer"] = {"id": oid, "name": name, "everyday": everyday_b64, "live": live_b64,
                       "state": "waiting", "until": st["until"]}
        st.pop("code", None)                      # used up
        return oid


def offered(live: str, now: float) -> dict | None:
    """The offer waiting for a yes on the desk: its id, the phone's name and the keys' words."""
    with _pair(live) as st:
        o = st.get("offer")
        if o and o["state"] == "waiting" and o["until"] > now:
            return {"id": o["id"], "name": o["name"], "words": words(o["everyday"], o["live"])}
    return None


def answer(live: str, offer_id: str, yes: bool, devices: "Devices", now: float | None = None) -> str:
    """The desk's yes or no. Returns the line the screen says."""
    with _pair(live) as st:
        o = st.get("offer")
        if not o or o["id"] != offer_id or o["state"] != "waiting":
            return "that pairing is over"
        if o["until"] <= (time.time() if now is None else now):
            return "that pairing ran out"         # it stays expired: the phone may have given up already
        if not yes:
            o["state"] = "declined"
            return f"{o['name']} not paired"
        try:
            o["device"] = devices.add(o["name"], o["everyday"], o["live"])
        except Refused as ex:
            o.update(state="declined", why=str(ex))   # the phone shows why
            return str(ex)
        o["state"] = "paired"
        return f"{o['name']} paired"


def offer_state(live: str, offer_id: str, now: float) -> dict:
    """What the phone polls: waiting, paired (with its device id), declined (with the desk's reason, if it had one),
    expired or unknown."""
    with _pair(live) as st:
        o = st.get("offer")
        if not o or o["id"] != offer_id:
            return {"state": "unknown"}
        if o["state"] == "waiting" and o["until"] <= now:
            return {"state": "expired"}
        more = {"device": o["device"]} if o["state"] == "paired" else {"why": o["why"]} if o.get("why") else {}
        return {"state": o["state"], **more}
