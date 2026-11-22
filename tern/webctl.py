"""The web screen's service (tern-web.service), run from the settings box's WEB rows.

`tern web --config` (what the unit runs) takes its port and host from config.json's "web" section and
exits cleanly when web.enabled is off, so the unit (Restart=on-failure) stays down. When a WEB setting
changes the engine restarts or stops the unit here. The firewall is not touched: opening a new port
takes root (sudo ufw allow PORT/tcp), so the screen says so instead.
"""

from __future__ import annotations

import shutil
import socket
import subprocess

UNIT = "tern-web"
ENABLED = True                   # tests turn this off: no systemctl calls


def systemctl(*args: str) -> tuple[int, str]:
    """(exit code, output) of systemctl --user; (-1, why) when it can't run."""
    if not ENABLED or not shutil.which("systemctl"):
        return -1, "systemctl unavailable"
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as ex:
        return -1, str(ex)
    return r.returncode, (r.stdout + r.stderr).strip()


def state() -> str:
    """active, inactive, failed, activating (a restart loop: usually the port is taken) or unknown."""
    code, out = systemctl("is-active", UNIT)
    return out.splitlines()[-1] if code != -1 and out else "unknown"


def apply(web) -> str:
    """Make the service match web settings: running on its port and host, or stopped."""
    if not web.enabled:
        code, out = systemctl("stop", UNIT)
        return "web screen off" if code == 0 else f"web screen: stop failed ({out or code})"
    code, out = systemctl("restart", UNIT)
    if code != 0:
        return f"web screen: restart failed ({out or code}); is tern-web.service installed? (tern web-service)"
    return f"web screen restarting on port {web.port}, " + ("open to the network" if web.network else "this computer only")


def host(web) -> str:
    return "0.0.0.0" if web.network else "127.0.0.1"


def url(web) -> str:
    """Where to open it: the LAN address when it is open to the network; https once tern web-cert ran."""
    import os
    from . import webauth
    tls = os.path.exists(webauth.CERT_FILE) and os.path.exists(webauth.KEY_FILE)
    where = (lan_ip() if web.network else None) or "localhost"
    return f"{'https' if tls else 'http'}://{where}:{web.port}/"


def lan_ip() -> str | None:
    """This computer's LAN address (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None
