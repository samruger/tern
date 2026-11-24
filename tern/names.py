"""The variants' display names (names.json), and the settings log (settings-log.jsonl).

Settings versions: the desk counts its settings, starting at 1. A change to a field in config.json that changes how
the built-in variants' records are made (RECORD_KEYS) bumps the version, and every new record row is stamped with it,
so results can be split by settings period. Rows from before versions existed count as version 0. A variant made on
the screen never changes, so its record needs none.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time

from . import config as cfgmod
from . import variants as varmod
from .config import SERIES, Config, Setting, Strategy
from .shadow import ORDER, VARIANTS

NAMES_FILE = "names.json"                        # runtime/: the built-in variants' display names
VARIANT_NAME_MAX = varmod.NAME_MAX

# fields that change how the built-in variants' records are made: a change bumps the settings version
RECORD_KEYS = ("strategy.band_lo", "strategy.band_hi", "strategy.minutes", "strategy.max_spread",
               "strategy.series", "strategy.contracts", "strategy.max_hour_cost",
               "paper_capital")                  # (the cash a built-in variant can spend starts there)

# The rule, size and paper start of the built-in variants, in config.json. The screen never changes these (a change
# is a new variant); a hand edit still does, and is logged.
BASE_RULE = [
    Setting("strategy", "band_lo", "band low", 0.50, 0.98, 0.01, kind="p", group="RULE",
            what="the favorite's mid must be at least this (a variant with its own band keeps it)"),
    Setting("strategy", "band_hi", "band high", 0.51, 0.99, 0.01, kind="p",
            what="the favorite's mid must be below this"),
    Setting("strategy", "minutes", "slots", kind="slots",
            what="minutes before the close the rule enters at; at least one stays on"),
    Setting("strategy", "max_spread", "max spread", 0.01, 0.10, 0.01, kind="p",
            what="widest bid-ask spread a strike may have"),
    Setting("strategy", "series", "coins", kind="coins",
            what="the coins the rule trades (multi trades all of them); at least one stays on"),
    *(Setting("strategy" if n != "paper_capital" else "", n, r.label, r.lo, r.hi, r.step, r.kind, what=r.what)
      for n, r in varmod.RULE.items() if n in varmod.SIZES),
]
LABELS = {r.key: r for r in [*varmod.RULE_SETTINGS, *BASE_RULE]}


def _get(cfg: Config, key: str):
    sec, _, name = key.rpartition(".")
    return getattr(getattr(cfg, sec) if sec else cfg, name)


def diff_record(old: Config, new: Config) -> list[tuple[str, object, object]]:
    """(key, was, now) for each record field that differs between two configs."""
    out = []
    for key in RECORD_KEYS:
        a, b = _get(old, key), _get(new, key)
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
    r = LABELS.get(key) or cfgmod.setting(key)
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
    r = LABELS.get(key) or cfgmod.setting(key)
    return ({"live": "live ", "gate": "gate "}.get(r.sec, "") + r.label) if r else key


def slug(name: str, taken: set[str]) -> str:
    """A file name part: lowercase letters, digits and dashes, unique among `taken`."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "report"
    out, i = base, 2
    while out in taken:
        out, i = f"{base}-{i}", i + 1
    return out


class Names:
    """runtime/names.json: {"variants": {built-in variant: display name}}.

    A variant's display name is only what the screens call it: its record, the gate, config.json's live.variant and
    `tern report` keep the name in shadow.TABLE, so renaming never splits a record. One made on the screen keeps its
    display name in variants.json."""

    def __init__(self, runtime: str):
        self.path = os.path.join(runtime, NAMES_FILE)
        self.variants: dict[str, str] = {}
        try:
            with open(self.path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        vs = d.get("variants") if isinstance(d, dict) and isinstance(d.get("variants"), dict) else {}
        self.variants = {k: varmod.clean(v)[:VARIANT_NAME_MAX] for k, v in vs.items()
                         if k in VARIANTS and varmod.clean(v) and varmod.clean(v) != k}

    def save(self) -> None:
        with open(self.path + ".tmp", "w") as fh:
            json.dump({"variants": self.variants}, fh)
        os.replace(self.path + ".tmp", self.path)

    def variant(self, name: str) -> str:
        return self.variants.get(name, name)

    def shown(self) -> list[str]:
        """What the built-in variants are called on the screen: no own variant may be called that."""
        return [self.variant(n) for n in ORDER]

    def rename_variant(self, name: str, to: str, taken=()) -> tuple[str, str]:
        """(was, now). An empty name, or the variant's own, goes back to the variant's own name. taken: what
        the ones made on the screen are called."""
        if name not in VARIANTS:
            raise ValueError(f"unknown variant {name!r} (known: {', '.join(ORDER)})")
        to = varmod.clean(to)
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
