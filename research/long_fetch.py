"""90-day hourly: list markets, ref price from Binance 1m monthly/daily klines, near-money candles per event."""
import json, os, sys, time, urllib.parse, zipfile, io, datetime as dt
from fetch import markets, get, B, D, ts
series, sym, days = sys.argv[1], sys.argv[2], 92
end = int(time.time()); start = end - days * 86400
# 1m klines: monthly files for full months, daily for current month
kl = {}
today = dt.date.today()
months = sorted({(today - dt.timedelta(days=k)).strftime("%Y-%m") for k in range(days + 1)})
for mo in months:
    url = f"https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{sym}-1m-{mo}.zip"
    try: blobs = [get(url)]
    except Exception:
        blobs = []
        for d in range(1, 32):
            day = f"{mo}-{d:02d}"
            try: blobs.append(get(f"https://data.binance.vision/data/spot/daily/klines/{sym}/1m/{sym}-1m-{day}.zip", tries=1))
            except Exception: pass
    for b in blobs:
        z = zipfile.ZipFile(io.BytesIO(b))
        for line in z.read(z.namelist()[0]).decode().splitlines():
            c = line.split(","); t = int(c[0]); t = t // 1_000_000 if t > 1e14 else t // 1000
            kl[t] = float(c[1])  # open price of minute starting at t
print(sym, "1m klines", len(kl), flush=True)
mp = os.path.join(D, f"{series}_markets90.json")
if os.path.exists(mp): mk = json.load(open(mp))
else:
    mk = [m for m in markets(series, start, end) if m.get("result") in ("yes", "no") and m.get("floor_strike")]
    mk = [{k: m[k] for k in ("ticker", "event_ticker", "close_time", "open_time", "result", "floor_strike", "volume_fp")} for m in mk]
    json.dump(mk, open(mp, "w"))
print(series, "markets", len(mk), flush=True)
ev = {}
for m in mk:
    c = ts(m["close_time"]); ref = kl.get(c - 3600)
    if ref and abs(m["floor_strike"] * 1.0013 / ref - 1) < 0.02:
        ev.setdefault(m["event_ticker"], []).append(m)
path = os.path.join(D, f"{series}_candles90.json")
done = json.load(open(path)) if os.path.exists(path) else {}
print(series, "events", len(ev), "markets", sum(map(len, ev.values())), flush=True)
for n, (e, ms) in enumerate(sorted(ev.items())):
    ms = [m for m in ms if m["ticker"] not in done]
    for i in range(0, len(ms), 20):
        g = ms[i:i+20]; c = ts(g[0]["close_time"])
        try:
            d = json.loads(get(f"{B}/markets/candlesticks?" + urllib.parse.urlencode(dict(
                market_tickers=",".join(m["ticker"] for m in g), start_ts=c - 3660, end_ts=c + 60, period_interval=1))))
        except Exception as ex:
            print("bad", e, ex, flush=True); continue
        for x in d["markets"]:
            done[x["market_ticker"]] = [[k["end_period_ts"], k["yes_bid"].get("close_dollars"), k["yes_ask"].get("close_dollars")]
                                        for k in x["candlesticks"]]
        time.sleep(0.08)
    if n % 200 == 0: json.dump(done, open(path, "w")); print(series, n, flush=True)
json.dump(done, open(path, "w")); print(series, "DONE", flush=True)
