"""tern: command line.

  tern                  the screen (starts the engine in paper if it is not running)
  tern run              run the engine in the foreground
  tern status           a plain-text summary
  tern report           the rule's variants side by side (shadow records, paper only)
  tern report --profile NAME
                        a profile's variants, split by settings period ("1" or "profile 1": the desk)
  tern report --profiles
                        every profile's book side by side; --deleted: the deleted ones
  tern live-check       test the Kalshi key: balance and positions, no orders
  tern web [--host H] [--port P]
                        the screen in a browser, view only (default http://localhost:8787/)
  tern service          write a systemd user service that keeps the engine running
  tern web-service [--host H]
                        the same for tern web (tern-web.service, port 8787; --host 0.0.0.0 to open it up)
  tern web-password     set tern web's login password (prints a generated one if you enter none)
  tern web-cert         make tern web's self-signed HTTPS certificate
"""

from __future__ import annotations

import json
import os
import sys
import time

from . import config as cfgmod


def _status(runtime: str) -> int:
    from .engine import live_dir
    try:
        with open(os.path.join(live_dir(runtime), "status.json")) as fh:
            st = json.load(fh)
    except (OSError, json.JSONDecodeError):
        print("No status yet. Start the engine with: python3 -m tern run")
        return 1
    age = time.time() - st["heartbeat"]
    print(f"mode {st['mode']}{' (live armed)' if st.get('live_enabled') else ''}, "
          + (f"engine stopped {age:.0f}s ago" if st.get("stopped") else f"last poll {age:.0f}s ago")
          + (f", live error: {st['live_error']}" if st.get("live_error") else ""))
    for s, e in (st.get("events") or {}).items():
        print(f"{s}: {e['event']} closes in {max(0, e['close'] - time.time()) / 60:.1f} min")
    for name in ("paper", "live"):
        b = st.get(name) or {}
        if not b or (name == "live" and not b.get("start")):
            continue
        print(f"\n{name}: equity ${b['equity']:,.2f} (start ${b['start']:,.2f}), cash ${b['cash']:,.2f}, "
              f"at risk ${b['at_risk']:,.2f}, fees ${b['fees']:,.2f}")
        for p in b["pos"]:
            print(f"  {'YES' if p['q'] > 0 else 'NO ':3s} {abs(p['q']):g} @ {p['avg']*100:.1f}c  {p['t']}")
    g = st.get("gate") or {}
    if g:
        print(f"\nrule on paper: {g['entries']} settled, {g['open']} open, P&L ${g['pnl']:+,.2f}")
        for c in g["checks"]:
            print(f"  [{'x' if c['ok'] else ' '}] {c['name']}  ({c['detail']})")
    return 0


def _live_check() -> int:
    from .kalshi import Kalshi, KalshiError
    k = cfgmod.keys()
    if not k:
        print(f"No key. Put KALSHI_KEY_ID=... and KALSHI_KEY_FILE=/path/to/key.pem in {cfgmod.KEYFILE_ENV} (chmod 600),\n"
              "or export both. Create the key at kalshi.com > Account > API keys.")
        return 1
    try:
        api = Kalshi(*k)
    except Exception as ex:
        print(f"Could not read the private key: {ex}")
        return 1
    print(f"key {k[0][:8]}… loaded ({type(api.key).__name__})")
    try:
        print(f"balance OK: ${api.balance():,.2f}")
        pos = api.positions()
        print(f"positions OK: {len(pos)} open" + "".join(f"\n  {t} {q:+g}" for t, q in pos.items()))
    except KalshiError as ex:
        print(f"failed: {ex}")
        return 1
    print("No orders were sent.")
    return 0


def _unit_quote(text: str) -> str:
    """One systemd word: quoted so spaces survive, with % and $ kept literal."""
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{text}"'


def _unit(root: str, runtime: str, python: str) -> str:
    # The desk may live on a removable card. A user unit cannot order itself after a udisks mount,
    # so the pre-check fails while the runtime is absent and Restart= retries until the card is in.
    # It also keeps the desk from ever starting on a blank runtime.
    return f"""[Unit]
Description=tern Kalshi hourly desk
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory={root.replace("%", "%%")}
Environment={_unit_quote("TERN_RUNTIME=" + runtime)}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-%h/.config/tern/kalshi.env
ExecStartPre=/usr/bin/test -d {_unit_quote(runtime)}
ExecStart={_unit_quote(python)} -m tern run
Restart=always
RestartSec=15
TimeoutStopSec=60

[Install]
WantedBy=default.target
"""


def _web_unit(root: str, runtime: str, python: str, host: str = "127.0.0.1") -> str:
    # View only: it reads the status file the engine writes, so it needs no keys. Off localhost tern web
    # refuses to start without its login and certificate.
    return f"""[Unit]
Description=tern web screen (view only)
After=tern.service

[Service]
WorkingDirectory={root.replace("%", "%%")}
Environment={_unit_quote("TERN_RUNTIME=" + runtime)}
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/usr/bin/test -d {_unit_quote(runtime)}
ExecStart={_unit_quote(python)} -m tern web --host {host} --port 8787
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""


def _web_service(runtime: str, host: str = "127.0.0.1") -> int:
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    path = os.path.join(unit_dir, "tern-web.service")
    with open(path, "w") as fh:
        fh.write(_web_unit(os.path.realpath(cfgmod.ROOT), os.path.realpath(runtime), sys.executable, host))
    print(f"Wrote {path}\n\nStart it now and at every login:\n"
          "  systemctl --user daemon-reload\n  systemctl --user enable --now tern-web\n\n"
          "Then open http://localhost:8787/ (https:// once tern web-cert has made a certificate)")
    return 0


def _web_password() -> int:
    import getpass, secrets
    from . import webauth
    pw = getpass.getpass("new tern web password (enter for a generated one): ") if sys.stdin.isatty() else ""
    made = not pw
    if made:
        pw = secrets.token_urlsafe(15)
    elif getpass.getpass("again: ") != pw:
        print("they differ; nothing changed"); return 1
    try:
        webauth.set_password(pw)
    except ValueError as ex:
        print(ex); return 1
    print((f"password: {pw}\n" if made else "") + f"saved to {webauth.AUTH_FILE}; every browser is signed out.\n"
          "Restart tern web if it is running: systemctl --user restart tern-web")
    return 0


def _web_cert() -> int:
    import socket
    from . import webauth
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:   # the LAN address (no packet is sent)
            s.connect(("192.0.2.1", 9)); ips.add(s.getsockname()[0])
    except OSError:
        pass
    fp = webauth.make_cert(sorted(ips))
    print(f"wrote {webauth.CERT_FILE} and {webauth.KEY_FILE} for {', '.join(sorted(ips)) or 'localhost'}\n\n"
          "Browsers will warn that it is self-signed. Before accepting, check the certificate's\n"
          f"SHA-256 fingerprint matches this one:\n  {fp}\n\n"
          "Restart tern web if it is running: systemctl --user restart tern-web")
    return 0


def _service(runtime: str) -> int:
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    os.makedirs(runtime, exist_ok=True)
    path = os.path.join(unit_dir, "tern.service")
    with open(path, "w") as fh:
        fh.write(_unit(os.path.realpath(cfgmod.ROOT), os.path.realpath(runtime), sys.executable))
    print(f"Wrote {path}\n\nStart it now and at every login:\n"
          "  systemctl --user daemon-reload\n  systemctl --user enable --now tern\n\n"
          "To keep it running while you are logged out:\n  loginctl enable-linger $USER")
    return 0


def _periods(name: str, sh, lines: list[dict], sv_now: int) -> list[str]:
    """A profile's variants, one table per settings period, each headed with when it ran and what changed."""
    import datetime as dt
    from . import profiles as profmod
    from .shadow import table
    day = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    starts: dict[int, float] = {}
    why: dict[int, list[str]] = {}
    for ln in lines:
        sv = ln.get("sv")
        if isinstance(sv, int):
            starts.setdefault(sv, ln.get("ts") or 0)
            if ln.get("key") in profmod.RECORD_KEYS:
                why.setdefault(sv, []).append(f"{profmod.label(ln['key'])} {profmod.show(ln['key'], ln.get('from'))}"
                                              f" → {profmod.show(ln['key'], ln.get('to'))}")
    versions = sorted({v for n in shadow_names() for v in sh.versions(n)} | {sv_now})
    out = []
    for sv in versions:
        later = [starts[v] for v in starts if v > sv]
        when = (f"{day(starts[sv])} to {day(min(later)) if later else 'now'}" if sv in starts else
                "before settings were logged" if sv == 0 else "")
        made = any(ln.get("key") == "created" for ln in lines)
        head = f"settings {sv}" + (f", {when}" if when else "") + (": " + ", ".join(why[sv]) if why.get(sv) else
                                                                     (": as created" if made else ": the settings in use when logging began")
                                                                     if sv == 1 else "")
        rows = [r for r in sh.summary(sv=sv) if r["entries"] or r["open"]]
        out += ["", head] + (["  " + l for l in table(rows)] if rows else ["  no entries yet"])
    rows = [r for r in sh.summary() if r["entries"] or r["open"]]
    if len(versions) > 1 and rows:
        out += ["", "all settings together"] + ["  " + l for l in table(rows)]
    return out


def shadow_names() -> list[str]:
    from .shadow import ORDER, RETIRED
    return list(ORDER) + list(RETIRED)


def _report_profiles(runtime: str, args: list[str]) -> int:
    from . import profiles as profmod
    from .shadow import Shadow
    cfg = cfgmod.load(runtime)
    loaded = profmod.load_all(runtime, cfg.taker_fee)
    if args[0] == "--deleted":
        root = os.path.join(runtime, profmod.DELETED_DIR)
        gone = []
        for n in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            try:
                gone.append(profmod.Profile.load(os.path.join(root, n), cfg.taker_fee))
            except Exception as ex:
                print(f"{n}: unreadable ({ex})")
        if not gone:
            print("no deleted profiles"); return 0
        print(f"deleted profiles, kept in {root}:")
        _side_by_side([(f"{p.name} ({p.id})", p.variant, p.log.sv, p.shadow) for p in gone])
        return 0
    one = profmod.Names(runtime).profile       # profile 1's name (N on the screen renames it)
    if args[0] == "--profiles":
        rows = [(one, cfg.strategy.variant, profmod.SettingsLog(os.path.join(runtime, "settings-log.jsonl")).sv,
                 Shadow(runtime, cfg.taker_fee))]
        rows += [(p.name + ("" if p.enabled else " (off)"), p.variant, p.log.sv, p.shadow) for p in loaded]
        _side_by_side(rows)
        print("\neach profile's book is its picked variant's record; picking the best-looking of many flatters it:")
        print("look at entries and t (by hour, shows after 10 hours; under about 2 it is still noise).")
        return 0
    if args[0] == "--profile" and len(args) > 1:
        want = " ".join(args[1:]).strip().lower()
        if want in ("1", "profile 1", one.lower()):
            log = profmod.SettingsLog(os.path.join(runtime, "settings-log.jsonl"))
            print(f"{one} (profile 1, the desk): book trades {cfg.strategy.variant}, settings {log.sv}")
            print("\n".join(_periods(one, Shadow(runtime, cfg.taker_fee), log.lines(), log.sv)))
            return 0
        p = next((p for p in loaded if want in (p.id, p.name.lower())), None)
        if p is None:
            print(f"no profile {' '.join(args[1:])!r}; known: {one} (1), " + ", ".join(f"{p.name} ({p.id})" for p in loaded))
            return 2
        print(f"{p.name} ({p.id}): {'on' if p.enabled else 'off'}, book trades {p.variant}, settings {p.log.sv}")
        print("\n".join(_periods(p.name, p.shadow, p.log.lines(), p.log.sv)))
        return 0
    print("tern report [--profile NAME | --profiles | --deleted]")
    return 2


def _side_by_side(rows: list[tuple]) -> None:
    w = max(len(r[0]) for r in rows)
    print(f"{'profile':<{w}}  {'book':<8} {'set':>3} {'hours':>5} {'entries':>7} {'P&L':>9} {'¢/contract':>10} {'t':>6}")
    for name, variant, sv, sh in rows:
        r = sh.summary([variant])[0]
        pc = f"{r['per_contract'] * 100:+.2f}" if r["per_contract"] is not None else "-"
        t = f"{r['t']:+.2f}" if r["t"] is not None else "-"
        print(f"{name:<{w}}  {variant:<8} {sv:>3} {r['hours']:>5} {r['entries']:>7} {r['pnl']:>+9.2f} {pc:>10} {t:>6}")


def main(argv: list[str]) -> int:
    runtime = cfgmod.RUNTIME
    cmd = argv[0] if argv else "dash"
    if cmd in ("-h", "--help", "help"):
        print(__doc__); return 0
    if cmd == "dash":
        from .dash import main as dash
        dash(runtime); return 0
    if cmd == "run":
        from .engine import Desk
        Desk(runtime, echo=True).run(once="--once" in argv); return 0
    if cmd == "status":
        return _status(runtime)
    if cmd == "report" and len(argv) > 1:
        return _report_profiles(runtime, argv[1:])
    if cmd == "report":
        from .shadow import DESC, RETIRED, Shadow, table
        sh = Shadow(runtime)
        use = cfgmod.load(runtime).strategy.variant
        print("\n".join(table(sh.summary())))
        print("\n" + "\n".join(f"{n}: {d}" + ("  ← in use" if n == use else "") for n, d in DESC.items()))
        old = [r for r in sh.summary(list(RETIRED)) if r["entries"]]
        if old:
            print("\nretired, kept for the record:")
            print("\n".join(table(old)[1:]))
            print("\n".join(f"{n}: {d}" for n, d in RETIRED.items()))
        print("\nt is by hour and shows after 10 hours; under about 2 it is still noise.")
        return 0
    if cmd == "web":
        from .web import serve
        opt = dict(zip(argv[1::2], argv[2::2]))
        serve(runtime, opt.get("--host", "127.0.0.1"), int(opt.get("--port", 8787))); return 0
    if cmd == "live-check":
        return _live_check()
    if cmd == "service":
        return _service(runtime)
    if cmd == "web-service":
        return _web_service(runtime, dict(zip(argv[1::2], argv[2::2])).get("--host", "127.0.0.1"))
    if cmd == "web-password":
        return _web_password()
    if cmd == "web-cert":
        return _web_cert()
    print(f"unknown command {cmd!r}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
