"""The screen, btop style. It reads the engine's status file and drops command files;
closing it never stops the engine."""

from __future__ import annotations

import curses
import fcntl
import json
import os
import re
import textwrap
import subprocess
import sys
import time
import uuid

from . import config as cfgmod
from . import shadow as shadowmod
from . import themes
from .engine import live_dir

# ---------------------------------------------------------------- colors

BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

ROW = -1                                    # placeholder attr: "tint me by my row", filled in when drawn

# what each theme role draws as on an 8-color terminal (themes only apply with 256 colors)
FEW = {"grey": WHITE, "key": MAGENTA, "head": WHITE, "sym": CYAN, "event": BLUE, "unit": WHITE,
       "word_a": WHITE, "word_b": WHITE, "word_c": WHITE, "ticker": CYAN, "strike": YELLOW, "slot": YELLOW,
       "verb": MAGENTA, "variant": CYAN, "phrase": YELLOW, "sep": MAGENTA, "value": WHITE,
       "rows": WHITE, "fade_txt": WHITE, "fade_ts": BLUE}
FEW_BORDER = {"spot": CYAN, "contract": MAGENTA, "ladder": BLUE, "book": YELLOW, "equity": GREEN, "log": BLUE}
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
        self.cache: dict[int, int] = {}
        self.set_theme(None)

    def set_theme(self, name: str | None) -> None:
        self.name = name if name in themes.THEMES else themes.DEFAULT
        self.t = themes.get(self.name)
        self.grey = self.t["grey"] if self.many else 8 if curses.COLORS >= 16 else WHITE

    def __call__(self, fg: int, bold: bool = False, dim: bool = False) -> int:
        if self.many:
            fg = self.t["base"].get(fg, fg)
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

    def c(self, role: str) -> int:
        """The theme's color for a role (see themes.py)."""
        return self.t[role] if self.many else FEW[role]

    def border(self, name: str) -> int:
        return self.t["border"][name] if self.many else FEW_BORDER[name]

    def pick(self, role: str, frac: float, reverse: bool = False) -> int:
        """A color from a theme scale (rows, fade_txt, fade_ts) at 0..1 along it."""
        if not self.many:
            return FEW[role]
        scale = self.t[role][::-1] if reverse else self.t[role]
        return scale[max(0, min(len(scale) - 1, int(frac * (len(scale) - 1) + 0.5)))]

    def grad(self, name: str) -> list[int]:
        return self.t["grads"][name] if self.many else GRAD8[name]


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
    r"|(?P<verb>\b(?:settled|started|stopped|opened|closed|placed|filled|cancell?ed)\b)"
    r"|(?P<variant>\b[a-z]+[0-9]*(?= [+-][0-9]))"
    r"|(?P<good>\b(?:bought|authenticated|state saved)\b)"
    r"|(?P<bad>\b(?:KILL|rejected|failed|unreadable|error|stopped for this slot)\b)"
    r"|(?P<phrase>\b(?:favorites in band|in band|mode)\b)"
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


# The help's prose (the variant lists in it come from shadow.TABLE and shadow.RETIRED, their only home): (heading, paragraph) pairs, reflowed to the help's width after {braces} are filled
# from the config in use (help_lines), so numbers are never stale. A line starting with "|" is kept as is.
HELP = [
    ("", "tern trades Kalshi's hourly crypto above-or-below markets by itself. Every trade comes from the "
         "rule; there are no manual orders. The engine runs on its own (tern.service) and keeps trading "
         "with this screen closed."),
    ("THE RULE", "Kalshi prices long shots too high and favorites too low. At {minutes} minutes before each "
                 "hourly close, the rule buys the favorite side of every strike whose mid is {band} (spread "
                 "{spread}c or less) at the ask, {contracts:g} contracts a strike and at most ${hour_cost:,.0f} an hour, "
                 "and holds it to settlement. Over 67 days of BTC history it made +0.6c a contract on data it "
                 "was not chosen on (t=1.1): positive, small, unproven."),
    ("VARIANTS", "Each variant runs on paper side by side with its own record and real fees, written down "
                 "before it saw any data:\n"
                 "{variants}\n"
                 "The one marked ● in the variants box (3) is what the paper book trades, and live too once it "
                 "passes the gate. Highlight another with ↑↓ and press enter to switch; while live is on or armed "
                 "you type \"live\" to confirm. Open positions ride to settlement. Picking whichever looks best "
                 "after watching flatters it: the gate is the real check.\n"
                 "Retired, records kept and listed by (tern report):\n"
                 "{retired}"),
    ("THE GATE", "(g) Live trades the variant in use only after its own paper record has {days:g} days of hours "
                 "traded, {entries} settled entries and P&L of ${pnl:g} or more after fees. Hours with the engine "
                 "off don't count, and switching variants never skips it: the new one needs its own record. "
                 "The gate catches a broken rule, not a weak one."),
    ("LIVE", "(L) arms live: it asks you to type \"live\". It needs KALSHI_KEY_ID and KALSHI_KEY_FILE in "
             "~/.config/tern/kalshi.env. The status line turns red (● LIVE) once Kalshi accepts the key. "
             "Caps: ${risk:,.0f} at risk at once and ${order:,.0f} per order; a loss of ${day:,.0f} since local midnight "
             "turns live off and leaves open positions to settle. Every order is immediate-or-cancel. Kalshi "
             "is the record: its positions overwrite the engine's every 15 seconds. (L) again disarms."),
    ("COINS", "(tab) and shift+tab step through {coins}. (r) turns the rule on or off for the coin on "
              "screen. The 67-day test was BTC only, so the rule trades BTC unless you turn others on."),
    ("PAPER", "The paper book starts at ${paper:,.0f}. (R) wipes it and its record of the rule (it asks y "
              "first); the variants' records are kept."),
]
HELP_WIDTH = 78                                   # the prose wraps here; the key legend may run wider

# the boxes and the status line: (number, name, what it shows)
HELP_BOXES = [
    ("¹", "spot", "the coin's price; (c) cycles the span: 5m 15m 1h 24h"),
    ("²", "contract", "one strike as a bet (\"BTC above $61,900?\"): YES and NO bid/ask in cents,"),
    ("", "", "  YES's price this hour on 0-100¢ (◀ now; green/red: where the rule buys"),
    ("", "", "  YES/NO), and how far it is from being bought. Starts on the nearest ★"),
    ("³", "ladder", "every strike high to low: bid, ask, probability bar, spread, volume and"),
    ("", "", "  your position; ★ marks the band. ↑↓ picks the strike the contract box shows"),
    ("³", "variants", "(3) swaps it in for the ladder: each variant's hours, entries, P&L, ¢ a"),
    ("", "", "  contract, win rate, t, and gate progress (yellow: still short); ● in use;"),
    ("", "", "  the highlighted one's P&L by hour below"),
    ("⁴", "book", "equity, P&L, cash, at risk, fees, the paper record of the rule, then open"),
    ("", "", "  positions, settlements and fills; (v) switches paper / live"),
    ("⁵", "equity", "that book's equity by the minute"),
    ("⁶", "log", "what the engine did, newest at the bottom; scroll back with ↑↓"),
    ("", "status", "bottom row: PAPER or LIVE, the coin's price and 1h change, the hourly event"),
    ("", "", "  and time to its close, rule ON/off with the next slot (+live when live"),
    ("", "", "  trades), stale data warnings (usually Wi-Fi), engine ok / stale / down"),
]

# overlay text worth a color: checks, key references like (g), words to type, warnings, paths and names
OVERLAY_TOKENS = re.compile(
    r"(?P<ok>\[x\])|(?P<todo>\[ \])"
    r"|(?P<ref>\((?:[a-zA-Z?1-6]|tern \w[\w-]*|enter|tab|esc)\))"
    r"|(?P<quote>\"[^\"]+\"|\blive and press enter\b)"
    r"|(?P<warn>\b(?:real money|unproven|turns live off|turns itself off|Nothing trades|No key found yet|Wipe)\b)"
    r"|(?P<keyhint>^(?:y|any key|any other key)\b|\besc\b|·\s(?:any (?:other )?key)\b)"
    r"|(?P<path>~/\S+|\bKALSHI_KEY_(?:ID|FILE)\b|\blive\.\w+)")
RULE_LINE = "\0rule"                              # placeholder text: draw_overlay turns it into a divider

# the help's key legend: two columns of (section, [(keys, what they do)])
HELP_KEYS = [
    [("move", [("←→", "focus: ladder or variants / book / log"),
               ("↑↓  j k", "scroll the focused box: the ladder picks a"),
               ("", "strike, variants highlights one"),
               ("PgUp PgDn", "ten steps at a time"),
               ("mouse wheel", "scroll the box under the pointer"),
               ("tab S-tab", "next / previous coin")]),
     ("look", [("1 2", "show / hide spot, contract"),
               ("3", "ladder → variants → hidden"),
               ("4 5 6", "show / hide book, equity, log"),
               ("c", "spot chart span: 5m 15m 1h 24h"),
               ("v", "book and equity: paper / live"),
               ("w", "watch the highlighted variant: book,"),
               ("", "equity and band as if it traded (view only)"),
               ("t T", "next / previous of {themes} color themes")])],
    [("trade", [("enter", "use the highlighted variant"),
                ("r", "the rule trades this coin on / off"),
                ("g", "gate, variants and go-live checklist"),
                ("e", "settings: live limits and size, paper,"),
                ("", "gate, alerts, what the log shows"),
                ("L", "arm (type \"live\") / disarm live"),
                ("R", "reset the paper book (asks y)")]),
     ("engine", [("s", "stop (asks y) / start the engine"),
                 ("h ?", "this help"),
                 ("q", "close the screen; the engine keeps going")]),
     ("settings", [("↑↓", "pick a setting"),
                   ("←→  - +", "step it down / up, or flip it"),
                   ("enter 0-9", "type a value, enter saves"),
                   ("esc", "close")]),
     ("in here", [("↑↓ PgUp PgDn", "scroll this help or the gate"),
                  ("Home End", "top / bottom"),
                  ("any other key", "close it")])],
]

# command line, shown at the end of the help
HELP_CLI = [
    ("tern", "this screen (starts the engine in paper if it is down)"),
    ("tern report", "every variant's record, retired ones too"),
    ("tern status", "a plain-text summary"),
    ("tern live-check", "test the Kalshi key: balance and positions, no orders"),
    ("systemctl --user status tern", "the engine's service"),
]

# what the log box may show (settings, e): key -> how the settings box names it
LOG_FILTERS = {"all": "everything", "no_shadow": "no shadow lines", "quiet": "live, warnings, commands",
               "trades": "trades only"}
TRADE_LINE = re.compile(r"^(?:paper|live): (?:\d+m slot on .*, [1-9]\d* bought$|\S+ (?:settled|finalized) )")
SCREEN_SETTINGS = [cfgmod.Setting("screen", "log", "log shows", kind="choice", group="SCREEN",
                                  what="shadow: the variants' hourly results · quiet hides paper too · trades: buys and settlements only")]

# spot chart spans: c cycles them in this order (the engine sends ~15 min of seconds and 24h of minutes)
CHARTS = {"5m": 300, "15m": 900, "1h": 3600, "24h": 86400}

# the boxes, in the order their number keys toggle them, as rows of (left, right)
PANELS = ["spot", "contract", "ladder", "book", "equity", "log"]
ROWS = [("spot", "contract", 0.36, 0.6), ("ladder", "book", 0.44, 0.52), ("equity", "log", 0.28, 0.4)]   # (.., height share, left width share)
SUPER = "¹²³⁴⁵⁶⁷⁸⁹"
LEFT_MIN = {"ladder": 56, "variants": 56}   # the ladder's columns need this much before its bar has any room
SLOT3 = ["ladder", "variants"]       # the boxes 3 cycles through in the ladder's place, then hidden
SCROLLS = ["ladder", "variants", "book", "log"]   # the boxes ←→ focus and ↑↓ scroll

# key hints drawn on each box's top border (btop style); a hidden box's hints move to the bottom row
PANEL_KEYS = {
    "spot": lambda s: [("tab", " next coin"), ("r", "ule on/off", "") if s.rule_on() is None
                       else ("r", "ule ", "on", "/off") if s.rule_on() else ("r", "ule on/", "off"),   # pink: the state now
                       ("c", "hart " + s.chart)],
    "book": lambda s: [("v", "iew"), ("L", "ive"), ("R", "eset")],
    "contract": lambda s: [("t", "heme ", s.P.name)],
    "log": lambda s: [("g", "ate"), ("", "s", "e", "ttings"), ("s", "top" if s.alive else "tart"), ("h", "elp"), ("q", "uit")],
}


class Screen:
    def __init__(self, stdscr, runtime: str):
        self.s = stdscr
        self.runtime = runtime
        self.fast = live_dir(runtime)
        self.cmd_dir = os.path.join(runtime, "cmd")
        os.makedirs(self.cmd_dir, exist_ok=True)
        self.st: dict = {}
        self.st_stamp: tuple | None = None     # (inode, mtime, size) of the status file last parsed
        self.series = "KXBTCD"
        self.sel: str | None = None
        self.chart = "15m"
        self.book_view = None
        self.overlay: str | None = None        # "help", "gate" or "settings" while one is open
        self.set_sel = 0                       # the settings row the cursor is on
        self.set_buf: str | None = None        # the value being typed in the settings box, or None
        self.set_sent: dict[str, tuple] = {}   # key -> (value, time) sent but not yet in the status
        self.log_filter = "all"                # what the log box shows: LOG_FILTERS
        self.ov_off, self.ov_room, self.ov_len = 0, 1, 0   # its scroll: first line shown, lines that fit, lines in all
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
        self.drawing = ""                      # the box being drawn, so box() can mark the focused one
        self.focus = "ladder"                  # the box ↑↓ scroll; ←→ move it
        self.slot3 = "ladder"                  # what sits in the ladder's place: ladder or variants
        self.var_sel: str | None = None        # the variant the cursor is on in the variants box
        self.var_top = 0                       # first variant row shown when the list is taller than its box
        self.watch: str | None = None          # w: a variant shown as if it traded (view only; trading is unchanged)
        try:
            with open(self.prefs_path) as fh:
                d = json.load(fh)
            self.hidden = set(d.get("hidden", [])) & set(PANELS)
            if d.get("series") in cfgmod.SERIES:
                self.series = d["series"]
            if d.get("chart") in CHARTS:
                self.chart = d["chart"]
            if isinstance(d.get("watch"), str):
                self.watch = d["watch"]
            if d.get("log_filter") in LOG_FILTERS:
                self.log_filter = d["log_filter"]
            if d.get("slot3") in SLOT3:
                self.slot3 = d["slot3"]
                if self.slot3 == "variants":
                    self.focus = "variants"
            self.P.set_theme(d.get("theme"))
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass

    # ------------------------------------------------------------ io
    def read(self) -> None:
        if time.time() - self.alive_at > 1:
            self.alive, self.alive_at = engine_alive(self.runtime), time.time()
        try:                                   # the engine writes it once a second: parse it only when it is new
            with open(os.path.join(self.fast, "status.json")) as fh:
                stamp = os.fstat(fh.fileno())
                stamp = (stamp.st_ino, stamp.st_mtime_ns, stamp.st_size)
                if stamp != self.st_stamp:
                    self.st = json.load(fh)
                    self.st_stamp = stamp
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
                json.dump({"hidden": sorted(self.hidden), "series": self.series, "chart": self.chart, "theme": self.P.name,
                           "slot3": self.slot3, "watch": self.watch, "log_filter": self.log_filter}, fh)
            os.replace(self.prefs_path + ".tmp", self.prefs_path)
        except OSError:
            pass

    def toggle(self, i: int) -> None:
        name = PANELS[i]
        if name == "ladder":                   # 3 cycles: ladder → variants → hidden → ladder
            if "ladder" in self.hidden:
                self.hidden.discard("ladder"); self.slot3 = "ladder"
            elif self.slot3 == "ladder":
                self.slot3 = "variants"
            else:
                self.hidden.add("ladder"); self.slot3 = "ladder"
            if self.focus in SLOT3:
                self.focus = self.slot3
            self.save_prefs()
            self.say("ladder hidden" if "ladder" in self.hidden else f"{self.slot3} shown"
                     + (" · ↑↓ pick, enter uses it" if self.slot3 == "variants" else ""))
            return
        self.hidden ^= {name}
        self.save_prefs()
        self.say(f"{name} {'hidden' if name in self.hidden else 'shown'}"
                 + (" · every box is hidden: 1-6 bring them back" if self.hidden >= set(PANELS) else ""))

    def layout(self, H: int, W: int) -> list[tuple[str, int, int, int, int]]:
        """(panel, y, x, h, w) for every box shown. A row's survivor takes the full width; rows share the height."""
        rows = [(l, r, share, lw) for l, r, share, lw in ROWS if l not in self.hidden or r not in self.hidden]
        rows = [(self.slot3 if l == "ladder" else l, r, share, lw) for l, r, share, lw in rows]
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
        hint whose key shows in the theme's key color. Segments that don't fit drop from the left."""
        P = self.P
        a = P(color)
        key = key or self.panel_key
        saved, self.clip = self.clip, None
        focused = self.drawing == self.focus
        if focused:
            a |= curses.A_BOLD
        tl, hz, tr, vt, bl, br = "╭─╮│╰╯"
        self.put(y, x, tl + hz * (w - 2) + tr, a)
        for r in range(1, h - 1):
            self.put(y + r, x, vt, a)
            self.put(y + r, x + w - 1, vt, a)
        self.put(y + h - 1, x, bl + hz * (w - 2) + br, a)
        cx = x + 2
        self.put(y, cx, hz, a); cx += 1
        if focused:   # title in the box's own color with a marker; the border is drawn bold
            tab = f"{key}{title} ◂"
            self.put(y, cx, tab, P(color, bold=True)); cx += len(tab)
        else:
            if key:
                self.put(y, cx, key, P(P.c("key"), bold=True)); cx += len(key)
            self.put(y, cx, title, P(WHITE, bold=True)); cx += len(title)
        self.put(y, cx, hz, a)
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
        self.put(y, x, "─", border); x += 1
        if isinstance(seg, str):
            self.put(y, x, seg, P(P.grey)); x += len(seg)
        else:                                    # pieces alternate pink (a key, or the rule's state) and white
            for i, piece in enumerate(seg):
                self.put(y, x, piece, P(P.c("key"), bold=True) if i % 2 == 0 else P(WHITE)); x += len(piece)
        self.put(y, x, "─", border)

    def wview(self) -> dict | None:
        """The watched variant's book-shaped view, or None when not watching (or the engine lacks it)."""
        if not self.watch:
            return None
        v = (self.st.get("views") or {}).get(self.watch)
        return None if v is None or self.watch == self.active() else v

    def active(self) -> str | None:
        return (self.st.get("strategy") or {}).get("variant")

    def strat(self) -> dict | None:
        """The rule's settings the screen marks the ladder and band with: the watched variant's, else the active one's."""
        w = self.wview()
        return w["strategy"] if w else self.st.get("strategy")

    def rule_on(self) -> bool | None:
        """Whether the rule (or the watched variant) trades the coin on screen; None until the engine has said."""
        S = self.strat()
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
        many, grey = P.many, P.grey
        # each segment is (text, attr) pieces; segments are set apart by color and a gap, not a bar
        def span(pieces):
            nonlocal x
            for text, attr in pieces:
                self.put(hy, x, text, attr); x += len(text)
            x += 2
        x += 1
        price = [(prod.split('-')[0] + " ", P(P.c("sym"), bold=True)), (money(p), P(WHITE, bold=True))]
        if chg is not None:
            price.append((f" {'▲' if chg >= 0 else '▼'}{chg:+.2%}", P(GREEN if chg >= 0 else RED, bold=True)))
            price.append((" 1h", P(P.c("unit"))))
        span(price)
        ev = (st.get("events") or {}).get(self.series)
        if ev:
            tau = ev["close"] - now
            span([(ev['event'], P(P.c("event"), bold=True)), (" closes ", P(P.c("word_a"))),
                  (clock(tau), P(YELLOW if tau < 600 else WHITE, bold=True))])
        S = self.strat() or {}
        if S:
            on = self.series in (S.get("series") or [])
            seg = [("r", P(P.c("key"), bold=True)), ("ule ", P(P.c("word_b"))), ("ON" if on else "off", P(GREEN if on else RED, bold=True))]
            if ev and on:
                tau = ev["close"] - now
                ahead = [m for m in S.get("minutes", []) if m * 60 < tau]
                if ahead:
                    seg += [(" next ", P(P.c("word_c"))), (f"{max(ahead)}m", P(P.c("key"), bold=True)),
                            (" slot in ", P(P.c("word_c"))), (clock(tau - max(ahead) * 60), P(WHITE))]
            if st.get('auto_live') and on and not self.wview():
                seg.append((" +live", P(RED, bold=True)))
            if self.wview():
                seg[1:2] = [(f"ule ({self.watch}) ", P(P.c("word_b")))]
            span(seg)
        if self.wview():
            span([("watching ", P(YELLOW)), (self.watch, P(YELLOW, bold=True)),
                  (" · trading ", P(self.P.grey)), (self.active() or "?", P(GREEN, bold=True)), (" (w)", P(self.P.grey))])
        # the engine can be up while its feeds are not (Wi-Fi drops): say how old the data is
        stale = []
        sec = sp.get("sec") or []
        if ok and sec and now - sec[-1][0] > 10:
            stale.append(f"spot {ago(now - sec[-1][0])} old")
        if ok and ev and ev.get("age", 0) > 30:
            stale.append(f"ladder {ago(ev['age'])} old")
        right = f" {'●' if ok else '○'} engine {'ok' if ok else ('down — s starts it' if down else 'starting' if st.get('stopped') or not hb else 'stale')}  {time.strftime('%H:%M:%S')} "
        if stale:
            seg = " · ".join(stale) + " "
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
        self.box(y, x, h, w, prod.lower(), self.P.border("spot"), "", PANEL_KEYS["spot"](self))
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

    def in_band(self, r: dict, S: dict | None = None) -> bool:
        """The strike's favorite is in the variant's band with a tight enough spread: the rule would buy it."""
        S = S if S is not None else (self.strat() or {})
        bid, ask = r.get("b"), r.get("a")
        if not S or bid is None or ask is None:
            return False
        mid = (bid + ask) / 2
        return S["band_lo"] <= max(mid, 1 - mid) < S["band_hi"] and ask - bid <= S["max_spread"] + 1e-9

    def contract_panel(self, y, x, h, w) -> None:
        """One strike as the bet it is: "BTC above $61,900?", both sides' prices, YES's price over the
        hour on a fixed 0-100¢ scale with the rule's bands beside it, and how far it is from being bought."""
        P, st = self.P, self.st
        r = self.row(self.sel)
        prod = cfgmod.SERIES[self.series]
        coin = prod.split("-")[0]
        title = f"{coin} above ${strike_label(r['k'])}?" if r else "contract"
        self.box(y, x, h, w, title, self.P.border("contract"), "", PANEL_KEYS["contract"](self))
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
        bid, ask = r["b"], r["a"]
        mid = (bid + ask) / 2 if bid is not None and ask is not None else (bid if bid is not None else ask)
        S = self.strat() or {}
        no = lambda p: None if p is None else 1 - p
        # YES 52/53¢  NO 47/48¢  53% YES  vol 1,234 (bid/ask: what you'd get selling / pay buying)
        info = [("YES", f"{cents(bid).strip()}/{cents(ask).strip()}¢", GREEN),
                ("NO", f"{cents(no(ask)).strip()}/{cents(no(bid)).strip()}¢", RED),
                ("", f"{(mid or 0)*100:.0f}% YES", WHITE), ("vol", f"{r['v']:,.0f}", self.P.grey)]
        cx = x + 2
        for k, v, c in info:
            if k:
                self.put(y + 1, cx, k + " ", P(c, bold=True)); cx += len(k) + 1
            self.put(y + 1, cx, v + "   ", P(WHITE if k in ("YES", "NO") else c, bold=k != "vol")); cx += len(v) + 3
        if cx < x + w - 17:
            self.put(y + 1, x + w - 16, "bid/ask, cents", P(self.P.grey))
        gh, gw = h - 4, w - 2 - 7
        if gh > 0:
            lo_b, hi_b = S.get("band_lo", 0.75), S.get("band_hi", 0.95)
            row_of = lambda v: max(0, min(gh - 1, int(round((1 - v) * (gh - 1)))))
            mids = by_time(pts, min(t0, pts[0][0]) if pts else t0, t1, gw * 2) if pts else []
            if mids:
                self.graph(y + 2, x + 1, gh, gw, mids, 0.0, 1.0, P.grad("yes"), [lo_b, 1 - lo_b, 0.5], self.P.grey)
            else:
                self.put(y + 2 + gh // 2, x + 3, "history builds while the screen watches this strike"[:gw - 2], P(self.P.grey))
            # the bands beside the chart: green where the rule buys YES, red where it buys NO
            gx = x + 1 + gw
            step = 1 / max(1, gh - 1)
            for rr in range(gh):                             # a row covers prices v ± half a row
                top, bot = 1 - (rr - 0.5) * step, 1 - (rr + 0.5) * step
                if bot < hi_b and top > lo_b:
                    self.put(y + 2 + rr, gx, "▐", P(GREEN, bold=True))
                elif bot < 1 - lo_b and top > 1 - hi_b:
                    self.put(y + 2 + rr, gx, "▐", P(RED, bold=True))
            for v in (1.0, lo_b, 0.5, 1 - lo_b, 0.0):
                col = GREEN if v == lo_b else RED if v == 1 - lo_b else self.P.grey
                self.put(y + 2 + row_of(v), gx + 1, f"{v*100:3.0f}¢", P(col, bold=col != self.P.grey))
            if mid is not None:                              # where the price is now, on the scale
                self.put(y + 2 + row_of(mid), gx, "◀", P(YELLOW, bold=True))
        if mid is not None and S:
            cx = x + 2
            for text, col in self.leaning(r, mid, S, prod):
                self.put(y + h - 2, cx, text, P(col, bold=col not in (self.P.grey, WHITE))); cx += len(text)

    def leaning(self, r: dict, mid: float, S: dict, prod: str) -> list[tuple[str, int]]:
        """How far this strike is from being bought: which side leads, at what price, how many cents from
        the band, and how far the coin is from the strike."""
        grey = self.P.grey
        side, fav = ("YES", mid) if mid >= 0.5 else ("NO", 1 - mid)
        col = GREEN if side == "YES" else RED
        who = "the rule" if not self.wview() else self.watch
        lo_b, hi_b = S["band_lo"], S["band_hi"]
        out: list = []
        if self.in_band(r, S):
            out += [("★ ", YELLOW), (f"{who} buys {side} ", YELLOW), (f"{int(fav * 100 + 0.5)}¢", col)]
        elif fav >= hi_b:
            out += [(f"{side} {int(fav * 100 + 0.5)}¢", col), (f" · over {hi_b*100:.0f}¢: too sure to buy", grey)]
        elif lo_b <= fav:
            out += [(f"{side} {int(fav * 100 + 0.5)}¢", col), (f" · in band, spread over {S['max_spread']*100:.0f}¢", grey)]
        else:
            fc, lc = int(fav * 100 + 0.5), int(lo_b * 100 + 0.5)
            out += [("leaning " if fav > 0.52 else "coin flip, ", grey), (f"{side} {fc}¢", col),
                    (" · ", grey), (f"{lc - fc}¢", WHITE), (f" from the {lc}¢ band", grey)]
        spot = (((self.st.get("spot") or {}).get(prod) or {}).get("now"))
        if spot is not None and r.get("k") is not None:
            k = strike_of(r["k"])
            d = spot - k
            out += [(" · ", grey), (f"{prod.split('-')[0]} ${f'{abs(d):,.0f}' if spot >= 1000 else px(abs(d))} "
                                       f"{'over' if d >= 0 else 'under'}", WHITE)]
        return out

    def ladder_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        lad = self.ladder()
        self.box(y, x, h, w, "ladder", self.P.border("ladder"), "", "↑↓")
        if not lad:
            self.put(y + 2, x + 3, "waiting for the hourly event…", P(self.P.grey)); return
        prod = cfgmod.SERIES[self.series]
        spot = ((st.get("spot") or {}).get(prod) or {}).get("now")
        tickers = [r["t"] for r in lad]
        if self.sel not in tickers:
            if spot:   # the nearest strike the rule would buy (★), else the nearest one
                pool = [r for r in lad if self.in_band(r)] or lad
                self.sel = min(pool, key=lambda r: abs(r["k"] - spot))["t"]
            else:
                self.sel = tickers[len(tickers) // 2]
            self.write_view()
        i = tickers.index(self.sel)
        rows = h - 3
        top = max(0, min(len(lad) - rows, i - rows // 2))
        book = self.cur_book()
        held = {p["t"]: p for p in (book or {}).get("pos", [])}
        S = self.strat() or {}
        hdr = f"{'strike':>9} {'bid':>5} {'ask':>5}  {'prob':<{max(6, w - 47)}} {'sprd':>4} {'vol':>7} {'pos':>6}"
        self.put(y + 1, x + 2, hdr[:w - 4], P(self.P.c("head"), bold=True))
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
            tint = P(P.pick("rows", frac, reverse=True)) | base     # long shots blue, near-certain pink
            self.put(yy, x + 26 + barw, f"{(spread*100 if spread is not None else 0):>4.1f}", tint)
            self.put(yy, x + 31 + barw, f"{volume(r['v']):>7}", tint)
            hp = held.get(r["t"])
            if hp:
                self.put(yy, x + 39 + barw, f"{hp['q']:+6g}"[:6], P(GREEN if hp["q"] > 0 else MAGENTA, bold=True) | base)
        if top > 0:
            self.put(y, x + w - 2, "▲", P(YELLOW))
        if top + rows < len(lad):
            self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))

    def variants_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        vs = st.get("variants") or []
        G = st.get("gate_cfg") or {}
        self.box(y, x, h, w, "variants", self.P.border("ladder"), "", [("⏎", " use"), ("w", "atch"), "↑↓"])
        if not vs:
            self.put(y + 2, x + 3, "waiting for the engine…", P(self.P.grey)); return
        names = [v["name"] for v in vs]
        active = next((v["name"] for v in vs if v.get("active")), None)
        if self.var_sel not in names:
            self.var_sel = active or names[0]
        head = P(self.P.c("head"), bold=True)
        hdr = f"  {'variant':<8} {'hours':>5} {'entries':>7} {'P&L':>9} {'¢/ct':>6} {'win':>4} {'t':>6} gate"
        self.put(y + 1, x + 2, hdr[:w - 4], head)
        # The list scrolls under the fixed header: the rows that fit, moved so the highlighted one shows
        rows = max(1, min(len(vs), h - 3))
        si = names.index(self.var_sel)
        self.var_top = max(0, min(self.var_top, len(vs) - rows))
        if si < self.var_top:
            self.var_top = si
        elif si >= self.var_top + rows:
            self.var_top = si - rows + 1
        top = self.var_top
        for j, v in enumerate(vs[top:top + rows], start=top):
            yy = y + 2 + j - top
            sel = v["name"] == self.var_sel
            base = curses.A_REVERSE if sel else 0
            self.put(yy, x + 1, " " * (w - 2), base)
            row = P(P.pick("rows", j / max(1, len(vs) - 1))) | base
            watched = v["name"] == self.watch and not v.get("active")
            self.put(yy, x + 2, "●" if v.get("active") else "◉" if watched else " ",
                     P(GREEN if v.get("active") else YELLOW, bold=True) | base)
            self.put(yy, x + 4, f"{v['name']:<8}", P(P.c("variant"), bold=True) | base)
            pc = f"{v['per_contract']*100:+.2f}" if v.get("per_contract") is not None else "-"
            win = f"{v['win']:.0%}" if v.get("win") is not None else "-"
            t = f"{v['t']:+.2f}" if v.get("t") is not None else "-"
            self.put(yy, x + 13, f"{v['hours']:>5} {v['entries']:>7} ", row)
            pnl_c = P(GREEN if v["pnl"] >= 0 else RED, bold=True) | base
            self.put(yy, x + 28, f"{v['pnl']:>+9.2f}", pnl_c)
            self.put(yy, x + 38, f"{pc:>6} {win:>4} ", row)
            tc = self.P.grey if v.get("t") is None else GREEN if v["t"] >= 2 else RED if v["t"] <= -2 else WHITE
            self.put(yy, x + 50, f"{t:>6}", P(tc, bold=v.get("t") is not None and abs(v["t"]) >= 2) | base)
            cx = x + 57
            g = v.get("gate") or {}
            if g.get("passed"):
                self.put(yy, cx, "✓ passed", P(GREEN, bold=True) | base)
            else:
                for c in g.get("checks", []):          # a passed check is a word; one still short shows how far
                    have = c["have"]
                    txt = ({"days": "days", "entries": "ent", "pnl": "P&L"}[c["key"]] if c["ok"] else
                           f"{have:.1f}/{c['need']:g}d" if c["key"] == "days" else
                           f"{have}/{c['need']}" if c["key"] == "entries" else "P&L<0")
                    txt = ("✓" if c["ok"] else "") + txt
                    self.put(yy, cx, txt + " ", P(GREEN if c["ok"] else YELLOW) | base)
                    cx += len(txt) + 1
        # the picked variant: what it is, its cumulative P&L by hour, and what enter would do
        if top > 0:
            self.put(y + 1, x + w - 2, "▲", P(YELLOW))
        if top + rows < len(vs):
            self.put(y + 2 + rows - 1, x + w - 2, "▼", P(YELLOW))
        cur = next(v for v in vs if v["name"] == self.var_sel)
        yy = y + 3 + rows
        if yy >= y + h - 2:                            # a short box: the rows and the tip line are all that fit
            yy = y + h - 2
        else:
            self.put(yy, x + 2, f"{cur['name']}: ", P(P.c("variant"), bold=True))
            self.put(yy, x + 4 + len(cur["name"]), (cur.get("desc") or "")[:w - 8 - len(cur["name"])], P(WHITE))
        curve = cur.get("curve") or []
        gy, gh = yy + 1, y + h - 3 - (yy + 1)
        gw = w - 2 - 11
        if len(curve) >= 2 and gh >= 2:
            vals = [0.0] + curve
            lo, hi = min(vals), max(vals)
            pad = (hi - lo) * 0.1 or 1.0
            self.graph(gy, x + 1, gh, gw, vals, lo - pad, hi + pad, P.grad("eq_up" if vals[-1] >= 0 else "eq_dn"), [0.0], self.P.grey)
            self.put(gy, x + 1 + gw + 1, money(hi, True)[:10], P(self.P.grey))
            self.put(gy + gh - 1, x + 1 + gw + 1, money(lo, True)[:10], P(self.P.grey))
            self.put(gy + gh, x + 2, f"P&L over its last {len(curve)} hours", P(self.P.grey))
        elif gh >= 1 and len(curve) < 2:
            self.put(gy + 1, x + 3, "the curve starts after a couple of settled hours", P(self.P.grey))
        # The bottom row says what the highlighted variant is doing, when it is the one in use or the one
        # watched. What w and enter do is on the top border; on a short box the row is the last variant's.
        if y + 1 + rows < y + h - 2:
            if cur["name"] == active:
                tip = f"● {active} is in use: paper now, live " + ("too" if (cur.get("gate") or {}).get("passed")
                                                                  else "once it passes the gate")
                self.put(y + h - 2, x + 2, tip[:w - 4], P(GREEN, bold=True))
            elif cur["name"] == self.watch:
                self.put(y + h - 2, x + 2, f"◉ watching {cur['name']} (view only)"[:w - 4], P(YELLOW, bold=True))

    def toggle_watch(self) -> None:
        """w: show the highlighted variant as if it traded (book, equity, band), or stop. View only."""
        if self.wview():
            name, self.watch = self.watch, None
            self.save_prefs(); self.say(f"stopped watching {name}: back to the {self.active()} paper book"); return
        if self.slot3 != "variants" or "ladder" in self.hidden:
            self.say("w watches a variant: press 3 for the variants box, highlight one, then w"); return
        name = self.var_sel
        if not name or name == self.active():
            self.say(f"{name} is what trades: the paper book already shows it"); return
        self.watch = name; self.pos_off = 0; self.save_prefs()
        self.say(f"watching {name} (view only) · {self.active()} still trades · w or v goes back")

    def use_variant(self) -> None:
        """Enter in the variants box: trade the picked variant. Paper switches at once; while live is
        on or armed it asks for "live" first, as arming does."""
        if self.slot3 != "variants" or "ladder" in self.hidden:
            self.say("enter picks a variant: press 3 for the variants box first"); return
        vs = self.st.get("variants") or []
        cur = next((v for v in vs if v["name"] == self.var_sel), None)
        active = next((v["name"] for v in vs if v.get("active")), None)
        if not cur:
            return
        if cur["name"] == active:
            self.say(f"{active} is already in use"); return
        if self.st.get("mode") == "live" or self.st.get("live_enabled"):
            g = cur.get("gate") or {}
            self.modal = {"type": "variant", "title": "SWITCH LIVE VARIANT", "color": RED, "buf": "",
                          "name": cur["name"], "was": active,
                          "lines": [f"Live is on: real money would trade {cur['name']} instead of {active}.",
                                    f"{cur['name']}: {cur.get('desc', '')}",
                                    "It has passed the gate." if g.get("passed") else
                                    f"It has not passed the gate (g): live waits until it does.",
                                    "Open positions ride to settlement.",
                                    "", "type live and press enter (esc cancels):", "> "]}
            return
        self.send(op="variant", name=cur["name"])

    def cur_book(self):
        if self.wview():
            return self.wview()
        v = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
        return self.st.get(v)

    def book_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        wv = self.wview()
        v = "watch" if wv else self.book_view or ("live" if st.get("mode") == "live" else "paper")
        b = wv if wv else st.get(v) or {}
        self.box(y, x, h, w, "book", self.P.border("book"), "", PANEL_KEYS["book"](self))
        tabs: list[tuple[str, int]] = []
        for name in ("paper", "live"):
            tabs += [(f" {name} ", P(WHITE, bold=True) | curses.A_REVERSE if name == v else P(self.P.grey)), (" ", 0)]
        if wv:
            tabs += [(f" watching {self.watch} ", P(YELLOW, bold=True) | curses.A_REVERSE), (" view only", P(self.P.grey))]
        body = [tabs]                                    # the whole book is one list of lines and scrolls as one
        if b:
            eq, start = b.get("equity"), b.get("start") or 0
            pnl = (eq - start) if eq is not None and start else None
            val = P.c("value")               # book values in pink; labels take the row gradient
            lines = [("equity", money(eq), val), ("P&L", money(pnl, True) + (f"  {pnl/start:+.2%}" if pnl is not None and start else ""),
                                                     GREEN if (pnl or 0) >= 0 else RED),
                     ("cash", money(b.get("cash")), val), ("at risk", money(b.get("at_risk")), YELLOW),
                     ("fees", money(b.get("fees")), self.P.grey)]
            if v == "live":
                L = st.get("live_cfg") or {}
                lines.append(("caps", f"risk {money(L.get('capital_usd'))} · order {money(L.get('max_order_usd'))} · day -{money(L.get('max_daily_loss'))}", self.P.grey))
            g = st.get("gate") or {}
            if wv:                                       # the watched variant's own record, not the paper book's
                vr = next((x for x in st.get("variants") or [] if x["name"] == self.watch), {})
                g = {"entries": vr.get("entries", 0), "open": vr.get("open", 0), "pnl": vr.get("pnl", 0.0),
                     "win": vr.get("win"), "per_contract": vr.get("per_contract") or 0.0} if vr else {}
                lines[0] = ("equity", money(eq) + "  on a paper start", val)
            if g:
                wr = f"{g['win']:.0%}" if g.get("win") is not None else "-"
                lines.append(("rule", f"{g['entries']} settled · {g['open']} open · {money(g['pnl'], True)} · win {wr} · "
                                      f"{g['per_contract']*100:+.2f}¢/ct", GREEN if g["pnl"] >= 0 else RED))
            body += [[(f"{k:<8} ", ROW), (val, P(c, bold=k == "equity"))] for k, val, c in lines]
            body += [[("", 0)]] + self.book_lines(b)
        yy = y + 1
        room = max(0, h - 2)
        self.pos_off = max(0, min(self.pos_off, len(body) - room))
        if self.pos_off > 0:
            self.put(y, x + w - 2, "▲", P(YELLOW))
        if self.pos_off + room < len(body):
            self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))
        for j, line in enumerate(body[self.pos_off:self.pos_off + room]):
            cx = x + 2
            row = P(P.pick("rows", j / max(1, room - 1)))     # btop-style gradient down the list
            for text, attr in line:
                self.put(yy, cx, text, row if attr == ROW else attr); cx += len(text)
            yy += 1

    def book_lines(self, b: dict) -> list[list[tuple[str, int]]]:
        """The book's scrolling part (petrel style): open positions, then the last settlements, then
        the last fills, newest first. Each line is a list of (text, attr) pieces."""
        P, now = self.P, time.time()
        grey, head, white = P(self.P.grey), P(self.P.c("head"), bold=True), ROW

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
        wv = self.wview()
        v = self.book_view or ("live" if st.get("mode") == "live" else "paper")
        b = wv if wv else st.get(v) or {}
        curve = [e for _, e in (b.get("curve") or [])]
        if wv:
            curve = [b["start"]] + curve
        self.box(y, x, h, w, f"equity · {'watching ' + self.watch if wv else v}", self.P.border("equity"), "",
                 f"{len(curve) - 1} h" if wv else f"{len(curve)} min")
        if len(curve) < 2:
            self.put(y + h // 2, x + 3, "the curve starts after a settled hour" if wv else
                     "the curve starts after a couple of minutes", P(self.P.grey)); return
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
        self.box(y, x, h, w, "log", self.P.border("log"), "", PANEL_KEYS["log"](self))
        logs = self.logs_shown()
        rows = h - 2
        end = self.log_end(logs, rows)
        saved, self.clip = self.clip, x + w - 2
        shown = logs[max(0, end - rows):end]
        for j, (ts, lvl, msg) in enumerate(shown):
            yy = y + 1 + j
            age = (len(shown) - 1 - j) / max(1, rows - 1)          # 0 for the bottom (newest) line
            self.put(yy, x + 2, time.strftime("%H:%M:%S", time.localtime(ts)), P(P.pick("fade_ts", age)))
            self.put(yy, x + 10, LOG_MARK.get(lvl, " "), P(LOG_BASE.get(lvl, WHITE), bold=True))
            cx = x + 11
            for text, attr in self.log_spans(msg, lvl, P.pick("fade_txt", age)):
                self.put(yy, cx, text, attr); cx += len(text)
                if cx >= x + w - 2:
                    break
        self.clip = saved
        if end - rows > 0:
            self.put(y, x + w - 2, "▲", P(YELLOW))
        if end < len(logs):
            note = f"{len(logs) - end} newer "
            self.put(y + h - 1, x + w - 2 - len(note), note, P(YELLOW))
            self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))

    def log_spans(self, msg: str, lvl: str, fade: int = WHITE) -> list[tuple[str, int]]:
        """Split one log line into colored pieces: book names, tickers, YES/NO, signed money, slots, numbers."""
        P, many = self.P, self.P.many
        base = LOG_BASE.get(lvl)
        plain = P(base) if base is not None else P(fade)
        out, i = [], 0
        for m in LOG_TOKENS.finditer(msg):
            if m.start() > i:
                out.append((msg[i:m.start()], plain))
            g, t = m.lastgroup, m.group()
            if g == "book":
                name = t[:-1]
                out.append((name, P(LOG_BOOK.get(name.lower(), YELLOW), bold=True)))
                out.append((":", P(LOG_BOOK.get(name.lower(), YELLOW))))
            elif g == "ticker":
                ev, _, k = t.rpartition("-") if m.group("strike") else (t, "", "")
                out.append((ev, P(P.c("ticker"))))
                if k:
                    out.append(("-", P(P.c("ticker"))))
                    out.append((k, P(P.c("strike"), bold=True)))
            elif g == "yes":
                out.append((t, P(GREEN, bold=True)))
            elif g == "no":
                out.append((t, P(MAGENTA, bold=True)))
            elif g == "signed":
                out.append((t, P(GREEN if t[0] == "+" else RED, bold=True)))
            elif g == "money":
                out.append((t, P(YELLOW)))
            elif g == "slot":
                out.append((t, P(P.c("slot"), bold=True)))
            elif g == "verb":
                out.append((t, P(P.c("verb"))))
            elif g == "variant":
                out.append((t, P(P.c("variant"), bold=True)))
            elif g == "phrase":
                out.append((t, P(P.c("phrase"))))
            elif g == "good":
                out.append((t, P(GREEN)))
            elif g == "bad":
                out.append((t, P(RED, bold=True)))
            elif g == "num":
                out.append((t, P(WHITE, bold=True)))
            else:   # sep
                out.append((t, P(P.c("sep"))))
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
        self.scroll(name, d)

    def scroll(self, name: str | None, d: int) -> None:
        """Move one box d steps (negative is up). Boxes that don't scroll ignore it."""
        if name == "ladder":
            self.step_ladder(d)
        elif name == "variants":
            names = [v["name"] for v in self.st.get("variants") or []]
            if names:
                i = names.index(self.var_sel) if self.var_sel in names else 0
                self.var_sel = names[max(0, min(len(names) - 1, i + d))]
        elif name == "book":
            self.pos_off = max(0, self.pos_off + d)    # book_panel clamps the far end
        elif name == "log":
            logs = self.logs_shown()
            rows = self.rects["log"][2] - 2 if "log" in self.rects else 1
            end = max(min(rows, len(logs)), min(len(logs), self.log_end(logs, rows) + 3 * d))
            self.log_anchor = None if end >= len(logs) else logs[end - 1][0]

    def focusable(self) -> list[str]:
        """The scrolling boxes on screen, in reading order."""
        H, W = self.s.getmaxyx()
        return [n for n, *_ in self.layout(H, W) if n in SCROLLS]

    def move_focus(self, d: int) -> None:
        names = self.focusable()
        if not names:
            self.say("no scrolling box is shown · 3, 4, 6 bring back ladder or variants, book, log"); return
        i = names.index(self.focus) if self.focus in names else -1 if d > 0 else 0
        self.focus = names[(i + d) % len(names)]
        self.say(f"focus: {self.focus}")

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

    def draw_overlay(self, lines: list, title: str, color: int = CYAN, scroll: bool = False, hints=None) -> None:
        """A centered box of lines. A line is a plain string (colored by `rich`) or a list of (text, attr).
        With scroll, lines that don't fit scroll by self.ov_off (help and gate); ▲▼ and the place show on the border."""
        H, W = self.s.getmaxyx()
        lines = [self.rich(l) if isinstance(l, str) else l for l in lines]
        w = min(W - 4, max(sum(len(t) for t, _ in l) for l in lines) + 4)
        h = min(H - 2, len(lines) + 2)
        y, x = (H - h) // 2, (W - w) // 2
        room = h - 2
        off = 0
        if scroll:
            self.ov_room, self.ov_len = room, len(lines)
            self.ov_off = off = max(0, min(self.ov_off, len(lines) - room))
        for r in range(h):
            self.put(y + r, x, " " * w)
        long = scroll and len(lines) > room
        self.box(y, x, h, w, title, color, "", hints or ([("↑↓", " scroll"), ("esc", " close")] if long else [("any key", " closes")]))
        saved, self.clip = self.clip, x + w - 2
        for j, l in enumerate(lines[off:off + room]):
            cx = x + 2
            for text, attr in l:
                if text == RULE_LINE:                      # a divider across the box
                    text = "─" * (w - 4)
                self.put(y + 1 + j, cx, text, attr); cx += len(text)
        self.clip = saved
        if long:
            P = self.P
            if off > 0:
                self.put(y, x + w - 2, "▲", P(YELLOW))
            if off + room < len(lines):
                self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))
            where = f" {off + 1}-{min(len(lines), off + room)} of {len(lines)} "
            self.put(y + h - 1, x + w - 4 - len(where), where, P(P.grey))

    def rich(self, line: str) -> list[tuple[str, int]]:
        """Color a line of help, gate or prompt text: a leading CAPS heading, [x] / [ ] checks, (k) key
        references, "quoted" words to type, warnings, and numbers, money and tickers as in the log."""
        P = self.P
        out: list[tuple[str, int]] = []
        m = re.match(r"^([A-Z][A-Z ]*[A-Z])(\s{2,})(.*)$", line)
        if m:
            out += [(m.group(1), P(P.c("key"), bold=True)), (m.group(2), 0)]
            line = m.group(3)
        i = 0
        for m in OVERLAY_TOKENS.finditer(line):
            if m.start() > i:
                out += self.log_spans(line[i:m.start()], "", WHITE)
            g, t = m.lastgroup, m.group()
            if g == "ok":
                out.append((t, P(GREEN, bold=True)))
            elif g == "todo":
                out.append((t, P(RED, bold=True)))
            elif g == "ref":
                out += [("(", P(P.grey)), (t[1:-1], P(P.c("key"), bold=True)), (")", P(P.grey))]
            elif g == "quote":
                out.append((t, P(P.c("key"), bold=True)))
            elif g == "warn":
                out.append((t, P(YELLOW, bold=True)))
            elif g == "keyhint":
                out.append((t, P(P.c("key"), bold=True)))
            elif g == "path":
                out.append((t, P(P.c("ticker"))))
            i = m.end()
        if i < len(line):
            out += self.log_spans(line[i:], "", WHITE)
        return out or [("", 0)]

    def help_lines(self, W: int = 200) -> list:
        """The help: prose filled from the config in use, the boxes, the key legend (two columns
        when the screen is wide enough, else one), and the command line. It scrolls."""
        P, st = self.P, self.st
        S = st.get("strategy") or cfgmod.Strategy().__dict__
        G = st.get("gate_cfg") or cfgmod.Gate().__dict__
        L = st.get("live_cfg") or cfgmod.Live().__dict__
        mins = [str(m) for m in S.get("minutes", [])]
        fill = dict(
            minutes=", ".join(mins[:-1]) + " and " + mins[-1] if len(mins) > 1 else "".join(mins),
            band=f"{S['band_lo']*100:.0f}-{S['band_hi']*100:.0f}c", spread=f"{S['max_spread']*100:g}",
            contracts=S["contracts"], hour_cost=S["max_hour_cost"],
            days=G["min_days"], entries=G["min_entries"], pnl=G["min_pnl"],
            risk=L["capital_usd"], order=L["max_order_usd"], day=L["max_daily_loss"],
            coins=" ".join(v.split("-")[0] for v in cfgmod.SERIES.values()), paper=cfgmod.Config().paper_capital,
            variants="\n".join(f"|  {n:<8} {d}" for n, d in shadowmod.DESC.items()),
            retired="\n".join(f"|  {n:<8} {d}" for n, d in shadowmod.RETIRED.items()))
        lines: list = []
        width = max(40, min(HELP_WIDTH, W - 8))
        for n, (heading, para) in enumerate(HELP):
            if n:
                lines.append("")
            pad = 10 if heading else 0
            first = True
            for chunk in para.format(**fill).split("\n"):
                if chunk.startswith("|"):                 # a literal line, indented under the text
                    lines.append(" " * pad + chunk[1:]); first = False; continue
                for k, ln in enumerate(textwrap.wrap(chunk, width - pad)):
                    lead = f"{heading:<{pad}}" if first else " " * pad
                    lines.append(lead + ln); first = False
        head = lambda t: [(t, P(P.c("key"), bold=True))]
        divider = [(RULE_LINE, P(P.grey))]
        lines += ["", divider, head("BOXES")]
        for num, name, what in HELP_BOXES:
            lines.append([("  " + f"{num:<1}", P(P.c("key"), bold=True)), (f"{name:<9}", P(P.c("variant"), bold=True)),
                          (" " + what, P(WHITE))])
        lines += ["", divider, head("KEYS")]
        cols = []
        for col in HELP_KEYS:
            kw = max(len(k) for _, keys in col for k, _ in keys) + 2      # this column's key width
            blocks = []
            for section, keys in col:
                blocks.append([[("  " + section, P(P.c("head"), bold=True))]] +
                              [[("    " + f"{k:<{kw}}", P(P.c("key"), bold=True)),
                                (what.format(themes=len(themes.NAMES)), P(WHITE))] for k, what in keys])
            cols.append(blocks)
        width = max(sum(len(t) for t, _ in r) for b in cols[0] for r in b) + 3
        right_w = max(sum(len(t) for t, _ in r) for b in cols[1] for r in b)
        if width + right_w + 8 <= W - 4:                          # side by side, sections stacked in each column
            left = [r for n, b in enumerate(cols[0]) for r in ([[("", 0)]] if n else []) + b]
            right = [r for n, b in enumerate(cols[1]) for r in ([[("", 0)]] if n else []) + b]
            for j in range(max(len(left), len(right))):
                l = left[j] if j < len(left) else []
                r = right[j] if j < len(right) else []
                lines.append(l + [(" " * (width - sum(len(t) for t, _ in l)), 0)] + r)
        else:                                                     # narrow: one column
            for n, b in enumerate(cols[0] + cols[1]):
                lines += ([[("", 0)]] if n else []) + b
        lines += ["", divider, head("COMMAND LINE")]
        cw = max(len(c) for c, _ in HELP_CLI) + 2
        lines += [[("  " + f"{c:<{cw}}", P(P.c("ticker"))), (what, P(WHITE))] for c, what in HELP_CLI]
        lines += ["", [(f"  runtime files: {cfgmod.RUNTIME}", P(P.grey))]]
        return lines

    def gate_lines(self) -> list:
        st, P = self.st, self.P
        g = st.get("gate") or {}
        L = st.get("live_cfg") or {}

        def head(text, note=""):
            return [(text, P(P.c("head"), bold=True))] + ([(note, P(P.grey))] if note else [])
        out: list = [head("PAPER CHECKS", f"   {g.get('variant', 'the rule')} trades live only when every one passes"),
                     "  (unless live.auto_needs_gate is false)"]
        for c in g.get("checks", []):
            out.append(f"  [{'x' if c['ok'] else ' '}] {c['name']}  ({c['detail']})")
        if st.get("shadow"):
            rows = shadowmod.table(st["shadow"])
            out += ["", head("VARIANTS", "   side by side on paper; (3) shows them, enter picks one (tern report)")]
            out += [[("  " + rows[0], P(P.grey, bold=True))]] + ["  " + l for l in rows[1:]]
        out += ["", head("GO LIVE", "   all three are needed")]
        out.append(f"  [{'x' if st.get('keys') else ' '}] Kalshi key present ({cfgmod.KEYFILE_ENV})")
        out.append(f"  [{'x' if st.get('live_enabled') else ' '}] armed with L")
        out.append(f"  [{'x' if st.get('mode') == 'live' else ' '}] authenticated" + (f": {st.get('live_error')}" if st.get("live_error") else ""))
        out += ["", head("CAPS"), f"  ${L.get('capital_usd', 0):,.0f} at risk · ${L.get('max_order_usd', 0):,.0f} per order · "
                    f"-${L.get('max_daily_loss', 0):,.0f} a day turns live off"]
        return out

    # ------------------------------------------------------------ settings (e)
    def set_rows(self) -> list:
        """The settings box's rows: the engine's (config.SETTINGS) and the screen's own."""
        return cfgmod.SETTINGS + SCREEN_SETTINGS

    def set_value(self, key: str):
        """What the setting is: the engine's value, or the one just sent until the engine shows it (10 s)."""
        if key == "screen.log":
            return self.log_filter
        now_v = (self.st.get("settings") or {}).get(key)
        sent = self.set_sent.get(key)
        if sent and now_v != sent[0] and time.time() - sent[1] < 10:
            return sent[0]
        self.set_sent.pop(key, None)
        return now_v

    @staticmethod
    def set_fmt(row, v) -> str:
        if v is None:
            return "?"
        if row.kind == "bool":
            return "on" if v else "off"
        if row.kind == "choice":
            return LOG_FILTERS.get(v, v)
        if row.kind == "n":
            return f"{v:g}"
        return ("-" if v < 0 else "") + f"${abs(v):,.2f}".replace(".00", "")

    def settings_lines(self) -> list:
        P = self.P
        rows = self.set_rows()
        self.set_sel %= len(rows)
        lw = max(len(r.label) for r in rows)
        armed = self.st.get("live_enabled") or self.st.get("mode") == "live"
        out: list = []
        for i, row in enumerate(rows):
            if row.group:
                out += ([""] if out else []) + [[(row.group, P(P.c("head"), bold=True))]]
            sel = i == self.set_sel
            if sel and self.set_buf is not None:
                val = [(f" {self.set_buf}▏", P(YELLOW, bold=True) | curses.A_REVERSE)]
            else:
                v = self.set_value(row.key)
                col = YELLOW if row.key in self.set_sent else (GREEN if v else RED) if row.kind == "bool" else P.c("key")
                val = [(f" {self.set_fmt(row, v)} ", P(col, bold=True) | (curses.A_REVERSE if sel else 0))]
            mark = ("▸ ", P(P.c("key"), bold=True)) if sel else ("  ", 0)
            out.append([mark, (f"{row.label:<{lw}}  ", P(WHITE, bold=sel))] + val)
        row = rows[self.set_sel]
        def details(r) -> list[str]:
            """The two lines under the divider for row r."""
            if r.kind in ("usd", "n"):
                return [r.what, f"{self.set_fmt(r, r.lo)} to {self.set_fmt(r, r.hi)}, steps of {self.set_fmt(r, r.step)}  ·  saved at once"]
            return [r.what, "←→ or enter changes it  ·  saved at once"]
        # pad to the widest any row needs, so the box keeps one size while the cursor moves
        wide = max(len(t) for r in rows for t in details(r))
        out += [RULE_LINE] + [[(t.ljust(wide), P(P.grey))] for t in details(row)]
        if "settings" not in self.st or any(r.key not in self.st["settings"] for r in cfgmod.SETTINGS):
            out += [[("The engine running is older than this screen: restart it (s, s) to change these.", P(YELLOW, bold=True))]]
        elif armed:
            out += [[("Live is armed: raising a live limit or lowering a gate asks you to type \"live\".", P(YELLOW, bold=True))]]
        return out

    def send_setting(self, row, value) -> None:
        key = row.key
        if row.key == "screen.log":
            self.log_filter = value; self.log_anchor = None; self.save_prefs(); return
        if row.kind in ("usd", "n"):
            if not row.lo <= value <= row.hi:
                self.say(f"{row.label} must be {self.set_fmt(row, row.lo)} to {self.set_fmt(row, row.hi)}"); return
            if row.kind == "n":
                value = float(int(value))
        cur = self.set_value(key)
        if cur is not None and value == cur:
            return
        loosens = row.risk and cur is not None and (value - cur) * row.risk > 0
        if loosens and (self.st.get("live_enabled") or self.st.get("mode") == "live"):
            what = "Raise" if row.risk > 0 else "Lower"
            self.modal = {"type": "setlive", "title": "LIVE IS ARMED", "color": RED, "buf": "", "key": key, "value": value,
                          "lines": [f"{what} {row.sec} {row.label} from {self.set_fmt(row, cur)} to {self.set_fmt(row, value)}?",
                                    "Live is armed: this is real money on Kalshi.", "",
                                    "type live and press enter (esc cancels):", "> "]}
            return
        self.send(op="set", key=key, value=value)
        self.set_sent[key] = (value, time.time())

    def settings_key(self, ch: int) -> None:
        rows = self.set_rows()
        row = rows[self.set_sel % len(rows)]
        if self.set_buf is not None:                      # typing a value
            if ch in (10, 13, curses.KEY_ENTER):
                buf, self.set_buf = self.set_buf.strip().lstrip("$").replace(",", ""), None
                try:
                    self.send_setting(row, float(buf))
                except ValueError:
                    if buf:
                        self.say(f"not a number: {buf}")
            elif ch == 27:
                self.set_buf = None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.set_buf = self.set_buf[:-1]
            elif 32 <= ch < 127 and chr(ch) in "0123456789.$,-" and (chr(ch) != "-" or not self.set_buf):
                self.set_buf = (self.set_buf + chr(ch))[:12]
            return
        step = {curses.KEY_LEFT: -1, ord("-"): -1, curses.KEY_RIGHT: 1, ord("+"): 1, ord("="): 1}.get(ch)
        if ch in (curses.KEY_UP, ord("k")):
            self.set_sel = (self.set_sel - 1) % len(rows)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.set_sel = (self.set_sel + 1) % len(rows)
        elif row.kind in ("bool", "choice") and (step or ch in (10, 13, curses.KEY_ENTER, ord(" "))):
            cur = self.set_value(row.key)
            if cur is None:
                self.say("no value from the engine yet"); return
            if row.kind == "bool":
                self.send_setting(row, not cur)
            else:
                order = list(LOG_FILTERS)
                i = order.index(cur) if cur in order else 0
                self.send_setting(row, order[(i + (step or 1)) % len(order)])
        elif step:
            cur = self.set_value(row.key)
            if cur is None:
                self.say("no value from the engine yet"); return
            self.send_setting(row, max(row.lo, min(row.hi, round((cur + step * row.step) / row.step) * row.step)))
        elif ch in (10, 13, curses.KEY_ENTER):
            self.set_buf = ""
        elif ord("0") <= ch <= ord("9"):
            self.set_buf = chr(ch)
        else:                                             # esc, e or anything else closes it
            self.overlay = None

    def logs_shown(self) -> list:
        """The engine's log lines the log box shows, after the log filter (settings, e)."""
        logs = self.st.get("logs") or []
        if self.log_filter == "all":
            return logs
        out = []
        for line in logs:
            lvl, msg = line[1], line[2]
            if msg.startswith("shadow:"):
                continue
            if self.log_filter == "trades" and not TRADE_LINE.match(msg):
                continue
            if self.log_filter == "quiet" and lvl == "info" and msg.startswith("paper:"):
                continue
            out.append(line)
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
            self.panel_key = SUPER[PANELS.index("ladder" if name in SLOT3 else name)]
            self.drawing = name
            getattr(self, name + "_panel")(y, x, h, w)
        self.clip = None
        self.panel_key = ""
        self.drawing = ""
        if not boxes:
            msg = "every box is hidden · press 1-6 to show spot, contract, ladder, book, equity, log"
            self.put(H // 2, max(0, (W - len(msg)) // 2), msg, self.P(self.P.grey))
        self.footer(H, W)
        if self.overlay == "settings":
            self.draw_overlay(self.settings_lines(), "settings", self.P.border("contract"),
                              hints=[("↑↓", " pick"), ("←→", " step"), ("enter", " type"), ("esc", " close")])
        elif self.overlay:
            lines = self.help_lines(W) if self.overlay == "help" else self.gate_lines()
            self.draw_overlay(lines, self.overlay, self.P.border("contract"), scroll=True)
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
            if d and self.overlay == "settings" and not self.modal:
                self.set_sel = (self.set_sel + d) % len(self.set_rows())
            elif d and self.overlay and not self.modal:
                self.ov_off = max(0, self.ov_off + 3 * d)
            elif d and not self.modal:
                self.wheel(my, mx, d)
            return True
        if self.modal:
            m = self.modal; self.modal = None
            if m.get("type") in ("arm", "variant", "setlive"):
                arm = m["type"] == "arm"
                kept = "not armed" if arm else "cap kept" if m["type"] == "setlive" else f"kept {m.get('was')}"
                if ch in (10, 13, curses.KEY_ENTER):
                    if m["buf"].strip().lower() == "live":
                        if arm:
                            self.send(op="live", on=True); self.say("live armed")
                        elif m["type"] == "setlive":
                            self.send(op="set", key=m["key"], value=m["value"])
                            self.set_sent[m["key"]] = (m["value"], time.time())
                        else:
                            self.send(op="variant", name=m["name"])
                    else:
                        self.say(kept + " (type live exactly)")
                    return True
                if ch in (27,):
                    self.say(kept); return True
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
        if self.overlay == "settings":
            self.settings_key(ch)
            return True
        if self.overlay:
            step = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_PPAGE: -(self.ov_room - 1),
                    curses.KEY_NPAGE: self.ov_room - 1, ord(" "): self.ov_room - 1,
                    curses.KEY_HOME: -10 ** 6, curses.KEY_END: 10 ** 6}.get(ch)
            if step is not None and self.ov_len > self.ov_room:   # draw_overlay clamps the far end
                self.ov_off = max(0, min(self.ov_len - self.ov_room, self.ov_off + step))
            else:
                self.overlay = None
            return True
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, ord("k"), ord("j")):
            names = self.focusable()
            if self.focus not in names and names:
                self.focus = names[0]
            self.scroll(self.focus, {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                                     curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10}[ch])
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.move_focus(-1 if ch == curses.KEY_LEFT else 1)
        elif ch in (10, 13, curses.KEY_ENTER):
            self.use_variant()
        elif ord("1") <= ch <= ord(str(len(PANELS))):
            self.toggle(ch - ord("1"))
        elif ch in (9, curses.KEY_BTAB):
            order = list(cfgmod.SERIES)
            step = -1 if ch == curses.KEY_BTAB else 1
            self.series = order[(order.index(self.series) + step) % len(order)] if self.series in order else order[0]
            self.sel = None; self.write_view(); self.save_prefs(); self.say(f"viewing {cfgmod.SERIES[self.series]}")
        elif ch == ord("r"):
            self.send(op="series", series=self.series)
        elif ch in (ord("t"), ord("T")):
            self.P.set_theme(themes.next_name(self.P.name, -1 if ch == ord("T") else 1))
            self.save_prefs()
            self.say(f"theme {self.P.name}" + ("" if self.P.many else " (needs a 256-color terminal)"))
        elif ch == ord("c"):
            order = list(CHARTS)
            self.chart = order[(order.index(self.chart) + 1) % len(order)] if self.chart in order else "15m"
            self.save_prefs()
        elif ch == ord("v"):
            if self.wview():                             # v leaves the watch view for the real book
                self.watch = None; self.save_prefs(); self.say("back to the paper/live book")
            else:
                cur = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
                self.book_view = "live" if cur == "paper" else "paper"
        elif ch == ord("w"):
            self.toggle_watch()
        elif ch == ord("g"):
            self.overlay, self.ov_off = "gate", 0
        elif ch == ord("e"):
            self.overlay, self.set_buf = "settings", None
        elif ch == ord("?") or ch == ord("h"):
            self.overlay, self.ov_off = "help", 0
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


def main(runtime: str = cfgmod.RUNTIME) -> None:
    os.environ.setdefault("ESCDELAY", "25")
    # Name the terminal window, so the taskbar and title bar say "tern".
    if sys.stdout.isatty():
        sys.stdout.write("\033]2;tern\007"); sys.stdout.flush()
    curses.wrapper(lambda s: Screen(s, runtime).loop())
