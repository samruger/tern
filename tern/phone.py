"""The web's phone pages: which browsers get them, and what each page shows, as data that phone.html draws.

View only, as the rest of the web: a viewer's coin, strike, variant, theme and chart are that viewer's own, and nothing
here sends the engine a command (the paired app's go by /act, signed on the phone: this only says what they would ask).
Each viewer has a WebScreen like any other browser's (its drawing unused), so the numbers are the ones the boxes show.
"""

from __future__ import annotations

import json
import os
import re
import time
import zlib

from . import about as aboutmod
from . import config as cfgmod
from . import dash
from . import reports
from . import variants as varmod
from .variants import describe

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone.html")
PAGES = ("home", "ladder", "contract", "book", "variants", "variant", "log", "gate", "reports", "report", "themes", "help", "settings", "edit")
UI_COOKIE = "tern_ui"                            # "full" or "phone": what the viewer chose over what was detected
UI = ("phone", "full")
PHONE = re.compile(r"iPhone|iPod|Android.*\bMobile\b|Windows Phone|Opera Mini")
# Every key the desk's screen has, and where the app does it (the screen's own KEYS legend is for a keyboard)
APP_KEYS = [("enter", "a variant \u2192 select"), ("space", "a variant \u2192 on / off"), ("e", "a variant \u2192 edit"),
            ("r", "a variant \u2192 rename"), ("D", "a variant \u2192 delete"), ("L", "a variant \u2192 go live"),
            ("C", "the home page \u2192 coin"), ("c", "the chart \u2192 span"), ("V", "the book \u2192 paper / live"),
            ("s", "a report \u2192 save"), ("t", "the themes page \u2192 pick one"), ("T", "the themes page \u2192 pick one"),
            ("P", "settings \u2192 engine")]
POINTS = 160                                     # values in a chart
LOG_ENTRIES = 60
ROLE = {dash.GREEN: "up", dash.RED: "dn", dash.YELLOW: "warn", dash.MAGENTA: "no"}


def is_phone(headers) -> bool:
    """Whether a browser is a phone's. Chromium says so in a header of its own; the others only in their name. A tablet
    isn't, and neither is a phone asked for the desktop site: it calls itself a computer."""
    hint = (headers.get("Sec-CH-UA-Mobile") or "").strip()
    if hint in ("?0", "?1"):
        return hint == "?1"
    ua = headers.get("User-Agent") or ""
    return "iPad" not in ua and bool(PHONE.search(ua))


# ---------------------------------------------------------------- pieces

def signed(v) -> str:
    return "up" if (v or 0) >= 0 else "dn"


def thin(vals: list, n: int = POINTS) -> list:
    """A chart's values: n of them at most, the last among them."""
    k = len(vals)
    return vals if k <= n else [vals[(i + 1) * k // n - 1] for i in range(n)]


def look(scr) -> dict:
    """The viewer's theme: 256-color indexes by role."""
    P, t = scr.P, scr.P.t
    c = lambda n: t["base"].get(n, n)
    return {"name": P.name, **{k: c(t[k]) for k in ("grey", "key", "head", "fill", "tab", "tab_text", "sym", "unit", "variant", "value", "slot")},
            "up": c(dash.GREEN), "dn": c(dash.RED), "warn": c(dash.YELLOW), "no": c(dash.MAGENTA), "text": c(dash.WHITE),
            "border": {k: c(v) for k, v in t["border"].items()}, "rows": [c(x) for x in t["rows"]],
            "grads": {k: P.grad(k) for k in ("spot", "yes", "eq_up", "eq_dn")}}


def spans(scr, pieces: list) -> list:
    """Colored text from the screen, (text, attr), as [text, color index or -1, bold]."""
    return [[t, (a & 0x1FF) - 1, int(bool(a & dash.curses.A_BOLD))] for t, a in pieces]


def engine(scr) -> dict:
    """How the engine is, as the status line says it."""
    st, now = scr.st, time.time()
    hb = st.get("heartbeat") or 0
    ok = now - hb < 5 and not st.get("stopped")
    if ok:
        sec = ((st.get("spot") or {}).get(cfgmod.SERIES[scr.series]) or {}).get("sec") or []
        ev = (st.get("events") or {}).get(scr.series) or {}
        old = [f"{what} {dash.ago(age)}" for what, age, most in (("spot", now - sec[-1][0] if sec else 0, 10), ("ladder", ev.get("age") or 0, 30)) if age > most]
        return {"ok": True, "text": " · ".join(old) + " old" if old else ""}
    text = "the engine is paused" if not scr.alive else "the engine is starting" if st.get("stopped") or not hb else \
        f"the engine has said nothing for {dash.ago(now - hb)}"
    return {"ok": False, "text": text}


def coins(scr) -> list:
    """Every coin: its price and its hour."""
    out, now = [], time.time()
    for series, prod in cfgmod.SERIES.items():
        sp = (scr.st.get("spot") or {}).get(prod) or {}
        p = sp.get("now")
        past = next((q for t, q in reversed(sp.get("min") or []) if t + 60 <= now - 3600), None)
        out.append({"series": series, "coin": cfgmod.coin(series), "px": dash.px(p) if p is not None else "-",
                    "chg": p / past - 1 if p is not None and past else None})
    return out


def hour(scr) -> dict:
    """When the coin's hour closes, and the selected variant's slots in it: done, the next one, later."""
    now, S = time.time(), scr.strat() or {}
    ev = (scr.st.get("events") or {}).get(scr.series) or {}
    left = (ev.get("close") or 0) - now
    if left <= 0:
        return {}
    mins = sorted(S.get("minutes") or [], reverse=True) if scr.series in (S.get("series") or []) else []
    nxt = max((m for m in mins if m * 60 < left), default=None)
    return {"left": dash.clock(left), "slots": [{"at": f"{m:g}m", "state": "done" if m * 60 >= left else "next" if m == nxt else "later",
                                                 "in": dash.clock(left - m * 60) if m == nxt else ""} for m in mins]}


def spot_chart(scr) -> dict:
    now, span = time.time(), dash.CHARTS[scr.chart]
    vals = dash.by_time(scr.spot_points(cfgmod.SERIES[scr.series]), now - span, now, POINTS)
    row = scr.row(scr.sel)
    return {"vals": vals, "t0": now - span * len(vals) / POINTS, "t1": now, "strike": dash.strike_of(row["k"]) if row else None}


def bet(ticker: str, q: float) -> dict:
    m = re.search(r"-[TB]([0-9.]+)$", ticker or "")
    return {"coin": cfgmod.coin((ticker or "").split("-")[0]), "strike": dash.strike_label(float(m.group(1))) if m else "?",
            "side": "YES" if q > 0 else "NO", "qty": f"{abs(q):g}"}


def positions(scr, b: dict) -> list:
    now = time.time()
    return [{**bet(p["t"], p["q"]), "avg": dash.cents(p["avg"]).strip(), "mark": dash.cents(p.get("mark")).strip(),
             "pnl": dash.money(p.get("pnl"), True), "role": signed(p.get("pnl")), "closes": dash.clock((p.get("close") or now) - now)}
            for p in b.get("pos") or []]


def gate_of(v: dict) -> dict:
    """A variant's gate: passed, or how far each check has come."""
    g = v.get("gate") or {}
    out = []
    for c in g.get("checks") or []:
        name = {"days": "days traded", "entries": "settled entries", "pnl": "paper P&L"}.get(c.get("key"))
        if not name:
            continue
        have, need = c["have"], c["need"]
        out.append({"name": name, "ok": bool(c["ok"]), "frac": 1.0 if c["ok"] else max(0.0, min(1.0, have / need)) if need > 0 else 0.0,
                    "text": f"{have:.1f} of {need:g}" if c["key"] == "days" else f"{have:,} of {need:,}" if c["key"] == "entries" else
                    f"{dash.money(have, True)} of {dash.money(need)}"})
    return {"passed": bool(g.get("passed")), "checks": out}


def variant(scr, v: dict) -> dict:
    """A variant's row."""
    st, today = scr.st, v.get("today") or [0, 0, 0]
    return {"name": v["name"], "title": scr.vn(v["name"]), "on": v.get("on", True), "own": bool(v.get("own")),
            "selected": v["name"] == scr.variant, "live": bool(v.get("live") and st.get("live_enabled")),
            "pnl": dash.money(v["pnl"], True), "role": signed(v["pnl"]), "n": v["pnl"], "today": today[0],
            "hours": v["hours"], "entries": v["entries"], "open": v.get("open", 0),
            "win": f"{v['win']:.0%}" if v.get("win") is not None else "-", "t": f"{v['t']:+.2f}" if v.get("t") is not None else "-",
            "ct": f"{v['per_contract'] * 100:+.2f}¢" if v.get("per_contract") is not None else "-",
            "passed": bool((v.get("gate") or {}).get("passed")), "stopped": bool(v.get("stopped"))}


def book_view(scr) -> str:
    return scr.book_view or ("live" if scr.st.get("mode") == "live" else "paper")


def menu(scr) -> dict:
    """What the menu says beside each page's name: [text, role]."""
    st = scr.st
    b = st.get(book_view(scr)) or {}
    vs, g = st.get("variants") or [], st.get("gate") or {}
    eq, start = b.get("equity"), b.get("start") or 0
    pnl = eq - start if eq is not None and start else None
    up = sum(1 for v in vs if v["pnl"] > 0)
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    bad = sum(1 for l in st.get("logs") or [] if l[0] >= midnight and l[1] in ("warn", "error"))
    checks = [c for c in g.get("checks") or []]
    star = sum(1 for r in scr.ladder() if scr.in_band(r))
    return {"book": [dash.money(pnl, True) if pnl is not None else "", signed(pnl)],
            "variants": [f"{up} of {len(vs)} up" if vs else "", ""],
            "ladder": [f"{star} in band" if star else "", "warn"],
            "log": [f"{bad} warning{'' if bad == 1 else 's'} today" if bad else "", "warn"],
            "gate": ["passed", "up"] if g.get("passed") else [f"{sum(1 for c in checks if c.get('ok'))} of {len(checks)}" if checks else "", "warn"],
            "themes": [scr.P.name, ""], "help": ["", ""], "settings": ["", ""]}


# ---------------------------------------------------------------- pages

def home(scr, arg) -> dict:
    v = scr.vrow(scr.variant)
    b = scr.st.get(book_view(scr)) or {}
    return {"chart": spot_chart(scr), "hour": hour(scr), "view": book_view(scr),
            "money": [[l, t, ROLE.get(c, "")] for l, t, c in scr.book_money(b)[:4]] if b else [],
            "variant": {**variant(scr, v), "gate": gate_of(v), "curve": thin(v.get("curve") or [], 80)} if v else None,
            "open": positions(scr, b)}


def ladder(scr, arg) -> dict:
    """The strikes that are not settled in all but name, high to low, and any that are held or near the price."""
    lad = scr.ladder()
    held = {p["t"]: p for p in (scr.cur_book() or {}).get("pos", [])}
    now = ((scr.st.get("spot") or {}).get(cfgmod.SERIES[scr.series]) or {}).get("now")
    at = min(range(len(lad)), key=lambda i: abs(lad[i]["k"] - now)) if lad and now is not None else len(lad) // 2
    rows = []
    for i, r in enumerate(lad):
        bid, ask = r["b"], r["a"]
        mid = (bid + ask) / 2 if bid is not None and ask is not None else bid if bid is not None else ask
        if not (abs(i - at) <= 6 or r["t"] in held or mid is not None and 0.02 < mid < 0.98):
            continue
        h = held.get(r["t"])
        rows.append({"t": r["t"], "k": dash.strike_label(r["k"]), "bid": dash.cents(bid).strip(), "ask": dash.cents(ask).strip(), "p": mid or 0.0,
                     "above": now is not None and now > r["k"], "band": scr.in_band(r), "sel": r["t"] == scr.sel,
                     "pos": f"{'YES' if h['q'] > 0 else 'NO'} {abs(h['q']):g}" if h else "", "side": "up" if h and h["q"] > 0 else "no"})
    return {"rows": rows, "px": dash.px(now) if now is not None else None, "hour": hour(scr)}


def contract(scr, arg) -> dict:
    """One strike as the bet it is."""
    if arg and scr.row(arg) and scr.sel != arg:
        scr.sel = arg; scr.write_view()
    r = scr.row(scr.sel)
    if not r:
        return {"none": "waiting for the hourly event"}
    prod, S = cfgmod.SERIES[scr.series], scr.strat() or {}
    bid, ask = r["b"], r["a"]
    mid = (bid + ask) / 2 if bid is not None and ask is not None else bid if bid is not None else ask
    no = lambda p: None if p is None else 1 - p
    now, close = time.time(), ((scr.st.get("events") or {}).get(scr.series) or {}).get("close")
    pts = scr.contract_points(scr.sel)
    t0 = (close or now) - 3600
    t0 = min(t0, pts[0][0]) if pts else t0
    vals = dash.by_time(pts, t0, now, POINTS) if pts else []
    tick = [x["t"] for x in scr.ladder()]
    i = tick.index(r["t"])
    held = next((p for p in (scr.cur_book() or {}).get("pos", []) if p["t"] == r["t"]), None)
    return {"t": r["t"], "title": f"{prod.split('-')[0]} above ${dash.strike_label(r['k'])}?",
            "yes": [dash.cents(bid).strip(), dash.cents(ask).strip()], "no": [dash.cents(no(ask)).strip(), dash.cents(no(bid)).strip()],
            "p": mid, "vol": dash.volume(r["v"]), "band": [S.get("band_lo", 0.75), S.get("band_hi", 0.95)],
            "chart": {"vals": vals, "t0": now - (now - t0) * len(vals) / POINTS, "t1": now},
            "lean": spans(scr, [(t, scr.P(c, bold=c not in (scr.P.grey, dash.WHITE))) for t, c in scr.leaning(r, mid, S, prod)]) if mid is not None and S else [],
            "up": tick[i - 1] if i else None, "down": tick[i + 1] if i + 1 < len(tick) else None,
            "held": positions(scr, {"pos": [held]})[0] if held else None, "hour": hour(scr)}


def book(scr, arg) -> dict:
    st, now, v = scr.st, time.time(), book_view(scr)
    b = st.get(v) or {}
    done, fills = [], []
    for t in reversed(b.get("trades") or []):
        if t.get("kind") == "settle":
            q = -t["delta"]
            won = (t.get("result") == "yes") == (q > 0)
            done.append({**bet(t["ticker"], q), "won": "void" if t.get("result") == "void" else "won" if won else "lost",
                         "pnl": dash.money(t["realized"], True), "role": signed(t["realized"]), "ago": dash.ago(now - t["ts"])})
        elif t.get("kind") == "fill":
            q = t["delta"]
            fills.append({**bet(t["ticker"], q), "at": dash.cents(t["price"] if q > 0 else 1 - t["price"]).strip(),
                          "fee": dash.money(t["fee"]), "ago": dash.ago(now - t["ts"])})
    live = scr.vn(scr.live_variant()) if st.get("live_enabled") and scr.live_variant() else ""
    return {"view": v, "paper": scr.vn(scr.variant), "live": live, "money": [[l, t, ROLE.get(c, "")] for l, t, c in scr.book_money(b)] if b else [],
            "record": [[l, t, ROLE.get(c, "")] for l, t, c in scr.book_record(st.get("gate") or {})] if v == "paper" else [],
            "curve": thin([p[1] for p in b.get("curve") or []]), "start": b.get("start"),
            "open": positions(scr, b), "done": done, "fills": fills}


def variants(scr, arg) -> dict:
    return {"rows": [{**variant(scr, v), "curve": thin(v.get("curve") or [], 40)} for v in scr.st.get("variants") or []]}


def one_variant(scr, arg) -> dict:
    v = scr.vrow(arg) or scr.vrow(scr.variant)
    if v is None:
        return {"none": "waiting for the engine"}
    view = (scr.st.get("views") or {}).get(v["name"]) or {}
    today = v.get("today") or [0, 0, 0]
    stats = [["P&L", dash.money(v["pnl"], True), signed(v["pnl"])], ["today", dash.money(today[0], True), signed(today[0])],
             ["equity", dash.money(view.get("equity")), ""], ["cash", dash.money(view.get("cash")), ""], ["win", variant(scr, v)["win"], ""],
             ["a contract", variant(scr, v)["ct"], signed(v.get("per_contract"))], ["entries", f"{v['entries']:,}", ""],
             ["hours", f"{v['hours']:,}", ""], ["t", variant(scr, v)["t"], ""], ["won today", f"{today[2]} of {today[1]}" if today[1] else "-", ""]]
    rule = v.get("rule") or {}
    try:
        says = describe(rule, v.get("paper_limits", False)).split(" · ")
    except (KeyError, TypeError):                # an engine from before a field was added
        says = []
    kind, lines = scr.live_ask(v)
    return {**variant(scr, v), "desc": v.get("desc") or "", "stats": stats, "gate": gate_of(v), "curve": thin(v.get("curve") or []),
            "points": len(v.get("curve") or []), "rule": says, "positions": positions(scr, view), "live_ask": {"kind": kind, "lines": lines},
            "all": [[x["name"], scr.vn(x["name"])] for x in scr.st.get("variants") or []]}


def log(scr, arg) -> dict:
    """The log's entries, newest first; an hour's and a slot's table goes with its line."""
    rows = scr.log_rows(scr.logs_shown(), 100_000)
    out: list = []
    for ts, lvl, text, sub, cells in rows:
        if cells is None and sub == 0:
            # log_rows gives a slot's and an hour's line its book's name, as the log has it
            out.append({"k": f"{ts}:{text[:60]}", "at": time.strftime("%H:%M", time.localtime(ts)), "day": time.strftime("%a %b %-d", time.localtime(ts)),
                        "lvl": lvl, "text": spans(scr, scr.log_spans(text, lvl)), "head": [], "rows": []})
        elif cells and out:
            e = out[-1]
            if not e["head"]:
                e["head"] = [t.strip() for t, _ in cells]
                continue
            fixed = len(e["head"]) - (e["head"][-1] == "coins")
            row = [[t.strip(), role] for t, role in cells[:fixed]]
            e["rows"].append(row + ([["".join(t for t, _ in cells[fixed:]), "note"]] if len(e["head"]) > fixed else []))
    return {"entries": out[::-1][:LOG_ENTRIES], "filter": scr.log_filter, "filters": dash.LOG_FILTERS}


def gate(scr, arg) -> dict:
    st, name = scr.st, scr.vn(scr.variant)
    v = scr.vrow(scr.variant) or {}
    live = scr.live_variant()
    armed = bool(st.get("live_enabled"))
    rule = v.get("rule") or {}
    note = ""
    lg = st.get("live_gate") or {}
    if armed and not lg.get("ok", True):
        note = f"live waits: {(lg.get('checks') or [{}])[0].get('name', '')}"
    elif live and live != scr.variant:
        note = f"live {'trades' if armed else 'is set to'} {scr.vn(live)}"
    elif not (st.get("live_cfg") or {}).get("auto_needs_gate", True):
        note = "live.auto_needs_gate is false: the checks are not needed"
    return {"name": name, **gate_of(v),
            "live": [{"name": "Kalshi key present", "ok": bool(st.get("keys"))},
                     {"name": "armed" + (f", {scr.vn(live)}" if armed and live else ""), "ok": armed},
                     {"name": "authenticated", "ok": st.get("mode") == "live", "error": st.get("live_error") or ""}],
            "limits": [["at risk", f"${rule.get('max_risk', 0):,.0f}"], ["an order", f"${rule.get('max_order', 0):,.0f}"],
                       ["a day", f"-${rule.get('day_loss', 0):,.0f}"]], "note": note,
            "rows": [variant(scr, r) for r in st.get("variants") or []]}


def report_list(scr, arg) -> dict:
    return {"rows": [{"id": e.id, "label": e.label, "saved": e.saved} for e in scr.rep_entries()]}


def one_report(scr, arg) -> dict:
    """A report as blocks: paragraphs, and tables of their own columns."""
    ids = {e.id: e for e in scr.rep_entries()}
    e = ids.get(arg) or ids["desk"]
    got = reports.FEED.get(scr.runtime, e.id)
    blocks: list = []
    for l in got.lines if got else []:
        if dash.REPORT_HEAD.match(l):
            blocks.append({"head": l.split(), "rows": []})
            continue
        m = dash.REPORT_ROW.match(l)
        if m and blocks and "head" in blocks[-1]:
            blocks[-1]["rows"].append([m[2], *m[3].split()])
        elif m:                                  # a table that goes on under another's heading
            head = next((b["head"] for b in reversed(blocks) if "head" in b), [])
            blocks.append({"head": head, "rows": [[m[2], *m[3].split()]]})
        elif l.strip():
            blocks.append({"text": spans(scr, scr.log_spans(l.strip(), ""))})
    return {"id": e.id, "label": e.label, "ready": got is not None, "blocks": blocks, "all": [[x.id, x.label] for x in ids.values()],
            "from": f"runtime/{e.id[len('saved:'):]}" if e.saved else reports.command(e.id)}


def theme_list(scr, arg) -> dict:
    out = []
    for name in dash.themes.NAMES:
        t = dash.themes.THEMES[name]
        ramp = dash.themes.smooth(tuple(t["grads"]["yes"]))
        out.append({"name": name, "color": t["border"]["spot"], "ramp": [ramp[i * len(ramp) // 12] for i in range(12)]})
    return {"rows": out}


def help_page(scr, arg) -> dict:
    """The help's sections as the screen words them, the keys as the app has them."""
    P, out = scr.P, []
    for title, lines in scr.help_sections(60):
        if title == "KEYS":
            lines = [[(f"{a:<6}", P(P.c("key"), bold=True)), (where, P(dash.WHITE))] for a, where in APP_KEYS]
        out.append({"title": title, "lines": [[[l, -1, 0]] if isinstance(l, str) else spans(scr, l) for l in lines]})
    return {"sections": out}


def setting_row(scr, r, value, about: list) -> dict:
    """One setting as the app draws it. value: the setting's own (a list for slots and coins, None for a cap that is off)."""
    choices = [[k, v] for k, v in dash.LOG_FILTERS.items()] if r.kind == "choice" else scr.set_choices(r) if r.kind in ("slots", "coins") else []
    return {"key": r.key, "label": r.label, "value": value, "shown": scr.set_fmt(r, value), "kind": r.kind, "lo": r.lo, "hi": r.hi,
            "step": r.step, "choices": choices, "about": [[a, b] for a, b in about], "risk": r.risk}


def settings_page(scr, arg) -> dict:
    """The desk's and the screen's settings, grouped, with what each is; the phones paired are listed, not changed here."""
    groups: list = []
    for r in cfgmod.SETTINGS + dash.SCREEN_SETTINGS:
        if r.group or not groups:
            groups.append({"name": r.group, "rows": []})
        about = aboutmod.DESK.get(r.key) or [("text", r.what[:1].upper() + r.what[1:] + ".")]
        groups[-1]["rows"].append(setting_row(scr, r, scr.set_value(r.key), about))
    devs = getattr(getattr(scr, "hub", None), "devices", None)        # the process's one (its file lock is not taken twice)
    live = scr.live_variant() if scr.st.get("live_enabled") else None
    return {"groups": groups, "phones": devs.all() if devs is not None else [],
            "engine": {"alive": dash.engine_alive(scr.runtime), "live": scr.vn(live) if live else ""}}


def edit_page(scr, arg) -> dict:
    """A variant's rule as the editor's rows; the draft is the app's own until it saves."""
    v = scr.vrow(arg) or scr.vrow(scr.variant)
    if v is None:
        return {"none": "waiting for the engine"}
    rule = v.get("rule") or {}
    ctx = {"vol": None, "price": None, "limits": v.get("paper_limits", False), "name": scr.vn(v["name"])}
    told = None
    if all(rule.get(f.name) is not None for f in varmod.RULE_SETTINGS if f.name not in ("cap", "max_vol")):
        ctx["vol"], ctx["price"] = scr.vols(rule["series"]), scr.price(rule["series"][0])
        told = {"cap": None, "max_vol": None, **rule}
    rows = []
    for f in varmod.RULE_SETTINGS:
        try:
            about = aboutmod.rule(f.name, told, ctx) if told else []
        except (KeyError, TypeError, ValueError):
            about = []
        rows.append(setting_row(scr, f, rule.get(f.name), about or [("text", f.what[:1].upper() + f.what[1:] + ".")]))
    return {"from": v["name"], "title": scr.vn(v["name"]), "own": bool(v.get("own")), "rows": rows, "max_own": varmod.MAX_OWN}


MAKE = {"home": home, "ladder": ladder, "contract": contract, "book": book, "variants": variants, "variant": one_variant,
        "log": log, "gate": gate, "reports": report_list, "report": one_report, "themes": theme_list,
        "help": help_page, "settings": settings_page, "edit": edit_page}


def page(scr, req: dict) -> dict:
    """What a page shows now. req: page, arg (a strike, a variant, a report), view (the book's), look (the theme's colors
    the browser has)."""
    name = req.get("page") if req.get("page") in PAGES else "home"
    arg = req.get("arg") if isinstance(req.get("arg"), str) and len(req["arg"]) <= 200 else None
    if req.get("view") in ("paper", "live"):
        scr.book_view = req["view"]
    if name == "contract" and arg:               # a strike's coin comes with it (a link, the back button)
        series = arg.split("-")[0]
        if series in cfgmod.SERIES and series != scr.series:
            scr.series, scr.sel = series, None
    scr.pick_strike()
    st = scr.st
    out = {"page": name, "now": time.time(), "engine": engine(scr), "mode": "live" if st.get("mode") == "live" else "armed" if st.get("live_enabled") else "paper",
           "error": st.get("live_error") or "", "coins": coins(scr), "series": scr.series, "coin": cfgmod.coin(scr.series), "chart": scr.chart,
           "charts": list(dash.CHARTS), "selected": {"name": scr.variant, "title": scr.vn(scr.variant)} if scr.variant else None,
           "theme": scr.P.name, "menu": menu(scr), "flash": scr.flash if scr.flashing() else ""}
    data = MAKE[name](scr, arg)
    out["etag"] = f"{zlib.crc32(json.dumps([name, arg, data]).encode()):08x}"
    if req.get("etag") != out["etag"]:           # (the same as the browser has: not sent again)
        out["data"] = data
    if req.get("look") != scr.P.name:
        out["look"] = look(scr)
    return out
