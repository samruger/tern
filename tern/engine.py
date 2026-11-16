"""The desk: polls spot and the current hourly event, runs the favorite rule, takes settings
commands from the screen, settles positions, reconciles live, and writes the status the screen reads."""

from __future__ import annotations

import collections
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import threading
import time
import traceback
import uuid

from . import config as cfgmod
from .book import Book, taker_fee
from .kalshi import Kalshi, KalshiError, quote
from .record import Recorder
from . import shadow as shadowmod
from .shadow import Shadow
from .spot import coinbase, history
from .strategy import due_slot, signals


CMD_MAX_AGE = 30.0     # seconds a command from the screen stays valid


LOCK_TRIES = 6                           # 0.2s apart
LOG_TIME = "%Y-%m-%d %H:%M:%S"           # how log() stamps a line in tern.log, and how it is read back
LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w[\w ]{4}) (.*)$")   # level is padded to 5
LOST_REPLY_WAITS = (1.0, 2.0, 4.0)   # seconds between looks for an order whose reply never came


class Unconfirmed(KalshiError):
    """An order was sent and Kalshi could not say what became of it."""


def _ago(sec: float) -> str:
    sec = abs(sec)
    return f"{sec:.0f}s ago" if sec < 120 else f"{sec/60:.0f}m ago" if sec < 7200 else f"{sec/3600:.0f}h ago"


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
        past = history(product, 60)              # a network call: never while holding the lock
        with self.lock:
            if product in self.products:
                return
            self.sec[product] = collections.deque(maxlen=3600)
            self.minute[product] = collections.deque(past, maxlen=1440 * 2)
            self.products.add(product)           # last: run() only sees a product whose queues exist

    def fresh(self, product: str, max_age: float = 10.0) -> float | None:
        """The price, or None when the last one is older than max_age (Coinbase unreachable)."""
        with self.lock:
            sec = self.sec.get(product)
            return sec[-1][1] if sec and time.time() - sec[-1][0] <= max_age else None

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
        self._load_log_tail()                  # the screen's log box keeps its history across restarts
        self.results: dict[str, str] = {}
        self.events: dict[str, dict] = {}      # series -> {event, title, close, markets, fetched}
        self.hist: dict[str, collections.deque] = {}   # ticker -> (ts, bid, ask)
        self.view = {"series": "KXBTCD", "ticker": None}
        self.seeded: set[str] = set()
        self.mode = "paper"
        self.live_error = ""
        self.foreign: list[str] = []
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
        self._saved_sig: tuple | None = None         # cheap fingerprint of that state (see _fingerprint)
        self._keys_ok = False
        self.lost_replies = 0                        # orders whose reply was lost or unreadable, ever
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
        self.taken = st.get("taken", {})             # "book|event" -> {ticker: entries}, for a variant's per-strike cap
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
        self.taken = {k: v for k, v in self.taken.items() if fresh(k)}
        # meta is only needed for strikes that are quoted now or still owed a settlement
        need = {m["ticker"] for e in self.events.values() for m in e["markets"]}
        need |= set(self.paper.pos) | set(self.live.pos) | {a["ticker"] for a in self.auto if a["pnl"] is None}
        if any(e["markets"] for e in self.events.values()):
            self.meta = {t: m for t, m in self.meta.items() if t in need}
            for k in [k for k in self.last if k.startswith("st") and k[2:] not in need]:
                del self.last[k]
        # Serializing the whole record is the expensive part (tens of ms, growing with it), so it only
        # happens when a cheap fingerprint of what changes it moves, or once a minute as a backstop.
        sig = self._fingerprint()
        if sig == self._saved_sig and now - self.last["fullsave"] < 60:
            return
        st = {"paper": self.paper.to_dict(), "live": self.live.to_dict(), "auto": self.auto[-20000:],
              "slots": {k: sorted(v) for k, v in self.slots.items()}, "hour_cost": self.hour_cost, "taken": self.taken,
              "day": self.day, "meta": self.meta, "ev_close": self.ev_close}
        text = json.dumps(st, separators=(",", ":"))
        if text != self._saved:                      # identical text: spare the disk
            _atomic_write(os.path.join(self.runtime, "state.json"), text, sync=True)
            self._saved = text
        # only now: a write that raised leaves the fingerprint stale, so the next save tries again
        self._saved_sig, self.last["fullsave"] = sig, now
        path = os.path.join(self.runtime, "state.json")
        if now - self.last["bak"] >= 3600:
            self.last["bak"] = now
            _atomic_write(path + ".bak", text, sync=True)

    def _fingerprint(self) -> tuple:
        def book(b: Book):
            return (len(b.trades), b.trades[-1]["ts"] if b.trades else 0, len(b.curve), tuple(b.curve[-1]) if b.curve else (),
                    round(b.cash, 6), round(b.realized, 6), round(b.start or 0, 6), len(b.pos),
                    sum(round(p["q"], 6) for p in b.pos.values()))
        return (book(self.paper), book(self.live), len(self.auto), sum(1 for a in self.auto if a["pnl"] is not None),
                sum(len(v) for v in self.slots.values()), len(self.slots), round(sum(self.hour_cost.values()), 6),
                len(self.hour_cost), sum(sum(v.values()) for v in self.taken.values()), json.dumps(self.day, sort_keys=True), len(self.meta), len(self.ev_close))

    def _load_log_tail(self, nbytes: int = 96_000) -> None:
        """Seed the in-memory log from the end of tern.log, newest lines first until the deque is
        full. Lines that log() did not write (a traceback) are skipped."""
        try:
            with open(os.path.join(self.runtime, "tern.log"), "rb") as fh:
                fh.seek(0, os.SEEK_END)
                start = max(0, fh.tell() - nbytes)
                fh.seek(start)
                lines = fh.read().decode(errors="replace").splitlines()[1 if start else 0:]   # mid-file, the first is cut
        except OSError:
            return
        got = []
        for ln in reversed(lines):
            m = LOG_LINE.match(ln)
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(m.group(1), LOG_TIME))
            except (ValueError, OverflowError):
                continue
            got.append([ts, m.group(2).strip(), m.group(3)])
            if len(got) >= self.logs.maxlen:
                break
        got.reverse()
        # The file keeps whole seconds, and the screen's scrollback needs every line's time to be
        # unique and rising: spread a burst within its second, and never step back (a DST hour).
        for k in range(1, len(got)):
            if got[k][0] <= got[k - 1][0]:
                got[k][0] = got[k - 1][0] + 0.001
        self.logs.extend(tuple(g) for g in got)

    def log(self, msg: str, level: str = "info") -> None:
        msg = " ".join(str(msg).split("\n"))        # one line each: the file is read back line by line
        line = (max(time.time(), self.logs[-1][0] + 0.001 if self.logs else 0), level, msg)
        self.logs.append(line)
        with open(os.path.join(self.runtime, "tern.log"), "a") as fh:
            fh.write(f"{time.strftime(LOG_TIME)} {level:5s} {msg}\n")
        if self.echo:
            print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    # ------------------------------------------------------------ market data
    def _refresh_event(self, series: str) -> None:
        now = time.time()
        e = self.events.get(series)
        since = now - self.last["ev" + series]
        if not e or (e["close"] <= now and since >= 5) or since > 120:
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
                self.rec.snap(series, e["close"], self.spot.fresh(cfgmod.SERIES[series]), ms, now)   # a stale price is no price
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

    def _mids(self) -> dict[str, float]:
        out = {}
        for t, (b, a, _, _) in ((m["ticker"], quote(m)) for e in self.events.values() for m in e["markets"]):
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
        other = sorted(t for t in ex if t.split("-", 1)[0] not in cfgmod.SERIES)
        if other and other != self.foreign:      # positions the desk did not open and cannot settle
            self.log(f"reconcile: leaving {len(other)} position(s) outside tern's markets alone: {', '.join(other[:5])}"
                     + ("…" if len(other) > 5 else ""), "warn")
        self.foreign = other
        for t in (set(ex) - set(other)) | set(self.live.pos):
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
        # Measured on the ledger (realized + open P&L), not on cash: the balance comes from Kalshi and
        # a settlement reaches it at a different moment than it reaches the book.
        today = time.strftime("%Y-%m-%d")
        pnl = self.live.realized + self.live.unrealized(self._mids())
        if self.day.get("date") != today or "pnl" not in self.day:
            self.day = {"date": today, "pnl": pnl}
        if pnl < self.day["pnl"] - self.cfg.live.max_daily_loss:
            self.log(f"KILL: live down ${self.day['pnl'] - pnl:,.2f} today (limit ${self.cfg.live.max_daily_loss:,.2f}). "
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
        # The rule only buys, but a strike's favorite can flip between slots (YES at 25m, NO at 15m), and
        # then part of the order closes what is held. Only the part that opens a position adds risk, so
        # only it meets the caps, as paper books it (Kalshi nets a buy of NO against held YES).
        held = book.pos.get(ticker, {})
        q, avg = held.get("q", 0.0), held.get("avg", 0.0)
        closing = min(abs(delta), abs(q)) if q and (q > 0) != (delta > 0) else 0.0
        cost = (abs(delta) - closing) * (price if delta > 0 else 1 - price)
        risk_after = book.at_risk() - closing * avg + cost
        L = self.cfg.live
        if cost > 1e-9:
            if cost > L.max_order_usd + 1e-9:
                raise KalshiError(f"order ${cost:,.2f} is over live.max_order_usd ${L.max_order_usd:,.2f}")
            if risk_after > L.capital_usd + 1e-9:
                raise KalshiError(f"would put ${risk_after:,.2f} at risk, over live.capital_usd ${L.capital_usd:,.2f}")
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
            # A 4xx is Kalshi refusing the order: nothing happened. Anything else (no reply, a 5xx from
            # a gateway that may have passed the order on) means the order may exist: look it up.
            if ex.status is not None and ex.status < 500:
                raise
            self.lost_replies += 1
            filled, px, fee = self._lost_reply(ticker, cid, price)
        except (AttributeError, TypeError, ValueError):
            # Kalshi answered but the reply can't be read: the order went through, so look it up
            self.lost_replies += 1
            filled, px, fee = self._lost_reply(ticker, cid, price)
        if filled <= 0:
            return None
        self.last["bal"] = 0                     # refresh balance soon
        self.last["save"] = 0                    # and put the fill on disk at the end of this step
        return book.trade(ticker, filled if delta > 0 else -filled, px, round(fee, 4), meta, tag)

    def _lost_reply(self, ticker: str, cid: str, price: float) -> tuple[float, float, float]:
        """The order was sent and no answer came back. Find it by its client id: (filled, YES price, fee).
        Raises Unconfirmed when Kalshi cannot be asked, so the caller counts the order as possibly filled."""
        o, answered = None, False                # answered: the most recent look got a reply
        for wait in LOST_REPLY_WAITS:
            time.sleep(wait)
            try:
                o = self.api.find_order(ticker, cid); answered = True
            except KalshiError:
                answered = False; continue
            if o:
                break
        # An early "not listed" can be Kalshi not showing a new order yet; only the last look decides
        if not o and not answered:
            self.last["pos"] = 0                 # reconcile on the next step: Kalshi is the record
            raise Unconfirmed(f"order {cid} on {ticker}: no reply and Kalshi could not be asked at the end; it may have filled")
        if not o:
            self.log(f"order {cid} reply lost and still not on Kalshi after {len(LOST_REPLY_WAITS)} looks; treating as unfilled", "warn")
            return 0.0, price, 0.0
        try:
            filled = float(o.get("fill_count_fp") or 0)
            fee = float(o.get("taker_fees_dollars") or 0) + float(o.get("maker_fees_dollars") or 0)
        except (AttributeError, TypeError, ValueError):
            self.last["pos"] = 0
            raise Unconfirmed(f"order {cid} on {ticker}: found on Kalshi but its fill can't be read; it may have filled")
        px = price
        if filled > 0 and o.get("order_id"):     # the order shows its limit; the fills show what was paid
            try:
                fs = self.api.order_fills(o["order_id"])
                n = sum(float(f.get("count_fp") or 0) for f in fs)
                if n > 0:
                    px = sum(float(f.get("count_fp") or 0) * float(f["yes_price_dollars"]) for f in fs) / n
                    fee = sum(float(f.get("fee_cost") or 0) for f in fs)
            except (KalshiError, KeyError, TypeError, ValueError):
                pass                             # keep the limit price: never better than the real fill
        return filled, px, fee

    # ------------------------------------------------------------ the rule
    def _live_rule_on(self, gate: dict) -> bool:
        return self.mode == "live" and (gate["passed"] or not self.cfg.live.auto_needs_gate)

    def variant(self) -> str:
        """The variant paper and live trade. A name config.json doesn't know falls back to base."""
        v = self.cfg.strategy.variant
        return v if v in shadowmod.VARIANTS else "base"

    def _auto(self, gate: dict | None = None) -> None:
        S = self.cfg.strategy
        lo, hi, cap, mins, spread, coins = shadowmod.params(self.variant(), S)
        gate = gate or self.gate()
        books = [self.paper]
        if self._live_rule_on(gate):
            books.append(self.live)
        now = time.time()
        for series in coins:
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
                if slot not in mins:
                    continue                         # the variant skips this slot
                held = self.taken.setdefault(key, {})
                sig = [s for s in signals(e["markets"], lo, hi, spread) if cap is None or held.get(s["ticker"], 0) < cap]
                if book is self.live and sig:
                    self._save_state()               # the slot is on disk before any order: a restart never repeats it
                placed = 0
                lost0 = self.lost_replies
                for s in sig:
                    if book is self.live and self.lost_replies > lost0:
                        # Kalshi's order endpoint is failing: each lookup sleeps ~7s, and a run of them
                        # would stall settling, the loss check and the screen while prices go stale.
                        self.log(f"live rule stopped for this slot: an order reply was lost, the rest are skipped", "warn")
                        break
                    spent = self.hour_cost.get(key, 0.0)
                    unit = s["price"] if s["delta_sign"] > 0 else 1 - s["price"]
                    n = min(S.contracts, max(0.0, (S.max_hour_cost - spent) / max(unit, 1e-6)))
                    n = float(int(n))
                    if n < 1:
                        continue                     # a cheaper favorite further on may still fit (as in shadow)
                    try:
                        rec = self._exec(book, s["ticker"], s["delta_sign"] * n, s["price"], s["size"], "auto")
                    except Unconfirmed as ex:        # count it as filled and stop: the budget must not be overspent
                        self.hour_cost[key] = spent + n * unit + taker_fee(n, s["price"], self.cfg.taker_fee)
                        held[s["ticker"]] = held.get(s["ticker"], 0) + 1
                        self.log(f"{book.name} rule stopped for this slot: {ex}", "error"); break
                    except KalshiError as ex:
                        self.log(f"{book.name} rule order {s['ticker']} rejected: {ex}", "warn"); continue
                    if not rec:
                        continue
                    self.hour_cost[key] = spent + abs(rec["delta"]) * unit + rec["fee"]
                    held[s["ticker"]] = held.get(s["ticker"], 0) + 1
                    placed += 1
                    if book is self.paper:
                        self.auto.append({"ts": now, "ticker": s["ticker"], "side": s["side"], "n": abs(rec["delta"]),
                                          "cost": unit, "fee": rec["fee"], "slot": slot, "pnl": None})
                self.log(f"{book.name}: {slot}m slot on {e['event']} ({self.variant()}): {len(sig)} favorites in band, {placed} bought")
                if book is self.live and sig:
                    self._save_state()

    def gate(self, name: str | None = None, rec: dict | None = None) -> dict:
        """The gate for a variant (the one in use by default), judged on that variant's own shadow
        record, so switching variants never lets a short or losing record trade live. The paper
        book's record of the rule rides along for the book box."""
        G = self.cfg.gate
        name = name or self.variant()
        r = rec or self.shadow.summary([name])[0]
        days = r["hours"] / 24              # hours it actually traded, not time elapsed
        checks = [
            {"name": f"{name} on paper ≥ {G.min_days:g} days", "ok": days >= G.min_days, "detail": f"{days:.1f} days of hours traded",
             "key": "days", "have": days, "need": G.min_days},
            {"name": f"≥ {G.min_entries} settled {name} entries", "ok": r["entries"] >= G.min_entries, "detail": f"{r['entries']}",
             "key": "entries", "have": r["entries"], "need": G.min_entries},
            {"name": f"{name} P&L ≥ ${G.min_pnl:g}", "ok": r["entries"] > 0 and r["pnl"] >= G.min_pnl, "detail": f"{'-' if r['pnl'] < 0 else '+'}${abs(r['pnl']):,.2f}",
             "key": "pnl", "have": r["pnl"], "need": G.min_pnl},
        ]
        rows = [a for a in self.auto if a["pnl"] is not None]
        pnl = sum(a["pnl"] for a in rows)
        wins = sum(1 for a in rows if a["pnl"] > 0)
        per = sum(a["pnl"] / a["n"] for a in rows) / len(rows) if rows else 0.0
        return {"variant": name, "checks": checks, "passed": all(c["ok"] for c in checks), "entries": len(rows),
                "open": len(self.auto) - len(rows), "pnl": pnl, "win": wins / len(rows) if rows else None,
                "per_contract": per}

    # ------------------------------------------------------------ shadow variants and the recorder
    def _shadow(self) -> None:
        now = time.time()
        for series in shadowmod.all_coins(self.cfg.strategy):
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
            # finalized without yes/no: voided. Those rows would otherwise stay open for good.
            n += self.shadow.void({m["ticker"] for m in ms if m.get("status") in ("finalized", "settled")
                                   and m.get("result") not in ("yes", "no", None, "")})
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
        ready = [t for t in due if now - self.last["st" + t] >= 20]   # throttle first, so nothing holds the queue
        for t in sorted(ready, key=lambda t: (self.last["st" + t], t))[:6]:   # longest-waiting first
            self.last["st" + t] = now
            try:
                m = self.pub.market(t)
            except KalshiError:
                continue
            res = m.get("result")
            if res not in ("yes", "no"):
                # Finalized without yes/no: Kalshi voided it and returns the stake. Without this the
                # position would be asked about forever and count toward the live capital cap.
                if res == "void" or (m.get("status") in ("finalized", "settled") and res not in (None, "")):
                    self._void(t, res)
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

    def _void(self, t: str, res) -> None:
        for b in (self.paper, self.live):
            if b.void(t):
                self.log(f"{b.name}: {t} finalized as {res!r}, not yes/no: voided, stake returned", "warn")
        for a in self.auto:
            if a["ticker"] == t and a["pnl"] is None:
                a["pnl"] = round(-a["fee"], 4)

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
            if not isinstance(c, dict):
                continue
            try:
                sent = float(c.get("ts") or name.split("-", 1)[0])
            except ValueError:
                sent = 0.0
            age = time.time() - sent
            try:
                if not -CMD_MAX_AGE <= age <= CMD_MAX_AGE:   # queued while the desk was down: never run it late
                    msg = f"ignored {c.get('op')!r} command sent {_ago(age)}: too old, send it again"
                else:
                    msg = self._command(c)
            except Exception as ex:
                msg = f"error: {ex}"
            self.results[c.get("id", name)] = msg
            self.log(msg, "cmd")
        while len(self.results) > 50:
            self.results.pop(next(iter(self.results)))

    def _command(self, c: dict) -> str:
        op = c.get("op")
        if op == "live":
            self.cfg.live.enabled = bool(c["on"]); cfgmod.save(self.cfg, self.runtime)
            if not c["on"]:
                self.mode = "paper"; self.api = None
                return "live off: back to paper (live positions ride to settlement)"
            self.last["bal"] = 0
            self.day = {}                        # the daily loss counts from now: a same-day KILL must not fire again at once
            return "live armed: authenticating with Kalshi (the daily loss limit counts from now)"
        if op == "series":
            s = c["series"]
            if s not in cfgmod.SERIES:
                return f"unknown series {s!r} (known: {', '.join(cfgmod.SERIES)})"
            lst = list(self.cfg.strategy.series)
            lst = [x for x in lst if x != s] if s in lst else lst + [s]
            self.cfg.strategy.series = lst; cfgmod.save(self.cfg, self.runtime)
            if shadowmod.VARIANTS.get(self.variant(), (None,) * 6)[5]:
                return f"rule coins set to {', '.join(lst) or 'nothing'}, but {self.variant()} trades its own coins until you switch back"
            return f"rule trades: {', '.join(lst) or 'nothing'}"
        if op == "variant":
            name = c.get("name")
            if name not in shadowmod.VARIANTS:
                return f"unknown variant {name!r} (known: {', '.join(shadowmod.VARIANTS)})"
            if name == self.variant():
                return f"{name} is already in use"
            was = self.variant()
            self.cfg.strategy.variant = name; cfgmod.save(self.cfg, self.runtime)
            g = self.gate(name)
            msg = f"variant {was} → {name}: paper trades it from the next slot (open positions ride to settlement)"
            if self.mode == "live" or self.cfg.live.enabled:
                msg += " · live trades it now" if g["passed"] or not self.cfg.live.auto_needs_gate else \
                       f" · live waits: {name} has not passed the gate"
            self.log(msg, "warn" if self.mode == "live" else "info")
            return msg
        if op == "reset_paper":
            self.paper = Book("paper", self.cfg.paper_capital); self.auto = []
            self.slots = {k: v for k, v in self.slots.items() if not k.startswith("paper|")}
            self.hour_cost = {k: v for k, v in self.hour_cost.items() if not k.startswith("paper|")}
            self.taken = {k: v for k, v in self.taken.items() if not k.startswith("paper|")}
            return f"paper reset to ${self.cfg.paper_capital:,.2f}"
        return f"unknown command {op!r}"

    # ------------------------------------------------------------ status
    def _has_keys(self, now: float) -> bool:
        """Whether a Kalshi key is present, re-read from disk every 10s rather than every step."""
        if now - self.last["keys"] >= 10:
            self.last["keys"] = now
            self._keys_ok = cfgmod.keys() is not None
        return self._keys_ok

    def _status(self, gate: dict | None = None, stopped: bool = False) -> None:
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
        if sel and sel not in self.seeded and not stopped:   # no network on the way out
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
        gate = gate or self.gate()
        shadow = self.shadow.summary()
        variants = [{**r, "desc": shadowmod.DESC.get(r["name"], ""), "active": r["name"] == self.variant(),
                     "gate": self.gate(r["name"], r)} for r in shadow]
        lo, hi, cap, mins, spread, coins = shadowmod.params(self.variant(), self.cfg.strategy)
        strategy = {**self.cfg.strategy.__dict__, "variant": self.variant(), "band_lo": lo, "band_hi": hi, "cap": cap,
                    "minutes": mins, "max_spread": spread, "series": coins}
        st = {"heartbeat": now, "stopped": stopped, "pid": os.getpid(), "mode": self.mode, "live_enabled": self.cfg.live.enabled,
              "live_error": self.live_error, "keys": self._has_keys(now), "balance": self.balance,
              "spot": sp, "events": evs, "hist": hist, "view": self.view,
              "paper": book_view(self.paper), "live": book_view(self.live),
              "gate": gate, "shadow": shadow, "variants": variants, "gate_cfg": self.cfg.gate.__dict__,
              "strategy": strategy, "live_cfg": self.cfg.live.__dict__,
              "logs": [list(x) for x in list(self.logs)[-200:]], "results": self.results,
              "auto_live": self._live_rule_on(gate)}
        _atomic_json(os.path.join(self.fast, "status.json"), st)

    # ------------------------------------------------------------ loop
    def _read_view(self) -> None:
        try:
            with open(os.path.join(self.fast, "view.json")) as fh:
                v = json.load(fh)
            if isinstance(v, dict) and v.get("series") in cfgmod.SERIES and isinstance(v.get("ticker"), (str, type(None))):
                self.view = v
        except (OSError, json.JSONDecodeError):
            pass

    def step(self) -> None:
        now = time.time()
        if now - self.last["cfg"] >= 5:
            self.last["cfg"] = now
            self.cfg = cfgmod.load(self.runtime, self.cfg)
        self._read_view()
        wanted = set(shadowmod.all_coins(self.cfg.strategy)) | {self.view["series"]}
        unknown = sorted(wanted - set(cfgmod.SERIES))
        if unknown and now - self.last["badseries"] > 60:
            self.last["badseries"] = now
            self.log(f"config: ignoring unknown series {', '.join(unknown)} (known: {', '.join(cfgmod.SERIES)})", "warn")
        for s in sorted(wanted & set(cfgmod.SERIES)):
            self.spot.want(cfgmod.SERIES[s])
            try:
                self._refresh_event(s)
            except KalshiError as ex:
                if now - self.last["everr" + s] > 30:
                    self.last["everr" + s] = now; self.log(f"{s} market data: {ex}", "warn")
        self._live_ready()
        self._commands()
        gate = self.gate()
        self._auto(gate)
        self._shadow()
        self._settle()
        mids = self._mids()
        self.paper.mark_curve(self.paper.equity(mids), now)
        if self.mode == "live":
            self.live.mark_curve(self.live.equity(mids), now)
        if now - self.last["save"] >= 5:
            self.last["save"] = now; self._save_state()
        self._status(gate)

    def run(self, once: bool = False) -> None:
        lock = open(os.path.join(self.runtime, "engine.lock"), "a")
        # A screen checking whether the engine is up takes this lock for an instant, so one refusal
        # is not proof of another engine: try for a second before believing it.
        for attempt in range(LOCK_TRIES):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt + 1 == LOCK_TRIES:
                    print("tern engine already running"); return
                time.sleep(0.2)
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
            except Exception:                        # tern.log unwritable: the line is still in memory
                pass
            try:
                self._status(stopped=True)           # the screen gets that line (status lives in tmpfs)
            except Exception:
                pass
