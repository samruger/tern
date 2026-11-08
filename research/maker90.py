"""Maker version of the favorite edge on hourly BTC: rest a bid on the favorite, filled only if the
ask later comes down to our price (pessimistic: we are last in queue). No maker fee on KXBTCD."""
import json, os, sys
import numpy as np
from common import *

series = sys.argv[1] if len(sys.argv) > 1 else "KXBTCD"
mk = {m["ticker"]: m for m in json.load(open(os.path.join(D, f"{series}_markets90.json")))}
cd = json.load(open(os.path.join(D, f"{series}_candles90.json")))


def run(lo, hi, post_taus, improve, cancel_tau, through):
    per = {}
    for tk, cs in cd.items():
        m = mk[tk]; c = ts(m["close_time"]); y = m["result"] == "yes"
        q = sorted((c - t, float(b), float(a)) for t, b, a in cs if b is not None and a is not None)[::-1]  # tau desc
        qd = {x[0]: x for x in q}
        for T in post_taus:
            if T not in qd: continue
            _, b, a = qd[T]
            if a >= 1 or b <= 0 or a - b < 0.005: continue
            mid = (a + b) / 2
            if lo <= mid < hi: side = 1; px = min(b + improve, a - 0.01)          # bid YES
            elif lo <= 1 - mid < hi: side = 0; px = min((1 - a) + improve, (1 - b) - 0.01)  # bid NO
            else: continue
            filled = False
            for tau, b2, a2 in q:
                if tau >= T or tau < cancel_tau: continue
                # our YES bid at px fills if YES ask trades down to px - through; NO bid fills if YES bid rises to 1-px+through
                if side == 1 and a2 <= px - through: filled = True; break
                if side == 0 and b2 >= 1 - px + through: filled = True; break
            if not filled: continue
            win = y if side == 1 else not y
            per.setdefault(c, []).append(float(win) - px)
    hrs = np.array(sorted(per)); v = np.array([np.mean(per[h]) for h in hrs]); n = sum(map(len, per.values()))
    t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
    th = np.array_split(v, 3)
    wk = (hrs - hrs[0]) // (7 * 86400); weekly = np.array([v[wk == w].mean() for w in np.unique(wk)])
    print(f"  fav[{lo},{hi}) post@{post_taus} improve={improve:.2f} cancel<{cancel_tau}s through={through:.2f}: "
          f"fills={n} hours={len(hrs)} avg={v.mean()*100:+.2f}c t={t:+.2f} thirds={' '.join(f'{x.mean()*100:+.2f}' for x in th)} +weeks {np.sum(weekly>0)}/{len(weekly)}")


print(series)
run(0.80, 0.98, (3000, 1800, 900), 0.00, 300, 0.00)   # primary maker spec (fixed in advance)
run(0.80, 0.98, (3000, 1800, 900), 0.00, 300, 0.01)   # stricter: must trade 1c through
run(0.80, 0.98, (3000, 1800, 900), 0.01, 300, 0.00)   # join one tick better
run(0.70, 0.95, (3000, 1800, 900), 0.00, 300, 0.00)
run(0.80, 0.98, (3000, 2400, 1800, 1200, 900), 0.00, 300, 0.00)
