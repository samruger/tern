"""Settings, kept in runtime/config.json and re-read every few seconds. Missing keys fall back to these defaults."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.environ.get("TERN_RUNTIME") or os.path.join(ROOT, "runtime")
KEYFILE_ENV = os.path.expanduser("~/.config/tern/kalshi.env")

SERIES = {  # Kalshi hourly above/below series -> Coinbase spot product
    "KXBTCD": "BTC-USD",
    "KXETHD": "ETH-USD",
}


@dataclass
class Strategy:
    """The hourly favorite rule. Defaults are the variant picked on the first half of 67 days of
    BTC history and checked on the untouched second half (+0.61c/contract, t=1.1: positive, unproven)."""
    auto: bool = True                          # place the rule's trades by itself (paper always; live only past the gate)
    series: list = field(default_factory=lambda: ["KXBTCD"])   # ETH was not tested on 67 days
    band_lo: float = 0.75                      # favorite's mid must be in [band_lo, band_hi)
    band_hi: float = 0.95
    minutes: list = field(default_factory=lambda: [25, 20, 15, 10])  # entry slots, minutes before the close
    max_spread: float = 0.02
    contracts: float = 5.0                     # per strike per slot
    max_hour_cost: float = 60.0                # dollars the rule may put into one hourly event


@dataclass
class Live:
    enabled: bool = False          # set from the screen (L, then type "live") or here. The desk never sets it itself.
    capital_usd: float = 100.0     # most the live book may have at risk (cost of open positions) at once
    max_order_usd: float = 25.0    # largest single live order, manual or auto
    max_daily_loss: float = 30.0   # live loss since local midnight that cancels everything and returns to paper
    auto_needs_gate: bool = True   # the rule trades live only after its paper record passes the gate


@dataclass
class Gate:
    min_days: float = 14.0
    min_entries: int = 300         # settled paper entries by the rule
    min_pnl: float = 0.0           # paper P&L of the rule, dollars, after fees


@dataclass
class Config:
    paper_capital: float = 1_000.0
    taker_fee: float = 0.07        # Kalshi: ceil(0.07 * C * P * (1-P)) per order, to the cent
    view_strikes: int = 15         # rows in the ladder
    live: Live = field(default_factory=Live)
    strategy: Strategy = field(default_factory=Strategy)
    gate: Gate = field(default_factory=Gate)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        def keep(kind, data):
            names = {f.name for f in fields(kind)}
            return {k: v for k, v in (data or {}).items() if k in names}
        top = keep(cls, d)
        top["live"] = Live(**keep(Live, d.get("live")))
        top["strategy"] = Strategy(**keep(Strategy, d.get("strategy")))
        top["gate"] = Gate(**keep(Gate, d.get("gate")))
        return cls(**top)


def load(runtime: str = RUNTIME) -> Config:
    path = os.path.join(runtime, "config.json")
    try:
        with open(path) as fh:
            return Config.from_dict(json.load(fh))
    except FileNotFoundError:
        cfg = Config()
        save(cfg, runtime)
        return cfg
    except (json.JSONDecodeError, TypeError):
        return Config()


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
