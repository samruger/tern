"""Hourly above/below: Kalshi calibration, model info beyond mid, taker backtest."""
import sys, json, os
import numpy as np
from math import erf, sqrt
from common import *
from resid_lib import fit, ll

LAG = int(sys.argv[1]) if len(sys.argv) > 1 else 2
Phi = np.vectorize(lambda x: 0.5 * (1 + erf(x / sqrt(2))))


def basis_fn(S, series15):
    mk = json.load(open(os.path.join(D, f"{series15}_markets.json")))
    pts = sorted((ts(m["open_time"]), np.log(S.avg(ts(m["open_time"]) - 60, ts(m["open_time"])) / m["floor_strike"]))
                 for m in mk if m.get("floor_strike") and S.ok(ts(m["open_time"]) - 61))
    T = np.array([p[0] for p in pts]); V = np.array([p[1] for p in pts])
    return lambda t: V[max(np.searchsorted(T, t, "right") - 1, 0)]


def snaps(series, sym, s15):
    S = Spot(sym); bas = basis_fn(S, s15)
    mk = {m["ticker"]: m for m in json.load(open(os.path.join(D, f"{series}_markets.json")))}
    cd = json.load(open(os.path.join(D, f"{series}_candles.json")))
    _sel = set(json.load(open(os.path.join(D, f"{series}_sel.json"))))
    cd = {k: v for k, v in cd.items() if k in _sel}
    out = []
    for tk, cs in cd.items():
        m = mk[tk]; c = ts(m["close_time"]); y = m["result"] == "yes"
        if not (S.ok(c - 3600 - 1000) and S.ok(c + 1)): continue
        for t, bid, ask, last, vol in cs:
            tau = c - t
            if tau <= 0 or tau > 3600 or bid is None or ask is None: continue
            bid, ask = float(bid), float(ask)
            if ask >= 1 or bid <= 0: continue
            tl = t - LAG
            K = m["floor_strike"] * np.exp(bas(tl))       # strike in Binance units
            s2 = np.var(np.diff(np.log(S.p[tl - 1800 - S.t0:tl - S.t0:10]))) / 10
            St = S.at(tl - 1); tl_tau = tau + LAG
            if tl_tau >= 60:
                z = np.log(St / K) / np.sqrt(s2 * (tl_tau - 40))
            else:
                known = S.cs[tl - S.t0] - S.cs[c - 60 - S.t0]
                z = ((known + tl_tau * St) / 60 - K) / (St * np.sqrt(s2 * tl_tau ** 3 / 3) / 60)
            out.append((tau, bid, ask, np.clip(z, -8, 8), y, c, float(vol or 0)))
    return np.array(out, dtype=float)


for series, sym, s15 in [("KXBTCD", "BTCUSDT", "KXBTC15M"), ("KXETHD", "ETHUSDT", "KXETH15M")]:
    a = snaps(series, sym, s15)
    tau, bid, ask, z, y, c, vol = a.T
    mid = (bid + ask) / 2; lm = np.log(mid / (1 - mid)); p = Phi(z)
    split = np.quantile(np.unique(c), 0.5); tr = c < split; te = ~tr
    print(f"\n== {series} snapshots={len(a)} markets={len(np.unique(c))}hrs lag={LAG}s  median spread {np.median(ask-bid)*100:.1f}c")
    for lo, hi in [(1800, 3601), (600, 1800), (120, 600), (1, 120)]:
        k = (tau >= lo) & (tau < hi)
        X0 = np.c_[lm, np.ones_like(lm)]; X1 = np.c_[lm, z, np.ones_like(lm)]
        w0 = fit(X0[k & tr], y[k & tr]); w1 = fit(X1[k & tr], y[k & tr])
        d = ll(X0[k & te], w0, y[k & te]) - ll(X1[k & te], w1, y[k & te])
        lmid = -np.mean(np.where(y[k], np.log(mid[k]), np.log(1 - mid[k])))
        lmod = -np.mean(np.where(y[k], np.log(np.clip(p[k], 1e-4, 1)), np.log(np.clip(1 - p[k], 1e-4, 1))))
        print(f"  tau[{lo},{hi}) n={k.sum():6d} LL mid={lmid:.4f} model={lmod:.4f}  b_z={w1[1]:+.2f}  test dLL(mid+z vs mid)={d*1000:+.2f}m")
    print("  calibration of mid:")
    for lo, hi in [(0, .02), (.02, .05), (.05, .1), (.1, .3), (.3, .7), (.7, .9), (.9, .95), (.95, .98), (.98, 1)]:
        k = (mid >= lo) & (mid < hi)
        if k.sum(): print(f"    {lo:.2f}-{hi:.2f}: mid {mid[k].mean():.4f} realized {y[k].mean():.4f} n={k.sum()}")
    # taker sim, one entry per market at first qualifying minute
    for edge in [0.02, 0.05, 0.10]:
        res = []
        for name, sel in [("train", tr), ("test", te)]:
            pnl = []; seen = set()
            key = c * 1e7 + np.arange(len(c))  # placeholder
            for i in np.lexsort((-tau, c)):
                if not sel[i]: continue
                mkey = (c[i], round(float(a[i, 0]) // 1e9))
                if p[i] - ask[i] - fee(ask[i]) > edge:
                    pnl.append(y[i] - ask[i] - fee(ask[i]))
                elif bid[i] - p[i] - fee(bid[i]) > edge:
                    pnl.append((1 - y[i]) - (1 - bid[i]) - fee(1 - bid[i]))
            pnl = np.array(pnl)
            res.append(f"{name}: n={len(pnl)} avg={pnl.mean()*100 if len(pnl) else 0:+.2f}c")
        print(f"  every-minute taker, edge>{edge:.2f}: " + "  ".join(res))
