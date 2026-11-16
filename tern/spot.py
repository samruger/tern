"""Spot prices from Coinbase (a CF Benchmarks BRTI constituent), one ticker poll per second."""

from __future__ import annotations

import json

from . import net

HOST = "api.exchange.coinbase.com"
HEADERS = {"User-Agent": "tern/1"}


def _get(target: str, timeout: float):
    status, raw = net.request(HOST, "GET", target, headers=HEADERS, timeout=timeout)
    if status != 200:
        raise ValueError(f"HTTP {status}")
    return json.loads(raw)


def coinbase(product: str, timeout: float = 4.0) -> float | None:
    try:
        d = _get(f"/products/{product}/ticker", timeout)
        b, a = float(d["bid"]), float(d["ask"])
        return (a + b) / 2
    except Exception:
        return None


def history(product: str, granularity: int = 60, timeout: float = 6.0) -> list[list[float]]:
    """Up to 300 recent candles' closes as [ts, close], oldest first (seeds the chart on start)."""
    try:
        rows = _get(f"/products/{product}/candles?granularity={granularity}", timeout)
        return sorted([[float(x[0]), float(x[4])] for x in rows])
    except Exception:
        return []
