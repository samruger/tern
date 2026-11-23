"""Shadow records: variants of the rule, run forward on paper side by side.

Each variant was written down before it saw any data, and each keeps its own record with real fees,
so after a few weeks the comparison is out of sample. Nothing here touches the paper or live books;
the engine trades whichever variant strategy.variant names, and that variant's record here is its gate.

The built-in variants are listed once, in TABLE below. The screen's variants box, its help, `tern report` and
the gate all read that table; README.md repeats it for people, and a test fails if it falls behind. The ones
made on the screen are each profile's own (variants.py): a Shadow runs both.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import deque

from .book import taker_fee
from .config import SERIES
from .strategy import due_slot, signals
from .variants import VariantSet, as_rule, describe

# name, (band_lo, band_hi, entries per strike per hour, slots, max spread, coins, max vol), what it does.
# max vol is the coin's realized volatility over the last hour (vol() below, bp a minute); above it a slot is skipped.
# None takes the value under strategy in config.json (coins: strategy.series) / no cap. The description is what
# people see; the comment is why the variant exists. A new variant's hours start when the engine
# restarts with it, so say that date in its description.
TABLE = [
    ("base", (None, None, None, None, None, None, None), "the rule: favorites at 75-95¢, every slot"),
    ("band90", (0.90, None, None, None, None, None, None), "only favorites at 90-95¢"),
    # added 2026-11-14 after two hours lost on strikes bought at every slot: later slots skip a strike already held
    ("cap1", (None, None, 1, None, None, None, None), "one entry per strike per hour (from 2026-11-15)"),
    # the band where the 67-day study found the bias (favorites ~2 points under)
    ("band80", (0.80, 0.90, None, None, None, None, None), "only favorites at 80-90¢ (from 2026-11-16)"),
    # less time left for the price to cross the strike
    ("late", (None, None, None, (15, 10), None, None, None), "only the 15m and 10m slots (from 2026-11-16)"),
    # pays less to get in
    ("tight1", (None, None, None, None, 0.01, None, None), "only strikes with a 1¢ spread (from 2026-11-16)"),
    # the cheapest slice: with band80 and band90 it splits the band in three
    ("band75", (None, 0.80, None, None, None, None, None), "only favorites at 75-80¢ (from 2026-11-16)"),
    # the other half of late
    ("early", (None, None, None, (25, 20), None, None, None), "only the 25m and 20m slots (from 2026-11-16)"),
    # Only BTC was tested on 67 days, so this is the first look at the others. Its dollars are up to
    # six times base's: compare it by cents a contract and t, not P&L.
    ("multi", (None, None, None, None, None, tuple(SERIES), None),
     "every coin at once, each its own budget; judge by ¢/ct (from 2026-11-16)"),
    # added 2026-11-19 after the worst day (-$93, five hours lost to sharp late moves). 7.6 bp/min is the
    # 75th percentile of base's entry slots 11-09..11-18, set from that data alone, not from P&L and not from
    # 11-19. Before 11-19 calm slots made more (+3.1 vs +1.2 ¢/ct), but on 11-19 they lost more.
    ("calm", (None, None, None, None, None, None, 7.6),
     "skips slots after a wild hour: rv60 over 7.6 bp/min (from 2026-11-20)"),
]
VARIANTS = {name: p for name, p, _ in TABLE}
ORDER = list(VARIANTS)
DESC = {name: desc for name, _, desc in TABLE}
# Retired: they no longer trade, their settled rows stay in shadow-settled.jsonl, `tern report` lists them.
RETIRED = {
    "stop20": "sold at 20¢ under entry; retired 2026-11-16: -$191 in 165 hours, t -3.0",
    "arb": "cross-strike pair under $1; retired 2026-11-16: never fired in 165 hours",
}


def own_set(dir: str) -> VariantSet:
    """The variants a profile made itself, from its folder (runtime/ for profile 1)."""
    return VariantSet(dir, (*VARIANTS, *RETIRED))


def params(name: str, S, own: VariantSet | None = None) -> tuple:
    """(band_lo, band_hi, cap, slots, max_spread, coins, max_vol) for a variant on strategy S. One of own's is
    its own rule whatever S says. An unknown name is the rule as configured. A built-in variant's slots are a
    subset of S.minutes; a slot it skips still counts as taken."""
    mine = own.rule(name) if own is not None and name not in VARIANTS else None
    if mine is not None:
        return tuple(mine[f] for f in ("band_lo", "band_hi", "cap", "minutes", "max_spread", "series", "max_vol"))
    lo, hi, cap, slots, spread, coins, mvol = VARIANTS.get(name, (None,) * 7)
    mins = [m for m in S.minutes if slots is None or m in slots]
    return lo or S.band_lo, hi or S.band_hi, cap, mins, spread or S.max_spread, list(coins or S.series), mvol


def vol(minute, now: float, n: int = 60) -> float | None:
    """Realized volatility of the last n one-minute returns, in basis points a minute, from [minute_ts, close]
    rows oldest first (the spot feed's minute queue; the last row is the minute still running). None when
    the hour has gaps of more than two minutes: a variant with a max vol then skips, it can't tell."""
    rows = [r for r in minute if r[0] <= now][-(n + 1):]
    if len(rows) < n + 1 or rows[-1][0] - rows[0][0] > (n + 2) * 60:
        return None
    rets = [math.log(b[1] / a[1]) for a, b in zip(rows, rows[1:]) if a[1] > 0 and b[1] > 0]
    if len(rets) < n:
        return None
    return math.sqrt(sum(x * x for x in rets) / len(rets)) * 1e4

def too_wild(mvol: float | None, v: float | None) -> bool:
    return mvol is not None and (v is None or v > mvol)


def rule_of(name: str, S, own: VariantSet | None = None) -> dict:
    """A variant's whole rule (variants.FIELDS), built in or own."""
    return as_rule(params(name, S, own))


def desc_of(name: str, own: VariantSet | None = None) -> str:
    mine = own.rule(name) if own is not None and name not in VARIANTS else None
    return describe(mine) if mine is not None else DESC.get(name, "")


def known(name, own: VariantSet | None = None) -> bool:
    return name in VARIANTS or own is not None and own.has(name)


def all_coins(S, own: VariantSet | None = None) -> list[str]:
    """Every series some variant trades: the engine fetches these and the shadow steps on them."""
    out = list(S.series)
    for name in [*VARIANTS, *(own.active() if own else [])]:
        out += [c for c in params(name, S, own)[5] if c not in out]
    return out


def all_minutes(S, own: VariantSet | None = None) -> list:
    """Every slot some variant enters at: the configured ones and own variants' others."""
    out = list(S.minutes)
    for name in own.active() if own else []:
        out += [m for m in own.rule(name)["minutes"] if m not in out]
    return out


def hour_of(event: str) -> str:
    """The hour an event closes in, without its series: two coins in one hour are one hour."""
    return event.split("-", 1)[-1]


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


class Shadow:
    def __init__(self, runtime: str, fee_rate: float = 0.07):
        self.open_path = os.path.join(runtime, "shadow.json")
        self.done_path = os.path.join(runtime, "shadow-settled.jsonl")
        self.rate = fee_rate
        self.own = own_set(runtime)                # this profile's own variants
        self.rows: list[dict] = []                 # open entries
        self.slots: dict[str, list] = {}           # event -> slots taken
        self.agg: dict[str, dict[str, list]] = {}  # variant -> event -> [pnl, contracts, entries, wins, stops]
        self.by_sv: dict[tuple, dict[str, list]] = {}   # (variant, settings version) -> event -> the same
        self.closes: dict[str, float] = {}         # event -> close time, to order the hours
        self.recent: dict[str, deque] = {}         # variant -> its last settled rows, for the screen's watch view
        self.fees: dict[str, float] = {}           # variant -> entry fees paid on settled rows
        self.dirty = False
        self.changes = 0                           # goes up whenever rows are added or settled: summaries cache on it
        try:
            with open(self.open_path) as fh:
                d = json.load(fh)
            self.rows, self.slots = d.get("rows", []), d.get("slots", {})
        except (OSError, json.JSONDecodeError):
            pass
        done = set()
        try:
            with open(self.done_path) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                        self._count(r)
                        done.add(self._key(r))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
        except OSError:
            pass
        # A row is written to the settled file before shadow.json is saved without it. If the engine
        # died in between, the row is in both: the settled file wins, so it is not settled twice.
        n = len(self.rows)
        # retired variants (stop20, arb) had no open rows when they stopped; any left would not settle right.
        # One of the profile's own, deleted, may have: they settle.
        self.rows = [r for r in self.rows if self._key(r) not in done and (self.own.damaged or r.get("v") in VARIANTS or r.get("v") in self.own.all)]
        self.dirty = len(self.rows) != n

    @staticmethod
    def _key(r: dict) -> tuple:
        return (r.get("v"), r.get("ticker"), r.get("ticker2"), r.get("ts"), r.get("slot"))

    # ------------------------------------------------------------ bookkeeping
    def _count(self, r: dict) -> None:
        a = self.agg.setdefault(r["v"], {}).setdefault(r["event"], [0.0, 0.0, 0, 0, 0])
        b = self.by_sv.setdefault((r["v"], r.get("sv", 0)), {}).setdefault(r["event"], [0.0, 0.0, 0, 0, 0])
        for x in (a, b):
            x[0] += r["pnl"]; x[1] += r["n"]; x[2] += 1; x[3] += r["pnl"] > 0; x[4] += r.get("exit") == "stop"
        if r.get("close"):
            self.closes[r["event"]] = r["close"]
        self.recent.setdefault(r["v"], deque(maxlen=30)).append(r)
        self.fees[r["v"]] = self.fees.get(r["v"], 0.0) + (r.get("fee") or 0.0)

    def _close(self, r: dict, pnl: float, **extra) -> None:
        self.changes += 1
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

    def order(self) -> list[str]:
        """The variants that run here: the built-in ones, then the profile's own, oldest first."""
        return ORDER + self.own.active()

    def bought(self, name: str) -> bool:
        """Whether a variant has any entry, open or settled: its rule then never changes."""
        return bool(self.agg.get(name)) or any(r["v"] == name for r in self.rows)

    # ------------------------------------------------------------ each engine step
    def step(self, event: str, close: float, markets: list[dict], S, now: float, vol: float | None = None,
             sv: int | None = None) -> list[str]:
        """Run every variant on one fresh snapshot of an hourly event; vol is the coin's vol() now.
        sv, when given, is the settings version each new row is stamped with. Returns lines for the log."""
        done = self.slots.setdefault(event, [])
        self.closes[event] = close                 # settle() keeps an event's slots until it has closed
        slot = due_slot(close - now, all_minutes(S, self.own), set(done))
        if slot is None:
            return []
        done.append(slot); self.dirty = True; self.changes += 1
        series = event.split("-", 1)[0]
        for name in self.order():
            lo, hi, cap, mins, spread, coins, mvol = params(name, S, self.own)
            if slot not in mins or series not in coins or too_wild(mvol, vol):
                continue
            mine = [r for r in self.rows if r["v"] == name and r["event"] == event]
            spent = sum(r["n"] * r["cost"] + r["fee"] for r in mine)
            held = {}
            for r in mine:
                held[r["ticker"]] = held.get(r["ticker"], 0) + 1
            for s in signals(markets, lo, hi, spread):
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
                                  "side": s["side"], "n": n, "cost": unit, "fee": fee, "slot": slot, "pnl": None,
                                  **({} if sv is None else {"sv": sv})})
        return []

    # ------------------------------------------------------------ settlement
    def pending(self, now: float) -> dict[str, float]:
        """Closed events that still have open rows: event -> close time."""
        return {r["event"]: r["close"] for r in self.rows if r["close"] < now}

    def needs(self, event: str) -> set[str]:
        return {r["ticker"] for r in self.rows if r["event"] == event}

    def settle(self, results: dict[str, bool], now: float | None = None) -> int:
        now = time.time() if now is None else now
        n = 0
        for r in list(self.rows):
            if r["ticker"] not in results:
                continue
            pay = float(results[r["ticker"]] if r["side"] == "yes" else not results[r["ticker"]])
            self.rows.remove(r)
            self._close(r, r["n"] * (pay - r["cost"]) - r["fee"])
            n += 1
        # Forget the slots of hours that are over. An hour still trading keeps its slots even with no
        # open rows: dropping them would let a slot that found nothing run a second time. An event
        # whose close is unknown (slots from before a restart) is kept until there are too many.
        keep = {r["event"] for r in self.rows} | {event_of(t) for t in results}
        for e in [e for e in self.slots if e not in keep]:
            if self.closes.get(e, now) < now or len(self.slots) > 60:
                del self.slots[e]
        return n

    def void(self, tickers: set[str]) -> int:
        """Close rows on voided strikes: the stake comes back, the entry fee stays paid."""
        n = 0
        for r in [r for r in self.rows if r["ticker"] in tickers]:
            self.rows.remove(r)
            self._close(r, -r["fee"], exit="void")
            n += 1
        return n

    # ------------------------------------------------------------ report
    def versions(self, name: str) -> list[int]:
        """The settings versions a variant has settled rows under (0: rows from before versions)."""
        return sorted(v for n, v in self.by_sv if n == name)

    def summary(self, names: list[str] | None = None, sv: int | None = None) -> list[dict]:
        """One record per variant. hours counts distinct closing hours; curve is the cumulative P&L
        after each of the last 240 of them, oldest first. With sv, only rows made under that settings version."""
        out = []
        for name in names or self.order():
            ev = self.agg.get(name, {}) if sv is None else self.by_sv.get((name, sv), {})
            by_hour: dict[str, float] = {}
            for e, a in ev.items():
                by_hour[hour_of(e)] = by_hour.get(hour_of(e), 0.0) + a[0]
            hours = list(by_hour.values())
            pnl, n = sum(hours), sum(a[1] for a in ev.values())
            entries, wins, stops = (sum(a[i] for a in ev.values()) for i in (2, 3, 4))
            t = None
            if len(hours) >= 10:
                m = pnl / len(hours)
                sd = math.sqrt(sum((x - m) ** 2 for x in hours) / (len(hours) - 1))
                t = m / (sd / math.sqrt(len(hours))) if sd else None
            when = {}
            for e in ev:
                when[hour_of(e)] = max(when.get(hour_of(e), 0.0), self.closes.get(e, 0.0))
            curve, run = [], 0.0
            for h in sorted(by_hour, key=lambda h: when[h]):
                run += by_hour[h]; curve.append(round(run, 2))
            out.append({"name": name, "entries": entries, "open": sum(1 for r in self.rows if r["v"] == name and (sv is None or r.get("sv", 0) == sv)),
                        "contracts": n, "pnl": pnl, "per_contract": pnl / n if n else None,
                        "win": wins / entries if entries else None, "stops": stops, "hours": len(hours), "t": t,
                        "curve": curve[-240:]})
        return out

    def view(self, name: str, rec: dict, mids: dict, strikes: dict, start: float) -> dict:
        """A variant's shadow record shaped like a book, so the screen can show it as if it traded:
        open entries marked at the mid, the last settlements and fills, and equity on a paper start.
        rec is the variant's summary() row. Watching it changes nothing about what trades."""
        pos, open_cost, open_fee, mark = [], 0.0, 0.0, 0.0
        for r in sorted((r for r in self.rows if r["v"] == name), key=lambda r: (r["close"], r["ts"])):
            m = mids.get(r["ticker"])
            val = None if m is None else (m if r["side"] == "yes" else 1 - m)
            q = r["n"] if r["side"] == "yes" else -r["n"]
            open_cost += r["n"] * r["cost"]; open_fee += r["fee"]
            mark += r["n"] * (val if val is not None else r["cost"])
            pos.append({"t": r["ticker"], "q": q, "avg": r["cost"], "mark": val, "k": strikes.get(r["ticker"]),
                        "close": r["close"], "pnl": None if val is None else r["n"] * (val - r["cost"])})
        cash = start + rec["pnl"] - open_cost - open_fee
        trades = []
        done = list(self.recent.get(name, ()))
        for r in done + [r for r in self.rows if r["v"] == name]:
            q = r["n"] if r["side"] == "yes" else -r["n"]
            trades.append({"kind": "fill", "ticker": r["ticker"], "delta": q, "ts": r["ts"], "fee": r["fee"],
                           "price": r["cost"] if q > 0 else 1 - r["cost"], "realized": -r["fee"]})
        for r in done:
            q = r["n"] if r["side"] == "yes" else -r["n"]
            void = r.get("exit") == "void"
            won = r["pnl"] + r["fee"] > 0
            trades.append({"kind": "settle", "ticker": r["ticker"], "delta": -q, "ts": r.get("close") or r["ts"],
                           "realized": r["pnl"] + r["fee"],  # the fee shows on the fill, as in the book
                           "result": "void" if void else ("yes" if won == (q > 0) else "no")})
        trades.sort(key=lambda t: (t["ts"], t["kind"] == "settle"))
        curve = [round(start + c, 2) for c in rec.get("curve") or []]
        return {"cash": cash, "equity": cash + mark, "start": start, "realized": rec["pnl"],
                "fees": self.fees.get(name, 0.0) + open_fee, "at_risk": open_cost, "pos": pos,
                "trades": trades[-30:], "curve": [[i, e] for i, e in enumerate(curve)], "hourly": True}


def table(rows: list[dict]) -> list[str]:
    w = max([8] + [len(r["name"]) for r in rows])
    out = [f"{'variant':<{w}} {'hours':>5} {'entries':>7} {'open':>4} {'P&L':>9} {'¢/contract':>10} {'win':>5} {'t':>6}"]
    for r in rows:
        pc = f"{r['per_contract']*100:+.2f}" if r["per_contract"] is not None else "-"
        win = f"{r['win']:.0%}" if r["win"] is not None else "-"
        t = f"{r['t']:+.2f}" if r["t"] is not None else "-"
        out.append(f"{r['name']:<{w}} {r['hours']:>5} {r['entries']:>7} {r['open']:>4} {r['pnl']:>+9.2f} {pc:>10} {win:>5} {t:>6}")
    return out
