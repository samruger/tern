"""Your own variants: complete rules made on the screen, kept per profile (profile 1 is the desk).

A variant here is a whole rule: band, slots, spread, coins, and the two optional caps. It borrows nothing from
the profile's settings (those hold its size and budget). shadow.TABLE's variants are built in and never change
from the screen. A variant that has bought anything never changes either: changing one of those makes a new
variant with the change, with an empty record (engine._rule_edit), so a record is always one rule's.

They are in variants.json beside the profile's records. A variant that bought something and was then deleted
stays listed there as deleted: its open entries still settle and its key is never used again.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time

from .config import SERIES, Setting

FILE = "variants.json"
MAX_OWN = 12                                     # of your own, in each profile
NAME_MAX = 12                                    # a variant's key and display name
SLOT_CHOICES = (30, 25, 20, 15, 10, 5)           # minutes before the close a rule may enter at

# The rule rows of the settings box and of E in the variants box. Values are the rule's own; 0 is "off" in a cap.
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
]
RULE = {r.name: r for r in RULE_SETTINGS}
FIELDS = tuple(RULE)


def check(field: str, value, rule: dict):
    """The value as it will be stored in rule, or ValueError with a message the screen can show."""
    r = RULE.get(field)
    if r is None:
        raise ValueError(f"{field!r} is not part of a rule")
    if r.kind in ("slots", "coins"):
        ok = SLOT_CHOICES if r.kind == "slots" else tuple(SERIES)
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
    value = round(float(value), 2)
    if field == "band_lo" and value >= rule["band_hi"]:
        raise ValueError(f"band low must stay below band high ({rule['band_hi'] * 100:.0f}¢)")
    if field == "band_hi" and value <= rule["band_lo"]:
        raise ValueError(f"band high must stay above band low ({rule['band_lo'] * 100:.0f}¢)")
    return value


def as_rule(params: tuple) -> dict:
    """shadow.params()'s tuple as a rule."""
    lo, hi, cap, mins, spread, coins, mvol = params
    return {"band_lo": lo, "band_hi": hi, "minutes": list(mins), "max_spread": spread, "series": list(coins),
            "cap": cap, "max_vol": mvol}


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
    return " · ".join(out)


def clean(name) -> str:
    return " ".join(name.split()) if isinstance(name, str) else ""


def _valid(d) -> bool:
    if not isinstance(d, dict) or not isinstance(d.get("rule"), dict) or set(d["rule"]) != set(FIELDS):
        return False
    try:
        for f in FIELDS:
            v = d["rule"][f]
            if f in ("minutes", "series"):       # kept as they are: a built-in's slots may be any from config.json
                want = (int, float) if f == "minutes" else str
                if not isinstance(v, list) or not v or any(isinstance(x, bool) or not isinstance(x, want) for x in v):
                    return False
            elif v is not None or f not in ("cap", "max_vol"):
                float(v)
    except (TypeError, ValueError):
        return False
    return True


class VariantSet:
    """A profile's own variants. reserved: the names shadow.py holds (built in and retired)."""

    def __init__(self, dir: str, reserved=()):
        self.path = os.path.join(dir, FILE)
        self.reserved = tuple(reserved)
        self.all: dict[str, dict] = {}           # key -> {rule, title, from, made, deleted}
        self.changes = 0                         # goes up with every change: the engine's summaries cache on it
        self.moved: dict[tuple, str] = {}        # (edit session, variant) -> the copy that session made of it
        self.damaged = False
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            items = d["variants"] if isinstance(d, dict) else {}
            self.all = {k: {"title": k, "from": "", "made": 0, "deleted": False, **v} for k, v in items.items()
                        if k not in self.reserved and _valid(v)}
            self.damaged = len(self.all) != len(items)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            self.damaged = True

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
        out, i = base, 2
        while out in self.all or out in self.reserved:
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
        name = clean(name)
        if name:
            self._free(name, taken=taken)
            key = self._key(name)
        else:
            stem = (re.sub(r"-\d+$", "", parent) or "rule")[:NAME_MAX - 3]
            low = {n.lower() for n in taken} | {v["title"].lower() for v in self.all.values()}
            # band80-2, band80-3 ...: the first number nothing has had
            i = 2
            while (cand := f"{stem}-{i}") in self.all or cand in self.reserved or cand in low:
                i += 1
            key = name = cand
        self.all[key] = {"rule": copy.deepcopy(rule), "title": name, "from": parent, "made": round(time.time(), 3),
                         "deleted": deleted}
        self.save()
        return key

    def set(self, key: str, field: str, value) -> tuple[object, object]:
        """Change one field of a variant's rule: (was, now). The caller knows it has bought nothing."""
        rule = self.all[key]["rule"]
        value = check(field, value, rule)
        was, rule[field] = rule[field], value
        if was != value:
            self.save()
        return was, value

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
        self.moved = {k: v for k, v in self.moved.items() if v != key}
        self.save()
