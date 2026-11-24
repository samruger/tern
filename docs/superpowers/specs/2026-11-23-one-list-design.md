# One list of variants, no profiles — design

**Why:** variants hold every trading setting now, so a profile is only a folder, the same name means different things
in each, and `w` / `enter` / `d` / `L` are four ways of saying which variant. The user: "remove profiles and have one
list of variants"; n makes one, an edit makes a new one and the old one and its record stay; enter selects; L goes
live with the selected one.

**Approaches:** (a) keep profiles as shared accounts (the user does not want several variants on one pot); (b) hide
profiles on the screen only: two lists stay underneath; (c) the user's pick: one list, profiles' own variants moved in.

**Design (c):**
- **One list:** the built-in variants and your own (runtime/variants.json, up to 24 of your own). Each always runs on paper with its own
  record and cash. Profiles, the profiles box, `d` and the separate paper book (Book "paper", its curve and rule log)
  are gone; their files stay.
- **Selecting:** `enter` selects the highlighted variant: book, equity, ladder band, contract, gate and help show it.
  It is the screen's own (dash.json; a browser's prefs on the web), never the engine's. `w` is gone.
- **Live:** `L` arms live with the selected variant (config live.variant; type "live"), past its own gate as before.
  Selecting another does not move live. `L` with the live one selected turns live off; with another selected it asks
  to move live there (type "live") or x turns it off. Live never falls back to another variant.
- **`n`** opens a draft of the highlighted one's settings; `s` names it and saves a new variant with an empty record.
  A saved variant never changes. `r` is n on the selected one with the coin flipped. (There was an `E` too, for an
  hour; the user: "its the same thing as new".)
- **`space`** turns a variant on or off (off: no new entries, open ones settle). Turning the live one on while armed
  asks "live"; off stops live's entries.
- **`D`** deletes (not built-ins, not the one live trades while armed), **`N`** renames.
- **`R`** starts the selected variant over: its record so far is kept in the report under a deleted name, its cash is its
  paper start again, its gate starts over. Asks y. Not while live is armed with it.
- **Settings (`e`):** LIVE, GATE, ALERTS, WEB, SCREEN.
- **Move (once, at engine start, when runtime/profiles/ exists):** each profile's own variants come into the list with
  their entries, under a free key; deleted ones and the profile's built-in copies with entries come in as deleted. The
  folders move to runtime/profiles-merged/, and *.pre-merge keeps the files as they were. Interrupted, it starts again
  from those. Settings versions: moved rows keep theirs as psv and take the desk's current one.
- live.variant starts as what live would have traded before.

**Non-goals:** shared cash; editing a saved variant; changing the gate, live's caps or execution.
