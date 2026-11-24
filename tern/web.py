"""tern web: the screen in a browser, view only.

The browser does not draw its own copy of the dash. Each browser tab gets a real dash.Screen
drawing into a virtual terminal here; the cells go to the page, which paints them like foot does.
So the web view always looks and behaves like the screen, minus the keys that change anything:
no quit, engine stop or start, or live, and no making, renaming, turning on/off or deleting a
variant. Selecting one (enter) only views it, for that viewer. The settings box opens and its cursor moves,
but only its log filter row changes (for that viewer). Reports can be read but not saved. It sends
no command files and never writes the screen's own view.json or dash.json. Its viewers' coins
and strikes go to view-web.json, so the engine also sends those strikes' contract history.
"""

from __future__ import annotations

import curses
import glob
import gzip
import html
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import uuid
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import config as cfgmod
from . import dash
from . import webauth
from .engine import live_dir

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web.html")

# keys the page sends by name (KeyboardEvent.key, plus BackTab for shift+tab) -> what curses would give
NAMED = {"ArrowUp": curses.KEY_UP, "ArrowDown": curses.KEY_DOWN, "ArrowLeft": curses.KEY_LEFT,
         "ArrowRight": curses.KEY_RIGHT, "PageUp": curses.KEY_PPAGE, "PageDown": curses.KEY_NPAGE,
         "Home": curses.KEY_HOME, "End": curses.KEY_END, "Tab": 9, "BackTab": curses.KEY_BTAB,
         "Enter": 10, "Escape": 27, "Backspace": 127}
# keys that only change what this viewer sees: they reach the screen; everything else stops here
VIEW_KEYS = set("1234567890hjkcCVe") | {"Enter", "Escape", "Home", "End", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown",
                                       "Tab", "BackTab"}
CONTROL_KEYS = {"q": "quit", "Q": "quit", "P": "engine pause/start", "s": "saving a report", "L": "live",
                "r": "renaming", " ": "turning a variant on or off",
                "D": "deleting"}
# border hints whose key changes something: not drawn on the web (⏎ selects a variant: it only views)
CONTROL_HINTS = {"r", "L", "P", "q", "space", "D"}
# what a hint on a border sends when clicked
HINT_KEY = {"tab": "Tab", "any key": "Escape", "esc": "Escape", "↑↓": None, "←→": None, "⏎": "Enter"}

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

    def on(self, fg: int, bg: int, bold: bool = False) -> int:
        """fg on bg: the background's index + 1 is in bits 9-17 (under curses' own bits, the ones the page reads)."""
        return self(fg, bold=bold) | (bg + 1) << 9


def clean_prefs(d) -> dict:
    """The parts of a dash.json-shaped dict the screen accepts, checked the way Screen.__init__ checks them."""
    out = {}
    if not isinstance(d, dict):
        return out
    for k, ok in (("series", cfgmod.SERIES), ("chart", dash.CHARTS)):
        if d.get(k) in ok:
            out[k] = d[k]
    if isinstance(d.get("theme"), str) and d["theme"] in dash.themes.THEMES:
        out["theme"] = d["theme"]
    if "variant" in d and (d["variant"] is None or isinstance(d["variant"], str) and len(d["variant"]) <= 64):
        out["variant"] = d["variant"]           # one since deleted falls back to live's on the next read
    if d.get("log_filter") in dash.LOG_FILTERS:
        out["log_filter"] = d["log_filter"]
    if d.get("tab") in dash.BOXES:
        out["tab"] = d["tab"]
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
        self.hits: list[tuple[int, int, int, str]] = []    # (y, x0, x1, key) of clickable tabs and hints
        self.rowmap: dict[str, dict[int, str]] = {}         # box -> screen row -> ladder ticker / variant name
        self.apply_prefs(prefs or {})
        self.prefs = self.current_prefs()

    # ------------------------------------------------------------ no writes, shared reads
    def apply_prefs(self, d: dict) -> None:
        d = clean_prefs(d)
        if "series" in d:
            self.series, self.sel = d["series"], None
        if "chart" in d:
            self.chart = d["chart"]
        if "variant" in d:
            self.variant = d["variant"]
        if "theme" in d:
            self.P.set_theme(d["theme"])
        if "tab" in d:
            self.tab = d["tab"]
        if "log_filter" in d:
            self.log_filter, self.log_anchor = d["log_filter"], None

    def current_prefs(self) -> dict:
        return {"series": self.series, "chart": self.chart, "theme": self.P.name,
                "variant": self.variant, "log_filter": self.log_filter, "tab": self.tab}

    def save_prefs(self) -> None:                # kept by the browser (localStorage), not in dash.json
        self.prefs = self.current_prefs()

    def write_view(self) -> None:                # the hub tells the engine, in view-web.json
        self.hub.publish()

    def send(self, **cmd) -> str:                # unreachable: every key that sends is filtered out
        self.say("view only: the web screen sends no commands")
        return ""

    def send_setting(self, row, value) -> None:  # the settings box shows, but only its log row changes here
        if row.key == "screen.log":              # just this viewer's log box, kept with its other prefs
            return super().send_setting(row, value)
        self.say("view only: settings are changed from the screen in the terminal")

    def edit_variant(self) -> None:              # e on the variants
        self.say("view only: making a variant is done from the screen in the terminal")

    def save_report(self) -> None:
        self.say("view only: reports are saved from the screen in the terminal")

    def engine_key(self) -> None:                # reached from the reports (o): elsewhere press() stops it
        self.say("view only: engine start/stop is done from the screen in the terminal")

    def settings_lines(self) -> list:
        out = super().settings_lines()
        if self.set_rows()[self.set_sel].key == "screen.log":
            return out                           # this row really is saved at once
        for line in out:                         # its "saved at once" hints don't hold here
            if isinstance(line, list) and len(line) == 1 and "saved at once" in line[0][0]:
                t = line[0][0]
                line[0] = (t.replace("saved at once", "view only here").ljust(len(t)), line[0][1])
        return out

    def read(self) -> None:
        self.alive, self.alive_at = self.hub.alive(), time.time()
        self.raw = self.hub.status()
        self.apply_selected()                    # the variant this viewer selected, as the terminal screen does

    # ------------------------------------------------------------ drawing, with click targets
    def draw(self) -> None:
        self.hits, self.rowmap = [], {}
        super().draw()

    def box(self, y, x, h, w, title, color, right=()) -> None:
        segs = [right] if isinstance(right, str) else list(right)
        segs = [s for s in segs if isinstance(s, str) or not set(s[0::2]) & CONTROL_HINTS and s[:2] not in (("e", "dit"), ("s", "ave"))]   # any key piece
        super().box(y, x, h, w, title, color, segs)

    def tab_seg(self, y, x, i, name, color, gap) -> int:
        end = super().tab_seg(y, x, i, name, color, gap)
        self.hits.append((y, x, end, dash.BOX_KEYS[i]))   # a click shows that box, as its key does
        return end

    def hint(self, y, x, seg, border) -> None:
        if not isinstance(seg, str):
            k = HINT_KEY.get(seg[0], seg[0]) or (seg[2] if len(seg) > 2 and not seg[0] else None)   # ("", "rep", "o", "rts")
            if k:
                self.hits.append((y, x + 1, x + 1 + sum(map(len, seg)), k))
        super().hint(y, x, seg, border)

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
        if name in VIEW_KEYS or self.tab == "settings" and name in (" ", "+", "-", "="):   # (a setting: send_setting)
            self.key(code)
        elif name in CONTROL_KEYS:
            self.say(f"view only: {CONTROL_KEYS[name]} is done from the screen in the terminal")

    def wheel_at(self, cy: int, cx: int, d: int) -> None:
        self.wheel(cy, cx, d)

    def click(self, cy: int, cx: int) -> None:
        for y, x0, x1, k in self.hits:
            if y == cy and x0 <= cx < x1:
                self.press(k); return
        if self.tab == "reports":                # a listed report is picked
            if cx <= dash.REPORT_LIST_W and cy in self.rep_rows:
                self.pick_report(self.rep_rows[cy])
            return
        for name, rows in self.rowmap.items():
            r = self.rects.get(name)
            if r and cy in rows and r[1] < cx < r[1] + r[3] - 1:
                if name == "ladder":
                    self.sel = rows[cy]; self.write_view()
                else:
                    self.var_sel = rows[cy]
                return

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
        with open(path, errors="replace") as fh:
            text = fh.read()
        for line in text.splitlines():
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


# The login page's source link. The AGPL asks whoever runs a changed tern for others to offer them its source:
# point this at yours.
SOURCE = "https://github.com/samruger/tern"

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>tern</title>
<style>html,body{margin:0;height:100%;background:#1a1b26;color:#a9b1d6;font:16px monospace}
body{display:flex;align-items:center;justify-content:center}
form{display:flex;flex-direction:column;gap:12px;width:min(320px,calc(100% - 32px))}
input,button{font:inherit;padding:10px;border-radius:4px;border:1px solid #414868;background:#16161e;color:#c0caf5}
button{background:#7aa2f7;color:#1a1b26;border:0;cursor:pointer}p{margin:0;color:#f7768e;min-height:1.2em}
a{color:#7aa2f7;align-self:center;font-size:14px}</style>
</head><body><form method="post" action="/login"><b>tern</b>
<input type="password" name="password" placeholder="password" autocomplete="current-password" autofocus required>
<button>sign in</button><p>{msg}</p>
<a href="{source}" target="_blank" rel="noopener noreferrer">source</a></form></body></html>"""

LOOPBACK = ("127.0.0.1", "::1", "localhost")
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "font-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


class Server(ThreadingHTTPServer):
    """Each connection gets its own thread, and its TLS handshake happens there, not in the accept loop,
    so a client that connects and says nothing holds up only itself, for at most TIMEOUT seconds a read.
    Connections are capped in all and per address, so a flood of idle ones cannot pile up threads.
    Plain http:// on the HTTPS port gets a redirect to https:// instead of a reset."""
    daemon_threads = True
    request_queue_size = 64
    TIMEOUT = 20
    MAX_CONN, MAX_PER_IP = 64, 16
    ctx = None

    def __init__(self, *a, **kw):
        self.conns: dict[str, int] = {}
        self.conn_lock = threading.Lock()
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        ip = client_address[0]
        with self.conn_lock:
            full = sum(self.conns.values()) >= self.MAX_CONN or self.conns.get(ip, 0) >= self.MAX_PER_IP
            if not full:
                self.conns[ip] = self.conns.get(ip, 0) + 1
        if full:
            self.shutdown_request(request); return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(ip); raise

    def handle_error(self, request, client_address):
        """A browser or scanner hanging up mid-request is normal on the internet: say nothing about it."""
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)

    def _release(self, ip: str) -> None:
        with self.conn_lock:
            n = self.conns.get(ip, 1) - 1
            if n > 0:
                self.conns[ip] = n
            else:
                self.conns.pop(ip, None)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def finish_request(self, request, client_address):
        request.settimeout(self.TIMEOUT)
        if self.ctx is None:
            return super().finish_request(request, client_address)
        try:
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if first and first != b"\x16":            # not a TLS handshake: someone typed http://
            return self._to_https(request)
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            super().finish_request(tls, client_address)
        finally:
            tls.close()

    def _to_https(self, request) -> None:
        try:
            head = request.recv(4096).decode("latin-1")
            path = (head.split(" ", 2)[1:2] or ["/"])[0]
            m = re.search(r"^host:[ \t]*([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(:\d+)?[ \t]*\r?$", head, re.M | re.I)
            if m and path.startswith("/"):
                to = f"https://{m.group(1)}:{self.server_address[1]}{path}"
                request.sendall(f"HTTP/1.1 301 Moved Permanently\r\nLocation: {to}\r\nContent-Length: 0\r\n"
                                f"Connection: close\r\n\r\n".encode("latin-1"))
            else:
                request.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 11\r\nConnection: close\r\n\r\n"
                                b"use https:/")
        except (OSError, UnicodeError):
            pass


def serve(runtime: str = cfgmod.RUNTIME, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Login is on when ~/.config/tern/web.json has a password, HTTPS when web-cert.pem is there.
    Off localhost, both are required."""
    auth = webauth.Auth() if os.path.exists(webauth.AUTH_FILE) else None
    tls = os.path.exists(webauth.CERT_FILE) and os.path.exists(webauth.KEY_FILE)
    if host not in LOOPBACK and not (auth and tls):
        raise SystemExit("tern web: off localhost it needs a login and HTTPS first:\n"
                         "  python3 -m tern web-password\n  python3 -m tern web-cert")
    limiter, guard = webauth.Limiter(), threading.Lock()
    cookie_name = "__Host-" + webauth.COOKIE if tls else webauth.COOKIE   # __Host-: only this host, https, path /
    hub = Hub(runtime)
    look = {**_foot_colors(), "attr": {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE}}
    fonts = _font_files()
    look["fonts"] = sorted(fonts)

    class H(BaseHTTPRequestHandler):
        server_version = "tern-web"
        sys_version = ""
        protocol_version = "HTTP/1.1"           # keep the connection: no TLS handshake per frame

        def log_message(self, format, *args):           # quiet: a request every quarter second per tab
            pass

        def reply(self, code: int, body: bytes, ctype: str, cache: str = "no-store", extra: dict | None = None) -> None:
            if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, 5)
                self.send_response(code); self.send_header("Content-Encoding", "gzip")
            else:
                self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, to: str, cookie: str | None = None) -> None:
            self.reply(303, b"", "text/plain", extra={"Location": to, **({"Set-Cookie": cookie} if cookie else {})})

        def cookie(self, value: str, age: int) -> str:
            return (f"{cookie_name}={value}; Max-Age={age}; Path=/; HttpOnly; SameSite=Strict"
                    + ("; Secure" if tls else ""))

        def signed_in(self) -> bool:
            if auth is None:
                return True
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except CookieError:
                return False
            m = jar.get(cookie_name)
            return bool(m) and auth.valid(m.value)

        def login_page(self, msg: str = "", code: int = 200) -> None:
            self.reply(code, LOGIN_PAGE.replace("{source}", html.escape(SOURCE)).replace("{msg}", html.escape(msg)).encode(), "text/html; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/login":
                return self.redirect("/") if self.signed_in() else self.login_page()
            if path == "/logout":
                return self.redirect("/login", self.cookie("", 0))
            if not self.signed_in():
                return self.redirect("/login") if path == "/" else self.reply(401, b"sign in", "text/plain")
            if path == "/":
                with open(PAGE, "rb") as fh:
                    self.reply(200, fh.read(), "text/html; charset=utf-8")
            elif path == "/look":
                self.reply(200, json.dumps(look).encode(), "application/json")
            elif path.startswith("/font/") and path[6:] in fonts:
                with open(fonts[path[6:]], "rb") as fh:
                    self.reply(200, fh.read(), "font/ttf", "private, max-age=86400")
            else:
                self.reply(404, b"not found", "text/plain")

        def body(self) -> bytes | None:
            """The request's body, read in full so the kept connection stays in step; None if too big."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= 65536:
                self.close_connection = True
                return None
            return self.rfile.read(n)

        def do_login(self, data: bytes):
            ip = self.client_address[0]
            pw = parse_qs(data.decode(errors="replace")).get("password", [""])[0]
            with guard:
                wait, stamp = limiter.attempt(ip)
            if wait:
                return self.login_page(f"too many wrong tries: wait {int(wait // 60) + 1} min", 429)
            if auth.check(pw):
                with guard:
                    limiter.ok(ip, stamp)
                return self.redirect("/", self.cookie(auth.token(), webauth.TTL))
            time.sleep(1)
            self.login_page("wrong password", 401)

        def do_POST(self):
            data = self.body()
            if data is None:
                return self.reply(413, b"too big", "text/plain")
            if self.path == "/login" and auth is not None:
                return self.do_login(data)
            if self.path != "/frame":
                return self.reply(404, b"not found", "text/plain")
            if not self.signed_in():
                return self.reply(401, b"sign in", "text/plain")
            try:
                req = json.loads(data or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("not an object")
                body = json.dumps(hub.handle(req), separators=(",", ":"), ensure_ascii=False).encode()
            except (ValueError, TypeError) as ex:
                return self.reply(400, str(ex).encode(), "text/plain")
            except Exception:
                traceback.print_exc()
                return self.reply(500, b"tern web hit an error drawing the screen", "text/plain")
            self.reply(200, body, "application/json; charset=utf-8")

    srv = Server((host, port), H)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(webauth.CERT_FILE, webauth.KEY_FILE)
        srv.ctx = ctx
    shown = "localhost" if host in LOOPBACK else host
    print(f"tern web: {'https' if tls else 'http'}://{shown}:{port}/  (view only, "
          f"{'login on' if auth else 'no login'}; ctrl-c stops it, the engine keeps running)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
