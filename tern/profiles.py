"""Profiles: whole copies of the desk on paper, run side by side with it.

Profile 1 is the desk itself (runtime/), with its paper book, live, the gate and its variant records; nothing
here moves or rewrites its files. Each other profile is a folder under runtime/profiles/ with its own
settings (config.json: only its strategy and paper start are used), its own record for every variant run
under those settings (shadow.json, shadow-settled.jsonl, as runtime/ has), and a log of every settings
change (settings-log.jsonl). The engine runs every enabled profile's variants on the same snapshot it trades
profile 1 on, so a difference between profiles comes from their settings, not from timing.

A profile never sends an order. Its "book" is the record of the variant it picked, shown on its paper start.

Settings versions: each profile (profile 1 too) counts its settings, starting at 1. A change to a field that
changes how records are made (RECORD_KEYS) bumps the version, and every new record row is stamped with it,
so results can be split by settings period. Rows from before versions existed count as version 0.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time

from . import config as cfgmod
from .config import SERIES, Config, Setting, Strategy
from .shadow import ORDER, VARIANTS, Shadow

MAX_PROFILES = 8                                 # besides profile 1
SLOT_CHOICES = (30, 25, 20, 15, 10, 5)           # minutes before the close a profile may enter at
PROFILES_DIR = "profiles"
DELETED_DIR = "profiles-deleted"
NAME_MAX = 40

# fields that change how variant records are made: a change bumps the settings version
RECORD_KEYS = ("strategy.band_lo", "strategy.band_hi", "strategy.minutes", "strategy.max_spread",
               "strategy.series", "strategy.contracts", "strategy.max_hour_cost")


def _paper(key: str) -> Setting:
    r = cfgmod.setting(key)
    assert r is not None, key
    return Setting(r.sec, r.name, r.label, r.lo, r.hi, r.step, r.kind, 0, r.what, "PAPER" if key == "strategy.contracts" else "")


# The settings box's rows for profiles 2+. No live or gate rows: profiles never trade live.
PROFILE_SETTINGS = [
    Setting("strategy", "variant", "book trades", kind="variant", group="PROFILE",
            what="the variant whose record this profile's book shows; every variant keeps running"),
    Setting("", "enabled", "running", kind="bool",
            what="off: no new entries, and the hours while off don't count; open ones still settle"),
    Setting("strategy", "band_lo", "band low", 0.50, 0.98, 0.01, kind="p", group="RULE",
            what="the favorite's mid must be at least this (a variant with its own band keeps it)"),
    Setting("strategy", "band_hi", "band high", 0.51, 0.99, 0.01, kind="p",
            what="... and below this"),
    Setting("strategy", "minutes", "slots", kind="slots",
            what="minutes before the close the rule enters at; at least one stays on"),
    Setting("strategy", "max_spread", "max spread", 0.01, 0.10, 0.01, kind="p",
            what="widest bid-ask spread a strike may have"),
    Setting("strategy", "series", "coins", kind="coins",
            what="the coins the rule trades (multi trades all of them); at least one stays on"),
    _paper("strategy.contracts"),
    _paper("strategy.max_hour_cost"),
    _paper("paper_capital"),
]
PSET = {r.key: r for r in PROFILE_SETTINGS}

load_errors: dict[str, str] = {}                 # folder -> why it would not load, from the last load_all()


def _get(cfg: Config, key: str):
    sec, _, name = key.rpartition(".")
    return getattr(getattr(cfg, sec) if sec else cfg, name)


def diff_record(old: Strategy, new: Strategy) -> list[tuple[str, object, object]]:
    """(key, was, now) for each record field that differs between two strategies."""
    out = []
    for key in RECORD_KEYS:
        name = key.split(".", 1)[1]
        a, b = getattr(old, name), getattr(new, name)
        if a != b:
            out.append((key, copy.copy(a), copy.copy(b)))
    return out


class SettingsLog:
    """settings-log.jsonl: one line per change, {"ts", "sv", "key", "from", "to"}. sv is the version after it."""

    def __init__(self, path: str):
        self.path = path
        self.sv = 1
        for line in self.lines():
            if isinstance(line.get("sv"), int):
                self.sv = line["sv"]

    def lines(self) -> list[dict]:
        out = []
        try:
            with open(self.path) as fh:
                for raw in fh:
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(d, dict):
                        out.append(d)
        except OSError:
            pass
        return out

    def append(self, key: str, was, now, bump: bool) -> int:
        if bump:
            self.sv += 1
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), "sv": self.sv, "key": key, "from": was, "to": now},
                                separators=(",", ":")) + "\n")
        return self.sv


def slug(name: str, taken: set[str]) -> str:
    """A folder name for a profile: lowercase letters, digits and dashes, unique among `taken`."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:NAME_MAX] or "profile"
    out, i = base, 2
    while out in taken or out == "1":
        out, i = f"{base}-{i}", i + 1
    return out


class Profile:
    def __init__(self, dir: str, name: str, enabled: bool, created: float, cfg: Config, fee: float):
        self.dir = dir
        self.id = os.path.basename(dir)
        self.name, self.enabled, self.created, self.cfg = name, enabled, created, cfg
        self.log = SettingsLog(os.path.join(dir, "settings-log.jsonl"))
        self.shadow = Shadow(dir, fee)

    @classmethod
    def load(cls, dir: str, fee: float) -> "Profile":
        with open(os.path.join(dir, "profile.json")) as fh:
            meta = json.load(fh)
        if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
            raise ValueError("profile.json has no name")
        if not os.path.exists(os.path.join(dir, "config.json")):
            raise ValueError("no config.json")
        cfg = cfgmod.load(dir)
        return cls(dir, meta["name"][:NAME_MAX], bool(meta.get("enabled", True)), float(meta.get("created") or 0), cfg, fee)

    def save_meta(self) -> None:
        path = os.path.join(self.dir, "profile.json")
        with open(path + ".tmp", "w") as fh:
            json.dump({"name": self.name, "enabled": self.enabled, "created": self.created, "sv": self.log.sv}, fh)
        os.replace(path + ".tmp", path)

    @property
    def variant(self) -> str:
        v = self.cfg.strategy.variant
        return v if v in VARIANTS else "base"

    def reload_cfg(self) -> list[tuple]:
        """Re-read config.json (a hand edit). Unreadable keeps the settings in use. Each changed record
        field is logged and bumps the version; a changed variant is logged without a bump."""
        new = cfgmod.load(self.dir, self.cfg)
        if new is self.cfg:
            return []
        changes = diff_record(self.cfg.strategy, new.strategy)
        for key, was, now in changes:
            self.log.append(key, was, now, bump=True)
        if new.strategy.variant != self.cfg.strategy.variant:
            self.log.append("strategy.variant", self.cfg.strategy.variant, new.strategy.variant, bump=False)
        if new.paper_capital != self.cfg.paper_capital:
            self.log.append("paper_capital", self.cfg.paper_capital, new.paper_capital, bump=False)
        self.cfg = new
        if changes:
            self.save_meta()
        return changes

    def check(self, key: str, value):
        """The value as it will be stored, or ValueError with a message the screen can show."""
        r = PSET.get(key)
        if r is None:
            raise ValueError(f"{key!r} is not a profile setting")
        S = self.cfg.strategy
        if r.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{r.label} must be on or off")
            return value
        if r.kind == "variant":
            if value not in VARIANTS:
                raise ValueError(f"unknown variant {value!r} (known: {', '.join(ORDER)})")
            return value
        if r.kind in ("slots", "coins"):
            ok = SLOT_CHOICES if r.kind == "slots" else tuple(SERIES)
            if not isinstance(value, list) or any(isinstance(x, bool) or x not in ok for x in value):
                raise ValueError(f"{r.label} must be chosen from {', '.join(map(str, ok))}")
            if not value:
                raise ValueError(f"at least one of the {r.label} stays on")
            return [x for x in ok if x in value]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise ValueError(f"{r.label} must be a number")
        if not r.lo <= value <= r.hi + 1e-9:
            raise ValueError(f"{r.label} must be between {r.lo:g} and {r.hi:g}")
        if r.kind == "p":
            value = round(float(value), 2)
            if key == "strategy.band_lo" and value >= S.band_hi:
                raise ValueError(f"band low must stay below band high ({S.band_hi * 100:.0f}¢)")
            if key == "strategy.band_hi" and value <= S.band_lo:
                raise ValueError(f"band high must stay above band low ({S.band_lo * 100:.0f}¢)")
            return value
        return float(int(value)) if r.kind == "n" else float(value)

    def set(self, key: str, value) -> tuple[object, object]:
        """Change one setting: checked, saved, logged. Returns (was, now)."""
        value = self.check(key, value)
        if key == "enabled":
            was, self.enabled = self.enabled, value
            self.save_meta()
        else:
            was = copy.copy(_get(self.cfg, key))
            sec, _, name = key.rpartition(".")
            setattr(getattr(self.cfg, sec) if sec else self.cfg, name, value)
            cfgmod.save(self.cfg, self.dir)
        if was != value:
            self.log.append(key, was, value, bump=key in RECORD_KEYS)
            self.save_meta()
        return was, value

    def step(self, event: str, close: float, markets: list[dict], now: float, vol: float | None) -> None:
        if self.enabled:
            self.shadow.step(event, close, markets, self.cfg.strategy, now, vol=vol, sv=self.log.sv)


def _root(runtime: str) -> str:
    return os.path.join(runtime, PROFILES_DIR)


def _ids(runtime: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(_root(runtime)) if os.path.isdir(os.path.join(_root(runtime), n)))
    except OSError:
        return []


def load_all(runtime: str, fee: float) -> list[Profile]:
    """Every profile folder that loads, oldest first. Those that don't are named in load_errors."""
    out = []
    load_errors.clear()
    for pid in _ids(runtime):
        try:
            out.append(Profile.load(os.path.join(_root(runtime), pid), fee))
        except Exception as ex:                  # a broken folder must not stop the rest
            load_errors[pid] = str(ex) or type(ex).__name__
    return sorted(out, key=lambda p: (p.created, p.id))


def create(runtime: str, name: str, base: Config, fee: float) -> Profile:
    """A new profile: a copy of base's strategy and paper start, empty records, settings version 1."""
    taken = set(_ids(runtime))
    if len(taken) >= MAX_PROFILES:
        raise ValueError(f"at most {MAX_PROFILES} profiles besides profile 1: delete one first")
    name = " ".join(name.split())[:NAME_MAX] or f"profile {len(taken) + 2}"
    pid = slug(name, taken)
    d = os.path.join(_root(runtime), pid)
    os.makedirs(d)
    cfg = Config(paper_capital=base.paper_capital, taker_fee=base.taker_fee,
                 strategy=Strategy(**copy.deepcopy(base.strategy.__dict__)))
    cfgmod.save(cfg, d)
    p = Profile(d, name, True, time.time(), cfg, fee)
    p.log.append("created", None, name, bump=False)
    p.save_meta()
    return p


def delete(runtime: str, pid: str) -> str:
    """Move a profile's folder to runtime/profiles-deleted/<id>-<YYYYMMDD-HHMM>/ and return where it went."""
    if not pid or pid not in _ids(runtime):
        raise ValueError(f"no profile {pid!r}")
    to_root = os.path.join(runtime, DELETED_DIR)
    os.makedirs(to_root, exist_ok=True)
    to = base = os.path.join(to_root, f"{pid}-{time.strftime('%Y%m%d-%H%M')}")
    i = 2
    while os.path.exists(to):
        to, i = f"{base}-{i}", i + 1
    shutil.move(os.path.join(_root(runtime), pid), to)
    return to
