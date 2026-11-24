# Mouse clicks in the terminal — short design

**Why:** the user asked for mouse support. The terminal has only the wheel; the web page already has clicks on tabs,
border keys, ladder and variant rows, reports and help sections.

**Approaches:** (a) move the web's click code into `dash.Screen`, so the terminal and the web share one copy, and add
what is missing (picked); (b) a second copy in the terminal's input code: two to keep in step.

**Design (a):** every draw records what is clickable (`Screen.hits`). A left press:

| On | Does |
|---|---|
| a tab | shows that box |
| a key on a border | presses that key; each of `↑↓` `←→` `PgUp PgDn` is its own |
| a ladder, variants, reports or help row | highlights it |
| the highlighted variant again | enter: selects it |
| a settings or variant editor row | highlights it; again: enter. Each slot and coin in a row is its own |
| a theme | uses it |
| paper / live in the book | shows that one |

- A click does only what keys do: live still has `live` typed, delete and engine stop still ask `y`.
- Stray clicks (asked for later): `space` and starting the engine ask `y`, by key too; a click that turns live off,
  changes a desk setting, discards changes or closes the screen asks `y` (the keys don't: `L` is the way out of live).
- While a question is open, clicks do nothing. While a value is being typed, clicks on other rows do nothing.
- While a variant is being edited, its tabs do not leave it: `s` or `esc` does, as with keys.
- The web stays view only: a click goes through the same filter its keys do.
- Text is selected with shift+drag, as before.

**Non-goals:** hover, drag, right click, and a mouse on the bare Linux console (Arch's ncurses has no gpm).
