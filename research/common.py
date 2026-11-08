import json, os, glob, datetime as dt
import numpy as np
import pandas as pd

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def spot(sym):
    """Per-second close series indexed by unix second (forward filled)."""
    cache = os.path.join(D, f"{sym}_1s.npz")
    fs = sorted(glob.glob(os.path.join(D, f"{sym}-1s-*.csv")))
    if os.path.exists(cache) and os.path.getmtime(cache) > max(os.path.getmtime(f) for f in fs):
        z = np.load(cache); return int(z["t0"]), z["p"]
    parts = [pd.read_csv(f, header=None, names=["t", "p"]) for f in fs]
    df = pd.concat(parts)
    t = df.t.values
    t = np.where(t > 1e14, t // 1_000_000, t // 1000)  # us or ms
    t0 = int(t.min()); n = int(t.max()) - t0 + 1
    p = np.full(n, np.nan); p[t - t0] = df.p.values
    p = pd.Series(p).ffill().values
    np.savez(cache, t0=t0, p=p)
    return t0, p


class Spot:
    def __init__(self, sym):
        self.t0, self.p = spot(sym)
        self.lr = np.diff(np.log(self.p), prepend=np.log(self.p[0]))
        self.cs = np.concatenate([[0], np.cumsum(self.p)])
        self.cs2 = np.concatenate([[0], np.cumsum(self.lr ** 2)])

    def ok(self, t):
        return self.t0 <= t < self.t0 + len(self.p)

    def at(self, t):
        return self.p[t - self.t0]

    def avg(self, a, b):
        """mean of per-second prices for seconds in [a, b)."""
        i, j = a - self.t0, b - self.t0
        return (self.cs[j] - self.cs[i]) / (j - i)

    def rv(self, t, w):
        """per-second variance of log returns over [t-w, t)."""
        i = t - self.t0
        return (self.cs2[i] - self.cs2[i - w]) / w


def load(series):
    mk = json.load(open(os.path.join(D, f"{series}_markets.json")))
    cd = json.load(open(os.path.join(D, f"{series}_candles.json")))
    return mk, cd


def fee(p, rate=0.07):
    """Kalshi taker fee per contract, large-order limit (no per-order rounding)."""
    return rate * p * (1 - p)
