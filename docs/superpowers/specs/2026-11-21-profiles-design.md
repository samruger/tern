# Profiles: design

2026-11-21. Status: approved.

## Why

The variants already run side by side on paper, but they all share one set of settings, and changing a
setting changes the one record in place, so later you can't tell which settings earned what. The user wants
to keep running the current setup untouched and, next to it, try other settings on the variants (base or any
other), with every result logged so they can see how each settings-and-variant combination performs.

## What a profile is

A whole copy of the desk on paper: its own settings, the variant its book trades, its own record for every
variant run under those settings, and a log of every settings change.

- **Profile 1 is the desk as it is now.** Its files stay where they are in `runtime/` and nothing about them
  moves or changes. It keeps its paper book, live, the gate, its variant records and its history.
- **New profiles** (e.g. "profile 2") start as a copy of profile 1's settings, with a fresh paper start and
  empty records. The user then changes their settings.
- **All enabled profiles run at the same time**, on the same market snapshot each engine step, so a difference
  between two profiles comes from their settings, not from timing.
- **Variants take their settings from the profile.** A variant changes only its own field(s) (band90: band_lo
  0.90; late: slots 15/10; ...) and takes the rest from its profile, exactly as `shadow.params(name, S)` does
  now with `S` being that profile's strategy. So a profile with band 80–92¢ runs base at 80–92¢, late at 80–92¢
  in its two slots, and so on. A variant whose own band falls outside the profile's (band75 under an 80–92¢
  band) simply finds nothing; the screen shows that as 0 entries, which is correct.

## Goals

- Create, enable/disable and delete profiles from the terminal screen.
- Change a profile's settings while it runs; every change is logged with its time.
- Results are split by settings period, so a report says e.g. "80–92¢ from 11-22 to 11-25: +1.1¢/ct over 140
  entries, t 0.8" for each variant.
- See any profile on the screen: its book (its picked variant's record shown as a book), its variants box,
  equity, and the gate box as information.
- Compare profiles side by side, with entries and t next to P&L.
- The web screen can view any profile; it changes nothing.

## Non-goals

- No live trading for any profile but profile 1. Profiles 2+ never send an order and have no live settings.
- No replay/backtest of a profile over recorded ticks (`runtime/ticks/`). A possible later add-on.
- No new variants. The variant table stays as it is.
- No moving or rewriting of profile 1's existing files.

## Anti-goals (would make it a failure even if it works)

- Anything in profile code able to stop, slow or change profile 1's trading or live. A broken profile must
  log an error and carry on.
- A record that mixes settings without saying so.
- Losing a deleted profile's records.
- More Kalshi requests per step than now (the Wi-Fi is flaky).
- A screen too busy to read; tern's look (btop-style, themed) stays.

## Approaches considered

- **A. Profiles inside the one engine (chosen).** Each extra profile is a folder with its settings and its own
  `Shadow` records; the engine runs every enabled profile's variants on the snapshot it already fetched. Exact
  comparison, no extra requests, one service. Costs real engine work; isolated per profile.
- **B. One engine process per profile** (`TERN_RUNTIME` per folder). Little engine change, but every process
  fetches Kalshi on its own, they see prices seconds apart, one service each, and the screen must gather
  results from several folders. Rejected: timing differences would muddy the comparison and multiply requests.
- **C. Replay over recorded ticks.** Not an alternative to running forward (5 s prices, near-the-money strikes
  only); noted as a later add-on.

The user picked A.

## Decided

### Files

```
runtime/                         profile 1, unchanged (config.json, shadow*.json*, state.json, ...)
runtime/settings-log.jsonl       profile 1's settings changes, from now on (new)
runtime/profiles/<id>/
    profile.json                 {"name", "enabled", "created", "sv"}  (sv = settings version, see below)
    config.json                  the profile's settings, same shape as runtime/config.json; only paper and
                                 strategy fields are used
    shadow.json                  open rows   (same format as runtime/shadow.json)
    shadow-settled.jsonl         settled rows (same format, plus "sv")
    settings-log.jsonl           one line per change: {"ts", "sv", "key", "from", "to"}
runtime/profiles-deleted/<id>-<YYYYMMDD-HHMM>/   a deleted profile's folder, moved here whole
```

`<id>` is a short safe slug made from the name (`profile-2`); the name is what the screen shows.

### Settings versions and periods

Each profile has a settings version `sv`, starting at 1 and going up by one with each change. Every new shadow
row is stamped with the profile's current `sv`. Profile 1's existing rows have no `sv` and count as period 0
("before profiles"). A settings period is a run of `sv` values with no change between them, i.e. each `sv` is
one period; the settings log says what each period's settings were. Reports group a variant's settled rows by
`sv`.

Open rows keep the `sv` they were entered under, so a change mid-hour never relabels entries already taken.

### What a profile's settings are

For profiles 2+, the settings box (`e`) shows, for the profile being viewed:

| group | rows |
|---|---|
| PROFILE | variant the book trades (choice of the 10), enabled (on/off) |
| RULE | band low, band high, slots (each of 30/25/20/15/10/5 on/off), max spread, coins (each on/off) |
| PAPER | contracts a strike, most an hour, paper start |

Bounds (my call): band low 0.50–0.98 and band high 0.51–0.99 in 0.01 steps, low always below high; max spread
0.01–0.10 in 0.01 steps; at least one slot and one coin stay on; contracts, most an hour and paper start use
the bounds the settings box already has. Live and gate rows are not shown for profiles 2+.

Profile 1's settings box stays as it is now (its rule knobs remain `config.json` edits) so profile work can't
loosen live by accident (my call). Its changes are now also written to `runtime/settings-log.jsonl` and bump
its `sv`.

Paper start changes take effect for the profile's book view at once (the book is a view of the record on that
start), unlike profile 1's paper book, which waits for a reset.

### A profile's book

For profiles 2+, the book, equity curve, ladder marks and band show the profile's picked variant's record as a
book on its paper start, the same way `w` (watch) shows a variant today (`Shadow.view`). There is no separate
paper `Book` object and no fills beyond the shadow rows. Profile 1's real paper book is unchanged.

### Enable, disable, delete

- **Disabled:** takes no new entries; hours while off don't count. Open rows still settle. Records and settings
  stay; enabling picks up from the next slot.
- **Delete:** confirms first ("type delete"), then moves the folder to `runtime/profiles-deleted/`. `tern
  report --deleted` can still list them. Nothing is erased.
- Profile 1 cannot be disabled or deleted (it holds live).
- At most 8 profiles besides profile 1 (my call: bounds CPU and memory; easy to raise).

### Engine

- Loads profiles at start and re-reads `runtime/profiles/` every few seconds (as it does config.json).
- In `_shadow()`, after profile 1's `shadow.step`, each enabled profile runs its own `Shadow.step` on the same
  event snapshot with its own strategy and vol. Settlement passes the same results dict to every profile's
  `Shadow.settle` / `void`; an event stays in `closed` until no profile `needs` it.
- The coins fetched each step are the union of every enabled profile's coins (a profile can add a coin to the
  fetch list; that is the only way requests can grow, and only by the user's choice).
- Every per-profile call is wrapped: an exception logs `profile <name>: <error>` (throttled) and skips that
  profile for that step. Profile 1 and live never wait on a profile.
- New command ops through the existing `runtime/cmd/` queue: `profile_new {name}`, `profile_enable {id, on}`,
  `profile_delete {id}`, `profile_set {id, key, value}`. `set` for profile 1 is unchanged except it also writes
  the settings log. Each is checked against the bounds above and logged like `set` is now.
- `status.json` gains a `profiles` list (id, name, enabled, variant, sv, and each variant's summary row for
  the comparison box). Full book views (`views`) are sent only for the profile the screen or a web viewer is
  looking at, which `view.json` / `view-web.json` now name, so status.json doesn't grow with every profile.

### Screen

- A profiles box, reached the way the variants box is (`3` cycles ladder → variants → profiles → hidden).
  One row per profile: name, on/off, the variant its book trades, its book P&L, entries, ¢/ct, t, settings
  version. Keys in it: `↑↓` pick, `enter` view that profile, `space` enable/disable, `n` new profile
  (asks for a name, default "profile N"), `D` delete (asks to type `delete`).
- The header shows which profile is being viewed. Viewing another profile changes what the book, equity,
  variants box and settings box show; it changes nothing about what trades.
- The variants box for a profile shows that profile's records, and `enter` there sets that profile's variant.
- The help (`?`) gains a profiles section.

### Report

`tern report` gains `--profile <name>` (that profile's variants, split by settings period, each period
labelled with its settings from the log) and `--profiles` (one line per profile × its picked variant, side by
side). Plain `tern report` stays as it is (profile 1).

### Web

View only, as now. The web screen can open the profiles box and view any profile (a per-viewer pref, kept in
the browser with the others). New, enable, delete, settings changes and picking a variant say "view only".

### Tests

At least: a profile's variants trade under its own settings on the same snapshot as profile 1; profile 1's
files and records are byte-for-byte untouched by profile activity; a settings change bumps `sv`, is logged, and
open rows keep their old `sv`; disabled profiles take no entries but settle open ones; delete moves the folder;
an exception in one profile doesn't stop profile 1's step; status.json carries views only for viewed profiles;
web sends no profile commands.

### Smaller calls (mine, from the builder check)

- Fees: every profile uses profile 1's `taker_fee`; it is Kalshi's, not a setting to experiment with.
- Changing a profile's variant or turning it on/off is written to its settings log but does not bump `sv`:
  neither changes how any variant's record is made.
- The gate box, viewed on profile 2+, shows that profile's picked variant against profile 1's gate settings,
  as information only.
- No renaming in this version; make a new profile instead.
- A new profile copies profile 1's paper start too.

## Left to the builder

- Exact layout and colors of the profiles box and header, within tern's existing style and themes.
- How the settings box draws multi-choice rows (slots, coins).
- Throttle intervals for profile error logs and for re-reading `runtime/profiles/`.
- Report formatting.
- README section for profiles.
