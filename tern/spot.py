"""Spot prices from Coinbase (a CF Benchmarks BRTI constituent), one ticker poll per second."""

from __future__ import annotations

import json
import urllib.request


def coinbase(product: str, timeout: float = 4.0) -> float | None:
    req = urllib.request.Request(f"https://api.exchange.coinbase.com/products/{product}/ticker",
                                 headers={"User-Agent": "tern/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        b, a = float(d["bid"]), float(d["ask"])
        return (a + b) / 2
    except Exception:
        return None


def history(product: str, granularity: int = 60, timeout: float = 6.0) -> list[list[float]]:
    """Up to 300 recent candles' closes as [ts, close], oldest first (seeds the chart on start)."""
    req = urllib.request.Request(
        f"https://api.exchange.coinbase.com/products/{product}/candles?granularity={granularity}",
        headers={"User-Agent": "tern/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rows = json.loads(r.read())
        return sorted([[float(x[0]), float(x[4])] for x in rows])
    except Exception:
        return []
