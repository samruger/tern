"""HTTPS over connections that stay open.

The desk makes about nine requests a second (six spot prices, the Kalshi ladders). Opening a new TLS
connection for each one was most of the engine's CPU, so each thread keeps one connection per host
and reuses it. Replies are asked for gzipped: the ladders are a few hundred KB of JSON every 2 seconds.
"""

from __future__ import annotations

import gzip
import http.client
import threading
import time
import zlib

IDLE = 20.0          # seconds: a connection idle longer than this is reopened, not trusted (servers drop idle ones)
_local = threading.local()      # connections are not shared between threads


def request(host: str, method: str, target: str, body: bytes | None = None, headers: dict | None = None,
            timeout: float = 10.0, reuse: bool = True) -> tuple[int, bytes]:
    """Send one request: (status, body). Raises OSError or http.client.HTTPException when no reply
    could be read. With reuse=False the request goes out on a new connection that is closed after:
    for a request that must never be sent twice (an order), where a kept connection the server has
    quietly closed would leave it unknown whether the request arrived."""
    conns: dict = _local.__dict__.setdefault("conns", {})
    hdrs = {"Accept-Encoding": "gzip", **(headers or {})}
    while True:
        conn, used = conns.pop(host, (None, 0.0)) if reuse else (None, 0.0)
        if conn is not None and time.monotonic() - used > IDLE:
            conn.close(); conn = None
        kept = conn is not None
        if conn is None:
            conn = http.client.HTTPSConnection(host, timeout=timeout)
        try:
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request(method, target, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
        except (OSError, http.client.HTTPException) as ex:
            conn.close()
            if kept and not isinstance(ex, TimeoutError):
                continue                         # the server had closed the kept connection: once more on a new one
            raise
        if reuse and not r.will_close:
            conns[host] = (conn, time.monotonic())
        else:
            conn.close()
        if (r.getheader("Content-Encoding") or "").lower() == "gzip":
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError, zlib.error) as ex:
                raise http.client.HTTPException(f"unreadable gzip reply ({ex})")
        return r.status, data
