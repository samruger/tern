"""Fetch trades: last WIN seconds of every market, or full history for a random sample."""
import json, os, sys, time, random, urllib.parse
from fetch import get, B, D, ts
series, mode = sys.argv[1], sys.argv[2]   # mode: tail | full
mk = [m for m in json.load(open(os.path.join(D, f"{series}_markets.json"))) if m.get("result") in ("yes","no")]
if mode == "full":
    random.seed(7); mk = random.sample(mk, 150); WIN = 10**9
else:
    WIN = 120
path = os.path.join(D, f"{series}_trades_{mode}.json")
done = json.load(open(path)) if os.path.exists(path) else {}
for n, m in enumerate(mk):
    if m["ticker"] in done: continue
    c = ts(m["close_time"]); o = ts(m["open_time"])
    rows, cur = [], None
    while True:
        q = dict(ticker=m["ticker"], limit=1000, min_ts=max(o - 5, c - WIN), max_ts=c + 5)
        if cur: q["cursor"] = cur
        d = json.loads(get(f"{B}/markets/trades?" + urllib.parse.urlencode(q)))
        for t in d["trades"]:
            rows.append([t["created_time"], t["taker_side"], float(t["yes_price_dollars"]), float(t["count_fp"])])
        cur = d.get("cursor")
        if not cur or not d["trades"]: break
        time.sleep(0.05)
    done[m["ticker"]] = rows
    if n % 100 == 0:
        json.dump(done, open(path, "w")); print(series, mode, n, len(rows), flush=True)
    time.sleep(0.05)
json.dump(done, open(path, "w")); print(series, mode, "DONE", flush=True)
