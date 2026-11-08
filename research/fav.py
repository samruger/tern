"""Favorite-longshot test on hourly markets: buy the favorite at the ask, PnL aggregated per hour."""
import sys, json, os
import numpy as np
from common import *

def run(series, lo, hi, taus, verbose=True):
    mk = {m["ticker"]: m for m in json.load(open(os.path.join(D, f"{series}_markets.json")))}
    cd = json.load(open(os.path.join(D, f"{series}_candles.json")))
    _sel = set(json.load(open(os.path.join(D, f"{series}_sel.json"))))
    cd = {k: v for k, v in cd.items() if k in _sel}
    per_hour = {}  # (close, tau) -> list pnl
    for tk, cs in cd.items():
        m = mk[tk]; c = ts(m["close_time"]); y = m["result"] == "yes"
        q = {c - t: (b, a) for t, b, a, _, _ in cs if b is not None and a is not None}
        for T in taus:
            if T not in q: continue
            b, a = float(q[T][0]), float(q[T][1])
            if a >= 1 or b <= 0: continue
            mid = (a + b) / 2
            if lo <= mid < hi:            # YES is the favorite -> buy YES at ask
                pnl = y - a - fee(a)
            elif lo <= 1 - mid < hi:      # NO is favorite -> buy NO at 1-bid
                cost = 1 - b; pnl = (1 - y) - cost - fee(cost)
            else: continue
            per_hour.setdefault(c, []).append(pnl)
    hrs = np.array(sorted(per_hour))
    v = np.array([np.mean(per_hour[h]) for h in hrs])  # avg c/contract that hour (equal size per strike)
    n = np.array([len(per_hour[h]) for h in hrs])
    half = len(hrs) // 2
    t = v.mean() / (v.std() / np.sqrt(len(v)))
    days = (hrs - hrs[0]) // 86400
    daily = [v[days == d].mean() for d in np.unique(days)]
    if verbose:
        print(f"{series} fav mid in [{lo},{hi}) taus={taus}: hours={len(hrs)} contracts/hr={n.mean():.1f} "
              f"avg={v.mean()*100:+.2f}c t={t:+.2f}  1st half={v[:half].mean()*100:+.2f}c 2nd half={v[half:].mean()*100:+.2f}c  "
              f"winning days {sum(d>0 for d in daily)}/{len(daily)}")
    return v

if __name__ == "__main__":
    for series in ["KXBTCD", "KXETHD"]:
        for lo, hi in [(0.70, 0.90), (0.80, 0.95), (0.90, 0.98), (0.60, 0.80)]:
            for taus in [(3000,), (1800,), (900,), (300,)]:
                run(series, lo, hi, taus)
        print()
