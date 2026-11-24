"""A ledger of Kalshi binary positions, used for both paper and live.

Positions are signed on the YES leg: +n holds n YES, -n holds n NO. A trade is a signed YES
quantity at a YES price, the way Kalshi's V2 orders quote it, with Kalshi's netting:
buying YES while holding NO closes NO first.
"""

from __future__ import annotations

import math
import time


def taker_fee(n: float, p: float, rate: float = 0.07) -> float:
    """Kalshi taker fee for one order: 0.07 * C * P * (1-P), rounded up to the cent."""
    return math.ceil(round(rate * abs(n) * p * (1 - p) * 100, 6)) / 100


def afford(n: float, unit: float, price: float, cash: float, rate: float = 0.07) -> float:
    """The most whole contracts, up to n, that cash pays for at unit each with the fee of one order at price."""
    k = int(max(0.0, min(n, cash / max(unit, 1e-6))))
    while k > 0 and k * unit + taker_fee(k, price, rate) > cash + 1e-9:
        k -= 1
    return float(k)


def limited(n: float, unit: float, at_risk: float, most: float, order: float) -> float:
    """The most whole contracts, up to n, at unit each, with at_risk in open bets already: `most` at once, `order` at a time."""
    return float(int(max(0.0, min(n, order / max(unit, 1e-6) + 1e-9, (most - at_risk) / max(unit, 1e-6) + 1e-9))))


def latest(trades: list[dict], n: int = 20) -> list[dict]:
    """The last n fills and the last n settlements, in the order they came: a busy hour of one leaves the other in."""
    left, out = {"fill": n, "settle": n}, []
    for t in reversed(trades):
        if left.get(t.get("kind"), 0) > 0:
            left[t["kind"]] -= 1
            out.append(t)
        elif not any(left.values()):
            break
    return out[::-1]


class Book:
    def __init__(self, name: str, cash: float):
        self.name = name
        self.cash = cash
        self.start = cash
        self.pos: dict[str, dict] = {}    # ticker -> {q, avg, event, strike, close, series}
        self.realized = 0.0
        self.fees = 0.0
        self.trades: list[dict] = []      # the last fills and settlements, newest last
        self.curve: list[list[float]] = []  # [ts, equity] per minute
        # The whole history lives in append-only files, not in the saved state. These hold what is
        # not in those files yet; the engine appends them and empties the lists (engine._save_state).
        self.new_trades: list[dict] = []
        self.new_curve: list[list[float]] = []

    # ------------------------------------------------------------ trading
    def trade(self, ticker: str, delta: float, price: float, fee: float, meta: dict, tag: str) -> dict:
        """Apply a fill of `delta` YES contracts (+buy / -sell) at YES price `price`."""
        p = self.pos.get(ticker) or {"q": 0.0, "avg": 0.0, **meta}
        q, avg = p["q"], p["avg"]
        realized = 0.0
        rest = delta
        if q and (q > 0) != (delta > 0):                  # closing part or all
            closed = min(abs(q), abs(delta))
            if q > 0:                                     # selling YES at price
                realized = closed * (price - avg); self.cash += closed * price
                q -= closed; rest = delta + closed
            else:                                         # buying YES closes NO, worth 1 - price
                realized = closed * ((1 - price) - avg); self.cash += closed * (1 - price)
                q += closed; rest = delta - closed
            if abs(q) < 1e-9:
                q, avg = 0.0, 0.0
        if abs(rest) > 1e-9:                              # opening / adding
            unit = price if rest > 0 else 1 - price
            n0 = abs(q); n1 = abs(rest)
            avg = (avg * n0 + unit * n1) / (n0 + n1)
            q += rest
            self.cash -= n1 * unit
        self.cash -= fee
        self.fees += fee
        self.realized += realized - fee
        p.update(q=q, avg=avg)
        if abs(q) < 1e-9:
            self.pos.pop(ticker, None)
        else:
            self.pos[ticker] = p
        rec = {"ts": time.time(), "ticker": ticker, "delta": delta, "price": price, "fee": fee,
               "realized": realized - fee, "tag": tag, "kind": "fill"}
        self._record(rec)
        return rec

    def _record(self, rec: dict, keep: int = 5000) -> None:
        self.trades.append(rec); self.new_trades.append(rec)
        del self.trades[:-keep]                   # the list in memory does not grow without end

    def settle(self, ticker: str, result_yes: bool) -> dict | None:
        p = self.pos.pop(ticker, None)
        if not p:
            return None
        q, avg = p["q"], p["avg"]
        payout = abs(q) if (q > 0) == result_yes else 0.0
        pnl = payout - abs(q) * avg
        self.cash += payout
        self.realized += pnl
        rec = {"ts": time.time(), "ticker": ticker, "delta": -q, "price": 1.0 if result_yes else 0.0, "fee": 0.0,
               "realized": pnl, "tag": "settle", "kind": "settle", "result": "yes" if result_yes else "no"}
        self._record(rec)
        return rec

    def void(self, ticker: str) -> dict | None:
        """The market was voided: the stake comes back at cost, no profit or loss (fees stay paid)."""
        p = self.pos.pop(ticker, None)
        if not p:
            return None
        q, avg = p["q"], p["avg"]
        self.cash += abs(q) * avg
        rec = {"ts": time.time(), "ticker": ticker, "delta": -q, "price": avg if q > 0 else 1 - avg, "fee": 0.0,
               "realized": 0.0, "tag": "void", "kind": "settle", "result": "void"}
        self._record(rec)
        return rec

    # ------------------------------------------------------------ marks
    def at_risk(self) -> float:
        return sum(abs(p["q"]) * p["avg"] for p in self.pos.values())

    def unrealized(self, mids: dict[str, float]) -> float:
        """Open P&L against cost; a position with no quote counts as flat."""
        v = 0.0
        for t, p in self.pos.items():
            m = mids.get(t)
            if m is not None:
                v += abs(p["q"]) * ((m if p["q"] > 0 else 1 - m) - p["avg"])
        return v

    def equity(self, mids: dict[str, float]) -> float:
        v = self.cash
        for t, p in self.pos.items():
            m = mids.get(t)
            if m is None:
                m = p["avg"] if p["q"] > 0 else 1 - p["avg"]
            v += p["q"] * m if p["q"] > 0 else -p["q"] * (1 - m)
        return v

    def mark_curve(self, eq: float, now: float, keep: int = 20_000) -> None:
        if not self.curve or now - self.curve[-1][0] >= 60:
            self.curve.append([round(now), round(eq, 2)]); self.new_curve.append(self.curve[-1])
            del self.curve[:-keep]

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        """What the book is now. Its history (trades, curve) is not part of it: see new_trades."""
        return {"name": self.name, "cash": self.cash, "start": self.start, "pos": self.pos,
                "realized": self.realized, "fees": self.fees}

    @classmethod
    def from_dict(cls, d: dict) -> "Book":
        b = cls(d["name"], d["cash"])
        b.start = d.get("start", d["cash"]); b.pos = d.get("pos", {}); b.realized = d.get("realized", 0.0)
        b.fees = d.get("fees", 0.0)
        return b
