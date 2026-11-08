"""90-day favorite-longshot test on hourly markets.
PRIMARY (fixed before looking): buy the favorite at the ask (incl. taker fee) when its mid is in [0.80, 0.98),
at 50, 30 and 15 minutes before close; equal 1 contract per (strike, snapshot); PnL averaged per hour;
significance from hour-level t-stat and week-by-week sign. Everything else is secondary/descriptive.
"""
import json, os
import numpy as np
from common import *

TAUS = (3000, 1800, 900)


def load90(series):
    mk = {m["ticker"]: m for m in json.load(open(os.path.join(D, f"{series}_markets90.json")))}
    cd = json.load(open(os.path.join(D, f"{series}_candles90.json")))
    rows = []  # close, tau, bid, ask, y
    for tk, cs in cd.items():
        m = mk[tk]; c = ts(m["close_time"]); y = m["result"] == "yes"
        for t, b, a in cs:
            if b is None or a is None: continue
            b, a = float(b), float(a)
            if a >= 1 or b <= 0: continue
            rows.append((c, c - t, b, a, y))
    return np.array(rows, dtype=float)


def strat(r, lo, hi, taus):
    c, tau, b, a, y = r.T
    k = np.isin(tau, taus)
    mid = (a + b) / 2
    yes_fav = k & (mid >= lo) & (mid < hi)
    no_fav = k & (1 - mid >= lo) & (1 - mid < hi)
    pnl = np.full(len(r), np.nan)
    pnl[yes_fav] = y[yes_fav] - a[yes_fav] - fee(a[yes_fav])
    cn = 1 - b
    pnl[no_fav] = (1 - y[no_fav]) - cn[no_fav] - fee(cn[no_fav])
    ok = ~np.isnan(pnl)
    hrs, inv = np.unique(c[ok], return_inverse=True)
    per = np.bincount(inv, pnl[ok]) / np.bincount(inv)
    return hrs, per, ok.sum()


def report(name, hrs, per, n):
    t = per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))
    wk = (hrs - hrs[0]) // (7 * 86400)
    weekly = np.array([per[wk == w].mean() for w in np.unique(wk)])
    thirds = np.array_split(per, 3)
    print(f"  {name}: hours={len(hrs)} contracts={n} avg={per.mean()*100:+.2f}c/contract t={t:+.2f} "
          f"thirds={' '.join(f'{x.mean()*100:+.2f}' for x in thirds)}  +weeks {np.sum(weekly>0)}/{len(weekly)}")


for series in ["KXBTCD"]:
    r = load90(series)
    c, tau, b, a, y = r.T
    days = (c.max() - c.min()) / 86400
    print(f"\n== {series}: {days:.0f} days, {len(np.unique(c))} hours, {len(r)} snapshots")
    report("PRIMARY fav [0.80,0.98) @50/30/15m", *strat(r, 0.80, 0.98, TAUS))
    print("  secondary:")
    for lo, hi in [(0.6, 0.8), (0.7, 0.9), (0.9, 0.98), (0.95, 0.99)]:
        report(f"fav [{lo},{hi}) @50/30/15m", *strat(r, lo, hi, TAUS))
    # descriptive calibration at the primary snapshots, with hour-clustered s.e.
    mid = (a + b) / 2; k0 = np.isin(tau, TAUS)
    print("  calibration at 50/30/15m (mid -> realized, hour-clustered se):")
    for lo, hi in [(0, .02), (.02, .05), (.05, .1), (.1, .2), (.2, .4), (.4, .6), (.6, .8), (.8, .9), (.9, .95), (.95, .98), (.98, 1)]:
        k = k0 & (mid >= lo) & (mid < hi)
        if k.sum() < 50: continue
        hrs, inv = np.unique(c[k], return_inverse=True)
        dev = np.bincount(inv, y[k] - mid[k]) / np.bincount(inv)
        print(f"    {lo:.2f}-{hi:.2f}: mid {mid[k].mean():.4f} realized {y[k].mean():.4f}  "
              f"diff {dev.mean()*100:+.2f}pt ± {dev.std(ddof=1)/np.sqrt(len(dev))*100:.2f}")
