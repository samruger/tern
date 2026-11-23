"""The reports: what `tern report` prints and the screen's reports box (o) shows.

Live reports are worked out from the records each time: profile 1's variants, every profile's book side by side,
one profile by settings period, the deleted profiles. Saved ones are the runtime folder's report-*.txt, shown as
written; save() makes one (s on the screen, tern report --save, the weekly timer). Reading a report changes
nothing: no record, setting or profile folder is written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import dataclass

from . import config as cfgmod
from . import profiles as profmod
from .shadow import DESC, ORDER, RETIRED, Shadow, desc_of, table
from .variants import describe

SAVED = re.compile(r"^report-([^/\0]+)\.txt$")       # a saved report's file name in the runtime folder
SAVED_AT = re.compile(r"^\d{4}-(\d\d-\d\d)-(\d\d)(\d\d)-(.+)$")   # what save() puts in it: day, time, which report
SAVED_MAX = 1_000_000                               # bytes of one that are shown


def _config(runtime: str) -> cfgmod.Config:
    """config.json, or the defaults. Unlike cfgmod.load, a missing file is not written."""
    return cfgmod.load(runtime) if os.path.exists(os.path.join(runtime, "config.json")) else cfgmod.Config()


# ---------------------------------------------------------------- the live ones

def desk(runtime: str) -> list[str]:
    """Profile 1's variants side by side: built in, its own, deleted and retired."""
    sh = Shadow(runtime)
    use = _config(runtime).strategy.variant
    lost = sorted(sh.own.used() - set(shadow_names(sh)))      # records of variants variants.json no longer has
    w = max(len(n) for n in shadow_names(sh) + lost)
    out = table(sh.summary(), w)
    own = {n: (f"({sh.own.title(n)}) " if sh.own.title(n) != n else "") + desc_of(n, sh.own) for n in sh.own.active()}
    out += [""] + [f"{n}: {d}" + ("  ← in use" if n == use else "") for n, d in {**DESC, **own}.items()]
    gone = [r for r in sh.summary(sh.own.deleted()) if r["entries"] or r["open"]] if sh.own.deleted() else []
    if gone:
        out += ["", "deleted, kept for the record:"] + table(gone, w)[1:]
    if lost:
        out += ["", "not in variants.json any more, kept for the record:"] + table(sh.summary(lost), w)[1:]
    old = [r for r in sh.summary(list(RETIRED)) if r["entries"]]
    if old:
        out += ["", "retired, kept for the record:"] + table(old, w)[1:] + [f"{n}: {d}" for n, d in RETIRED.items()]
    return out + ["", "t is by hour and shows after 10 hours."]


def side_by_side(runtime: str, room: int | None = None) -> list[str]:
    """Every profile's book: its picked variant's record."""
    cfg = _config(runtime)
    rows = [(profmod.Names(runtime).profile, cfg.strategy.variant,
             profmod.SettingsLog(os.path.join(runtime, "settings-log.jsonl")).sv, Shadow(runtime, cfg.taker_fee))]
    rows += [(p.name + ("" if p.enabled else " (off)"), p.variant, p.log.sv, p.shadow)
             for p in profmod.load_all(runtime, cfg.taker_fee)]
    return _books(rows, room) + ["", "each profile's book is its picked variant's record:",
                                 "look at entries and t (by hour, shows after 10 hours) as well as P&L."]


def deleted(runtime: str, room: int | None = None) -> list[str]:
    """The deleted profiles' books, from the folders kept in profiles-deleted/."""
    fee = _config(runtime).taker_fee
    root = os.path.join(runtime, profmod.DELETED_DIR)
    gone, out = [], []
    for n in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        try:
            gone.append(profmod.Profile.load(os.path.join(root, n), fee))
        except Exception as ex:
            out.append(f"{n}: unreadable ({ex})")
    if not gone:
        return out + ["no deleted profiles"]
    return out + [f"deleted profiles, kept in {root}:"] + \
        _books([(f"{p.name} ({p.id})", p.variant, p.log.sv, p.shadow) for p in gone], room)


def find(runtime: str, name: str) -> str:
    """The id of the profile called that, by name or id ("1" is the desk). LookupError names the known ones."""
    want, one = name.strip().lower(), profmod.Names(runtime).profile    # profile 1's name (N on the screen renames it)
    if want in ("1", "profile 1", one.lower()):
        return "1"
    loaded = profmod.load_all(runtime, _config(runtime).taker_fee)
    p = next((p for p in loaded if want in (p.id, p.name.lower())), None)
    if p is None:
        raise LookupError(f"no profile {name!r}; known: {one} (1)" + "".join(f", {p.name} ({p.id})" for p in loaded))
    return p.id


def of_profile(runtime: str, pid: str) -> list[str]:
    """One profile's variants, one table per settings period. pid "1" is the desk."""
    cfg = _config(runtime)
    if pid == "1":
        log = profmod.SettingsLog(os.path.join(runtime, "settings-log.jsonl"))
        return [f"{profmod.Names(runtime).profile} (profile 1, the desk): book trades {cfg.strategy.variant}, settings {log.sv}"] + \
            _periods(Shadow(runtime, cfg.taker_fee), log.lines(), log.sv)
    p = profmod.Profile.load(os.path.join(runtime, profmod.PROFILES_DIR, pid), cfg.taker_fee)
    return [f"{p.name} ({p.id}): {'on' if p.enabled else 'off'}, book trades {p.variant}, settings {p.log.sv}"] + \
        _periods(p.shadow, p.log.lines(), p.log.sv)


def _periods(sh, lines: list[dict], sv_now: int) -> list[str]:
    """A profile's variants, one table per settings period, each headed with when it ran and what changed."""
    day = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    starts: dict[int, float] = {}
    why: dict[int, list[str]] = {}
    for ln in lines:
        sv = ln.get("sv")
        if isinstance(sv, int):
            starts.setdefault(sv, ln.get("ts") or 0)
            if ln.get("key") in profmod.RECORD_KEYS:
                why.setdefault(sv, []).append(f"{profmod.label(ln['key'])} {profmod.show(ln['key'], ln.get('from'))}"
                                              f" → {profmod.show(ln['key'], ln.get('to'))}")
    versions = sorted({v for n in shadow_names(sh) for v in sh.versions(n)} | {sv_now})
    out = []
    for sv in versions:
        later = [starts[v] for v in starts if v > sv]
        when = (f"{day(starts[sv])} to {day(min(later)) if later else 'now'}" if sv in starts else
                "before settings were logged" if sv == 0 else "")
        made = any(ln.get("key") == "created" for ln in lines)
        head = f"settings {sv}" + (f", {when}" if when else "") + (": " + ", ".join(why[sv]) if why.get(sv) else
                                                                     (": as created" if made else ": the settings in use when logging began")
                                                                     if sv == 1 else "")
        rows = [r for r in sh.summary(sh.order() + sh.own.deleted(), sv=sv) if r["entries"] or r["open"]]
        out += ["", head] + (["  " + l for l in table(rows)] if rows else ["  no entries yet"])
    rows = [r for r in sh.summary(sh.order() + sh.own.deleted()) if r["entries"] or r["open"]]
    if len(versions) > 1 and rows:
        out += ["", "all settings together"] + ["  " + l for l in table(rows)]
    out += _own_lines(sh, rows)
    return out


def _own_lines(sh, rows: list[dict]) -> list[str]:
    """The profile's own variants: each one's rule, and whether it was deleted."""
    have = {r["name"] for r in rows}
    own = [(k, v) for k, v in sh.own.all.items() if not v["deleted"] or k in have]
    return ["", "its own variants:"] + [
        f"  {k}" + (f" ({v['title']})" if v["title"] != k else "") + f": {describe(v['rule'])}" +
        (f", from {v['from']}" if v["from"] else "") + (" · deleted" if v["deleted"] else "") for k, v in own] if own else []


def shadow_names(sh) -> list[str]:
    return list(ORDER) + list(RETIRED) + list(sh.own.all)


def _books(rows: list[tuple], room: int | None = None) -> list[str]:
    """(name, variant, settings, records) rows under a header; with room, long names are cut so a line fits in it."""
    b = max([8] + [len(r[1]) for r in rows])
    w = max([7] + [len(r[0]) for r in rows])
    rest = 2 + b + sum(1 + n for n in (3, 5, 7, 9, 10, 6))
    if room is not None:
        w = min(w, max(8, room - rest))
    out = [f"{'profile':<{w}}  {'book':<{b}} {'set':>3} {'hours':>5} {'entries':>7} {'P&L':>9} {'¢/contract':>10} {'t':>6}"]
    for name, variant, sv, sh in rows:
        r = sh.summary([variant])[0]
        pc = f"{r['per_contract'] * 100:+.2f}" if r["per_contract"] is not None else "-"
        t = f"{r['t']:+.2f}" if r["t"] is not None else "-"
        name = name if len(name) <= w else name[:w - 1] + "…"
        out.append(f"{name:<{w}}  {variant:<{b}} {sv:>3} {r['hours']:>5} {r['entries']:>7} {r['pnl']:>+9.2f} {pc:>10} {t:>6}")
    return out


# ---------------------------------------------------------------- what there is to read

@dataclass
class Entry:
    id: str                 # desk, profiles, profile:<id>, deleted or saved:<file>
    label: str              # what the list calls it
    saved: bool = False


def index(runtime: str) -> list[Entry]:
    """Every report there is: the live ones, then the saved ones, newest first."""
    out = [Entry("desk", "variants"), Entry("profiles", "profiles"), Entry("profile:1", profmod.Names(runtime).profile)]
    found = []
    for pid in profmod._ids(runtime):
        try:
            with open(os.path.join(runtime, profmod.PROFILES_DIR, pid, "profile.json")) as fh:
                d = json.load(fh)
            found.append((float(d.get("created") or 0), pid, str(d["name"])[:profmod.NAME_MAX]))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    out += [Entry(f"profile:{pid}", name) for _, pid, name in sorted(found)]
    out.append(Entry("deleted", "deleted"))
    try:
        names = os.listdir(runtime)
    except OSError:
        names = []
    return out + [Entry(f"saved:{n}", SAVED_AT.sub(r"\1 \2:\3 \4", stem), True)
                  for stem, n in sorted(((SAVED.match(n)[1], n) for n in names if SAVED.match(n)), reverse=True)]


def command(rid: str) -> str:
    """What prints a live report."""
    kind, _, arg = rid.partition(":")
    return "tern report" + (f" --profile {arg}" if kind == "profile" else
                            {"profiles": " --profiles", "deleted": " --deleted"}.get(rid, ""))


def save(runtime: str, rid: str, now: float | None = None) -> str:
    """Keep a live report as it is now; returns its file's name in the runtime folder."""
    label = next((e.label for e in index(runtime) if e.id == rid and not e.saved), None)
    if label is None:
        raise LookupError(f"no report {rid!r}")
    return save_text(runtime, label, command(rid), lines(runtime, rid), now)


def save_text(runtime: str, label: str, source: str, text: list[str], now: float | None = None) -> str:
    """Write report-<day>-<time>-<label>.txt, never over one that is there; returns its name."""
    at = dt.datetime.fromtimestamp(time.time() if now is None else now)
    taken: set[str] = set()
    while True:
        tail = profmod.slug(label, taken)
        name = f"report-{at:%Y-%m-%d-%H%M}-{tail}.txt"
        path = os.path.join(runtime, name)
        try:
            fh = open(path, "x", encoding="utf-8")
        except FileExistsError:
            taken.add(tail)
            continue
        try:
            with fh:
                fh.write("\n".join([f"{at:%Y-%m-%d %H:%M} · {source}", "", *text]) + "\n")
        except OSError:
            os.remove(path)                           # no half of a report is left behind
            raise
        return name


def lines(runtime: str, rid: str, room: int | None = None) -> list[str]:
    """A report's lines. LookupError for an id there is no such report of."""
    kind, _, arg = rid.partition(":")
    if rid == "desk":
        return desk(runtime)
    if rid == "profiles":
        return side_by_side(runtime, room)
    if rid == "deleted":
        return deleted(runtime, room)
    if kind == "profile" and (arg == "1" or arg in profmod._ids(runtime)):
        return of_profile(runtime, arg)
    if kind == "saved" and SAVED.match(arg):
        with open(os.path.join(runtime, arg), encoding="utf-8", errors="replace") as fh:
            text = fh.read(SAVED_MAX)
        return ["".join(c for c in l.expandtabs() if c.isprintable()) for l in text.splitlines()]
    raise LookupError(f"no report {rid!r}")


FILES = ("config.json", "profile.json", "names.json", "variants.json", "settings-log.jsonl", "shadow.json", "shadow-settled.jsonl")


def _stamp(runtime: str, rid: str) -> tuple:
    """Changes when a file the report is made from does."""
    kind, _, arg = rid.partition(":")
    root = os.path.join(runtime, profmod.DELETED_DIR if rid == "deleted" else profmod.PROFILES_DIR)
    if kind == "saved":
        paths = [os.path.join(runtime, arg)]
    else:
        try:
            each = [os.path.join(root, n) for n in sorted(os.listdir(root))] if rid in ("profiles", "deleted") else []
        except OSError:
            each = []
        dirs = [] if rid == "deleted" else [runtime] if kind != "profile" or arg == "1" else [os.path.join(root, arg)]
        paths = [os.path.join(d, f) for d in dirs + each for f in FILES] + [os.path.join(runtime, "config.json")]
    out = []
    for p in paths:
        try:
            s = os.stat(p)
            out.append((p, s.st_mtime_ns, s.st_size))
        except OSError:
            out.append((p, 0, 0))
    return tuple(out)


@dataclass
class Report:
    lines: list[str]
    at: float               # when it was worked out


class Feed:
    """Reports for a screen that must not wait: each is worked out on a thread, kept, and worked out again
    once one of its files changes. The web's viewers share one."""
    KEEP = 64

    def __init__(self, check: float = 1.0):
        self.check = check                            # seconds between looks at a report's files
        self.lock = threading.Lock()
        self.have: dict[tuple, tuple[tuple, Report]] = {}
        self.looked: dict[tuple, float] = {}
        self.busy: set[tuple] = set()

    def get(self, runtime: str, rid: str, room: int | None = None) -> Report | None:
        """The report as last worked out (None: not yet)."""
        key = (runtime, rid, room if rid in ("profiles", "deleted") else None)
        with self.lock:
            old = self.have.get(key)
            if key in self.busy or old and time.time() - self.looked.get(key, 0) < self.check:
                return old[1] if old else None
            self.looked[key] = time.time()
            stamp = _stamp(runtime, rid)
            if old and old[0] == stamp:
                return old[1]
            self.busy.add(key)
        threading.Thread(target=self._work, args=(key, stamp), daemon=True).start()
        return old[1] if old else None

    def _work(self, key: tuple, stamp: tuple) -> None:
        try:
            got = lines(*key)
        except Exception as ex:                       # a broken file must not stop the screen
            got = ["this report could not be read:", str(ex) or type(ex).__name__]
            stamp = ()                                # tried again at the next look
        with self.lock:
            self.have.pop(key, None)
            self.have[key] = (stamp, Report(got, time.time()))
            while len(self.have) > self.KEEP:
                k = next(iter(self.have))
                self.have.pop(k); self.looked.pop(k, None)
            self.busy.discard(key)


FEED = Feed()
