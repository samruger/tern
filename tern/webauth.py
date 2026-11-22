"""tern web's login and HTTPS: one password, a signed cookie, limits on wrong guesses, a self-signed cert.

Nothing here lives in the repo. The password's hash and the cookie secret are in ~/.config/tern/web.json
(mode 600); the certificate and its key are web-cert.pem and web-key.pem next to it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time

DIR = os.path.expanduser("~/.config/tern")
AUTH_FILE = os.path.join(DIR, "web.json")
CERT_FILE = os.path.join(DIR, "web-cert.pem")
KEY_FILE = os.path.join(DIR, "web-key.pem")

ITER = 600_000            # PBKDF2-SHA256 rounds: about a third of a second a guess
COOKIE = "tern_s"
TTL = 30 * 86400          # a login lasts 30 days
WINDOW = 15 * 60          # wrong guesses are counted over 15 minutes
PER_IP = 5                # wrong guesses from one address before it waits
ALL = 30                  # wrong guesses from everyone before every login waits


def _private_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _hash(pw: str, salt: bytes, n: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, n)


def set_password(pw: str, path: str | None = None) -> None:
    """Store the password's hash and a new cookie secret, which signs out every browser."""
    if len(pw) < 12:
        raise ValueError("use at least 12 characters")
    salt = secrets.token_bytes(16)
    _private_write(path or AUTH_FILE, json.dumps({"salt": salt.hex(), "hash": _hash(pw, salt, ITER).hex(), "iter": ITER,
                                     "secret": secrets.token_hex(32)}))


class Auth:
    def __init__(self, path: str | None = None):
        with open(path or AUTH_FILE) as fh:
            d = json.load(fh)
        self.salt, self.hash = bytes.fromhex(d["salt"]), bytes.fromhex(d["hash"])
        self.iter, self.secret = int(d["iter"]), bytes.fromhex(d["secret"])

    def check(self, pw: str) -> bool:
        return hmac.compare_digest(_hash(pw[:1024], self.salt, self.iter), self.hash)

    def _sig(self, msg: str) -> str:
        return hmac.new(self.secret, msg.encode(), hashlib.sha256).hexdigest()

    def token(self, now: float | None = None) -> str:
        msg = f"{int((now or time.time()) + TTL)}.{secrets.token_hex(8)}"
        return f"{msg}.{self._sig(msg)}"

    def valid(self, tok: str | None, now: float | None = None) -> bool:
        try:
            exp, nonce, sig = (tok or "").split(".")
            return hmac.compare_digest(self._sig(f"{exp}.{nonce}"), sig) and int(exp) > (now or time.time())
        except ValueError:
            return False


class Limiter:
    """Wrong guesses per address and in all. wait() is the seconds until a guess is heard again."""

    def __init__(self):
        self.ips: dict[str, list[float]] = {}
        self.all: list[float] = []

    def _prune(self, now: float) -> None:
        self.all = [t for t in self.all if now - t < WINDOW]
        for ip in list(self.ips):
            self.ips[ip] = [t for t in self.ips[ip] if now - t < WINDOW]
            if not self.ips[ip]:
                del self.ips[ip]

    def wait(self, ip: str, now: float | None = None) -> float:
        now = now or time.time()
        self._prune(now)
        mine = self.ips.get(ip, [])
        w = 0.0
        if len(mine) >= PER_IP:
            w = mine[-PER_IP] + WINDOW - now
        if len(self.all) >= ALL:
            w = max(w, self.all[-ALL] + WINDOW - now)
        return max(0.0, w)

    def fail(self, ip: str, now: float | None = None) -> None:
        now = now or time.time()
        self.ips.setdefault(ip, []).append(now)
        self.all.append(now)

    def ok(self, ip: str) -> None:
        self.ips.pop(ip, None)


def make_cert(ips: list[str], cert: str | None = None, key: str | None = None) -> str:
    """A self-signed certificate for these addresses (and localhost). Returns its SHA-256 fingerprint."""
    cert, key = cert or CERT_FILE, key or KEY_FILE
    os.makedirs(os.path.dirname(cert), mode=0o700, exist_ok=True)
    san = ",".join(["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in ips])
    old = os.umask(0o077)
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "825", "-subj", "/CN=tern", "-addext", f"subjectAltName={san}",
                        "-keyout", key, "-out", cert], check=True, capture_output=True)
    finally:
        os.umask(old)
    return fingerprint(cert)


def fingerprint(cert: str | None = None) -> str:
    out = subprocess.run(["openssl", "x509", "-in", cert or CERT_FILE, "-noout", "-fingerprint", "-sha256"],
                         check=True, capture_output=True, text=True).stdout
    return out.strip().split("=", 1)[-1]
