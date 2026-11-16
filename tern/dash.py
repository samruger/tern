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
from .engine import live_dir

# ---------------------------------------------------------------- colors

BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)
PINK = 205                                  # 256-color: box numbers and key hints on the borders

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


def ago(sec: float) -> str:
    s = int(max(0, sec))
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s / 3600:.1f}h"


def volume(v: float) -> str:
    """Contracts traded, in at most 7 characters: 182,103 · 1.23M · 45.6M."""
    return f"{v:,.0f}" if v < 999_999.5 else f"{v / 1e6:.2f}M" if v < 99.995e6 else f"{v / 1e6:.0f}M"


def strike_of(k: float) -> float:
    """Kalshi's floor_strike sits one tick under the round number (66799.99, 74.4999, 0.8599): add that tick back."""
    s = repr(float(k))
    d = len(s.split(".")[1]) if "." in s and "e" not in s else 0
    return round(k + 10 ** -d, d) if d and s.endswith("9") else k


def px(v: float) -> str:
    """A coin price at a precision that suits its size: 66,800 · 604.99 · 74.75 · 0.8600."""
    a = abs(v)
    return f"{v:,.0f}" if a >= 1000 else f"{v:,.2f}" if a >= 10 else f"{v:.3f}" if a >= 1 else f"{v:.4f}"


def strike_label(k) -> str:
    return px(strike_of(k)) if k is not None else "?"


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


def service_runs(runtime: str) -> bool:
    """True when the tern systemd user service is installed for this runtime: then systemd owns the
    engine, and starting or stopping it goes through systemctl (a plain kill would be restarted)."""
    try:
        env = subprocess.run(["systemctl", "--user", "show", "tern.service", "-p", "Environment", "-p", "LoadState"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "LoadState=loaded" in env and f"TERN_RUNTIME={os.path.realpath(runtime)}" in env


def start_engine(runtime: str) -> str:
    if engine_alive(runtime):
        return "engine already running"
    if service_runs(runtime):
        r = subprocess.run(["systemctl", "--user", "start", "--no-block", "tern.service"], capture_output=True, text=True)
        return "starting the engine (tern.service)" if r.returncode == 0 else f"systemctl start failed: {r.stderr.strip()}"
    os.makedirs(runtime, exist_ok=True)
    out = open(os.path.join(runtime, "engine.out"), "a")
    subprocess.Popen([sys.executable, "-m", "tern", "run"], cwd=cfgmod.ROOT, stdout=out, stderr=out,
                     start_new_session=True, env={**os.environ, "TERN_RUNTIME": runtime})
    return "engine started in the background"


def stop_engine(runtime: str, pid: int | None) -> str:
    if not engine_alive(runtime):
        return "engine is not running"
    if service_runs(runtime):
        r = subprocess.run(["systemctl", "--user", "stop", "--no-block", "tern.service"], capture_output=True, text=True)
        return "stopping the engine (tern.service): it saves first" if r.returncode == 0 else f"systemctl stop failed: {r.stderr.strip()}"
    try:                                   # started by hand or by the dash: SIGTERM, which saves and exits
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            if b"tern" not in fh.read():
                raise OSError
        os.kill(pid, 15)
        return "stopping the engine: it saves first"
    except (OSError, TypeError):
        return "could not find the engine's process"


HELP = """\
tern trades Kalshi's hourly crypto above-or-below markets (the rule: BTC only).

THE RULE  Kalshi prices long shots too high and favorites too low. At 25,
20, 15 and 10 minutes before each hourly close, the rule buys the favorite
side of every strike whose mid is 75-95c (spread 2c or less) at the ask
and holds it to settlement. Over 67 days of BTC history this variant made
+0.6c a contract on data it was not chosen on (t=1.1): positive, small,
unproven. Paper always runs it. Live runs it only after the gate (g).

There are no manual orders: every trade comes from the rule.

LIVE      L arms live: it asks you to type "live". Needs KALSHI_KEY_ID and
KALSHI_KEY_FILE (in ~/.config/tern/kalshi.env). Caps: live.capital_usd at
risk, live.max_order_usd per order, live.max_daily_loss turns live off.

  ↑↓ PgUp PgDn  move in the ladder     tab         next coin: BTC ETH SOL XRP BNB HYPE
  mouse wheel   scrolls the panel under the pointer: ladder, log, book
  1-6           show / hide a box: spot, contract, ladder, book, equity, log
  r             rule trades this coin on/off
  c             chart: 5m 15m 1h 24h   v           book shown: paper / live
  L             arm / disarm live      g           gate and checklist
  R             reset paper            s           start / stop the engine
  h ?           this help              q           close (the engine keeps running)
"""

# spot chart spans: c cycles them in this order (the engine sends ~15 min of seconds and 24h of minutes)
CHARTS = {"5m": 300, "15m": 900, "1h": 3600, "24h": 86400}

# the boxes, in the order their number keys toggle them, as rows of (left, right)
PANELS = ["spot", "contract", "ladder", "book", "equity", "log"]
ROWS = [("spot", "contract", 0.36, 0.6), ("ladder", "book", 0.44, 0.52), ("equity", "log", 0.28, 0.4)]   # (.., height share, left width share)
SUPER = "¹²³⁴⁵⁶⁷⁸⁹"
LEFT_MIN = {"ladder": 56}    # the ladder's columns need this much before its bar has any room

# key hints drawn on each box's top border (btop style); a hidden box's hints move to the bottom row
PANEL_KEYS = {
    "spot": lambda s: [("tab", " next coin"), ("r", "ule on/off", "") if s.rule_on() is None
                       else ("r", "ule ", "on", "/off") if s.rule_on() else ("r", "ule on/", "off"),   # pink: the state now
                       ("c", "hart " + s.chart)],
    "book": lambda s: [("v", "iew"), ("L", "ive"), ("R", "eset")],
    "log": lambda s: [("g", "ate"), ("s", "top" if s.alive else "tart"), ("h", "elp"), ("q", "uit")],
}


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
        self.pos_off = 0                       # first line shown under the book's totals
        self.prefs_path = os.path.join(runtime, "dash.json")
        self.hidden: set[str] = set()          # boxes turned off with 1-6
        self.panel_key = ""                    # the number drawn in the title of the box being drawn
        try:
            with open(self.prefs_path) as fh:
                d = json.load(fh)
            self.hidden = set(d.get("hidden", [])) & set(PANELS)
            if d.get("series") in cfgmod.SERIES:
                self.series = d["series"]
            if d.get("chart") in CHARTS:
                self.chart = d["chart"]
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass

    # ------------------------------------------------------------ io
    def read(self) -> None:
        if time.time() - self.alive_at > 1:
            self.alive, self.alive_at = engine_alive(self.runtime), time.time()
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
                json.dump({"hidden": sorted(self.hidden), "series": self.series, "chart": self.chart}, fh)
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
        avail, total = H - 1, sum(r[2] for r in rows)   # the status line takes the last row; the boxes start at the top
        out, y = [], 0
        for i, (l, r, share, lws) in enumerate(rows):
            h = avail - y if i == len(rows) - 1 else max(7, int(avail * share / total))
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
        text = text[:W - x]
        if self.clip is not None:
            text = text[:max(0, self.clip - x)]
        if not text:
            return
        try:
            if y == H - 1 and x + len(text) == W:
                # addstr can't write the screen's last cell (the cursor has nowhere to go); insstr can
                if len(text) > 1:
                    self.s.addstr(y, x, text[:-1], attr)
                self.s.insstr(y, W - 1, text[-1], attr)
            else:
                self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    def box(self, y: int, x: int, h: int, w: int, title: str, color: int, key: str = "", right=()) -> None:
        """A rounded box with its number and title on the top border, btop style. `right` holds the
        segments drawn at the top border's right end: a plain string (grey info) or a (key, rest)
        hint whose key shows pink (PINK). Segments that don't fit drop from the left."""
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
            self.put(y, cx, key, P(PINK if P.many else MAGENTA, bold=True)); cx += len(key)
        self.put(y, cx, title, P(WHITE, bold=True)); cx += len(title)
        self.put(y, cx, "├", a)
        segs = [right] if isinstance(right, str) else list(right)
        segs = [s for s in segs if s]
        lens = [len(s) if isinstance(s, str) else sum(map(len, s)) for s in segs]
        while segs and cx + 2 + sum(n + 3 for n in lens) > x + w - 3:   # each segment: ┤..├ and a ─ before it
            segs.pop(0); lens.pop(0)
        rx = x + w - 3 - sum(n + 3 for n in lens) + 1        # keep the corner free for a scroll ▲
        for seg, n in zip(segs, lens):
            self.hint(y, rx, seg, a); rx += n + 3
        self.clip = saved

    def hint(self, y: int, x: int, seg, border: int) -> None:
        """One ┤segment├ on a border line."""
        P = self.P
        self.put(y, x, "┤", border); x += 1
        if isinstance(seg, str):
            self.put(y, x, seg, P(P.grey)); x += len(seg)
        else:                                    # pieces alternate pink (a key, or the rule's state) and white
            for i, piece in enumerate(seg):
                self.put(y, x, piece, P(PINK if P.many else MAGENTA, bold=True) if i % 2 == 0 else P(WHITE)); x += len(piece)
        self.put(y, x, "├", border)

    def rule_on(self) -> bool | None:
        """Whether the rule trades the coin on screen; None until the engine has said."""
        S = self.st.get("strategy")
        return None if not S else self.series in (S.get("series") or [])

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
    def header(self, H: int, W: int) -> None:
        """The status line, on the screen's last row (btop keeps its boxes up top)."""
        hy = H - 1
        P, st = self.P, self.st
        now = time.time()
        hb = st.get("heartbeat") or 0
        ok = now - hb < 5 and not st.get("stopped")
        # no process holding the lock is "down", however it ended; a live one that stopped polling is "stale"
        down = not ok and not self.alive
        mode = st.get("mode", "?")
        x = 0
        self.put(hy, x, " tern ", P(CYAN, bold=True) | curses.A_REVERSE); x += 7
        if mode == "live":
            self.put(hy, x, " ● LIVE ", P(RED, bold=True) | curses.A_REVERSE); x += 9
        else:
            armed = " (live armed: " + (st.get("live_error") or "authenticating") + ")" if st.get("live_enabled") else ""
            self.put(hy, x, " ● PAPER ", P(GREEN, bold=True) | curses.A_REVERSE); x += 10
            if armed:
                self.put(hy, x, armed[:60], P(YELLOW)); x += min(60, len(armed)) + 1
        prod = cfgmod.SERIES[self.series]
        sp = (st.get("spot") or {}).get(prod) or {}
        p = sp.get("now")
        mins = sp.get("min") or []
        chg = None
        if p and len(mins) > 60:
            chg = p / mins[-61][1] - 1
        seg = f" {prod.split('-')[0]} {money(p)} "
        self.put(hy, x, seg, P(WHITE, bold=True)); x += len(seg)
        if chg is not None:
            seg = f"{'▲' if chg >= 0 else '▼'}{chg:+.2%} 1h "
            self.put(hy, x, seg, P(GREEN if chg >= 0 else RED)); x += len(seg)
        ev = (st.get("events") or {}).get(self.series)
        if ev:
            tau = ev["close"] - now
            seg = f"│ {ev['event']} closes {clock(tau)} "
            self.put(hy, x, seg, P(YELLOW if tau < 600 else WHITE)); x += len(seg)
        S = st.get("strategy") or {}
        if S:
            on = self.series in (S.get("series") or [])
            nxt = ""
            if ev and on:
                tau = ev["close"] - now
                ahead = [m for m in S.get("minutes", []) if m * 60 < tau]
                if ahead:
                    nxt = f" next {max(ahead)}m slot in {clock(tau - max(ahead) * 60)}"
            seg = f"│ rule {'ON' if on else 'off'}{nxt}{' +live' if st.get('auto_live') and on else ''} "
            self.put(hy, x, seg, P(GREEN if on else self.P.grey)); x += len(seg)
        # the engine can be up while its feeds are not (Wi-Fi drops): say how old the data is
        stale = []
        sec = sp.get("sec") or []
        if ok and sec and now - sec[-1][0] > 10:
            stale.append(f"spot {ago(now - sec[-1][0])} old")
        if ok and ev and ev.get("age", 0) > 30:
            stale.append(f"ladder {ago(ev['age'])} old")
        right = f" {'●' if ok else '○'} engine {'ok' if ok else ('down — s starts it' if down else 'starting' if st.get('stopped') or not hb else 'stale')}  {time.strftime('%H:%M:%S')} "
        if stale:
            seg = "│ " + " · ".join(stale) + " "
            self.put(hy, x, seg[:max(0, W - x - len(right))], P(YELLOW, bold=True))
        self.put(hy, W - len(right), right, P(GREEN if ok else RED))

    def spot_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        prod = cfgmod.SERIES[self.series]
        sp = (st.get("spot") or {}).get(prod) or {}
        now = time.time()
        sec, mins = sp.get("sec") or [], sp.get("min") or []
        span = CHARTS[self.chart]
        first_sec = sec[0][0] if sec else now
        pts = [[t + 60, p] for t, p in mins if t + 60 < first_sec] + sec   # minute closes, then live seconds
        gw = w - 2 - 11
        vals = by_time(pts, now - span, now, gw * 2)
        strike = None
        row = self.row(self.sel)
        if row:
            strike = strike_of(row["k"])
        self.box(y, x, h, w, prod.lower(), CYAN, "", PANEL_KEYS["spot"](self))
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
            self.put(ry, lx, px(v)[:10], P(self.P.grey))
        if strike and lo <= strike <= hi:
            ry = y + 1 + int(round((hi - strike) / (hi - lo) * (gh - 1)))
            self.put(ry, lx, px(strike)[:10], P(YELLOW, bold=True))
        span = "-" + self.chart
        self.put(y + h - 2, x + 2, span, P(self.P.grey))
        self.put(y + h - 2, x + 1 + gw - 3, "now", P(self.P.grey))
        if strike and vals:
            d = vals[-1] - strike
            self.put(y + h - 2, x + 2 + gw // 2 - 12, f"spot {'above' if d >= 0 else 'below'} strike by ${px(abs(d))}",
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
        self.box(y, x, h, w, "ladder", 208 if self.P.many else YELLOW, "", "↑↓")
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
            above = spot is not None and spot > r["k"]
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
            self.put(yy, x + 31 + barw, f"{volume(r['v']):>7}", P(self.P.grey) | base)
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
        self.box(y, x, h, w, "book", YELLOW, "", PANEL_KEYS["book"](self))
        tabs: list[tuple[str, int]] = []
        for name in ("paper", "live"):
            tabs += [(f" {name} ", P(WHITE, bold=True) | curses.A_REVERSE if name == v else P(self.P.grey)), (" ", 0)]
        body = [tabs]                                    # the whole book is one list of lines and scrolls as one
        if b:
            eq, start = b.get("equity"), b.get("start") or 0
            pnl = (eq - start) if eq is not None and start else None
            lines = [("equity", money(eq), WHITE), ("P&L", money(pnl, True) + (f"  {pnl/start:+.2%}" if pnl is not None and start else ""),
                                                     GREEN if (pnl or 0) >= 0 else RED),
                     ("cash", money(b.get("cash")), WHITE), ("at risk", money(b.get("at_risk")), YELLOW),
                     ("fees", money(b.get("fees")), self.P.grey)]
            if v == "live":
                L = st.get("live_cfg") or {}
                lines.append(("caps", f"risk {money(L.get('capital_usd'))} · order {money(L.get('max_order_usd'))} · day -{money(L.get('max_daily_loss'))}", self.P.grey))
            g = st.get("gate") or {}
            if g:
                wr = f"{g['win']:.0%}" if g.get("win") is not None else "-"
                lines.append(("rule", f"{g['entries']} settled · {g['open']} open · {money(g['pnl'], True)} · win {wr} · "
                                      f"{g['per_contract']*100:+.2f}¢/ct", GREEN if g["pnl"] >= 0 else RED))
            body += [[(f"{k:<8} ", P(self.P.grey)), (val, P(c, bold=k == "equity"))] for k, val, c in lines]
            body += [[("", 0)]] + self.book_lines(b)
        yy = y + 1
        room = max(0, h - 2)
        self.pos_off = max(0, min(self.pos_off, len(body) - room))
        if self.pos_off > 0:
            self.put(y, x + w - 2, "▲", P(YELLOW))
        if self.pos_off + room < len(body):
            self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))
        for line in body[self.pos_off:self.pos_off + room]:
            cx = x + 2
            for text, attr in line:
                self.put(yy, cx, text, attr); cx += len(text)
            yy += 1

    def book_lines(self, b: dict) -> list[list[tuple[str, int]]]:
        """The book's scrolling part (petrel style): open positions, then the last settlements, then
        the last fills, newest first. Each line is a list of (text, attr) pieces."""
        P, now = self.P, time.time()
        grey, head, white = P(self.P.grey), P(self.P.grey, bold=True), P(WHITE)

        def side(q):
            return [(f"{'YES' if q > 0 else 'NO':<4}", P(GREEN if q > 0 else MAGENTA, bold=True))]

        def strike(ticker):
            m = re.search(r"-[TB]([0-9.]+)$", ticker or "")
            return f"{strike_label(float(m.group(1))) if m else '?':>9} "

        out: list[list[tuple[str, int]]] = []
        pos = b.get("pos") or []
        out.append([(f"{'open':<4} {'strike':>9} {'side':<4} {'qty':>6} {'avg':>5} {'mark':>5} {'P&L':>8} {'closes':>7}", head)])
        for p in pos:
            pl = p.get("pnl")
            out.append([(f"{'':<5}{strike_label(p.get('k')):>9} ", white), *side(p["q"]),
                        (f" {abs(p['q']):>6g} {cents(p['avg']):>5} {cents(p.get('mark')):>5} ", white),
                        (f"{money(pl, True):>8}", P(GREEN if (pl or 0) >= 0 else RED)),
                        (f" {clock((p.get('close') or now) - now):>7}", grey)])
        if not pos:
            out.append([("     no open positions", grey)])

        trades = list(reversed(b.get("trades") or []))
        settled = [t for t in trades if t.get("kind") == "settle"]
        out.append([("", 0)])
        out.append([(f"{'settled':<8}{'strike':>6} {'side':<4} {'qty':>6} {'won':>11} {'P&L':>8} {'ago':>7}", head)])
        for t in settled:
            q = -t["delta"]
            won = (t.get("result") == "yes") == (q > 0)
            word, wc = ("void", grey) if t.get("result") == "void" else ("won", P(GREEN)) if won else ("lost", P(RED))
            out.append([(f"{'':<5}{strike(t['ticker'])}", white), *side(q),
                        (f" {abs(q):>6g} ", white), (f"{word:>11} ", wc),
                        (f"{money(t['realized'], True):>8}", P(GREEN if t["realized"] >= 0 else RED)),
                        (f" {ago(now - t['ts']):>7}", grey)])
        if not settled:
            out.append([("     none yet", grey)])

        fills = [t for t in trades if t.get("kind") == "fill"]
        out.append([("", 0)])
        out.append([(f"{'fills':<8}{'strike':>6} {'side':<4} {'qty':>6} {'at':>5} {'fee':>5} {'P&L':>8} {'ago':>7}", head)])
        for t in fills:
            q = t["delta"]                                   # + bought YES, - bought NO at 1 - price
            at = t["price"] if q > 0 else 1 - t["price"]
            out.append([(f"{'':<5}{strike(t['ticker'])}", white), *side(q),
                        (f" {abs(q):>6g} {cents(at):>5} {money(t['fee']):>5} ", white),
                        (f"{money(t['realized'], True):>8}", P(GREEN if t["realized"] >= 0 else RED)),
                        (f" {ago(now - t['ts']):>7}", grey)])
        if not fills:
            out.append([("     none yet", grey)])
        return out

    def equity_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        v = self.book_view or ("live" if st.get("mode") == "live" else "paper")
        b = st.get(v) or {}
        curve = [e for _, e in (b.get("curve") or [])]
        self.box(y, x, h, w, f"equity · {v}", GREEN, "", f"{len(curve)} min")
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
        self.box(y, x, h, w, "log", BLUE, "", PANEL_KEYS["log"](self))
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
        """Row H-2 is the bottom border of the lowest boxes. It carries the hints of any hidden
        box (so tab, ?, q... never go missing) at its right end, and a flash message at its left."""
        P = self.P
        orphans = [k for name, fn in PANEL_KEYS.items() if name not in self.rects for k in fn(self)]
        if orphans:
            border = P(P.grey)
            need = sum(sum(map(len, seg)) + 3 for seg in orphans) - 1
            rx = W - 3 - need if self.rects else max(1, (W - need) // 2)
            for seg in orphans:
                if rx >= 1:
                    self.hint(H - 2, rx, seg, border)
                rx += sum(map(len, seg)) + 3
        if self.flash and time.time() - self.flash_at < 8:
            msg = f" {self.flash} "[:max(0, W - 4)]
            self.put(H - 2, 2, msg, P(YELLOW, bold=True) | curses.A_REVERSE)

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
        self.header(H, W)
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
            if m.get("type") == "stop":
                if ch in (ord("y"), ord("Y")):
                    self.say(stop_engine(self.runtime, self.st.get("pid"))); self.alive_at = 0
                else:
                    self.say("engine kept running")
                return True
            if m.get("type") == "reset":
                if ch in (ord("y"), ord("Y")):
                    self.send(op="reset_paper")
                return True
            return True
        if self.overlay:
            self.overlay = None; return True
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, ord("k"), ord("j")):
            self.step_ladder({curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                              curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10}[ch])
        elif ord("1") <= ch <= ord(str(len(PANELS))):
            self.toggle(ch - ord("1"))
        elif ch == 9:
            order = list(cfgmod.SERIES)
            self.series = order[(order.index(self.series) + 1) % len(order)] if self.series in order else order[0]
            self.sel = None; self.write_view(); self.save_prefs(); self.say(f"viewing {cfgmod.SERIES[self.series]}")
        elif ch == ord("r"):
            self.send(op="series", series=self.series)
        elif ch == ord("c"):
            order = list(CHARTS)
            self.chart = order[(order.index(self.chart) + 1) % len(order)] if self.chart in order else "15m"
            self.save_prefs()
        elif ch == ord("v"):
            cur = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
            self.book_view = "live" if cur == "paper" else "paper"
        elif ch == ord("g"):
            self.overlay = self.gate_lines()
        elif ch == ord("?") or ch == ord("h"):
            self.overlay = HELP_LINES
        elif ch == ord("s"):
            if engine_alive(self.runtime):
                self.modal = {"type": "stop", "title": "stop engine", "lines": [
                    "Stop the engine? Nothing trades, paper or live, until it starts again",
                    "(s here, or reopening the dash). It saves its state before it exits.", "",
                    "y stops · any other key keeps it running"]}
            else:
                self.say(start_engine(self.runtime)); self.alive_at = 0
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
                                        "The rule trades live only past the gate (g).",
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
