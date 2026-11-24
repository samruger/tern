# Variants hold every trading setting; profiles and the desk hold the rest — design

**Why:** profiles and variants both held trading settings, and the rule rows were in two boxes. The user wants
clear profile settings, clear variant settings, nothing redundant, and a flow that makes sense for testing
strategies against each other.

**Model (agreed 2026-11-23):**
- **Variant:** one strategy under test, with everything about how it trades: band, slots, spread, coins, entries a
  strike, most volatility, contracts a strike, most an hour, paper start. Once saved it never changes.
- **Profile:** a named group of variants with one picked for its book, and on/off. No trading settings.
- **Desk:** live, gate, alerts, web. The same rows whichever profile is viewed.

**Approaches:** (a) `e` makes variants, `E` edits in place: a tested record could belong to an untested rule;
(b) keep "the first keypress makes a copy", only moving rows: a variant appears by surprise, and one with no
entries behaves differently; (c) the user's pick: `E` is a draft, saved once, as a new variant.

**Flow:** highlight a variant, `E` opens every setting; changes are a draft (`19 → 10 *`). `s` asks for a name and
saves one new variant with an empty record; `esc` throws the draft away. Saving never changes what trades, paper
or live; only `enter` does (which asks for "live" when armed, as now). A refused save keeps the draft.

**Goes away:** `n` in the variants box, RULE / PAPER / "book trades" rows in `e`, the book moving to a copy,
in-place changes to a variant with no entries. `r` opens `E` on the variant in use with that coin flipped in the
draft (my call: it used to change what trades).

**Details (my calls):**
- Built-in variants take contracts, most an hour and paper start from their profile's config.json, as now.
- Upgrade: an own variant without the three fields gets its profile's current values, saved at engine start. No
  variant's trades change.
- Live keeps its own contracts and three limits in LIVE, desk-wide. Live uses the variant's most an hour.
- Profile 1's paper book is one account: `R` starts it on the paper start of the variant in use.
- Raising a live limit or lowering a gate asks for "live" while armed, also while another profile is viewed.
- Copying without a change is refused. The 12-a-profile limit stays.
- The web screen still changes nothing.

**Non-goals:** removing the built-in variants from other profiles; per-variant live contracts; changing the gate.
