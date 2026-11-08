"""Download settled Kalshi crypto markets + 1-min candles, and Binance 1s klines."""
import json, os, sys, time, urllib.request, urllib.parse, zipfile, io, datetime as dt

B = "https://api.elections.kalshi.com/trade-api/v2"
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(D, exist_ok=True)
UA = {"User-Agent": "research-script"}


def get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(1.5 * (i + 1)); continue
            raise
        except Exception:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(url)


def markets(series, min_close, max_close):
    out, cur = [], None
    while True:
        q = dict(series_ticker=series, status="settled", limit=1000,
                 min_close_ts=min_close, max_close_ts=max_close)
        if cur: q["cursor"] = cur
        d = json.loads(get(f"{B}/markets?" + urllib.parse.urlencode(q)))
        out += d["markets"]
        cur = d.get("cursor")
        if not cur or not d["markets"]: break
        time.sleep(0.1)
    return out


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def candles(series, mk, path):
    """mk: list of markets. Batch by close time windows."""
    done = {}
    if os.path.exists(path):
        done = json.load(open(path))
    todo = [m for m in mk if m["ticker"] not in done]
    # group into batches of tickers with similar windows
    todo.sort(key=lambda m: m["close_time"])
    n = 0
    for i in range(0, len(todo), 20):
        g = todo[i:i + 20]
        s = min(ts(m["open_time"]) for m in g) - 60
        e = max(ts(m["close_time"]) for m in g) + 60
        url = f"{B}/markets/candlesticks?" + urllib.parse.urlencode(dict(
            market_tickers=",".join(m["ticker"] for m in g), start_ts=s, end_ts=e, period_interval=1))
        d = json.loads(get(url))
        for x in d["markets"]:
            done[x["market_ticker"]] = [
                [c["end_period_ts"],
                 c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars"),
                 c["price"].get("close_dollars"), c.get("volume_fp")]
                for c in x["candlesticks"]]
        n += 1
        if n % 25 == 0:
            json.dump(done, open(path, "w")); print(series, len(done), flush=True)
        time.sleep(0.08)
    json.dump(done, open(path, "w"))
    return done


def binance(sym, day):
    p = os.path.join(D, f"{sym}-1s-{day}.csv")
    if os.path.exists(p): return p
    url = f"https://data.binance.vision/data/spot/daily/klines/{sym}/1s/{sym}-1s-{day}.zip"
    z = zipfile.ZipFile(io.BytesIO(get(url)))
    raw = z.read(z.namelist()[0]).decode()
    with open(p, "w") as f:  # keep open_time, close
        for line in raw.splitlines():
            c = line.split(",")
            f.write(f"{c[0]},{c[4]}\n")
    return p


if __name__ == "__main__":
    what = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 28
    end = int(time.time()); start = end - days * 86400
    if what == "kalshi":
        for series in sys.argv[3:]:
            mp = os.path.join(D, f"{series}_markets.json")
            if os.path.exists(mp):
                mk = json.load(open(mp))
            else:
                mk = markets(series, start, end)
                json.dump(mk, open(mp, "w"))
            print(series, "markets", len(mk), flush=True)
            candles(series, mk, os.path.join(D, f"{series}_candles.json"))
    elif what == "binance":
        today = dt.date.today()
        for sym in sys.argv[3:]:
            for k in range(days + 1, 0, -1):
                day = (today - dt.timedelta(days=k)).isoformat()
                try:
                    binance(sym, day)
                except Exception as e:
                    print("miss", sym, day, e)
            print(sym, "done", flush=True)
