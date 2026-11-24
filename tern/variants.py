"""Your own variants: complete rules made on the screen, kept per profile (profile 1 is the desk).

A variant here is everything about how it trades: band, slots, spread, coins, the two optional caps, contracts a
strike, most an hour and its paper start. It borrows nothing from the profile. shadow.TABLE's variants are built
in. No variant ever changes once it is saved: E on the screen opens one as a draft, and saving that draft makes a
new variant with an empty record (engine._variant_new), so a record is always one rule's.

They are in variants.json beside the profile's records. A variant that bought something and was then deleted
stays listed there as deleted: its open entries still settle and its key is never used again.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import time

from .config import SERIES, Setting

FILE = "variants.json"
MAX_OWN = 12                                     # of your own, in each profile
NAME_MAX = 12                                    # a variant's key and display name
KEY = re.compile(r"[a-z][a-z0-9-]{0,11}")
SLOT_CHOICES = (30, 25, 20, 15, 10, 5)           # minutes before the close a rule may enter at

# The rows of E in the variants box. Values are the variant's own; 0 is "off" in a cap.
RULE_SETTINGS = [
    Setting("rule", "band_lo", "band low", 0.50, 0.98, 0.01, kind="p", group="RULE",
            what="the favorite's mid must be at least this"),
    Setting("rule", "band_hi", "band high", 0.51, 0.99, 0.01, kind="p", what="... and below this"),
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
            what="contracts it buys per strike per slot ($ P&L scales with it; live has its own, in settings)"),
    Setting("rule", "max_hour_cost", "most an hour", 5, 2_000, 5,
            what="$ it may put into one coin's hour, on paper and live"),
    Setting("rule", "paper_capital", "paper start", 100, 1_000_000, 100, group="PAPER",
            what="the cash its paper book starts with; profile 1's paper book takes it at a reset (R)"),
]
SIZES = ("contracts", "max_hour_cost", "paper_capital")   # what variants saved before 2026-11-23 evening took from their profile
RULE = {r.name: r for r in RULE_SETTINGS}
FIELDS = tuple(RULE)


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


def as_rule(params: tuple, contracts: float, max_hour_cost: float, paper_capital: float) -> dict:
    """shadow.params()'s tuple, with the size and the paper start, as a rule."""
    lo, hi, cap, mins, spread, coins, mvol = params
    return {"band_lo": lo, "band_hi": hi, "minutes": list(mins), "max_spread": spread, "series": list(coins),
            "cap": cap, "max_vol": mvol, "contracts": contracts, "max_hour_cost": max_hour_cost,
            "paper_capital": paper_capital}


def same(a: dict, b: dict) -> bool:
    key = lambda r: {**r, "minutes": sorted(r["minutes"]), "series": sorted(r["series"])}
    return key(a) == key(b)


def describe(rule: dict) -> str:
    """A rule in a line, as the variants box and tern report say it."""
    out = [f"{rule['band_lo'] * 100:.0f}-{rule['band_hi'] * 100:.0f}¢", "slots " + " ".join(str(m) for m in rule["minutes"]),
           f"≤{rule['max_spread'] * 100:.0f}¢ wide", " ".join(SERIES.get(c, c).split("-")[0] for c in rule["series"])]
    if rule.get("cap"):
        out.append(f"{rule['cap']} a strike")
    if rule.get("max_vol"):
        out.append(f"vol ≤{rule['max_vol']:g}")
    out.append(f"{rule['contracts']:g}/strike · ${rule['max_hour_cost']:g}/h")
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
    if not (num(rule["contracts"]) and rule["contracts"] >= 1):
        return "its contracts a strike is not a count"
    if not all(num(rule[f]) and rule[f] > 0 for f in ("max_hour_cost", "paper_capital")):
        return "its most an hour or paper start is not a number"
    return ""


def _loaded(key, d, sizes: dict) -> dict | None:
    """One variant from variants.json, or None when it can't be used. Its name and dates are only for show.
    sizes: what one saved before variants held their size and paper start gets."""
    if not (isinstance(key, str) and KEY.fullmatch(key) and isinstance(d, dict)):
        return None
    if isinstance(d.get("rule"), dict):
        d = {**d, "rule": {**sizes, **d["rule"]}}
    if why_bad(d.get("rule")):
        return None
    if not isinstance(d.get("deleted", False), bool):
        return None
    title, made = clean(d.get("title")), d.get("made")
    return {"rule": d["rule"], "title": title if 0 < len(title) <= NAME_MAX else key,
            "from": d["from"] if isinstance(d.get("from"), str) else "",
            "made": made if isinstance(made, (int, float)) else 0, "deleted": d.get("deleted", False)}


class VariantSet:
    """A profile's own variants. reserved: the names shadow.py holds (built in and retired). sizes: its profile's
    contracts, most an hour and paper start, for variants saved before they held their own."""

    def __init__(self, dir: str, reserved=(), sizes: dict | None = None):
        self.path = os.path.join(dir, FILE)
        self.reserved = tuple(reserved)
        self.all: dict[str, dict] = {}           # key -> {rule, title, from, made, deleted}
        self.changes = 0                         # goes up with every change: the engine's summaries cache on it
        self.by: dict[str, str] = {}             # variant -> the id of the command that made it, since the start
        self.used = lambda: set()                # keys that have records (Shadow sets it): never given to another
        self.filled = False                      # some got their size from sizes: keep_filled() saves that
        self.damaged = False                     # something in the file would not load: save() keeps a copy of it
        try:
            with open(self.path) as fh:
                items = json.load(fh)["variants"]
            sizes = {**{"contracts": 5.0, "max_hour_cost": 60.0, "paper_capital": 1000.0}, **(sizes or {})}
            got = {k: _loaded(k, v, sizes) for k, v in items.items() if k not in self.reserved}
            self.all = {k: v for k, v in got.items() if v is not None}
            self.filled = any(not set(SIZES) <= set(items[k]["rule"]) for k in self.all)
            self.damaged = len(self.all) != len(items)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            self.damaged = True

    def keep_filled(self) -> None:
        """The engine's, once: sizes filled in from the profile are the variants' own from now on."""
        if self.filled and not self.damaged:     # (a damaged file is only written over by a change)
            self.save()
        self.filled = False

    def save(self) -> None:
        if self.damaged:                         # something in it would not load: never write over the only copy
            keep = f"{self.path}.damaged-{int(time.time())}"
            try:
                os.replace(self.path, keep)
            except OSError:
                pass
            self.damaged = False
        with open(self.path + ".tmp", "w") as fh:
            json.dump({"variants": self.all}, fh, indent=1)
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

    def title(self, name: str) -> str:
        return self.all[name]["title"] if name in self.all else name

    def _key(self, name: str) -> str:
        """A key for records: lowercase letters, digits and dashes, starting with a letter, never used before."""
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rule"
        base = ("v" + base if not base[0].isalpha() else base)[:NAME_MAX].strip("-")
        out, i, used = base, 2, self.used()
        while out in self.all or out in self.reserved or out in used:
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

    def add(self, rule: dict, name: str = "", parent: str = "", taken=(), deleted: bool = False) -> str:
        """A new variant; returns its key. Without a name it is called after its parent: band80-2."""
        if not deleted and len(self.active()) >= MAX_OWN:
            raise ValueError(f"at most {MAX_OWN} variants of your own in a profile: delete one first (D)")
        bad = why_bad(rule)
        if bad:
            raise ValueError(f"{parent or 'that rule'} can't be used as it is: {bad}")
        name = clean(name)
        if name:
            self._free(name, taken=taken)
            key = self._key(name)
        else:
            stem = (re.sub(r"-\d+$", "", parent) or "rule")[:NAME_MAX - 3]
            low = {n.lower() for n in taken} | {v["title"].lower() for v in self.all.values()} | self.used()
            # band80-2, band80-3 ...: the first number nothing has had
            i = 2
            while (cand := f"{stem}-{i}") in self.all or cand in self.reserved or cand in low:
                i += 1
            key = name = cand
        self.all[key] = {"rule": copy.deepcopy(rule), "title": name, "from": parent, "made": round(time.time(), 3),
                         "deleted": deleted}
        self.save()
        return key

    def rename(self, key: str, to: str, taken=()) -> tuple[str, str]:
        to = clean(to) or key
        self._free(to, but=key, taken=taken)
        was, self.all[key]["title"] = self.all[key]["title"], to
        self.save()
        return was, to

    def delete(self, key: str, used: bool) -> None:
        """used: it has bought something, so its rule is kept for its record; otherwise nothing is left of it."""
        if used:
            self.all[key]["deleted"] = True
        else:
            del self.all[key]
        self.save()
