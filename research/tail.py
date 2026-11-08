"""Trade-level: who wins, takers or makers? By time-to-close and price. Plus model-gated taking in the final minutes."""
import sys, json, os
import numpy as np
from math import erf, sqrt
from common import *

mode = sys.argv[1] if len(sys.argv) > 1 else "tail"
LAG = 1


def zmodel(S, K, c, t, s2):
    """z-score that the settle avg (60s before c) >= K, using spot up to second t."""
    tau = c - t
    St = S.at(t - 1)
    if tau >= 60:
        return np.log(St / K) / np.sqrt(s2 * (tau - 40))
    known = S.cs[t - S.t0] - S.cs[c - 60 - S.t0]
    mean = (known + tau * St) / 60
    sd = St * np.sqrt(s2 * max(tau, 1) ** 3 / 3) / 60
    return (mean - K) / max(sd, 1e-12)


Phi = lambda x: 0.5 * (1 + erf(x / sqrt(2)))

for series, sym in [("KXBTC15M", "BTCUSDT"), ("KXETH15M", "ETHUSDT")]:
    S = Spot(sym)
    mk = {m["ticker"]: m for m in json.load(open(os.path.join(D, f"{series}_markets.json")))}
    tr = json.load(open(os.path.join(D, f"{series}_trades_{mode}.json")))
    R = []  # tau, taker cost, taker won, count, model prob for taker side, open
    for tk, rows in tr.items():
        m = mk[tk]; y = m["result"] == "yes"
        o, c = ts(m["open_time"]), ts(m["close_time"])
        ok = S.ok(o - 1000) and S.ok(c + 1)
        K = S.avg(o - 60, o) if ok else None
        s2 = None
        for tstr, side, yp, n in rows:
            t = dt.datetime.fromisoformat(tstr.replace("Z", "+00:00")).timestamp()
            tau = c - t
            if tau <= 0: continue
            cost = yp if side == "yes" else 1 - yp
            won = y if side == "yes" else not y
            pm = np.nan
            if ok:
                ti = int(t) - LAG
                if s2 is None: s2 = np.var(np.diff(np.log(S.p[o - 900 - S.t0:o - S.t0:10]))) / 10
                pz = Phi(zmodel(S, K, c, ti, s2))
                pm = pz if side == "yes" else 1 - pz
            R.append((tau, cost, won, n, pm, o))
    R = np.array(R, dtype=float)
    tau, cost, won, n, pm, o = R.T
    tk_pnl = won - cost - fee(cost)            # taker per contract
    mk_pnl = cost - won                        # maker per contract (no maker fee on 15m)
    print(f"\n== {series} [{mode}] trades={len(R)} contracts={n.sum():,.0f}")
    print("  overall: taker avg {:+.2f}c/contract, maker avg {:+.2f}c (gross of rebates), fee avg {:.2f}c".format(
        np.average(tk_pnl, weights=n) * 100, np.average(mk_pnl, weights=n) * 100, np.average(fee(cost), weights=n) * 100))
    tb = [(0, 10), (10, 30), (30, 60), (60, 120), (120, 300), (300, 600), (600, 901)]
    pb = [(0, .05), (.05, .2), (.2, .5), (.5, .8), (.8, .95), (.95, 1.0)]
    print("  maker c/contract by time-to-close (rows) x taker cost (cols); [contracts k]")
    print("  " + " " * 10 + "".join(f"{f'{a:.2f}-{b:.2f}':>16}" for a, b in pb))
    for a, b in tb:
        k0 = (tau >= a) & (tau < b)
        if not k0.any(): continue
        cells = []
        for pa, pbb in pb:
            k = k0 & (cost >= pa) & (cost < pbb)
            cells.append(f"{np.average(mk_pnl[k], weights=n[k])*100:+6.2f} [{n[k].sum()/1e3:6.0f}]" if n[k].sum() > 0 else " " * 16)
        print(f"  {a:>4}-{b:<4}s" + "".join(f"{c:>16}" for c in cells))
    # calibration of taker cost vs win rate at extremes
    print("  extremes: cost bucket -> contract-weighted win rate (taker), avg cost")
    for pa, pbb in [(0, .02), (.02, .05), (.05, .1), (.9, .95), (.95, .98), (.98, 1)]:
        k = (cost >= pa) & (cost < pbb)
        if n[k].sum(): print(f"    {pa:.2f}-{pbb:.2f}: win {np.average(won[k],weights=n[k]):.4f} cost {np.average(cost[k],weights=n[k]):.4f}  n_trades={k.sum()}")
    # model-gated taking: if model says the taker side is worth > cost+fee+edge, would buying at that trade price have paid?
    ok = ~np.isnan(pm)
    split = np.quantile(np.unique(o), 0.5)
    print("  model-gated taking at observed trade prices (per-contract, equal weight per trade):")
    for edge in [0.02, 0.05, 0.10]:
        out = []
        for name, sel in [("train", o < split), ("test", o >= split)]:
            for side_sign, lab in [(1, "with")]:
                k = ok & sel & (pm - cost - fee(cost) > edge)
                out.append(f"{name}: n={k.sum()} avg={tk_pnl[k].mean()*100:+.2f}c" if k.any() else f"{name}: n=0")
        print(f"    edge>{edge:.2f}: " + "  ".join(out))
