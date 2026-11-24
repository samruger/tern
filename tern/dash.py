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
import threading
import time
import uuid

from . import config as cfgmod
from . import names as namesmod
from . import reports
from . import shadow as shadowmod
from . import variants as varmod
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
FEW_BORDER = {"spot": CYAN, "contract": MAGENTA, "ladder": BLUE, "book": YELLOW, "log": BLUE}
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
    r"|(?P<variant>\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?= [+-][0-9]))"
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
    ("THE RULE", "Kalshi prices long shots too high and favorites too low. With {variant}, the variant selected, the "
                 "rule enters at {minutes} minutes before each "
                 "hourly close: it buys the favorite side of every strike whose mid is {band} (spread "
                 "{spread}c or less) at the ask, {contracts:g} contracts a strike and at most ${hour_cost:,.0f} an hour "
                 "a coin ({rule_coins}){caps}, and holds it to settlement. Over 67 days of BTC history the base rule "
                 "(75-95c, 25 20 15 10 minutes, 2c) made +0.6c a contract on data it was not chosen on."),
    ("VARIANTS", "Each variant runs on paper side by side with the others, with its own record, its own cash and "
                 "real fees. The built-in ones:\n"
                 "{variants}\n"
                 "(5) shows them. Highlight one with ↑↓ and press (enter) to select it, marked ●: the book, ladder, "
                 "contract and gate show it. Selecting changes nothing that trades.\n"
                 "A variant holds everything about how it trades: its rule, contracts a strike, most an hour and paper start. "
                 "It never buys more than the cash it has left. Once saved it never changes. Make your own, marked +: (e) there opens "
                 "the highlighted one's settings as a draft, and (s) there saves your changes as a new variant with an empty "
                 "record; the one it came from and its record stay. (space) turns one off or on, (D) deletes one, (r) renames "
                 "it. At most {max_own} of your "
                 "own; a deleted one's record stays on file.\n"
                 "Retired, records kept and listed by (tern report):\n"
                 "{retired}"),
    ("THE GATE", "(g) Live trades a variant only after its own paper record has {days:g} days of hours "
                 "traded, {entries} settled entries and P&L of ${pnl:g} or more after fees. Hours with the engine or the "
                 "variant off don't count, and moving live to another never skips it: that one needs its own record."),
    ("LIVE", "(L) goes live with the selected variant: it asks you to type \"live\". Selecting another one later does not "
             "move live; (L) with another selected asks whether to move it there, and (L) with live's own selected turns it "
             "off. It needs KALSHI_KEY_ID and KALSHI_KEY_FILE in "
             "~/.config/tern/kalshi.env. The status line turns red (● LIVE) once Kalshi accepts the key. "
             "Caps: ${risk:,.0f} at risk at once and ${order:,.0f} per order; a loss of ${day:,.0f} since local midnight "
             "turns live off and leaves open positions to settle. Every order is immediate-or-cancel. Kalshi "
             "is the record: its positions overwrite the engine's every 15 seconds."),
    ("COINS", "(C) steps through {coins}. Which coins a variant trades is in its settings. "
              "The 67-day test was BTC only, so the rule trades BTC unless you turn others on."),
    ("PAPER", "{variant} starts with ${paper:,.0f}, its paper start. Every variant that is on trades on paper all the "
              "time, live or not, so its record keeps growing."),
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
    ("⁴", "book", "the selected variant's equity, P&L, cash, at risk, fees and record, then open"),
    ("", "", "  positions, settlements and fills; (V) switches paper / live"),
    ("⁶", "log", "what the engine did, newest at the bottom; scroll back with ↑↓"),
    ("⁵", "variants", "each variant's hours, entries, P&L, ¢ a contract, win rate, t,"),
    ("", "", "  and gate progress (yellow: still short); ● selected, L live's, + one you"),
    ("", "", "  made; the highlighted one's P&L by hour below"),
    ("", "tabs", "one box fills the screen; the others are tabs on its top border: (1) to (6) show"),
    ("", "", "  one, (tab) steps through. Its own keys are on its bottom border, and the log's"),
    ("", "", "  has every key that isn't one box's"),
    ("", "status", "bottom row: PAPER or LIVE, the coin's price and 1h change, the hourly event"),
    ("", "", "  and time to its close, rule ON/off with the next slot (+live when live"),
    ("", "", "  trades), stale data warnings (usually Wi-Fi), engine ok / stale / down"),
]

# overlay text worth a color: checks, key references like (g), words to type, warnings, paths and names
OVERLAY_TOKENS = re.compile(
    r"(?P<ok>\[x\])|(?P<todo>\[ \])"
    r"|(?P<ref>\((?:[a-zA-Z?1-8]|tern \w[\w-]*|enter|tab|esc)\))"
    r"|(?P<quote>\"[^\"]+\"|\blive and press enter\b)"
    r"|(?P<warn>\b(?:real money|turns live off|turns itself off|Nothing trades|No key found yet|Wipe)\b)"
    r"|(?P<keyhint>^(?:y|any key|any other key)\b|\besc\b|·\s(?:any (?:other )?key)\b)"
    r"|(?P<path>~/\S+|\bKALSHI_KEY_(?:ID|FILE)\b|\blive\.\w+)")
SHADOW_LINE = re.compile(r"^(shadow: \S+ settled|paper: \d+m slot on \S+ bought) · (.+)$")   # the engine's lines after an
                                                                                             # hour settles, and a slot
LOG_GRID_COLS = 3                                # variants a row at most, however wide the screen
SHADOW_CELL = re.compile(r"^(\S+) ([+-]\d[\d,]*\.\d\d|\d+)$")
RULE_LINE = "\0rule"                              # placeholder text: draw_overlay turns it into a divider

# the help's key legend: two columns of (section, [(keys, what they do)])
HELP_KEYS = [
    [("move", [("tab S-tab", "next / previous box"),
               ("1-6", "spot, contract, ladder, book, variants, log"),
               ("↑↓  j k", "scroll: the ladder and contract pick a"),
               ("", "strike, variants highlights one"),
               ("PgUp PgDn", "ten steps at a time"),
               ("C", "next coin")]),
     ("look", [("c", "spot: chart span: 5m 15m 1h 24h"),
               ("V", "book: paper / live view"),
               ("o", "reports: every record, live and saved"),
               ("", "(←→ ↑↓ move, PgUp PgDn page, s saves one)"),
               ("t T", "next / previous of {themes} color themes")])],
    [("variants", [("enter", "select the highlighted one: the book,"),
                ("", "band and gate show it"),
                ("e", "edit the highlighted one's settings: s saves"),
                ("", "them as a new variant, esc discards; that one"),
                ("", "and its record stay as they are"),
                ("space", "turn it on / off"),
                ("D", "delete it, if it is one you made"),
                ("r", "rename it: its record keeps its own name")]),
     ("trade", [("L", "live: on with the selected variant, off, or"),
                ("", "moved to it (type \"live\")"),
                ("g", "gate, variants and go-live checklist"),
                ("s", "settings (live limits, gate, alerts, web, log)")]),
     ("engine", [("P", "pause (asks y) / start the engine"),
                 ("h ?", "this help"),
                 ("q", "close the screen; the engine keeps going")]),
     ("settings", [("↑↓", "pick a setting"),
                   ("←→  - +", "step it down / up, or flip it"),
                   ("enter 0-9", "type a value, enter saves"),
                   ("s", "in a variant (e): save as a new variant"),
                   ("esc", "close; in a variant: throw changes away")]),
     ("in here", [("↑↓ PgUp PgDn", "scroll this help or the gate"),
                  ("Home End", "top / bottom"),
                  ("any other key", "close it")])],
]

# command line, shown at the end of the help
HELP_CLI = [
    ("tern", "this screen (starts the engine in paper if it is down)"),
    ("tern report", "every variant's record, deleted and retired ones too (o shows it here)"),
    ("tern report --periods", "the same by settings period"),
    ("tern report --save", "save a summary: status, variants (report-timer: weekly)"),
    ("tern status", "a plain-text summary"),
    ("tern live-check", "test the Kalshi key: balance and positions, no orders"),
    ("systemctl --user status tern", "the engine's service"),
]

# what the log box may show (settings, s): key -> how the settings box names it
LOG_FILTERS = {"all": "everything", "no_shadow": "no shadow lines", "quiet": "live, warnings, commands",
               "trades": "trades only"}
TRADE_LINE = re.compile(r"^(?:live: \d+m slot on .*, [1-9]\d* bought$|paper: \d+m slot on \S+ bought · |live: \S+ (?:settled|finalized) )")
SCREEN_SETTINGS = [cfgmod.Setting("screen", "log", "log shows", kind="choice", group="SCREEN",
                                  what="shadow: the variants' hourly results · quiet hides paper too · trades: buys and settlements only")]

# spot chart spans: c cycles them in this order (the engine sends ~15 min of seconds and 24h of minutes)
CHARTS = {"5m": 300, "15m": 900, "1h": 3600, "24h": 86400}

# the boxes, each filling the screen; the others are tabs on its top border. A box's number shows it, tab steps through
BOXES = ["spot", "contract", "ladder", "book", "variants", "log"]
SUPER = "¹²³⁴⁵⁶⁷⁸⁹"
DROP_FIRST = ("↑↓", "space")                     # border hints that go before the others when they don't all fit


# the reports screen (o): the list's width, and the lines of a report that are a table's header, a table's row
# (name, then whole-number columns and a P&L) and a name with what it is
REPORT_LIST_W = 28
REPORT_HEAD = re.compile(r"^\s*variant\s.*\bhours\s+entries\b")
REPORT_ROW = re.compile(r"^(\s*)(\S.*?)(\s+\d+\s+\d+(?:\s+\d+)?\s+[+-]\d+\.\d\d\s+\S.*)$")
REPORT_NAME = re.compile(r"^(\s*)([a-z0-9][\w-]*(?: \([^)]*\))?)(: )(.+)$")


def wrap_report(lines: list[str], w: int) -> list[str]:
    """A report's lines at a width: sentences wrap, indented under themselves; a table's lines stay as they are."""
    out: list[str] = []
    for l in lines:
        if len(l) <= w or REPORT_HEAD.match(l) or REPORT_ROW.match(l):
            out.append(l)
        else:
            lead = " " * (len(l) - len(l.lstrip()))
            out += textwrap.wrap(l, w, subsequent_indent=lead + "    ", drop_whitespace=True) or [l]
    return out


# each box's keys, on its bottom border. The log's has every key that isn't one box's
PANEL_KEYS = {
    "spot": lambda s: [("C", "oin"), ("c", "hart " + s.chart)],
    "contract": lambda s: [("C", "oin"), ("↑↓", " strike")],
    "ladder": lambda s: [("C", "oin"), ("↑↓", " strike")],
    "book": lambda s: [("V", "iew"), ("L", "ive"), "↑↓"],
    "variants": lambda s: [("⏎", " select"), ("e", "dit"), ("space", " on/off"), ("r", "ename"), ("D", "elete"), ("L", "ive"), "↑↓"],
    "log": lambda s: [("t", "heme ", s.P.name), ("g", "ate"), ("s", "ettings"), ("", "rep", "o", "rts"),
                      ("P", "ause" if s.alive else " start"), ("h", "elp"), ("q", "uit"), "↑↓"],
}


def selected_overlay(st: dict, name: str | None) -> dict:
    """The status as the screen shows it with variant `name` selected: its record as the paper book, its rule, its gate.
    Selecting changes nothing that trades. live_gate is the engine's: the variant live trades."""
    row = next((v for v in st.get("variants") or [] if v["name"] == name), None)
    if row is None:
        return st
    view = (st.get("views") or {}).get(name) or {}
    return {**st, "paper": view, "strategy": view.get("strategy"), "live_gate": st.get("gate") or {},
            "gate": {**(row.get("gate") or {}), "entries": row.get("entries", 0), "open": row.get("open", 0),
                     "pnl": row.get("pnl", 0.0), "win": row.get("win"), "per_contract": row.get("per_contract") or 0.0}}


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
        self.edit_var: str | None = None       # e: the variant the box's draft starts from (None: the settings box)
        self.draft: dict = {}                  # E: field -> value changed there and not saved
        self.set_back: int | None = None       # the settings row to go back to after E
        self.var_new: dict | None = None       # a draft was sent to be saved: cid, and its variant and draft
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
        self.log_anchor: tuple | None = None   # (ts, sub) of the bottom log row while scrolled back; None follows new lines
        self.pos_off = 0                       # first line shown under the book's totals
        self.prefs_path = os.path.join(runtime, "dash.json")
        self.tab = BOXES[0]                    # the box on screen
        self.drawing = ""                      # the box being drawn: box() gives it the tabs and its keys
        self.var_sel: str | None = None        # the variant the cursor is on in the variants box
        self.var_top = 0                       # first variant row shown when the list is taller than its box
        self.variant: str | None = None        # the variant selected (enter): the book, band and gate show it
        self.raw: dict = {}                    # status.json as the engine wrote it; self.st is it with that one selected
        self.set_sub = 0                       # in a slots / coins row of the settings box: the choice the cursor is on
        self.rep_list: list = []               # o: the reports there are (reports.index), looked up every 2 s
        self.rep_at = 0.0
        self.rep_sel = "desk"                  # the report shown
        self.rep_focus = "reports"             # which of its two boxes ↑↓ move in: reports (the list) or report
        self.rep_top = 0                       # first row of the list shown
        self.rep_off, self.rep_room, self.rep_len = 0, 1, 0   # the report's scroll: as ov_off, ov_room, ov_len
        self.rep_rows: dict[int, str] = {}     # screen row -> the report listed there, as last drawn
        self.rep_saving = False                # s: a report is being saved, on a thread
        self.rep_drawn: tuple = ()             # (what it was made from, its colored lines)
        try:
            with open(self.prefs_path) as fh:
                d = json.load(fh)
            if d.get("series") in cfgmod.SERIES:
                self.series = d["series"]
            if d.get("chart") in CHARTS:
                self.chart = d["chart"]
            if isinstance(d.get("variant"), str):
                self.variant = d["variant"]
            if d.get("log_filter") in LOG_FILTERS:
                self.log_filter = d["log_filter"]
            if d.get("tab") in BOXES:
                self.tab = d["tab"]
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
                    self.raw = json.load(fh)
                    self.st_stamp = stamp
        except (OSError, json.JSONDecodeError):
            pass
        self.apply_selected()
        for cid in list(self.pending):
            res = (self.st.get("results") or {}).get(cid)
            if res:
                self.say(res); self.pending.pop(cid)
                if self.var_new and self.var_new["cid"] == cid:
                    self.var_new = None                  # refused: the draft stays open
            elif time.time() - self.pending[cid] > 15:
                self.say("no answer from the engine (is it running? press P)"); self.pending.pop(cid)
                if self.var_new and self.var_new["cid"] == cid:
                    self.var_new = None

    def apply_selected(self) -> None:
        """self.st from self.raw, with the selected variant's record in it. One that is gone (deleted) or was never
        picked: the one live trades, else the first."""
        raw = self.raw
        names = [v["name"] for v in raw.get("variants") or []]
        if names and self.variant not in names:
            was, live = self.variant, (raw.get("live_cfg") or {}).get("variant")
            self.variant = live if live in names else names[0]
            self.st = selected_overlay(raw, self.variant)
            if was is not None:
                self.say(f"{was} is gone: {self.vn(self.variant)} is selected")
            self.save_prefs()
        self.st = selected_overlay(raw, self.variant)
        if self.var_new:
            vn = self.var_new                        # a draft was saved: highlight the new variant, close its box
            new = next((v["name"] for v in self.st.get("variants") or [] if v.get("by") == vn["cid"]), None)
            if new:
                self.var_new, self.var_sel = None, new
                if (self.edit_var, self.draft) == (vn["variant"], vn["draft"]):
                    self.close_settings()            # (not one opened since, or changed since)
        if self.edit_var and self.st.get("variants") and self.vrow(self.edit_var) is None:
            self.say("that variant is gone" + (": the changes were not saved" if self.draft else ""))
            self.close_settings()                    # deleted meanwhile
            if (self.modal or {}).get("type") == "vname":
                self.modal = None

    def write_view(self) -> None:
        tmp = os.path.join(self.fast, "view.json.tmp")
        with open(tmp, "w") as fh:
            json.dump({"series": self.series, "ticker": self.sel}, fh)
        os.replace(tmp, os.path.join(self.fast, "view.json"))

    def save_prefs(self) -> None:
        try:
            with open(self.prefs_path + ".tmp", "w") as fh:
                json.dump({"series": self.series, "chart": self.chart, "theme": self.P.name,
                           "variant": self.variant, "log_filter": self.log_filter, "tab": self.tab}, fh)
            os.replace(self.prefs_path + ".tmp", self.prefs_path)
        except OSError:
            pass

    def show_tab(self, name: str) -> None:
        """1-6, tab: that box fills the screen."""
        self.tab, self.flash = name, ""
        self.save_prefs()

    def back_to(self, what: str) -> str:
        """How to get to the variants when they aren't on screen."""
        return f"{BOXES.index('variants') + 1} shows them, {what}"

    def shown(self, name: str) -> bool:
        return name == self.tab

    def send(self, **cmd) -> str:
        """Put a command where the engine picks it up; returns its id (its result comes back under it)."""
        cid = uuid.uuid4().hex[:12]
        cmd["id"] = cid
        cmd["ts"] = time.time()                      # the desk ignores commands older than a few seconds
        path = os.path.join(self.cmd_dir, f"{time.time():.6f}-{cid}.json")
        with open(path + ".tmp", "w") as fh:
            json.dump(cmd, fh)
        os.replace(path + ".tmp", path)
        self.pending[cid] = time.time()
        return cid

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

    def box(self, y: int, x: int, h: int, w: int, title: str, color: int, right=()) -> None:
        """A rounded box, btop style. One of BOXES has the tabs on its top border, with title at the right end when
        that says more than its name, and `right`, its keys, on its bottom border. Any other box has its title on the
        top border and `right` at that border's right end. `right` holds segments: a plain string (grey info) or a
        (key, rest) hint whose key shows in the theme's key color."""
        P = self.P
        a = P(color)
        saved, self.clip = self.clip, None
        tabbed = self.drawing in BOXES
        focused = self.overlay == "reports" and self.drawing == self.rep_focus
        if focused:
            a |= curses.A_BOLD
        tl, hz, tr, vt, bl, br = "╭─╮│╰╯"
        self.put(y, x, tl + hz * (w - 2) + tr, a)
        for r in range(1, h - 1):
            self.put(y + r, x, vt, a)
            self.put(y + r, x + w - 1, vt, a)
        self.put(y + h - 1, x, bl + hz * (w - 2) + br, a)
        cx = x + 3
        if tabbed:
            for i, name in enumerate(BOXES):
                cx = self.tab_seg(y, cx, i, name, color, a)
            if title != self.drawing:
                info = title[:max(0, x + w - 5 - cx)]
                self.put(y, x + w - 4 - len(info), info, P(WHITE, bold=True))
        elif focused:   # title in the box's own color with a marker; the border is drawn bold
            self.put(y, cx, title + " ◂", P(color, bold=True)); cx += len(title) + 2
        else:
            self.put(y, cx, title, P(WHITE, bold=True)); cx += len(title)
        hy, start = (y + h - 1, x + 2) if tabbed else (y, cx)
        segs = [right] if isinstance(right, str) else list(right)
        segs = [s for s in segs if s]
        lens = [len(s) if isinstance(s, str) else sum(map(len, s)) for s in segs]
        fits = lambda: start + 2 + sum(n + 3 for n in lens) <= x + w - 3   # each segment: ┤..├ and a ─ before it
        for spare in DROP_FIRST:                         # the least needed go first: ↑↓, then space on/off
            if not fits():
                i = next((j for j, g in enumerate(segs) if g == spare or isinstance(g, tuple) and g[0] == spare), None)
                if i is not None:
                    segs.pop(i); lens.pop(i)
        while segs and not fits():
            segs.pop(0); lens.pop(0)
        rx = x + w - 3 - sum(n + 3 for n in lens) + 1        # keep the corner free for a scroll ▲▼
        free = 2 + len(f" {self.flash} ") + 1 if tabbed and self.flashing() else 0   # a flash covers the keys it reaches
        for seg, n in zip(segs, lens):
            if rx >= free:
                self.hint(hy, rx, seg, a)
            rx += n + 3
        self.clip = saved

    def tab_seg(self, y: int, x: int, i: int, name: str, color: int, border: int) -> int:
        """One box's tab on the top border; returns where the next one goes."""
        P = self.P
        if name == self.drawing:
            text = f"┤{SUPER[i]}{name}├"
            self.put(y, x, text, P(color, bold=True)); return x + len(text)
        self.put(y, x, "─", border)
        self.put(y, x + 1, SUPER[i], P(P.c("key"), bold=True))
        self.put(y, x + 2, name, P(P.grey))
        self.put(y, x + 2 + len(name), "─", border)
        return x + 3 + len(name)

    def flashing(self) -> bool:
        return bool(self.flash) and time.time() - self.flash_at < 8

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

    def vn(self, name: str | None) -> str:
        """A variant's display name (r renames it); its record stays under its own name."""
        if not name:
            return "?"
        mine = next((v for v in self.raw.get("variants") or [] if v["name"] == name), {})
        return mine.get("title") or (self.raw.get("variant_names") or {}).get(name, name)

    def vrow(self, name: str | None) -> dict | None:
        """The engine's row of a variant."""
        return next((v for v in self.raw.get("variants") or [] if v["name"] == name), None)

    def live_variant(self) -> str | None:
        """The variant live trades (or would, once armed)."""
        return (self.raw.get("live_cfg") or {}).get("variant") or None

    def strat(self) -> dict | None:
        """The selected variant's settings: the screen marks the ladder and band with them."""
        return self.st.get("strategy")

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
        """The status line, on the screen's last row (btop keeps its boxes up top). It is drawn in the fullest
        wording that fits the width, then in shorter ones, so no part runs into the next or off the screen."""
        need = lambda: sum(len(t) for _, seg in left for t, _ in seg) + 2 * len(left) + sum(len(t) for t, _ in right)
        for lvl in range(4):
            left, right = self.status_parts(lvl)
            if need() <= W:
                break
        for drop in ("variant", "price"):             # still too long: the book box says which variant; then the price
            if need() > W:
                left = [(tag, seg) for tag, seg in left if tag != drop]
        hy, x = H - 1, 0
        for _, seg in left:
            for text, attr in seg:
                self.put(hy, x, text[:max(0, W - x)], attr); x += len(text)
            x += 1 if seg[0][1] & curses.A_REVERSE else 2   # pills carry their own padding
        rw = sum(len(t) for t, _ in right)
        rx = max(x, W - rw)
        for text, attr in right:
            self.put(hy, rx, text[:max(0, W - rx)], attr); rx += len(text)

    def status_parts(self, lvl: int) -> tuple[list, list]:
        """The status line's segments at wording level lvl (0 fullest, 3 shortest): (left segments, right
        pieces). A segment is (tag, list of (text, attr)); the tag says what header may drop."""
        P, st = self.P, self.st
        now = time.time()
        hb = st.get("heartbeat") or 0
        ok = now - hb < 5 and not st.get("stopped")
        # no process holding the lock is "down", however it ended; a live one that stopped polling is "stale"
        down = not ok and not self.alive
        mode = st.get("mode", "?")
        grey = self.P.grey
        left: list = [("tern", [(" tern ", P(CYAN, bold=True) | curses.A_REVERSE)])]
        if mode == "live":
            left.append(("mode", [(" ● LIVE " if lvl < 2 else " LIVE ", P(RED, bold=True) | curses.A_REVERSE)]))
        else:
            left.append(("mode", [(" ● PAPER " if lvl < 2 else " PAPER ", P(GREEN, bold=True) | curses.A_REVERSE)]))
            if st.get("live_enabled"):
                armed = "(live armed: " + (st.get("live_error") or "authenticating") + ")"
                left.append(("armed", [(armed[:60] if lvl < 2 else "(live armed)", P(YELLOW))]))
        if self.variant:
            left.append(("variant", [(f" {self.vn(self.variant)} ", P(P.c("variant"), bold=True) | curses.A_REVERSE)]))
        prod = cfgmod.SERIES[self.series]
        sp = (st.get("spot") or {}).get(prod) or {}
        p = sp.get("now")
        mins = sp.get("min") or []
        chg = p / mins[-61][1] - 1 if p and len(mins) > 60 else None
        sym = prod.split('-')[0]
        price = [(sym + " ", P(P.c("sym"), bold=True)),
                 (money(p) if lvl < 2 or p is None else f"${p:,.0f}" if p >= 1000 else money(p), P(WHITE, bold=True))]
        if chg is not None:
            price.append((f" {'▲' if chg >= 0 else '▼'}{chg:+.2%}", P(GREEN if chg >= 0 else RED, bold=True)))
            if lvl < 1:
                price.append((" 1h", P(P.c("unit"))))
        left.append(("price", price))
        ev = (st.get("events") or {}).get(self.series)
        if ev:
            tau = ev["close"] - now
            seg = [(ev['event'], P(P.c("event"), bold=True)), (" closes ", P(P.c("word_a")))] if lvl < 1 else \
                [("closes ", P(P.c("word_a")))]
            left.append(("event", seg + [(clock(tau), P(YELLOW if tau < 600 else WHITE, bold=True))]))
        S = self.strat() or {}
        if S:
            on = self.series in (S.get("series") or [])
            seg = [("rule ", P(P.c("word_b"))), ("ON" if on else "off", P(GREEN if on else RED, bold=True))]
            if ev and on:
                tau = ev["close"] - now
                ahead = [m for m in S.get("minutes", []) if m * 60 < tau]
                if ahead:
                    seg += ([(" next ", P(P.c("word_c"))), (f"{max(ahead)}m", P(P.c("key"), bold=True)),
                             (" slot in ", P(P.c("word_c")))] if lvl < 2 else
                            [(" · ", P(grey)), (f"{max(ahead)}m", P(P.c("key"), bold=True)), (" in ", P(P.c("word_c")))]) \
                           + [(clock(tau - max(ahead) * 60), P(WHITE))]
            if st.get('auto_live'):
                if self.live_variant() == self.variant:
                    seg += [(" +live", P(RED, bold=True))] if on else []
                else:
                    seg.append((f" live: {self.vn(self.live_variant())}", P(RED, bold=True)))
            left.append(("rule", seg))
        # the engine can be up while its feeds are not (Wi-Fi drops): say how old the data is
        stale = []
        sec = sp.get("sec") or []
        if ok and sec and now - sec[-1][0] > 10:
            stale.append(f"spot {ago(now - sec[-1][0])}" + (" old" if lvl < 2 else ""))
        if ok and ev and ev.get("age", 0) > 30:
            stale.append(f"ladder {ago(ev['age'])}" + (" old" if lvl < 2 else ""))
        if stale:
            left.append(("stale", [(" · ".join(stale) + ("" if lvl < 2 else " old"), P(YELLOW, bold=True))]))
        state = "ok" if ok else ("down — P starts it" if down else "starting" if st.get("stopped") or not hb else "stale")
        if lvl >= 2 and down:
            state = "down (P)"
        word = "engine " if lvl < 3 else ""
        clk = time.strftime("%H:%M:%S") if lvl < 1 else time.strftime("%H:%M") if lvl < 3 else ""
        right = [(f" {'●' if ok else '○'} {word}{state}" + (f"  {clk} " if clk else " "), P(GREEN if ok else RED))]
        return left, right

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
        self.box(y, x, h, w, prod.lower(), self.P.border("spot"), PANEL_KEYS["spot"](self))
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
        self.box(y, x, h, w, title, self.P.border("contract"), PANEL_KEYS["contract"](self))
        if not r:
            self.put(y + 2, x + 3, "waiting for the hourly event…", P(self.P.grey)); return
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
        who = self.vn(self.variant) if self.variant else "the rule"
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

    def pick_strike(self) -> None:
        """A strike for the spot, contract and ladder boxes when none is picked, or the one picked is gone."""
        lad = self.ladder()
        if not lad or self.sel in [r["t"] for r in lad]:
            return
        spot = ((self.st.get("spot") or {}).get(cfgmod.SERIES[self.series]) or {}).get("now")
        if spot:       # the nearest strike the rule would buy (★), else the nearest one
            pool = [r for r in lad if self.in_band(r)] or lad
            self.sel = min(pool, key=lambda r: abs(r["k"] - spot))["t"]
        else:
            self.sel = lad[len(lad) // 2]["t"]
        self.write_view()

    def ladder_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        lad = self.ladder()
        self.box(y, x, h, w, "ladder", self.P.border("ladder"), PANEL_KEYS["ladder"](self))
        if not lad:
            self.put(y + 2, x + 3, "waiting for the hourly event…", P(self.P.grey)); return
        spot = ((st.get("spot") or {}).get(cfgmod.SERIES[self.series]) or {}).get("now")
        i = [r["t"] for r in lad].index(self.sel)
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
        self.box(y, x, h, w, "variants", self.P.border("ladder"), PANEL_KEYS["variants"](self))
        if not vs:
            self.put(y + 2, x + 3, "waiting for the engine…", P(self.P.grey)); return
        names = [v["name"] for v in vs]
        if self.var_sel not in names:
            self.var_sel = self.variant if self.variant in names else names[0]
        head = P(self.P.c("head"), bold=True)
        nw = max([8] + [len(self.vn(n)) for n in names])   # the name column: as wide as the longest name
        sh = nw - 7                                         # ... and every column after it moves along
        hdr = f"   {'variant':<{nw}} {'hours':>5} {'entries':>7} {'P&L':>9} {'¢/ct':>6} {'win':>4} {'t':>6} gate"
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
            if v["name"] == self.variant:                # selected; live's; one made here (the rest are built in)
                self.put(yy, x + 2, "●", P(GREEN, bold=True) | base)
            if v.get("live") and st.get("live_enabled"):
                self.put(yy, x + 3, "L", P(RED, bold=True) | base)
            if v.get("own"):
                self.put(yy, x + 4, "+", P(P.c("key"), bold=True) | base)
            on = v.get("on", True)
            self.put(yy, x + 5, f"{self.vn(v['name']):<{nw}}", P(P.c("variant") if on else P.grey, bold=on) | base)
            pc = f"{v['per_contract']*100:+.2f}" if v.get("per_contract") is not None else "-"
            win = f"{v['win']:.0%}" if v.get("win") is not None else "-"
            t = f"{v['t']:+.2f}" if v.get("t") is not None else "-"
            self.put(yy, x + 13 + sh, f"{v['hours']:>5} {v['entries']:>7} ", row)
            pnl_c = P(GREEN if v["pnl"] >= 0 else RED, bold=True) | base
            self.put(yy, x + 28 + sh, f"{v['pnl']:>+9.2f}", pnl_c)
            self.put(yy, x + 38 + sh, f"{pc:>6} {win:>4} ", row)
            tc = self.P.grey if v.get("t") is None else GREEN if v["t"] >= 2 else RED if v["t"] <= -2 else WHITE
            self.put(yy, x + 50 + sh, f"{t:>6}", P(tc, bold=v.get("t") is not None and abs(v["t"]) >= 2) | base)
            cx = x + 57 + sh
            g = v.get("gate") or {}
            if not on:
                self.put(yy, cx, "off", P(RED, bold=True) | base)
            elif g.get("passed"):
                self.put(yy, cx, "✓ passed", P(GREEN, bold=True) | base)
            else:
                for c in g.get("checks", []):          # a passed check is a word; one still short shows how far
                    have = c["have"]
                    txt = ({"days": "days", "entries": "ent", "pnl": "P&L"}[c["key"]] if c["ok"] else
                           f"{have:.1f}/{c['need']:g}d" if c["key"] == "days" else
                           f"{have}/{c['need']}" if c["key"] == "entries" else
                           f"P&L<{'-' if c['need'] < 0 else ''}${abs(c['need']):g}")
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
            nm = cur["name"] if self.vn(cur["name"]) == cur["name"] else f"{self.vn(cur['name'])} ({cur['name']})"
            nm = "+ " + nm if cur.get("own") else nm
            self.put(yy, x + 2, f"{nm}: ", P(P.c("variant"), bold=True))
            self.put(yy, x + 4 + len(nm), (cur.get("desc") or "")[:w - 8 - len(nm)], P(WHITE))
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
        # The bottom row says what the highlighted variant is doing. On a short box the row is the last variant's.
        if y + 1 + rows < y + h - 2:
            name, sel_, live = self.vn(cur["name"]), cur["name"] == self.variant, cur.get("live") and st.get("live_enabled")
            tip, col = "", GREEN
            if not cur.get("on", True):
                tip, col = f"{name} is off: no new entries, open ones settle · space turns it on", YELLOW
            elif cur.get("cash") is not None and cur["cash"] < (cur.get("rule") or {}).get("band_lo", 0) and not cur.get("open"):
                tip, col = f"{name} has {money(cur['cash'])} left: it buys nothing · e, s makes a fresh one", YELLOW
            elif live:
                tip, col = f"L live trades {name}" + (" · selected" if sel_ else ""), RED
            elif sel_:
                tip = f"● {name} is selected: the book shows it · L goes live with it" + \
                    ("" if (cur.get("gate") or {}).get("passed") else ", once it passes the gate")
            else:
                tip, col = f"enter selects {name} · changes nothing that trades", P.grey
            self.put(y + h - 2, x + 2, tip[:w - 4], P(col, bold=col != P.grey))

    def select_variant(self) -> None:
        """enter: the highlighted variant is the one the book, band and gate show. Nothing that trades changes."""
        if not self.shown("variants"):
            self.say("enter selects a variant: " + self.back_to("highlight one, then enter")); return
        cur = self.vrow(self.var_sel)
        if cur is None:
            return
        if cur["name"] == self.variant:
            self.say(f"{self.vn(self.variant)} is already selected"); return
        self.variant, self.pos_off = cur["name"], 0
        live = self.live_variant() if self.armed() else None
        self.book_view = "paper" if live and live != self.variant else None   # live's book is another variant's
        self.save_prefs(); self.apply_selected()
        self.say(f"{self.vn(self.variant)} selected: the book shows it" + (f" · live still trades {self.vn(live)}" if live and live != self.variant else ""))

    # ---- the variants: e, D, space, r
    def variant_key(self, ch: int) -> None:
        """D or space with the variants box on screen, on the highlighted variant."""
        cur = self.vrow(self.var_sel)
        if cur is None:
            self.say("no variants from the engine yet"); return
        if self.older_engine():
            return
        name = self.vn(cur["name"])
        livs = self.armed() and cur["name"] == self.live_variant()
        if ch == ord(" "):
            on = not cur.get("on", True)
            cmd = {"op": "variant_on", "variant": cur["name"], "on": on}
            if on and livs:
                self.modal = {"type": "livecmd", "title": "LIVE IS ARMED", "color": RED, "buf": "", "cmd": cmd, "kept": f"{name} kept off",
                              "lines": [f"Live is armed with {name}: turned on, real money trades it from its next slot,",
                                        "past its gate.", "", "type live and press enter (esc cancels):", "> "]}
            else:
                self.send(**cmd)
        elif not cur["own"]:
            self.say(f"{name} is built in: it can't be deleted (space turns it off)")
        elif livs:
            self.say(f"{name} is what live trades: turn live off or move it first (L)")
        else:
            kept = "Its record stays on file (tern report lists it)." if cur["entries"] or cur["open"] else "It has not bought anything."
            self.modal = {"type": "vdelete", "title": f"delete {name}", "color": RED, "name": cur["name"],
                          "lines": [f"Delete {name}? It stops running. {kept}", "", "y deletes · any other key keeps it"]}

    def edit_variant(self) -> None:
        """e: a draft of the highlighted one's settings. s saves it as a new variant; that one stays."""
        if self.vrow(self.var_sel) is None:
            self.say("no variants from the engine yet"); return
        if not self.older_engine():
            self.open_settings(self.var_sel)

    def live_key(self) -> None:
        """L: live on with the selected variant, off, or (armed with another) moved to the selected one."""
        row = self.vrow(self.variant)
        if row is None:
            self.say("no variants from the engine yet"); return
        if self.older_engine():
            return
        st, name, live = self.st, self.vn(self.variant), self.live_variant()
        if st.get("live_enabled") and live == self.variant:
            self.send(op="live", on=False); self.say("disarming live"); return
        L = st.get("live_cfg") or {}
        gate = f"{name} has passed its gate (g)." if (row.get("gate") or {}).get("passed") else \
            f"{name} has not passed its gate (g): live waits until it does." if L.get("auto_needs_gate", True) else \
            f"{name} has not passed its gate (g), and live.auto_needs_gate is off: it trades anyway."
        off = [] if row.get("on", True) else [f"{name} is off: live enters nothing until space turns it on."]
        if st.get("live_enabled"):
            self.modal = {"type": "arm", "title": "MOVE LIVE", "color": RED, "buf": "", "variant": self.variant,
                          "lines": [f"Live trades {self.vn(live)}. Move it to {name}? Real money would trade {name} instead.",
                                    gate, *off, "Open live positions ride to settlement. Live's caps and size stay as they are.",
                                    "", "type live to move it, off to turn live off (esc cancels):", "> "]}
            return
        self.modal = {"type": "arm", "title": "ARM LIVE", "color": RED, "buf": "", "variant": self.variant,
                      "lines": [f"Live trading uses real money on Kalshi. It would trade {name}.", gate, *off,
                                f"Caps: ${L.get('capital_usd', 0):,.0f} at risk, ${L.get('max_order_usd', 0):,.0f} per order,",
                                f"live turns itself off after -${L.get('max_daily_loss', 0):,.0f} in a day.",
                                *([] if st.get("keys") else ["", f"No key found yet: live stays off until {cfgmod.KEYFILE_ENV} exists."]),
                                "", "type live and press enter (esc cancels):", "> "]}

    def open_settings(self, variant: str | None = None) -> None:
        """The settings box (the desk's and the screen's), or the same box on a variant's settings, as a draft."""
        self.overlay, self.set_buf, self.edit_var, self.draft = "settings", None, variant, {}
        self.set_sub = 0
        if variant is not None:
            self.set_back, self.set_sel = self.set_sel, 0

    def close_settings(self) -> None:
        self.overlay, self.set_buf = None, None
        if self.edit_var:
            self.edit_var, self.draft = None, {}
            self.set_sel, self.set_back = self.set_back or 0, None     # back where e's box was left

    def rule_now(self, field: str):
        """The variant the draft starts from, as it is saved: one of its settings (None until the engine has said)."""
        return ((self.vrow(self.edit_var) or {}).get("rule") or {}).get(field)

    def draft_lines(self) -> list[str]:
        """What the draft changes, a line each."""
        return [f"{r.label}  {self.set_fmt(r, self.rule_now(r.name))} → {self.set_fmt(r, self.draft[r.name])}"
                for r in varmod.RULE_SETTINGS if r.name in self.draft]

    def save_draft(self) -> None:
        """s in a variant's box: asks for a name, then the draft becomes a new variant."""
        if self.var_new:
            self.say("the last save has not been answered yet"); return
        if self.older_engine():
            return
        self.modal = {"type": "vname", "title": "save as a new variant", "color": CYAN, "buf": "", "max": varmod.NAME_MAX,
                      "cmd": {"op": "variant_new", "from": self.edit_var, "rule": dict(self.draft)},
                      "lines": [f"A new variant: {self.vn(self.edit_var)} with",
                                *("  " + t for t in self.draft_lines() or ["the same settings"]), "",
                                f"It starts with an empty record. {self.vn(self.edit_var)} and its record stay as they are.",
                                f"name, up to {varmod.NAME_MAX} characters (enter alone picks one, esc goes back):", "> "]}

    def older_engine(self) -> bool:
        """Whether the engine is from before there was one list: it would do something else with these commands."""
        rows = self.raw.get("variants") or []
        if not rows or all("on" in v for v in rows):
            return False
        self.say("the engine running is older than this screen: restart it (P, P)")
        return True

    def armed(self) -> bool:
        return bool(self.raw.get("live_enabled") or self.raw.get("mode") == "live")

    def rename_ask(self) -> None:
        """r: rename the highlighted variant."""
        v = self.var_sel
        if not self.shown("variants") or self.vrow(v) is None:
            self.say("r renames a variant: " + self.back_to("highlight one, then r")); return
        name, mx = self.vn(v), namesmod.VARIANT_NAME_MAX
        self.modal = {"type": "rename", "title": f"rename {name}", "color": CYAN, "buf": name, "max": mx,
                      "target": {"variant": v},
                      "lines": ["Only what the screen calls it changes: its record, the gate and tern report",
                                f"keep it as {v}.", f"enter on an empty line names it {v} again", "",
                                f"new variant name, up to {mx} characters (ctrl+u clears, esc keeps it):", "> " + name]}

    def cur_book(self):
        v = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
        return self.st.get(v)

    def book_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        v = self.book_view or ("live" if st.get("mode") == "live" else "paper")
        b = st.get(v) or {}
        self.box(y, x, h, w, "book", self.P.border("book"), PANEL_KEYS["book"](self))
        tabs: list[tuple[str, int]] = []
        for name in ("paper", "live"):
            label = f"{self.vn(self.variant)} on paper" if name == "paper" else \
                f"live · {self.vn(self.live_variant())}" if st.get("live_enabled") and self.live_variant() else "live"
            tabs += [(f" {label} ", P(WHITE, bold=True) | curses.A_REVERSE if name == v else P(self.P.grey)), (" ", 0)]
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
            if g:
                wr = f"{g['win']:.0%}" if g.get("win") is not None else "-"
                c = GREEN if g["pnl"] >= 0 else RED
                a1 = f"{g['entries']} settled · {g['open']} open · {money(g['pnl'], True)}"
                a2 = f"win {wr} · {g['per_contract']*100:+.2f}¢/ct"
                if len(a1) + len(a2) + 3 <= w - 13:
                    lines.append(("record", f"{a1} · {a2}", c))
                else:                                    # a narrow book: two lines, not one cut short
                    lines += [("record", a1, c), ("", a2, c)]
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
            return f"{strike_label(float(m.group(1))) if m else '?':>8} "

        out: list[list[tuple[str, int]]] = []
        pos = b.get("pos") or []
        out.append([(f"{'open':<4} {'strike':>8} {'side':<4} {'qty':>4} {'avg':>5} {'mark':>5} {'P&L':>8} {'closes':>6}", head)])
        for p in pos:
            pl = p.get("pnl")
            out.append([(f"{'':<5}{strike_label(p.get('k')):>8} ", white), *side(p["q"]),
                        (f" {abs(p['q']):>4g} {cents(p['avg']):>5} {cents(p.get('mark')):>5} ", white),
                        (f"{money(pl, True):>8}", P(GREEN if (pl or 0) >= 0 else RED)),
                        (f" {clock((p.get('close') or now) - now):>6}", grey)])
        if not pos:
            out.append([("     no open positions", grey)])

        trades = list(reversed(b.get("trades") or []))
        settled = [t for t in trades if t.get("kind") == "settle"]
        out.append([("", 0)])
        out.append([(f"{'settled':<8}{'strike':>6} {'side':<4} {'qty':>4} {'won':>5} {'P&L':>8} {'ago':>6}", head)])
        for t in settled:
            q = -t["delta"]
            won = (t.get("result") == "yes") == (q > 0)
            word, wc = ("void", grey) if t.get("result") == "void" else ("won", P(GREEN)) if won else ("lost", P(RED))
            out.append([(f"{'':<5}{strike(t['ticker'])}", white), *side(q),
                        (f" {abs(q):>4g} ", white), (f"{word:>5} ", wc),
                        (f"{money(t['realized'], True):>8}", P(GREEN if t["realized"] >= 0 else RED)),
                        (f" {ago(now - t['ts']):>6}", grey)])
        if not settled:
            out.append([("     none yet", grey)])

        fills = [t for t in trades if t.get("kind") == "fill"]
        out.append([("", 0)])
        out.append([(f"{'fills':<8}{'strike':>6} {'side':<4} {'qty':>4} {'at':>5} {'fee':>5} {'P&L':>8} {'ago':>6}", head)])
        for t in fills:
            q = t["delta"]                                   # + bought YES, - bought NO at 1 - price
            at = t["price"] if q > 0 else 1 - t["price"]
            out.append([(f"{'':<5}{strike(t['ticker'])}", white), *side(q),
                        (f" {abs(q):>4g} {cents(at):>5} {money(t['fee']):>5} ", white),
                        (f"{money(t['realized'], True):>8}", P(GREEN if t["realized"] >= 0 else RED)),
                        (f" {ago(now - t['ts']):>6}", grey)])
        if not fills:
            out.append([("     none yet", grey)])
        return out

    def log_panel(self, y, x, h, w) -> None:
        P = self.P
        self.box(y, x, h, w, "log", self.P.border("log"), PANEL_KEYS["log"](self))
        logs = self.log_rows(self.logs_shown(), w)
        rows = h - 2
        end = self.log_end(logs, rows)
        saved, self.clip = self.clip, x + w - 2
        shown = logs[max(0, end - rows):end]
        for j, (ts, lvl, msg, sub, cells, indent) in enumerate(shown):
            yy = y + 1 + j
            age = (len(shown) - 1 - j) / max(1, rows - 1)          # 0 for the bottom (newest) line
            if cells:                                              # a shadow line's columns: name and P&L
                cx = x + 11 + indent
                for name, pnl in cells:
                    self.put(yy, cx, name, P(P.c("variant"), bold=True))
                    self.put(yy, cx + len(name), pnl, P({"+": GREEN, "-": RED}.get(pnl.lstrip()[0], WHITE), bold=True))
                    cx += len(name) + len(pnl) + 3
            if sub:                                                # a continuation row: no time, no text
                continue
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

    def log_rows(self, logs: list, w: int) -> list[tuple]:
        """The log box's screen rows, (ts, level, text, sub, cells, indent). A shadow settlement line ("shadow: EV
        settled · base +1.20 · band90 -0.40 ..."), or a slot's ("paper: 25m slot on EV bought · base 5 ..."), becomes
        columns of (name, P&L or entries) cells, 3 a row at most (fewer
        if the box is narrow): the first row after "settled", the rest under it, each variant under the one
        above (sub 1, 2... and no text). Every other line is one row: sub 0, no cells."""
        out = []
        for ts, lvl, msg in logs:
            m = SHADOW_LINE.match(msg)
            cells = [SHADOW_CELL.match(c) for c in m.group(2).split(" · ")] if m else []
            if not m or not all(cells):
                out.append((ts, lvl, msg, 0, None, 0)); continue
            head = m.group(1)
            indent = len(head) + 2                                 # the cells start two spaces after "settled"
            names = [self.vn(c.group(1)) for c in cells]
            pnls = [c.group(2) for c in cells]
            nw, pw = max(map(len, names)) + 1, max(map(len, pnls))
            per = max(1, min(LOG_GRID_COLS, (w - 13 - indent + 3) // (nw + pw + 3)))   # 3 spaces between
            grid = [(f"{n:<{nw}}", f"{v:>{pw}}") for n, v in zip(names, pnls)]
            for k in range(0, len(grid), per):
                out.append((ts, lvl, head if k == 0 else "", k // per, grid[k:k + per], indent))
        return out

    def log_end(self, logs: list, rows: int) -> int:
        """One past the bottom log row shown: the newest, or the row scrolled back to ((ts, sub) of it)."""
        if self.log_anchor is None:
            return len(logs)
        end = 0
        for k, e in enumerate(logs):
            if (e[0], e[3]) <= self.log_anchor:
                end = k + 1
        return max(end, min(rows, len(logs)))

    def wheel(self, my: int, mx: int, d: int) -> None:
        """Mouse wheel over a panel: d is -1 (up) or +1 (down)."""
        name = next((n for n, (y, x, h, w) in self.rects.items() if y <= my < y + h and x <= mx < x + w), None)
        self.scroll(name, 3 * d if name == "report" else d)

    def scroll(self, name: str | None, d: int) -> None:
        """Move one box d steps (negative is up). Boxes that don't scroll ignore it."""
        if name in ("ladder", "contract"):
            self.step_ladder(d)
        elif name == "variants":
            names = [v["name"] for v in self.st.get("variants") or []]
            if names:
                i = names.index(self.var_sel) if self.var_sel in names else 0
                self.var_sel = names[max(0, min(len(names) - 1, i + d))]
        elif name == "book":
            self.pos_off = max(0, self.pos_off + d)    # book_panel clamps the far end
        elif name == "reports":
            ids = [r.id for r in self.rep_entries()]
            self.pick_report(ids[max(0, min(len(ids) - 1, ids.index(self.rep_sel) + d))])
        elif name == "report":
            self.rep_off = max(0, min(self.rep_len - self.rep_room, self.rep_off + d))
        elif name == "log":
            r = self.rects.get("log")
            logs = self.log_rows(self.logs_shown(), r[3] if r else 100)
            rows = r[2] - 2 if r else 1
            end = max(min(rows, len(logs)), min(len(logs), self.log_end(logs, rows) + 3 * d))
            self.log_anchor = None if end >= len(logs) else (logs[end - 1][0], logs[end - 1][3])

    def step_ladder(self, d: int) -> None:
        tickers = [r["t"] for r in self.ladder()]
        if not tickers:
            return
        i = tickers.index(self.sel) if self.sel in tickers else len(tickers) // 2
        self.sel = tickers[max(0, min(len(tickers) - 1, i + d))]
        self.write_view()

    def footer(self, H: int, W: int) -> None:
        """A flash message, on the box's bottom border (row H-2)."""
        if self.flashing():
            msg = f" {self.flash} "[:max(0, W - 4)]
            self.put(H - 2, 2, msg, self.P(YELLOW, bold=True) | curses.A_REVERSE)

    def draw_overlay(self, lines: list, title: str, color: int = CYAN, scroll: bool = False, hints=None,
                     pin: int = 0, follow: int | None = None) -> None:
        """A centered box of lines. A line is a plain string (colored by `rich`) or a list of (text, attr).
        With scroll, lines that don't fit scroll by self.ov_off (help and gate); ▲▼ and the place show on the border.
        pin keeps the last pin lines in place (settings: the divider and what the row is) while the lines above
        them scroll just enough to show line `follow` (the cursor's)."""
        H, W = self.s.getmaxyx()
        lines = [self.rich(l) if isinstance(l, str) else l for l in lines]
        w = min(W - 4, max(sum(len(t) for t, _ in l) for l in lines) + 4)
        h = min(H - 2, len(lines) + 2)
        y, x = (H - h) // 2, (W - w) // 2
        room = h - 2
        off = 0
        if pin and len(lines) > room and room > pin:
            body, foot = lines[:-pin], lines[-pin:]
            br = room - pin
            off = self.pin_off = max(0, min(getattr(self, "pin_off", 0), len(body) - br))
            if follow is not None:
                off = self.pin_off = min(max(off, follow - br + 1), follow)
            for r in range(h):
                self.put(y + r, x, " " * w)
            self.box(y, x, h, w, title, color, hints or [("any key", " closes")])
            saved, self.clip = self.clip, x + w - 2
            for j, l in enumerate(body[off:off + br] + foot):
                cx = x + 2
                for text, attr in l:
                    if text == RULE_LINE:
                        text = "─" * (w - 4)
                    self.put(y + 1 + j, cx, text, attr); cx += len(text)
            self.clip = saved
            if off > 0:
                self.put(y + 1, x + w - 2, "▲", self.P(YELLOW))
            if off + br < len(body):
                self.put(y + br, x + w - 2, "▼", self.P(YELLOW))
            return
        if scroll:
            self.ov_room, self.ov_len = room, len(lines)
            self.ov_off = off = max(0, min(self.ov_off, len(lines) - room))
        for r in range(h):
            self.put(y + r, x, " " * w)
        long = scroll and len(lines) > room
        self.box(y, x, h, w, title, color, hints or ([("↑↓", " scroll"), ("esc", " close")] if long else [("any key", " closes")]))
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
        caps = (f", {S['cap']:g} a strike an hour" if S.get("cap") else "") + \
            (f", no slot after an hour over {S['max_vol']:g} bp a minute" if S.get("max_vol") else "")
        fill = dict(
            variant=self.vn(S["variant"]) if S.get("variant") else "base", caps=caps, max_own=varmod.MAX_OWN,
            rule_coins=" ".join(cfgmod.SERIES.get(c, c).split("-")[0] for c in S.get("series") or []),
            minutes=", ".join(mins[:-1]) + " and " + mins[-1] if len(mins) > 1 else "".join(mins),
            band=f"{S['band_lo']*100:.0f}-{S['band_hi']*100:.0f}c", spread=f"{S['max_spread']*100:g}",
            contracts=S["contracts"], hour_cost=S["max_hour_cost"],
            days=G["min_days"], entries=G["min_entries"], pnl=G["min_pnl"],
            risk=L["capital_usd"], order=L["max_order_usd"], day=L["max_daily_loss"],
            coins=" ".join(v.split("-")[0] for v in cfgmod.SERIES.values()), paper=((self.vrow(self.variant) or {}).get("rule") or {}).get("paper_capital") or cfgmod.Config().paper_capital,
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
        live, lg = self.live_variant(), st.get("live_gate") or {}
        out: list = [head("PAPER CHECKS", f"   {self.vn(self.variant)} trades live only when every one passes"),
                     "  (unless live.auto_needs_gate is false)" +
                     (f" · live {'trades' if st.get('live_enabled') else 'is set to'} {self.vn(live)}" if live and live != self.variant else "")]
        if st.get("live_enabled") and not lg.get("ok", True):
            out.append(f"  [ ] live waits: {(lg.get('checks') or [{}])[0].get('name', '')}")
        for c in g.get("checks", []):
            out.append(f"  [{'x' if c['ok'] else ' '}] {c['name']}  ({c['detail']})")
        if st.get("shadow"):
            rows = shadowmod.table(st["shadow"])
            out += ["", head("VARIANTS", "   side by side on paper; (5) shows them, (enter) selects one (tern report)")]
            out += [[("  " + rows[0], P(P.grey, bold=True))]] + ["  " + l for l in rows[1:]]
        out += ["", head("GO LIVE", "   all three are needed")]
        out.append(f"  [{'x' if st.get('keys') else ' '}] Kalshi key present ({cfgmod.KEYFILE_ENV})")
        out.append(f"  [{'x' if st.get('live_enabled') else ' '}] armed with L" + (f", {self.vn(live)}" if st.get("live_enabled") and live else ""))
        out.append(f"  [{'x' if st.get('mode') == 'live' else ' '}] authenticated" + (f": {st.get('live_error')}" if st.get("live_error") else ""))
        out += ["", head("CAPS"), f"  ${L.get('capital_usd', 0):,.0f} at risk · ${L.get('max_order_usd', 0):,.0f} per order · "
                    f"-${L.get('max_daily_loss', 0):,.0f} a day turns live off"]
        return out

    # ------------------------------------------------------------ reports (o)
    def rep_entries(self) -> list:
        """The reports there are; the one shown goes back to the first when it is gone."""
        if not self.rep_list or not 0 <= time.time() - self.rep_at < 2:
            self.rep_list, self.rep_at = reports.index(self.runtime), time.time()
        if self.rep_sel not in [r.id for r in self.rep_list]:
            self.pick_report(self.rep_list[0].id)
        return self.rep_list

    def pick_report(self, rid: str) -> None:
        if rid != self.rep_sel:
            self.rep_sel, self.rep_off = rid, 0

    def reports_screen(self, H: int, W: int) -> None:
        """Two boxes over the whole screen but the status line: the list, and the report picked in it."""
        P = self.P
        entries = self.rep_entries()
        h, lw = H - 1, REPORT_LIST_W
        # the list: LIVE, then SAVED
        self.drawing = "reports"
        self.rects["reports"] = (0, 0, h, lw)
        self.box(0, 0, h, lw, "reports", P.border("book"), ["↑↓"])
        rows: list = []                                   # a heading, "" or an entry
        for saved, title in ((False, "LIVE"), (True, "SAVED")):
            these = [r for r in entries if r.saved == saved]
            rows += ([""] if rows else []) + [title] + (these or ["  none yet"])
        room = h - 2
        at = next(i for i, r in enumerate(rows) if not isinstance(r, str) and r.id == self.rep_sel)
        self.rep_top = max(0, min(self.rep_top, len(rows) - room))
        self.rep_top = min(max(self.rep_top, at - room + 1), max(0, at - 1))   # the first entry: its heading too
        self.rep_rows = {}
        for j, r in enumerate(rows[self.rep_top:self.rep_top + room]):
            yy = 1 + j
            if isinstance(r, str):
                self.put(yy, 2, r, P(P.c("head"), bold=True) if r.isupper() else P(WHITE))
                continue
            sel = r.id == self.rep_sel
            base = curses.A_REVERSE if sel else 0
            name = r.label if len(r.label) <= lw - 6 else r.label[:lw - 7] + "…"
            self.put(yy, 1, f"   {name}".ljust(lw - 2), P(P.pick("rows", entries.index(r) / max(1, len(entries) - 1)), bold=sel) | base)
            self.rep_rows[yy] = r.id
        if self.rep_top > 0:
            self.put(1, lw - 1, "▲", P(YELLOW))
        if self.rep_top + room < len(rows):
            self.put(h - 2, lw - 1, "▼", P(YELLOW))
        # the report
        cur = next(r for r in entries if r.id == self.rep_sel)
        x, w = lw, W - lw
        self.drawing = "report"
        self.rects["report"] = (0, x, h, w)
        self.box(0, x, h, w, cur.label, P.border("ladder"),
                 ([] if cur.saved else [("s", "ave")]) + [("←→", " box"), ("↑↓", " scroll" if self.rep_focus == "report" else " pick"), ("esc", " close")])
        self.drawing = ""
        got = reports.FEED.get(self.runtime, cur.id)
        if got is None:
            self.rep_room, self.rep_len = room, 0
            self.put(2, x + 3, "reading the records…", P(WHITE)); return
        key = (got, w, P.name, P.many)
        if not self.rep_drawn or self.rep_drawn[0][0] is not got or self.rep_drawn[0][1:] != key[1:]:
            self.rep_drawn = (key, self.report_spans(wrap_report(got.lines, w - 4)))
        lines = self.rep_drawn[1]
        self.rep_room, self.rep_len = room, len(lines)
        self.rep_off = off = max(0, min(self.rep_off, len(lines) - room))
        saved, self.clip = self.clip, x + w - 2
        for j, spans in enumerate(lines[off:off + room]):
            cx = x + 2
            for text, attr in spans:
                self.put(1 + j, cx, text, attr); cx += len(text)
        self.clip = saved
        # the bottom border: where it comes from, and the place in it
        src = f"runtime/{cur.id[len('saved:'):]}" if cur.saved else \
            reports.command(cur.id) + time.strftime(" · worked out %H:%M:%S", time.localtime(got.at))
        self.put(h - 1, x + 3, f" {src} ", P(P.c("ticker")))
        if off > 0:
            self.put(1, x + w - 1, "▲", P(YELLOW))
        if off + room < len(lines):
            self.put(h - 2, x + w - 1, "▼", P(YELLOW))
        if len(lines) > room:
            where = f" {off + 1}-{off + room} of {len(lines)} "
            self.put(h - 1, x + w - 3 - len(where), where, P(P.c("key"), bold=True))

    def report_spans(self, lines: list[str]) -> list[list[tuple[str, int]]]:
        """A report's lines, colored: tables as in the variants box, headings, names, and what rich colors."""
        P = self.P
        out: list = []
        tables = [i for i, l in enumerate(lines) if REPORT_HEAD.match(l)]
        for i, l in enumerate(lines):
            row, named = REPORT_ROW.match(l), REPORT_NAME.match(l)
            if i in tables:
                out.append([(l, P(P.c("head"), bold=True))])
            elif row:                                     # a table's row: the name (and book), then the numbers
                top = max([t for t in tables if t < i], default=i - 1)
                end = next((j for j in range(i, len(lines)) if not REPORT_ROW.match(lines[j])), len(lines))
                num = P(P.pick("rows", (i - top - 1) / max(1, end - top - 2)))
                name, gap, book = re.match(r"(.*?)(\s{2,}|$)(.*)", row[2]).groups()
                out.append([(row[1] + name, P(P.c("variant"), bold=True)), (gap, 0), (book, P(P.c("slot"), bold=True))])
                for part in re.split(r"([+-]\d[\d.]*)", row[3]):
                    out[-1].append((part, P(GREEN if part[0] == "+" else RED, bold=True) if re.match(r"[+-]\d", part) else num))
            elif l.endswith(":") or l == "all settings together":
                out.append([(l, P(P.c("head"), bold=True))])
            elif re.match(r"settings \d+(?:, |: |$)", l):      # settings 2, when: what changed
                m = re.match(r"(settings \d+)(.*)", l)
                out.append([(m[1], P(P.c("head"), bold=True))] + self.log_spans(m[2], "", WHITE))
            elif named and named[2] not in LOG_BOOK:      # base: the rule ...
                body, use, after = named[4].partition("← live")
                out.append([(named[1] + named[2], P(P.c("variant"), bold=True)), (named[3], P(P.c("sep")))] +
                           self.log_spans(body, "", WHITE) + [(use + after, P(GREEN, bold=True))])
            else:
                out.append(self.rich(l))
            out[-1] = [(t, a) for t, a in out[-1] if t]
        return out

    def reports_key(self, ch: int) -> None:
        step = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1}.get(ch)
        page = {curses.KEY_PPAGE: -1, curses.KEY_NPAGE: 1, ord(" "): 1}.get(ch)
        if ch in (27, ord("o"), ord("q"), ord("Q")):
            self.overlay = None
        elif step:
            self.scroll(self.rep_focus, step)
        elif page:                                        # these page the report from either box
            self.scroll("report", page * max(1, self.rep_room - 1))
        elif ch in (curses.KEY_HOME, curses.KEY_END):
            self.scroll(self.rep_focus, -10 ** 6 if ch == curses.KEY_HOME else 10 ** 6)
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT, 10, 13, curses.KEY_ENTER):
            self.rep_focus = "reports" if ch == curses.KEY_LEFT else "report"
        elif ch == ord("s"):
            self.save_report()
        elif ch == ord("P"):
            self.engine_key()
        elif ch in (9, curses.KEY_BTAB):
            self.rep_focus = "report" if self.rep_focus == "reports" else "reports"
        elif ch in (ord("t"), ord("T")):
            self.next_theme(-1 if ch == ord("T") else 1)

    def engine_key(self) -> None:
        """P: pause the engine (asks first): it saves and stops until it is started again. Or start it."""
        if engine_alive(self.runtime):
            self.modal = {"type": "stop", "title": "pause engine", "lines": [
                "Pause the engine? Nothing trades, paper or live, until it starts again",
                "(P here, or reopening the dash). It saves its state before it stops.", "",
                "y pauses · any other key keeps it running"]}
        else:
            self.say(start_engine(self.runtime)); self.alive_at = 0

    def save_report(self) -> None:
        """Keep the live report shown as a file. On a thread: it is worked out again, whole."""
        rid = self.rep_sel
        if rid.startswith("saved:"):
            self.say(f"already saved: runtime/{rid[len('saved:'):]}"); return
        if self.rep_saving:
            return
        self.rep_saving = True
        self.say("saving the report…")

        def work() -> None:
            try:
                msg = f"saved runtime/{reports.save(self.runtime, rid)}"
            except Exception as ex:
                msg = f"not saved: {getattr(ex, 'strerror', None) or ex or type(ex).__name__}"
            self.rep_saving, self.rep_at = False, 0.0     # the list is looked up again
            self.say(msg)
        threading.Thread(target=work).start()             # not a daemon: closing the screen lets it finish

    def next_theme(self, d: int) -> None:
        self.P.set_theme(themes.next_name(self.P.name, d))
        self.save_prefs()
        self.say(f"theme {self.P.name}" + ("" if self.P.many else " (needs a 256-color terminal)"))

    # ------------------------------------------------------------ settings (s)
    def set_rows(self) -> list:
        """The box's rows: a variant's, or the desk's and the screen's."""
        if self.edit_var:
            return list(varmod.RULE_SETTINGS)
        return cfgmod.SETTINGS + SCREEN_SETTINGS

    def set_value(self, key: str):
        """What the setting is: the draft's value or the variant's; the engine's value, or the one just sent until
        the engine shows it (10 s)."""
        if key == "screen.log":
            return self.log_filter
        if key.startswith("rule."):
            return self.draft[key[5:]] if key[5:] in self.draft else self.rule_now(key[5:])
        now_v = (self.raw.get("settings") or {}).get(key)
        sent = self.set_sent.get(key)
        if sent and now_v != sent[0] and time.time() - sent[1] < 10:
            return sent[0]
        self.set_sent.pop(key, None)
        return now_v

    def set_fmt(self, row, v) -> str:
        if row.kind == "cap":
            return f"{v:g}" if v else "off"
        if v is None:
            return "?"
        if row.kind == "bool":
            return "on" if v else "off"
        if row.kind == "choice":
            return LOG_FILTERS.get(v, v)
        if row.kind == "p":
            return f"{v * 100:.0f}¢"
        if row.kind in ("slots", "coins"):
            return " ".join(str(x) if row.kind == "slots" else cfgmod.SERIES.get(x, x).split("-")[0] for x in v)
        if row.kind == "n":
            return f"{v:g}"
        return ("-" if v < 0 else "") + f"${abs(v):,.2f}".replace(".00", "")

    def settings_lines(self) -> list:
        P = self.P
        rows = self.set_rows()
        self.set_sel %= len(rows)
        lw = max(len(r.label) for r in rows)
        armed = self.armed()
        out: list = []
        for i, row in enumerate(rows):
            if row.group:
                out += ([""] if out else []) + [[(row.group, P(P.c("head"), bold=True))] + self.rule_head(row)]
            sel = i == self.set_sel
            if sel and self.set_buf is not None:
                val = [(f" {self.set_buf}▏", P(YELLOW, bold=True) | curses.A_REVERSE)]
            elif row.kind in ("slots", "coins"):         # each choice, on bright and off grey; the cursor's reversed
                v = self.set_value(row.key) or []
                ch = self.set_choices(row)
                val = [(" ", 0)]
                for j, c in enumerate(ch):
                    label = str(c) if row.kind == "slots" else cfgmod.SERIES[c].split("-")[0]
                    on = c in v
                    val += [(f" {label} ", P(GREEN if on else P.grey, bold=on) |
                             (curses.A_REVERSE if sel and j == min(self.set_sub, len(ch) - 1) else 0))]
                if row.sec == "rule" and row.name in self.draft:
                    val += [(" *", P(YELLOW, bold=True))]
            else:
                v = self.set_value(row.key)
                changed = row.sec == "rule" and row.name in self.draft
                col = YELLOW if changed or row.key in self.set_sent else (GREEN if v else RED) if row.kind == "bool" else P.c("key")
                val = [(f" {self.set_fmt(row, v)} ", P(col, bold=True) | (curses.A_REVERSE if sel else 0))]
                if changed:                              # 19 → 10 *
                    val = [(f" {self.set_fmt(row, self.rule_now(row.name))} →", P(P.grey))] + val + [("*", P(YELLOW, bold=True))]
            if sel:                                      # the line the settings box keeps in view
                self.set_line = len(out) - (1 if i == 0 else 0)   # the first row: its heading too
            mark = ("▸ ", P(P.c("key"), bold=True)) if sel else ("  ", 0)
            out.append([mark, (f"{row.label:<{lw}}  ", P(WHITE, bold=sel))] + val)
        row = rows[self.set_sel]
        def details(r) -> list[str]:
            """The two lines under the divider for row r."""
            if r.sec == "rule":
                how = "←→ pick one · space or enter turns it on / off" if r.kind in ("slots", "coins") else \
                    f"{self.set_fmt(r, r.lo)} to {self.set_fmt(r, r.hi)}, steps of " + ("1¢" if r.kind == "p" else f"{r.step:g}")
                return [r.what, how]
            if r.kind in ("usd", "n"):
                return [r.what, f"{self.set_fmt(r, r.lo)} to {self.set_fmt(r, r.hi)}, steps of {self.set_fmt(r, r.step)}  ·  saved at once"]
            return [r.what, "←→ or enter changes it  ·  saved at once"]
        # pad to the widest any row needs, so the box keeps one size while the cursor moves
        wide = max(len(t) for r in rows for t in details(r))
        self.set_foot = len(out)                         # from here down stays put while the rows scroll
        out += [RULE_LINE] + [[(t.ljust(wide), P(P.grey))] for t in details(row)]
        if row.sec == "web":                             # what the service is doing now, and where to open it
            ws = self.st.get("web_state") or {}
            st = ws.get("state", "unknown")
            say = {"active": ("running · " + ws.get("url", ""), GREEN), "inactive": ("stopped", P.grey),
                   "failed": ("failed: journalctl --user -u tern-web", RED),
                   "activating": ("starting… (stuck here: the port may be taken; journalctl --user -u tern-web)", YELLOW),
                   "deactivating": ("stopping…", YELLOW)}.get(st, (st, P.grey))
            out += [[("web screen now: ", P(WHITE)), (say[0], P(say[1], bold=True))]]
        else:                                            # the same height on every row
            out += [[("", 0)]]
        if any(r.key not in (self.raw.get("settings") or {}) for r in rows if r.sec not in ("screen", "rule")) or \
                self.edit_var and any("on" not in v for v in self.raw.get("variants") or []):
            out += [[("The engine running is older than this screen: restart it (P, P) to change these.", P(YELLOW, bold=True))]]
        elif self.edit_var:
            out += [[(f"Nothing is saved until s: that makes a new variant; {self.vn(self.edit_var)} stays as it is.", P(P.grey))]]
        elif armed:
            out += [[("Live is armed: raising a live limit or lowering a gate asks you to type \"live\".", P(YELLOW, bold=True))]]
        return out

    def rule_head(self, row) -> list:
        """What follows RULE: the variant the draft starts from, and how much of it is changed."""
        P = self.P
        if row.group != "RULE":
            return []
        n = len(self.draft)
        own = (self.vrow(self.edit_var) or {}).get("own")
        return [("  " + self.vn(self.edit_var), P(P.c("variant"), bold=True)), ("  built in" if not own else "", P(P.grey)),
                (f"  {n} change{'s' if n > 1 else ''}, not saved" if n else "", P(YELLOW))]

    def set_choices(self, row) -> list:
        if row.kind == "coins":
            return list(cfgmod.SERIES)
        return sorted({*varmod.SLOT_CHOICES, *(self.set_value(row.key) or [])}, reverse=True)   # and config.json's own

    def send_setting(self, row, value) -> None:
        key = row.key
        if row.key == "screen.log":
            self.log_filter = value; self.log_anchor = None; self.save_prefs(); return
        if row.sec == "rule":                        # into the draft: nothing is sent until s
            if row.kind in ("p", "cap", "usd", "n"):
                value = int(value) if row.name == "cap" else float(int(value)) if row.kind == "n" else \
                    round(value, 1 if row.kind == "cap" else 2)
                if not row.lo - 1e-9 <= value <= row.hi + 1e-9:
                    self.say(f"{row.label} must be {self.set_fmt(row, row.lo)} to {self.set_fmt(row, row.hi)}"); return
                if row.kind == "cap":
                    value = value or None
                other = {"band_lo": "band_hi", "band_hi": "band_lo"}.get(row.name)
                edge = self.set_value("rule." + other) if other else None
                if edge is not None and (value >= edge if row.name == "band_lo" else value <= edge):
                    self.say(f"{row.label} must stay {'below' if row.name == 'band_lo' else 'above'} {edge * 100:.0f}¢"); return
            now = self.rule_now(row.name)
            if value == now or row.kind in ("slots", "coins") and now is not None and sorted(value) == sorted(now):
                self.draft.pop(row.name, None)       # back to what it was
            else:
                self.draft[row.name] = value
            return
        if row.kind in ("usd", "n"):
            if not row.lo <= value <= row.hi:
                self.say(f"{row.label} must be {self.set_fmt(row, row.lo)} to {self.set_fmt(row, row.hi)}"); return
            if row.kind == "n":
                value = float(int(value))
        cur = self.set_value(key)
        if cur is not None and value == cur:
            return
        loosens = row.risk and cur is not None and (value - cur) * row.risk > 0
        if loosens and self.armed():
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
                buf, self.set_buf = self.set_buf.strip().lstrip("$").replace(",", "").rstrip("¢"), None
                try:
                    v = float(buf)
                    self.send_setting(row, v / 100 if row.kind == "p" and v > 1 else v)
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
            self.set_sel = (self.set_sel - 1) % len(rows); self.set_sub = 0
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.set_sel = (self.set_sel + 1) % len(rows); self.set_sub = 0
        elif row.kind in ("slots", "coins") and (step or ch in (10, 13, curses.KEY_ENTER, ord(" "))):
            choices = self.set_choices(row)
            self.set_sub = min(self.set_sub, len(choices) - 1)
            if step:
                self.set_sub = max(0, min(len(choices) - 1, self.set_sub + step)); return
            cur = self.set_value(row.key)
            if cur is None:
                self.say("no value from the engine yet"); return
            cur = list(cur)
            c = choices[self.set_sub]
            want = [x for x in choices if (x in cur) != (x == c)]
            if not want:
                self.say(f"at least one of the {row.label} stays on"); return
            self.send_setting(row, want)
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
            if row.kind == "cap" and cur is None and self.rule_now("contracts") is not None:
                cur = 0                                   # off
            if cur is None:
                self.say("no value from the engine yet"); return
            self.send_setting(row, max(row.lo, min(row.hi, round((cur + step * row.step) / row.step) * row.step)))
        elif ch in (10, 13, curses.KEY_ENTER):
            self.set_buf = ""
        elif ord("0") <= ch <= ord("9"):
            if row.kind in ("usd", "n", "p", "cap"):      # the rest have nothing to type
                self.set_buf = chr(ch)
        elif not self.edit_var:                           # esc, s or anything else closes it
            self.close_settings()
        elif ch == ord("s"):
            self.save_draft()
        elif ch == 27:                                    # a draft goes only with esc, not with a stray key
            vn = self.var_new
            if vn and (self.edit_var, self.draft) == (vn["variant"], vn["draft"]):
                self.say("closed: it is being saved")
            elif self.draft:
                self.say("changes thrown away" + ("" if vn else ": nothing was saved"))
            self.close_settings()

    def logs_shown(self) -> list:
        """The engine's log lines the log box shows, after the log filter (settings, s)."""
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
        if self.overlay == "reports":
            self.reports_screen(H, W)
            self.footer(H, W)
            if self.modal:                           # P: pause the engine?
                self.draw_overlay(self.modal["lines"], self.modal["title"], self.modal.get("color", YELLOW))
            self.s.refresh(); return
        self.pick_strike()
        self.rects[self.tab] = (0, 0, H - 1, W)          # the status line takes the last row
        self.clip, self.drawing = W - 1, self.tab
        getattr(self, self.tab + "_panel")(0, 0, H - 1, W)
        self.clip, self.drawing = None, ""
        self.footer(H, W)
        if self.overlay == "settings":
            lines = self.settings_lines()
            self.draw_overlay(lines, f"edit {self.vn(self.edit_var)}" if self.edit_var else "settings", self.P.border("contract"), pin=len(lines) - self.set_foot,
                              follow=self.set_line, hints=[("↑↓", " pick"), ("←→", " step"), ("enter", " type")] +
                              ([("s", "ave as new"), ("esc", " discard")] if self.edit_var else [("esc", " close")]))
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
            elif d and self.overlay == "reports":
                self.wheel(my, mx, d)
            elif d and self.overlay and not self.modal:
                self.ov_off = max(0, self.ov_off + 3 * d)
            elif d and not self.modal:
                self.wheel(my, mx, d)
            return True
        if self.modal:
            m = self.modal; self.modal = None
            if m.get("type") == "vdelete":
                if ch in (ord("y"), ord("Y")):
                    self.send(op="variant_delete", variant=m["name"])
                else:
                    self.say("kept")
                return True
            if m.get("type") in ("rename", "vname"):
                if ch in (10, 13, curses.KEY_ENTER):
                    if m["type"] == "vname":
                        cid = self.send(name=m["buf"].strip(), **m["cmd"])
                        self.var_new = {"cid": cid, "variant": m["cmd"]["from"], "draft": m["cmd"]["rule"]} if cid else None
                    else:
                        self.send(op="rename", name=m["buf"].strip(), **m["target"])
                    return True
                if ch == 27:
                    self.say("not saved yet: the draft is still open" if m["type"] == "vname" else "name kept"); return True
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    m["buf"] = m["buf"][:-1]
                elif ch == 21:                   # ctrl+u: clear the line
                    m["buf"] = ""
                elif 32 <= ch < 127 and len(m["buf"]) < m["max"]:
                    m["buf"] += chr(ch)
                m["lines"][-1] = "> " + m["buf"]
                self.modal = m
                return True
            if m.get("type") in ("arm", "setlive", "livecmd"):
                arm = m["type"] == "arm"
                moving = m["title"] == "MOVE LIVE"
                kept = m["kept"] if m["type"] == "livecmd" else "live not moved" if moving else "not armed" if arm else "cap kept"
                if ch in (10, 13, curses.KEY_ENTER):
                    if m["buf"].strip().lower() == "live":
                        if arm:
                            self.send(op="live", on=True, variant=m["variant"]); self.say("live moved" if moving else "live armed")
                        elif m["type"] == "setlive":
                            self.send(op="set", key=m["key"], value=m["value"])
                            self.set_sent[m["key"]] = (m["value"], time.time())
                        else:
                            self.send(**m["cmd"])
                    elif moving and m["buf"].strip().lower() == "off":
                        self.send(op="live", on=False); self.say("disarming live")
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
            return True
        if self.overlay == "settings":
            self.settings_key(ch)
            return True
        if self.overlay == "reports":
            self.reports_key(ch)
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
            self.scroll(self.tab, {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                                     curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10}[ch])
        elif ch in (10, 13, curses.KEY_ENTER):
            self.select_variant()
        elif ch in (ord("D"), ord(" ")):
            if self.shown("variants"):
                self.variant_key(ch)
            else:
                self.say(("D deletes a variant: " if ch == ord("D") else "space turns a variant on or off: ") +
                         self.back_to("highlight one first"))
        elif ch == ord("r"):
            self.rename_ask()
        elif ord("1") <= ch <= ord(str(len(BOXES))):
            self.show_tab(BOXES[ch - ord("1")])
        elif ch in (9, curses.KEY_BTAB):
            self.show_tab(BOXES[(BOXES.index(self.tab) + (-1 if ch == curses.KEY_BTAB else 1)) % len(BOXES)])
        elif ch == ord("C"):
            order = list(cfgmod.SERIES)
            self.series = order[(order.index(self.series) + 1) % len(order)] if self.series in order else order[0]
            self.sel = None; self.write_view(); self.save_prefs(); self.say(f"viewing {cfgmod.SERIES[self.series]}")
        elif ch in (ord("t"), ord("T")):
            self.next_theme(-1 if ch == ord("T") else 1)
        elif ch == ord("c") and self.shown("spot"):
            order = list(CHARTS)
            self.chart = order[(order.index(self.chart) + 1) % len(order)] if self.chart in order else "15m"
            self.save_prefs()
        elif ch == ord("V") and self.shown("book"):
            cur = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
            self.book_view = "live" if cur == "paper" else "paper"
        elif ch == ord("g"):
            self.overlay, self.ov_off = "gate", 0
        elif ch == ord("e"):
            if self.shown("variants"):
                self.edit_variant()
            else:
                self.say("e edits a variant: " + self.back_to("highlight one, then e"))
        elif ch == ord("o"):
            self.overlay, self.rep_focus, self.rep_at = "reports", "reports", 0.0
        elif ch == ord("?") or ch == ord("h"):
            self.overlay, self.ov_off = "help", 0
        elif ch == ord("P"):
            self.engine_key()
        elif ch == ord("s"):
            self.open_settings()
        elif ch == ord("L"):
            self.live_key()
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
