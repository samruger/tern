# Zoom a box instead of hiding boxes — short design

**Why:** hiding boxes to make room is fiddly on a 104–111 × 31–33 screen. The user wants to look at one box
big, then go back: "pressing the number of the box full screens it, pressing it again restores".

**Approaches:** (a) `m` then the number zooms, `m` restores (user's first idea); (b) the number alone zooms, the
same number restores (user's pick: one key, and the numbers are already on the borders). Hiding goes away in both.

**Design (b):**
- 1-6 on page 1, 1-4 on page 2, 1-8 merged: the box with that number fills every row above the status line.
  The same number again restores the layout; another number switches the zoom to that box (my call).
- A zoomed box keeps its number on its border and gets the focus if it scrolls. The other boxes' hints go to
  the bottom row, as hidden boxes' did.
- The bottom border's `─page 1/2─` hint becomes `─3 back─` (the zoomed box's number) while zoomed.
- `p` while zoomed restores and flips (the numbers mean other boxes on the other page). In merged, p is unchanged.
- A size change that takes the zoomed box off the view clears the zoom.
- Not saved: reopening starts unzoomed. The web gets it per tab (digits are already view keys), not in prefs.
- Hiding is removed: `hidden` in dash.json / browser prefs is ignored and no longer written; the
  "every box here is hidden" message goes. Help and README say zoom.

**Non-goals:** zooming with the mouse; remembering the zoom.
