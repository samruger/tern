"""tern web: the screen in a browser, view only.

The browser does not draw its own copy of the dash. Each browser tab gets a real dash.Screen
drawing into a virtual terminal here; the cells go to the page, which paints them like foot does.
So the web view always looks and behaves like the screen, minus the keys that change anything:
no quit, engine stop or start, paper reset, live, rule coins or picking a variant. It sends
no command files and never writes the screen's own view.json or dash.json. Its viewers' coins
and strikes go to view-web.json, so the engine also sends those strikes' contract history.
"""

from __future__ import annotations

import curses
import glob
import gzip
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as cfgmod
from . import dash
from .engine import live_dir

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web.html")

# keys the page sends by name (KeyboardEvent.key, plus BackTab for shift+tab) -> what curses would give
NAMED = {"ArrowUp": curses.KEY_UP, "ArrowDown": curses.KEY_DOWN, "ArrowLeft": curses.KEY_LEFT,
         "ArrowRight": curses.KEY_RIGHT, "PageUp": curses.KEY_PPAGE, "PageDown": curses.KEY_NPAGE,
         "Home": curses.KEY_HOME, "End": curses.KEY_END, "Tab": 9, "BackTab": curses.KEY_BTAB,
         "Enter": 10, "Escape": 27, "Backspace": 127}
# keys that only change what this viewer sees: they reach the screen; everything else stops here
VIEW_KEYS = set("123456jktTcvwgh?") | {"ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown",
                                       "Tab", "BackTab"}
CONTROL_KEYS = {"q": "quit", "Q": "quit", "s": "engine start/stop", "R": "paper reset", "L": "live",
                "r": "rule coins", "Enter": "picking a variant"}
# border hints whose key changes something: not drawn on the web
CONTROL_HINTS = {"r", "L", "R", "s", "q", "⏎"}
# what a hint on a border sends when clicked
HINT_KEY = {"tab": "Tab", "any key": "Escape", "esc": "Escape", "↑↓": None}

SESSION_IDLE = 120.0     # seconds without a request before a browser tab's screen is dropped
VIEW_REFRESH = 10.0      # view-web.json is rewritten at least this often while anyone watches
MIN_W, MIN_H, MAX_W, MAX_H = 100, 30, 400, 200

_PAL_SWAP = threading.Lock()


class VTerm:
    """Just enough of a curses window for dash.Screen: a grid of (char, attr) cells."""

    def __init__(self, h: int, w: int):
        self.resize(h, w)

    def resize(self, h: int, w: int) -> None:
        self.h, self.w = h, w
        self.erase()

    def getmaxyx(self) -> tuple[int, int]:
        return self.h, self.w

    def erase(self) -> None:
        self.ch = [[" "] * self.w for _ in range(self.h)]
        self.at = [[0] * self.w for _ in range(self.h)]

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if not 0 <= y < self.h:
            raise curses.error("off screen")
        row, ats = self.ch[y], self.at[y]
        for i, c in enumerate(text):
            if 0 <= x + i < self.w:
                row[x + i], ats[x + i] = c, attr

    insstr = addstr

    def refresh(self) -> None:
        pass

    def runs(self) -> list[list[list]]:
        """Each row as [text, attr] runs of one attribute."""
        out = []
        for row, ats in zip(self.ch, self.at):
            r, i = [], 0
            while i < self.w:
                j = i
                while j < self.w and ats[j] == ats[i]:
                    j += 1
                r.append(["".join(row[i:j]), ats[i]])
                i = j
            out.append(r)
        return out


class WebPal(dash.Pal):
    """The screen's palette without a terminal: an attr is the 256-color index + 1 in its low 9 bits,
    plus curses' own bold / dim / reverse bits. The page decodes it."""

    def __init__(self):
        self.bg = -1
        self.many = True
        self.cache = {}
        self.set_theme(None)

    def __call__(self, fg: int, bold: bool = False, dim: bool = False) -> int:
        fg = self.t["base"].get(fg, fg)
        a = (fg + 1) & 0x1FF
        if bold:
            a |= curses.A_BOLD
        if dim:
            a |= curses.A_DIM
        return a


def clean_prefs(d) -> dict:
    """The parts of a dash.json-shaped dict the screen accepts, checked the way Screen.__init__ checks them."""
    out = {}
    if not isinstance(d, dict):
        return out
    if isinstance(d.get("hidden"), list):
        out["hidden"] = sorted(set(x for x in d["hidden"] if isinstance(x, str)) & set(dash.PANELS))
    for k, ok in (("series", cfgmod.SERIES), ("chart", dash.CHARTS), ("slot3", dash.SLOT3)):
        if d.get(k) in ok:
            out[k] = d[k]
    if isinstance(d.get("theme"), str) and d["theme"] in dash.themes.THEMES:
        out["theme"] = d["theme"]
    if "watch" in d and (d["watch"] is None or isinstance(d["watch"], str)):
        out["watch"] = d["watch"]
    return out


class WebScreen(dash.Screen):
    """dash.Screen for one browser tab. Reads come from the hub; nothing is written to the runtime."""

    def __init__(self, hub: "Hub", h: int, w: int, prefs: dict | None = None):
        with _PAL_SWAP:                          # Screen.__init__ builds a Pal: make it the terminal-free one
            real, dash.Pal = dash.Pal, WebPal
            try:
                super().__init__(VTerm(h, w), hub.runtime)   # starts from the screen's own dash.json
            finally:
                dash.Pal = real
        self.hub = hub
        self.hits: list[tuple[int, int, int, str]] = []    # (y, x0, x1, key) of clickable hints and box numbers
        self.rowmap: dict[str, dict[int, str]] = {}         # box -> screen row -> ladder ticker / variant name
        self.apply_prefs(prefs or {})
        self.prefs = self.current_prefs()

    # ------------------------------------------------------------ no writes, shared reads
    def apply_prefs(self, d: dict) -> None:
        d = clean_prefs(d)
        if "hidden" in d:
            self.hidden = set(d["hidden"])
        if "series" in d:
            self.series, self.sel = d["series"], None
        if "chart" in d:
            self.chart = d["chart"]
        if "watch" in d:
            self.watch = d["watch"]
        if "slot3" in d:
            self.slot3 = d["slot3"]
            self.focus = "variants" if self.slot3 == "variants" else "ladder"
        if "theme" in d:
            self.P.set_theme(d["theme"])

    def current_prefs(self) -> dict:
        return {"hidden": sorted(self.hidden), "series": self.series, "chart": self.chart, "theme": self.P.name,
                "slot3": self.slot3, "watch": self.watch}

    def save_prefs(self) -> None:                # kept by the browser (localStorage), not in dash.json
        self.prefs = self.current_prefs()

    def write_view(self) -> None:                # the hub tells the engine, in view-web.json
        self.hub.publish()

    def send(self, **cmd) -> None:               # unreachable: every key that sends is filtered out
        self.say("view only: the web screen sends no commands")

    def read(self) -> None:
        self.alive, self.alive_at = self.hub.alive(), time.time()
        self.st = self.hub.status()

    # ------------------------------------------------------------ drawing, with click targets
    def draw(self) -> None:
        self.hits, self.rowmap = [], {}
        super().draw()

    def box(self, y, x, h, w, title, color, key="", right=()) -> None:
        segs = [right] if isinstance(right, str) else list(right)
        segs = [s for s in segs if isinstance(s, str) or s[0] not in CONTROL_HINTS]
        key = key or self.panel_key
        if key in dash.SUPER:                    # the box number: a click shows / hides it, as 1-6 do
            self.hits.append((y, x + 3, x + 3 + len(key), str(dash.SUPER.index(key) + 1)))
        super().box(y, x, h, w, title, color, key, segs)

    def hint(self, y, x, seg, border) -> None:
        if not isinstance(seg, str):
            k = HINT_KEY.get(seg[0], seg[0])
            if k:
                self.hits.append((y, x + 1, x + 1 + sum(map(len, seg)), k))
        super().hint(y, x, seg, border)

    def footer(self, H: int, W: int) -> None:
        with _PAL_SWAP:                          # one tab at a time swaps the module's table
            real = dash.PANEL_KEYS
            dash.PANEL_KEYS = {n: (lambda fn: lambda s: [g for g in fn(s) if g[0] not in CONTROL_HINTS])(fn)
                               for n, fn in real.items()}   # hidden boxes' hints, without the control ones
            try:
                super().footer(H, W)
            finally:
                dash.PANEL_KEYS = real

    def ladder_panel(self, y, x, h, w) -> None:
        super().ladder_panel(y, x, h, w)
        lad = self.ladder()
        tickers = [r["t"] for r in lad]
        if self.sel in tickers:
            rows = h - 3
            top = max(0, min(len(lad) - rows, tickers.index(self.sel) - rows // 2))
            self.rowmap["ladder"] = {y + 2 + j: t for j, t in enumerate(tickers[top:top + rows])}

    def variants_panel(self, y, x, h, w) -> None:
        super().variants_panel(y, x, h, w)
        names = [v["name"] for v in self.st.get("variants") or []]
        if names:
            rows = max(1, min(len(names), h - 3))
            self.rowmap["variants"] = {y + 2 + j: n for j, n in enumerate(names[self.var_top:self.var_top + rows])}

    # ------------------------------------------------------------ input from the page
    def press(self, name: str) -> None:
        code = NAMED.get(name, ord(name) if len(name) == 1 else None)
        if code is None:
            return
        if self.overlay:                         # help / gate: scroll keys scroll, any other key closes
            self.key(code); return
        if name in VIEW_KEYS:
            self.key(code)
        elif name in CONTROL_KEYS:
            self.say(f"view only: {CONTROL_KEYS[name]} is done from the screen in the terminal")

    def wheel_at(self, cy: int, cx: int, d: int) -> None:
        if self.overlay:
            self.ov_off = max(0, self.ov_off + 3 * d)
        else:
            self.wheel(cy, cx, d)

    def click(self, cy: int, cx: int) -> None:
        for y, x0, x1, k in self.hits:
            if y == cy and x0 <= cx < x1:
                self.press(k); return
        if self.overlay:
            self.overlay = None; return
        for name, rows in self.rowmap.items():
            r = self.rects.get(name)
            if r and cy in rows and r[1] < cx < r[1] + r[3] - 1:
                self.focus = name
                if name == "ladder":
                    self.sel = rows[cy]; self.write_view()
                else:
                    self.var_sel = rows[cy]
                return
        for name, (y, x, h, w) in self.rects.items():
            if y <= cy < y + h and x <= cx < x + w and name in dash.SCROLLS:
                self.focus = name; return

    def frame(self, h: int, w: int) -> dict:
        if (h, w) != self.s.getmaxyx():
            self.s.resize(h, w)
        self.read()
        self.draw()
        return {"rows": self.s.runs(), "prefs": self.prefs}


class Hub:
    """The browser tabs' screens, one shared parse of the status file, and view-web.json for the engine."""

    def __init__(self, runtime: str):
        self.runtime = runtime
        self.fast = live_dir(runtime)
        self.lock = threading.Lock()
        self.sessions: dict[str, list] = {}     # sid -> [screen, lock, last seen]
        self.st: dict = {}
        self.stamp = None
        self.checked = 0.0
        self.alive_v, self.alive_at = False, 0.0
        self.view_sent, self.view_at = None, 0.0

    def status(self) -> dict:
        with self.lock:
            if time.time() - self.checked > 0.2:
                self.checked = time.time()
                try:
                    with open(os.path.join(self.fast, "status.json")) as fh:
                        s = os.fstat(fh.fileno())
                        stamp = (s.st_ino, s.st_mtime_ns, s.st_size)
                        if stamp != self.stamp:
                            self.st, self.stamp = json.load(fh), stamp
                except (OSError, json.JSONDecodeError):
                    pass
            return self.st

    def alive(self) -> bool:
        """Whether the engine's process is there. The screen asks the engine lock; the web reads /proc,
        so it never touches the lock the engine takes on start."""
        if time.time() - self.alive_at > 1:
            self.alive_at = time.time()
            pid = self.st.get("pid")
            try:
                with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
                    self.alive_v = b"tern" in fh.read() and not self.st.get("stopped")
            except (OSError, TypeError, ValueError):
                self.alive_v = False
        return self.alive_v

    def session(self, sid: str | None, h: int, w: int, prefs: dict | None) -> tuple[str, list]:
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.sessions.items() if now - v[2] > SESSION_IDLE]:
                del self.sessions[k]
            if sid in self.sessions:
                s = self.sessions[sid]
                s[2] = now
                return sid, s
        scr = WebScreen(self, h, w, prefs)
        sid = uuid.uuid4().hex
        with self.lock:
            s = self.sessions[sid] = [scr, threading.Lock(), now]
        return sid, s

    def publish(self, force: bool = False) -> None:
        """Write the viewers' coins and strikes for the engine, when they change and every few seconds."""
        with self.lock:
            scrs = [s[0] for s in self.sessions.values()]
        series = sorted({s.series for s in scrs})
        tickers = sorted({s.sel for s in scrs if s.sel})
        view = {"series": series, "tickers": tickers}
        if not scrs or (view == self.view_sent and time.time() - self.view_at < VIEW_REFRESH and not force):
            return
        self.view_sent, self.view_at = view, time.time()
        tmp = os.path.join(self.fast, "view-web.json.tmp")
        try:
            with open(tmp, "w") as fh:
                json.dump(view, fh)
            os.replace(tmp, os.path.join(self.fast, "view-web.json"))
        except OSError:
            pass

    def handle(self, req: dict) -> dict:
        w = max(MIN_W, min(MAX_W, int(req.get("w") or MIN_W)))
        h = max(MIN_H, min(MAX_H, int(req.get("h") or MIN_H)))
        sid, (scr, lock, _) = self.session(req.get("sid"), h, w, req.get("prefs"))
        with lock:
            if (h, w) != scr.s.getmaxyx():
                scr.s.resize(h, w)
            scr.read()
            if isinstance(req.get("key"), str):
                scr.press(req["key"][:16])
            if isinstance(req.get("wheel"), dict):
                ev = req["wheel"]
                scr.wheel_at(int(ev.get("y", 0)), int(ev.get("x", 0)), 1 if (ev.get("d") or 0) > 0 else -1)
            if isinstance(req.get("click"), dict):
                ev = req["click"]
                scr.click(int(ev.get("y", 0)), int(ev.get("x", 0)))
            out = scr.frame(h, w)
        self.publish()
        return {"sid": sid, **out}


# ---------------------------------------------------------------- the page's look: foot's colors and font

def _foot_colors() -> dict:
    """foot's 16 colors, foreground and background, read from its config (and the omarchy theme it includes)."""
    want = {"foreground": "a9b1d6", "background": "1a1b26"}
    base = ["1a1b26", "f7768e", "9ece6a", "e0af68", "7aa2f7", "ad8ee6", "449dab", "a9b1d6",
            "414868", "ff7a93", "b9f27c", "ff9e64", "7da6ff", "bb9af7", "0db9d7", "c0caf5"]
    font, size, pad = "JetBrainsMono Nerd Font", 11.0, 14

    def read(path: str, seen: set) -> None:
        nonlocal font, size, pad
        path = os.path.expanduser(path)
        if path in seen or not os.path.isfile(path):
            return
        seen.add(path)
        for line in open(path, errors="replace"):
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            k, v = (p.strip() for p in line.split("=", 1))
            if k == "include":
                read(v, seen)
            elif k in want and re.fullmatch(r"[0-9a-fA-F]{6}", v):
                want[k] = v
            elif re.fullmatch(r"(regular|bright)[0-7]", k) and re.fullmatch(r"[0-9a-fA-F]{6}", v):
                base[int(k[-1]) + (8 if k.startswith("bright") else 0)] = v
            elif k == "font":
                name, _, rest = v.partition(":")
                font = name.strip() or font
                m = re.search(r"size=([0-9.]+)", rest)
                size = float(m.group(1)) if m else size
            elif k == "pad":
                m = re.match(r"(\d+)", v)
                pad = int(m.group(1)) if m else pad
    read("~/.config/foot/foot.ini", set())
    return {"fg": "#" + want["foreground"], "bg": "#" + want["background"], "base": ["#" + c for c in base],
            "font": font, "size_pt": size, "pad": pad}


def _font_files() -> dict[str, str]:
    out = {}
    for style, pat in (("regular", "*JetBrainsMono*Nerd*Font-Regular.ttf"), ("bold", "*JetBrainsMono*Nerd*Font-Bold.ttf"),
                       ("regular", "*JetBrainsMono-Regular.ttf"), ("bold", "*JetBrainsMono-Bold.ttf")):
        if style not in out:
            hit = sorted(glob.glob(os.path.join("/usr/share/fonts", "**", pat), recursive=True))
            if hit:
                out[style] = hit[0]
    return out


def serve(runtime: str = cfgmod.RUNTIME, host: str = "127.0.0.1", port: int = 8787) -> None:
    hub = Hub(runtime)
    look = {**_foot_colors(), "attr": {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE}}
    fonts = _font_files()
    look["fonts"] = sorted(fonts)

    class H(BaseHTTPRequestHandler):
        server_version = "tern-web"

        def log_message(self, format, *args):           # quiet: a request every quarter second per tab
            pass

        def reply(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, 5)
                self.send_response(code); self.send_header("Content-Encoding", "gzip")
            else:
                self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                with open(PAGE, "rb") as fh:
                    self.reply(200, fh.read(), "text/html; charset=utf-8")
            elif path == "/look":
                self.reply(200, json.dumps(look).encode(), "application/json")
            elif path.startswith("/font/") and path[6:] in fonts:
                with open(fonts[path[6:]], "rb") as fh:
                    self.reply(200, fh.read(), "font/ttf", "max-age=86400")
            else:
                self.reply(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/frame":
                self.reply(404, b"not found", "text/plain"); return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("not an object")
                body = json.dumps(hub.handle(req), separators=(",", ":"), ensure_ascii=False).encode()
            except (ValueError, TypeError) as ex:
                self.reply(400, str(ex).encode(), "text/plain"); return
            self.reply(200, body, "application/json; charset=utf-8")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    shown = "localhost" if host in ("127.0.0.1", "::1", "localhost") else host
    print(f"tern web: http://{shown}:{port}/  (view only; ctrl-c stops it, the engine keeps running)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
