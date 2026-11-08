"""Fetch candles for strikes within 2% of the BRTI-equivalent price at the START of the hour (no lookahead)."""
import json, os, sys, time, urllib.parse
import numpy as np
from fetch import get, B, D, ts
from common import Spot
series, sym = sys.argv[1], sys.argv[2]
S = Spot(sym)
mk = [m for m in json.load(open(os.path.join(D, f"{series}_markets.json"))) if m.get("result") in ("yes","no") and m.get("floor_strike")]
path = os.path.join(D, f"{series}_candles.json")
done = json.load(open(path))
sel = []
for m in mk:
    c = ts(m["close_time"])
    if not S.ok(c - 3600): continue
    ref = S.at(c - 3600) / np.exp(13e-4)   # approx basis; 2% band is wide enough
    if abs(m["floor_strike"] / ref - 1) < 0.02: sel.append(m)
json.dump([m["ticker"] for m in sel], open(os.path.join(D, f"{series}_sel.json"), "w"))
todo = sorted([m for m in sel if m["ticker"] not in done], key=lambda m: m["close_time"])
print(series, "selected", len(sel), "to fetch", len(todo), flush=True)
for i in range(0, len(todo), 20):
    g = todo[i:i+20]
    e = max(ts(m["close_time"]) for m in g) + 60; s = min(ts(m["close_time"]) for m in g) - 3660
    try:
        d = json.loads(get(f"{B}/markets/candlesticks?" + urllib.parse.urlencode(dict(
            market_tickers=",".join(m["ticker"] for m in g), start_ts=s, end_ts=e, period_interval=1))))
    except Exception as ex:
        print("bad group", g[0]["ticker"], g[-1]["ticker"], s, e, ex, flush=True)
        for m in g: done[m["ticker"]] = []
        continue
    for x in d["markets"]:
        done[x["market_ticker"]] = [[c["end_period_ts"], c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars"),
            c["price"].get("close_dollars"), c.get("volume_fp")] for c in x["candlesticks"]]
    if i % 1000 == 0: json.dump(done, open(path, "w")); print(series, i, flush=True)
    time.sleep(0.1)
json.dump(done, open(path, "w")); print(series, "DONE", flush=True)
