import json, os, sys, time, urllib.parse
from fetch import markets, get, B, D, ts
series = sys.argv[1]; days = 14
end = int(time.time())
mp = os.path.join(D, f"{series}_markets.json")
if os.path.exists(mp): mk = json.load(open(mp))
else:
    mk = markets(series, end - days*86400, end); json.dump(mk, open(mp, "w"))
mk = [m for m in mk if m.get("result") in ("yes","no") and m.get("expiration_value") not in (None,"")
      and m.get("floor_strike") and abs(m["floor_strike"]/float(m["expiration_value"]) - 1) < 0.015]
print(series, "near-money markets", len(mk), flush=True)
path = os.path.join(D, f"{series}_candles.json")
done = json.load(open(path)) if os.path.exists(path) else {}
todo = sorted([m for m in mk if m["ticker"] not in done], key=lambda m: m["close_time"])
for i in range(0, len(todo), 20):
    g = todo[i:i+20]
    e = max(ts(m["close_time"]) for m in g) + 60; s = min(ts(m["close_time"]) for m in g) - 3660
    d = json.loads(get(f"{B}/markets/candlesticks?" + urllib.parse.urlencode(dict(
        market_tickers=",".join(m["ticker"] for m in g), start_ts=s, end_ts=e, period_interval=1))))
    for x in d["markets"]:
        done[x["market_ticker"]] = [[c["end_period_ts"], c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars"),
            c["price"].get("close_dollars"), c.get("volume_fp")] for c in x["candlesticks"]]
    if i % 1000 == 0: json.dump(done, open(path, "w")); print(series, i, flush=True)
    time.sleep(0.08)
json.dump(done, open(path, "w")); print(series, "DONE", flush=True)
