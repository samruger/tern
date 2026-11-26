# The app controls the desk — design

2026-11-25. Approved approach: A (a key in the phone's secure hardware).

## Intent

The user wants to run tern from their phone the way they run it from the desktop screen: everything the desk has
to offer, the help included, and every action — variants (select, edit into a new one, rename, on/off, delete),
live, the desk settings, pausing and starting the engine, saving reports. They like the app's phone layout as it is;
the controls go into it, in the same look.

It must be secure: only the user's paired phone running the user's copy of the app may change anything. Browsers —
on a desktop or a phone — stay view only, exactly as today. The app and the browser page get separate on/off
switches, so the app can be on while the browser page is off.

The app asks to set up fingerprint unlock itself. Nothing in setup needs a USB cable.

## Goals

- The app offers every page and every action the desktop screen has.
- Only a paired phone can send a command; a stolen password, cookie or apk sends nothing.
- Fingerprint unlock, set up from inside the app.
- Separate switches: app on/off, browsers on/off.
- Pairing and unpairing done on the desk's own screen, over the network.

## Non-goals

- Browsers getting any control. They stay view only, even signed in, even on the phone.
- Changing how the desktop screen works, or the phone pages' look.
- iPhone, or other Android phones beyond "it works on any Android 10+ with a fingerprint reader".
- Push notifications.

## Anti-goals (a failure even if it works)

- Any path where a password alone can change something.
- The key ever leaving the phone's secure hardware, or anything secret committed to the public repo.
- The app's pages drifting from the desktop's rules: every action goes through the engine's existing checks.
- Needing USB, adb, or a computer next to the phone after the first install.

## Constraints

- tern is stdlib-only Python: signature checks must be pure Python (tern/sign.py already does Ed25519/RSA-PSS).
- The app is built with android/build.py: no Gradle, no libraries (no androidx). BiometricPrompt and Android
  Keystore are in the platform. minSdk is 29; the user's phone is a Galaxy S22 Ultra (Android 14+).
- The repo is public: pairing records live in ~/.config/tern (mode 600), never in runtime/ or git.
- The SD card is vfat (no file permissions): nothing secret on it.
- The app is a WebView of `tern web` (tern/web.py + phone.py + phone.html); pages come from the desk, so page
  changes need no new apk. The new native parts (key, fingerprint, signing) do need one new apk, installed the
  way the user did before (KDE Connect).

## Approaches considered

- **A. A key in the phone's secure hardware (chosen).** At pairing the app makes an EC P-256 key in Android
  Keystore (StrongBox when there is one, else the TEE; never exportable). The desk stores its public key. Every
  command is signed; the desk checks it. Fingerprint gating is a property of the key itself. Chosen: strongest,
  and the browser path stays untouched.
- B. A client certificate checked at TLS. Same strength, but browsers start showing "choose a certificate"
  prompts, and failures can't be explained on the page.
- C. A shared secret kept in the app. Simplest, but it is just a file: a backup or rooted phone copies it. Not
  good enough for live.

## How it works

### Keys and fingerprint

- Two Keystore keys per pairing, both EC P-256, signing ECDSA-SHA256, user authentication required (biometric
  strong):
  - **everyday key**: valid for 5 minutes after a fingerprint. Signs every command except the live ones.
  - **live key**: needs a fingerprint for every single use (BiometricPrompt with a CryptoObject). Signs: arming
    live, moving live to another variant, and any setting or command the desktop would ask to type "live" for.
- Turning live **off** is signed with the everyday key (it must stay quick, as L is on the desktop).
- If the phone has no fingerprint enrolled, the app says so and opens Android's enroll screen
  (Settings.ACTION_BIOMETRIC_ENROLL; on API 29, the security settings) and continues when the user comes back.
- If fingerprints on the phone change, Android invalidates the keys (setInvalidatedByBiometricEnrollment true):
  the app then says it must be paired again. That is intended.

### Pairing (all over the network)

1. On the desk's screen, settings box → APP group → "pair a phone": the screen shows a 6-digit code, valid
   5 minutes, one use. The code is written where tern web reads it (the runtime tmpfs dir, live_dir).
2. In the app: menu → "pair this phone" (offered by itself when the app is unpaired). The app explains the
   fingerprint, makes the two keys (fingerprint asked here, enrolling first if needed), asks for the code and a
   name for the phone (default: the phone's model).
3. The app sends {code, name, both public keys}, signed by the everyday key. The desk checks the code.
4. The desk's screen asks: "pair <name>? its key: XXXX XXXX XXXX" (short fingerprint of the public keys); the app
   shows the same characters. y on the desktop pairs it. Wrong code 5 times → codes are refused for 10 minutes.
5. Stored in ~/.config/tern/app-devices.json (600): device id, name, both public keys, paired time, last used.
   At most 3 phones.
- Unpair: settings → APP → the phone's row → D (asks y). Takes effect at once.
- The settings box lists paired phones with name and last used.

### Signing in and the switches

- WEB settings become: **browsers** (on/off; today's "web screen"), **app** (on/off), port, network. tern-web.service
  runs when either is on.
- A paired app signs in with its key, not the password: it signs a challenge from the desk (everyday key) and gets
  its own session cookie, marked as an app session for that device. The password stays for browsers.
- Browsers off: tern web answers only app sessions and the app's pairing/sign-in endpoints; the login page and
  browser pages are refused ("the browser page is turned off"), localhost included.
- App off: app sign-in, pairing and commands are refused; browsers behave as today.
- An app session is view rights only, like a browser's. The rights to change something come from each command's
  signature, never from the cookie.

### Commands

- A new POST endpoint (e.g. /act) takes {payload, sig, key: "everyday"|"live"}. payload is the exact JSON text the
  app signed; the desk verifies over those bytes, then parses it. It holds: device id, a counter that only goes up
  (the desk keeps the last one per device and refuses anything not above it), a timestamp (refused outside ±30 s,
  as CMD_MAX_AGE), and the command.
- Engine commands (live, variant_new, rename, variant_delete, variant_on, set) are written to runtime/cmd exactly as
  the desktop screen writes them, with the same fields; the engine's checks and messages are unchanged. Their results
  come back the way the screen gets them (status "results" by id) and show on the phone as the desktop's flash line does.
- Screen-side actions done by tern web itself: engine pause/start (systemctl --user, as dash.start_engine /
  stop does), saving a report (reports.save).
- The desk refuses a live command signed by the everyday key, and logs every app command with the phone's name
  (log line "app <name>: …").
- Pure-Python P-256 ECDSA verify goes in tern/sign.py, with test vectors (NIST/Wycheproof) in the tests.

### In the app (pages and actions)

The look stays as today's phone pages. Added:
- **help** page: the desktop help's contents (dash.HELP prose, filled from the live config as the desktop does),
  with the key legend replaced by where each action is in the app.
- **settings** page: every desk setting (gate, alerts, web/app, screen log) with tern/about.py's explanation;
  tap to change; the APP section lists paired phones.
- **variant editor**: every rule setting (variants.RULE_SETTINGS) with about.py's text, opened from a variant;
  save makes a new variant (as e + s does), discard asks.
- On a variant: select, on/off, edit, rename, delete, go live / move live. Book: paper/live view. Engine:
  pause/start. Reports: save. Coins, chart span, themes, log filter: as today (the viewer's own).
- **Confirmations** exactly where the desktop asks them: y-style for on/off, delete, engine start/pause, desk
  settings, discard; a typed "live" for arming/moving live and the settings the desktop guards that way — then the
  fingerprint for the live key.
- Selecting a variant stays the app's own, as each browser's is; it does not change the desktop screen's selection.
- The JavaScript bridge (addJavascriptInterface) exists only on the approved-certificate desk pages the app already
  restricts itself to; it offers "sign this payload" and "is paired / pair", nothing else.

## Left to the builder

- Exact endpoint names, the pairing screen wording, and the app's button layout within the existing look.
- How the help's key legend is reworded for the app.
- Wrong-signature and replay handling details beyond what is above (log them; never act).
- Keeping the 36 KB-ish app small.

## Calls made without asking (for the user to check)

- Fingerprint: one touch unlocks everyday actions for 5 minutes; every live action asks again.
- Turning live off needs only the everyday key (quick, like L on the desktop).
- The app signs in with its key, no password, once paired.
- Up to 3 paired phones; unpairing from the desktop's settings.
- With browsers off, localhost browsers are off too.
- Changing the phone's fingerprints means pairing again.
