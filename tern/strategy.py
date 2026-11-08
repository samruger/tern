"""The hourly favorite rule.

Kalshi's hourly above/below markets price long shots too high and favorites too low: over
67 days of BTC history, contracts the market put at 5-20c won about 2 points less often than
their price, and 80-90c favorites about 2 points more often. The rule buys the favorite at
the ask, at fixed minutes before the close, and holds it to settlement.
"""

from __future__ import annotations

from .kalshi import quote


def signals(markets: list[dict], band_lo: float, band_hi: float, max_spread: float) -> list[dict]:
    """Every strike whose favorite side has its mid in [band_lo, band_hi) and spread <= max_spread."""
    out = []
    for m in markets:
        if m.get("status") not in (None, "active", "open"):
            continue
        bid, ask, bs, az = quote(m)
        if bid is None or ask is None or ask - bid > max_spread + 1e-9:
            continue
        mid = (bid + ask) / 2
        if band_lo <= mid < band_hi:
            out.append({"ticker": m["ticker"], "side": "yes", "delta_sign": 1, "price": ask, "size": az,
                        "fav_mid": mid, "strike": m.get("floor_strike")})
        elif band_lo <= 1 - mid < band_hi:
            out.append({"ticker": m["ticker"], "side": "no", "delta_sign": -1, "price": bid, "size": bs,
                        "fav_mid": 1 - mid, "strike": m.get("floor_strike")})
    return out


def due_slot(tau: float, minutes: list, done: set, window: float = 45.0):
    """The entry slot (minutes before close) whose time has just arrived, or None."""
    for m in sorted(minutes, reverse=True):
        lag = m * 60 - tau
        if 0 <= lag < window and m not in done:
            return m
    return None
