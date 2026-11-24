"""Profiles, until 2026-11-23, were folders under runtime/profiles/, each with variants and records of its own. There is
one list now: merge() moves theirs into it, once, when the engine starts and that folder is still there.

A profile's own variants come in with everything they bought. The running ones keep running, under their names where
those are free. Its deleted ones, and its copies of the built-in variants (the same rules at its own size), come in as
deleted: they stop, and their records stay in the report. The folders move to runtime/profiles-merged/, whole.

Moving that folder is the last step, so until it has happened the next start begins again, from the *.pre-merge
copies of the desk's files as they were.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import time

from .config import Config
from .shadow import ORDER, VARIANTS, Shadow, own_set, rule_of

FROM = "profiles"
TO = "profiles-merged"
BUSY = "merging"                                 # runtime/: *.pre-merge are complete, the desk's files may be half done
OLD = ".pre-merge"
FILES = ("config.json", "variants.json", "shadow.json", "shadow-settled.jsonl")
ROW_KEYS = ("v", "ts", "event", "close", "ticker", "side", "n", "cost", "fee", "slot")   # an open row needs every one


def _put(path: str, text: str) -> None:
    with open(path + ".tmp", "w") as fh:
        fh.write(text)
        fh.flush(); os.fsync(fh.fileno())
    os.replace(path + ".tmp", path)


def _json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def _lines(path: str) -> list[str]:
    try:
        with open(path) as fh:
            return fh.readlines()
    except FileNotFoundError:
        return []


def _restore(runtime: str) -> None:
    """The desk's files as they were before the merge."""
    P = lambda n: os.path.join(runtime, n)
    for n in FILES:
        if os.path.exists(P(n + OLD)):
            shutil.copy2(P(n + OLD), P(n))
        elif os.path.exists(P(n)):
            os.remove(P(n))


def merge(runtime: str, sv: int) -> list[str]:
    """Lines for the log. What stops it leaves the desk's files as they were, and the next start tries again: never half
    of it for the engine to run on, and never an engine that won't start."""
    try:
        return _merge(runtime, sv)
    except Exception as ex:
        if os.path.isdir(os.path.join(runtime, FROM)) and os.path.exists(os.path.join(runtime, BUSY)):
            _restore(runtime)
            os.remove(os.path.join(runtime, BUSY))
        return [f"the profiles in runtime/{FROM}/ could not be moved into the list ({ex}): the next start tries again"]


def _merge(runtime: str, sv: int) -> list[str]:
    """sv: the desk's settings version, which the rows take (theirs is kept as psv)."""
    P = lambda *n: os.path.join(runtime, *n)
    root = P(FROM)
    if not os.path.isdir(root):
        if os.path.exists(P(BUSY)):              # it was done but for this
            os.remove(P(BUSY))
        return []
    if os.path.exists(P(BUSY)):                  # a start that was cut short: from the files as they were
        _restore(runtime)
    raw = _json(P("config.json"), {})            # (one that can't be read stops it here, before anything is touched)
    if not isinstance(raw, dict):
        raise ValueError("config.json is not a settings file")
    if not os.path.exists(P(BUSY)):
        for n in FILES:
            if os.path.exists(P(n + OLD)):
                os.remove(P(n + OLD))
            if os.path.exists(P(n)):
                shutil.copy2(P(n), P(n + OLD + ".tmp"))
                os.replace(P(n + OLD + ".tmp"), P(n + OLD))
        _put(P(BUSY), "")

    said: list[str] = []
    desk = Shadow(runtime)
    own, rows, slots, settled = desk.own, list(desk.rows), dict(desk.slots), []
    live = raw.get("live") if isinstance(raw.get("live"), dict) else {}
    live_pid, live_variant = str(live.get("profile") or "1"), None

    def meta(pid: str) -> dict:
        d = _json(os.path.join(root, pid, "profile.json"), {})
        return d if isinstance(d, dict) else {}
    order = []
    for pid in sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))):
        try:
            order.append((float(meta(pid).get("created") or 0), pid))
        except (OSError, ValueError, TypeError):
            order.append((0.0, pid))
    # the running ones' names come first: a deleted one of an earlier profile must not take one
    run_keys: set[str] = set()
    given: set[str] = set()                      # every key handed out: a record's without a variant is in no list yet
    for _, pid in order:
        run_keys |= set(own_set(os.path.join(root, pid)).active())
    for i, (_, pid) in enumerate(sorted(order), start=2):
        d = os.path.join(root, pid)
        try:
            name = str(meta(pid).get("name") or pid)
            on = bool(meta(pid).get("enabled", True))
            cfg = Config.from_dict(_json(os.path.join(d, "config.json"), {}))
            mine_raw = _json(os.path.join(d, "config.json"), {})
            p = Shadow(d)
            seen = {r["v"] for r in p.rows} | set(p.agg)
            add: dict[str, dict] = {}
            key: dict[str, str] = {}

            def free(want: str, runs: bool = False) -> str:
                taken = set(own.all) | set(add) | set(own.reserved) | own.used() | given | (set() if runs else run_keys)
                if want not in taken:
                    given.add(want)
                    return want
                stem, k = f"{want[:9].strip('-')}-p{i}", 2
                out = stem
                while out in taken:
                    out, k = f"{stem[:10]}-{k}", k + 1
                given.add(out)
                return out

            for k, v in p.own.all.items():       # its own
                key[k] = new = free(k, not v["deleted"])
                titles = {x["title"].lower() for x in [*own.all.values(), *add.values()] if not x["deleted"]} | \
                    {n.lower() for n in (*own.reserved, *(x for x in [*own.all, *add] if x != new))}
                title = v["title"] if v["title"].lower() not in titles else new
                add[new] = {**copy.deepcopy(v), "title": title, "was": name}
                if not v["deleted"] and (not on or k in p.own.off):
                    own.off.add(new)
            for k in [n for n in ORDER if n in seen]:    # its copies of the built-in ones
                key[k] = new = free(f"{k[:9].strip('-')}-p{i}")
                add[new] = {"rule": rule_of(k, cfg, p.own), "title": new, "from": k, "made": round(time.time(), 3),
                            "deleted": True, "was": name}
            for k in sorted(seen - set(key) - set(VARIANTS)):   # records variants.json no longer has
                key[k] = free(k)

            def moved(r: dict) -> dict:
                r = {**r, "v": key[r["v"]], "p": pid, "psv": r.get("sv", 0), "sv": sv}
                return r
            open_ = [moved(r) for r in p.rows if all(x in r for x in ROW_KEYS)]
            done = []
            for line in _lines(os.path.join(d, "shadow-settled.jsonl")):
                try:
                    r = json.loads(line)
                    done.append(json.dumps(moved(r), separators=(",", ":")) + "\n")
                except (ValueError, KeyError, TypeError):
                    continue
        except Exception as ex:                  # one that can't be read stays whole in its folder; the rest still move
            said.append(f"profile folder {pid} could not be read, its variants are not in the list ({ex}): kept in runtime/{TO}/")
            continue
        own.all.update(add)
        rows += open_
        settled += done
        for ev, s in p.slots.items():
            slots[ev] = sorted({*slots.get(ev, []), *s}, reverse=True)
        strat = mine_raw.get("strategy") if isinstance(mine_raw.get("strategy"), dict) else {}
        if live_pid == pid:
            live_variant = key.get(strat.get("variant"), "")
        run = [add[k]["title"] for k in add if not add[k]["deleted"]]
        said.append(f"{name}: {', '.join(run) or 'no variants of its own'} moved into the one list, "
                    f"{len(add) - len(run)} more kept for the record, {len(done)} entries")

    # everything is worked out: write it
    have = _lines(P("shadow-settled.jsonl"))
    if have and not have[-1].endswith("\n"):     # a line cut short by a crash must not swallow the next
        have[-1] += "\n"
    _put(P("shadow-settled.jsonl"), "".join(have + settled))
    _put(P("shadow.json"), json.dumps({"rows": rows, "slots": slots}, separators=(",", ":")))
    own.save()
    if live_pid != "1":
        raw["live"] = {**live, "variant": live_variant or ""}
        _put(P("config.json"), json.dumps(raw, indent=2))
    to, n = P(TO), 2
    while os.path.exists(to):
        to, n = P(f"{TO}-{n}"), n + 1
    os.replace(root, to)                         # done
    os.remove(P(BUSY))
    return said
