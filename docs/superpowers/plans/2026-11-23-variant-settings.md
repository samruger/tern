# Variant settings — plan

Spec: docs/superpowers/specs/2026-11-23-variant-settings-design.md. Stdlib only; tests: `python -m unittest
tests.test_tern`. No commentary on results in docs. README Changes row in the same commit.

1. **Rules carry size and paper start** (variants.py, shadow.py, profiles.py). RULE_SETTINGS gains `contracts`,
   `max_hour_cost`, `paper_capital`; `rule_of(name, S, own, paper)`; `size_of(name, S, own) -> (contracts,
   max_hour_cost)`; `start_of(name, cfg, own)`; VariantSet(dir, reserved, sizes) fills missing fields and sets
   `.filled`; `checked(changes, rule)` checks a whole draft. Shadow.step sizes by variant.
   Tests: fill, persisted once, two variants of one profile at different sizes, bad sizes refused.
2. **Engine**: `variant_new {from, name, rule}`; no `variant_rule`, no `series`; `set` / `profile_set` refuse the
   moved keys; books, views, `R` and profile rows read the variant's own. Tests: save leaves the book alone, a
   draft moving both band ends, unchanged refused, live uses the variant's hourly limit and its own contracts.
3. **Screen**: draft box on `E` (s, esc), `r` drafts, `e` is PROFILE / LIVE / GATE / ALERTS / WEB / SCREEN for every
   profile. Tests: nothing sent before s, refused save keeps the draft, esc drops it, live limit asks for "live"
   while another profile is viewed, `n` in variants says E, web still blocked.
4. **Help, README, Changes**, whole suite, try the real screen on a copy of runtime/, merge, restart the engine.

## Review focus
- Moving both band ends past each other in one draft is valid.
- The variant under E is deleted, or the engine restarts, while a draft is open: no crash, says so.
- variants.json from before the change, with deleted variants that still have open entries: all still load.
- A live limit changed while viewing a profile must still ask for "live" (the overlay status says live is off).
- An old screen's `variant_rule` sent to the new engine is refused and changes nothing.
