"""The screen, btop style. It reads the engine's status file and drops command files;
closing it never stops the engine."""

from __future__ import annotations

import curses
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid

from . import config as cfgmod
from .book import taker_fee
from .engine import live_dir

# ---------------------------------------------------------------- colors

BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

GRAD256 = {
    "spot": [24, 25, 26, 32, 38, 44, 50, 87],
    "yes": [34, 70, 106, 142, 178, 214, 208, 203],
    "eq_up": [22, 28, 34, 35, 41, 47, 48, 49],
    "eq_dn": [52, 88, 124, 160, 196, 197, 198, 199],
}
GRAD8 = {"spot": [BLUE, CYAN], "yes": [GREEN, YELLOW, RED], "eq_up": [GREEN], "eq_dn": [RED]}


class Pal:
    def __init__(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            self.bg = -1
        except curses.error:
            self.bg = BLACK
        self.many = curses.COLORS >= 256
        self.grey = 8 if curses.COLORS >= 16 else WHITE
        self.cache: dict[int, int] = {}

    def __call__(self, fg: int, bold: bool = False, dim: bool = False) -> int:
        if fg not in self.cache:
            n = len(self.cache) + 1
            if n >= curses.COLOR_PAIRS:
                return curses.A_NORMAL
            curses.init_pair(n, fg, self.bg)
            self.cache[fg] = curses.color_pair(n)
        a = self.cache[fg]
        if bold:
            a |= curses.A_BOLD
        if dim:
            a |= curses.A_DIM
        return a

    def grad(self, name: str) -> list[int]:
        return (GRAD256 if self.many else GRAD8)[name]


# log colors: the line's level tints its plain text and its marker; tokens get their own colors
LOG_BASE = {"error": RED, "warn": YELLOW, "live": RED, "cmd": CYAN}
LOG_MARK = {"error": "✖", "warn": "▲", "live": "●", "cmd": "›"}
LOG_BOOK = {"paper": CYAN, "live": RED, "shadow": MAGENTA, "kill": RED}
LOG_TOKENS = re.compile(
    r"(?P<book>^[A-Za-z]+:)(?=\s)"
    r"|(?P<ticker>\bKX[A-Z0-9]+-[0-9A-Z]+(?P<strike>-[TB][0-9.]+)?)"
    r"|(?P<yes>\bYES\b)|(?P<no>\bNO\b)"
    r"|(?P<signed>(?<![\w.])[+-]\$?[0-9][0-9,]*(?:\.[0-9]+)?[¢%]?)"
    r"|(?P<money>\$[0-9][0-9,]*(?:\.[0-9]+)?)"
    r"|(?P<slot>\b[0-9]+m slot\b)"
    r"|(?P<good>\b(?:bought|authenticated|state saved)\b)"
    r"|(?P<bad>\b(?:KILL|rejected|failed|unreadable|error|stopped for this slot)\b)"
    r"|(?P<num>\b[0-9]+(?:\.[0-9]+)?\b)"
    r"|(?P<sep>[·→]|->)")

# ---------------------------------------------------------------- formatting

def money(x, sign: bool = False) -> str:
    if x is None:
        return "-"
    s = f"${abs(x):,.2f}"
    if x < 0:
        return "-" + s
    return ("+" + s) if sign else s


def clock(sec: float) -> str:
    s = int(max(0, sec))
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}:{s % 60:02d}"


def cents(p) -> str:
    if p is None:
        return "  - "
    c = p * 100
    return f"{c:4.1f}" if c < 10 or c > 90 else f"{c:4.0f}"


def strike_label(k) -> str:
    return f"{k + 0.01:,.0f}" if k is not None else "?"


# ---------------------------------------------------------------- braille

BITS = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]]


def resample(vals: list[float], n: int) -> list[float]:
    if len(vals) <= n:
        return vals
    out = []
    step = len(vals) / n
    for i in range(n):
        seg = vals[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]
        out.append(seg[-1])
    return out


def by_time(points: list, t0: float, t1: float, n: int) -> list[float]:
    """Sample [ts, value] points (any density, oldest first) into n columns over [t0, t1]:
    each column takes the last value at or before its end; columns before the first point are skipped."""
    out, j, last = [], 0, None
    step = (t1 - t0) / max(1, n)
    for i in range(n):
        end = t0 + (i + 1) * step
        while j < len(points) and points[j][0] <= end:
            last = points[j][1]; j += 1
        if last is not None:
            out.append(last)
    return out


def braille(vals: list[float], w: int, h: int, lo: float, hi: float, filled: bool = True,
            marks: list[float] | None = None) -> tuple[list[list[int]], list[list[int]]]:
    """Render vals (oldest first, right-aligned) into an h x w grid of braille codes.
    Returns (codes, kind) where kind 1 = data dot, 2 = mark-only dot."""
    W, Hp = w * 2, h * 4
    codes = [[0] * w for _ in range(h)]
    kind = [[0] * w for _ in range(h)]
    span = (hi - lo) or 1.0

    def py(v):
        return max(0, min(Hp - 1, int(round((hi - v) / span * (Hp - 1)))))

    vals = resample(vals, W)
    x0 = W - len(vals)
    prev = None
    for i, v in enumerate(vals):
        x = x0 + i
        y = py(v)
        if filled:
            ys = range(y, Hp)
        else:
            ys = range(min(y, prev), max(y, prev) + 1) if prev is not None else [y]
        for yy in ys:
            codes[yy // 4][x // 2] |= BITS[yy % 4][x % 2]
            kind[yy // 4][x // 2] = 1
        prev = y
    for mv in marks or []:
        if not lo <= mv <= hi:
            continue
        y = py(mv)
        for x in range(0, W, 2):
            r, c = y // 4, x // 2
            if kind[r][c] != 1:
                codes[r][c] |= BITS[y % 4][x % 2]
                kind[r][c] = 2
    return codes, kind


# ---------------------------------------------------------------- engine control

def engine_alive(runtime: str) -> bool:
    lock = os.path.join(runtime, "engine.lock")
    if not os.path.exists(lock):
        return False
    with open(lock, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def start_engine(runtime: str) -> str:
    if engine_alive(runtime):
        return "engine already running"
    os.makedirs(runtime, exist_ok=True)
    out = open(os.path.join(runtime, "engine.out"), "a")
    subprocess.Popen([sys.executable, "-m", "tern", "run"], cwd=cfgmod.ROOT, stdout=out, stderr=out,
                     start_new_session=True, env={**os.environ, "TERN_RUNTIME": runtime})
    return "engine started in the background"


HELP = """\
tern trades Kalshi's hourly BTC/ETH above-or-below markets.

THE RULE  Kalshi prices long shots too high and favorites too low. At 25,
20, 15 and 10 minutes before each hourly close, the rule buys the favorite
side of every strike whose mid is 75-95c (spread 2c or less) at the ask
and holds it to settlement. Over 67 days of BTC history this variant made
+0.6c a contract on data it was not chosen on (t=1.1): positive, small,
unproven. Paper always runs it. Live runs it only after the gate (g).

MANUAL    Move to a strike with the arrows. b buys YES, n buys NO at the
touch (immediate-or-cancel), x closes your position there at the touch.
+/- change the size. In live every order asks first.

LIVE      L arms live: it asks you to type "live". Needs KALSHI_KEY_ID and
KALSHI_KEY_FILE (in ~/.config/tern/kalshi.env). Caps: live.capital_usd at
risk, live.max_order_usd per order, live.max_daily_loss turns live off.

  ↑↓ PgUp PgDn  move in the ladder     b / n / x   buy YES / buy NO / close
  mouse wheel   scrolls the panel under the pointer: ladder, log, open positions
  + -           order size             tab         BTC <-> ETH
  1-6           show / hide a box: spot, contract, ladder, book, equity, log
  a             rule on/off            r           rule trades this coin on/off
  c             chart: 15m / 24h       v           book shown: paper / live
  L             arm / disarm live      g           gate and checklist
  R             reset paper            s           start the engine
  ?             this help              q           close (the engine keeps running)
"""

SIZES = [1, 2, 5, 10, 25, 50, 100, 250]

# the boxes, in the order their number keys toggle them, as rows of (left, right)
PANELS = ["spot", "contract", "ladder", "book", "equity", "log"]
ROWS = [("spot", "contract", 0.36, 0.6), ("ladder", "book", 0.44, 0.52), ("equity", "log", 0.20, 0.4)]   # (.., height share, left width share)
SUPER = "¹²³⁴⁵⁶⁷⁸⁹"
LEFT_MIN = {"ladder": 56}    # the ladder's columns need this much before its bar has any room


class Screen:
    def __init__(self, stdscr, runtime: str):
        self.s = stdscr
        self.runtime = runtime
        self.fast = live_dir(runtime)
        self.cmd_dir = os.path.join(runtime, "cmd")
        os.makedirs(self.cmd_dir, exist_ok=True)
        self.st: dict = {}
        self.series = "KXBTCD"
        self.sel: str | None = None
        self.size_i = 2
        self.chart = "15m"
        self.book_view = None
        self.overlay: list[str] | None = None
        self.modal: dict | None = None
        self.flash, self.flash_at = "", 0.0
        self.pending: dict[str, float] = {}
        self.alive, self.alive_at = False, 0.0
        self.P = Pal()
        self.clip: int | None = None
        self.rects: dict[str, tuple[int, int, int, int]] = {}   # panel -> (y, x, h, w) as last drawn
        self.log_anchor: float | None = None   # ts of the bottom log line while scrolled back; None follows new lines
        self.pos_off = 0                       # first open position shown in the book
        self.prefs_path = os.path.join(runtime, "dash.json")
        self.hidden: set[str] = set()          # boxes turned off with 1-6
        self.panel_key = ""                    # the number drawn in the title of the box being drawn
        try:
            with open(self.prefs_path) as fh:
                self.hidden = set(json.load(fh).get("hidden", [])) & set(PANELS)
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass

    # ------------------------------------------------------------ io
    def read(self) -> None:
        try:
            with open(os.path.join(self.fast, "status.json")) as fh:
                self.st = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
        for cid in list(self.pending):
            res = (self.st.get("results") or {}).get(cid)
            if res:
                self.say(res); self.pending.pop(cid)
            elif time.time() - self.pending[cid] > 15:
                self.say("no answer from the engine (is it running? press s)"); self.pending.pop(cid)

    def write_view(self) -> None:
        tmp = os.path.join(self.fast, "view.json.tmp")
        with open(tmp, "w") as fh:
            json.dump({"series": self.series, "ticker": self.sel}, fh)
        os.replace(tmp, os.path.join(self.fast, "view.json"))

    def save_prefs(self) -> None:
        try:
            with open(self.prefs_path + ".tmp", "w") as fh:
                json.dump({"hidden": sorted(self.hidden)}, fh)
            os.replace(self.prefs_path + ".tmp", self.prefs_path)
        except OSError:
            pass

    def toggle(self, i: int) -> None:
        name = PANELS[i]
        self.hidden ^= {name}
        self.save_prefs()
        self.say(f"{name} {'hidden' if name in self.hidden else 'shown'}"
                 + (" · every box is hidden: 1-6 bring them back" if self.hidden >= set(PANELS) else ""))

    def layout(self, H: int, W: int) -> list[tuple[str, int, int, int, int]]:
        """(panel, y, x, h, w) for every box shown. A row's survivor takes the full width; rows share the height."""
        rows = [(l, r, share, lw) for l, r, share, lw in ROWS if l not in self.hidden or r not in self.hidden]
        avail, total = H - 2, sum(r[2] for r in rows)
        out, y = [], 1
        for i, (l, r, share, lws) in enumerate(rows):
            h = avail - (y - 1) if i == len(rows) - 1 else max(7, int(avail * share / total))
            shown = [n for n in (l, r) if n not in self.hidden]
            if len(shown) == 2:
                lw = max(int(W * lws), LEFT_MIN.get(l, 0))
                out += [(l, y, 0, h, lw), (r, y, lw, h, W - lw)]
            else:
                out.append((shown[0], y, 0, h, W))
            y += h
        return out

    def send(self, **cmd) -> None:
        cid = uuid.uuid4().hex[:12]
        cmd["id"] = cid
        cmd["ts"] = time.time()                      # the desk ignores commands older than a few seconds
        path = os.path.join(self.cmd_dir, f"{time.time():.6f}-{cid}.json")
        with open(path + ".tmp", "w") as fh:
            json.dump(cmd, fh)
        os.replace(path + ".tmp", path)
        self.pending[cid] = time.time()

    def say(self, msg: str) -> None:
        self.flash, self.flash_at = msg, time.time()

    # ------------------------------------------------------------ drawing primitives
    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        H, W = self.s.getmaxyx()
        if y < 0 or y >= H or x >= W:
            return
        if x < 0:
            text = text[-x:]; x = 0
        text = text[:W - x - (1 if y == H - 1 else 0)]
        if self.clip is not None:
            text = text[:max(0, self.clip - x)]
        if not text:
            return
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    def box(self, y: int, x: int, h: int, w: int, title: str, color: int, key: str = "", right: str = "") -> None:
        P = self.P
        a = P(color)
        key = key or self.panel_key
        saved, self.clip = self.clip, None
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", a)
        for r in range(1, h - 1):
            self.put(y + r, x, "│", a)
            self.put(y + r, x + w - 1, "│", a)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", a)
        cx = x + 2
        self.put(y, cx, "┤", a); cx += 1
        if key:
            self.put(y, cx, key, P(RED, bold=True)); cx += len(key)
        self.put(y, cx, title, P(WHITE, bold=True)); cx += len(title)
        self.put(y, cx, "├", a)
        if right:
            rx = x + w - 3 - len(right) - 2
            self.put(y, rx, "┤", a); self.put(y, rx + 1, right, P(self.P.grey)); self.put(y, rx + 1 + len(right), "├", a)
        self.clip = saved

    def graph(self, y: int, x: int, h: int, w: int, vals: list[float], lo: float, hi: float, grad: list[int],
              marks: list[float] | None = None, mark_color: int = YELLOW, filled: bool = True) -> None:
        if w <= 0 or h <= 0 or not vals:
            return
        codes, kind = braille(vals, w, h, lo, hi, filled, marks)
        P = self.P
        for r in range(h):
            g = grad[min(len(grad) - 1, int((h - 1 - r) / max(1, h - 1) * (len(grad) - 1) + 0.5))] if len(grad) > 1 else grad[0]
            ga, ma = P(g), P(mark_color)
            run, run_attr, run_x = "", None, x
            for c in range(w):
                code = codes[r][c]
                ch = chr(0x2800 + code) if code else " "
                at = ma if kind[r][c] == 2 else ga
                if at != run_attr and run:
                    self.put(y + r, run_x, run, run_attr); run, run_x = "", x + c
                run_attr = at; run += ch
            if run:
                self.put(y + r, run_x, run, run_attr)

    # ------------------------------------------------------------ panels
    def header(self, W: int) -> None:
        P, st = self.P, self.st
        now = time.time()
        hb = st.get("heartbeat") or 0
        ok = now - hb < 5
        mode = st.get("mode", "?")
        x = 0
        self.put(0, x, " tern ", P(CYAN, bold=True) | curses.A_REVERSE); x += 7
        if mode == "live":
            self.put(0, x, " ● LIVE ", P(RED, bold=True) | curses.A_REVERSE); x += 9
        else:
            armed = " (live armed: " + (st.get("live_error") or "authenticating") + ")" if st.get("live_enabled") else ""
            self.put(0, x, " ● PAPER ", P(GREEN, bold=True) | curses.A_REVERSE); x += 10
            if armed:
                self.put(0, x, armed[:60], P(YELLOW)); x += min(60, len(armed)) + 1
        prod = cfgmod.SERIES[self.series]
        sp = (st.get("spot") or {}).get(prod) or {}
        p = sp.get("now")
        mins = sp.get("min") or []
        chg = None
        if p and len(mins) > 60:
            chg = p / mins[-61][1] - 1
        seg = f" {prod.split('-')[0]} {money(p)} "
        self.put(0, x, seg, P(WHITE, bold=True)); x += len(seg)
        if chg is not None:
            seg = f"{'▲' if chg >= 0 else '▼'}{chg:+.2%} 1h "
            self.put(0, x, seg, P(GREEN if chg >= 0 else RED)); x += len(seg)
        ev = (st.get("events") or {}).get(self.series)
        if ev:
            tau = ev["close"] - now
            seg = f"│ {ev['event']} closes {clock(tau)} "
            self.put(0, x, seg, P(YELLOW if tau < 600 else WHITE)); x += len(seg)
        S = st.get("strategy") or {}
        if S:
            on = S.get("auto") and self.series in (S.get("series") or [])
            nxt = ""
            if ev and on:
                tau = ev["close"] - now
                ahead = [m for m in S.get("minutes", []) if m * 60 < tau]
                if ahead:
                    nxt = f" next {max(ahead)}m slot in {clock(tau - max(ahead) * 60)}"
            seg = f"│ rule {'ON' if on else 'off'}{nxt}{' +live' if st.get('auto_live') and on else ''} "
            self.put(0, x, seg, P(GREEN if on else self.P.grey)); x += len(seg)
        right = f" {'●' if ok else '○'} engine {'ok' if ok else ('stale' if hb else 'down — s starts it')}  {time.strftime('%H:%M:%S')} "
        self.put(0, W - len(right), right, P(GREEN if ok else RED))

    def spot_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        prod = cfgmod.SERIES[self.series]
        sp = (st.get("spot") or {}).get(prod) or {}
        now = time.time()
        sec, mins = sp.get("sec") or [], sp.get("min") or []
        span = 900 if self.chart == "15m" else 86400
        first_sec = sec[0][0] if sec else now
        pts = [[t + 60, p] for t, p in mins if t + 60 < first_sec] + sec   # minute closes, then live seconds
        gw = w - 2 - 11
        vals = by_time(pts, now - span, now, gw * 2)
        strike = None
        row = self.row(self.sel)
        if row:
            strike = row["k"] + 0.01
        self.box(y, x, h, w, prod.lower(), CYAN, "", f"{self.chart} · c")
        gh = h - 3
        if not vals:
            self.put(y + h // 2, x + 3, "waiting for spot…", P(self.P.grey)); return
        lo, hi = min(vals), max(vals)
        if strike and (hi - lo) and abs(strike - (lo + hi) / 2) < 3 * (hi - lo):
            lo, hi = min(lo, strike), max(hi, strike)
        pad = (hi - lo) * 0.08 or vals[-1] * 0.0005
        lo, hi = lo - pad, hi + pad
        self.graph(y + 1, x + 1, gh, gw, vals, lo, hi, P.grad("spot"), [strike] if strike else None)
        lx = x + 1 + gw + 1
        for i, v in enumerate([hi, (hi + lo) / 2, lo]):
            ry = y + 1 + [0, gh // 2, gh - 1][i]
            self.put(ry, lx, f"{v:,.0f}"[:10], P(self.P.grey))
        if strike and lo <= strike <= hi:
            ry = y + 1 + int(round((hi - strike) / (hi - lo) * (gh - 1)))
            self.put(ry, lx, f"{strike:,.0f}"[:10], P(YELLOW, bold=True))
        span = "-15m" if self.chart == "15m" else "-24h"
        self.put(y + h - 2, x + 2, span, P(self.P.grey))
        self.put(y + h - 2, x + 1 + gw - 3, "now", P(self.P.grey))
        if strike and vals:
            d = vals[-1] - strike
            self.put(y + h - 2, x + 2 + gw // 2 - 12, f"spot {'above' if d >= 0 else 'below'} strike by ${abs(d):,.0f}",
                     P(GREEN if d >= 0 else RED))

    def ladder(self) -> list[dict]:
        """Strikes high to low, like a price axis."""
        ev = (self.st.get("events") or {}).get(self.series) or {}
        return list(reversed(ev.get("ladder") or []))

    def row(self, ticker):
        ev = (self.st.get("events") or {}).get(self.series) or {}
        for r in ev.get("ladder") or []:
            if r["t"] == ticker:
                return r
        return None

    def contract_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        r = self.row(self.sel)
        title = f"yes {strike_label(r['k'])}" if r else "contract"
        self.box(y, x, h, w, title, MAGENTA, "", "")
        if not r:
            self.put(y + 2, x + 3, "pick a strike in the ladder", P(self.P.grey)); return
        hist = (st.get("hist") or {}).get(self.sel) or []
        pts = []
        for t, b, a in hist:
            if b is not None and a is not None:
                pts.append([t, (a + b) / 2])
            elif b is not None or a is not None:
                pts.append([t, b if b is not None else a])
        ev = (st.get("events") or {}).get(self.series) or {}
        t1 = time.time(); t0 = (ev.get("close") or t1) - 3600
        mids = by_time(pts, min(t0, pts[0][0]) if pts else t0, t1, (w - 8) * 2)
        bid, ask = r["b"], r["a"]
        mid = (bid + ask) / 2 if bid is not None and ask is not None else (bid or ask)
        info = [("bid", cents(bid) + "¢", GREEN), ("ask", cents(ask) + "¢", RED),
                ("prob", f"{(mid or 0)*100:.1f}%", WHITE), ("vol", f"{r['v']:,.0f}", self.P.grey)]
        cx = x + 2
        for k, v, c in info:
            self.put(y + 1, cx, k + " ", P(self.P.grey)); cx += len(k) + 1
            self.put(y + 1, cx, v + "  ", P(c, bold=True)); cx += len(v) + 2
        gh, gw = h - 4, w - 2 - 6
        if mids and gh > 0:
            lo, hi = max(0.0, min(mids) - 0.05), min(1.0, max(mids) + 0.05)
            S = st.get("strategy") or {}
            marks = [m for m in (S.get("band_lo"), 1 - (S.get("band_lo") or 0)) if m is not None]
            self.graph(y + 2, x + 1, gh, gw, mids, lo, hi, P.grad("yes"), marks, self.P.grey)
            self.put(y + 2, x + 1 + gw + 1, f"{hi*100:3.0f}¢", P(self.P.grey))
            self.put(y + 2 + gh - 1, x + 1 + gw + 1, f"{lo*100:3.0f}¢", P(self.P.grey))
        else:
            self.put(y + 3, x + 3, "history builds while the screen watches this strike", P(self.P.grey))
        S = st.get("strategy") or {}
        if mid is not None and S:
            fav = mid if mid >= 0.5 else 1 - mid
            spread = (ask - bid) if bid is not None and ask is not None else 1
            inb = S["band_lo"] <= fav < S["band_hi"] and spread <= S["max_spread"] + 1e-9
            msg = (f"★ in the rule's band: favorite {'YES' if mid >= 0.5 else 'NO'} at {fav*100:.0f}¢" if inb
                   else f"outside the rule's band ({S['band_lo']*100:.0f}-{S['band_hi']*100:.0f}¢, spread ≤{S['max_spread']*100:.0f}¢)")
            self.put(y + h - 2, x + 2, msg[:w - 4], P(YELLOW if inb else self.P.grey))

    def ladder_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        lad = self.ladder()
        size = SIZES[self.size_i]
        self.box(y, x, h, w, "ladder", YELLOW, "", f"size {size} · ↑↓ b n x")
        if not lad:
            self.put(y + 2, x + 3, "waiting for the hourly event…", P(self.P.grey)); return
        prod = cfgmod.SERIES[self.series]
        spot = ((st.get("spot") or {}).get(prod) or {}).get("now")
        tickers = [r["t"] for r in lad]
        if self.sel not in tickers:
            if spot:
                self.sel = min(lad, key=lambda r: abs(r["k"] - spot))["t"]
            else:
                self.sel = tickers[len(tickers) // 2]
            self.write_view()
        i = tickers.index(self.sel)
        rows = h - 3
        top = max(0, min(len(lad) - rows, i - rows // 2))
        book = self.cur_book()
        held = {p["t"]: p for p in (book or {}).get("pos", [])}
        S = st.get("strategy") or {}
        hdr = f"{'strike':>9} {'bid':>5} {'ask':>5}  {'prob':<{max(6, w - 47)}} {'sprd':>4} {'vol':>7} {'pos':>6}"
        self.put(y + 1, x + 2, hdr[:w - 4], P(self.P.grey, bold=True))
        barw = max(6, w - 47)
        for j, r in enumerate(lad[top:top + rows]):
            yy = y + 2 + j
            bid, ask = r["b"], r["a"]
            mid = (bid + ask) / 2 if bid is not None and ask is not None else (bid if bid is not None else ask)
            fav = max(mid, 1 - mid) if mid is not None else 0
            spread = (ask - bid) if bid is not None and ask is not None else None
            inb = S and S["band_lo"] <= fav < S["band_hi"] and spread is not None and spread <= S["max_spread"] + 1e-9
            above = spot is not None and spot >= r["k"] + 0.01
            sel = r["t"] == self.sel
            base = curses.A_REVERSE if sel else 0
            self.put(yy, x + 1, " " * (w - 2), base)
            kc = GREEN if above else RED
            self.put(yy, x + 1, "★" if inb else " ", P(YELLOW, bold=True) | base)
            self.put(yy, x + 2, f"{strike_label(r['k']):>9}", P(kc, bold=sel) | base)
            self.put(yy, x + 12, f"{cents(bid):>5}", P(GREEN) | base)
            self.put(yy, x + 18, f"{cents(ask):>5}", P(RED) | base)
            # probability bar
            frac = max(0.0, min(1.0, mid or 0))
            full = frac * barw
            n8 = int(full * 8)
            bar = "█" * (n8 // 8) + (" ▏▎▍▌▋▊▉"[n8 % 8] if n8 % 8 else "")
            g = P.grad("yes")
            col = g[min(len(g) - 1, int(frac * (len(g) - 1) + 0.5))]
            self.put(yy, x + 25, bar.ljust(barw), P(col) | base)
            self.put(yy, x + 25 + barw - 4, f"{(mid or 0)*100:3.0f}%", P(WHITE, bold=True) | base)
            self.put(yy, x + 26 + barw, f"{(spread*100 if spread is not None else 0):>4.1f}", P(self.P.grey) | base)
            self.put(yy, x + 31 + barw, f"{r['v']:>7,.0f}"[:7], P(self.P.grey) | base)
            hp = held.get(r["t"])
            if hp:
                self.put(yy, x + 39 + barw, f"{hp['q']:+6g}"[:6], P(GREEN if hp["q"] > 0 else MAGENTA, bold=True) | base)
        if top > 0:
            self.put(y, x + w - 2, "▲", P(YELLOW))
        if top + rows < len(lad):
            self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))

    def cur_book(self):
        v = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
        return self.st.get(v)

    def book_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        v = self.book_view or ("live" if st.get("mode") == "live" else "paper")
        b = st.get(v) or {}
        self.box(y, x, h, w, "book", GREEN, "", "v paper/live")
        tabs = x + 2
        for name in ("paper", "live"):
            a = P(WHITE, bold=True) | curses.A_REVERSE if name == v else P(self.P.grey)
            self.put(y + 1, tabs, f" {name} ", a); tabs += len(name) + 3
        if not b:
            return
        eq, start = b.get("equity"), b.get("start") or 0
        pnl = (eq - start) if eq is not None and start else None
        lines = [("equity", money(eq), WHITE), ("P&L", money(pnl, True) + (f"  {pnl/start:+.2%}" if pnl is not None and start else ""),
                                                 GREEN if (pnl or 0) >= 0 else RED),
                 ("cash", money(b.get("cash")), WHITE), ("at risk", money(b.get("at_risk")), YELLOW),
                 ("fees", money(b.get("fees")), self.P.grey)]
        if v == "live":
            L = st.get("live_cfg") or {}
            lines.append(("caps", f"risk {money(L.get('capital_usd'))} · order {money(L.get('max_order_usd'))} · day -{money(L.get('max_daily_loss'))}", self.P.grey))
        yy = y + 2
        for k, val, c in lines:
            self.put(yy, x + 2, f"{k:<8}", P(self.P.grey)); self.put(yy, x + 11, val[:w - 13], P(c, bold=k == "equity")); yy += 1
        g = st.get("gate") or {}
        if g:
            wr = f"{g['win']:.0%}" if g.get("win") is not None else "-"
            self.put(yy, x + 2, f"rule   ", P(self.P.grey))
            self.put(yy, x + 11, f"{g['entries']} settled · {g['open']} open · {money(g['pnl'], True)} · win {wr} · "
                                 f"{g['per_contract']*100:+.2f}¢/ct"[:w - 13], P(GREEN if g["pnl"] >= 0 else RED))
            yy += 1
        yy += 1
        pos = b.get("pos") or []
        self.put(yy, x + 2, f"{'strike':>9} {'side':<4} {'qty':>6} {'avg':>5} {'mark':>5} {'P&L':>8} {'closes':>7}"[:w - 4], P(self.P.grey, bold=True))
        yy += 1
        now = time.time()
        room = max(0, y + h - 1 - yy)
        self.pos_off = max(0, min(self.pos_off, len(pos) - room))
        if self.pos_off > 0:
            self.put(yy, x + w - 2, "▲", P(YELLOW))
        if self.pos_off + room < len(pos):
            self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))
        for p in pos[self.pos_off:self.pos_off + room]:
            side = "YES" if p["q"] > 0 else "NO"
            pl = p.get("pnl")
            self.put(yy, x + 2, f"{strike_label(p.get('k')):>9} ", P(WHITE))
            self.put(yy, x + 12, f"{side:<4}", P(GREEN if side == "YES" else MAGENTA, bold=True))
            self.put(yy, x + 16, f" {abs(p['q']):>6g} {cents(p['avg']):>5} {cents(p.get('mark')):>5} ", P(WHITE))
            self.put(yy, x + 37, f"{money(pl, True):>8}", P(GREEN if (pl or 0) >= 0 else RED))
            self.put(yy, x + 46, f" {clock((p.get('close') or now) - now):>7}", P(self.P.grey))
            yy += 1
        if not pos:
            self.put(yy, x + 2, "no open positions", P(self.P.grey))

    def equity_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        v = self.book_view or ("live" if st.get("mode") == "live" else "paper")
        b = st.get(v) or {}
        curve = [e for _, e in (b.get("curve") or [])]
        self.box(y, x, h, w, f"equity · {v}", BLUE, "", f"{len(curve)} min")
        if len(curve) < 2:
            self.put(y + h // 2, x + 3, "the curve starts after a couple of minutes", P(self.P.grey)); return
        lo, hi = min(curve), max(curve)
        pad = (hi - lo) * 0.1 or 1.0
        start = b.get("start") or curve[0]
        gw = w - 2 - 11
        self.graph(y + 1, x + 1, h - 2, gw, curve, lo - pad, hi + pad,
                   P.grad("eq_up" if curve[-1] >= start else "eq_dn"), [start], self.P.grey)
        self.put(y + 1, x + 1 + gw + 1, money(hi)[:10], P(self.P.grey))
        self.put(y + h - 2, x + 1 + gw + 1, money(lo)[:10], P(self.P.grey))

    def log_panel(self, y, x, h, w) -> None:
        P = self.P
        self.box(y, x, h, w, "log", self.P.grey, "", "")
        logs = self.st.get("logs") or []
        rows = h - 2
        end = self.log_end(logs, rows)
        saved, self.clip = self.clip, x + w - 2
        for j, (ts, lvl, msg) in enumerate(logs[max(0, end - rows):end]):
            yy = y + 1 + j
            self.put(yy, x + 2, time.strftime("%H:%M:%S", time.localtime(ts)), P(self.P.grey))
            self.put(yy, x + 10, LOG_MARK.get(lvl, " "), P(LOG_BASE.get(lvl, WHITE), bold=True))
            cx = x + 11
            for text, attr in self.log_spans(msg, lvl):
                self.put(yy, cx, text, attr); cx += len(text)
                if cx >= x + w - 2:
                    break
        self.clip = saved
        if end - rows > 0:
            self.put(y, x + w - 2, "▲", P(YELLOW))
        if end < len(logs):
            note = f"▼ {len(logs) - end} newer"
            self.put(y + h - 1, x + w - 2 - len(note), note, P(YELLOW))

    def log_spans(self, msg: str, lvl: str) -> list[tuple[str, int]]:
        """Split one log line into colored pieces: book names, tickers, YES/NO, signed money, slots, numbers."""
        P, many = self.P, self.P.many
        base = LOG_BASE.get(lvl)
        plain = P(base) if base is not None else P(250 if many else WHITE)
        out, i = [], 0
        for m in LOG_TOKENS.finditer(msg):
            if m.start() > i:
                out.append((msg[i:m.start()], plain))
            g, t = m.lastgroup, m.group()
            if g == "book":
                name = t[:-1]
                out.append((name, P(LOG_BOOK.get(name.lower(), YELLOW), bold=True)))
                out.append((":", P(self.P.grey)))
            elif g == "ticker":
                ev, _, k = t.rpartition("-") if m.group("strike") else (t, "", "")
                out.append((ev, P(73 if many else CYAN)))
                if k:
                    out.append(("-", P(self.P.grey)))
                    out.append((k, P(WHITE, bold=True)))
            elif g == "yes":
                out.append((t, P(GREEN, bold=True)))
            elif g == "no":
                out.append((t, P(MAGENTA, bold=True)))
            elif g == "signed":
                out.append((t, P(GREEN if t[0] == "+" else RED, bold=True)))
            elif g == "money":
                out.append((t, P(YELLOW)))
            elif g == "slot":
                out.append((t, P(214 if many else YELLOW, bold=True)))
            elif g == "good":
                out.append((t, P(GREEN)))
            elif g == "bad":
                out.append((t, P(RED, bold=True)))
            elif g == "num":
                out.append((t, P(WHITE, bold=True)))
            else:   # sep
                out.append((t, P(self.P.grey)))
            i = m.end()
        if i < len(msg):
            out.append((msg[i:], plain))
        return out

    def log_end(self, logs: list, rows: int) -> int:
        """One past the bottom log line shown: the newest, or the line scrolled back to."""
        if self.log_anchor is None:
            return len(logs)
        end = 0
        for k, e in enumerate(logs):
            if e[0] <= self.log_anchor:
                end = k + 1
        return max(end, min(rows, len(logs)))

    def wheel(self, my: int, mx: int, d: int) -> None:
        """Mouse wheel over a panel: d is -1 (up) or +1 (down)."""
        name = next((n for n, (y, x, h, w) in self.rects.items() if y <= my < y + h and x <= mx < x + w), None)
        if name == "ladder":
            self.step_ladder(d)
        elif name == "book":
            self.pos_off = max(0, self.pos_off + d)    # book_panel clamps the far end
        elif name == "log":
            logs = self.st.get("logs") or []
            rows = self.rects["log"][2] - 2
            end = max(min(rows, len(logs)), min(len(logs), self.log_end(logs, rows) + 3 * d))
            self.log_anchor = None if end >= len(logs) else logs[end - 1][0]

    def step_ladder(self, d: int) -> None:
        tickers = [r["t"] for r in self.ladder()]
        if not tickers:
            return
        i = tickers.index(self.sel) if self.sel in tickers else len(tickers) // 2
        self.sel = tickers[max(0, min(len(tickers) - 1, i + d))]
        self.write_view()

    def footer(self, H: int, W: int) -> None:
        P = self.P
        if self.flash and time.time() - self.flash_at < 8:
            self.put(H - 1, 0, (" " + self.flash).ljust(W), P(YELLOW, bold=True) | curses.A_REVERSE); return
        keys = [("b", "uy yes"), ("n", "o"), ("x", " close"), ("+-", " size"), ("1-6", " boxes"), ("tab", " coin"), ("a", "uto"),
                ("r", "ule coin"), ("c", "hart"), ("v", "iew"), ("L", "ive"), ("g", "ate"), ("?", "help"), ("q", "uit")]
        x = 1
        for k, rest in keys:
            if x + len(k) + len(rest) + 2 > W:
                break
            self.put(H - 1, x, k, P(RED, bold=True)); x += len(k)
            self.put(H - 1, x, rest + "  ", P(WHITE)); x += len(rest) + 2

    def draw_overlay(self, lines: list[str], title: str, color: int = CYAN) -> None:
        H, W = self.s.getmaxyx()
        w = min(W - 4, max(len(l) for l in lines) + 4)
        h = min(H - 2, len(lines) + 2)
        y, x = (H - h) // 2, (W - w) // 2
        for r in range(h):
            self.put(y + r, x, " " * w)
        self.box(y, x, h, w, title, color)
        for j, l in enumerate(lines[:h - 2]):
            self.put(y + 1 + j, x + 2, l[:w - 4], self.P(WHITE))

    def gate_lines(self) -> list[str]:
        st = self.st
        g = st.get("gate") or {}
        L = st.get("live_cfg") or {}
        out = ["The rule trades live only when every paper check passes", "(unless live.auto_needs_gate is false).", ""]
        for c in g.get("checks", []):
            out.append(f"  [{'x' if c['ok'] else ' '}] {c['name']}  ({c['detail']})")
        if st.get("shadow"):
            from .shadow import table
            out += ["", "Variants running side by side on paper (tern report):"] + ["  " + l for l in table(st["shadow"])]
        out += ["", "To go live at all:"]
        out.append(f"  [{'x' if st.get('keys') else ' '}] Kalshi key present ({cfgmod.KEYFILE_ENV})")
        out.append(f"  [{'x' if st.get('live_enabled') else ' '}] armed with L")
        out.append(f"  [{'x' if st.get('mode') == 'live' else ' '}] authenticated" + (f": {st.get('live_error')}" if st.get("live_error") else ""))
        out += ["", f"Caps: ${L.get('capital_usd', 0):,.0f} at risk · ${L.get('max_order_usd', 0):,.0f} per order · "
                    f"-${L.get('max_daily_loss', 0):,.0f} a day turns live off", "", "any key closes"]
        return out

    def draw(self) -> None:
        self.s.erase()
        H, W = self.s.getmaxyx()
        if W < 100 or H < 30:
            self.put(0, 0, f"tern needs at least 100x30 (now {W}x{H})", self.P(RED, bold=True)); self.s.refresh(); return
        self.header(W)
        self.rects = {}
        boxes = self.layout(H, W)
        for name, y, x, h, w in boxes:
            self.rects[name] = (y, x, h, w)
            self.clip = x + w - 1
            self.panel_key = SUPER[PANELS.index(name)]
            getattr(self, name + "_panel")(y, x, h, w)
        self.clip = None
        self.panel_key = ""
        if not boxes:
            msg = "every box is hidden · press 1-6 to show spot, contract, ladder, book, equity, log"
            self.put(H // 2, max(0, (W - len(msg)) // 2), msg, self.P(self.P.grey))
        self.footer(H, W)
        if self.overlay:
            self.draw_overlay(self.overlay, "help" if self.overlay is HELP_LINES else "gate", CYAN)
        if self.modal:
            self.draw_overlay(self.modal["lines"], self.modal["title"], self.modal.get("color", YELLOW))
        self.s.refresh()

    # ------------------------------------------------------------ input
    def order(self, action: str) -> None:
        r = self.row(self.sel)
        if not r:
            self.say("no strike selected"); return
        n = SIZES[self.size_i]
        live = self.st.get("mode") == "live"
        book = self.st.get("live" if live else "paper") or {}
        if action == "close":
            held = {p["t"]: p for p in book.get("pos", [])}.get(r["t"])
            if not held:
                self.say(f"no {'live' if live else 'paper'} position at {strike_label(r['k'])}"); return
            n = abs(held["q"])
            px = r["b"] if held["q"] > 0 else r["a"]
            desc = f"close {n:g} {'YES' if held['q'] > 0 else 'NO'} @ {cents(px if held['q'] > 0 else (1 - px if px is not None else None))}¢"
            unit = px if held["q"] > 0 else (1 - px if px is not None else None)
        elif action == "buy_yes":
            px = r["a"]; unit = px; desc = f"buy {n} YES @ {cents(px)}¢"
        else:
            px = r["b"]; unit = None if px is None else 1 - px; desc = f"buy {n} NO @ {cents(unit)}¢"
        if px is None:
            self.say("nothing on that side of the book"); return
        fee = taker_fee(n, px)
        cmd = {"op": "order", "ticker": r["t"], "action": action, "n": n if action != "close" else 0,
               "book": "live" if live else "paper", "px": px}      # the desk refuses another book or a worse price
        if live:
            self.modal = {"title": "LIVE ORDER", "color": RED, "cmd": cmd,
                          "lines": [f"{desc} on {r['t']}", f"strike {strike_label(r['k'])}  ·  ${n*unit:,.2f} + fee ≈ ${fee:.2f}",
                                    "immediate-or-cancel at the touch, real money", "", "enter sends · any other key cancels"]}
        else:
            self.send(**cmd); self.say(f"paper: {desc} sent")

    def key(self, ch: int) -> bool:
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bs = curses.getmouse()
            except curses.error:
                return True
            d = -1 if bs & curses.BUTTON4_PRESSED else 1 if bs & curses.BUTTON5_PRESSED else 0
            if d and not self.modal and not self.overlay:
                self.wheel(my, mx, d)
            return True
        if self.modal:
            m = self.modal; self.modal = None
            if m.get("type") == "arm":
                if ch in (10, 13, curses.KEY_ENTER):
                    if m["buf"].strip().lower() == "live":
                        self.send(op="live", on=True); self.say("live armed")
                    else:
                        self.say("not armed (type live exactly)")
                    return True
                if ch in (27,):
                    self.say("not armed"); return True
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    m["buf"] = m["buf"][:-1]
                elif 32 <= ch < 127:
                    m["buf"] += chr(ch)
                m["lines"][-1] = "> " + m["buf"]
                self.modal = m
                return True
            if m.get("type") == "reset":
                if ch in (ord("y"), ord("Y")):
                    self.send(op="reset_paper")
                return True
            if ch in (10, 13, curses.KEY_ENTER) and m.get("cmd"):
                self.send(**m["cmd"]); self.say("live order sent")
            else:
                self.say("cancelled")
            return True
        if self.overlay:
            self.overlay = None; return True
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, ord("k"), ord("j")):
            self.step_ladder({curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                              curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10}[ch])
        elif ch == ord("b"):
            self.order("buy_yes")
        elif ch == ord("n"):
            self.order("buy_no")
        elif ch == ord("x"):
            self.order("close")
        elif ch in (ord("+"), ord("=")):
            self.size_i = min(len(SIZES) - 1, self.size_i + 1)
        elif ch in (ord("-"), ord("_")):
            self.size_i = max(0, self.size_i - 1)
        elif ord("1") <= ch <= ord(str(len(PANELS))):
            self.toggle(ch - ord("1"))
        elif ch == 9:
            self.series = "KXETHD" if self.series == "KXBTCD" else "KXBTCD"
            self.sel = None; self.write_view(); self.say(f"viewing {cfgmod.SERIES[self.series]}")
        elif ch == ord("a"):
            on = not (self.st.get("strategy") or {}).get("auto")
            self.send(op="auto", on=on)
        elif ch == ord("r"):
            self.send(op="series", series=self.series)
        elif ch == ord("c"):
            self.chart = "24h" if self.chart == "15m" else "15m"
        elif ch == ord("v"):
            cur = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
            self.book_view = "live" if cur == "paper" else "paper"
        elif ch == ord("g"):
            self.overlay = self.gate_lines()
        elif ch == ord("?") or ch == ord("h"):
            self.overlay = HELP_LINES
        elif ch == ord("s"):
            self.say(start_engine(self.runtime))
        elif ch == ord("R"):
            self.modal = {"type": "reset", "title": "reset paper", "lines": ["Wipe the paper book and the rule's paper record?",
                                                                               "This also restarts the gate's clock.", "", "y resets · any other key keeps it"]}
        elif ch == ord("L"):
            if self.st.get("live_enabled"):
                self.send(op="live", on=False); self.say("disarming live")
            else:
                L = self.st.get("live_cfg") or {}
                self.modal = {"type": "arm", "title": "ARM LIVE", "color": RED, "buf": "",
                              "lines": ["Live trading uses real money on Kalshi.",
                                        f"Caps: ${L.get('capital_usd', 0):,.0f} at risk, ${L.get('max_order_usd', 0):,.0f} per order,",
                                        f"live turns itself off after -${L.get('max_daily_loss', 0):,.0f} in a day.",
                                        "Manual orders go live at once; the rule goes live only past the gate (g).",
                                        *([] if self.st.get("keys") else ["", f"No key found yet: live stays off until {cfgmod.KEYFILE_ENV} exists."]),
                                        "", "type live and press enter (esc cancels):", "> "]}
        return True

    def loop(self) -> None:
        curses.curs_set(0)
        self.s.nodelay(True)
        self.s.keypad(True)
        curses.set_escdelay(25)
        # Wheel only, so the wheel scrolls the panel under the pointer. Clicks stay with
        # the terminal; shift+drag still selects text in foot.
        curses.mousemask(curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)
        curses.mouseinterval(0)
        if not engine_alive(self.runtime):
            self.say(start_engine(self.runtime))
        self.write_view()
        last = 0.0
        while True:
            now = time.time()
            if now - last >= 0.25:
                self.read(); self.draw(); last = now
            ch = self.s.getch()
            if ch == -1:
                time.sleep(0.03); continue
            if ch == curses.KEY_RESIZE:
                self.draw(); continue
            if not self.key(ch):
                return
            self.draw()


HELP_LINES = HELP.splitlines()


def main(runtime: str = cfgmod.RUNTIME) -> None:
    os.environ.setdefault("ESCDELAY", "25")
    # Name the terminal window, so the taskbar and title bar say "tern".
    if sys.stdout.isatty():
        sys.stdout.write("\033]2;tern\007"); sys.stdout.flush()
    curses.wrapper(lambda s: Screen(s, runtime).loop())
