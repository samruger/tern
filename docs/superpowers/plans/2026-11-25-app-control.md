# The App Controls the Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Android app, paired to the user's phone with keys in its secure hardware and fingerprint unlock, does everything the desktop screen does; browsers stay view only; app and browsers get separate on/off switches.

**Architecture:** The phone holds two Keystore EC P-256 keys (everyday: 5 minutes after a fingerprint; live: a fingerprint each use). The desk keeps their public keys in `~/.config/tern/app-devices.json` and checks a signature, a rising counter and a timestamp on every app request. Signed commands become the same `runtime/cmd` files the desktop screen writes, so the engine's checks are unchanged; pause/start and saving a report run in tern web as the screen does. Pairing is a code shown on the desktop screen plus a y there.

**Tech Stack:** Python 3 stdlib (tern), pure-Python P-256 ECDSA verify; Java 17 + Android platform APIs only (KeyStore, BiometricPrompt, WebView), built by `android/build.py` (no Gradle); `tern/phone.html` (plain JS).

**Spec:** `docs/superpowers/specs/2026-11-25-app-control-design.md`

## Global Constraints

- Python stays stdlib only; Java uses only `android.jar` (no androidx). `javac ... -Xlint:all ... -Werror` must pass.
- minSdk 29, target 36. The phone is a Galaxy S22 Ultra.
- Nothing secret in the repo or on the SD card: pairing records in `~/.config/tern/app-devices.json`, mode 600, written with `webauth._private_write`.
- Browsers (any device, signed in or not) never get a control and never see a control button.
- Everyday key: `setUserAuthenticationParameters(300, AUTH_BIOMETRIC_STRONG)` (API 30+; API 29: `setUserAuthenticationValidityDurationSeconds(300)`). Live key: authentication every use (timeout 0), used through `BiometricPrompt` with a `CryptoObject`. Both `setInvalidatedByBiometricEnrollment(true)`, StrongBox when available.
- Live key required for: `{"op":"live","on":true}` (arm or move), and `op:"set"` that loosens a setting with `risk` while live is armed (the desktop's "type live" cases, `dash.py:2545`). Turning live off: everyday key.
- At most 3 paired phones. Pairing code: 6 digits, 5 minutes, one use; 5 wrong codes → codes refused for 10 minutes.
- Timestamps outside ±30 s (`engine.CMD_MAX_AGE`) are refused; a counter not above the last one seen for that device is refused.
- Every app command is logged on the desk as `app <phone name>: <what>`.
- Copy style: the project's plain, lowercase, no-jargon wording (see existing `say()` messages). README `## Changes` gets a row in the same commit as the major change (memory: tern-changes-log). No "edge" talk in docs.
- Commit only what each task touches; the user's uncommitted README Android-section edits stay unstaged unless they say otherwise (stage hunks with `git apply --cached`).

## Review Focus

1. **A browser that copies the app's cookie, or replays a captured `/act` body** — must get view only / a refusal, never an action. (Task 4 tests: replayed body refused by counter; app cookie alone can't `/act`.)
2. **Phone clock off by more than 30 s** — every command would be refused silently. Expect a clear message on the phone ("the phone's clock is N s off the desk's"). (Task 4 test: skewed ts → refusal text names the offset; the desk's time is returned so the app can show it.)
3. **Fingerprints changed on the phone / key invalidated** — the app must say "pair again", not loop on errors. (Task 8: `KeyPermanentlyInvalidatedException` path → unpaired state.)
4. **Engine stopped when a command is sent** — the command file would sit and expire; the phone must say the engine isn't running (as the desktop says "no answer from the engine"). (Task 5 test: engine not alive → `/act` answers that, writes no file.)
5. **Two pairing attempts at once / a code typed on a second phone** — only one offer at a time; a second offer while one waits is refused. (Task 3 test.)

---

### Task 1: P-256 ECDSA verify

**Files:**
- Modify: `tern/sign.py` (new section "P-256 ECDSA, verify only")
- Test: `tests/test_tern.py` (new class `P256`)

**Interfaces:**
- Produces: `sign.p256_key(spki_der: bytes) -> tuple[int, int]` (raises `ValueError` unless SubjectPublicKeyInfo with OIDs 1.2.840.10045.2.1 + 1.2.840.10045.3.1.7 and an uncompressed on-curve point); `sign.p256_verify(spki_der: bytes, msg: bytes, sig_der: bytes) -> bool` (SHA-256; DER `SEQUENCE{INTEGER r, INTEGER s}`; False, never raises, on any bad input; r, s must be in [1, n-1]).

- [ ] **Step 1: Write the failing tests** — `test_verifies_openssl_signatures`: in a temp dir run `openssl ecparam -name prime256v1 -genkey -noout -out k.pem`, `openssl pkey -in k.pem -pubout -outform DER -out pub.der`, `openssl dgst -sha256 -sign k.pem -out sig.der msg`; assert `p256_verify(pub, msg, sig)` True for 20 random messages; False for msg+b"x", for sig with one byte flipped, for r=0, s=n, for another key's pub. `test_rejects_bad_keys`: `p256_key` raises ValueError for an Ed25519 SPKI (`openssl genpkey -algorithm ed25519`), a point with y+1, a compressed point, truncated DER.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_tern.P256 -v` — Expected: FAIL (`p256_verify` missing).
- [ ] **Step 3: Implement** in `tern/sign.py` using its `_der`/`_seq`/`_oid`; affine or Jacobian point math with `pow(x, -1, p)`; curve constants from SEC 2 (p, a=-3, b, G, n).
- [ ] **Step 4: Run** the class — Expected: PASS. Then the whole suite: `python3 -m unittest tests.test_tern` — Expected: OK.
- [ ] **Step 5: Commit** `git add tern/sign.py tests/test_tern.py && git commit -m "P-256 signature checks, for the app's keys"`

### Task 2: Paired phones and signed requests (`tern/devices.py`)

**Files:**
- Create: `tern/devices.py`
- Test: `tests/test_tern.py` (new class `Devices`)

**Interfaces:**
- Consumes: `sign.p256_key`, `sign.p256_verify`; `webauth._private_write`, `webauth.DIR`.
- Produces:
  - `FILE = os.path.join(webauth.DIR, "app-devices.json")`, `MAX = 3`, `SKEW = 30.0`.
  - `words(everyday_b64: str, live_b64: str) -> str`: `"ABCD EF01 2345"` — first 6 bytes of `sha256(b64decode(everyday) + b64decode(live))`, hex upper, groups of 4. (The app computes the same.)
  - `class Refused(Exception)`: its text is shown on the phone as is.
  - `class Devices(path=FILE)`: `all() -> list[dict]` (`{"id","name","paired","used"}`, no keys); `add(name: str, everyday_b64: str, live_b64: str) -> str` (new id = `secrets.token_hex(6)`; `Refused` at MAX or on a bad key); `remove(id: str) -> bool`; `secret() -> bytes` (32 bytes made on first use, kept in the file, for app session cookies); `check(payload: str, sig_b64: str, key: str, now: float) -> tuple[dict, dict]` → `(device_summary, cmd)`.
  - File shape: `{"secret": hex, "phones": {id: {"name","everyday","live","paired","used","n"}}}`; re-read when its mtime changes; each successful `check` saves `n` and `used`.
  - `check` refuses (`Refused`) when: payload not a JSON object with `d` (str), `n` (int), `ts` (number), `cmd` (object); unknown device ("this phone isn't paired: pair it again from the desk"); `key` not `"everyday"`/`"live"`; signature bad ("not signed by this phone's key"); `abs(now - ts) > SKEW` ("the phone's clock is N s off the desk's"); `n <= last n` ("sent twice: refused"); `needs_live(cmd, …)` and key is everyday ("live needs your fingerprint").
  - `needs_live(cmd: dict, cfg: Config, armed: bool) -> bool`: True for `op=="live" and on`; for `op=="set"` whose `cfgmod.setting(key).risk` loosens the current value while `armed`.
  - `session_token(dev_id: str, now: float) -> str` and `session_device(tok: str | None, now: float) -> str | None`: HMAC-SHA256 with `secret()`, 30-day expiry (same shape as `webauth.Auth.token`, with the device id inside); a removed device's token is invalid at once.

- [ ] **Step 1: Write the failing tests**, signing with openssl-made keys as in Task 1 (helper `_phone(d)` returning both SPKI b64s and a `sign(text, which)`):
  - `test_words_match_the_formula` — known input → expected 14-char string.
  - `test_add_remove_and_the_file_is_private` — mode 0o600; no key material in `all()`; a 4th `add` raises Refused; `remove` returns True then False.
  - `test_check_accepts_then_refuses_a_replay` — first check OK returns cmd; identical second → Refused "sent twice"; n+1 OK.
  - `test_check_refusals` — each refusal above, with the message text asserted (clock message contains the offset in seconds).
  - `test_live_needs_the_live_key` — `{"op":"live","on":True,"variant":"base"}` signed everyday → Refused "fingerprint"; signed live → OK; `{"op":"live","on":False}` everyday → OK; `op:"set"` lowering `gate.min_days` with armed True → needs live; armed False → not.
  - `test_session_tokens` — valid for its device; invalid after `remove`, after expiry, tampered.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_tern.Devices -v` — Expected: FAIL (no module).
- [ ] **Step 3: Implement** `tern/devices.py` as specified; one `threading.Lock` around file reads/writes in the process.
- [ ] **Step 4: Run** the class, then the suite — Expected: OK.
- [ ] **Step 5: Commit** `git commit -m "Paired phones: their keys, and checking what they sign"`

### Task 3: Switches, pairing on the desk, and the desktop screen's APP rows

**Files:**
- Modify: `tern/config.py` (`Web` gets `app: bool = False`; the `web.enabled` Setting label becomes "browsers", what: "the browser page, view only, on any computer or phone; the app has its own switch"; new `Setting("web", "app", "the app", kind="bool", what="the paired phone app, with control like this screen")`)
- Modify: `tern/webctl.py:36-` (`apply(web)`: the service runs when `web.enabled or web.app`); `tern/__main__.py:282-288` (`web --config` starts when either is on)
- Modify: `tern/devices.py` (pairing, below)
- Modify: `tern/dash.py` (settings box: an APP group after WEB — `set_rows()` `dash.py:2306`, `settings_panel` `dash.py:1929`; a pairing modal; the offer question)
- Test: `tests/test_tern.py` (`Devices`, `Settings`/screen tests near the existing settings-box tests)

**Interfaces:**
- Consumes: `Devices`, `words`, `engine.live_dir(runtime)`.
- Produces (in `devices.py`; state file `os.path.join(live_dir, "app-pair.json")`, written atomically under `fcntl.flock` on `app-pair.lock`):
  - `new_code(live: str, now: float) -> str` — 6 digits (`secrets.randbelow`), until now+300; replaces any earlier code and offer.
  - `offer(live: str, code: str, name: str, everyday_b64: str, live_b64: str, sig_b64: str, now: float) -> str` — returns an offer id (`secrets.token_hex(8)`). `sig_b64` is the everyday key's signature over `f"pair\n{code}\n{name}\n{everyday_b64}\n{live_b64}"`. Refused: no code / expired ("no pairing code on the desk: open settings, APP, pair a phone"); wrong code (counts; 5 → refused for 600 s, "too many wrong codes: wait 10 min"); an offer already waiting ("another phone is waiting to be paired"); bad signature; name cleaned to ≤ 24 printable chars, default "phone". The code is used up by a right offer.
  - `offered(live: str, now: float) -> dict | None` — `{"id","name","words"}` while one waits.
  - `answer(live: str, offer_id: str, yes: bool, devices: Devices) -> str` — yes: `devices.add(...)`, state `paired` with the device id; no: `declined`. Returns the line the screen says.
  - `offer_state(live: str, offer_id: str, now: float) -> dict` — `{"state": "waiting"|"paired"|"declined"|"expired"|"unknown", "device": id?}`; an offer expires with its code's 5 minutes.
- Desktop screen (`dash.py`): settings rows "pair a phone" (enter: `new_code`, modal showing the code in large digits and "type it into the app · it lasts 5 minutes · esc closes"; the modal closes by itself when an offer arrives) and one row per paired phone (`name · paired <date> · last used <ago>`; `D` asks y, then `Devices.remove`). While `offered()` returns an offer, the screen (any tab) shows a question: `pair <name>? its key: <words> — the app shows the same · y pairs · any other key says no`.

- [ ] **Step 1: Write the failing tests**:
  - `test_switches` — `web --config` returns without serving when both off; serves (mock `serve`) when only `app` on; `webctl.apply` treats `app` alone as on.
  - `test_pairing_flow` — `new_code` → `offer` right → `offered` shows name+words → `answer(yes)` → `offer_state` paired with an id present in `Devices().all()`; code can't be reused.
  - `test_pairing_refusals` — expired code, wrong code ×5 → lockout message, second offer while one waits, bad signature, `answer(no)` → declined and nothing added.
  - Screen test (existing screen rig, `press`/`text` helpers): settings shows APP rows; enter on "pair a phone" shows a 6-digit code; with an offer waiting, the question appears and `y` pairs; `D` on a phone row + `y` removes it.
- [ ] **Step 2: Run** the new tests — Expected: FAIL.
- [ ] **Step 3: Implement** config, webctl, `__main__`, devices pairing, dash rows/modals.
- [ ] **Step 4: Run** the suite — Expected: OK.
- [ ] **Step 5: Commit** `git commit -m "Pairing a phone from the settings box; separate switches for browsers and the app"`

### Task 4: tern web — app endpoints and the browsers switch

**Files:**
- Modify: `tern/web.py` (`serve()` and handler `H`, `web.py:508-661`)
- Test: `tests/test_tern.py` (extend `WebLogin`-style server tests: new class `WebApp`)

**Interfaces:**
- Consumes: `devices.offer/offer_state/Devices.check/session_token/session_device`, `config.load(runtime).web` (re-read when config.json's mtime changes, so the switches apply without a restart of tern-web beyond what webctl does).
- Produces (all POST, JSON in/out, refused with 403 `{"error": text}` when `web.app` is off):
  - `/app/pair` `{code,name,everyday,live,sig}` → `{"offer": id, "words": ...}` (per-IP limiter as for the password).
  - `/app/paired` `{offer}` → `offer_state(...)`.
  - `/app/session` `{payload, sig, key}` with `cmd == {"op":"session"}` → sets cookie `__Host-tern_a` (or `tern_a` without TLS) = `session_token(device)`, `Max-Age` 30 days, `HttpOnly; SameSite=Strict; Secure`; returns `{"ok": true, "name": phone name}`.
  - `/act` `{payload, sig, key}` → requires a valid app session cookie whose device equals `payload.d`; then `Devices.check`; then Task 5's `act()`; returns `{"id": cid, "say": text}`. Refusals → 403 `{"error": text, "now": desk time}`.
  - Signed-in check becomes: browser cookie valid **and** `web.enabled`, or app cookie valid **and** `web.app`. With browsers off, `/login` and `/` for a browser answer 403 "the browser page is turned off (settings, WEB)". App sessions always get the phone page (ignore `tern_ui`).
  - App session responses add header `X-Tern-App: 1`; `/phone` responses include `"control": true` for app sessions (the page shows buttons only then).

- [ ] **Step 1: Write the failing tests** (server on a free port with temp `webauth`/`devices` files and openssl-made keys, as in `test_server_needs_login_and_https`):
  - `test_app_session_then_view` — session → `/phone` works with only the app cookie and says `control: true`; with a browser cookie `control` is absent.
  - `test_act_needs_both_cookie_and_signature` — `/act` with app cookie but a body signed by another key → 403; with a valid signature but a browser cookie → 403; replayed body → 403 "sent twice".
  - `test_clock_message` — ts 90 s off → 403 whose error names "90 s" and includes `now`.
  - `test_browsers_off` — `web.enabled` False: browser `/` → 403 text; app session still views and acts.
  - `test_app_off` — `web.app` False: `/app/*` and `/act` → 403; browsers unchanged.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_tern.WebApp -v` — Expected: FAIL.
- [ ] **Step 3: Implement** in `web.py`; keep the 64 KB body limit; app endpoints never accept the password cookie as a substitute.
- [ ] **Step 4: Run** the suite — Expected: OK.
- [ ] **Step 5: Commit** `git commit -m "tern web: the app signs in and sends signed commands; browsers can be turned off"`

### Task 5: Doing what the app asks (`act`)

**Files:**
- Modify: `tern/web.py` (`Hub.act(device: dict, cmd: dict) -> dict`)
- Test: `tests/test_tern.py` (`WebApp`)

**Interfaces:**
- Consumes: `dash.start_engine(runtime)`, `dash.stop_engine(runtime, pid)`, `dash.engine_alive` (as the screen uses them, `dash.py:280-312`), `reports.save(runtime, rid)`, the engine's `runtime/cmd` file format (`dash.Screen.send`, `dash.py:685`).
- Produces: `Hub.act(device, cmd) -> {"id": cid, "say": text}`.
  - Engine ops (`live`, `variant_new`, `rename`, `variant_delete`, `variant_on`, `set`): only these, only their known fields; written exactly like `Screen.send` (id, ts, tmp + replace) plus `"by": "app <name>"`. If the engine isn't alive: no file, say "the engine isn't running: start it first". Results reach the phone through the status's `results[cid]` — `/phone` responses include `"results": {cid: text}` for ids the session sent in the last 60 s.
  - Engine-side: `_command` logs `app <name>: <msg>` when `by` is present (`engine.py` command loop, `engine.py:913-918`, the `self.log(msg, "cmd")` line).
  - Web-side ops: `{"op":"engine","run":true|false}` → start/stop as the screen; `{"op":"report_save","report":rid}` → `reports.save`. Both logged via a status note line in the say text.
  - Anything else → `Refused("unknown command")`.

- [ ] **Step 1: Write the failing tests**: each engine op writes one cmd file with the screen's fields and `by`; unknown op refused; extra fields dropped; engine not alive → no file and the message; `engine` op calls the mocked start/stop; `report_save` writes a report file in the temp runtime; engine `_command` with `by` logs the `app <name>:` prefix.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the suite — Expected: OK.
- [ ] **Step 5: Commit** `git commit -m "The app's commands go to the engine the way the screen's do"`

### Task 6: Phone pages for everything: help, settings, the variant editor

**Files:**
- Modify: `tern/phone.py` (`PAGES` gains `"help"`, `"settings"`, `"edit"`; new functions `help_page(scr, arg)`, `settings_page(scr, arg)`, `edit_page(scr, arg)`; `menu()` lists help and settings)
- Test: `tests/test_tern.py` (`Phone`)

**Interfaces:**
- Consumes: `scr.help_sections(w)` (`dash.py:2028`), `cfgmod.SETTINGS`, `dash.SCREEN_SETTINGS`, `scr.set_value(key)`, `about.py` texts, `varmod.RULE_SETTINGS`, `shadow.rule_of`.
- Produces page data:
  - help: `{"sections": [{"title", "lines": [spans]}]}` from `help_sections(60)`; the KEYS section replaced by `phone.APP_KEYS`, a list of `(action, where in the app)` covering every desktop key (L → "a variant → go live", P → "settings → engine", s → "a report → save", space/e/r/D → "a variant → …", C/c/V/t → as today's pages).
  - settings: `{"groups": [{"name", "rows": [{"key","label","value","shown","kind","lo","hi","step","choices","about","risk"}]}], "phones": Devices().all(), "engine": {"alive": bool}}`.
  - edit: for variant `arg`: `{"from": name, "rows": [same row shape for RULE_SETTINGS, value from the variant's rule], "max_own": MAX_OWN}`.

- [ ] **Step 1: Write the failing tests**: each new page returns its shape for a rig screen; help's KEYS section mentions every key in `dash.KEYS_ON` and t/T/P (no key left without a place); settings rows equal `cfgmod.SETTINGS + SCREEN_SETTINGS` in order with values; edit rows equal RULE_SETTINGS with the variant's values.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_tern.Phone -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the suite — Expected: OK.
- [ ] **Step 5: Commit** `git commit -m "Phone pages: help, settings and the variant editor"`

### Task 7: Controls in the phone page

**Files:**
- Modify: `tern/phone.html`
- Test: `tests/test_tern.py` (`Phone`: static checks of phone.html), plus a headless-chromium check if the screenshot script's chromium is available

**Interfaces:**
- Consumes: Task 4's `control` flag and `/act`; Task 6's pages; the native bridge `window.TernApp` from Task 8: `TernApp.sign(reqId: string, cmdJson: string, live: boolean)` → later `window.ternSigned(reqId, bodyJson)` or `window.ternRefused(reqId, text)`; `TernApp.paired(): boolean`.
- Produces, shown only when `last.control && window.TernApp`:
  - variant page: select · on/off · edit · rename · delete · go live / move live / live off.
  - book: paper/live view; settings: tap a row to change (steppers for numbers, toggles, choices), engine pause/start, phones list (view only here; unpair stays on the desk).
  - edit page: change rows into a draft; "save as new" (asks for an optional name) and "discard" (asks).
  - report page: save.
  - Confirmations: a sheet with the desktop's wording (yes / no) for on/off, delete, engine start/pause, desk settings, discard; for live arm/move and live-guarded settings a typed `live` field, then the fingerprint (the native side asks).
  - After `/act`, the `say` text and then `results[cid]` show in the existing `say()` line.

- [ ] **Step 1: Write the failing tests**: phone.html contains no control markup outside the `control` branch (string checks for the action names inside `if (last.control && window.TernApp)` blocks); every command it can build uses only ops from Task 5's list (regex over `op:` literals).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** in the page's existing `el()`/`morph()` style and theme variables; no new libraries.
- [ ] **Step 4: Run** the suite — Expected: OK. If chromium is present: load the page with a stub `window.TernApp` and a fake `/phone`, confirm buttons render and a confirm sheet opens.
- [ ] **Step 5: Commit** `git commit -m "The phone page's controls, for the paired app only"`

### Task 8: The app — keys, fingerprint, pairing, signing

**Files:**
- Create: `android/src/tern/desk/Keys.java` (Keystore keys, signing, `words()`), `android/src/tern/desk/Act.java` (payload text, counter; pure Java, testable), `android/src/tern/desk/Bridge.java` (`@JavascriptInterface sign`, `paired`)
- Modify: `android/src/tern/desk/MainActivity.java` (pair screen, session sign-in, bridge only for approved desk pages), `android/AndroidManifest.xml` (`USE_BIOMETRIC`), `android/build.py` (version 1.1)
- Create: `android/ActTest.java`; Test: `tests/test_tern.py` (class next to the `LinkTest` one, `test_tern.py:3946`)

**Interfaces:**
- Consumes: Task 2/3/4 formats exactly: payload `{"d","n","ts","cmd"}` (compact JSON, keys in that order), pairing signature text `pair\n{code}\n{name}\n{everyday}\n{live}`, `words()` formula, endpoints `/app/pair`, `/app/paired`, `/app/session`, `/act`.
- Produces:
  - `Act.payload(String device, long n, double ts, String cmdJson) -> String`; `Act.next(SharedPreferences)` counter (starts at 1, stored before use).
  - `Keys`: aliases `tern.everyday`, `tern.live`; `make()` (both keys; refuses if `KeyInfo` says not in secure hardware); `publicB64(alias)`; `sign(alias, byte[]) -> byte[]` through BiometricPrompt (everyday: prompt only when the 5-minute window has lapsed — catch `UserNotAuthenticatedException`, prompt, retry; live: always `CryptoObject`); `KeyPermanentlyInvalidatedException` → delete keys, clear device id, show "your fingerprints changed: pair this phone again".
  - No fingerprint enrolled (`BiometricManager.canAuthenticate(BIOMETRIC_STRONG) == BIOMETRIC_ERROR_NONE_ENROLLED`): explain, then `Settings.ACTION_BIOMETRIC_ENROLL` (API 30+) or `Settings.ACTION_SECURITY_SETTINGS` (29); retry on resume.
  - Pair screen (native views, the app's existing colors): explains → fingerprint → code + name fields (name default `Build.MODEL`) → shows `words()` and "press y on the desk" → polls `/app/paired` every 2 s up to 5 min → on paired stores device id, does `/app/session`, loads the pages.
  - Sign-in on open: if paired, `/app/session` natively over `Link`'s pinned connection, cookie put into `CookieManager`; the password page is only for unpaired viewing.
  - Bridge: added with `addJavascriptInterface` only after `onPageFinished` confirms an approved desk page; `sign` runs `/act`'s signing and answers via `evaluateJavascript("ternSigned(...)")`.

- [ ] **Step 1: Write the failing test** `ActTest`/python: compile `Act.java` + `ActTest.java` with plain `javac`; ActTest makes a **software** P-256 key (`KeyPairGenerator.getInstance("EC")`, `secp256r1`), prints SPKI b64, a payload from `Act.payload`, its `SHA256withECDSA` signature b64, and `Keys.words`-equivalent (`Act.words`) output; Python asserts `sign.p256_verify` True, the payload parses with keys `d,n,ts,cmd` in order, and `words` equals `devices.words`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_tern.ActJava -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `Act`, `Keys`, `Bridge`, MainActivity and manifest changes (put `words()` in `Act` so it is testable; `Keys` calls it).
- [ ] **Step 4: Run** the test, then `python3 android/build.py` — Expected: test PASS; build ends with apksigner verify OK and `~/tern.apk` written (javac -Werror clean).
- [ ] **Step 5: Commit** `git commit -m "The app: keys in the phone's secure hardware, fingerprint, pairing and signed commands"`

### Task 9: Docs, restart, hand the apk over

**Files:**
- Modify: `README.md` (Remote access / Android app: pairing, fingerprint, the two switches, what the app can do; Files table: `devices.py`; `## Changes` row 2026-11-25), `tern/dash.py` HELP (settings: APP; web: browsers vs app), `tern/about.py` if the WEB rows are explained there.

- [ ] **Step 1:** Write the docs; run the full suite — Expected: OK.
- [ ] **Step 2:** Commit (only these hunks; leave the user's own README edits unstaged).
- [ ] **Step 3:** Ask the user before restarting `tern` and `tern-web` (`systemctl --user restart tern tern-web`), then confirm both active and the log clean.
- [ ] **Step 4:** Build `~/tern.apk`; tell the user to send it over KDE Connect and open it; walk the pairing (settings → APP → pair a phone). Turn `web.app` on only when they say.
- [ ] **Step 5:** Push to `github main` only when the user asks.
