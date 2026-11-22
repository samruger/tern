# Pages Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace slot 3's ladder → variants → profiles cycling with two fixed pages (`p` flips them) that size
themselves to the terminal, merge into one screen when it is big enough, and keep a hidden set per page.

**Architecture:** All in `tern/dash.py`'s Screen: a table of pages (rows of boxes), a `layout()` that fits the
current view's rows to H×W from per-box minimum and fixed heights, per-view hidden sets saved in dash.json, and
`p` / `1`–`8` keys. `tern/web.py` carries the same prefs per viewer and lets `p`, `7` and `8` through.

**Tech Stack:** Python 3 stdlib, curses; tests `python3 -m unittest tests.test_tern`.

**Spec:** `docs/superpowers/specs/2026-11-22-pages-layout-design.md`

## Global Constraints

- Box numbers: 1 spot, 2 contract, 3 ladder, 4 book, 5 equity, 6 log, 7 variants, 8 profiles. Same number on
  every page.
- Page 1 · trade: spot | contract, ladder | book, equity | log. Page 2 · compare: variants (full width),
  profiles | equity, log. Merged: spot | contract, ladder | book, variants | profiles, equity | log.
- Views: `page1`, `page2`, `merged`, each with its own hidden set.
- No key cycles boxes. `3` only shows or hides the ladder.
- Smallest screen stays 100×30.
- The web changes nothing in the runtime. Its prefs stay in the browser.
- The repo is public: never commit `~/.config/tern/web.json` or the cert.

## Review Focus

- A screen of 100×30 or 111×33 with every box shown on page 2: variants keeps all 10 rows, and nothing
  overlaps or runs off the screen.
- Enter on page 2, where both variants and profiles show: it must act on the focused box only (use a variant
  or view a profile), never both, and on the web only viewing a profile gets through.
- A dash.json or browser prefs from before this change (`hidden` as a list, `slot3`) loads without error.
- Every box on a view hidden: the hint row and the message still say how to get them back, with the right
  numbers for that view.
- Resizing across the merge threshold while the focus is on a box the new view doesn't show.

---

### Task 1: Pages, fitting and per-view hiding

**Files:**
- Modify: `tern/dash.py`: the PANELS/ROWS/SLOT3/LEFT_MIN block near line 412, `Screen.__init__` prefs,
  `save_prefs`, `toggle`, `layout`, `focusable`/`move_focus`, `draw`, and the number-key branch in `key`.
- Test: `tests/test_tern.py`, new class `Pages`.

**Interfaces:**
- Produces:
  - `BOXES: list[str]` in number order.
  - `PAGES: dict[str, list[tuple[str, ...]]]` with keys `page1`, `page2` and `merged`.
  - `Screen.page: int` (1 or 2).
  - `Screen.hidden: dict[str, set[str]]`.
  - `Screen.view(H, W) -> str`.
  - `Screen.shown(name) -> bool`: the box is in the last drawn rects, or failing that in the current
    layout.
  - `Screen.flip_page()`.
  - `Screen.toggle(i)`, where `i` indexes BOXES.
  - `Screen.layout(H, W)`: same return shape as now.

- [ ] **Step 1: Write the failing tests** (`class Pages`, a Screen on `web.VTerm`, the same way the
  ScreenProfiles tests build one):
  - `test_number_keys_hide_on_this_page_only`: page 1, `5` hides equity. After `p`, equity shows on page 2.
    `hidden == {"page1": {"equity"}, "page2": set(), "merged": set()}`.
  - `test_number_for_box_on_other_page_says_where`: on page 1, `7` leaves the hidden sets as they were, and the
    flash contains `variants is on page 2`.
  - `test_page2_fits_small_screens`: at 33×111 and 30×100 with 10 variants and 3 profiles, the variants rect's
    height is ≥ 10 + 3, its width is W, and no rect runs past H-1 or W or overlaps another.
  - `test_page1_layout_at_small_size`: page 1 at 33×111 has spot, contract, ladder, book, equity and log, all
    inside the screen.
  - `test_merged_when_big`: at 50×140 the view is `merged`, and all 8 boxes are laid out. `p` there says
    `both pages fit`.
  - `test_old_prefs_load`: given dash.json `{"hidden": ["contract"], "slot3": "variants"}`, hidden page1 is
    `{"contract"}`, the page is 1 and there's no error. Given `{"page": 2, "hidden": {"page2": ["log"]}}`,
    the page is 2 and page2 is `{"log"}`. Saving writes `page` and a dict `hidden` without `slot3`.
  - `test_no_key_cycles`: pressing `3` three times leaves the ladder shown, hidden, then shown on page 1.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_tern.Pages`. Expected: it fails (no `page`,
  `hidden` is a set).

- [ ] **Step 3: Implement.**
  - The view is `merged` when every merged row fits at its minimum height in H-1 rows and its want width in
    W. Otherwise it is `page{page}`.
  - Box minimum heights:
    - spot, equity: 6
    - contract: 8
    - ladder: 8
    - book: 9
    - log: 4
    - variants: rows + 3, plus 1 for the tip line when it fits
    - profiles: rows + 6
  - Variants and profiles are fixed: a row holding one of them takes that height. The other rows share
    what's left by their old shares, each at least its minimum. When the minimums don't fit, the rows are
    scaled down, each to at least 3.
  - Want widths: ladder 56, variants 80, profiles 50, the rest 40. A pair narrower than the sum of its want
    widths stacks into two full-width rows. The left width keeps today's share with LEFT_MIN.
  - The number-key branch covers 1–8. `p` calls `flip_page`.
  - Focus is kept when the new view shows it. Otherwise it moves to the first focusable box (page 2: variants).
  - The "every box is hidden" message lists this view's boxes with their numbers.

- [ ] **Step 4: Run** `python3 -m unittest tests.test_tern`. Expected: Pages passes. Fix the existing tests that
  used `slot3` or pressed `3` to reach variants or profiles (Task 2 owns their behavior). For now they may only
  change in how they reach the box: `p` instead of `3`.

- [ ] **Step 5: Commit** `dash: two pages that fit the screen, hidden boxes per page`.

### Task 2: Keys inside the pages, the page hint, help and README

**Files:**
- Modify: `tern/dash.py`:
  - `in_profiles`, `use_variant`, `toggle_watch`
  - the Enter, space, `n` and `D` branches in `key`
  - `footer`
  - `HELP_BOXES` and `HELP_KEYS`
- Modify: `README.md`, the boxes table and the keys line near line 185.
- Test: `tests/test_tern.py`, class `Pages`.

- [ ] **Step 1: Write the failing tests:**
  - `test_enter_acts_on_focused_box`: on page 2, focus variants, highlight band90, Enter → one `variant`
    command. Focus profiles, Enter → no command, and `profile` is set to the highlighted one when that isn't
    profile 1.
  - `test_page_hint_on_bottom_border`: row H-2 contains `page` with `trade` and `compare`. After `p` the
    highlighted name changes (check the attr of the `compare` cells). In the merged view the hint isn't drawn.
  - `test_watch_without_variants_box_says_page2`: on page 1, `w` flashes `p` / `page 2`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_tern.Pages`. Expected: those three fail.
- [ ] **Step 3: Implement:**
  - Enter goes to `view_profile` when the focus is profiles, to `use_variant` when it is variants. Otherwise it
    says `enter: ←→ to focus variants or profiles`.
  - space, `n` and `D` work while profiles is shown.
  - The page hint is drawn as a border hint at x=2 on row H-2, before the flash: `("p", "age ", "trade", "
    compare")`, with the current page's name drawn bold and reversed.
  - Help and the README name `p`, 7 and 8 and page 2. Slot-3 cycling is removed.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_tern`. Expected: everything passes.
- [ ] **Step 5: Commit** `dash: enter by focus, page hint, help and README for pages`.

### Task 3: Web

**Files:**
- Modify: `tern/web.py`:
  - `VIEW_KEYS`, `clean_prefs`
  - `WebScreen.apply_prefs` and `current_prefs`
  - `press`, so Enter gets through only with the focus on profiles
  - `box` hits: the box number via `BOXES`
- Test: `tests/test_tern.py`: update `test_control_keys_send_nothing_and_view_keys_work` and the profile web
  test (`slot3` becomes `page`). Add `test_web_pages_prefs`.

- [ ] **Step 1: Write `test_web_pages_prefs`.** prefs `{"page": 2, "hidden": {"page2": ["log"]}}` round-trip
  through `handle`. An old `{"hidden": ["contract"], "slot3": "variants"}` loads as page1 `["contract"]`. `p`
  flips the page. Clicking the page hint flips it. Enter with the focus on variants sends nothing and says
  view only. The cmd folder stays empty.
- [ ] **Step 2: Run** it. Expected: it fails.
- [ ] **Step 3: Implement.** Prefs `hidden` is a dict of view → sorted list. A list is accepted as page1.
  `page` is in (1, 2). `slot3` is ignored. `VIEW_KEYS` gains `7`, `8` and `p`.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_tern`. Expected: everything passes.
- [ ] **Step 5: Commit** `tern web: pages per viewer`.
