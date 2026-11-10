"""The desk: polls spot and the current hourly event, runs the favorite rule, executes manual
orders from the screen, settles positions, reconciles live, and writes the status the screen reads."""

from __future__ import annotations

import collections
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import threading
import time
import traceback
import uuid

from . import config as cfgmod
from .book import Book, taker_fee
from .kalshi import Kalshi, KalshiError, quote
from .record import Recorder
from .shadow import Shadow
from .spot import coinbase, history
from .strategy import due_slot, signals


def live_dir(runtime: str) -> str:
    """Fast-changing files (status, view) live in tmpfs when there is one."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    tag = hashlib.sha1(os.path.realpath(runtime).encode()).hexdigest()[:8]   # realpath: a symlinked path is the same desk
    d = os.path.join(base, f"tern-{tag}") if base and os.path.isdir(base) else os.path.join(runtime, "live")
    os.makedirs(d, exist_ok=True)
    return d


def _ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _atomic_write(path: str, text: str, sync: bool = False) -> None:
    with open(path + ".tmp", "w") as fh:
        fh.write(text)
        if sync:
            fh.flush(); os.fsync(fh.fileno())
    os.replace(path + ".tmp", path)


def _atomic_json(path: str, obj) -> None:
    _atomic_write(path, json.dumps(obj, separators=(",", ":")))


def _event_of(ticker: str) -> str:
    """KXBTCD-26NOV0915-T74999.99 -> KXBTCD-26NOV0915"""
    return ticker.rsplit("-", 1)[0]


class SpotFeed(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.products: set[str] = set()
        self.sec: dict[str, collections.deque] = {}
        self.minute: dict[str, collections.deque] = {}
        self.now: dict[str, float] = {}

    def want(self, product: str) -> None:
        with self.lock:
            if product in self.products:
                return
            self.products.add(product)
            self.sec[product] = collections.deque(maxlen=3600)
            self.minute[product] = collections.deque(history(product, 60), maxlen=1440 * 2)

    def run(self) -> None:
        while True:
            t0 = time.time()
            for prod in list(self.products):
                p = coinbase(prod)
                if p is None:
                    continue
                now = time.time()
                with self.lock:
                    self.now[prod] = p
                    self.sec[prod].append((now, p))
                    mq = self.minute[prod]
                    mt = now // 60 * 60
                    if mq and mq[-1][0] == mt:
                        mq[-1] = [mt, p]
                    else:
                        mq.append([mt, p])
            time.sleep(max(0.05, 1.0 - (time.time() - t0)))


class Desk:
    def __init__(self, runtime: str = cfgmod.RUNTIME, echo: bool = False):
        self.runtime = runtime
        os.makedirs(runtime, exist_ok=True)
        self.fast = live_dir(runtime)
        self.cmd_dir = os.path.join(runtime, "cmd")
        os.makedirs(self.cmd_dir, exist_ok=True)
        self.echo = echo
        self.cfg = cfgmod.load(runtime)
        self.pub = Kalshi()
        self.api: Kalshi | None = None
        self.spot = SpotFeed()
        self.logs: collections.deque = collections.deque(maxlen=400)
        self.results: dict[str, str] = {}
        self.events: dict[str, dict] = {}      # series -> {event, title, close, markets, fetched}
        self.hist: dict[str, collections.deque] = {}   # ticker -> (ts, bid, ask)
        self.view = {"series": "KXBTCD", "ticker": None}
        self.seeded: set[str] = set()
        self.mode = "paper"
        self.live_error = ""
        self.balance: float | None = None
        self.last = collections.defaultdict(float)
        self._load_state()
        self.shadow = Shadow(runtime, self.cfg.taker_fee)
        self.rec = Recorder(runtime)
        self.closed: dict[str, dict] = {}      # hourly events that have closed and still owe their results
        for ev, close in self.shadow.pending(time.time()).items():
            self.closed[ev] = {"series": ev.split("-", 1)[0], "close": close}

    # ------------------------------------------------------------ state
    def _load_state(self) -> None:
        p = os.path.join(self.runtime, "state.json")
        st = {}
        self._saved = ""                             # the state text last written, to skip identical saves
        for path in (p, p + ".bak"):                 # a damaged state file falls back to the hourly backup
            if not os.path.exists(path):
                continue
            try:
                with open(path) as fh:
                    st = json.load(fh)
                if path != p:
                    self.log(f"state.json was unreadable; loaded the backup {os.path.basename(path)}", "error")
                break
            except (OSError, json.JSONDecodeError) as ex:
                keep = f"{path}.damaged-{int(time.time())}"
                try:
                    os.replace(path, keep)           # never overwrite a record we could not read
                except OSError:
                    pass
                self.log(f"{os.path.basename(path)} unreadable ({ex}); kept as {os.path.basename(keep)}", "error")
        self.paper = Book.from_dict(st["paper"]) if st.get("paper") else Book("paper", self.cfg.paper_capital)
        self.live = Book.from_dict(st["live"]) if st.get("live") else Book("live", 0.0)
        self.auto = st.get("auto", [])              # paper record of the rule: one row per entry
        self.slots = {k: set(v) for k, v in st.get("slots", {}).items()}     # "book|event" -> minutes done
        self.hour_cost = st.get("hour_cost", {})     # "book|event" -> dollars
        self.day = st.get("day", {})                 # live daily-loss anchor {date, equity}
        self.meta = st.get("meta", {})               # ticker -> {event, strike, close, series}
        self.ev_close = st.get("ev_close", {})       # event -> close time, for pruning

    def _save_state(self) -> None:
        now = time.time()
        cutoff = now - 3 * 86400
        fresh = lambda k: self.ev_close.get(k.split("|", 1)[1], 0) > cutoff
        self.ev_close = {k: v for k, v in self.ev_close.items() if v > cutoff}
        self.slots = {k: v for k, v in self.slots.items() if fresh(k)}
        self.hour_cost = {k: v for k, v in self.hour_cost.items() if fresh(k)}
        # meta is only needed for strikes that are quoted now or still owed a settlement
        need = {m["ticker"] for e in self.events.values() for m in e["markets"]}
        need |= set(self.paper.pos) | set(self.live.pos) | {a["ticker"] for a in self.auto if a["pnl"] is None}
        if any(e["markets"] for e in self.events.values()):
            self.meta = {t: m for t, m in self.meta.items() if t in need}
            for k in [k for k in self.last if k.startswith("st") and k[2:] not in need]:
                del self.last[k]
        st = {"paper": self.paper.to_dict(), "live": self.live.to_dict(), "auto": self.auto[-20000:],
              "slots": {k: sorted(v) for k, v in self.slots.items()}, "hour_cost": self.hour_cost,
              "day": self.day, "meta": self.meta, "ev_close": self.ev_close}
        text = json.dumps(st, separators=(",", ":"))
        if text == self._saved:                      # nothing changed: spare the disk
            return
        path = os.path.join(self.runtime, "state.json")
        _atomic_write(path, text, sync=True)
        self._saved = text
        if now - self.last["bak"] >= 3600:
            self.last["bak"] = now
            _atomic_write(path + ".bak", text, sync=True)

    def log(self, msg: str, level: str = "info") -> None:
        line = (time.time(), level, msg)
        self.logs.append(line)
        with open(os.path.join(self.runtime, "tern.log"), "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:5s} {msg}\n")
        if self.echo:
            print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    # ------------------------------------------------------------ market data
    def _refresh_event(self, series: str) -> None:
        now = time.time()
        e = self.events.get(series)
        if not e or e["close"] <= now or now - self.last["ev" + series] > 120:
            self.last["ev" + series] = now
            evs = [x for x in self.pub.open_events(series) if x.get("strike_date") and _ts(x["strike_date"]) > now]
            if not evs:
                return
            x = min(evs, key=lambda x: _ts(x["strike_date"]))
            if not e or e["event"] != x["event_ticker"]:
                if e:
                    self.closed[e["event"]] = {"series": series, "close": e["close"]}
                e = {"event": x["event_ticker"], "title": x.get("title", ""), "close": _ts(x["strike_date"]),
                     "markets": [], "fetched": 0.0}
                self.events[series] = e
                self.ev_close[e["event"]] = e["close"]
        if now - e["fetched"] >= 2.0:
            ms = self.pub.event_markets(e["event"])
            ms = [m for m in ms if m.get("floor_strike") is not None]
            ms.sort(key=lambda m: m["floor_strike"])
            e["markets"], e["fetched"] = ms, now
            if e["close"] > now:
                self.rec.snap(series, e["close"], self.spot.now.get(cfgmod.SERIES[series]), ms, now)
            for m in ms:
                self.meta.setdefault(m["ticker"], {"event": e["event"], "strike": m["floor_strike"],
                                                   "close": e["close"], "series": series})
                if m["ticker"] in self.hist or self._near(series, m):
                    bid, ask, _, _ = quote(m)
                    h = self.hist.setdefault(m["ticker"], collections.deque(maxlen=1800))
                    if not h or now - h[-1][0] >= 4.5:
                        h.append((now, bid, ask))
            live_tk = {m["ticker"] for m in ms}
            for t in [t for t in self.hist if t not in live_tk and self.meta.get(t, {}).get("series") == series]:
                del self.hist[t]

    def _seed(self, ticker: str) -> None:
        """Fill a contract's chart from Kalshi's minute candles for the hour so far."""
        m = self.meta.get(ticker) or {}
        if not m.get("series"):
            return
        now = int(time.time())
        try:
            d = self.pub._req("GET", f"/series/{m['series']}/markets/{ticker}/candlesticks",
                              {"start_ts": now - 3600, "end_ts": now, "period_interval": 1})
        except KalshiError:
            return
        def f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        rows = []
        for c in d.get("candlesticks", []):
            b, a = f(c["yes_bid"].get("close_dollars")), f(c["yes_ask"].get("close_dollars"))
            if b is not None and b <= 0: b = None
            if a is not None and a >= 1: a = None
            if b is not None or a is not None:
                rows.append((float(c["end_period_ts"]), b, a))
        h = self.hist.setdefault(ticker, collections.deque(maxlen=1800))
        old = [r for r in rows if not h or r[0] < h[0][0]]
        h.extendleft(reversed(old))

    def _near(self, series: str, m: dict) -> bool:
        bid, ask, _, _ = quote(m)
        if bid is None and ask is None:
            return False
        mid = ((bid or 0) + (ask or 1)) / 2
        return 0.02 < mid < 0.98

    def _quotes(self) -> dict[str, tuple]:
        out = {}
        for e in self.events.values():
            for m in e["markets"]:
                out[m["ticker"]] = quote(m)
        return out

    def _mids(self) -> dict[str, float]:
        out = {}
        for t, (b, a, _, _) in self._quotes().items():
            if b is not None and a is not None:
                out[t] = (a + b) / 2
            elif b is not None:
                out[t] = b
            elif a is not None:
                out[t] = a
        return out

    # ------------------------------------------------------------ live
    def _live_ready(self) -> bool:
        if not self.cfg.live.enabled:
            if self.mode == "live":
                self.log("live switched off; back to paper", "warn")
            self.mode = "paper"; self.api = None
            return False
        if self.api is None:
            k = cfgmod.keys()
            if not k:
                self.live_error = "no Kalshi key (KALSHI_KEY_ID / KALSHI_KEY_FILE)"
                self.mode = "paper"; return False
            try:
                self.api = Kalshi(*k)
            except Exception as ex:
                self.live_error = f"key unreadable: {ex}"; self.mode = "paper"; return False
        now = time.time()
        if now - self.last["bal"] >= 10:
            self.last["bal"] = now
            try:
                self.balance = self.api.balance()
                self.live.cash = self.balance
                self.live_error = ""
                if self.mode != "live":
                    self.log(f"LIVE: authenticated, balance ${self.balance:,.2f}", "live")
                    if not self.live.start:
                        self.live.start = self.balance
                self.mode = "live"
            except KalshiError as ex:
                self.live_error = str(ex)[:160]
                if ex.status in (401, 403):
                    self.mode = "paper"; self.api = None
                    self.log(f"live auth failed: {ex}", "error")
                return self.mode == "live"
        if self.mode == "live" and now - self.last["pos"] >= 15:
            self.last["pos"] = now
            self._reconcile()
        if self.mode == "live":
            self._daily_loss()
        return self.mode == "live"

    def _reconcile(self) -> None:
        try:
            ex = self.api.positions()
        except KalshiError as e:
            self.live_error = str(e)[:160]; return
        mids = self._mids()
        for t in set(ex) | set(self.live.pos):
            have = self.live.pos.get(t, {}).get("q", 0.0)
            real = ex.get(t, 0.0)
            if abs(have - real) > 1e-6:
                m = self.meta.get(t, {})
                if abs(real) < 1e-9:
                    if self.meta.get(t, {}).get("close", 0) > time.time():
                        self.live.pos.pop(t, None)
                        self.log(f"reconcile: {t} gone on Kalshi, removed", "warn")
                    continue
                p = self.live.pos.get(t) or {"q": 0.0, "avg": 0.0, **m}
                if not p["avg"] or (have > 0) != (real > 0):
                    mid = mids.get(t, 0.5); p["avg"] = mid if real > 0 else 1 - mid
                p["q"] = real
                self.live.pos[t] = p
                self.log(f"reconcile: {t} {have:+g} -> {real:+g} (Kalshi is the record)", "warn")

    def _daily_loss(self) -> None:
        today = time.strftime("%Y-%m-%d")
        eq = self.live.equity(self._mids())
        if self.day.get("date") != today:
            self.day = {"date": today, "equity": eq}
        if eq < self.day["equity"] - self.cfg.live.max_daily_loss:
            self.log(f"KILL: live down ${self.day['equity'] - eq:,.2f} today (limit ${self.cfg.live.max_daily_loss:,.2f}). "
                     "Live switched off; open positions ride to settlement.", "error")
            self.cfg.live.enabled = False
            cfgmod.save(self.cfg, self.runtime)
            self.mode = "paper"; self.api = None

    # ------------------------------------------------------------ execution
    def _exec(self, book: Book, ticker: str, delta: float, price: float, avail: float, tag: str) -> dict | None:
        """Fill a taker order of `delta` YES at limit `price` in `book`. Returns the trade record."""
        meta = {k: self.meta.get(ticker, {}).get(k) for k in ("event", "strike", "close", "series")}
        if book is self.paper:
            n = min(abs(delta), avail) if avail else 0
            if n <= 0:
                return None
            d = n if delta > 0 else -n
            return book.trade(ticker, d, price, taker_fee(n, price, self.cfg.taker_fee), meta, tag)
        # live
        if self.mode != "live" or not self.api:
            raise KalshiError("live is not active")
        q = book.pos.get(ticker, {}).get("q", 0.0)
        reducing = q and (q > 0) != (delta > 0) and abs(delta) <= abs(q) + 1e-9
        cost = abs(delta) * (price if delta > 0 else 1 - price)
        if not reducing:
            L = self.cfg.live
            if cost > L.max_order_usd + 1e-9:
                raise KalshiError(f"order ${cost:,.2f} is over live.max_order_usd ${L.max_order_usd:,.2f}")
            if book.at_risk() + cost > L.capital_usd + 1e-9:
                raise KalshiError(f"would put ${book.at_risk() + cost:,.2f} at risk, over live.capital_usd ${L.capital_usd:,.2f}")
            if self.balance is not None and cost > self.balance:
                raise KalshiError(f"order ${cost:,.2f} is over the balance ${self.balance:,.2f}")
        cid = f"tern-{uuid.uuid4().hex[:20]}"
        side = "bid" if delta > 0 else "ask"
        try:
            r = self.api.order(ticker, side, abs(delta), price, cid)
            filled = float(r.get("fill_count") or 0)
            px = float(r.get("average_fill_price") or price)
            fee = float(r.get("average_fee_paid") or 0) * filled
        except KalshiError as ex:
            if ex.status is not None:
                raise
            time.sleep(1.0)                      # reply lost: find the order by its client id
            o = self.api.find_order(ticker, cid)
            if not o:
                self.log(f"order {cid} reply lost and not found on Kalshi; treating as unfilled", "warn")
                return None
            filled = float(o.get("fill_count_fp") or 0)
            px = float(o.get("yes_price_dollars") or price)
            fee = float(o.get("taker_fees_dollars") or 0)
        if filled <= 0:
            return None
        self.last["bal"] = 0                     # refresh balance soon
        return book.trade(ticker, filled if delta > 0 else -filled, px, round(fee, 4), meta, tag)

    # ------------------------------------------------------------ the rule
    def _auto(self) -> None:
        S = self.cfg.strategy
        if not S.auto:
            return
        gate = self.gate()
        books = [self.paper]
        if self.mode == "live" and (gate["passed"] or not self.cfg.live.auto_needs_gate):
            books.append(self.live)
        now = time.time()
        for series in S.series:
            e = self.events.get(series)
            if not e or not e["markets"] or now - e["fetched"] > 10:
                continue
            tau = e["close"] - now
            for book in books:
                key = f"{book.name}|{e['event']}"
                done = self.slots.setdefault(key, set())
                slot = due_slot(tau, S.minutes, done)
                if slot is None:
                    continue
                done.add(slot)
                sig = signals(e["markets"], S.band_lo, S.band_hi, S.max_spread)
                placed = 0
                for s in sig:
                    spent = self.hour_cost.get(key, 0.0)
                    unit = s["price"] if s["delta_sign"] > 0 else 1 - s["price"]
                    n = min(S.contracts, max(0.0, (S.max_hour_cost - spent) / max(unit, 1e-6)))
                    n = float(int(n))
                    if n < 1:
                        break
                    try:
                        rec = self._exec(book, s["ticker"], s["delta_sign"] * n, s["price"], s["size"], "auto")
                    except KalshiError as ex:
                        self.log(f"{book.name} rule order {s['ticker']} rejected: {ex}", "warn"); continue
                    if not rec:
                        continue
                    self.hour_cost[key] = spent + abs(rec["delta"]) * unit + rec["fee"]
                    placed += 1
                    if book is self.paper:
                        self.auto.append({"ts": now, "ticker": s["ticker"], "side": s["side"], "n": abs(rec["delta"]),
                                          "cost": unit, "fee": rec["fee"], "slot": slot, "pnl": None})
                self.log(f"{book.name}: {slot}m slot on {e['event']}: {len(sig)} favorites in band, {placed} bought")

    def gate(self) -> dict:
        G = self.cfg.gate
        rows = [a for a in self.auto if a["pnl"] is not None]
        days = len({_event_of(a["ticker"]) for a in rows}) / 24      # hours the rule actually traded, not time elapsed
        pnl = sum(a["pnl"] for a in rows)
        checks = [
            {"name": f"rule on paper ≥ {G.min_days:g} days", "ok": days >= G.min_days, "detail": f"{days:.1f} days of hours traded"},
            {"name": f"≥ {G.min_entries} settled paper entries", "ok": len(rows) >= G.min_entries, "detail": f"{len(rows)}"},
            {"name": f"paper P&L of the rule ≥ ${G.min_pnl:g}", "ok": bool(rows) and pnl >= G.min_pnl, "detail": f"${pnl:+,.2f}"},
        ]
        wins = sum(1 for a in rows if a["pnl"] > 0)
        per = sum(a["pnl"] / a["n"] for a in rows) / len(rows) if rows else 0.0
        return {"checks": checks, "passed": all(c["ok"] for c in checks), "entries": len(rows),
                "open": len(self.auto) - len(rows), "pnl": pnl, "win": wins / len(rows) if rows else None,
                "per_contract": per}

    # ------------------------------------------------------------ shadow variants and the recorder
    def _shadow(self) -> None:
        now = time.time()
        for series in self.cfg.strategy.series:
            e = self.events.get(series)
            if e and e["markets"] and now - e["fetched"] <= 10 and e["close"] > now and e.get("shadowed") != e["fetched"]:
                e["shadowed"] = e["fetched"]
                for note in self.shadow.step(e["event"], e["close"], e["markets"], self.cfg.strategy, now):
                    self.log(note)
        for ev, c in list(self.closed.items()):
            if now < c["close"] + 45 or now - c.get("tried", 0) < 20:
                continue
            c["tried"] = now
            try:
                ms = self.pub.event_markets(ev)
            except KalshiError:
                continue
            res = {m["ticker"]: m["result"] == "yes" for m in ms if m.get("result") in ("yes", "no")}
            late = now > c["close"] + 1800
            if not late and (not res or len(res) < len(ms) or not self.shadow.needs(ev) <= set(res)):
                continue
            n = self.shadow.settle(res)
            self.rec.results(c["series"], c["close"], [(m["floor_strike"], m["result"]) for m in ms
                                                        if m["ticker"] in res and m.get("floor_strike") is not None])
            del self.closed[ev]
            if n:
                self.log("shadow: " + ev + " settled · " + " · ".join(
                    f"{r['name']} {r['pnl']:+.2f}" for r in self.shadow.summary() if r["entries"]))
        self.shadow.save()

    # ------------------------------------------------------------ settlement
    def _settle(self) -> None:
        now = time.time()
        due = {t for b in (self.paper, self.live) for t, p in b.pos.items() if (p.get("close") or 0) < now - 20}
        due |= {a["ticker"] for a in self.auto if a["pnl"] is None and self.meta.get(a["ticker"], {}).get("close", now) < now - 20}
        for t in sorted(due)[:6]:
            if now - self.last["st" + t] < 20:
                continue
            self.last["st" + t] = now
            try:
                m = self.pub.market(t)
            except KalshiError:
                continue
            res = m.get("result")
            if res not in ("yes", "no"):
                continue
            y = res == "yes"
            for b in (self.paper, self.live):
                r = b.settle(t, y)
                if r:
                    self.log(f"{b.name}: {t} settled {res.upper()}, {'+' if r['realized'] >= 0 else '-'}${abs(r['realized']):,.2f}")
            for a in self.auto:
                if a["ticker"] == t and a["pnl"] is None:
                    won = y if a["side"] == "yes" else not y
                    a["pnl"] = round(a["n"] * ((1.0 if won else 0.0) - a["cost"]) - a["fee"], 4)

    # ------------------------------------------------------------ commands from the screen
    def _commands(self) -> None:
        for name in sorted(os.listdir(self.cmd_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.cmd_dir, name)
            try:
                with open(path) as fh:
                    c = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            try:
                msg = self._command(c)
            except Exception as ex:
                msg = f"error: {ex}"
            self.results[c.get("id", name)] = msg
            self.log(msg, "cmd")
        while len(self.results) > 50:
            self.results.pop(next(iter(self.results)))

    def _command(self, c: dict) -> str:
        op = c.get("op")
        if op == "order":
            t = c["ticker"]; n = float(c["n"]); act = c["action"]
            q = self._quotes().get(t)
            if not q:
                return f"no quote for {t}"
            bid, ask, bs, az = q
            book = self.live if self.mode == "live" else self.paper
            if act == "close":
                have = book.pos.get(t, {}).get("q", 0.0)
                if not have:
                    return f"{book.name}: no position in {t}"
                n = min(n, abs(have)) if n else abs(have)
                delta = -n if have > 0 else n
            else:
                delta = n if act == "buy_yes" else -n
            price, avail = (ask, az) if delta > 0 else (bid, bs)
            if price is None:
                return f"{t}: no {'ask' if delta > 0 else 'bid'} to trade against"
            rec = self._exec(book, t, delta, price, avail, "manual")
            if not rec:
                return f"{book.name}: {act} {t} did not fill"
            n_done = abs(rec["delta"])
            if act == "close":
                held_yes = rec["delta"] < 0          # selling YES closes YES; buying YES closes NO
                side = "YES" if held_yes else "NO"
                shown = rec["price"] if held_yes else 1 - rec["price"]
                verb = "sold"
            else:
                side = "YES" if rec["delta"] > 0 else "NO"
                shown = rec["price"] if rec["delta"] > 0 else 1 - rec["price"]
                verb = "bought"
            return f"{book.name}: {verb} {n_done:g} {side} {t} @ {shown*100:.1f}¢ (fee ${rec['fee']:.2f})"
        if op == "auto":
            self.cfg.strategy.auto = bool(c["on"]); cfgmod.save(self.cfg, self.runtime)
            return f"rule {'on' if self.cfg.strategy.auto else 'off'}"
        if op == "live":
            self.cfg.live.enabled = bool(c["on"]); cfgmod.save(self.cfg, self.runtime)
            if not c["on"]:
                self.mode = "paper"; self.api = None
                return "live off: back to paper (live positions ride to settlement)"
            self.last["bal"] = 0
            return "live armed: authenticating with Kalshi"
        if op == "series":
            s = c["series"]
            lst = list(self.cfg.strategy.series)
            lst = [x for x in lst if x != s] if s in lst else lst + [s]
            self.cfg.strategy.series = lst; cfgmod.save(self.cfg, self.runtime)
            return f"rule trades: {', '.join(lst) or 'nothing'}"
        if op == "reset_paper":
            self.paper = Book("paper", self.cfg.paper_capital); self.auto = []
            self.slots = {k: v for k, v in self.slots.items() if not k.startswith("paper|")}
            return f"paper reset to ${self.cfg.paper_capital:,.2f}"
        return f"unknown command {op!r}"

    # ------------------------------------------------------------ status
    def _status(self) -> None:
        now = time.time()
        mids = self._mids()
        sp = {}
        with self.spot.lock:
            for prod in self.spot.products:
                sec = list(self.spot.sec[prod])
                sp[prod] = {"now": self.spot.now.get(prod),
                            "sec": [[round(t, 1), round(p, 2)] for t, p in sec[-900:]],
                            "min": [[t, round(p, 2)] for t, p in list(self.spot.minute[prod])[-1440:]]}
        evs = {}
        for series, e in self.events.items():
            ladder = []
            for m in e["markets"]:
                bid, ask, bs, az = quote(m)
                if bid is None and ask is None:
                    continue
                ladder.append({"t": m["ticker"], "k": m["floor_strike"], "b": bid, "a": ask, "bs": bs, "as": az,
                               "v": float(m.get("volume_fp") or 0)})
            evs[series] = {"event": e["event"], "title": e["title"], "close": e["close"], "age": now - e["fetched"],
                           "ladder": ladder}
        sel = self.view.get("ticker")
        if sel and sel not in self.seeded:
            self.seeded.add(sel); self._seed(sel)
        hist = {sel: [[round(t), b, a] for t, b, a in self.hist.get(sel, [])]} if sel in self.hist else {}

        def book_view(b: Book) -> dict:
            eq = b.equity(mids)
            pos = []
            for t, p in sorted(b.pos.items(), key=lambda kv: kv[1].get("close") or 0):
                m = mids.get(t)
                val = None if m is None else (m if p["q"] > 0 else 1 - m)
                pos.append({"t": t, "q": p["q"], "avg": p["avg"], "mark": val, "k": p.get("strike"),
                            "close": p.get("close"), "series": p.get("series"),
                            "pnl": None if val is None else abs(p["q"]) * (val - p["avg"])})
            return {"cash": b.cash, "equity": eq, "start": b.start, "realized": b.realized, "fees": b.fees,
                    "at_risk": b.at_risk(), "pos": pos, "curve": b.curve[-1500:],
                    "trades": b.trades[-30:]}
        st = {"heartbeat": now, "pid": os.getpid(), "mode": self.mode, "live_enabled": self.cfg.live.enabled,
              "live_error": self.live_error, "keys": cfgmod.keys() is not None, "balance": self.balance,
              "spot": sp, "events": evs, "hist": hist, "view": self.view,
              "paper": book_view(self.paper), "live": book_view(self.live),
              "gate": self.gate(), "shadow": self.shadow.summary(), "strategy": self.cfg.strategy.__dict__, "live_cfg": self.cfg.live.__dict__,
              "logs": [list(x) for x in list(self.logs)[-200:]], "results": self.results,
              "auto_live": self.mode == "live" and self.cfg.strategy.auto and (self.gate()["passed"] or not self.cfg.live.auto_needs_gate)}
        _atomic_json(os.path.join(self.fast, "status.json"), st)

    # ------------------------------------------------------------ loop
    def _read_view(self) -> None:
        try:
            with open(os.path.join(self.fast, "view.json")) as fh:
                v = json.load(fh)
            if v.get("series") in cfgmod.SERIES:
                self.view = v
        except (OSError, json.JSONDecodeError):
            pass

    def step(self) -> None:
        now = time.time()
        if now - self.last["cfg"] >= 5:
            self.last["cfg"] = now
            self.cfg = cfgmod.load(self.runtime, self.cfg)
        self._read_view()
        wanted = set(self.cfg.strategy.series) | {self.view["series"]}
        for s in wanted:
            self.spot.want(cfgmod.SERIES[s])
            try:
                self._refresh_event(s)
            except KalshiError as ex:
                if now - self.last["everr" + s] > 30:
                    self.last["everr" + s] = now; self.log(f"{s} market data: {ex}", "warn")
        self._live_ready()
        self._commands()
        self._auto()
        self._shadow()
        self._settle()
        mids = self._mids()
        self.paper.mark_curve(self.paper.equity(mids), now)
        if self.mode == "live":
            self.live.mark_curve(self.live.equity(mids), now)
        if now - self.last["save"] >= 5:
            self.last["save"] = now; self._save_state()
        self._status()

    def run(self, once: bool = False) -> None:
        lock = open(os.path.join(self.runtime, "engine.lock"), "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("tern engine already running"); return
        self.stopping = False
        def stop(signum, frame):
            self.stopping = True                     # only a flag: the step in progress finishes first
        signal.signal(signal.SIGTERM, stop)          # systemctl stop / restart
        signal.signal(signal.SIGINT, stop)           # Ctrl-C on `tern run`
        self.spot.start()
        self.log(f"engine started (pid {os.getpid()}), mode paper{' — live armed, authenticating' if self.cfg.live.enabled else ''}")
        try:
            while not self.stopping:
                t0 = time.time()
                try:
                    self.step()
                except Exception as ex:
                    self.log(f"step failed: {ex}", "error")
                    with open(os.path.join(self.runtime, "tern.log"), "a") as fh:
                        fh.write(traceback.format_exc())
                if once:
                    return
                if not self.stopping:
                    time.sleep(max(0.1, 1.0 - (time.time() - t0)))
        finally:
            self._shutdown(quiet=once)

    def _shutdown(self, quiet: bool = False) -> None:
        """Save everything between steps. A second signal must not cut a save short, and one
        failed save must not stop the others."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except ValueError:                       # not the main thread
                pass
        failed = []
        for name, save in (("state", self._save_state), ("shadow", self.shadow.save), ("quotes", self.rec.flush)):
            try:
                save()
            except Exception as ex:
                failed.append(name)
                try:
                    self.log(f"shutdown: saving {name} failed: {ex}", "error")
                except Exception:
                    pass
        if not quiet:
            try:
                self.log("engine stopped, " + (f"could not save: {', '.join(failed)}" if failed else "state saved"),
                         "error" if failed else "info")
            except Exception:
                pass
