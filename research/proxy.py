"""How well does Binance 1s reproduce Kalshi 15m results?"""
import sys
import numpy as np
from common import *

for series, sym in [("KXBTC15M", "BTCUSDT"), ("KXETH15M", "ETHUSDT")]:
    S = Spot(sym); mk, _ = load(series)
    rows = []
    for m in mk:
        if m.get("result") not in ("yes", "no") or m.get("expiration_value") in (None, ""): continue
        o, c = ts(m["open_time"]), ts(m["close_time"])
        if not (S.ok(o - 61) and S.ok(c + 1)): continue
        a0 = S.avg(o - 60, o); a1 = S.avg(c - 60, c)
        rows.append((m["floor_strike"], float(m["expiration_value"]), a0, a1, m["result"] == "yes"))
    r = np.array(rows, dtype=float)
    K, X, a0, a1, y = r.T
    pred = a1 >= a0
    basis0 = np.log(a0 / K); basis1 = np.log(a1 / X)
    move_k = np.log(X / K); move_b = np.log(a1 / a0)
    print(f"{series}: n={len(r)}  yes-rate={y.mean():.3f}")
    print(f"  Binance-predicted result matches Kalshi: {(pred == y).mean():.4f}")
    print(f"  Kalshi strike vs own settle agrees with result: {((X >= K) == y).mean():.4f}")
    print(f"  basis (Binance/BRTI) mean {basis0.mean()*1e4:.1f} bp, sd {basis0.std()*1e4:.2f} bp")
    print(f"  error in 15m move (Binance - BRTI): sd {(move_b - move_k).std()*1e4:.2f} bp; |move| median {np.median(abs(move_k))*1e4:.1f} bp")
    mis = pred != y
    print(f"  mismatches: |BRTI move| median {np.median(abs(move_k[mis]))*1e4:.2f} bp")
