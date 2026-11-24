"""The reports: what `tern report` prints and the screen's reports box (8) shows.

Live reports are worked out from the records each time: the variants side by side, and by settings period. Saved ones are the runtime folder's report-*.txt, shown as
written; save() makes one (s on the screen, tern report --save, the weekly timer). Reading a report changes
nothing: no record or setting is written.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import threading
import time
from dataclasses import dataclass

from . import config as cfgmod
from . import names as namesmod
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
    """The variants side by side: built in, your own, deleted and retired."""
    sh = Shadow(runtime)
    cfg = _config(runtime)
    live = cfg.live.variant if cfg.live.enabled else None
    lost = sorted(sh.own.used() - set(shadow_names(sh)))      # records of variants variants.json no longer has
    w = max(len(n) for n in shadow_names(sh) + lost)
    out = table(sh.summary(), w)
    own = {n: (f"({sh.own.title(n)}) " if sh.own.title(n) != n else "") + desc_of(n, sh.own) for n in sh.own.active()}
    out += [""] + [f"{n}: {d}" + (" · off" if n in sh.own.off else "") + ("  ← live" if n == live else "") for n, d in {**DESC, **own}.items()]
    gone = [r for r in sh.summary(sh.own.deleted()) if r["entries"] or r["open"]] if sh.own.deleted() else []
    if gone:
        out += ["", "deleted, kept for the record:"] + table(gone, w)[1:]
    if lost:
        out += ["", "not in variants.json any more, kept for the record:"] + table(sh.summary(lost), w)[1:]
    old = [r for r in sh.summary(list(RETIRED)) if r["entries"]]
    if old:
        out += ["", "retired, kept for the record:"] + table(old, w)[1:] + [f"{n}: {d}" for n, d in RETIRED.items()]
    return out + ["", "t is by hour and shows after 10 hours."]


def periods(runtime: str) -> list[str]:
    """The variants, one table per settings period."""
    cfg = _config(runtime)
    log = namesmod.SettingsLog(os.path.join(runtime, "settings-log.jsonl"))
    return [f"settings {log.sv}"] + _periods(Shadow(runtime, cfg.taker_fee), log.lines(), log.sv)


def _periods(sh, lines: list[dict], sv_now: int) -> list[str]:
    """One table of the variants per settings period, each headed with when it ran and what changed."""
    day = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    starts: dict[int, float] = {}
    why: dict[int, list[str]] = {}
    for ln in lines:
        sv = ln.get("sv")
        if isinstance(sv, int):
            starts.setdefault(sv, ln.get("ts") or 0)
            if ln.get("key") in namesmod.RECORD_KEYS:
                why.setdefault(sv, []).append(f"{namesmod.label(ln['key'])} {namesmod.show(ln['key'], ln.get('from'))}"
                                              f" → {namesmod.show(ln['key'], ln.get('to'))}")
            elif ln.get("key") == "note":            # a change of tern's own that makes records differently from here
                why.setdefault(sv, []).append(str(ln.get("to")))
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
    """Your own variants: each one's settings, what it was made from, and whether it was deleted."""
    have = {r["name"] for r in rows}
    own = [(k, v) for k, v in sh.own.all.items() if not v["deleted"] or k in have]
    return ["", "your own variants:"] + [
        f"  {k}" + (f" ({v['title']})" if v["title"] != k else "") + f": {describe(v['rule'])}" +
        (f", from {v['from']}" if v["from"] else "") + (f", was in {v['was']}" if v["was"] else "") +
        (" · deleted" if v["deleted"] else "") for k, v in own] if own else []


def shadow_names(sh) -> list[str]:
    return list(ORDER) + list(RETIRED) + list(sh.own.all)


# ---------------------------------------------------------------- what there is to read

@dataclass
class Entry:
    id: str                 # desk, periods or saved:<file>
    label: str              # what the list calls it
    saved: bool = False


def index(runtime: str) -> list[Entry]:
    """Every report there is: the live ones, then the saved ones, newest first."""
    out = [Entry("desk", "variants"), Entry("periods", "by period")]
    try:
        names = os.listdir(runtime)
    except OSError:
        names = []
    return out + [Entry(f"saved:{n}", SAVED_AT.sub(r"\1 \2:\3 \4", stem), True)
                  for stem, n in sorted(((SAVED.match(n)[1], n) for n in names if SAVED.match(n)), reverse=True)]


def command(rid: str) -> str:
    """What prints a live report."""
    return "tern report" + (" --periods" if rid == "periods" else "")


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
        tail = namesmod.slug(label, taken)
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


def lines(runtime: str, rid: str) -> list[str]:
    """A report's lines. LookupError for an id there is no such report of."""
    kind, _, arg = rid.partition(":")
    if rid == "desk":
        return desk(runtime)
    if rid == "periods":
        return periods(runtime)
    if kind == "saved" and SAVED.match(arg):
        with open(os.path.join(runtime, arg), encoding="utf-8", errors="replace") as fh:
            text = fh.read(SAVED_MAX)
        return ["".join(c for c in l.expandtabs() if c.isprintable()) for l in text.splitlines()]
    raise LookupError(f"no report {rid!r}")


FILES = ("config.json", "names.json", "variants.json", "settings-log.jsonl", "shadow.json", "shadow-settled.jsonl")


def _stamp(runtime: str, rid: str) -> tuple:
    """Changes when a file the report is made from does."""
    kind, _, arg = rid.partition(":")
    paths = [os.path.join(runtime, arg)] if kind == "saved" else [os.path.join(runtime, f) for f in FILES]
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

    def get(self, runtime: str, rid: str) -> Report | None:
        """The report as last worked out (None: not yet)."""
        key = (runtime, rid)
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
