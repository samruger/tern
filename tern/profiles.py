"""Profiles: whole copies of the desk on paper, run side by side with it.

Profile 1 is the desk itself (runtime/), with its paper book, live, the gate and its variant records; nothing
here moves or rewrites its files. Each other profile is a folder under runtime/profiles/ with its own
settings (config.json: only its strategy and paper start are used), its own variants (variants.json, as
runtime/ may have), its own record for every variant it runs (shadow.json, shadow-settled.jsonl, as runtime/
has), and a log of every settings change (settings-log.jsonl). The engine runs every enabled profile's variants on the same snapshot it trades
profile 1 on, so a difference between profiles comes from their settings, not from timing.

A profile never sends an order itself. Its "book" is the record of the variant it picked, shown on its paper
start. When d makes it the live profile (config live.profile), the engine's live book trades its rule, once
its own record of its variant passes the gate.

Settings versions: each profile (profile 1 too) counts its settings, starting at 1. A change to a field that
changes how records are made (RECORD_KEYS) bumps the version, and every new record row is stamped with it,
so results can be split by settings period. Rows from before versions existed count as version 0. Changing a
rule from the screen never does that: it makes a new variant (variants.py) with its own record.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time

from . import config as cfgmod
from . import variants as varmod
from .config import SERIES, Config, Setting, Strategy
from .shadow import ORDER, VARIANTS, Shadow, known, own_set, rule_of
from .variants import SLOT_CHOICES

MAX_PROFILES = 8                                 # besides profile 1
PROFILES_DIR = "profiles"
DELETED_DIR = "profiles-deleted"
NAME_MAX = 40
NAMES_FILE = "names.json"                        # runtime/: profile 1's name and the built-in variants' display names
VARIANT_NAME_MAX = varmod.NAME_MAX

# fields that change how variant records are made: a change bumps the settings version
RECORD_KEYS = ("strategy.band_lo", "strategy.band_hi", "strategy.minutes", "strategy.max_spread",
               "strategy.series", "strategy.contracts", "strategy.max_hour_cost")


# what the book trades: enter in the variants box, not a row of the settings box
VARIANT_SETTING = Setting("strategy", "variant", "book trades", kind="variant",
                          what="the variant whose rule this profile trades; every variant keeps running")
# A profile's own rows in the settings box (profile 1 has none: it always runs). How it trades is in its variants.
PROFILE_SETTINGS = [
    Setting("", "enabled", "running", kind="bool", group="PROFILE",
            what="off: no new entries, and the hours while off don't count; open ones still settle"),
]
SCREEN_KEYS = ("enabled", "strategy.variant")    # all of a profile the screen may set (profile_set)
# The rule, size and paper start of the built-in variants, in config.json. The screen never changes these (a change
# there is a new variant); a hand edit still does, and is logged.
BASE_RULE = [
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
    *(Setting("strategy" if n != "paper_capital" else "", n, r.label, r.lo, r.hi, r.step, r.kind, what=r.what)
      for n, r in varmod.RULE.items() if n in varmod.SIZES),
]
PSET = {r.key: r for r in [*PROFILE_SETTINGS, VARIANT_SETTING, *varmod.RULE_SETTINGS, *BASE_RULE]}
RULE_KEYS = {f"strategy.{f}": f for f in ("band_lo", "band_hi", "minutes", "max_spread", "series")}

ROW_KEYS = ("v", "ts", "event", "close", "ticker", "side", "n", "cost", "fee", "slot")   # an open row needs every one
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


def show(key: str, v) -> str:
    """A setting's value the way the screen and the report say it: 82¢, 25 20 15, BTC ETH, on, 5."""
    r = PSET.get(key) or cfgmod.setting(key)
    kind = r.kind if r else ""
    if kind == "bool" or isinstance(v, bool):
        return "on" if v else "off"
    if kind == "p":
        return f"{v * 100:.0f}¢"
    if kind == "cap":
        return f"{v:g}" if v else "off"
    if isinstance(v, list):
        return " ".join(SERIES.get(x, str(x)).split("-")[0] if isinstance(x, str) else str(x) for x in v)
    if kind == "usd":
        return f"${v:,.2f}".replace(".00", "")
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def label(key: str) -> str:
    r = PSET.get(key) or cfgmod.setting(key)
    return ({"live": "live ", "gate": "gate "}.get(r.sec, "") + r.label) if r else key


def slug(name: str, taken: set[str]) -> str:
    """A folder name for a profile: lowercase letters, digits and dashes, unique among `taken`."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:NAME_MAX] or "profile"
    out, i = base, 2
    while out in taken or out == "1":
        out, i = f"{base}-{i}", i + 1
    return out


def _clean(name) -> str:
    return " ".join(name.split()) if isinstance(name, str) else ""


class Names:
    """runtime/names.json: {"profile": profile 1's name, "variants": {variant: display name}}.

    A variant's display name is only what the screens call it: its records, the gate, config.json's
    strategy.variant and `tern report` keep the name in shadow.TABLE, so renaming never splits a record.
    Profiles 2+ keep their names in their own profile.json (Profile.rename)."""

    DEFAULT = "profile 1"

    def __init__(self, runtime: str):
        self.path = os.path.join(runtime, NAMES_FILE)
        self.profile, self.variants = self.DEFAULT, {}
        try:
            with open(self.path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        if isinstance(d, dict):
            self.profile = _clean(d.get("profile"))[:NAME_MAX] or self.DEFAULT
            vs = d.get("variants") if isinstance(d.get("variants"), dict) else {}
            self.variants = {k: _clean(v)[:VARIANT_NAME_MAX] for k, v in vs.items()
                             if k in VARIANTS and _clean(v) and _clean(v) != k}

    def save(self) -> None:
        with open(self.path + ".tmp", "w") as fh:
            json.dump({"profile": self.profile, "variants": self.variants}, fh)
        os.replace(self.path + ".tmp", self.path)

    def variant(self, name: str) -> str:
        return self.variants.get(name, name)

    def shown(self) -> list[str]:
        """What the built-in variants are called on the screen: no own variant may be called that."""
        return [self.variant(n) for n in ORDER]

    def rename_variant(self, name: str, to: str, taken=()) -> tuple[str, str]:
        """(was, now). An empty name, or the variant's own, goes back to the variant's own name. taken: what
        the profiles' own variants are called."""
        if name not in VARIANTS:
            raise ValueError(f"unknown variant {name!r} (known: {', '.join(ORDER)})")
        to = _clean(to)
        if any(to.lower() == t.lower() for t in taken):
            raise ValueError(f"{to!r} is already a variant's name")
        if len(to) > VARIANT_NAME_MAX:
            raise ValueError(f"a variant's name is at most {VARIANT_NAME_MAX} characters")
        to = to or name
        for other in ORDER:                      # no two variants may look alike, by key or by display name
            if other != name and to.lower() in (other.lower(), self.variant(other).lower()):
                raise ValueError(f"{to!r} is already {other}'s name")
        was = self.variant(name)
        if to == name:
            self.variants.pop(name, None)
        else:
            self.variants[name] = to
        self.save()
        return was, to


class Profile:
    def __init__(self, dir: str, name: str, enabled: bool, created: float, cfg: Config, fee: float):
        self.dir = dir
        self.id = os.path.basename(dir)
        self.name, self.enabled, self.created, self.cfg = name, enabled, created, cfg
        self.log = SettingsLog(os.path.join(dir, "settings-log.jsonl"))
        self.shadow = Shadow(dir, fee)

    @classmethod
    def load(cls, dir: str, fee: float, upgrade: bool = False) -> "Profile":
        """upgrade: the engine's load, which first turns a rule set before own variants existed into some (upgrade_rules)."""
        if upgrade:
            upgrade_rules(dir)
        with open(os.path.join(dir, "profile.json")) as fh:
            meta = json.load(fh)
        if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
            raise ValueError("profile.json has no name")
        # strict: a profile whose settings can't be read is skipped (and the error logged), never run on defaults
        try:
            with open(os.path.join(dir, "config.json")) as fh:
                cfg = Config.from_dict(json.load(fh))
        except (OSError, ValueError, TypeError, AttributeError) as ex:
            raise ValueError(f"config.json unreadable: {ex}") from None
        p = cls(dir, meta["name"][:NAME_MAX], bool(meta.get("enabled", True)), float(meta.get("created") or 0), cfg, fee)
        rows = p.shadow.rows
        p.shadow.rows = [r for r in rows if isinstance(r, dict) and all(k in r for k in ROW_KEYS)]
        p.shadow.dirty = p.shadow.dirty or len(p.shadow.rows) != len(rows)
        if upgrade:
            p.shadow.own.keep_filled()
        return p

    def save_meta(self) -> None:
        path = os.path.join(self.dir, "profile.json")
        with open(path + ".tmp", "w") as fh:
            json.dump({"name": self.name, "enabled": self.enabled, "created": self.created, "sv": self.log.sv}, fh)
        os.replace(path + ".tmp", path)

    def rename(self, name: str) -> tuple[str, str]:
        """(was, now). The folder (the profile's id) keeps its name; only what it is called changes."""
        name = _clean(name)
        if not name:
            raise ValueError("a profile needs a name")
        if len(name) > NAME_MAX:
            raise ValueError(f"a profile's name is at most {NAME_MAX} characters")
        was, self.name = self.name, name
        if was != name:
            self.log.append("name", was, name, bump=False)
            self.save_meta()
        return was, name

    @property
    def variant(self) -> str:
        v = self.cfg.strategy.variant
        return v if known(v, self.shadow.own) else "base"

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
        if r is None or r.sec == "rule":
            raise ValueError(f"{key!r} is not a profile setting")
        S = self.cfg.strategy
        if r.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{r.label} must be on or off")
            return value
        if r.kind == "variant":
            if not known(value, self.shadow.own):
                raise ValueError(f"unknown variant {value!r} (known: {', '.join(self.shadow.order())})")
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
    own, theirs = own_set(d), own_set(runtime)
    if theirs.has(cfg.strategy.variant):         # profile 1 trades one of its own: this one gets a copy of it
        own.all[cfg.strategy.variant] = {**copy.deepcopy(theirs.all[cfg.strategy.variant]), "made": round(time.time(), 3)}
    own.save()
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


# ------------------------------------------------------------ profiles from before own variants
DONE_FILE = "variants-upgraded"                  # in a profile's folder: upgrade_rules is done with it, for good
BUSY_FILE = "variants-upgrading"                 # ... has started writing: *.pre-variants are complete
OLD = ".pre-variants"


def _rows(path: str) -> tuple[dict, list[dict]] | None:
    """shadow.json and its rows, or None when it can't be read (the load that follows makes do without it)."""
    try:
        with open(path) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return (d, [r for r in d["rows"] if isinstance(r, dict)]) if isinstance(d, dict) and isinstance(d.get("rows"), list) else None


def _put(path: str, text: str) -> None:
    with open(path + ".tmp", "w") as fh:
        fh.write(text)
    os.replace(path + ".tmp", path)


def upgrade_rules(dir: str) -> list[str]:
    """Once per profile folder: the settings box used to change the rule every built-in variant in a profile
    started from. Each rule that made into something else becomes a variant of the profile's own with the entries
    it bought, and the built-in ones go back to their own rules, keeping only what they bought under them. The one
    the book trades, and base, keep running; the others are listed as deleted (their records stay). Files as they
    were are kept as *.pre-variants. Returns the new keys.

    DONE_FILE says it has happened (variants.json alone can't: losing it later must never bring the old files
    back). Everything is worked out before anything is written. From the first write until it is done BUSY_FILE
    is there, and only then does a new start begin again from the files as they were."""
    P = lambda n: os.path.join(dir, n)
    files = ("config.json", "shadow.json", "shadow-settled.jsonl", "settings-log.jsonl")
    busy = os.path.exists(P(BUSY_FILE))
    if os.path.exists(P(DONE_FILE)) or os.path.exists(P(varmod.FILE)):
        # (variants.json is written last, so with it there the upgrade, or a new profile, is complete)
        if not os.path.exists(P(DONE_FILE)):
            _put(P(DONE_FILE), "")
        if busy:
            os.remove(P(BUSY_FILE))
        return []
    if busy:
        for n in files:
            if os.path.exists(P(n) + OLD):
                shutil.copy2(P(n) + OLD, P(n))
    with open(P("config.json")) as fh:
        cfg = Config.from_dict(json.load(fh))    # unreadable: leave everything (the load that follows says so)
    S, std, log = cfg.strategy, Strategy(contracts=cfg.strategy.contracts, max_hour_cost=cfg.strategy.max_hour_cost), SettingsLog(P("settings-log.jsonl"))
    lines = [ln for ln in log.lines() if isinstance(ln.get("sv"), int)]
    own = own_set(dir)
    standard = all(getattr(S, f) == getattr(std, f) for f in RULE_KEYS.values())
    if standard and not busy and os.path.exists(P("config.json") + OLD):
        _put(P(DONE_FILE), "")                   # upgraded before there was a DONE_FILE, and its variants.json is gone
        return []
    if standard and \
            not any(ln.get("key") in RULE_KEYS for ln in lines if isinstance(ln.get("key"), str)):
        own.save()
        _put(P(DONE_FILE), "")
        return []

    # the rule in each settings version, walking the log back from what it is now
    cur = {f: copy.copy(getattr(S, f)) for f in RULE_KEYS.values()}
    at = {log.sv: dict(cur)}
    for ln in reversed(lines):
        at.setdefault(ln["sv"], dict(cur))
        if isinstance(ln.get("key"), str) and ln["key"] in RULE_KEYS:
            cur[RULE_KEYS[ln["key"]]] = ln.get("from")
            at.setdefault(ln["sv"] - 1, dict(cur))

    def under(name: str, sv) -> dict | None:
        """A built-in variant's rule in settings version sv, when the log says one that can run."""
        try:
            rule = rule_of(name, Config(paper_capital=cfg.paper_capital, strategy=Strategy(**{**std.__dict__, **at[sv]})))
            return None if varmod.why_bad(rule) else rule
        except (TypeError, ValueError, KeyError):
            return None

    open_rows = _rows(P("shadow.json"))
    try:
        with open(P("shadow-settled.jsonl")) as fh:
            settled = fh.readlines()
    except OSError:
        settled = []
    seen = {r["v"] for r in (open_rows[1] if open_rows else []) if isinstance(r.get("v"), str)}
    for raw in settled:
        try:
            seen.add(str(json.loads(raw)["v"]))
        except (ValueError, KeyError, TypeError):
            continue
    made: dict[str, list[str]] = {}              # built-in variant -> the keys its other rules became

    def key_for(name, sv) -> str:
        """The variant an entry by `name` in settings version sv belongs to now."""
        if not isinstance(name, str) or name not in VARIANTS or not isinstance(sv, int) or sv not in at:
            return name
        rule = under(name, sv)
        if rule is None or varmod.same(rule, rule_of(name, Config(paper_capital=cfg.paper_capital, strategy=std))):
            return name
        for k in made.get(name, []):
            if varmod.same(own.all[k]["rule"], rule):
                return k
        stem, i = name[:varmod.NAME_MAX - 3], 2
        while (k := f"{stem}-{i}") in own.all or k in own.reserved or k in seen:
            i += 1
        now = under(name, log.sv)
        live = name in (S.variant, "base") and now is not None and varmod.same(rule, now)
        own.all[k] = {"rule": rule, "title": k, "from": name, "made": round(time.time(), 3), "deleted": not live}
        made.setdefault(name, []).append(k)
        return k

    book = key_for(S.variant, log.sv)            # (a name nothing knows stays: the profile then waits for a pick)
    key_for("base", log.sv)
    out = []
    for raw in settled:
        try:
            r = json.loads(raw)
            k = key_for(r["v"], r.get("sv", 0))
            if k != r["v"]:
                r["v"] = k
                raw = json.dumps(r, separators=(",", ":")) + "\n"
        except (ValueError, KeyError, TypeError):
            pass
        out.append(raw)
    if open_rows:
        for r in open_rows[1]:
            if "v" in r:
                r["v"] = key_for(r["v"], r.get("sv", 0))

    for n in files:                              # from here on it writes: first the files as they were
        if os.path.exists(P(n)) and not busy:
            shutil.copy2(P(n), P(n) + OLD + ".tmp")
            os.replace(P(n) + OLD + ".tmp", P(n) + OLD)
    _put(P(BUSY_FILE), "")
    if settled:
        _put(P("shadow-settled.jsonl"), "".join(out))
    if open_rows:
        _put(P("shadow.json"), json.dumps({**open_rows[0], "rows": open_rows[1]}, separators=(",", ":")))
    for key, f in RULE_KEYS.items():             # the built-in variants go back to their own rules
        was, now = getattr(S, f), copy.copy(getattr(std, f))
        if was != now:
            setattr(S, f, now)
            log.append(key, was, now, bump=True)
    if book != S.variant:
        log.append("strategy.variant", S.variant, book, bump=False)
        S.variant = book
    cfgmod.save(cfg, dir)
    own.save()
    _put(P(DONE_FILE), "")
    os.remove(P(BUSY_FILE))
    return own.active()
