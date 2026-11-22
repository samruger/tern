"""Settings, kept in runtime/config.json and re-read every few seconds. Missing keys fall back to these defaults."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.environ.get("TERN_RUNTIME") or os.path.join(ROOT, "runtime")
KEYFILE_ENV = os.path.expanduser("~/.config/tern/kalshi.env")

SERIES = {  # Kalshi hourly above/below series -> Coinbase spot product; tab cycles them in this order
    "KXBTCD": "BTC-USD",
    "KXETHD": "ETH-USD",
    "KXSOLD": "SOL-USD",
    "KXXRPD": "XRP-USD",
    "KXBNBD": "BNB-USD",
    "KXHYPED": "HYPE-USD",
}


@dataclass
class Strategy:
    """The hourly favorite rule. Defaults are the variant picked on the first half of 67 days of
    BTC history and checked on the untouched second half (+0.61c/contract, t=1.1: positive, unproven)."""
    series: list = field(default_factory=lambda: ["KXBTCD"])   # ETH was not tested on 67 days
    band_lo: float = 0.75                      # favorite's mid must be in [band_lo, band_hi)
    band_hi: float = 0.95
    minutes: list = field(default_factory=lambda: [25, 20, 15, 10])  # entry slots, minutes before the close
    max_spread: float = 0.02
    contracts: float = 5.0                     # per strike per slot
    max_hour_cost: float = 60.0                # dollars the rule may put into one hourly event
    variant: str = "base"                      # the shadow variant paper and live trade (shadow.VARIANTS); set from
                                               # the screen's variants box. Live needs that variant's own record past the gate.


@dataclass
class Live:
    enabled: bool = False          # set from the screen (L, then type "live") or here. The desk never sets it itself.
    capital_usd: float = 100.0     # most the live book may have at risk (cost of open positions) at once
    max_order_usd: float = 25.0    # largest single live order the rule may send
    max_daily_loss: float = 30.0   # live loss since local midnight that cancels everything and returns to paper
    auto_needs_gate: bool = True   # the rule trades live only after its paper record passes the gate
    contracts: float = 5.0         # contracts live buys per strike per slot (paper uses strategy.contracts)


@dataclass
class Gate:
    min_days: float = 14.0
    min_entries: int = 300         # settled paper entries by the rule
    min_pnl: float = 0.0           # paper P&L of the rule, dollars, after fees


@dataclass
class Web:
    """tern web, the screen in a browser (view only). tern-web.service reads these (tern web --config);
    the engine restarts or stops it when one changes from the settings box."""
    enabled: bool = True
    port: int = 8787
    network: bool = False          # on: every address (needs its login and HTTPS); off: this computer only


@dataclass
class Config:
    paper_capital: float = 1_000.0
    taker_fee: float = 0.07        # Kalshi: ceil(0.07 * C * P * (1-P)) per order, to the cent
    view_strikes: int = 15         # rows in the ladder
    alerts: bool = True            # desktop notifications (notify-send) for live events, errors, engine stop
    live: Live = field(default_factory=Live)
    strategy: Strategy = field(default_factory=Strategy)
    gate: Gate = field(default_factory=Gate)
    web: Web = field(default_factory=Web)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        def keep(kind, data):
            """Known keys only, each coerced to its default's type. A value that can't be (minutes
            as text, a list where a number goes) raises TypeError, and load() keeps the config in use."""
            base, out = kind(), {}
            for k, v in (data or {}).items():
                if k not in {f.name for f in fields(kind)}:
                    continue
                want = getattr(base, k)
                if isinstance(want, bool):
                    if not isinstance(v, bool):
                        raise TypeError(f"{k} must be true or false")
                elif isinstance(want, (int, float)):
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                        raise TypeError(f"{k} must be a number")
                    v = type(want)(v)
                elif isinstance(want, list):
                    if not isinstance(v, list):
                        raise TypeError(f"{k} must be a list")
                    if want and isinstance(want[0], (int, float)):
                        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v):
                            raise TypeError(f"{k} must be a list of numbers")
                    elif any(not isinstance(x, str) for x in v):
                        raise TypeError(f"{k} must be a list of names")
                out[k] = v
            return out
        top = keep(cls, d)
        top["live"] = Live(**keep(Live, d.get("live")))
        top["strategy"] = Strategy(**keep(Strategy, d.get("strategy")))
        top["gate"] = Gate(**keep(Gate, d.get("gate")))
        top["web"] = Web(**keep(Web, d.get("web")))
        return cls(**top)


@dataclass(frozen=True)
class Setting:
    """One row of the screen's settings box (e). kind: "usd", "n" (a count) or "bool". risk: the direction
    that loosens a safety limit (+1 raising, -1 lowering, 0 neither); while live is armed that direction
    asks you to type "live" first."""
    sec: str
    name: str
    label: str
    lo: float = 0.0
    hi: float = 0.0
    step: float = 1.0
    kind: str = "usd"
    risk: int = 0
    what: str = ""
    group: str = ""

    @property
    def key(self) -> str:
        return f"{self.sec}.{self.name}" if self.sec else self.name


# The settings the screen's settings box (e) may change. The engine checks every change against this list
# and its bounds; anything else stays a config.json edit.
SETTINGS = [
    Setting("live", "capital_usd", "most at risk at once", 1, 10_000, 5, risk=+1, group="LIVE",
            what="$ of open live positions the rule may hold at once"),
    Setting("live", "max_order_usd", "largest order", 1, 1_000, 1, risk=+1,
            what="$ cap on any one live order"),
    Setting("live", "max_daily_loss", "daily loss limit", 1, 5_000, 5, risk=+1,
            what="live loss since local midnight that turns live off"),
    Setting("live", "contracts", "contracts a strike", 1, 100, 1, kind="n", risk=+1,
            what="contracts live buys per strike per slot; paper and the variants' records keep their own size"),
    Setting("strategy", "contracts", "contracts a strike", 1, 100, 1, kind="n", group="PAPER",
            what="contracts paper and the variants' records buy per strike per slot ($ P&L scales with it)"),
    Setting("strategy", "max_hour_cost", "most an hour", 5, 2_000, 5,
            what="$ the rule may put into one hourly event (paper, live and the variants' records)"),
    Setting("", "paper_capital", "paper start", 100, 1_000_000, 100,
            what="the paper book's starting cash; takes effect at the next reset (R)"),
    Setting("gate", "min_days", "days of hours traded", 1, 90, 1, kind="n", risk=-1, group="GATE",
            what="hours the variant in use has traded on paper, in days, before live may trade it"),
    Setting("gate", "min_entries", "settled entries", 10, 10_000, 50, kind="n", risk=-1,
            what="settled paper entries the variant in use needs before live may trade it"),
    Setting("gate", "min_pnl", "paper P&L at least", -1_000, 10_000, 10, risk=-1,
            what="paper P&L after fees the variant in use needs before live may trade it"),
    Setting("", "alerts", "desktop alerts", kind="bool", group="ALERTS",
            what="pop-ups for live orders and settlements, live turning off, errors and the engine stopping"),
    Setting("web", "enabled", "web screen", kind="bool", group="WEB",
            what="tern web: this screen in a browser, view only; off stops tern-web.service"),
    Setting("web", "port", "port", 1024, 65535, 1, kind="n",
            what="the port tern web listens on; a new one needs sudo ufw allow PORT/tcp (and the router forward)"),
    Setting("web", "network", "open to the network", kind="bool",
            what="on: other computers can reach it (login + HTTPS required); off: this computer only"),
]


def setting(key: str) -> Setting | None:
    """The SETTINGS row for "section.field" (or "field" for a top-level one)."""
    return next((r for r in SETTINGS if r.key == key), None)


def get_setting(cfg: "Config", key: str):
    r = setting(key)
    return getattr(getattr(cfg, r.sec) if r.sec else cfg, r.name)


def set_setting(cfg: "Config", key: str, value):
    """Set one SETTINGS value on cfg (not saved). Raises ValueError on an unknown key or a bad value."""
    r = setting(key)
    if r is None:
        raise ValueError(f"{key!r} is not a setting the screen may change")
    obj = getattr(cfg, r.sec) if r.sec else cfg
    if r.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{r.label} must be on or off")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise ValueError(f"{r.label} must be a number")
        if not r.lo <= value <= r.hi:
            raise ValueError(f"{r.label} must be between {r.lo:g} and {r.hi:g}")
        value = type(getattr(obj, r.name))(value)
    setattr(obj, r.name, value)
    return value


def load(runtime: str = RUNTIME, fallback: Config | None = None) -> Config:
    """Read config.json. If it is unreadable, keep `fallback` (the config already in use) over the defaults."""
    path = os.path.join(runtime, "config.json")
    try:
        with open(path) as fh:
            return Config.from_dict(json.load(fh))
    except FileNotFoundError:
        cfg = Config()
        save(cfg, runtime)
        return cfg
    except (OSError, ValueError, TypeError, AttributeError):   # unreadable, not JSON, or a value of the wrong type
        return fallback or Config()


def save(cfg: Config, runtime: str = RUNTIME) -> None:
    os.makedirs(runtime, exist_ok=True)
    path = os.path.join(runtime, "config.json")
    with open(path + ".tmp", "w") as fh:
        json.dump(cfg.to_dict(), fh, indent=2)
    os.replace(path + ".tmp", path)


def keys() -> tuple[str, str] | None:
    """(key id, private key PEM text) from KALSHI_KEY_ID + KALSHI_KEY_FILE, env or ~/.config/tern/kalshi.env."""
    vals = {k: os.environ.get(k) for k in ("KALSHI_KEY_ID", "KALSHI_KEY_FILE")}
    if not all(vals.values()) and os.path.exists(KEYFILE_ENV):
        with open(KEYFILE_ENV) as fh:
            for line in fh:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    vals.setdefault(k.strip(), None)
                    if not vals.get(k.strip()):
                        vals[k.strip()] = v.strip().strip("\"'")
    kid, kf = vals.get("KALSHI_KEY_ID"), vals.get("KALSHI_KEY_FILE")
    if not kid or not kf:
        return None
    kf = os.path.expanduser(kf)
    try:
        with open(kf) as fh:
            return kid, fh.read()
    except OSError:
        return None
