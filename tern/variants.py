"""Your own variants: complete rules made on the screen.

A variant here is everything about how it trades: band, slots, spread, coins, the two optional caps, contracts a
strike, most an hour, its limits (most at risk, largest order, daily loss) and its paper start. shadow.TABLE's variants
are built in and take theirs from config.json. No variant ever changes once it is saved: e on the screen opens one as a
draft, and saving that draft makes a new variant with an empty record (engine._variant_new), so a record is always one
rule's.

Paper and live both keep to a variant's limits. One made before limits were part of variants (2026-11-24) got the desk's of
that day, and only live keeps to them (paper_limits False): its record goes on as it was made. So do the built-in ones.

They are in variants.json beside the records, with the variants that are turned off. A variant that bought something and was then deleted
stays listed there as deleted: its open entries still settle and its key is never used again.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import time

from .config import GONE, SERIES, Setting, coin

FILE = "variants.json"
MAX_OWN = 24                                     # of your own
NAME_MAX = 12                                    # a variant's key and display name
KEY = re.compile(r"[a-z][a-z0-9-]{0,11}")
SLOT_CHOICES = (30, 25, 20, 15, 10, 5)           # minutes before the close a rule may enter at

# The rows of a variant's box (e). Values are the variant's own; 0 is "off" in a cap.
RULE_SETTINGS = [
    Setting("rule", "band_lo", "band low", 0.50, 0.98, 0.01, kind="p", group="RULE",
            what="the favorite's mid must be at least this"),
    Setting("rule", "band_hi", "band high", 0.51, 0.99, 0.01, kind="p", what="the favorite's mid must be below this"),
    Setting("rule", "minutes", "slots", kind="slots",
            what="minutes before the close the rule enters at; at least one stays on"),
    Setting("rule", "max_spread", "max spread", 0.01, 0.10, 0.01, kind="p",
            what="widest bid-ask spread a strike may have"),
    Setting("rule", "series", "coins", kind="coins",
            what="the coins the rule trades, each with its own hourly budget; at least one stays on"),
    Setting("rule", "cap", "entries a strike", 0, 6, 1, kind="cap",
            what="most entries on one strike in an hour; off: one at every slot"),
    Setting("rule", "max_vol", "most volatility", 0, 50, 0.1, kind="cap",
            what="skip a slot when the coin's last hour moved more than this (bp a minute); off: never skip"),
    Setting("rule", "contracts", "contracts a strike", 1, 100, 1, kind="n", group="SIZE",
            what="contracts it buys per strike per slot"),
    Setting("rule", "max_hour_cost", "most an hour", 5, 2_000, 5,
            what="$ it may put into one coin's hour"),
    Setting("rule", "max_risk", "most at risk", 5, 10_000, 5, group="LIMITS",
            what="$ of open bets it may have at once, over all its coins"),
    Setting("rule", "max_order", "largest order", 1, 1_000, 1,
            what="$ one buy may cost; a bigger one is cut to fit"),
    Setting("rule", "day_loss", "daily loss limit", 5, 5_000, 5,
            what="$ it may lose since midnight before it stops for the day; live turns off"),
    Setting("rule", "paper_capital", "paper start", 100, 1_000_000, 100, group="PAPER",
            what="the cash it starts with; it never buys more than it has left"),
]
SIZES = ("contracts", "max_hour_cost", "paper_capital")   # what variants saved before 2026-11-23 evening took from config.json
LIMITS = ("max_risk", "max_order", "day_loss")            # and before 2026-11-24
FILLED = (*SIZES, *LIMITS)
RULE = {r.name: r for r in RULE_SETTINGS}
FIELDS = tuple(RULE)


def keyish(name: str) -> str:
    """A name as a key would be: lowercase letters, digits and dashes, starting with a letter, at most NAME_MAX."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rule"
    return ("v" + base if not base[0].isalpha() else base)[:NAME_MAX].strip("-")


def check(field: str, value, rule: dict):
    """The value as it will be stored in rule, or ValueError with a message the screen can show."""
    r = RULE.get(field)
    if r is None:
        raise ValueError(f"{field!r} is not part of a rule")
    if r.kind in ("slots", "coins"):
        # a slot the rule already has (config.json's may be any minute) may stay, whether or not the screen offers it
        ok = tuple(sorted({*SLOT_CHOICES, *rule["minutes"]}, reverse=True)) if r.kind == "slots" else tuple(SERIES)
        if not isinstance(value, list) or any(isinstance(x, bool) or x not in ok for x in value):
            raise ValueError(f"{r.label} must be chosen from {', '.join(map(str, ok))}")
        if not value:
            raise ValueError(f"at least one of the {r.label} stays on")
        return [x for x in ok if x in value]
    if r.kind == "cap" and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{r.label} must be a number")
    if not r.lo - 1e-9 <= value <= r.hi + 1e-9:
        raise ValueError(f"{r.label} must be between {r.lo:g} and {r.hi:g}")
    if r.kind == "cap":
        value = int(value) if field == "cap" else round(float(value), 1)
        return value or None
    if r.kind == "n":
        return float(int(value))
    value = round(float(value), 2)
    if field == "band_lo" and value >= rule["band_hi"]:
        raise ValueError(f"band low must stay below band high ({rule['band_hi'] * 100:.0f}¢)")
    if field == "band_hi" and value <= rule["band_lo"]:
        raise ValueError(f"band high must stay above band low ({rule['band_lo'] * 100:.0f}¢)")
    return value


def checked(changes, rule: dict) -> dict:
    """A whole draft, as it will be stored, or ValueError. The band's two ends are judged by where both end up."""
    if not isinstance(changes, dict):
        raise ValueError("the changes are not a list of settings")
    ends = {**rule, **{f: changes[f] for f in ("band_lo", "band_hi") if f in changes}}
    try:
        return {f: check(f, v, ends) for f, v in changes.items()}
    except TypeError:                            # the other end of the band is not a number
        raise ValueError("the band must be two numbers") from None


def as_rule(params: tuple, contracts: float, max_hour_cost: float, limits: tuple, paper_capital: float) -> dict:
    """shadow.params()'s tuple, with the size, the limits and the paper start, as a rule."""
    lo, hi, cap, mins, spread, coins, mvol = params
    return {"band_lo": lo, "band_hi": hi, "minutes": list(mins), "max_spread": spread, "series": list(coins),
            "cap": cap, "max_vol": mvol, "contracts": contracts, "max_hour_cost": max_hour_cost,
            **dict(zip(LIMITS, limits)), "paper_capital": paper_capital}


def same(a: dict, b: dict) -> bool:
    key = lambda r: {**r, "minutes": sorted(r["minutes"]), "series": sorted(r["series"])}
    return key(a) == key(b)


def describe(rule: dict, limits: bool = False) -> str:
    """A rule in a line, as the variants box and tern report say it; its limits too, if its paper keeps to them."""
    out = [f"{rule['band_lo'] * 100:.0f}-{rule['band_hi'] * 100:.0f}¢", "slots " + " ".join(str(m) for m in rule["minutes"]),
           f"≤{rule['max_spread'] * 100:.0f}¢ wide", " ".join(coin(c) for c in rule["series"])]
    if rule.get("cap"):
        out.append(f"{rule['cap']} a strike")
    if rule.get("max_vol"):
        out.append(f"vol ≤{rule['max_vol']:g}")
    out.append(f"{rule['contracts']:g}/strike · ${rule['max_hour_cost']:g}/h")
    if limits:
        out.append(f"risk ${rule['max_risk']:g} · order ${rule['max_order']:g} · day -${rule['day_loss']:g}")
    return " · ".join(out)


def clean(name) -> str:
    return " ".join(name.split()) if isinstance(name, str) else ""


def why_bad(rule) -> str:
    """Why a rule can't run, or "". What add() saves and what loads are judged by this alone, so nothing is
    saved that the next start would drop. It is looser than the screen's bounds: a copy of a built-in variant
    may hold values only config.json can set."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if not isinstance(rule, dict) or set(rule) != set(FIELDS):
        return "it is not a whole rule"
    if not all(num(rule[f]) for f in ("band_lo", "band_hi", "max_spread")):
        return "its band or spread is not a number"
    if not rule["band_lo"] < rule["band_hi"]:
        return "its band is empty"
    if not isinstance(rule["minutes"], list) or not rule["minutes"] or not all(num(m) and m > 0 for m in rule["minutes"]):
        return "it has no slots"
    if not isinstance(rule["series"], list) or not rule["series"] or not all(isinstance(c, str) for c in rule["series"]):
        return "it has no coins"
    if rule["cap"] is not None and not (num(rule["cap"]) and rule["cap"] == int(rule["cap"]) >= 1):
        return "its entries a strike is not a count"
    if rule["max_vol"] is not None and not (num(rule["max_vol"]) and rule["max_vol"] > 0):
        return "its most volatility is not a number"
    if not all(num(rule[f]) and rule[f] >= 0 for f in FILLED):     # (a size of 0, config.json's only, buys nothing)
        return "its size, limits or paper start is not a number"
    return ""


def _loaded(key, d, sizes: dict) -> dict | None:
    """One variant from variants.json, or None when it can't be used. Its name and dates are only for show.
    sizes: what one saved before variants held their size, limits and paper start gets."""
    if not (isinstance(key, str) and KEY.fullmatch(key) and isinstance(d, dict)):
        return None
    if isinstance(d.get("rule"), dict):
        had = all(f in d["rule"] for f in LIMITS)
        d = {"paper_limits": had, **d, "rule": {**sizes, **d["rule"]}}
    if why_bad(d.get("rule")):
        return None
    if not isinstance(d.get("deleted", False), bool):
        return None
    title, made = clean(d.get("title")), d.get("made")
    return {"rule": d["rule"], "title": title if 0 < len(title) <= NAME_MAX else key,
            "from": d["from"] if isinstance(d.get("from"), str) else "",
            "was": d["was"] if isinstance(d.get("was"), str) else "",
            "made": made if isinstance(made, (int, float)) else 0, "deleted": d.get("deleted", False),
            "paper_limits": d["paper_limits"] is True}


class VariantSet:
    """The variants made on the screen. reserved: the names shadow.py holds (built in and retired). sizes: config.json's
    contracts, most an hour, limits and paper start, for variants saved before they held their own; guessed: config.json
    could not be read, so sizes are the defaults and are never saved as those variants' own."""

    def __init__(self, dir: str, reserved=(), sizes: dict | None = None, guessed: bool = False):
        self.path = os.path.join(dir, FILE)
        self.reserved = tuple(reserved)
        self.all: dict[str, dict] = {}           # key -> {rule, title, from, made, deleted, was (the profile it came
                                                 # from), paper_limits}
        self.off: set[str] = set()               # turned off (space), built in or own: no new entries
        self.changes = 0                         # goes up with every change: the engine's summaries cache on it
        self.by: dict[str, str] = {}             # variant -> the id of the command that made it, since the start
        self.used = lambda: set()                # keys that have records (Shadow sets it): never given to another
        self.unsized: dict[str, list] = {}       # variant -> the fields it got from sizes: keep_filled() saves them
        self.guessed = guessed
        self.trimmed: dict[str, list] = {}       # variant -> its coins that are not traded any more, taken out as it loaded
        self.off_lost = False                    # which ones are off could not be read: all of them are on
        self.damaged = False                     # something in the file would not load: save() keeps a copy of it
        try:
            with open(self.path) as fh:
                whole = json.load(fh)
            items = whole["variants"]
            sizes = {**{"contracts": 5.0, "max_hour_cost": 60.0, "paper_capital": 1000.0,
                        "max_risk": 100.0, "max_order": 25.0, "day_loss": 30.0}, **(sizes or {})}
            got = {k: _loaded(k, v, sizes) for k, v in items.items() if k not in self.reserved}
            self.all = {k: v for k, v in got.items() if v is not None}
            self.unsized = {k: fs for k in self.all if (fs := [f for f in FILLED if f not in items[k]["rule"]])}
            self.damaged = len(self.all) != len(items)
            for k, v in self.all.items():        # (a deleted one's rule says what it traded)
                left = [c for c in v["rule"]["series"] if c not in GONE]
                if not v["deleted"] and left and left != v["rule"]["series"]:
                    self.trimmed[k] = [c for c in v["rule"]["series"] if c in GONE]
                    v["rule"]["series"] = left
            off = whole.get("off", [])
            self.off_lost = not isinstance(off, list)
            self.damaged |= self.off_lost
            self.off = {n for n in off if isinstance(n, str)} if isinstance(off, list) else set()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            self.damaged = self.off_lost = True

    def keep_filled(self, sizes: dict | None = None) -> None:
        """The engine's, once: sizes filled in from config.json are the variants' own from now on. sizes: config.json's,
        when it can be read again after a start when it could not."""
        if sizes is not None and self.guessed:
            for k, fs in self.unsized.items():
                if k in self.all:
                    self.all[k]["rule"].update({f: sizes[f] for f in fs})
            self.guessed = False
        if self.unsized and not self.guessed or self.trimmed:
            self.save()                          # (what would not load is kept in a copy, as with any change)

    def save(self) -> None:
        if self.damaged:                         # something in it would not load: never write over the only copy
            keep = f"{self.path}.damaged-{int(time.time())}"
            try:
                os.replace(self.path, keep)
            except OSError:
                pass
            self.damaged = False
        out = self.all
        if self.guessed:                         # a guess is not saved: the next start fills it from config.json
            out = {k: {**v, "rule": {f: x for f, x in v["rule"].items() if f not in self.unsized.get(k, ())}}
                   for k, v in self.all.items()}
        else:
            self.unsized = {}
        with open(self.path + ".tmp", "w") as fh:
            json.dump({"variants": out, "off": sorted(self.off)}, fh, indent=1)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(self.path + ".tmp", self.path)
        self.changes += 1

    def active(self) -> list[str]:
        return [k for k, v in self.all.items() if not v["deleted"]]

    def deleted(self) -> list[str]:
        return [k for k, v in self.all.items() if v["deleted"]]

    def has(self, name) -> bool:
        return name in self.all and not self.all[name]["deleted"]

    def rule(self, name) -> dict | None:
        v = self.all.get(name)
        return v["rule"] if v else None

    def paper_limits(self, name) -> bool:
        """Whether paper keeps to a variant's limits too."""
        return name in self.all and self.all[name].get("paper_limits") is True

    def title(self, name: str) -> str:
        return self.all[name]["title"] if name in self.all else name

    def _key(self, name: str, but: str = "") -> str:
        """A key for records: lowercase letters, digits and dashes, starting with a letter, never used before.
        but: the variant being renamed, whose own key is free for it."""
        base = keyish(name)
        out, i, used = base, 2, self.used()
        while out != but and (out in self.all or out in self.reserved or out in used):
            tail = f"-{i}"
            out, i = base[:NAME_MAX - len(tail)].strip("-") + tail, i + 1
        return out

    def _free(self, title: str, but: str = "", taken=()) -> None:
        """ValueError when a title is too long or already what something here is called."""
        if len(title) > NAME_MAX:
            raise ValueError(f"a variant's name is at most {NAME_MAX} characters")
        low = title.lower()
        if low in {n.lower() for n in (*self.reserved, *taken)} or \
                any(low in (k, self.all[k]["title"].lower()) for k in self.active() if k != but):
            raise ValueError(f"{title!r} is already a variant's name")

    def add(self, rule: dict, name: str = "", parent: str = "", taken=(), deleted: bool = False, called: str = "") -> str:
        """A new variant; returns its key. Without a name it is called after what its parent is called (called, else
        its title here, else its key): band80-2."""
        if not deleted and len(self.active()) >= MAX_OWN:
            raise ValueError(f"at most {MAX_OWN} variants of your own: delete one first (D)")
        bad = why_bad(rule)
        if bad:
            raise ValueError(f"{parent or 'that rule'} can't be used as it is: {bad}")
        name = clean(name)
        if name:
            self._free(name, taken=taken)
            key = self._key(name)
        else:
            stem = re.sub(r"-\d+$", "", keyish(called or self.title(parent) or "rule"))[:NAME_MAX - 3].strip("-") or "rule"
            low = {n.lower() for n in taken} | {v["title"].lower() for v in self.all.values()} | self.used()
            # band80-2, band80-3 ...: the first number nothing has had
            i = 2
            while (cand := f"{stem}-{i}") in self.all or cand in self.reserved or cand in low:
                i += 1
            key = name = cand
        self.all[key] = {"rule": copy.deepcopy(rule), "title": name, "from": parent, "made": round(time.time(), 3),
                         "deleted": deleted, "was": "", "paper_limits": True}
        self.save()
        return key

    def turn(self, name: str, on: bool) -> None:
        (self.off.discard if on else self.off.add)(name)
        self.save()

    def renamed(self, key: str, to: str, taken=()) -> tuple[str, str]:
        """(the name it will have, the key its record will be under) for a rename, or ValueError. Empty: its key."""
        to = clean(to) or key
        self._free(to, but=key, taken=taken)
        return to, self._key(to, but=key)

    def rename(self, key: str, to: str, new: str) -> str:
        """The variant is called `to` and its key is `new` (from renamed()): everything here that names it moves
        with it, in the order it was in. Returns what it was called."""
        was = self.all[key]["title"]
        self.all = {(new if k == key else k): v for k, v in self.all.items()}
        self.all[new]["title"] = to
        for v in self.all.values():
            if v.get("from") == key:
                v["from"] = new
        for d in (self.by, self.unsized, self.trimmed):
            if key in d:
                d[new] = d.pop(key)
        if key in self.off:
            self.off.discard(key); self.off.add(new)
        self.save()
        return was

    def delete(self, key: str, used: bool) -> None:
        """used: it has bought something, so its rule is kept for its record; otherwise nothing is left of it."""
        self.off.discard(key)
        if used:
            self.all[key]["deleted"] = True
        else:
            del self.all[key]
        self.save()
