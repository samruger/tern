"""Shadow records: variants of the rule, and a no-prediction arbitrage, run forward on paper side by side.

Each variant was written down before it saw any data, and each keeps its own record with real fees,
so after a few weeks the comparison is out of sample. Nothing here touches the paper or live books.

  base     the rule as configured (favorite mid 75-95c)
  band90   only favorites at 90-95c
  stop20   the rule, but sell at the bid once the favorite's bid is 20c under the entry price
  cap1     the rule, but at most one entry per strike per hour: later slots skip a strike already held
           (added 2026-11-14 after two hours lost on strikes bought at every slot; judge it on later hours only)
  arb      when a higher strike's YES bid is above a lower strike's YES ask by more than both fees:
           buy YES on the lower strike and NO on the higher one. The pair pays at least $1.
"""

from __future__ import annotations

import json
import math
import os

from .book import taker_fee
from .kalshi import quote
from .strategy import due_slot, signals

VARIANTS = {            # name -> (band_lo, band_hi, stop, entries per strike); None takes the configured value / no cap
    "base": (None, None, None, None),
    "band90": (0.90, None, None, None),
    "stop20": (None, None, 0.20, None),
    "cap1": (None, None, None, 1),
}
ORDER = ["base", "band90", "stop20", "cap1", "arb"]


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


class Shadow:
    def __init__(self, runtime: str, fee_rate: float = 0.07):
        self.open_path = os.path.join(runtime, "shadow.json")
        self.done_path = os.path.join(runtime, "shadow-settled.jsonl")
        self.rate = fee_rate
        self.rows: list[dict] = []                 # open entries
        self.slots: dict[str, list] = {}           # event -> slots taken
        self.agg: dict[str, dict[str, list]] = {}  # variant -> event -> [pnl, contracts, entries, wins, stops]
        self.cool: dict[tuple, float] = {}         # arb pair -> last time taken
        self.dirty = False
        try:
            with open(self.open_path) as fh:
                d = json.load(fh)
            self.rows, self.slots = d.get("rows", []), d.get("slots", {})
        except (OSError, json.JSONDecodeError):
            pass
        try:
            with open(self.done_path) as fh:
                for line in fh:
                    try:
                        self._count(json.loads(line))
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError:
            pass

    # ------------------------------------------------------------ bookkeeping
    def _count(self, r: dict) -> None:
        a = self.agg.setdefault(r["v"], {}).setdefault(r["event"], [0.0, 0.0, 0, 0, 0])
        a[0] += r["pnl"]; a[1] += r["n"]; a[2] += 1; a[3] += r["pnl"] > 0; a[4] += r.get("exit") == "stop"

    def _close(self, r: dict, pnl: float, **extra) -> None:
        r.update(pnl=round(pnl, 4), **extra)
        self._count(r)
        with open(self.done_path, "a") as fh:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        with open(self.open_path + ".tmp", "w") as fh:
            json.dump({"rows": self.rows, "slots": self.slots}, fh, separators=(",", ":"))
        os.replace(self.open_path + ".tmp", self.open_path)
        self.dirty = False

    def _fee(self, n: float, p: float) -> float:
        return taker_fee(n, p, self.rate)

    # ------------------------------------------------------------ each engine step
    def step(self, event: str, close: float, markets: list[dict], S, now: float) -> list[str]:
        """Run every variant on one fresh snapshot of an hourly event. Returns lines for the log."""
        notes = []
        quotes = {m["ticker"]: quote(m) for m in markets}
        # stops
        for r in [r for r in self.rows if r.get("stop") and r["event"] == event]:
            bid, ask, bs, az = quotes.get(r["ticker"], (None, None, 0, 0))
            out, size = (bid, bs) if r["side"] == "yes" else (None if ask is None else 1 - ask, az)
            if out is not None and size > 0 and out <= r["cost"] - r["stop"] + 1e-9:
                self.rows.remove(r)
                self._close(r, r["n"] * (out - r["cost"]) - r["fee"] - self._fee(r["n"], out), exit="stop", out=out, out_ts=now)
        # entries
        done = self.slots.setdefault(event, [])
        slot = due_slot(close - now, S.minutes, set(done))
        if slot is not None:
            done.append(slot); self.dirty = True
            for name, (lo, hi, stop, cap) in VARIANTS.items():
                mine = [r for r in self.rows if r["v"] == name and r["event"] == event]
                spent = sum(r["n"] * r["cost"] + r["fee"] for r in mine)
                held = {}
                for r in mine:
                    held[r["ticker"]] = held.get(r["ticker"], 0) + 1
                for s in signals(markets, lo or S.band_lo, hi or S.band_hi, S.max_spread):
                    if cap is not None and held.get(s["ticker"], 0) >= cap:
                        continue
                    unit = s["price"] if s["delta_sign"] > 0 else 1 - s["price"]
                    n = float(int(min(S.contracts, s["size"], max(0.0, (S.max_hour_cost - spent) / max(unit, 1e-6)))))
                    if n < 1:
                        continue
                    fee = self._fee(n, s["price"])
                    spent += n * unit + fee
                    held[s["ticker"]] = held.get(s["ticker"], 0) + 1
                    self.rows.append({"v": name, "ts": now, "event": event, "close": close, "ticker": s["ticker"],
                                      "side": s["side"], "n": n, "cost": unit, "fee": fee, "slot": slot, "stop": stop, "pnl": None})
        # arbitrage: strikes ascending, so YES on a lower strike is never worth less than YES on a higher one
        live = [(m["ticker"], *quotes[m["ticker"]]) for m in markets if m.get("status") in (None, "active", "open")]
        best = None                                 # cheapest YES ask so far, among lower strikes
        for t, bid, ask, bs, az in live:
            if best and bid is not None and bid - best[1] > 0.005:
                lo_t, lo_ask, lo_size = best
                n = float(int(min(S.contracts, lo_size, bs)))
                if n >= 1 and now - self.cool.get((lo_t, t), -1e9) >= 60:
                    fee = self._fee(n, lo_ask) + self._fee(n, bid)
                    cost = lo_ask + 1 - bid
                    if n * (1 - cost) - fee > 0:
                        self.cool[(lo_t, t)] = now
                        self.rows.append({"v": "arb", "ts": now, "event": event, "close": close, "ticker": lo_t, "ticker2": t,
                                          "side": "pair", "n": n, "cost": cost, "fee": fee, "slot": None, "stop": None, "pnl": None})
                        self.dirty = True
                        notes.append(f"shadow arb: YES {lo_t} at {lo_ask*100:.0f}¢ + NO {t} at {(1-bid)*100:.0f}¢, "
                                     f"{n:g} pairs lock in ${n * (1 - cost) - fee:.2f} or more")
            if ask is not None and az >= 1 and (best is None or ask < best[1]):
                best = (t, ask, az)
        if len(self.cool) > 500:
            self.cool = {k: v for k, v in self.cool.items() if now - v < 60}
        return notes

    # ------------------------------------------------------------ settlement
    def pending(self, now: float) -> dict[str, float]:
        """Closed events that still have open rows: event -> close time."""
        return {r["event"]: r["close"] for r in self.rows if r["close"] < now}

    def needs(self, event: str) -> set[str]:
        return {t for r in self.rows if r["event"] == event for t in (r["ticker"], r.get("ticker2")) if t}

    def settle(self, results: dict[str, bool]) -> int:
        n = 0
        for r in list(self.rows):
            if r["ticker"] not in results or (r.get("ticker2") and r["ticker2"] not in results):
                continue
            if r["v"] == "arb":
                pay = float(results[r["ticker"]]) + float(not results[r["ticker2"]])
            else:
                pay = float(results[r["ticker"]] if r["side"] == "yes" else not results[r["ticker"]])
            self.rows.remove(r)
            self._close(r, r["n"] * (pay - r["cost"]) - r["fee"])
            n += 1
        live_events = {r["event"] for r in self.rows}
        for e in [e for e in self.slots if e not in live_events and e not in {event_of(t) for t in results}]:
            if len(self.slots) > 6:
                del self.slots[e]
        return n

    # ------------------------------------------------------------ report
    def summary(self) -> list[dict]:
        out = []
        for name in ORDER:
            ev = self.agg.get(name, {})
            hours = [a[0] for a in ev.values()]
            pnl, n = sum(hours), sum(a[1] for a in ev.values())
            entries, wins, stops = (sum(a[i] for a in ev.values()) for i in (2, 3, 4))
            t = None
            if len(hours) >= 10:
                m = pnl / len(hours)
                sd = math.sqrt(sum((x - m) ** 2 for x in hours) / (len(hours) - 1))
                t = m / (sd / math.sqrt(len(hours))) if sd else None
            out.append({"name": name, "entries": entries, "open": sum(1 for r in self.rows if r["v"] == name),
                        "contracts": n, "pnl": pnl, "per_contract": pnl / n if n else None,
                        "win": wins / entries if entries else None, "stops": stops, "hours": len(hours), "t": t})
        return out


def table(rows: list[dict]) -> list[str]:
    out = [f"{'variant':<8} {'hours':>5} {'entries':>7} {'open':>4} {'P&L':>9} {'¢/contract':>10} {'win':>5} {'stops':>5} {'t':>6}"]
    for r in rows:
        pc = f"{r['per_contract']*100:+.2f}" if r["per_contract"] is not None else "-"
        win = f"{r['win']:.0%}" if r["win"] is not None else "-"
        t = f"{r['t']:+.2f}" if r["t"] is not None else "-"
        out.append(f"{r['name']:<8} {r['hours']:>5} {r['entries']:>7} {r['open']:>4} {r['pnl']:>+9.2f} {pc:>10} {win:>5} {r['stops']:>5} {t:>6}")
    return out
