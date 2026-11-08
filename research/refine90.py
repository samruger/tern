"""Refinements of the hourly favorite rule. Select on the FIRST half of hours, report the SECOND half untouched.
Base: buy favorite at ask (+taker fee) when fav mid in band at given snapshot minutes."""
import json, os
import numpy as np
from common import *

mk = {m["ticker"]: m for m in json.load(open(os.path.join(D, "KXBTCD_markets90.json")))}
cd = json.load(open(os.path.join(D, "KXBTCD_candles90.json")))
R = []  # close, tau, cost, win, spread, mid_fav
for tk, cs in cd.items():
    m = mk[tk]; c = ts(m["close_time"]); y = m["result"] == "yes"
    for t, b, a in cs:
        if b is None or a is None: continue
        b, a = float(b), float(a); tau = c - t
        if a >= 1 or b <= 0 or tau % 300: continue
        mid = (a + b) / 2
        if mid >= 0.5: R.append((c, tau, a, float(y), a - b, mid))
        else: R.append((c, tau, 1 - b, float(not y), a - b, 1 - mid))
R = np.array(R)
c, tau, cost, win, spr, fm = R.T
pnl = win - cost - fee(cost)
hrs = np.unique(c); split = hrs[len(hrs) // 2]


def score(k):
    out = []
    for sel in (c < split, c >= split):
        kk = k & sel
        h, inv = np.unique(c[kk], return_inverse=True)
        per = np.bincount(inv, pnl[kk]) / np.bincount(inv)
        out.append((per.mean() * 100, per.mean() / (per.std(ddof=1) / np.sqrt(len(per))), kk.sum()))
    return out


cands = []
for lo, hi in [(0.7, 0.9), (0.75, 0.95), (0.8, 0.95), (0.8, 0.98), (0.85, 0.97), (0.9, 0.98)]:
    for taus in [(3000, 1800, 900), (3000,), (1800,), (900,), (3300, 3000, 2700, 2400), (2400, 1800, 1200), (1500, 1200, 900, 600)]:
        for smax in [0.011, 0.021, 1.0]:
            k = (fm >= lo) & (fm < hi) & np.isin(tau, taus) & (spr <= smax)
            (a1, t1, n1), (a2, t2, n2) = score(k)
            cands.append((t1, a1, n1, a2, t2, n2, f"band[{lo},{hi}) taus={taus} spread<={smax:.2f}"))
cands.sort(reverse=True)
print("top 8 by FIRST-half t-stat, with untouched SECOND half:")
for t1, a1, n1, a2, t2, n2, name in cands[:8]:
    print(f"  {name:60s} H1 {a1:+.2f}c t={t1:+.2f} n={n1}  |  H2 {a2:+.2f}c t={t2:+.2f} n={n2}")
base = [x for x in cands if x[-1] == "band[0.8,0.98) taus=(3000, 1800, 900) spread<=1.00"][0]
print(f"\nbase (pre-registered): H1 {base[1]:+.2f}c t={base[0]:+.2f} | H2 {base[3]:+.2f}c t={base[4]:+.2f}")
h2 = np.array([x[3] for x in cands]); print(f"all {len(cands)} variants, second-half avg: median {np.median(h2):+.2f}c, share>0 {np.mean(h2>0):.0%}")
