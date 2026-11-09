"""Recorder: keeps what the engine sees, so ideas can be tested on our own data later.

runtime/ticks/<series>-<UTC date>.csv.gz   every 5 s, one line per near-the-money strike:
    ts,tau,spot,strike,bid,ask,bid_size,ask_size      (close time = ts + tau; prices in dollars, empty = no quote)
runtime/ticks/results.csv                   close_ts,series,strike,result

Each minute is appended as its own gzip member, so a crash loses at most a minute and `zcat` reads the lot.
"""

from __future__ import annotations

import gzip
import os
import time

from .kalshi import quote

HEADER = "ts,tau,spot,strike,bid,ask,bid_size,ask_size\n"


def _f(x) -> str:
    return "" if x is None else f"{x:.10g}"


class Recorder:
    def __init__(self, runtime: str, every: float = 5.0, flush: float = 60.0):
        self.dir = os.path.join(runtime, "ticks")
        os.makedirs(self.dir, exist_ok=True)
        self.every, self.flush_every = every, flush
        self.buf: dict[str, list[str]] = {}       # file name -> lines
        self.last: dict[str, float] = {}
        self.flushed = time.time()

    def snap(self, series: str, close: float, spot: float | None, markets: list[dict], now: float) -> None:
        if now - self.last.get(series, 0) < self.every:
            return
        self.last[series] = now
        name = f"{series}-{time.strftime('%Y%m%d', time.gmtime(now))}.csv.gz"
        lines = self.buf.setdefault(name, [])
        head = f"{now:.0f},{close - now:.0f},{_f(spot)}"
        for m in markets:
            bid, ask, bs, az = quote(m)
            if bid is None and ask is None:
                continue
            mid = ((bid or 0) + (ask or 1)) / 2
            if 0.02 < mid < 0.98:
                lines.append(f"{head},{_f(m['floor_strike'])},{_f(bid)},{_f(ask)},{_f(bs)},{_f(az)}\n")
        if now - self.flushed >= self.flush_every:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        self.flushed = now or time.time()
        for name, lines in self.buf.items():
            if not lines:
                continue
            path = os.path.join(self.dir, name)
            text = ("" if os.path.exists(path) else HEADER) + "".join(lines)
            with open(path, "ab") as fh:
                fh.write(gzip.compress(text.encode()))
        self.buf = {}

    def results(self, series: str, close: float, rows: list[tuple]) -> None:
        """rows: (strike, 'yes' | 'no')"""
        with open(os.path.join(self.dir, "results.csv"), "a") as fh:
            for k, res in rows:
                fh.write(f"{close:.0f},{series},{_f(k)},{res}\n")
