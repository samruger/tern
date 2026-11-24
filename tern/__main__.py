"""tern: command line.

  tern                  the screen (starts the engine in paper if it is not running)
  tern run              run the engine in the foreground
  tern status           a plain-text summary
  tern report           the variants side by side, built in and your own (paper records)
  tern report --periods the same, split by settings period
  tern report --save    save a summary (status, variants) as runtime/report-*.txt; o on the screen shows it
  tern report-timer     write a systemd user timer that does that every Monday at 10:00
  tern live-check       test the Kalshi key: balance and positions, no orders
  tern web [--host H] [--port P] | --config
                        the screen in a browser, view only (default http://localhost:8787/)
  tern service          write a systemd user service that keeps the engine running
  tern web-service [--host H]
                        the same for tern web (tern-web.service; --host 0.0.0.0 opens it up); its port,
                        on/off and network access are the settings box's WEB rows
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
    b = st.get("live") or {}
    if b.get("start"):
        print(f"\nlive: equity ${b['equity']:,.2f} (start ${b['start']:,.2f}), cash ${b['cash']:,.2f}, "
              f"at risk ${b['at_risk']:,.2f}, fees ${b['fees']:,.2f}")
        for p in b["pos"]:
            print(f"  {'YES' if p['q'] > 0 else 'NO ':3s} {abs(p['q']):g} @ {p['avg']*100:.1f}c  {p['t']}")
    g = st.get("gate") or {}
    rows = st.get("variants") or []
    if rows:
        print("\nvariants on paper:")
        for v in rows:
            print(f"  {v['name']:<12} {'on ' if v.get('on', True) else 'off'} {v['entries']:>6} settled, {v['open']} open, P&L ${v['pnl']:+,.2f}"
                  + ("  ← live" if v.get("live") and st.get("live_enabled") else ""))
    if g:
        print(f"\nthe live gate ({g.get('variant')}):")
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


def _web_unit(root: str, runtime: str, python: str) -> str:
    # View only: it reads the status file the engine writes, so it needs no keys. Off localhost tern web
    # refuses to start without its login and certificate. Its port, host and on/off are config.json's
    # "web" section (the settings box's WEB rows): off, it exits 0 and on-failure leaves it down.
    return f"""[Unit]
Description=tern web screen (view only)
After=tern.service

[Service]
WorkingDirectory={root.replace("%", "%%")}
Environment={_unit_quote("TERN_RUNTIME=" + runtime)}
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/usr/bin/test -d {_unit_quote(runtime)}
ExecStart={_unit_quote(python)} -m tern web --config
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
"""


def _web_service(runtime: str, network: bool | None = None) -> int:
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    path = os.path.join(unit_dir, "tern-web.service")
    with open(path, "w") as fh:
        fh.write(_web_unit(os.path.realpath(cfgmod.ROOT), os.path.realpath(runtime), sys.executable))
    cfg = cfgmod.load(runtime)
    if network is not None:                      # --host 0.0.0.0 / 127.0.0.1 sets the settings box's row
        cfg.web.network = network
        cfgmod.save(cfg, runtime)
    print(f"Wrote {path}\n\nStart it now and at every login:\n"
          "  systemctl --user daemon-reload\n  systemctl --user enable --now tern-web\n\n"
          f"Then open http://localhost:{cfg.web.port}/ (https:// once tern web-cert has made a certificate).\n"
          "Its port, on/off and network access are in the screen's settings box (e), WEB.")
    return 0


def _report_units(root: str, runtime: str, python: str) -> dict[str, str]:
    return {"tern-save-report.service": f"""[Unit]
Description=tern: save a report

[Service]
Type=oneshot
WorkingDirectory={root.replace("%", "%%")}
Environment={_unit_quote("TERN_RUNTIME=" + runtime)}
ExecStartPre=/usr/bin/test -d {_unit_quote(runtime)}
ExecStart={_unit_quote(python)} -m tern report --save
""", "tern-save-report.timer": """[Unit]
Description=tern: save a report every week

[Timer]
# If the machine is off then, soon after it is next up.
OnCalendar=Mon *-*-* 10:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""}


def _report_timer(runtime: str) -> int:
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    for name, text in _report_units(os.path.realpath(cfgmod.ROOT), os.path.realpath(runtime), sys.executable).items():
        with open(os.path.join(unit_dir, name), "w") as fh:
            fh.write(text)
        print(f"Wrote {os.path.join(unit_dir, name)}")
    print("\nStart it:\n  systemctl --user daemon-reload\n  systemctl --user enable --now tern-save-report.timer\n\n"
          "It saves a report every Monday at 10:00; o on the screen lists them.")
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


def _report(runtime: str, args: list[str]) -> int:
    from . import reports
    if not args:
        out = reports.desk(runtime)
    elif args == ["--periods"]:
        out = reports.periods(runtime)
    elif args == ["--save"]:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _status(runtime)
        name = reports.save_text(runtime, "summary", "tern report --save", [*buf.getvalue().rstrip().split("\n"), "",
                                 *reports.desk(runtime)])
        print(f"saved {os.path.join(runtime, name)}")
        return 0
    else:
        print("tern report [--periods | --save]")
        return 2
    print("\n".join(out))
    return 0


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
    if cmd == "report":
        return _report(runtime, argv[1:])
    if cmd == "report-timer":
        return _report_timer(runtime)
    if cmd == "web":
        from .web import serve
        from .webctl import host
        if "--config" in argv:                   # the unit's way: the settings box's WEB rows
            W = cfgmod.load(runtime).web
            if not W.enabled:
                print("tern web is off in the settings box (e, WEB): not starting"); return 0
            serve(runtime, host(W), W.port); return 0
        opt = dict(zip(argv[1::2], argv[2::2]))
        serve(runtime, opt.get("--host", "127.0.0.1"), int(opt.get("--port", 8787))); return 0
    if cmd == "live-check":
        return _live_check()
    if cmd == "service":
        return _service(runtime)
    if cmd == "web-service":
        h = dict(zip(argv[1::2], argv[2::2])).get("--host")
        return _web_service(runtime, None if h is None else h not in ("127.0.0.1", "localhost", "::1"))
    if cmd == "web-password":
        return _web_password()
    if cmd == "web-cert":
        return _web_cert()
    print(f"unknown command {cmd!r}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
