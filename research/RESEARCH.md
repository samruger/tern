# Research: Kalshi's short crypto markets

Done 2026-11-08 on Kalshi's public API, with Binance 1-second prices as the spot
proxy. Every profit figure below is after Kalshi's taker fee (0.07 × P × (1−P)
per contract) and pays the spread. Result in one line: **the market prices spot
moves very quickly, and long shots on the hourly markets are overpriced: buying the
favorite made about half a cent a contract.**

## Data

| | |
|---|---|
| 15-minute up/down (KXBTC15M, KXETH15M) | 28 days, ~2,600 settled markets each, minute bid/ask |
| Final 2 minutes of those | 2.7M individual trades |
| Hourly above/below (KXBTCD, KXETHD) | 14 days near-money (both coins); 67 days BTC (all Kalshi keeps) |
| Spot | Binance 1-second klines (data.binance.vision) |

## Findings, in order

1. **Binance stands in for the settlement index.** Rebuilding each 15-minute
   result from Binance 60-second averages matches Kalshi 97.7% (BTC) and 98.6%
   (ETH); every miss was a move under half a basis point. (`proxy.py`)
2. **Kalshi does not lag spot at minute resolution.** A fair-value model from
   spot, time left and realized volatility predicts outcomes worse than Kalshi's
   own mid at every horizon. Added to the mid in a regression fitted on the first
   half, it improves held-out log-loss by under 0.002 nats, even with zero-second
   stale data. Any lag is sub-second. (`model.py`, `resid.py`, `hourly.py`)
3. **Taking when the model disagrees loses.** 15-minute markets: flips sign
   between halves. Hourly: −1 to −4¢ a contract.
4. **The final minute is not a gift.** Trading on the half-known settlement
   average looked good on the first half of trades (+0.6 to +4.5¢) and lost on
   the second (−0.1 to −3.6¢). Makers across all final-minute trades roughly
   break even (−0.29¢ BTC, +0.10¢ ETH). (`tail.py`)
5. **Hourly long shots are overpriced.** 67 days of BTC, errors clustered by hour:

   | mid | realized − mid |
   |---|---|
   | 5–10¢ | −2.0 ± 0.5 pt |
   | 10–20¢ | −2.2 ± 0.7 pt |
   | 80–90¢ | +2.3 ± 0.7 pt |
   | 90–95¢ | +1.3 ± 0.5 pt |

   The same shape shows in ETH over 14 days.
6. **Turning it into trades.** The rule fixed before the 67-day run
   (buy the favorite at the ask when its mid is 80–98¢, 50/30/15 minutes out):
   **+0.51¢/contract, t = 1.85**, positive in each third, 6 of 10 weeks up.
   (`fav90.py`)
7. **Resting bids instead lose.** A post-only bid on the favorite, filled only
   when the ask comes down to it, loses 1–2¢: fills come when price is moving
   against you. (`maker90.py`)
8. **Refined on the first half, checked on the second.** 126 variants of band,
   timing and spread were ranked on the first 33 days. The best (favorite mid
   75–95¢, at 25/20/15/10 minutes, spread ≤ 2¢) made +1.79¢ (t = 3.2) there and
   **+0.61¢ (t = 1.1) on the untouched second half**; the pre-registered rule
   made +0.25¢ there. 71% of all variants were positive on the second half.
   (`refine90.py`)

## What that means

The refined rule took about 10
entries an hour in the backtest; at 5 contracts each and +0.6¢ a contract that is
roughly 30¢ an hour, or about $7 a day, while one failed favorite loses about $4. tern runs it on paper
and lets it trade live only after a paper record passes the gate.

ETH was not run on 67 days. That download needed more memory than the machine had
free.

## Reproducing

The scripts use numpy and pandas: `uv run --with numpy --with pandas python <script>`.
Data goes to `research/data/` (gitignored, about 1 GB). Order: `fetch.py`
(15-minute markets + Binance), `trades.py`, `hourly_fetch.py`, `hourly_fetch2.py`,
`long_fetch.py`, then the analyses named above. The candlestick and trade
endpoints rate-limit hard; run at most two downloaders at once.
