# Pages layout: design

2026-11-22. Status: draft, waiting for approval.

## Why

The user's tern window is about 111 columns × 33 rows (1000×660 px, foot size 11); maximized it is about
151 × 37. Box 3 holds three boxes (ladder → variants → profiles → hidden), so only one of them shows at a
time, and at 111 columns the variants box loses its gate column. The user dislikes having to cycle through boxes;
hiding a box with a key is fine. Ideally everything is on one screen. Where it can't be (all boxes at a readable
size need about 50 rows), one plain switch is acceptable.

## Approaches

- **A. Two fixed pages that size themselves (picked).** `p` flips between page 1 (trade) and page 2 (compare).
  Each box keeps its place, each page lays itself out to the terminal's size, and a big enough screen shows
  both pages as one.
- B. One screen that squeezes everything: no switching, but the charts get about 4 rows at this size,
  variants and profiles share a tabbed box, and boxes move when the size changes.
- C. Your own layouts (btop-style presets): the most flexible, but needs setting up and is more to learn.

## Decided (by the user)

- Approach A.
- Boxes can still be hidden on each page, and hiding on one page doesn't affect the other.
- No box ever cycles through other boxes. Slot 3's cycling goes away.

## My calls

- **Pages:**
  - page 1 · trade: spot | contract, ladder | book, equity | log (today's layout, with the ladder fixed in
    place).
  - page 2 · compare: variants across the full width, profiles | equity, then log.
  - Equity and log are on both pages.
- **Keys:**
  - `p` flips the page.
  - Each box has one number everywhere: 1 spot, 2 contract, 3 ladder, 4 book, 5 equity, 6 log, 7 variants,
    8 profiles.
  - A number shows or hides that box on the current page. A number for a box that isn't on this page says
    which page it is on ("variants is on page 2 · p").
  - Box numbers stay on the borders as now. The status line shows the page tabs ` 1 trade  2 compare ` with the
    current one highlighted.
- **Fitting a page to the screen:**
  - Tables keep all their rows: variants gets its count + 3, profiles its count + 3, and book and ladder get what
    they need.
  - The charts (spot, equity) give up height first, down to 5 rows.
  - The log takes what is left, at least 4 rows.
  - A pair whose two boxes can't both reach their minimum width stacks into two full-width boxes.
- **One screen when it fits:** when the terminal is big enough for every box at its minimum size (about 46 rows
  and 130 columns), page 1 and page 2 merge into one screen: spot | contract, ladder | book, variants |
  profiles, equity | log. There, `p` says "both pages fit on this screen", and hiding uses the merged view's
  own set.
- **Saved state:**
  - dash.json gets `page` and one hidden set per view (`page1`, `page2`, `merged`).
  - The old `hidden` becomes page 1's set. The old `slot3` is dropped.
  - On the web, the page and the per-view hidden sets are per-viewer prefs, kept in the browser like the others.
    `p`, the number keys and clicking a page tab work on the web.
- **Unchanged:** focus and scrolling (←→ move among the scrollable boxes on the current page), `w` watch,
  enter in variants or profiles, the settings, gate and help overlays, and the hints of a hidden box moving to the
  bottom row.
- Help and the README are updated: page 2 is described, and the slot-3 cycling is removed.

## Tests

- At 111×33 and 151×37, no box is cut off.
- Page 2 shows all 10 variant rows and the gate column at 111×33.
- Hiding a box on page 1 leaves page 2 alone.
- A number for a box on the other page says where that box is.
- An old dash.json (with `hidden` and `slot3`) loads.
- Web prefs carry the page and the hidden sets.
- A big screen shows the merged view.
- No number key cycles anything.
