"""Kalshi request signing in pure Python: Ed25519 (RFC 8032) and RSA-PSS/SHA-256 (salt = 32).

Reads PKCS#8 ("BEGIN PRIVATE KEY") for either type, and PKCS#1 ("BEGIN RSA PRIVATE KEY") for RSA.
"""

from __future__ import annotations

import base64
import hashlib
import os

# ---------------------------------------------------------------- DER

OID_ED25519 = "1.3.101.112"
OID_RSA = "1.2.840.113549.1.1.1"


def _der(buf: bytes, i: int = 0) -> tuple[int, bytes, int]:
    """Read one TLV at i: (tag, value, next index)."""
    tag = buf[i]; i += 1
    n = buf[i]; i += 1
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i:i + k], "big"); i += k
    return tag, buf[i:i + n], i + n


def _seq(buf: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(buf):
        tag, val, i = _der(buf, i)
        out.append((tag, val))
    return out


def _oid(b: bytes) -> str:
    parts = [b[0] // 40, b[0] % 40]
    v = 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v); v = 0
    return ".".join(map(str, parts))


def _pem(text: str) -> tuple[str, bytes]:
    lines = [ln.strip() for ln in text.strip().splitlines()]
    head = lines[0]
    body = "".join(ln for ln in lines if ln and not ln.startswith("-----"))
    return head, base64.b64decode(body)


# ---------------------------------------------------------------- RSA-PSS

class RSAKey:
    def __init__(self, n, e, d, p, q, dp, dq, qinv):
        self.n, self.e, self.d, self.p, self.q, self.dp, self.dq, self.qinv = n, e, d, p, q, dp, dq, qinv
        self.k = (n.bit_length() + 7) // 8

    @classmethod
    def from_pkcs1(cls, der: bytes) -> "RSAKey":
        _, body, _ = _der(der)
        ints = [int.from_bytes(v, "big") for t, v in _seq(body) if t == 0x02]
        return cls(*ints[1:9])

    def _raw(self, m: int) -> int:
        a = pow(m, self.dp, self.p)
        b = pow(m, self.dq, self.q)
        h = (self.qinv * (a - b)) % self.p
        return b + h * self.q

    def sign(self, msg: bytes, salt: bytes | None = None) -> bytes:
        hlen = 32
        mod_bits = self.n.bit_length()
        em_bits = mod_bits - 1
        em_len = (em_bits + 7) // 8
        salt = os.urandom(hlen) if salt is None else salt
        mh = hashlib.sha256(msg).digest()
        h = hashlib.sha256(b"\x00" * 8 + mh + salt).digest()
        db = b"\x00" * (em_len - len(salt) - hlen - 2) + b"\x01" + salt
        mask = _mgf1(h, em_len - hlen - 1)
        masked = bytearray(x ^ y for x, y in zip(db, mask))
        masked[0] &= 0xFF >> (8 * em_len - em_bits)
        em = bytes(masked) + h + b"\xbc"
        s = self._raw(int.from_bytes(em, "big"))
        return s.to_bytes(self.k, "big")


def _mgf1(seed: bytes, n: int) -> bytes:
    out, c = b"", 0
    while len(out) < n:
        out += hashlib.sha256(seed + c.to_bytes(4, "big")).digest(); c += 1
    return out[:n]


# ---------------------------------------------------------------- Ed25519

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P


def _add(a, b):
    x1, y1, z1, t1 = a; x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % _P
    B = (y1 + x1) * (y2 + x2) % _P
    C = 2 * t1 * t2 * _D % _P
    Dd = 2 * z1 * z2 % _P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s: int, pt):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt); s >>= 1
    return q


def _enc(pt) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, _P - 2, _P)
    x, y = x * zi % _P, y * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


_GY = 4 * pow(5, _P - 2, _P) % _P


def _recover_x(y: int, sign: int) -> int:
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    if (x & 1) != sign:
        x = _P - x
    return x


_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


class Ed25519Key:
    def __init__(self, seed: bytes):
        assert len(seed) == 32
        h = hashlib.sha512(seed).digest()
        a = int.from_bytes(h[:32], "little")
        a &= (1 << 254) - 8; a |= 1 << 254
        self.a, self.prefix = a, h[32:]
        self.pub = _enc(_mul(a, _G))

    def sign(self, msg: bytes) -> bytes:
        r = int.from_bytes(hashlib.sha512(self.prefix + msg).digest(), "little") % _L
        R = _enc(_mul(r, _G))
        k = int.from_bytes(hashlib.sha512(R + self.pub + msg).digest(), "little") % _L
        s = (r + k * self.a) % _L
        return R + int.to_bytes(s, 32, "little")


# ---------------------------------------------------------------- loading

def load_key(pem_text: str):
    head, der = _pem(pem_text)
    if "RSA PRIVATE KEY" in head:
        return RSAKey.from_pkcs1(der)
    if "PRIVATE KEY" not in head or "ENCRYPTED" in head:
        raise ValueError("expected an unencrypted PKCS#8 or PKCS#1 private key")
    _, body, _ = _der(der)
    items = _seq(body)
    alg = _oid(_seq(items[1][1])[0][1])
    inner = items[2][1]
    if alg == OID_ED25519:
        _, seed, _ = _der(inner)
        return Ed25519Key(seed)
    if alg == OID_RSA:
        return RSAKey.from_pkcs1(inner)
    raise ValueError(f"unsupported key algorithm {alg}")


def signature(key, timestamp_ms: str, method: str, path: str) -> str:
    """base64 signature of timestamp + METHOD + path (path includes /trade-api/v2, no query)."""
    msg = (timestamp_ms + method.upper() + path.split("?", 1)[0]).encode()
    return base64.b64encode(key.sign(msg)).decode()
