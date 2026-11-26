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

from . import about as aboutmod
from . import config as cfgmod
from . import devices as devmod
from . import names as namesmod
from . import reports
from . import shadow as shadowmod
from . import variants as varmod
from . import themes
from .engine import live_dir

# ---------------------------------------------------------------- colors

BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

PAIRS = 255                                 # the color pairs an attribute can name: curses.color_pair(256) is pair 0

ROW = -1                                    # placeholder attr: "tint me by my row", filled in when drawn

# what each theme role draws as on an 8-color terminal (themes only apply with 256 colors)
FEW = {"grey": WHITE, "key": MAGENTA, "head": WHITE, "sym": CYAN, "event": BLUE, "unit": WHITE,
       "word_a": WHITE, "word_b": WHITE, "word_c": WHITE, "ticker": CYAN, "strike": YELLOW, "slot": YELLOW,
       "verb": MAGENTA, "variant": CYAN, "phrase": YELLOW, "sep": MAGENTA, "value": WHITE,
       "rows": WHITE, "fade_txt": WHITE, "fade_ts": BLUE}
FEW_BORDER = {"spot": CYAN, "contract": MAGENTA, "ladder": BLUE, "book": YELLOW, "variants": GREEN, "log": BLUE,
              "gate": RED, "reports": CYAN, "settings": MAGENTA, "themes": YELLOW, "help": GREEN}
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
        self.cache: dict[tuple[int, int], int] = {}        # (fg, bg) -> its pair's attribute
        self.full, self.drawn = False, None                # whether they ran out; the box and theme they are for
        self.set_theme(None)

    def set_theme(self, name: str | None) -> None:
        self.name = name if name in themes.THEMES else themes.DEFAULT
        self.t = themes.get(self.name)
        self.grey = self.t["grey"] if self.many else 8 if curses.COLORS >= 16 else WHITE

    def new_frame(self, box: str) -> None:
        """Before everything is drawn again: another box or theme starts with all the pairs, as does one that ran out."""
        if self.full or self.drawn != (box, self.name):
            self.cache.clear()
        self.full, self.drawn = False, (box, self.name)

    def pair(self, fg: int, bg: int) -> int:
        """The attribute for fg on bg. With no pair left, the one made that looks most like it."""
        if (fg, bg) not in self.cache:
            n = len(self.cache) + 1
            if n > min(PAIRS, curses.COLOR_PAIRS - 1):
                self.full = True
                like = [k for k in self.cache if k[1] == bg and k[0] > 15]
                return self.cache[min(like, key=lambda k: themes.distance(k[0], fg))] if like and fg > 15 else curses.A_NORMAL
            curses.init_pair(n, fg, bg)
            self.cache[fg, bg] = curses.color_pair(n)
        return self.cache[fg, bg]

    def __call__(self, fg: int, bold: bool = False, dim: bool = False) -> int:
        if self.many:
            fg = self.t["base"].get(fg, fg)
        a = self.pair(fg, self.bg)
        if bold:
            a |= curses.A_BOLD
        if dim:
            a |= curses.A_DIM
        return a

    def on(self, fg: int, bg: int, bold: bool = False) -> int:
        """fg, or the nearest color readable there, on a background of its own. 8 colors have no dark one to spare: no
        background there."""
        if not self.many:
            return self(fg, bold=bold)
        fg = themes.readable(self.t["base"].get(fg, fg), bg)
        return self.pair(fg, bg) | (curses.A_BOLD if bold else 0)

    def row(self, on: bool):
        """What draws a list's row: as P(fg, bold), and on the theme's fill if it is the highlighted one."""
        if not on:
            return self
        if not self.many:
            return lambda fg, bold=False: self(fg, bold=bold) | curses.A_REVERSE
        return lambda fg, bold=False: self.on(fg, self.t["fill"], bold)

    def c(self, role: str) -> int:
        """The theme's color for a role (see themes.py)."""
        return self.t[role] if self.many else FEW[role]

    def border(self, name: str) -> int:
        return self.t["border"][name] if self.many else FEW_BORDER[name]

    def pick(self, role: str, frac: float, reverse: bool = False) -> int:
        """A color from a theme scale (rows, fade_txt, fade_ts) at 0..1 along it."""
        if not self.many:
            return FEW[role]
        scale = themes.smooth(tuple(self.t[role]))[::-1 if reverse else 1]
        c = scale[max(0, min(len(scale) - 1, int(frac * (len(scale) - 1) + 0.5)))]
        return themes.readable(c, themes.BACK) if role == "rows" else c

    def grad(self, name: str) -> list[int]:
        return list(themes.visible(themes.smooth(tuple(self.t["grads"][name])))) if self.many else GRAD8[name]


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
    Returns (codes, kind) where kind 1 = data dot, 2 = a dot of the first mark only, 3 = of the second, ..."""
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
    for n, mv in enumerate(marks or []):
        if not lo <= mv <= hi:
            continue
        y = py(mv)
        for x in range(0, W, 2):
            r, c = y // 4, x // 2
            if kind[r][c] in (0, n + 2):
                codes[r][c] |= BITS[y % 4][x % 2]
                kind[r][c] = n + 2
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
    ("ABOUT", "tern trades Kalshi's hourly crypto above-or-below markets by itself. Every trade comes from the "
         "rule; there are no manual orders. The engine runs on its own (tern.service) and keeps trading "
         "with this screen closed.\n"
         "Kalshi prices long shots too high and favorites too low. With {variant}, the variant selected, the "
                 "rule enters at {minutes} minutes before each "
                 "hourly close: it buys the favorite side of every strike whose mid is {band} (spread "
                 "{spread}c or less) at the ask, {contracts:g} contracts a strike and at most ${hour_cost:,.0f} an hour "
                 "a coin ({rule_coins}){caps}, and holds it to settlement. Over 67 days of BTC history the base rule "
                 "(75-95c, 25 20 15 10 minutes, 2c) made +0.6c a contract on data it was not chosen on.\n"
              "(C) steps through {coins}. Which coins a variant trades is in its settings. "
              "The 67-day test was BTC only, so the rule trades BTC unless you turn others on.\n"
              "{variant} starts with ${paper:,.0f}, its paper start. Every variant that is on trades on paper all the "
              "time, live or not, so its record keeps growing."),
    ("VARIANTS", "Each variant runs on paper side by side with the others, with its own record, its own cash and "
                 "real fees. The built-in ones:\n"
                 "{variants}\n"
                 "(5) shows them. Highlight one with ↑↓ and press (enter) to select it, marked ●: the book, ladder, "
                 "contract and gate show it. Selecting changes nothing that trades.\n"
                 "A variant holds everything about how it trades: its rule, contracts a strike, most an hour, its limits and its paper "
                 "start. Paper and live both keep to its limits (one from before 2026-11-24, and the built-in ones: only live). "
                 "It never buys more than the cash it has left. Once saved it never changes. Make your own, marked +: (e) there opens "
                 "the highlighted one's settings as a draft, and (s) there saves your changes as a new variant with an empty "
                 "record; the one it came from and its record stay. (space) turns one off or on, (D) deletes one, (r) renames "
                 "it. At most {max_own} of your "
                 "own; a deleted one's record stays on file.\n"
                 "Retired, records kept and listed by (tern report):\n"
                 "{retired}"),
    ("LIVE", "(7) Live trades a variant only after its own paper record has {days:g} days of hours "
                 "traded, {entries} settled entries and P&L of ${pnl:g} or more after fees. Hours with the engine or the "
                 "variant off don't count, and moving live to another never skips it: that one needs its own record.\n"
             "(L) goes live with the selected variant: it asks you to type \"live\". Selecting another one later does not "
             "move live; (L) with another selected asks whether to move it there, and (L) with live's own selected turns it "
             "off. It needs KALSHI_KEY_ID and KALSHI_KEY_FILE in "
             "~/.config/tern/kalshi.env. The status line turns red (● LIVE) once Kalshi accepts the key. "
             "{variant} keeps to ${risk:,.0f} at risk at once and ${order:,.0f} an order; a loss of ${day:,.0f} since local midnight "
             "turns live off and leaves open positions to settle. Every order is immediate-or-cancel. Kalshi "
             "is the record: its positions overwrite the engine's every 15 seconds."),
]
HELP_WIDTH = 96                                   # the prose wraps here at the widest

# the boxes and the status line: (number, name, what it shows)
HELP_BOXES = [
    ("¹", "spot", "the coin's price, with its change, high and low over the span and the strike; (c) cycles the span: 5m 15m 1h 24h"),
    ("²", "contract", "one strike as a bet (\"BTC above $61,900?\"): YES and NO bid/ask in cents, YES's price this hour on "
                      "0-100¢ (◀ now; green/red: where the rule buys YES/NO), and how far it is from being bought. Starts on the nearest ★"),
    ("³", "ladder", "every strike high to low: bid, ask, probability bar, spread, volume and your position; ★ marks the band. "
                    "↑↓ picks the strike the contract box shows"),
    ("⁴", "book", "the selected variant's money and record, its open positions, and what settled and was filled; (V) switches paper / live"),
    ("⁵", "variants", "each variant's hours, entries, P&L, ¢ a contract, win rate, t, and gate progress (yellow: still short); "
                      "● selected, L live's, + one you made; the highlighted one's P&L by hour below"),
    ("⁶", "log", "what the engine did, newest at the bottom; scroll back with ↑↓. On its right: the coins, the selected variant's "
                  "slots, warnings, and every variant's day"),
    ("⁷", "gate", "what the selected variant still needs to go live, caps, and every variant side by side"),
    ("⁸", "reports", "every record: worked out now, or saved"),
    ("⁹", "settings", "gate, alerts, web, what the log shows"),
    ("⁰", "themes", f"{len(themes.NAMES)} color themes: the one highlighted is in use"),
    ("", "help", "(h) this"),
    ("", "tabs", "one box fills the screen; the others are tabs on its top border: their keys show one, (tab) steps through. "
                 "Its own keys are on its bottom border and work only there; the log's are the ones that work everywhere"),
    ("", "status", "bottom row: PAPER or LIVE, the coin's price and 1h change, the hourly event and time to its close, rule "
                   "ON/off with the next slot (+live when live trades), stale data warnings (usually Wi-Fi), engine ok / stale / down"),
]

# overlay text worth a color: checks, key references like (7), words to type, warnings, paths and names
OVERLAY_TOKENS = re.compile(
    r"(?P<ok>\[x\])|(?P<todo>\[ \])"
    r"|(?P<ref>\((?:[a-zA-Z?0-9]|tern \w[\w-]*|enter|tab|esc)\))"
    r"|(?P<quote>\"[^\"]+\"|\blive and press enter\b)"
    r"|(?P<warn>\b(?:real money|turns live off|turns itself off|Nothing trades|No key found yet|Wipe)\b)"
    r"|(?P<keyhint>^(?:y|any key|any other key)\b|\besc\b|·\s(?:any (?:other )?key)\b)"
    r"|(?P<path>~/\S+|\bKALSHI_KEY_(?:ID|FILE)\b|\blive\.\w+)")
SHADOW_LINE = re.compile(r"^shadow: ([A-Z0-9]+)-(\S+) settled · (.+)$")       # the engine's line after a coin's hour settles
SLOT_LINE = re.compile(r"^paper: (\d+m) slot on ([A-Z0-9]+)-(\S+) bought · (.+)$")   # and after a slot
# one variant in them: "base +224.70 (3/4 -18.44)": its record, and of that event won/entries and P&L; "base 4 (20 for
# $17.20)": strikes, contracts and their cost (neither has what is in () before 2026-11-24)
SHADOW_CELL = re.compile(r"^(\S+) ([+-]\d[\d,]*\.\d\d|\d+)(?: \((\d+)/(\d+) ([+-][\d,]+\.\d\d)\))?(?: \((\d+) for \$([\d,]+\.\d\d)\))?$")

# the help's key legend: two columns of (section, [(keys, what they do)])
HELP_KEYS = [
    [("move", [("tab S-tab", "next / previous box"),
               ("1-9 0 h", "the box with that key on the top border"),
               ("↑↓  j k", "scroll: the ladder and contract pick a"),
               ("", "strike, variants highlights one"),
               ("PgUp PgDn", "ten steps at a time; Home End: the ends"),
               ("mouse wheel", "scrolls what it is over"),
               ("click", "a tab, a border's key, a row, a theme; again"),
               ("", "on a highlighted variant or setting: enter;"),
               ("", "what changes something asks y"),
               ("C", "next coin")]),
     ("look", [("c", "spot: chart span: 5m 15m 1h 24h"),
               ("V", "book: paper / live view"),
               ("s", "reports: save the one shown (PgUp PgDn page it)"),
               ("↑↓ ←→", "themes: pick one"),
               ("t T", "next / previous theme, on any screen")])],
    [("variants", [("enter", "select the highlighted one: the book,"),
                ("", "band and gate show it"),
                ("e", "edit the highlighted one's settings: s saves"),
                ("", "them as a new variant, esc discards; that one"),
                ("", "and its record stay as they are"),
                ("space", "turn it on / off (asks y)"),
                ("D", "delete it, if it is one you made"),
                ("r", "rename it: its record takes the new name")]),
     ("trade", [("L", "live: on with the selected variant, off, or"),
                ("", "moved to it (type \"live\")")]),
     ("settings", [("↑↓", "pick a setting"),
                   ("←→  - +", "step it down / up, or flip it"),
                   ("enter", "type a value, enter saves, esc leaves it"),
                   ("s esc", "in a variant (e): save as a new one / discard")]),
     ("engine", [("P", "pause / start the engine (asks y)"),
                 ("q", "close the screen; the engine keeps going")])],
]

# command line, shown at the end of the help
HELP_CLI = [
    ("tern", "this screen (starts the engine in paper if it is down)"),
    ("tern report", "every variant's record, deleted and retired ones too (8 shows it here)"),
    ("tern report --periods", "the same by settings period"),
    ("tern report --save", "save a summary: status, variants (report-timer: weekly)"),
    ("tern status", "a plain-text summary"),
    ("tern live-check", "test the Kalshi key: balance and positions, no orders"),
    ("systemctl --user status tern", "the engine's service"),
]

# what the log box may show (settings): key -> how the settings box names it
LOG_FILTERS = {"all": "everything", "no_shadow": "no shadow lines", "quiet": "live, warnings, commands",
               "trades": "trades only"}
TRADE_LINE = re.compile(r"^(?:live: \d+m slot on .*, [1-9]\d* bought$|paper: \d+m slot on \S+ bought · |live: \S+ (?:settled|finalized) )")
SCREEN_SETTINGS = [cfgmod.Setting("screen", "log", "log shows", kind="choice", group="SCREEN",
                                  what="shadow: the variants' hourly results · quiet hides paper too · trades: buys and settlements only")]

BIG = {"0": ("███", "█ █", "█ █", "█ █", "███"), "1": (" █ ", "██ ", " █ ", " █ ", "███"),
       "2": ("███", "  █", "███", "█  ", "███"), "3": ("███", "  █", "███", "  █", "███"),
       "4": ("█ █", "█ █", "███", "  █", "  █"), "5": ("███", "█  ", "███", "  █", "███"),
       "6": ("███", "█  ", "███", "█ █", "███"), "7": ("███", "  █", "  █", "  █", "  █"),
       "8": ("███", "█ █", "███", "█ █", "███"), "9": ("███", "█ █", "███", "  █", "███")}


def big(digits: str) -> list[str]:
    """Digits as five rows of block letters."""
    return ["  ".join(BIG[c][r] for c in digits) for r in range(5)]


# spot chart spans: c cycles them in this order (the engine sends ~15 min of seconds and 24h of minutes)
CHARTS = {"5m": 300, "15m": 900, "1h": 3600, "24h": 86400}

# the boxes, each filling the screen; the others are tabs on its top border. A box's key shows it, tab steps through
BOXES = ["spot", "contract", "ladder", "book", "variants", "log", "gate", "reports", "settings", "themes", "help"]
BOX_KEYS = "1234567890h"
SUPER = "¹²³⁴⁵⁶⁷⁸⁹⁰"                             # (help's h is in its name)

DROP_FIRST = ("↑↓", "space")                     # border hints that go before the others when they don't all fit

# keys by name (a browser's KeyboardEvent.key, plus BackTab for shift+tab) -> what curses gives
NAMED = {"ArrowUp": curses.KEY_UP, "ArrowDown": curses.KEY_DOWN, "ArrowLeft": curses.KEY_LEFT,
         "ArrowRight": curses.KEY_RIGHT, "PageUp": curses.KEY_PPAGE, "PageDown": curses.KEY_NPAGE,
         "Home": curses.KEY_HOME, "End": curses.KEY_END, "Tab": 9, "BackTab": curses.KEY_BTAB,
         "Enter": 10, "Escape": 27, "Backspace": 127}
# the keys a border shows that are not one letter: their names. Those of MOVES are clicked each on its own
HINT_KEY = {"⏎": "Enter", "enter": "Enter", "space": " ", "esc": "Escape", "↑": "ArrowUp", "↓": "ArrowDown",
            "←": "ArrowLeft", "→": "ArrowRight", "PgUp": "PageUp", "PgDn": "PageDown"}
# Every button's press and release, not just the ones key() uses: the terminal sends them all, and on one that is not in
# the mask getch() blocks until the next input (the screen stands still), nodelay or not. Not curses' own clicks: it
# would put a press and its release together as one.
MOUSE_EVENTS = sum(getattr(curses, f"BUTTON{n}_{what}") for n in range(1, 6) for what in ("PRESSED", "RELEASED"))
MOUSE_QUEUE = 8                                  # the mouse events curses keeps
MOVES = re.compile(r"[↑↓←→]|PgUp|PgDn")


# the reports box: the list's width, and the lines of a report that are a table's header, a table's row
# (name, then whole-number columns and a P&L) and a name with what it is
REPORT_LIST_W = 26
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
    "edit": lambda s: [("↑↓", " pick"), ("←→", " step"), ("enter", " type"), ("s", "ave as new"), ("esc", " discard")],
    "variants": lambda s: [("⏎", " select"), ("e", "dit"), ("space", " on/off"), ("r", "ename"), ("D", "elete"), ("L", "ive"), "↑↓"],
    "log": lambda s: [("t", "heme ", s.P.name), ("P", "ause" if s.alive else " start"), ("q", "uit"), "↑↓"],
    "gate": lambda s: [("L", "ive"), "↑↓"],
    "reports": lambda s: ([] if s.rep_sel.startswith("saved:") else [("s", "ave")]) + [("↑↓", " pick"), ("PgUp PgDn", " scroll")],
    "settings": lambda s: [("↑↓", " pick"), ("←→", " step"), ("enter", " type"), ("PgUp PgDn", " scroll")],
    "themes": lambda s: [("↑↓ ←→", " pick")],
    "help": lambda s: [("↑↓", " pick"), ("PgUp PgDn", " scroll")],
}


# the keys that are some boxes' alone, and work only there (settings' own, and a variant's editor's, come first in key());
# the rest, the log's t P q among them, work on every box
KEYS_ON = {**dict.fromkeys("\n\r reD", ("variants",)), "C": ("spot", "contract", "ladder"), "c": ("spot",), "V": ("book",),
           "L": ("book", "variants", "gate"), "s": ("reports",)}


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
    app_rows_on = True                         # the web screen has no APP rows: pairing is the desk's
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
        self.overlay: str | None = None        # "edit" while a variant's settings are open over the variants (e)
        self.set_sel = 0                       # the settings row the cursor is on
        self.set_buf: str | None = None        # the value being typed in the settings box, or None
        self.edit_var: str | None = None       # e: the variant the box's draft starts from (None: the settings box)
        self.draft: dict = {}                  # E: field -> value changed there and not saved
        self.set_back: int | None = None       # the settings row to go back to after E
        self.var_new: dict | None = None       # a draft was sent to be saved: cid, and its variant and draft
        self.set_sent: dict[str, tuple] = {}   # key -> (value, time) sent but not yet in the status
        self.log_filter = "all"                # what the log box shows: LOG_FILTERS
        self.offs: dict[str, int] = {}         # box -> the first line shown (gate, help, settings)
        self.about_of = None                   # the setting the right side of the settings box was scrolled on
        self.help_sel = 0                      # the help section picked
        self.theme_rows = 1                    # the themes box: rows in a column, as last drawn
        self.modal: dict | None = None
        self.devices = devmod.Devices(devmod.FILE)   # the phones paired (the APP rows); one instance for the screen
        self.phones: list = []                 # as last read (every 2 s): the desk's paired phones
        self.phones_at = 0.0
        self.pair_at = 0.0                     # when the pairing state was last looked at
        self.flash, self.flash_at = "", 0.0
        self.pending: dict[str, float] = {}
        self.alive, self.alive_at = False, 0.0
        self.P = Pal()
        self.clip: int | None = None
        self.rects: dict[str, tuple[int, int, int, int]] = {}   # panel -> (y, x, h, w) as last drawn
        self.clicked = False                   # a key is being pressed by a click
        self.hits: list[tuple] = []            # what a click is on, as last drawn: (y, x from, x to, what, its argument)
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
        self.rep_top = 0                       # first row of the list shown
        self.rep_off, self.rep_room, self.rep_len = 0, 1, 0   # the report's scroll: first line shown, lines that fit, lines in all
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
        self.pairing()
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
        moved = raw.get("renamed") or {}         # one of your own renamed: its key moved, and the screen goes with it
        if self.variant in moved:
            self.variant = moved[self.variant]; self.save_prefs()
        if self.var_sel in moved:
            self.var_sel = moved[self.var_sel]
        if self.edit_var in moved:
            self.edit_var = moved[self.edit_var]
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
                    self.close_edit()            # (not one opened since, or changed since)
        if self.edit_var and self.st.get("variants") and self.vrow(self.edit_var) is None:
            self.say("that variant is gone" + (": the changes were not saved" if self.draft else ""))
            self.close_edit()                    # deleted meanwhile
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
        """A box's key, tab: that box fills the screen."""
        self.tab, self.flash, self.set_buf = name, "", None
        self.save_prefs()

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
        """A rounded box, btop style. One of BOXES has the tabs on its top border and `right`, its keys, on its bottom
        border. Any other box has its title on the top border and `right` at that border's right end. `right` holds segments: a plain string (grey info) or a
        (key, rest) hint whose key shows in the theme's key color."""
        P = self.P
        a = P(color)
        saved, self.clip = self.clip, None
        tabbed = self.drawing in BOXES
        if tabbed:                                       # the tabs are on the row over the box's blank first one
            y, h = y - 1, h + 1
        tl, hz, tr, vt, bl, br = "╭─╮│╰╯"
        self.put(y, x, tl + hz * (w - 2) + tr, a)
        for r in range(1, h - 1):
            self.put(y + r, x, vt, a)
            self.put(y + r, x + w - 1, vt, a)
        self.put(y + h - 1, x, bl + hz * (w - 2) + br, a)
        cx = x + 3
        if tabbed:
            cx, lead = x + 2, w >= sum(len(n) + 4 for n in BOXES) + 3   # wide enough for a space on both sides of the names
            for i, name in enumerate(BOXES):
                cx = self.tab_seg(y, cx, i, name, color, lead) + 1        # + 1: the border joins them
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

    def tab_seg(self, y: int, x: int, i: int, name: str, color: int, lead: bool) -> int:
        """One box's tab on the top border, number and name on one background: its box's color while that is on screen,
        the theme's tab color for the rest. Returns where it ends."""
        P = self.P
        if name == self.drawing:
            key = text = P(color, bold=True) | curses.A_REVERSE
        elif P.many:
            key, text = P.on(P.c("key"), P.t["tab"], bold=True), P.on(P.t["tab_text"], P.t["tab"])
        else:
            key, text = P(P.c("key"), bold=True), P(P.grey)
        parts = [(SUPER[i], key), (name, text)] if i < len(SUPER) else [(name[0], key), (name[1:], text)]   # h is help's key
        x0 = x
        for t, a in [(" " if lead or i >= len(SUPER) else "", text), *parts, (" ", text)]:
            self.put(y, x, t, a); x += len(t)
        self.hit(y, x0, x, "tab", name)
        return x

    def flashing(self) -> bool:
        return bool(self.flash) and time.time() - self.flash_at < 8

    def hint(self, y: int, x: int, seg, border: int) -> None:
        """One ┤segment├ on a border line. A click on it presses its key."""
        P = self.P
        self.put(y, x, "─", border); x += 1
        key = seg if isinstance(seg, str) else seg[0]
        moves = list(MOVES.finditer(key))
        for m in moves:
            self.hit(y, x + m.start(), x + m.end(), "key", HINT_KEY[m.group()])
        if not moves and not isinstance(seg, str):
            self.hit(y, x, x + sum(map(len, seg)), "key", HINT_KEY.get(key, key))
        if isinstance(seg, str):
            self.put(y, x, seg, P(P.grey)); x += len(seg)
        else:                                    # pieces alternate pink (a key, or the rule's state) and white
            for i, piece in enumerate(seg):
                self.put(y, x, piece, P(P.c("key"), bold=True) if i % 2 == 0 else P(WHITE)); x += len(piece)
        self.put(y, x, "─", border)

    def hit(self, y: int, x0: int, x1: int, what: str, arg) -> None:
        """What is drawn at x0 up to x1 takes clicks: see click()."""
        self.hits.append((y, x0, x1, what, arg))

    def vn(self, name: str | None) -> str:
        """A variant's display name (r renames it). One of your own has its record under the same name; a built-in
        one's stays under its own."""
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
              marks: list[tuple[float, int]] | None = None, filled: bool = True, fit: bool = False) -> None:
        """A filled chart, colored by height: grad runs up the whole box or, with fit, up to the highest value drawn.
        marks: (value, color) lines across it, the first ones on top."""
        if w <= 0 or h <= 0 or not vals:
            return
        codes, kind = braille(vals, w, h, lo, hi, filled, [v for v, _ in marks or []])
        P = self.P
        top = next((r for r in range(h) if fit and 1 in kind[r]), 0)
        for r in range(h):
            g = grad[int(min(1.0, (h - 1 - r) / max(1, h - 1 - top)) * (len(grad) - 1) + 0.5)]
            ga, ma = P(g), [P(c) for _, c in marks or []]
            run, run_attr, run_x = "", None, x
            for c in range(w):
                code = codes[r][c]
                ch = chr(0x2800 + code) if code else " "
                at = ma[kind[r][c] - 2] if kind[r][c] > 1 else ga
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

    def spot_points(self, prod: str) -> list:
        """A coin's prices, [ts, price], oldest first: minute closes, then live seconds."""
        sp = (self.st.get("spot") or {}).get(prod) or {}
        sec, mins = sp.get("sec") or [], sp.get("min") or []
        first_sec = sec[0][0] if sec else time.time()
        return [[t + 60, p] for t, p in mins if t + 60 < first_sec] + sec

    def contract_points(self, ticker: str | None) -> list:
        """A strike's YES price this hour, [ts, price], oldest first."""
        out = []
        for t, b, a in (self.st.get("hist") or {}).get(ticker) or []:
            if b is not None and a is not None:
                out.append([t, (a + b) / 2])
            elif b is not None or a is not None:
                out.append([t, b if b is not None else a])
        return out

    def spot_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        prod = cfgmod.SERIES[self.series]
        now = time.time()
        span = CHARTS[self.chart]
        gw = w - 2 - 11
        vals = by_time(self.spot_points(prod), now - span, now, gw * 2)
        strike = None
        row = self.row(self.sel)
        if row:
            strike = strike_of(row["k"])
        self.box(y, x, h, w, "spot", self.P.border("spot"), PANEL_KEYS["spot"](self))
        gh = h - 4
        if not vals:
            self.put(y + h // 2, x + 3, "waiting for spot…", P(self.P.grey)); return
        lo, hi = min(vals), max(vals)
        if strike and (hi - lo) and abs(strike - (lo + hi) / 2) < 3 * (hi - lo):
            lo, hi = min(lo, strike), max(hi, strike)
        pad = (hi - lo) * 0.08 or vals[-1] * 0.0005
        lo, hi = lo - pad, hi + pad
        # the price, what it did in this span, and where the strike is
        head: list = [(f"{prod.split('-')[0]} ${px(vals[-1])}", P(P.c("sym"), bold=True))]
        chg = vals[-1] / vals[0] - 1 if vals[0] else 0.0
        head += [(f"   {'▲' if chg >= 0 else '▼'}{chg:+.2%}", P(GREEN if chg >= 0 else RED, bold=True)), (f" {self.chart}", P(P.c("unit"))),
                 ("   high ", P(P.grey)), (px(max(vals)), P(GREEN)), ("   low ", P(P.grey)), (px(min(vals)), P(RED))]
        if strike:
            d = vals[-1] - strike
            head += [("   strike ", P(P.grey)), (px(strike), P(YELLOW, bold=True)),
                     (f"   {'above' if d >= 0 else 'below'} it by ${px(abs(d))}", P(GREEN if d >= 0 else RED))]
        cx = x + 2
        for text, attr in head:
            self.put(y + 1, cx, text, attr); cx += len(text)
        ticks = [round(k * (gh - 1) / 4) for k in range(5)]
        at = lambda r: hi - (hi - lo) * r / max(1, gh - 1)
        dim = P.t["fill"] if P.many else P.grey
        self.graph(y + 2, x + 1, gh, gw, vals, lo, hi, P.grad("spot"),
                   ([(strike, YELLOW)] if strike else []) + [(at(r), dim) for r in ticks[1:-1]], fit=True)
        lx = x + 1 + gw + 1
        sr = int(round((hi - strike) / (hi - lo) * (gh - 1))) if strike and lo <= strike <= hi else None
        for r in ticks:
            if r != sr:
                self.put(y + 2 + r, lx, px(at(r))[:10], P(self.P.grey))
        if sr is not None:
            self.put(y + 2 + sr, lx, px(strike)[:10], P(YELLOW, bold=True))
        self.time_axis(y + h - 2, x + 1, gw, now - span, now)

    def time_axis(self, y: int, x: int, w: int, t0: float, t1: float) -> None:
        """Clock times under a chart that runs from t0 to t1 (now)."""
        for k in range(4):
            self.put(y, x + 1 + k * w // 4, time.strftime("%H:%M", time.localtime(t0 + (t1 - t0) * k / 4)), self.P(self.P.grey))
        self.put(y, x + w - 3, "now", self.P(self.P.grey))

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
        self.box(y, x, h, w, "contract", self.P.border("contract"), PANEL_KEYS["contract"](self))
        if not r:
            self.put(y + 2, x + 3, "waiting for the hourly event…", P(self.P.grey)); return
        pts = self.contract_points(self.sel)
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
        self.put(y + 1, x + 2, title, P(P.c("sym"), bold=True))
        cx = x + 2 + len(title) + 3
        for k, v, c in info:
            if k:
                self.put(y + 1, cx, k + " ", P(c, bold=True)); cx += len(k) + 1
            self.put(y + 1, cx, v + "   ", P(WHITE if k in ("YES", "NO") else c, bold=k != "vol")); cx += len(v) + 3
        if cx < x + w - 17:
            self.put(y + 1, x + w - 16, "bid/ask, cents", P(self.P.grey))
        gh, gw = h - 5, w - 2 - 7
        if gh > 0:
            lo_b, hi_b = S.get("band_lo", 0.75), S.get("band_hi", 0.95)
            row_of = lambda v: max(0, min(gh - 1, int(round((1 - v) * (gh - 1)))))
            t0 = min(t0, pts[0][0]) if pts else t0
            mids = by_time(pts, t0, t1, gw * 2) if pts else []
            self.time_axis(y + h - 3, x + 1, gw, t0, t1)
            if mids:
                self.graph(y + 2, x + 1, gh, gw, mids, 0.0, 1.0, P.grad("yes"), [(lo_b, P.grey), (1 - lo_b, P.grey), (0.5, P.grey)])
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
            self.hit(yy, x + 1, x + w - 1, "ladder", r["t"])
            inb = S and S["band_lo"] <= fav < S["band_hi"] and spread is not None and spread <= S["max_spread"] + 1e-9
            above = spot is not None and spot > r["k"]
            sel = r["t"] == self.sel
            R = P.row(sel)
            self.put(yy, x + 1, " " * (w - 2), R(WHITE))
            kc = GREEN if above else RED
            self.put(yy, x + 1, "★" if inb else " ", R(YELLOW, bold=True))
            self.put(yy, x + 2, f"{strike_label(r['k']):>9}", R(kc, bold=sel))
            self.put(yy, x + 12, f"{cents(bid):>5}", R(GREEN))
            self.put(yy, x + 18, f"{cents(ask):>5}", R(RED))
            # probability bar
            frac = max(0.0, min(1.0, mid or 0))
            full = frac * barw
            n8 = int(full * 8)
            bar = "█" * (n8 // 8) + (" ▏▎▍▌▋▊▉"[n8 % 8] if n8 % 8 else "")
            g = P.grad("yes")
            col = g[min(len(g) - 1, int(frac * (len(g) - 1) + 0.5))]
            self.put(yy, x + 25, bar.ljust(barw), R(col))
            self.put(yy, x + 25 + barw - 4, f"{(mid or 0)*100:3.0f}%", R(WHITE, bold=True))
            tint = R(P.pick("rows", frac, reverse=True))     # long shots blue, near-certain pink
            self.put(yy, x + 26 + barw, f"{(spread*100 if spread is not None else 0):>4.1f}", tint)
            self.put(yy, x + 31 + barw, f"{volume(r['v']):>7}", tint)
            hp = held.get(r["t"])
            if hp:
                self.put(yy, x + 39 + barw, f"{hp['q']:+6g}"[:6], R(GREEN if hp["q"] > 0 else MAGENTA, bold=True))
        if top > 0:
            self.put(y, x + w - 2, "▲", P(YELLOW))
        if top + rows < len(lad):
            self.put(y + h - 1, x + w - 2, "▼", P(YELLOW))

    def variants_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        vs = st.get("variants") or []
        G = st.get("gate_cfg") or {}
        if self.overlay == "edit":                          # e: the highlighted one's settings, in this box
            self.settings_panel(y, x, h, w, "variants"); return
        self.box(y, x, h, w, "variants", self.P.border("variants"), PANEL_KEYS["variants"](self))
        if not vs:
            self.put(y + 2, x + 3, "waiting for the engine…", P(self.P.grey)); return
        names = [v["name"] for v in vs]
        if self.var_sel not in names:
            self.var_sel = self.variant if self.variant in names else names[0]
        head = P(self.P.c("head"), bold=True)
        nw = max([8] + [len(self.vn(n)) for n in names])   # the name column: as wide as the longest name
        cols = [("hours", 5), ("entries", 7), ("P&L", 9), ("¢/ct", 6), ("win", 4), ("t", 6)]
        gap = 1 + max(0, min(4, (w - 7 - nw - 37 - 7 - 26) // 7))   # between the columns: what the width allows
        at, cx = {}, x + 5 + nw
        for name, cw in cols:
            at[name] = cx + gap
            cx += gap + cw
            self.put(y + 1, at[name], f"{name:>{cw}}", head)
        at["gate"] = cx + gap
        self.put(y + 1, x + 5, "variant", head); self.put(y + 1, at["gate"], "gate", head)
        # The graph's size is the box's alone, whatever the list holds. The list scrolls over it, under its fixed header:
        # the rows that fit, moved so the highlighted one shows.
        gh = h * 3 // 10
        gy = y + h - 3 - gh
        self.var_rows = rows = gy - y - 4
        si = names.index(self.var_sel)
        self.var_top = max(0, min(self.var_top, len(vs) - rows))
        if si < self.var_top:
            self.var_top = si
        elif si >= self.var_top + rows:
            self.var_top = si - rows + 1
        top = self.var_top
        for j, v in enumerate(vs[top:top + rows], start=top):
            yy = y + 2 + j - top
            self.hit(yy, x + 1, x + w - 1, "variant", v["name"])
            sel = v["name"] == self.var_sel
            R = P.row(sel)
            self.put(yy, x + 1, " " * (w - 2), R(WHITE))
            row = R(P.pick("rows", j / max(1, len(vs) - 1)))
            if v["name"] == self.variant:                # selected; live's; one made here (the rest are built in)
                self.put(yy, x + 2, "●", R(GREEN, bold=True))
            if v.get("live") and st.get("live_enabled"):
                self.put(yy, x + 3, "L", R(RED, bold=True))
            if v.get("own"):
                self.put(yy, x + 4, "+", R(P.c("key"), bold=True))
            on = v.get("on", True)
            self.put(yy, x + 5, f"{self.vn(v['name']):<{nw}}", R(P.c("variant") if on else P.grey, bold=on))
            pc = f"{v['per_contract']*100:+.2f}" if v.get("per_contract") is not None else "-"
            win = f"{v['win']:.0%}" if v.get("win") is not None else "-"
            t = f"{v['t']:+.2f}" if v.get("t") is not None else "-"
            self.put(yy, at["hours"], f"{v['hours']:>5}", row); self.put(yy, at["entries"], f"{v['entries']:>7}", row)
            pnl_c = R(GREEN if v["pnl"] >= 0 else RED, bold=True)
            self.put(yy, at["P&L"], f"{v['pnl']:>+9.2f}", pnl_c)
            self.put(yy, at["¢/ct"], f"{pc:>6}", row); self.put(yy, at["win"], f"{win:>4}", row)
            tc = self.P.grey if v.get("t") is None else GREEN if v["t"] >= 2 else RED if v["t"] <= -2 else WHITE
            self.put(yy, at["t"], f"{t:>6}", R(tc, bold=v.get("t") is not None and abs(v["t"]) >= 2))
            cx = at["gate"]
            g = v.get("gate") or {}
            if not on:
                self.put(yy, cx, "off", R(RED, bold=True))
            elif g.get("passed"):
                self.put(yy, cx, "✓ passed", R(GREEN, bold=True))
            else:
                for c in g.get("checks", []):          # a passed check is a word; one still short shows how far
                    have = c["have"]
                    txt = ({"days": "days", "entries": "ent", "pnl": "P&L"}[c["key"]] if c["ok"] else
                           f"{have:.1f}/{c['need']:g}d" if c["key"] == "days" else
                           f"{have}/{c['need']}" if c["key"] == "entries" else
                           f"P&L<{'-' if c['need'] < 0 else ''}${abs(c['need']):g}")
                    txt = ("✓" if c["ok"] else "") + txt
                    self.put(yy, cx, txt + " ", R(GREEN if c["ok"] else YELLOW))
                    cx += len(txt) + 1
        # the picked variant: what it is, its cumulative P&L by hour, and what enter would do
        if top > 0:
            self.put(y + 1, x + w - 2, "▲", P(YELLOW))
        if top + rows < len(vs):
            self.put(y + 2 + rows - 1, x + w - 2, "▼", P(YELLOW))
        self.count_lines(y + h - 1, x + 3, top, rows, len(vs))
        cur = next(v for v in vs if v["name"] == self.var_sel)
        nm = cur["name"] if self.vn(cur["name"]) == cur["name"] else f"{self.vn(cur['name'])} ({cur['name']})"
        nm = "+ " + nm if cur.get("own") else nm
        self.put(gy - 1, x + 2, f"{nm}: ", P(P.c("variant"), bold=True))
        self.put(gy - 1, x + 4 + len(nm), (cur.get("desc") or "")[:w - 8 - len(nm)], P(WHITE))
        curve = cur.get("curve") or []
        gw = w - 2 - 11
        if len(curve) >= 2:
            vals = [0.0] + curve
            lo, hi = min(vals), max(vals)
            vals = [vals[i * len(vals) // (gw * 2)] for i in range(gw * 2)] if len(vals) < gw * 2 else vals   # the whole width
            pad = (hi - lo) * 0.1 or 1.0
            self.graph(gy, x + 1, gh, gw, vals, lo - pad, hi + pad, P.grad("eq_up" if vals[-1] >= 0 else "eq_dn"), [(0.0, P.grey)], fit=True)
            zr = int(round((hi + pad) / (hi - lo + 2 * pad) * (gh - 1)))
            if lo < 0 < hi and 0 < zr < gh - 1:
                self.put(gy + zr, x + 1 + gw + 1, "$0", P(self.P.grey))
            self.put(gy, x + 1 + gw + 1, money(hi, True)[:10], P(self.P.grey))
            self.put(gy + gh - 1, x + 1 + gw + 1, money(lo, True)[:10], P(self.P.grey))
            self.put(gy + gh, x + 2, f"P&L over its last {len(curve)} hours", P(self.P.grey))
        else:
            self.put(gy + gh // 2, x + 3, "the curve starts after a couple of settled hours", P(self.P.grey))
        # The bottom row says what the highlighted variant is doing.
        name, sel_, live = self.vn(cur["name"]), cur["name"] == self.variant, cur.get("live") and st.get("live_enabled")
        tip, col = "", GREEN
        if not cur.get("on", True):
            tip, col = f"{name} is off: no new entries, open ones settle · space turns it on", YELLOW
        elif cur.get("stopped"):
            tip, col = f"{name} is past its daily loss limit: no new entries until midnight", YELLOW
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

            def go() -> None:
                if self.vrow(cur["name"]) is None:
                    self.say(f"{name} is gone"); return
                self.send(**cmd)
            if on and livs:
                self.modal = {"type": "livecmd", "title": "LIVE IS ARMED", "color": RED, "buf": "", "cmd": cmd, "kept": f"{name} kept off",
                              "lines": [f"Live is armed with {name}: turned on, real money trades it from its next slot,",
                                        "past its gate.", "", "type live and press enter (esc cancels):", "> "]}
            elif on:
                self.confirm(f"turn {name} on", [f"Turn {name} on? It buys on paper from its next slot."], go,
                             "turns it on", f"{name} kept off")
            else:
                self.confirm(f"turn {name} off", [f"Turn {name} off? No new entries; its open ones settle.",
                                                  *(["live trades it: live would enter nothing until it is on again."] if livs else [])],
                             go, "turns it off", f"{name} kept on", RED if livs else YELLOW)
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
            self.open_edit(self.var_sel)

    def live_key(self) -> None:
        """L: live on with the selected variant, off, or (armed with another) moved to the selected one."""
        row = self.vrow(self.variant)
        if row is None:
            self.say("no variants from the engine yet"); return
        if self.older_engine():
            return
        st, name, live = self.st, self.vn(self.variant), self.live_variant()
        if st.get("live_enabled") and live == self.variant:
            def off() -> None:
                self.send(op="live", on=False); self.say("disarming live")
            if self.clicked:                             # (the key is at once: it is the way out of live)
                self.confirm("live off", [f"Turn live off? {name} stops trading real money; its open positions ride to settlement."],
                             off, "turns it off", "live kept on")
            else:
                off()
            return
        L, rule = st.get("live_cfg") or {}, row.get("rule") or {}
        size = [f"It buys {rule.get('contracts', 0):g} contracts a strike, at most {money(rule.get('max_hour_cost', 0))} an hour a coin,",
                f"{money(rule.get('max_order', 0))} an order and {money(rule.get('max_risk', 0))} at risk at once.",
                f"Live turns itself off after -{money(rule.get('day_loss', 0))} in a day."]
        gate = f"{name} has passed its gate (7)." if (row.get("gate") or {}).get("passed") else \
            f"{name} has not passed its gate (7): live waits until it does." if L.get("auto_needs_gate", True) else \
            f"{name} has not passed its gate (7), and live.auto_needs_gate is off: it trades anyway."
        off = [] if row.get("on", True) else [f"{name} is off: live enters nothing until space turns it on."]
        if st.get("live_enabled"):
            self.modal = {"type": "arm", "title": "MOVE LIVE", "color": RED, "buf": "", "variant": self.variant,
                          "lines": [f"Live trades {self.vn(live)}. Move it to {name}? Real money would trade {name} instead.",
                                    gate, *off, *size, "Open live positions ride to settlement.",
                                    "", "type live to move it, off to turn live off (esc cancels):", "> "]}
            return
        self.modal = {"type": "arm", "title": "ARM LIVE", "color": RED, "buf": "", "variant": self.variant,
                      "lines": [f"Live trading uses real money on Kalshi. It would trade {name}.", gate, *off,
                                *size,
                                *([] if st.get("keys") else ["", f"No key found yet: live stays off until {cfgmod.KEYFILE_ENV} exists."]),
                                "", "type live and press enter (esc cancels):", "> "]}

    def open_edit(self, variant: str) -> None:
        """The settings rows on a variant's settings, as a draft, over the variants."""
        self.overlay, self.set_buf, self.edit_var, self.draft = "edit", None, variant, {}
        self.set_sub = 0
        self.set_back, self.set_sel = self.set_sel, 0

    def close_edit(self) -> None:
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

    def older_rows(self) -> bool:
        return any("on" not in v or "max_risk" not in (v.get("rule") or {}) for v in self.raw.get("variants") or [])

    def older_engine(self) -> bool:
        """Whether the engine is from before variants held all of this: it would do something else with these commands."""
        if not self.older_rows():
            return False
        self.say("the engine running is older than this screen: restart it (P, P)")
        return True

    def armed(self) -> bool:
        return bool(self.raw.get("live_enabled") or self.raw.get("mode") == "live")

    def rename_ask(self) -> None:
        """r: rename the highlighted variant."""
        v = self.var_sel
        if self.vrow(v) is None:
            self.say("no variants from the engine yet"); return
        name, mx = self.vn(v), namesmod.VARIANT_NAME_MAX
        self.modal = {"type": "rename", "title": f"rename {name}", "color": CYAN, "buf": name, "max": mx,
                      "target": {"variant": v},
                      "lines": (["Its record, the gate, live and tern report take the new name too:",
                                 f"{name} is left nowhere.", "", ] if self.vrow(v).get("own") else
                                ["Only what the screen calls it changes: its record, the gate and tern report",
                                 f"keep it as {v}.", f"enter on an empty line names it {v} again", ""]) + [
                                f"new variant name, up to {mx} characters (ctrl+u clears, esc keeps it):", "> " + name]}

    def cur_book(self):
        v = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
        return self.st.get(v)

    def book_panel(self, y, x, h, w) -> None:
        P, st = self.P, self.st
        v = self.book_view or ("live" if st.get("mode") == "live" else "paper")
        b = st.get(v) or {}
        self.box(y, x, h, w, "book", self.P.border("book"), PANEL_KEYS["book"](self))
        yy, cx = y + 1, x + 2
        for name in ("paper", "live"):
            label = f"{self.vn(self.variant)} on paper" if name == "paper" else \
                f"live · {self.vn(self.live_variant())}" if st.get("live_enabled") and self.live_variant() else "live"
            self.put(yy, cx, f" {label} ", P(P.border("book"), bold=True) | curses.A_REVERSE if name == v else P(self.P.grey))
            self.hit(yy, cx, cx + len(label) + 2, "book", name)
            cx += len(label) + 3
        if not b:
            return
        g = st.get("gate") or {}
        for tiles in (self.book_money(b), self.book_record(g)):
            cw = (w - 4) // 5
            for i, (label, text, color) in enumerate(tiles):
                self.put(yy + 2, x + 2 + i * cw, label, P(self.P.c("head")))
                self.put(yy + 3, x + 2 + i * cw, text, P(color, bold=True))
            yy += 3
        if v == "live":
            yy += 2
            r = (self.vrow(self.live_variant()) or {}).get("rule") or {}
            self.put(yy, x + 2, f"limits   at risk {money(r.get('max_risk'))} · an order {money(r.get('max_order'))} · "
                                f"a day's loss {money(r.get('day_loss'))}", P(self.P.grey))
        # the open positions on the left, what settled and was filled on the right; they scroll together
        yy += 2
        cw = (w - 6) // 2
        left, right = self.book_open(b, cw), self.book_done(b, cw)
        self.put(yy, x + 2, "", 0)
        room = max(0, y + h - 1 - yy)
        n = max(len(left), len(right))
        self.pos_off = max(0, min(self.pos_off, n - room))
        if self.pos_off > 0:
            self.put(yy, x + w - 2, "▲", P(YELLOW))
        if self.pos_off + room < n:
            self.put(y + h - 2, x + w - 2, "▼", P(YELLOW))
        for j in range(room):
            row = P(P.pick("rows", j / max(1, room - 1)))     # btop-style gradient down the list
            for lines, cx in ((left, x + 2), (right, x + 4 + cw)):
                for text, attr in lines[j + self.pos_off] if j + self.pos_off < len(lines) else []:
                    self.put(yy + j, cx, text, row if attr == ROW else attr); cx += len(text)

    def book_money(self, b: dict) -> list[tuple[str, str, int]]:
        """The book's money: (label, value, color)."""
        eq, start, val = b.get("equity"), b.get("start") or 0, self.P.c("value")
        pnl = (eq - start) if eq is not None and start else None
        return [("equity", money(eq), val),
                ("P&L", money(pnl, True) + (f" {pnl/start:+.2%}" if pnl is not None and start else ""), GREEN if (pnl or 0) >= 0 else RED),
                ("cash", money(b.get("cash")), val), ("at risk", money(b.get("at_risk")), YELLOW), ("fees", money(b.get("fees")), val)]

    def book_record(self, g: dict) -> list[tuple[str, str, int]]:
        """The selected variant's record, as the book's money is."""
        if not g:
            return []
        val, c = self.P.c("value"), GREEN if g["pnl"] >= 0 else RED
        return [("settled", f"{g['entries']:,}", val), ("record", money(g["pnl"], True), c),
                ("win", f"{g['win']:.0%}" if g.get("win") is not None else "-", val),
                ("a contract", f"{g['per_contract']*100:+.2f}¢" if g.get("per_contract") is not None else "-", c), ("open", f"{g['open']:,}", val)]

    def grid(self, title: str, cols: list[tuple[str, int]], rows: list[list[tuple[str, int]]], w: int, none: str) -> list:
        """A table as lines of (text, attr): a title, the columns' names, the rows (or none), spread to w."""
        head = self.P(self.P.c("head"), bold=True)
        gap = 1 + max(0, min(4, (w - sum(c for _, c in cols) - len(cols) + 1) // (len(cols) - 1)))
        left = ("coin", "side")                           # these columns start at their left end, the rest at their right
        fit = lambda name, c, text: f"{text:<{c}}" if name in left else f"{text:>{c}}"
        out = [[(title, self.P(self.P.border("book"), bold=True))], [((" " * gap).join(fit(n, c, n) for n, c in cols), head)]]
        for r in rows:
            out.append([(fit(n, c, t) + " " * gap, a) for (n, c), (t, a) in zip(cols, r)])
        return out + ([] if rows else [[(none, self.P(self.P.grey))]])

    def bet_cells(self, ticker: str, q: float) -> list[tuple[str, int]]:
        """A table row's first cells: the coin, the strike, the side."""
        series = (ticker or "").split("-")[0]
        m = re.search(r"-[TB]([0-9.]+)$", ticker or "")
        return [(cfgmod.coin(series)[:5], self.P(self.P.c("variant"))),
                (strike_label(float(m.group(1))) if m else "?", ROW),
                ("YES" if q > 0 else "NO", self.P(GREEN if q > 0 else MAGENTA, bold=True)), (f"{abs(q):g}", ROW)]

    def book_open(self, b: dict, w: int) -> list:
        P, now = self.P, time.time()
        pos = b.get("pos") or []
        gain = lambda v: P(GREEN if (v or 0) >= 0 else RED)
        rows = [[*self.bet_cells(p["t"], p["q"]), (cents(p["avg"]).strip(), ROW), (cents(p.get("mark")).strip(), ROW),
                 (money(p.get("pnl"), True), gain(p.get("pnl"))), (clock((p.get("close") or now) - now), P(P.grey))] for p in pos]
        return self.grid(f"OPEN {len(pos)}", [("coin", 4), ("strike", 7), ("side", 4), ("qty", 3), ("avg", 4), ("mark", 4),
                                              ("P&L", 8), ("closes", 6)], rows, w, "no open positions")

    def book_done(self, b: dict, w: int) -> list:
        """What settled, then the fills, newest first."""
        P, now = self.P, time.time()
        gain = lambda v: P(GREEN if v >= 0 else RED)
        trades = list(reversed(b.get("trades") or []))
        done, fills = [], []
        for t in trades:
            if t.get("kind") == "settle":
                q = -t["delta"]
                won = (t.get("result") == "yes") == (q > 0)
                word, wc = ("void", P(P.grey)) if t.get("result") == "void" else ("won", P(GREEN)) if won else ("lost", P(RED))
                done.append([*self.bet_cells(t["ticker"], q), (word, wc), (money(t["realized"], True), gain(t["realized"])),
                             (ago(now - t["ts"]), P(P.grey))])
            elif t.get("kind") == "fill":
                q = t["delta"]                               # + bought YES, - bought NO at 1 - price
                at = t["price"] if q > 0 else 1 - t["price"]
                fills.append([*self.bet_cells(t["ticker"], q), (cents(at).strip(), ROW), (money(t["fee"]), ROW),
                              (ago(now - t["ts"]), P(P.grey))])
        first = [("coin", 4), ("strike", 7), ("side", 4), ("qty", 4)]
        return self.grid("SETTLED", first + [("won", 5), ("P&L", 9), ("ago", 6)], done, w, "none yet") + [[("", 0)]] + \
            self.grid("FILLS", first + [("at", 5), ("fee", 9), ("ago", 6)], fills, w, "none yet")

    def log_panel(self, y, x, h, w) -> None:
        P = self.P
        self.box(y, x, h, w, "log", self.P.border("log"), PANEL_KEYS["log"](self))
        side = self.log_side(w)
        w -= side                                                  # the log itself is on the left
        self.split(y, x, h, "log", w)
        self.pane(y + 1, x + w + 2, h - 2, side - 4, self.log_summary(side - 4, h - 2))
        logs = self.log_rows(self.logs_shown(), w - 1)
        rows = h - 2
        end = self.log_end(logs, rows)
        saved, self.clip = self.clip, x + w - 1
        shown = logs[max(0, end - rows):end]
        roles = {"variant": (P.c("variant"), True), "count": (WHITE, True), "num": (WHITE, False), "money": (P.c("value"), False),
                 "note": (P.grey, False), "sep": (P.c("sep"), False), "head": (P.grey, False)}
        for j, (ts, lvl, msg, sub, cells) in enumerate(shown):
            yy = y + 1 + j
            age = (len(shown) - 1 - j) / max(1, rows - 1)          # 0 for the bottom (newest) line
            cx = x + 13                                            # a table, and a line's later rows, are under its text
            for text, role in cells or []:
                c, bold = roles.get(role) or ({"+": GREEN, "-": RED}.get(text.lstrip()[:1], WHITE), True)
                self.put(yy, cx, text, P(c, bold=bold)); cx += len(text)
            if sub == 0:
                self.put(yy, x + 2, time.strftime("%H:%M:%S", time.localtime(ts)), P(P.pick("fade_ts", age)))
                self.put(yy, x + 10, LOG_MARK.get(lvl, " "), P(LOG_BASE.get(lvl, WHITE), bold=True))
                cx = x + 11
            if sub and msg:                                        # (a later row's first word is no book's name)
                msg, cx = " " + msg, cx - 1
            for text, attr in self.log_spans(msg, lvl, P.pick("fade_txt", age)) if msg else []:
                self.put(yy, cx, text, attr); cx += len(text)
                if cx >= x + w:
                    break
        self.clip = saved
        if end - rows > 0:
            self.put(y, x + w - 1, "▲", P(YELLOW))
        if end < len(logs):
            self.put(y + h - 1, x + 3, f" {len(logs) - end} newer ▼ ", P(YELLOW))

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

    def log_rows(self, logs: list, w: int = 200) -> list[tuple]:
        """The log box's screen rows, (ts, level, text, sub, cells), in w columns. The engine writes a line a coin when an
        hour settles ("shadow: EV settled · base +1.20 (3/4 -18.44) ...") and after a slot ("paper: 25m slot on EV bought ·
        base 4 (20 for $17.20) ..."): an hour's, and a slot's, are one entry, where its last line is. Under it is a table, a
        row for each variant the hour settled or that bought, with a blank line around it: sub 1, 2..., no text, cells of
        (text, role). Any other line is as many rows as it needs, those after the first with sub 1, 2... and no time."""
        split = lambda text: [m for m in map(SHADOW_CELL.match, text.split(" · ")) if m]
        coin = cfgmod.coin
        num = lambda v: float(v.replace(",", ""))
        groups: dict[tuple, dict] = {}                           # (slot or "", hour) -> its lines' last place, coins, cells
        of: dict[int, dict] = {}                                 # line -> its group
        for n, (ts, lvl, msg) in enumerate(logs):
            m = SHADOW_LINE.match(msg) or SLOT_LINE.match(msg)
            if not m:
                continue
            slot, series, hour, text = ("", *m.groups()) if m.re is SHADOW_LINE else m.groups()
            cells = split(text)
            if len(cells) == text.count(" · ") + 1:
                g = of[n] = groups.setdefault((slot, hour), {"slot": slot, "cells": {}, "coins": {}})
                g["at"] = n
                g["coins"][coin(series)] = None
                for c in cells:                                  # by variant, then by coin; "": the record as the last line has it
                    by = g["cells"].setdefault(c.group(1), {})
                    by[""] = c
                    by[coin(series)] = c
        order = {v["name"]: k for k, v in enumerate(self.st.get("variants") or [])}   # as the variants box lists them
        out: list[tuple] = []

        def gap() -> None:
            if out and (out[-1][2] or out[-1][4]):
                out.append((out[-1][0], out[-1][1], "", out[-1][3] + 1, []))
        before: dict | None = None                               # the records as the hour before left them
        for n, (ts, lvl, msg) in enumerate(logs):
            g = of.get(n)
            if not g:
                one = len(msg) <= w - 11
                for k, t in enumerate([msg] if one else textwrap.wrap(msg, max(20, w - 13)) or [""]):
                    out.append((ts, lvl, t, k, None))
                continue
            if g["at"] != n:                                     # shown where the group's last line is
                continue
            table: list[list[tuple[str, str]]] = []
            who = sorted(g["cells"].items(), key=lambda x: order.get(x[0], len(order)))
            if g["slot"]:
                heads = ["variant", "buys", "contracts", "cost", "avg", "coins"]
                buys = spent = 0
                for name, by in who:
                    cs = {c: m for c, m in by.items() if c}
                    k = sum(int(m.group(2)) for m in cs.values())
                    full = all(m.group(6) for m in cs.values())
                    cn, cost = (sum(num(m.group(x)) for m in cs.values()) if full else None for x in (6, 7))
                    buys += k; spent = None if cost is None or spent is None else spent + cost
                    row = [(self.vn(name), "variant"), (f"{k}", "count"), (f"{cn:g}" if full else "", "num"),
                           (f"${cost:,.2f}" if full else "", "money"), (f"{cost / cn * 100:.0f}¢" if full and cn else "", "num")]
                    coins: list[tuple[str, str]] = []
                    for c, m in cs.items():
                        coins += ([(" · ", "sep")] if coins else []) + [(f"{c} {m.group(2)}" if len(cs) > 1 else c, "note")]
                    table.append(row + coins)
                head = f"paper: {g['slot']} slot bought " + " ".join(g["coins"]) + f" · {buys} buy{'' if buys == 1 else 's'}" + (f" · ${spent:,.2f}" if spent else "")
            else:
                heads = ["variant", "won", "this hour", "record"]
                now = {name: by[""].group(2) for name, by in who}
                won = all_ = 0
                for name, by in who:
                    cs = [m for c, m in by.items() if c and m.group(3)]
                    d = round(sum(num(m.group(5)) for m in cs), 2) if cs else \
                        None if before is None else round(num(now[name]) - num(before.get(name, "0")), 2)
                    if not cs and (d == 0 or any(m.group(3) for b in g["cells"].values() for m in b.values())):
                        continue                                 # it had nothing in this hour
                    a, b = sum(int(m.group(3)) for m in cs), sum(int(m.group(4)) for m in cs)
                    won += a; all_ += b
                    table.append([(self.vn(name), "variant"), (f"{a} of {b}" if cs else "", "num"), ("" if d is None else f"{d:+,.2f}", "signed"),
                                  (now[name], "signed")])
                head = "shadow: settled " + " ".join(g["coins"]) + (f" · {won} of {all_} won" if all_ else "" if table else " · no variant changed")
                before = {**(before or {}), **now}
            gap()
            out.append((ts, lvl, head, 0, None))
            keep = [c for c in range(len(heads)) if c > 4 or any(r[c][0] for r in table)]   # (older lines have no contracts, no won)
            wide = {c: max(len(heads[c]), *(len(r[c][0]) for r in table)) for c in keep if c < 5 and table}
            pad = lambda c, t: (f"{t:<{wide[c]}}" if c == 0 else f"{t:>{wide[c]}}") + ("   " if w >= 85 else " ")
            if table:
                out.append((ts, lvl, "", 1, [(pad(c, heads[c]) if c < 5 else heads[c], "head") for c in keep]))
            for k, row in enumerate(table, 2):
                out.append((ts, lvl, "", k, [(pad(c, row[c][0]), row[c][1]) for c in keep if c < 5] + row[5:]))
            gap()
        if out and not out[-1][2] and not out[-1][4]:
            out.pop()
        return out

    def log_side(self, w: int) -> int:
        """How much of the log box the summary on its right takes."""
        return max(38, min(58, w * 37 // 100))

    def log_summary(self, w: int, h: int) -> list:
        """The right of the log box: the coins, the selected variant's slots, what has come up, and every variant's day."""
        P, st, now = self.P, self.st, time.time()
        head = lambda t, note="": [(t, P(P.c("head"), bold=True)), (note, P(P.grey))]
        out: list = [head("COINS", "  price · 1 hour · moves")]
        for series, prod in cfgmod.SERIES.items():
            sp = (st.get("spot") or {}).get(prod) or {}
            px_, mins = sp.get("now"), sp.get("min") or []
            if px_ is None:
                continue
            past = next((p for t, p in reversed(mins) if t + 60 <= now - 3600), None)
            chg = px_ / past - 1 if past else None
            v = self.vols([series])[series]
            out.append([(f"{cfgmod.coin(series):<5}", P(P.c("sym"), bold=True)), (f"{px(px_):>9}  ", P(WHITE)),
                        (f"{chg:+.2%}".rjust(7) if chg is not None else " " * 7, P(GREEN if (chg or 0) >= 0 else RED)),
                        (f"  {v:4.1f} bp" if v is not None else "", P(P.grey))])
        S, ev = self.strat() or {}, (st.get("events") or {}).get(self.series) or {}
        left = (ev.get("close") or 0) - now
        if S.get("minutes") and left > 0:
            line, nxt = [], max([m for m in S["minutes"] if m * 60 < left], default=None)
            for m in sorted(S["minutes"], reverse=True):
                done = m * 60 >= left
                line += [(f":{60 - m:02g} " if 0 < m < 60 else f"{m:g}m ", P(P.c("slot"), bold=m == nxt)),
                         ("✓  " if done else f"in {clock(left - m * 60)}  " if m == nxt else "   ", P(GREEN if done else WHITE if m == nxt else P.grey))]
            out += [[], head("SLOTS", f"  {self.vn(self.variant)} · the hour closes in {clock(left)}"), line]
        t = time.localtime(now)
        start = now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
        bad = [l for l in st.get("logs") or [] if l[0] >= start and l[1] in ("warn", "error")]
        if bad:
            out += [[], head("WARNINGS", f"  {len(bad)} since midnight · the last at {time.strftime('%H:%M', time.localtime(bad[-1][0]))}")]
            out += [[(x, P(YELLOW))] for x in textwrap.wrap(bad[-1][2], max(10, w))[:2]]
        rows = [(v, v["today"]) for v in st.get("variants") or [] if v.get("today") and (v["today"][1] or v.get("open"))]
        if rows:
            rows.sort(key=lambda r: -r[1][0])
            n = h - len(out) - 3
            most = max([abs(d[0]) for _, d in rows] + [0.01])
            nw = max(7, *(len(self.vn(v["name"])) for v, _ in rows))
            bar = max(0, w - nw - 31)
            out += [[], head("TODAY", "  settled since midnight, best first"),
                    [(f"{'variant':<{nw}} {'P&L':>9} {'won':>9} {'open':>5}", P(P.grey, bold=True))]]
            for v, (pnl, entries, won) in rows[:max(1, n - (len(rows) > n))]:
                on = v["name"] == self.variant
                out.append([(f"{self.vn(v['name']):<{nw}} ", P(P.c("key") if on else P.c("variant"), bold=True)),
                            (f"{pnl:>+9.2f} ", P(GREEN if pnl >= 0 else RED, bold=True)), (f"{f'{won} of {entries}' if entries else '-':>9} ", P(WHITE)),
                            (f"{v.get('open', 0):>5}   ", P(P.grey)), ("■" * int(abs(pnl) / most * bar + 0.5), P(GREEN if pnl >= 0 else RED))])
            if len(rows) > n:
                out.append([(f"and {len(rows) - max(1, n - 1)} more", P(P.grey))])
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
        """Mouse wheel over a box: d is -1 (up) or +1 (down)."""
        name = next((n for n, (y, x, h, w) in self.rects.items() if y <= my < y + h and x <= mx < x + w), None)
        self.scroll(name, 3 * d if name in ("report", "gate", "helptext", "about") else d)

    def scroll(self, name: str | None, d: int) -> None:
        """Move one box d steps (negative is up). Boxes that don't scroll ignore it."""
        if name in ("ladder", "contract"):
            self.step_ladder(d)
        elif name == "help":
            self.help_sel, self.offs["help"] = max(0, min(len(HELP) + 2, self.help_sel + d)), 0
        elif name == "about":
            self.offs["about"] = max(0, self.offs.get("about", 0) + d)   # pane clamps the far end
        elif name == "helptext":
            self.offs["help"] = max(0, self.offs.get("help", 0) + d)   # pane clamps the far end
        elif name == "variants":
            names = [v["name"] for v in self.st.get("variants") or []]
            if names:
                i = names.index(self.var_sel) if self.var_sel in names else 0
                self.var_sel = names[max(0, min(len(names) - 1, i + d))]
        elif name == "book":
            self.pos_off = max(0, self.pos_off + d)    # book_panel clamps the far end
        elif name == "gate":
            self.offs[name] = max(0, self.offs.get(name, 0) + d)   # pane clamps the far end
        elif name == "settings" and self.set_buf is None:        # (a value being typed stays with its row)
            n = len(self.set_rows())
            self.set_sel = (self.set_sel + d) % n if abs(d) == 1 else max(0, min(n - 1, self.set_sel + d))
            self.set_sub = 0
        elif name == "themes":
            i = themes.NAMES.index(self.P.name) if self.P.name in themes.NAMES else 0
            self.P.set_theme(themes.NAMES[max(0, min(len(themes.NAMES) - 1, i + d))])
            self.save_prefs()
        elif name == "reports":
            ids = [r.id for r in self.rep_entries()]
            self.pick_report(ids[max(0, min(len(ids) - 1, ids.index(self.rep_sel) + d))])
        elif name == "report":
            self.rep_off = max(0, min(self.rep_len - self.rep_room, self.rep_off + d))
        elif name == "log":
            r = self.rects.get("log")
            logs = self.log_rows(self.logs_shown(), (r[3] - self.log_side(r[3]) if r else 100) - 1)
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

    def pane(self, y: int, x: int, h: int, w: int, lines: list, off: int = 0) -> int:
        """Lines in a part of a box, from line `off`: a string (colored by `rich`) or a list of (text, attr). Returns off,
        kept to what there is."""
        off = max(0, min(off, len(lines) - h))
        saved, self.clip = self.clip, x + w
        for j, l in enumerate(lines[off:off + h]):
            cx = x
            for text, attr in self.rich(l) if isinstance(l, str) else l:
                self.put(y + j, cx, text, attr); cx += len(text)
        self.clip = saved
        if off > 0:
            self.put(y, x + w, "▲", self.P(YELLOW))
        if off + h < len(lines):
            self.put(y + h - 1, x + w, "▼", self.P(YELLOW))
        return off

    def count_lines(self, y: int, x: int, off: int, room: int, n: int) -> None:
        """Which lines are showing, on the bottom border, when they are not all there."""
        if n > room and not self.flashing():
            self.put(y, x, f" {off + 1}-{off + room} of {n} ", self.P(self.P.grey))

    def split(self, y: int, x: int, h: int, name: str, lw: int) -> None:
        """The line between a box's two panes."""
        for r in range(h - 1):
            self.put(y + r, x + lw, "│", self.P(self.P.border(name)))

    def gate_panel(self, y, x, h, w) -> None:
        """The checks, going live and the limits stay put; the variants scroll under their heading."""
        self.box(y, x, h, w, "gate", self.P.border("gate"), PANEL_KEYS["gate"](self))
        top, rows = self.gate_lines(w - 4)
        self.pane(y + 1, x + 2, h - 2, w - 4, top)
        room = max(1, h - 2 - len(top))
        self.offs["gate"] = off = self.pane(y + 1 + len(top), x + 2, room, w - 4, rows, self.offs.get("gate", 0))
        self.count_lines(y + h - 1, x + 3, off, room, len(rows))

    def help_panel(self, y, x, h, w) -> None:
        """The sections on the left, and the one picked."""
        P, lw = self.P, 17
        self.box(y, x, h, w, "help", P.border("help"), PANEL_KEYS["help"](self))
        self.split(y, x, h, "help", lw)
        self.rects = {"help": (y, x, h, lw + 1), "helptext": (y, x + lw + 1, h, w - lw - 1)}
        secs = self.help_sections(w - lw - 5)
        for j, (title, _) in enumerate(secs):
            R = P.row(j == self.help_sel)
            self.put(y + 1 + j, x + 1, f"  {title.lower()}".ljust(lw - 1), R(P.pick("rows", j / (len(secs) - 1)), bold=j == self.help_sel))
            self.hit(y + 1 + j, x + 1, x + lw, "help", j)
        title, lines = secs[self.help_sel]
        self.put(y + 1, x + lw + 3, title, P(P.c("key"), bold=True))
        room = h - 4
        self.offs["help"] = off = self.pane(y + 3, x + lw + 3, room, w - lw - 5, lines, self.offs.get("help", 0))
        self.count_lines(y + h - 1, x + lw + 3, off, room, len(lines))

    def settings_panel(self, y, x, h, w, name="settings") -> None:
        """The settings on the left, what the one picked is on the right; a variant's (e), in its box."""
        P = self.P
        self.box(y, x, h, w, name, P.border(name), PANEL_KEYS["edit" if name == "variants" else name](self))
        size = lambda l: sum(len(t) for t, _ in l)
        lines = self.settings_lines()
        lw = min(max(46, max(map(size, lines)) + 4), w - 32)
        self.split(y, x, h, name, lw)
        room, n = h - 2, self.set_line
        off = max(0, min(self.offs.get("settings", 0), len(lines) - room))
        self.offs["settings"] = off = min(max(off, n - room + 1), max(0, n - 1))   # the one picked, and what's above it
        lines[n] = [*lines[n], (" " * max(0, lw - 2 - size(lines[n])), P.row(True)(WHITE))]
        off = self.pane(y + 1, x + 1, room, lw - 2, [[(" ", P.row(i == n)(WHITE))] + l for i, l in enumerate(lines)], off)
        for at, i, spans in self.set_at:                 # a setting's choices, and the rest of its line
            if off <= at < off + room:
                for x0, x1, j in spans:
                    self.hit(y + 1 + at - off, min(x + 2 + x0, x + lw), min(x + 2 + x1, x + lw), "set", (i, j))
                self.hit(y + 1 + at - off, x + 1, x + lw, "set", (i, None))
        self.rects = {"settings": (y, x, h, lw + 1), "about": (y, x + lw + 1, h, w - lw - 1)}
        about = self.settings_about(w - lw - 5)
        if self.about_of != (self.edit_var, self.set_sel):   # another setting: from its top
            self.about_of, self.offs["about"] = (self.edit_var, self.set_sel), 0
        self.offs["about"] = off = self.pane(y + 1, x + lw + 3, room, w - lw - 5, about, self.offs.get("about", 0))
        self.count_lines(y + h - 1, x + 3, off, room, len(about))

    def themes_panel(self, y, x, h, w) -> None:
        """Every theme, with its colors; the highlighted one is in use."""
        P = self.P
        self.box(y, x, h, w, "themes", P.border("themes"), PANEL_KEYS["themes"](self))
        names, room = themes.NAMES, h - 2
        cols = min(4, (w - 4) // 24)
        cw = (w - 4) // cols
        nw = max(map(len, names))
        sw = min(24, cw - nw - 4)                         # a palette's width: what the column has left
        self.theme_rows = rows = -(-len(names) // cols)   # a blank line between them keeps the palettes apart
        at = names.index(P.name)
        if rows * 2 - 1 > room:                           # a short screen: the blank line too
            y, room = y - 1, room + 1
        off = max(0, min(at % rows * 2 - room // 2, rows * 2 - 1 - room))
        for i, name in enumerate(names):
            c, r = divmod(i, rows)
            if not 0 <= r * 2 - off < room:
                continue
            yy, cx, t = y + 1 + r * 2 - off, x + 2 + c * cw, themes.THEMES[name]
            R = P.row(i == at)
            self.hit(yy, cx, cx + cw, "theme", name)
            self.put(yy, cx, f"{'▸' if i == at else ' '} ", R(P.c("key"), bold=True))
            self.put(yy, cx + 2, f"{name:<{nw}} ", R(t["border"]["spot"] if P.many else WHITE, bold=i == at))
            ramp = themes.smooth(tuple(t["grads"]["yes"]))
            for j in range(sw if P.many else 0):
                self.put(yy, cx + 3 + nw + j, "█", P(ramp[j * len(ramp) // sw]))
        if not P.many:
            self.put(y + h - 2, x + 2 + (cols - 1) * cw, "needs 256 colors", P(YELLOW))

    def draw_overlay(self, lines: list, title: str, color: int = CYAN) -> None:
        """A centered box of lines over the screen: a question."""
        H, W = self.s.getmaxyx()
        lines = [self.rich(l) if isinstance(l, str) else l for l in lines]
        w = min(W - 4, max(sum(len(t) for t, _ in l) for l in lines) + 4)
        h = min(H - 2, len(lines) + 2)
        y, x = (H - h) // 2, (W - w) // 2
        for r in range(h):
            self.put(y + r, x, " " * w)
        self.box(y, x, h, w, title, color)
        self.pane(y + 1, x + 2, h - 2, w - 4, lines)

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

    def help_sections(self, w: int) -> list[tuple[str, list]]:
        """The help, as (title, lines): prose filled from the config in use, the boxes, the key legend (two columns
        when w is wide enough, else one), and the command line."""
        P, st = self.P, self.st
        S = st.get("strategy") or cfgmod.Strategy().__dict__
        G = st.get("gate_cfg") or cfgmod.Gate().__dict__
        R = (self.vrow(self.variant) or {}).get("rule") or {}
        mins = [str(m) for m in S.get("minutes", [])]
        caps = (f", {S['cap']:g} a strike an hour" if S.get("cap") else "") + \
            (f", no slot after an hour over {S['max_vol']:g} bp a minute" if S.get("max_vol") else "")
        fill = dict(
            variant=self.vn(S["variant"]) if S.get("variant") else "base", caps=caps, max_own=varmod.MAX_OWN,
            rule_coins=" ".join(cfgmod.coin(c) for c in S.get("series") or []),
            minutes=", ".join(mins[:-1]) + " and " + mins[-1] if len(mins) > 1 else "".join(mins),
            band=f"{S['band_lo']*100:.0f}-{S['band_hi']*100:.0f}c", spread=f"{S['max_spread']*100:g}",
            contracts=S["contracts"], hour_cost=S["max_hour_cost"],
            days=G["min_days"], entries=G["min_entries"], pnl=G["min_pnl"],
            risk=R.get("max_risk", 100), order=R.get("max_order", 25), day=R.get("day_loss", 30),
            coins=" ".join(v.split("-")[0] for v in cfgmod.SERIES.values()), paper=((self.vrow(self.variant) or {}).get("rule") or {}).get("paper_capital") or cfgmod.Config().paper_capital,
            variants="\n".join(f"|  {n:<8} {d}" for n, d in shadowmod.DESC.items()),
            retired="\n".join(f"|  {n:<8} {d}" for n, d in shadowmod.RETIRED.items()))
        out: list[tuple[str, list]] = []
        width = max(40, min(HELP_WIDTH, w))
        for title, para in HELP:
            lines: list = []
            for chunk in para.format(**fill).split("\n"):
                if chunk.startswith("|"):                 # a literal line: a name, then what it is
                    m = re.match(r"(\s*\S+)(.*)$", chunk[1:])
                    name, rest = (m[1], m[2]) if m[2].strip() else ("", chunk[1:])
                    pad = " " * (len(name) + len(m[2]) - len(m[2].lstrip()))
                    for k, t in enumerate(textwrap.wrap(rest.strip(), width - len(pad))):
                        lines.append([(name + pad[len(name):] if k == 0 else pad, P(P.c("variant"), bold=True)), *self.log_spans(t, "", WHITE)])
                    continue
                lines += ([""] if lines else []) + textwrap.wrap(chunk, width)
                if chunk.endswith(":"):
                    lines.append("")
            out.append((title, lines))
        lines = []
        for num, name, what in HELP_BOXES:
            color = P.border(name) if name in BOXES else P.c("variant")
            room = max(20, w - 12)
            for k, t in enumerate(textwrap.wrap(what, room)):
                lines.append([(f"{num if k == 0 else '':<1}", P(P.c("key"), bold=True)), (f" {name if k == 0 else '':<9} ", P(color, bold=True)), (t, P(WHITE))])
        out.append(("BOXES", lines))
        lines = []
        cols = []
        for col in HELP_KEYS:
            kw = max(len(k) for _, keys in col for k, _ in keys) + 2      # this column's key width
            blocks = []
            for section, keys in col:
                blocks.append([[(section, P(P.c("head"), bold=True))]] +
                              [[("  " + f"{k:<{kw}}", P(P.c("key"), bold=True)),
                                (what.format(themes=len(themes.NAMES)), P(WHITE))] for k, what in keys])
            cols.append(blocks)
        width = max(sum(len(t) for t, _ in r) for b in cols[0] for r in b) + 3
        right_w = max(sum(len(t) for t, _ in r) for b in cols[1] for r in b)
        if width + right_w <= w:                          # side by side, sections stacked in each column
            left = [r for n, b in enumerate(cols[0]) for r in ([[("", 0)]] if n else []) + b]
            right = [r for n, b in enumerate(cols[1]) for r in ([[("", 0)]] if n else []) + b]
            for j in range(max(len(left), len(right))):
                l = left[j] if j < len(left) else []
                r = right[j] if j < len(right) else []
                lines.append(l + [(" " * (width - sum(len(t) for t, _ in l)), 0)] + r)
        else:                                                     # narrow: one column
            for n, b in enumerate(cols[0] + cols[1]):
                lines += ([[("", 0)]] if n else []) + b
        out.append(("KEYS", lines))
        cw = max(len(c) for c, _ in HELP_CLI) + 2
        lines = [[(f"{c:<{cw}}", P(P.c("ticker"))), (what, P(WHITE))] for c, what in HELP_CLI]
        out.append(("COMMANDS", lines + [[], [(f"runtime files: {cfgmod.RUNTIME}", P(P.grey))]]))
        return out

    def meter(self, frac: float, n: int) -> list:
        """A btop-style meter, n wide."""
        P, g = self.P, self.P.grad("yes")
        k = int(max(0.0, min(1.0, frac)) * n + 0.5)
        return [("■", P(g[i * (len(g) - 1) // max(1, n - 1)])) for i in range(k)] + [("■" * (n - k), P(P.t["fill"] if P.many else P.grey))]

    def gate_lines(self, w: int = 100) -> tuple[list, list]:
        """(the checks with how far each has come, what going live takes, the limits and the variants' heading; every
        variant, side by side)."""
        st, P = self.st, self.P
        lw = (w - 3) // 2
        head = lambda text, note="": [(text, P(P.c("head"), bold=True)), (note, P(P.grey))]
        box = lambda ok: ("[x] " if ok else "[ ] ", P(GREEN if ok else RED, bold=True))
        plain = P(WHITE)
        live, lg = self.live_variant(), st.get("live_gate") or {}
        name = self.vn(self.variant)
        left: list = [head("PAPER CHECKS", f"   {name} needs all of them"), []]
        short = {"days": "days traded", "entries": "settled entries", "pnl": "P&L"}
        for c in (st.get("gate") or {}).get("checks", []):
            if c.get("key") not in short:
                left.append([box(c["ok"]), (c["name"], plain)]); continue
            have, need = c["have"], c["need"]
            txt = f"{have:.1f} / {need:g}" if c["key"] == "days" else f"{have:,} / {need:,}" if c["key"] == "entries" else \
                f"{money(have, True)} / ${need:,.0f}"
            left.append([box(c["ok"]), (f"{short[c['key']]:<16}", plain), (f"{txt:>15}  ", P(GREEN if c["ok"] else YELLOW, bold=True)),
                         *self.meter(1.0 if c["ok"] else have / need if need > 0 else 0.0, max(6, lw - 40))])
        key = cfgmod.KEYFILE_ENV.replace(os.path.expanduser("~"), "~")
        right: list = [head("GO LIVE", "   all three are needed"), [],
                       [box(st.get("keys")), ("Kalshi key present ", plain), (key, P(P.c("ticker")))],
                       [box(st.get("live_enabled")), ("armed with L" + (f", {self.vn(live)}" if st.get("live_enabled") and live else ""), plain)],
                       [box(st.get("mode") == "live"), ("authenticated" + (f": {st.get('live_error')}" if st.get("live_error") else ""), plain)]]
        note: list = []
        if st.get("live_enabled") and not lg.get("ok", True):
            right.append([box(False), (f"live waits: {(lg.get('checks') or [{}])[0].get('name', '')}", P(YELLOW, bold=True))])
        elif live and live != self.variant:
            note.append((f"   live {'trades' if st.get('live_enabled') else 'is set to'} {self.vn(live)}", P(YELLOW)))
        elif not (st.get("live_cfg") or {}).get("auto_needs_gate", True):
            note.append(("   live.auto_needs_gate is false: the checks are not needed", P(YELLOW)))
        out: list = []
        for j in range(max(len(left), len(right))):
            l, r = (left[j] if j < len(left) else []), (right[j] if j < len(right) else [])
            out.append([*l, (" " * max(0, lw + 3 - sum(len(t) for t, _ in l)), 0), *r])
        r = (self.vrow(self.variant) or {}).get("rule") or {}
        out += [[], head("LIMITS") + self.rich(f"   ${r.get('max_risk', 0):,.0f} at risk · ${r.get('max_order', 0):,.0f} per order · "
                                               f"-${r.get('day_loss', 0):,.0f} a day turns live off") + note]
        rows, listed = st.get("shadow") or [], []
        if rows:
            table = shadowmod.table(rows)
            most = max([abs(r["pnl"]) for r in rows] + [1e-9])
            bar = w - len(table[0]) - 5
            out += [[], head("VARIANTS", "   side by side on paper"), [("  " + table[0], P(P.grey, bold=True))]]
            for j, (r, l) in enumerate(zip(rows, table[1:])):   # the name, the numbers down the gradient, P&L by its sign
                num = P(P.pick("rows", j / max(1, len(rows) - 1)))
                m = re.match(r"(\s*\S+)(.*)$", l)
                n = int(abs(r["pnl"]) / most * bar + 0.5)
                listed.append([("  " + m[1], P(P.c("variant"), bold=True))] +
                           [(t, P(GREEN, bold=True) if "+" in t else P(RED, bold=True) if re.match(r"-\d", t) else num)
                            for t in re.split(r"([+-]\d[\d.,]*)", m[2]) if t] +
                           [("   " + "■" * n, P(GREEN if r["pnl"] >= 0 else RED))])
        return out, listed

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

    def reports_panel(self, y, x, h, w) -> None:
        """The list on the left, and the report picked in it."""
        P = self.P
        entries = self.rep_entries()
        cur = next(r for r in entries if r.id == self.rep_sel)
        lw = REPORT_LIST_W
        self.box(y, x, h, w, "reports", P.border("reports"), PANEL_KEYS["reports"](self))
        self.rects = {"reports": (y, x, h, lw + 1), "report": (y, x + lw + 1, h, w - lw - 1)}
        self.split(y, x, h, "reports", lw)
        rows: list = []                                   # a heading, "" or an entry: LIVE, then SAVED
        for saved, title in ((False, "LIVE"), (True, "SAVED")):
            these = [r for r in entries if r.saved == saved]
            rows += ([""] if rows else []) + [title] + (these or ["  none yet"])
        room = h - 2
        at = next(i for i, r in enumerate(rows) if not isinstance(r, str) and r.id == self.rep_sel)
        self.rep_top = max(0, min(self.rep_top, len(rows) - room))
        self.rep_top = min(max(self.rep_top, at - room + 1), max(0, at - 1))   # the first entry: its heading too
        for j, r in enumerate(rows[self.rep_top:self.rep_top + room]):
            yy = y + 1 + j
            if isinstance(r, str):
                self.put(yy, x + 2, r, P(P.c("head"), bold=True) if r.isupper() else P(WHITE))
                continue
            sel = r.id == self.rep_sel
            name = r.label if len(r.label) <= lw - 5 else r.label[:lw - 6] + "…"
            self.put(yy, x + 1, f"   {name}".ljust(lw - 1), P.row(sel)(P.pick("rows", entries.index(r) / max(1, len(entries) - 1)), bold=sel))
            self.hit(yy, x + 1, x + lw, "report", r.id)
        if self.rep_top > 0:
            self.put(y + 1, x + lw - 1, "▲", P(YELLOW))
        if self.rep_top + room < len(rows):
            self.put(y + h - 2, x + lw - 1, "▼", P(YELLOW))
        # the report: what it is and where it comes from, then its lines
        rx, rw = x + lw + 2, w - lw - 4
        room = h - 4
        got = reports.FEED.get(self.runtime, cur.id)
        src = f"runtime/{cur.id[len('saved:'):]}" if cur.saved else \
            reports.command(cur.id) + (time.strftime(" · worked out %H:%M:%S", time.localtime(got.at)) if got else "")
        self.put(y + 1, rx, cur.label, P(P.c("head"), bold=True))
        self.put(y + 1, rx + len(cur.label) + 2, src, P(P.c("ticker")))
        if got is None:
            self.rep_room, self.rep_len = room, 0
            self.put(y + 3, rx, "reading the records…", P(WHITE)); return
        key = (got, rw, P.name, P.many)
        if not self.rep_drawn or self.rep_drawn[0][0] is not got or self.rep_drawn[0][1:] != key[1:]:
            self.rep_drawn = (key, self.report_spans(wrap_report(got.lines, rw)))
        lines = self.rep_drawn[1]
        self.rep_room, self.rep_len = room, len(lines)
        self.rep_off = off = max(0, min(self.rep_off, len(lines) - room))
        saved, self.clip = self.clip, x + w - 2
        for j, spans in enumerate(lines[off:off + room]):
            cx = rx
            for text, attr in spans:
                self.put(y + 3 + j, cx, text, attr); cx += len(text)
        self.clip = saved
        if off > 0:
            self.put(y + 3, x + w - 2, "▲", P(YELLOW))
        if off + room < len(lines):
            self.put(y + h - 2, x + w - 2, "▼", P(YELLOW))
        if len(lines) > room and not self.flashing():
            self.put(y + h - 1, rx, f" {off + 1}-{off + room} of {len(lines)} ", P(P.grey))

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

    def engine_key(self) -> None:
        """P: pause the engine (asks first): it saves and stops until it is started again. Or start it."""
        if engine_alive(self.runtime):
            self.modal = {"type": "stop", "title": "pause engine", "lines": [
                "Pause the engine? Nothing trades, paper or live, until it starts again",
                "(P here, or reopening the dash). It saves its state before it stops.", "",
                "y pauses · any other key keeps it running"]}
        else:
            def start() -> None:
                self.say(start_engine(self.runtime)); self.alive_at = 0
            live = self.live_variant() if self.st.get("live_enabled") else None
            self.confirm("start engine", ["Start the engine? The variants that are on trade on paper from the next slot.",
                                          *([f"Live is on: {self.vn(live)} would trade real money."] if live else [])],
                         start, "starts it", "engine not started", RED if live else YELLOW)

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
        if not self.shown("themes"):
            self.say(f"theme {self.P.name}" + ("" if self.P.many else " (needs a 256-color terminal)"))

    # ------------------------------------------------------------ settings
    def app_rows(self) -> list:
        """APP: pairing a phone, and a row for each phone paired (D removes it)."""
        if not self.app_rows_on:
            return []
        rows = [cfgmod.Setting("app", "pair", "pair a phone", kind="app", group="APP",
                               what="enter makes a 6-digit code to type into the app; then you check its key here and say yes")]
        for p in self.phones:
            when = time.strftime("%b %d", time.localtime(p["paired"]))
            used = "never" if not p["used"] else ago(time.time() - p["used"]) + " ago"
            rows.append(cfgmod.Setting("app", f"phone.{p['id']}", f"{p['name']} · paired {when} · last used {used}", kind="app",
                                       what=f"{p['name']}, a paired phone. D removes it: it can send nothing until it is paired again"))
        return rows

    def set_rows(self) -> list:
        """The box's rows: a variant's, or the desk's and the screen's."""
        if self.edit_var:
            return list(varmod.RULE_SETTINGS)
        return cfgmod.SETTINGS + self.app_rows() + SCREEN_SETTINGS

    def read_phones(self) -> None:
        self.phones, self.phones_at = self.devices.all(), time.time()

    def pairing(self) -> None:
        """Look at the pairing state: the phones paired (2 s), and a phone asking to be paired, which is put to you."""
        now = time.time()
        if now - self.phones_at > 2:
            try:
                self.read_phones()
            except (OSError, ValueError):
                self.phones_at = now
        if now - self.pair_at < 0.5:
            return
        self.pair_at = now
        m = self.modal
        if m and m.get("type") not in ("pair", "offer"):
            return                                           # another question is open: ask after it
        try:
            o = devmod.offered(self.fast, now)
        except (OSError, ValueError):
            return
        if o:
            if not m or m["type"] == "pair" or m["id"] != o["id"]:
                self.modal = {"type": "offer", "id": o["id"], "title": "pair a phone", "color": YELLOW,
                              "lines": [f"pair {o['name']}? its key: {o['words']} \u2014 the app shows the same", "",
                                        "y pairs \u00b7 any other key says no"]}
        elif m and m["type"] == "pair" and now > m["until"]:
            self.modal = None; self.say("the code ran out")
        elif m and m["type"] == "offer":
            self.modal = None; self.say("that pairing is over")

    def pair_start(self) -> None:
        now = time.time()
        try:
            code = devmod.new_code(self.fast, now)
        except OSError as ex:
            self.say(f"no code made: {ex}"); return
        self.modal = {"type": "pair", "title": "pair a phone", "color": CYAN, "code": code, "until": now + devmod.CODE_TTL,
                      "lines": ["", *big(code), "", "type it into the app \u00b7 it lasts 5 minutes \u00b7 esc closes"]}

    def app_row(self):
        """The settings row the cursor is on, if it is one of APP's."""
        rows = self.set_rows()
        row = rows[self.set_sel % len(rows)]
        return row if row.sec == "app" and not self.edit_var else None

    def phone_remove(self, row) -> None:
        pid = row.name[6:]
        name = next((p["name"] for p in self.phones if p["id"] == pid), "that phone")

        def remove() -> None:
            self.say(f"{name} removed" if self.devices.remove(pid) else f"{name} was already gone")
            self.read_phones()
        self.confirm(f"remove {name}", [f"Remove {name}? It can send nothing until it is paired again."], remove, "removes it",
                     f"{name} kept", RED)

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
            return " ".join(str(x) if row.kind == "slots" else cfgmod.coin(x) for x in v)
        if row.kind == "n":
            return f"{v:g}"
        return ("-" if v < 0 else "") + f"${abs(v):,.2f}".replace(".00", "")

    def settings_lines(self) -> list:
        P = self.P
        rows = self.set_rows()
        self.set_sel %= len(rows)
        lw = max(len(r.label) for r in rows if r.kind != "app")
        out: list = []
        self.set_at: list = []                           # for clicks: (line, row, its choices: from, to, which)
        for i, row in enumerate(rows):
            if row.group:
                out += ([[]] if out else []) + [[(" " + row.group, P(P.c("head"), bold=True))] + self.rule_head(row)]
            sel = i == self.set_sel
            spans: list = []
            if row.kind == "app":                        # an action, or a phone: a name and no value
                val = []
            elif sel and self.set_buf is not None:
                val = [(f" {self.set_buf}▏", P(YELLOW, bold=True) | curses.A_REVERSE)]
            elif row.kind in ("slots", "coins"):         # each choice, on bright and off grey; the cursor's reversed
                v = self.set_value(row.key) or []
                ch = self.set_choices(row)
                val = [(" ", P.row(sel)(WHITE))]
                for j, c in enumerate(ch):
                    label = str(c) if row.kind == "slots" else cfgmod.SERIES[c].split("-")[0]
                    on = c in v
                    cx = 2 + lw + 2 + sum(len(t) for t, _ in val)
                    spans.append((cx, cx + len(label) + 2, j))
                    val += [(f" {label} ", P.row(sel)(GREEN if on else P.grey, bold=on) |
                             (curses.A_REVERSE if sel and j == min(self.set_sub, len(ch) - 1) else 0))]
                if row.sec == "rule" and row.name in self.draft:
                    val += [(" *", P.row(sel)(YELLOW, bold=True))]
            else:
                v = self.set_value(row.key)
                changed = row.sec == "rule" and row.name in self.draft
                col = YELLOW if changed or row.key in self.set_sent else (GREEN if v else RED) if row.kind == "bool" else P.c("key")
                val = [(f" {self.set_fmt(row, v)} ", P(col, bold=True) | (curses.A_REVERSE if sel else 0))]
                if changed:                              # 19 → 10 *
                    val = [(f" {self.set_fmt(row, self.rule_now(row.name))} →", P.row(sel)(P.grey))] + val + \
                        [("*", P.row(sel)(YELLOW, bold=True))]
            if sel:                                      # the line the settings box keeps in view
                self.set_line = len(out)
            self.set_at.append((len(out), i, spans))
            R = P.row(sel)
            out.append([("▸ " if sel else "  ", R(P.c("key"), bold=True)),
                        (row.label if row.kind == "app" else f"{row.label:<{lw}}  ", R(P.pick("rows", i / max(1, len(rows) - 1)), bold=sel))] + val)
        return out

    def settings_about(self, w: int) -> list:
        """What the setting picked is, with examples worked from its values, what saving it does, and who uses what."""
        P = self.P
        rows = self.set_rows()
        row = rows[self.set_sel]
        out: list = [[(row.label, P(P.c("key"), bold=True))], []]
        looks = {"text": (WHITE, False), "head": (P.c("head"), True), "note": (P.grey, False), "fact": (P.c("value"), False)}
        wide = max(20, min(w, HELP_WIDTH))

        def say(text: str, color: int, bold: bool = False) -> None:
            out.extend([[(t, P(color, bold=bold))] for t in textwrap.wrap(text, wide)])

        def told(lines: list) -> None:
            for n, (style, text) in enumerate(lines):
                if n and (style in ("head", "text") or lines[n - 1][0] == "text" or style == "note" and lines[n - 1][0] == "fact"):
                    out.append([])                       # a paragraph; a heading's own lines follow it at once
                say(text, *looks[style])
        older = any(r.key not in (self.raw.get("settings") or {}) for r in rows if r.sec not in ("screen", "rule", "app")) or \
            self.edit_var and self.older_rows()
        r = {f.name: self.set_value(f.key) for f in varmod.RULE_SETTINGS} if self.edit_var else {}
        if row.sec == "rule" and not older and all(v is not None for f, v in r.items() if f not in ("cap", "max_vol")):
            me = self.vrow(self.edit_var) or {}
            told(aboutmod.rule(row.name, r, {"vol": self.vols(r["series"]), "price": self.price(r["series"][0]),
                                             "limits": me.get("paper_limits", False), "name": self.vn(self.edit_var)}))
        else:
            told(aboutmod.DESK.get(row.key) or [("text", row.what[:1].upper() + row.what[1:] + ".")])
        out.append([])
        saved = "Saved at once." if self.saves_here(row) else "View only here."
        if row.sec == "rule":
            say("←→ pick one · space or enter turns it on / off" if row.kind in ("slots", "coins") else
                f"{self.set_fmt(row, row.lo)} to {self.set_fmt(row, row.hi)}, steps of " + ("1¢" if row.kind == "p" else f"{row.step:g}"), P.grey)
        elif row.kind == "app":
            say("enter makes a code" if row.name == "pair" else "D removes it", P.grey)
        elif row.kind in ("usd", "n"):
            say(f"{self.set_fmt(row, row.lo)} to {self.set_fmt(row, row.hi)}, steps of {self.set_fmt(row, row.step)}. {saved}", P.grey)
        else:
            say("←→ or enter changes it. " + saved, P.grey)
        if row.sec == "web":                             # what the service is doing now, and where to open it
            ws = self.st.get("web_state") or {}
            st = ws.get("state", "unknown")
            now = {"active": ("running", GREEN), "inactive": ("stopped", P.grey),
                   "failed": ("failed: journalctl --user -u tern-web", RED),
                   "activating": ("starting… (stuck here: the port may be taken; journalctl --user -u tern-web)", YELLOW),
                   "deactivating": ("stopping…", YELLOW)}.get(st, (st, P.grey))
            out += [[], [("WEB SCREEN NOW", P(P.c("head"), bold=True))]]
            say(*now, True)
            if st == "active":
                out.extend([[(u, P(P.c("ticker")))] for u in ws.get("url", "").split(" · ")])
        if row.sec == "gate":
            out += self.gate_now(row, wide)
        if self.edit_var:
            changed = self.draft_lines()
            out += [[], [("CHANGES", P(P.c("head"), bold=True))]] + [[(t, P(YELLOW))] for t in changed or ["none yet"]]
            say(f"Nothing is saved until s: that makes a new variant, and {self.vn(self.edit_var)} stays as it is.", P.grey)
        if older:
            out.append([])
            say("The engine running is older than this screen: restart it (P, P) to change these.", YELLOW, True)
        elif self.edit_var:
            out += self.who_uses(row, wide)
        elif self.armed() and row.sec == "gate":
            out.append([])
            say("Live is armed: lowering a gate asks you to type \"live\".", YELLOW, True)
        return out

    def price(self, series: str) -> float | None:
        return ((self.st.get("spot") or {}).get(cfgmod.SERIES.get(series, "")) or {}).get("now")

    def vols(self, coins: list) -> dict:
        """The coins' volatility over the last hour, worked out as the engine does (shadow.vol). Only coins the engine fetches
        have one."""
        spot = self.st.get("spot") or {}
        return {c: shadowmod.vol((spot.get(cfgmod.SERIES.get(c, "")) or {}).get("min") or [], time.time()) for c in coins}

    def gate_now(self, row, w: int) -> list:
        """How far each variant is on one of the gate's checks."""
        P = self.P
        out: list = [[], [("VARIANTS NOW", P(P.c("head"), bold=True))]]
        rows = []
        for v in self.st.get("variants") or []:
            c = next((c for c in (v.get("gate") or {}).get("checks", []) if c["key"] == row.name[4:]), None)
            if c:
                have = c["have"]
                rows.append((self.vn(v["name"]), f"{have:.1f}" if c["key"] == "days" else f"{have:,}" if c["key"] == "entries" else money(have, True),
                             c["ok"], have / c["need"] if c["need"] > 0 else float(c["ok"])))
        nw, vw = (max([len(r[k]) for r in rows] + [0]) for k in (0, 1))
        bar = min(40, w - nw - vw - 8)
        for name, val, ok, frac in rows:
            out.append([("✓ " if ok else "  ", P(GREEN, bold=True)), (f"{name:<{nw}}  ", P(P.c("variant"))), (f"{val:>{vw}}  ", P(GREEN if ok else YELLOW))] +
                       (self.meter(1.0 if ok else frac, bar) if bar > 5 else []))
        return out

    def who_uses(self, row, w: int) -> list:
        """Every value the variants have for this setting, and whose it is. The draft's own is marked."""
        P = self.P
        out: list = [[], [("WHO USES WHAT", P(P.c("head"), bold=True))]]
        fmt = lambda v: self.set_fmt(row, v)
        by: dict[str, list] = {}
        for v in self.st.get("variants") or []:
            if row.name in (v.get("rule") or {}):
                val = v["rule"][row.name]
                by.setdefault(fmt(val), [val, []])[1].append(self.vn(v["name"]))
        mine = fmt(self.set_value(row.key))
        num = lambda x: (isinstance(x[1][0], list), x[1][0] if isinstance(x[1][0], (int, float)) else 0, x[0])
        vw = min(34, max([len(t) for t in by] + [len(mine)]))
        for text, (_, names) in sorted(by.items(), key=num):
            on = text == mine
            for k, t in enumerate(textwrap.wrap(" · ".join(names), max(10, w - vw - 4))):
                out.append([("▸ " if on and not k else "  ", P(P.c("key"), bold=True)),
                            (f"{text if not k else '':<{vw}}  ", P(P.c("key") if on else P.c("value"), bold=on)), (t, P(P.c("variant") if on else P.grey))])
        if mine not in by:
            out.append([("▸ ", P(P.c("key"), bold=True)), (f"{mine:<{vw}}  ", P(P.c("key"), bold=True)), ("none yet", P(P.grey))])
        return out

    def saves_here(self, row) -> bool:
        return True

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
        def change() -> None:
            self.send(op="set", key=key, value=value)
            self.set_sent[key] = (value, time.time())
        if self.clicked:
            self.confirm(f"change {row.label}", [f"Change {row.label} from {self.set_fmt(row, cur)} to {self.set_fmt(row, value)}?"],
                         change, "changes it", f"{row.label} kept")
        else:
            change()

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
        elif row.kind == "app":
            if row.name == "pair" and ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                self.pair_start()
            elif row.name != "pair" and ch == ord("D"):
                self.phone_remove(row)
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
        elif self.edit_var and ch == ord("s"):
            self.save_draft()
        elif self.edit_var and ch == 27:                                    # a draft goes only with esc, not with a stray key
            vn = self.var_new
            if vn and (self.edit_var, self.draft) == (vn["variant"], vn["draft"]):
                self.say("closed: it is being saved")
            elif self.draft:
                if self.clicked:
                    def drop() -> None:
                        self.say("changes thrown away"); self.close_edit()
                    self.confirm("discard changes", [f"Throw away the changes to {self.vn(self.edit_var)}?", *("  " + t for t in self.draft_lines())],
                                 drop, "throws them away", "changes kept")
                    return
                self.say("changes thrown away" + ("" if vn else ": nothing was saved"))
            self.close_edit()

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
        self.hits = []
        self.P.new_frame(self.tab)
        H, W = self.s.getmaxyx()
        if W < 100 or H < 30:
            self.put(0, 0, f"tern needs at least 100x30 (now {W}x{H})", self.P(RED, bold=True)); self.s.refresh(); return
        self.header(H, W)
        self.rects = {}
        self.pick_strike()
        self.rects[self.tab] = (0, 0, H - 1, W)          # the status line takes the last row
        self.clip, self.drawing = W - 1, self.tab
        getattr(self, self.tab + "_panel")(1, 0, H - 2, W)   # every box starts a blank line under its tabs
        self.clip, self.drawing = None, ""
        self.footer(H, W)
        if self.modal:
            self.draw_overlay(self.modal["lines"], self.modal["title"], self.modal.get("color", YELLOW))
        self.s.refresh()

    # ------------------------------------------------------------ input
    def press(self, name: str) -> bool:
        """A key by its name: what a click on it does."""
        return self.key(NAMED[name] if name in NAMED else ord(name))

    def click(self, my: int, mx: int) -> bool:
        """A left click: what is drawn there is shown, highlighted or pressed, as its key would. False closes the screen."""
        at = next((h for h in self.hits if h[0] == my and h[1] <= mx < h[2]), None)
        if at is None or self.modal:                     # a question takes keys only
            return True
        what, arg = at[3:]
        if what in ("key", "set"):
            self.clicked = True                          # what can't be taken back asks first
            try:
                return self.press(arg) is not False if what == "key" else self.click_setting(*arg)
            finally:
                self.clicked = False
        if what == "tab":
            if self.overlay == "edit":                   # a draft goes only with its own keys
                self.say("s saves the draft as a new variant, esc discards it")
            else:
                self.show_tab(arg)
        elif what == "variant":
            if arg == self.var_sel:
                return self.press("Enter") is not False
            self.var_sel = arg
        elif what == "ladder":
            self.sel = arg; self.write_view()
        elif what == "report":
            self.pick_report(arg)
        elif what == "help":
            self.scroll("help", arg - self.help_sel)
        elif what == "theme":
            self.P.set_theme(arg); self.save_prefs()
        elif what == "book":
            self.book_view = arg
        return True

    def confirm(self, title: str, lines: list, yes, does: str, kept: str, color: int = YELLOW) -> None:
        """Ask: y runs `yes` (False from it closes the screen), any other key says `kept`."""
        self.modal = {"type": "confirm", "title": title, "color": color, "yes": yes, "kept": kept,
                      "lines": [*lines, "", f"y {does} · any other key doesn't"]}

    def click_setting(self, i: int, j: int | None) -> bool:
        """A click on settings row i (its choice j): highlighted, or enter when it already was."""
        if self.set_buf is not None and i != self.set_sel:
            self.say("enter saves the value being typed, esc leaves it")
            return True
        choices = self.set_rows()[i].kind in ("slots", "coins")
        if i == self.set_sel and (j == self.set_sub if choices else True):
            return self.press("Enter") is not False
        if i != self.set_sel or j is not None:
            self.set_sel, self.set_sub = i, j or 0
        return True

    def key(self, ch: int) -> bool:
        if ch == curses.KEY_MOUSE:
            events = []                                  # one KEY_MOUSE can stand for several, handed out newest first
            for _ in range(MOUSE_QUEUE):
                try:
                    events.append(curses.getmouse())
                except curses.error:
                    break
            for _, mx, my, _, bs in reversed(events):
                d = -1 if bs & curses.BUTTON4_PRESSED else 1 if bs & curses.BUTTON5_PRESSED else 0
                if d and not self.modal:
                    self.wheel(my, mx, d)
                elif bs & curses.BUTTON1_PRESSED and not self.click(my, mx):
                    return False
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
            if m.get("type") == "pair":
                if ch == 27:
                    self.say("pairing closed: the code still works until it runs out")
                else:
                    self.modal = m
                return True
            if m.get("type") == "offer":
                try:
                    self.say(devmod.answer(self.fast, m["id"], ch in (ord("y"), ord("Y")), self.devices))
                except OSError as ex:
                    self.say(f"not answered: {ex}")
                self.read_phones()
                return True
            if m.get("type") == "confirm":
                if ch in (ord("y"), ord("Y")):
                    return m["yes"]() is not False
                self.say(m["kept"])
                return True
            if m.get("type") == "stop":
                if ch in (ord("y"), ord("Y")):
                    self.say(stop_engine(self.runtime, self.st.get("pid"))); self.alive_at = 0
                else:
                    self.say("engine kept running")
                return True
            return True
        if ch in (ord("t"), ord("T")) and self.set_buf is None:
            self.next_theme(-1 if ch == ord("T") else 1)
            return True
        if self.overlay == "edit":
            if ch in (curses.KEY_PPAGE, curses.KEY_NPAGE) and self.set_buf is None:
                self.scroll("about", -10 if ch == curses.KEY_PPAGE else 10)
            else:
                self.settings_key(ch)
            return True
        tab_key = ch in (9, curses.KEY_BTAB)
        if self.tab == "settings" and not tab_key and (self.set_buf is not None or ch in (
                curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT, ord("j"), ord("k"), ord("-"), ord("+"),
                ord("="), ord(" "), 10, 13, curses.KEY_ENTER) or ch == ord("D") and self.app_row()):
            self.settings_key(ch)                        # (a value being typed takes every key but tab)
            return True
        if ch in (ord("q"), ord("Q")):
            if not self.clicked:
                return False
            self.confirm("close", ["Close this screen? The engine keeps going."], lambda: False, "closes it", "screen kept open")
            return True
        if ch == curses.KEY_ENTER:
            ch = 10
        if self.tab not in KEYS_ON.get(chr(ch) if 0 <= ch < 128 else "", BOXES):   # another box's key: nothing here
            return True
        if ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, ord("k"), ord("j")):
            page = ch in (curses.KEY_PPAGE, curses.KEY_NPAGE)
            self.scroll({"reports": "report", "help": "helptext", "settings": "about"}.get(self.tab, self.tab) if page else self.tab, {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                                     curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10}[ch])
        elif ch in (curses.KEY_HOME, curses.KEY_END):
            self.scroll({"reports": "report", "help": "helptext"}.get(self.tab, self.tab), -10 ** 6 if ch == curses.KEY_HOME else 10 ** 6)
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT) and self.tab == "themes":
            self.scroll("themes", -self.theme_rows if ch == curses.KEY_LEFT else self.theme_rows)
        elif ch in (10, 13):
            self.select_variant()
        elif ch in (ord("D"), ord(" ")):
            self.variant_key(ch)
        elif ch == ord("r"):
            self.rename_ask()
        elif 0 < ch < 128 and chr(ch) in BOX_KEYS:
            self.show_tab(BOXES[BOX_KEYS.index(chr(ch))])
        elif tab_key:
            self.show_tab(BOXES[(BOXES.index(self.tab) + (-1 if ch == curses.KEY_BTAB else 1)) % len(BOXES)])
        elif ch == ord("C"):
            order = list(cfgmod.SERIES)
            self.series = order[(order.index(self.series) + 1) % len(order)] if self.series in order else order[0]
            self.sel = None; self.write_view(); self.save_prefs(); self.say(f"viewing {cfgmod.SERIES[self.series]}")
        elif ch == ord("c"):
            order = list(CHARTS)
            self.chart = order[(order.index(self.chart) + 1) % len(order)] if self.chart in order else "15m"
            self.save_prefs()
        elif ch == ord("V"):
            cur = self.book_view or ("live" if self.st.get("mode") == "live" else "paper")
            self.book_view = "live" if cur == "paper" else "paper"
        elif ch == ord("e"):
            self.edit_variant()
        elif ch == ord("P"):
            self.engine_key()
        elif ch == ord("s"):
            self.save_report()
        elif ch == ord("L"):
            self.live_key()
        return True

    def loop(self) -> None:
        curses.curs_set(0)
        self.s.nodelay(True)
        self.s.keypad(True)
        curses.set_escdelay(25)
        # The wheel scrolls the panel under the pointer, and a left press clicks; shift+drag still selects text in foot.
        curses.mousemask(MOUSE_EVENTS)
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
