"""tern: command line.

  tern                  the screen (starts the engine in paper if it is not running)
  tern run              run the engine in the foreground
  tern status           a plain-text summary
  tern report           the rule's variants side by side (shadow records, paper only)
  tern live-check       test the Kalshi key: balance and positions, no orders
  tern web [--host H] [--port P]
                        the screen in a browser, view only (default http://localhost:8787/)
  tern service          write a systemd user service that keeps the engine running
  tern web-service      the same for tern web (tern-web.service, localhost:8787)
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


def _web_unit(root: str, runtime: str, python: str) -> str:
    # View only and on localhost: it reads the status file the engine writes, so it needs no keys.
    return f"""[Unit]
Description=tern web screen (view only)
After=tern.service

[Service]
WorkingDirectory={root.replace("%", "%%")}
Environment={_unit_quote("TERN_RUNTIME=" + runtime)}
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/usr/bin/test -d {_unit_quote(runtime)}
ExecStart={_unit_quote(python)} -m tern web --host 127.0.0.1 --port 8787
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""


def _web_service(runtime: str) -> int:
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    path = os.path.join(unit_dir, "tern-web.service")
    with open(path, "w") as fh:
        fh.write(_web_unit(os.path.realpath(cfgmod.ROOT), os.path.realpath(runtime), sys.executable))
    print(f"Wrote {path}\n\nStart it now and at every login:\n"
          "  systemctl --user daemon-reload\n  systemctl --user enable --now tern-web\n\n"
          "Then open http://localhost:8787/")
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
        return _web_service(runtime)
    print(f"unknown command {cmd!r}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
