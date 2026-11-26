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
from . import devices
from . import phone
from . import reports
from . import webauth
from .engine import live_dir

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web.html")

# keys that only change what this viewer sees: they reach the screen; everything else stops here
VIEW_KEYS = set("1234567890hjktTcCVe") | {"Enter", "Escape", "Home", "End", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown",
                                       "Tab", "BackTab"}
CONTROL_KEYS = {"q": "quit", "Q": "quit", "P": "engine pause/start", "s": "saving a report", "L": "live",
                "r": "renaming", " ": "turning a variant on or off",
                "D": "deleting"}
# border hints whose key changes something: not drawn on the web (⏎ selects a variant: it only views)
CONTROL_HINTS = {"r", "L", "P", "q", "space", "D"}

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
        self.cache, self.full, self.drawn = {}, False, None
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
        return self(dash.themes.readable(self.t["base"].get(fg, fg), bg), bold=bold) | (bg + 1) << 9


def clean_prefs(d) -> dict:
    """The parts of a dash.json-shaped dict the screen accepts, checked the way Screen.__init__ checks them."""
    out = {}
    if not isinstance(d, dict):
        return out
    d = {k: v for k, v in d.items() if v is None or isinstance(v, str)}   # (a list can't be looked up)
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

    app_rows_on = False                          # pairing and the phones are the desk's

    def __init__(self, hub: "Hub", h: int, w: int, prefs: dict | None = None):
        with _PAL_SWAP:                          # Screen.__init__ builds a Pal: make it the terminal-free one
            real, dash.Pal = dash.Pal, WebPal
            try:
                super().__init__(VTerm(h, w), hub.runtime)   # starts from the screen's own dash.json
            finally:
                dash.Pal = real
        self.hub = hub
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

    def saves_here(self, row) -> bool:
        return row.key == "screen.log"

    def read(self) -> None:
        self.alive, self.alive_at = self.hub.alive(), time.time()
        self.raw = self.hub.status()
        self.apply_selected()                    # the variant this viewer selected, as the terminal screen does

    # ------------------------------------------------------------ drawing
    def box(self, y, x, h, w, title, color, right=()) -> None:
        segs = [right] if isinstance(right, str) else list(right)
        segs = [s for s in segs if isinstance(s, str) or not set(s[0::2]) & CONTROL_HINTS and s[:2] not in (("e", "dit"), ("s", "ave"))]   # any key piece
        super().box(y, x, h, w, title, color, segs)

    # ------------------------------------------------------------ input from the page
    def press(self, name: str) -> None:
        code = dash.NAMED.get(name, ord(name) if len(name) == 1 else None)
        if code is None:
            return
        if name in VIEW_KEYS or self.tab == "settings" and name in (" ", "+", "-", "="):   # (a setting: send_setting)
            self.key(code)
        elif name in CONTROL_KEYS and self.tab in dash.KEYS_ON.get(name, dash.BOXES):
            self.say(f"view only: {CONTROL_KEYS[name]} is done from the screen in the terminal")

    def wheel_at(self, cy: int, cx: int, d: int) -> None:
        self.wheel(cy, cx, d)

    def frame(self, h: int, w: int) -> dict:
        if (h, w) != self.s.getmaxyx():
            self.s.resize(h, w)
        self.read()
        self.draw()
        return {"rows": self.s.runs(), "prefs": self.prefs}


# the engine ops an app may send, and the fields each keeps (what the screen sends for it)
ENGINE_OPS = {"live": ("on", "variant"), "variant_new": ("from", "name", "rule"), "rename": ("variant", "name"),
              "variant_delete": ("variant",), "variant_on": ("variant", "on"), "set": ("key", "value")}
RESULT_KEEP = 60.0


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
        self.sent: dict[str, dict[str, float]] = {}   # app device id -> {command id: when sent}

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

    def act(self, device: dict, cmd: dict) -> dict:
        """A signed command from a paired phone. Engine ops go to the engine the way the screen's do, with `by`;
        engine and report_save are done here. Returns {"id", "say"}; the engine's answer comes back in /phone's results."""
        op, by = cmd.get("op"), f"app {device['name']}"
        cmd_dir = os.path.join(self.runtime, "cmd")

        def note(text: str) -> str:
            return dash.write_command(cmd_dir, {"op": "note", "text": text, "by": by})

        if op in ENGINE_OPS:
            if not dash.engine_alive(self.runtime):
                return {"id": "", "say": "the engine isn't running: start it first"}
            out = {k: cmd[k] for k in ENGINE_OPS[op] if k in cmd}
            os.makedirs(cmd_dir, exist_ok=True)
            cid = dash.write_command(cmd_dir, {"op": op, **out, "by": by})
            say = "sent"
        elif op == "engine" and isinstance(cmd.get("run"), bool):
            os.makedirs(cmd_dir, exist_ok=True)
            if cmd["run"]:
                say = dash.start_engine(self.runtime)
                started = say.startswith(("engine started", "starting the engine"))
                cid = note(f"engine: {say}") if started else ""   # waits in the folder for the engine to come up
            else:
                cid = note("stopping the engine from the app") if dash.engine_alive(self.runtime) else ""
                say = dash.stop_engine(self.runtime, self.status().get("pid"))
        elif op == "report_save" and isinstance(cmd.get("report"), str):
            try:
                say = "saved report " + reports.save(self.runtime, cmd["report"])
            except LookupError as ex:
                raise devices.Refused(str(ex)) from None
            os.makedirs(cmd_dir, exist_ok=True)
            cid = note(say)
        else:
            raise devices.Refused("unknown command")
        if cid:
            with self.lock:
                mine = self.sent.setdefault(device["id"], {})
                mine[cid] = time.time()
        return {"id": cid, "say": say}

    def results_for(self, device_id: str) -> dict:
        """The engine's answers to the commands this app sent in the last minute."""
        now = time.time()
        with self.lock:
            mine = self.sent.get(device_id, {})
            for k in [k for k, t in mine.items() if now - t > RESULT_KEEP]:
                del mine[k]
            ids = list(mine)
        res = self.status().get("results") or {}
        return {k: res[k] for k in ids if k in res}

    def armed(self, cfg=None) -> bool:
        """Live is armed if config.json says so or the engine's status does (no status file: config alone decides)."""
        st = self.status()
        return bool((cfg is not None and cfg.live.enabled) or st.get("live_enabled") or st.get("mode") == "live")

    def phone(self, req: dict, device_id: str | None = None) -> dict:
        """A phone page's data. `set`: what the viewer just picked (coin, chart, variant, theme, log filter)."""
        sid, (scr, lock, _) = self.session(req.get("sid"), MIN_H, MIN_W, req.get("prefs"))
        with lock:
            if isinstance(req.get("set"), dict):
                scr.apply_prefs(req["set"])
                if "variant" in req["set"]:
                    scr.book_view = None         # its own book, on paper
                scr.save_prefs()
            scr.read()
            out = phone.page(scr, req)
            scr.prefs = scr.current_prefs()
        self.publish()
        extra = {"results": self.results_for(device_id)} if device_id else {}
        return {"sid": sid, "prefs": scr.prefs, **out, **extra}


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
    devs = devices.Devices(devices.FILE)         # the one for this process (a second would fight over its file lock)
    app_cookie = "__Host-tern_a" if tls else "tern_a"
    pair_limiter = webauth.Limiter()
    conf_state = {"stamp": None, "cfg": cfgmod.Config()}
    conf_lock = threading.Lock()

    def conf() -> cfgmod.Config:
        """config.json, read again when it changes, so the switches apply without a restart."""
        path = os.path.join(runtime, "config.json")
        try:
            s = os.stat(path)
            stamp = (s.st_mtime_ns, s.st_size, s.st_ino)
        except OSError:
            stamp = None
        with conf_lock:
            if stamp is None or stamp != conf_state["stamp"]:
                conf_state["cfg"] = cfgmod.load(runtime, conf_state["cfg"])
                conf_state["stamp"] = stamp
            return conf_state["cfg"]

    look = {**_foot_colors(), "attr": {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE}}
    fonts = _font_files()
    look["fonts"] = sorted(fonts)
    look["login"] = auth is not None

    class H(BaseHTTPRequestHandler):
        server_version = "tern-web"
        sys_version = ""
        protocol_version = "HTTP/1.1"           # keep the connection: no TLS handshake per frame
        app_session = False

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
            if self.app_session:
                self.send_header("X-Tern-App", "1")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, to: str, cookie: str | None = None) -> None:
            self.reply(303, b"", "text/plain", extra={"Location": to, **({"Set-Cookie": cookie} if cookie else {})})

        def cookie(self, value: str, age: int) -> str:
            return (f"{cookie_name}={value}; Max-Age={age}; Path=/; HttpOnly; SameSite=Strict"
                    + ("; Secure" if tls else ""))

        def cookie_value(self, name: str) -> str | None:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except CookieError:
                return None
            return jar[name].value if name in jar else None

        def app_device(self) -> str | None:
            """The paired phone this request's app cookie belongs to, while the app is on."""
            if not conf().web.app:
                return None
            return devs.session_device(self.cookie_value(app_cookie), time.time())

        def signed_in(self) -> bool:
            """An app session (with the app on), or a browser (with browsers on, and its password if there is one)."""
            self.app_session = self.app_device() is not None
            if self.app_session:
                return True
            if not conf().web.enabled:
                return False
            if auth is None:
                return True
            value = self.cookie_value(cookie_name)
            return value is not None and auth.valid(value)

        def browsers_off(self) -> bool:
            """Answer 403 if browsers are off and this request isn't an app session."""
            if conf().web.enabled or self.app_session:
                return False
            self.reply(403, b"the browser page is turned off (settings, WEB)", "text/plain")
            return True

        def json_reply(self, code: int, obj: dict, extra: dict | None = None) -> None:
            self.reply(code, json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode(), "application/json; charset=utf-8", extra=extra)

        def refuse(self, text: str) -> None:
            self.json_reply(403, {"error": text, "now": time.time()})

        def ui(self) -> str:
            """Which page this browser gets: the one it chose, else a phone's to phones."""
            chose = self.cookie_value(phone.UI_COOKIE)
            return chose if chose in phone.UI else "phone" if phone.is_phone(self.headers) else "full"

        def login_page(self, msg: str = "", code: int = 200) -> None:
            self.reply(code, LOGIN_PAGE.replace("{source}", html.escape(SOURCE)).replace("{msg}", html.escape(msg)).encode(), "text/html; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            self.app_session = False
            if path == "/login":
                if self.signed_in():
                    return self.redirect("/")
                return self.browsers_off() or self.login_page()
            if path == "/logout":
                return self.redirect("/login", self.cookie("", 0))
            if not self.signed_in():
                if self.browsers_off():
                    return
                return self.redirect("/login") if path == "/" else self.reply(401, b"sign in", "text/plain")
            if path == "/":
                with open(phone.PAGE if self.app_session or self.ui() == "phone" else PAGE, "rb") as fh:
                    self.reply(200, fh.read(), "text/html; charset=utf-8", extra={"Vary": "User-Agent, Sec-CH-UA-Mobile, Cookie"})
            elif path.startswith("/ui/") and path[4:] in phone.UI:   # the other one, from now on
                self.redirect("/", f"{phone.UI_COOKIE}={path[4:]}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if tls else ""))
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

        def do_app(self, data: bytes):
            """The app's own endpoints: JSON in and out, signed by the phone's keys, never the password cookie."""
            self.app_session = False
            if not conf().web.app:
                return self.refuse("the app is turned off (settings, WEB)")
            try:
                req = json.loads(data or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("not an object")
            except ValueError:
                return self.json_reply(400, {"error": "the request was malformed"})
            live, now, ip = hub.fast, time.time(), self.client_address[0]
            try:
                if self.path == "/app/pair":
                    parts = [req.get(k) for k in ("code", "name", "everyday", "live", "sig")]
                    if not all(isinstance(x, str) for x in parts):
                        raise devices.Refused("the request was malformed")
                    with guard:
                        wait, stamp = pair_limiter.attempt(ip)
                    if wait:
                        raise devices.Refused(f"too many tries: wait {int(wait // 60) + 1} min")
                    try:
                        oid = devices.offer(live, *parts, now)
                        shown = devices.words(parts[2], parts[3])
                    except ValueError:
                        raise devices.Refused("the request was malformed")
                    with guard:
                        pair_limiter.ok(ip, stamp)
                    return self.json_reply(200, {"offer": oid, "words": shown})
                if self.path == "/app/paired":
                    if not isinstance(req.get("offer"), str):
                        raise devices.Refused("the request was malformed")
                    return self.json_reply(200, devices.offer_state(live, req["offer"], now))
                signed = [req.get(k) for k in ("payload", "sig", "key")]
                if not all(isinstance(x, str) for x in signed):
                    raise devices.Refused("the request was malformed")
                cfg = conf()
                armed = hub.armed(cfg)
                if self.path == "/app/session":
                    dev, cmd = devs.check(*signed, now, cfg, armed)
                    if cmd != {"op": "session"}:
                        raise devices.Refused("not a sign-in")
                    token = devs.session_token(dev["id"], now)
                    return self.json_reply(200, {"ok": True, "name": dev["name"]}, extra={"Set-Cookie": (
                        f"{app_cookie}={token}; Max-Age={webauth.TTL}; Path=/; HttpOnly; SameSite=Strict"
                        + ("; Secure" if tls else ""))})
                # /act: the app's cookie says who is signed in; the signature says this request is theirs
                who = self.app_device()
                if who is None:
                    raise devices.Refused("sign in again")
                try:
                    claimed = json.loads(signed[0]).get("d")
                except (ValueError, AttributeError):
                    claimed = None
                if claimed != who:
                    raise devices.Refused("this request is for another phone" if claimed else "the request was malformed")
                dev, cmd = devs.check(*signed, now, cfg, armed)
                return self.json_reply(200, hub.act(dev, cmd))
            except devices.Refused as ex:
                return self.refuse(str(ex))
            except Exception:
                traceback.print_exc()
                return self.json_reply(500, {"error": "tern web hit an error", "now": time.time()})

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
            self.app_session = False
            if self.path in ("/app/pair", "/app/paired", "/app/session", "/act"):
                return self.do_app(data)
            if self.path == "/login" and auth is not None:
                return self.browsers_off() or self.do_login(data)
            if self.path not in ("/frame", "/phone"):
                return self.reply(404, b"not found", "text/plain")
            if not self.signed_in():
                if self.browsers_off():
                    return
                return self.reply(401, b"sign in", "text/plain")
            try:
                req = json.loads(data or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("not an object")
                if self.path == "/phone":
                    out = hub.phone(req, self.app_device() if self.app_session else None)
                else:
                    out = hub.handle(req)
                if self.app_session and self.path == "/phone":
                    out["control"] = True
                body = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()
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
