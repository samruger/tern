"""Fair value for 15m up/down vs Kalshi quotes; calibration + taker backtest."""
import sys
import numpy as np
from math import erf, sqrt
from common import *

def Phi(x): return 0.5 * (1 + np.vectorize(erf)(x / sqrt(2)))

LAG = int(sys.argv[1]) if len(sys.argv) > 1 else 2      # seconds of stale spot data
VW = 900                                                # vol window (s)
VSTEP = 10                                              # vol from 10s returns (less microstructure noise)


def rv10(S, t, w):
    i = t - S.t0
    seg = np.log(S.p[i - w:i:VSTEP])
    return np.var(np.diff(seg)) / VSTEP if len(seg) > 3 else np.nan


def snapshots(series, sym, LAG=LAG):
    S = Spot(sym); mk, cd = load(series)
    out = []
    for m in mk:
        if m.get("result") not in ("yes", "no"): continue
        o, c = ts(m["open_time"]), ts(m["close_time"])
        if not (S.ok(o - VW - 61) and S.ok(c + 1)): continue
        K = S.avg(o - 60, o); y = m["result"] == "yes"
        for t, bid, ask, last, vol in cd.get(m["ticker"], []):
            tau = c - t
            if tau <= 0 or tau > 900 or bid is None or ask is None: continue
            bid, ask = float(bid), float(ask)
            if ask >= 1 or bid <= 0: continue
            tl = t - LAG
            s2 = rv10(S, tl, VW)
            St = S.at(tl - 1)
            tau_l = tau + LAG
            if tau_l >= 60:
                sd = np.sqrt(s2 * (tau_l - 40)); z = np.log(St / K) / sd
            else:
                known = S.cs[c - 60 - S.t0 + (60 - tau_l)] - S.cs[c - 60 - S.t0]
                mean = (known + tau_l * St) / 60
                sd = St * np.sqrt(s2 * tau_l ** 3 / 3) / 60
                z = (mean - K) / max(sd, 1e-9)
            out.append((tau, bid, ask, z, y, o))
    a = np.array(out, dtype=float)
    return a


def ll(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4); return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))


def main():
  for series, sym in [("KXBTC15M", "BTCUSDT"), ("KXETH15M", "ETHUSDT")]:
    a = snapshots(series, sym)
    tau, bid, ask, z, y, o = a.T
    p = Phi(z); mid = (bid + ask) / 2
    print(f"\n== {series}  snapshots={len(a)}  lag={LAG}s")
    print(f"  median spread {np.median(ask-bid)*100:.1f}c")
    for lo, hi in [(600, 901), (300, 600), (120, 300), (1, 120)]:
        k = (tau >= lo) & (tau < hi)
        print(f"  tau[{lo},{hi}) n={k.sum():5d}  logloss mid={ll(mid[k],y[k]):.4f} model={ll(p[k],y[k]):.4f} "
              f"blend={ll(0.5*mid[k]+0.5*p[k],y[k]):.4f}   mean|model-mid|={np.mean(abs(p[k]-mid[k]))*100:.1f}c")
    # calibration of mid by bucket
    print("  calibration (mid bucket: mean mid, realized, n)")
    for lo in np.arange(0, 1, 0.1):
        k = (mid >= lo) & (mid < lo + 0.1)
        if k.sum(): print(f"    {lo:.1f}-{lo+.1:.1f}: {mid[k].mean():.3f} {y[k].mean():.3f} {k.sum()}")
    # taker backtest: one trade per market at first qualifying snapshot, hold to settle
    split = np.quantile(np.unique(o), 0.5)
    for edge in [0.0, 0.02, 0.04, 0.06, 0.10]:
        res = {}
        for half, sel in [("train", o < split), ("test", o >= split)]:
            pnl, n, seen = [], 0, set()
            order = np.argsort(o * 1e4 - tau)
            for i in order:
                if not sel[i] or o[i] in seen: continue
                if p[i] - ask[i] - fee(ask[i]) > edge:
                    pnl.append((1.0 if y[i] else 0.0) - ask[i] - fee(ask[i])); seen.add(o[i])
                elif bid[i] - p[i] - fee(bid[i]) > edge:
                    nb = 1 - bid[i]
                    pnl.append((0.0 if y[i] else 1.0) - nb - fee(nb)); seen.add(o[i])
            pnl = np.array(pnl)
            res[half] = (len(pnl), pnl.mean() if len(pnl) else 0, pnl.sum())
        print(f"  edge>{edge:.2f}: " + "  ".join(f"{h}: n={n} avg={m*100:+.2f}c tot=${s:+.1f}" for h, (n, m, s) in res.items()))


if __name__ == "__main__":
    main()
