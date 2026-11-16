# tern

A desk for Kalshi's hourly BTC and ETH above-or-below markets, with a btop-style
screen: braille charts, a strike ladder, and paper and live books.
Every trade comes from the rule; there are no manual orders.
It paper trades from the start and sends no real order until you arm live.

Python 3.11+, standard library only. Kalshi request signing (Ed25519 and RSA-PSS)
is written in pure Python and checked against openssl in the tests.

```
python3 -m tern               # the screen; starts the engine in paper if it isn't running
python3 -m tern run           # the engine in the foreground
python3 -m tern status        # plain-text summary
python3 -m tern report        # the rule's variants side by side
python3 -m tern live-check    # test your Kalshi key (balance, positions; no orders)
python3 -m tern service       # a systemd user service so the engine keeps running
python3 -m unittest tests.test_tern
```

## Is there an edge?

Barely. The full write-up is [research/RESEARCH.md](research/RESEARCH.md). In short:

- **Kalshi tracks spot faster than a home bot can.** A fair-value model from
  live spot adds nothing to Kalshi's own price on held-out data, even fed
  zero-delay prices. The 15-minute markets and the final-minute settlement math
  showed no edge that held up out of sample.
- **Hourly long shots are overpriced.** On 67 days of BTC history, contracts
  priced 5–20¢ won about 2 points less often than their price said, and 80–90¢
  favorites about 2 points more often.
- **Buying the favorite at the ask captures a little of it.** The rule tern
  runs was picked on the first 33 days (+1.8¢ a contract) and made **+0.6¢ a
  contract on the next 34 days it had never seen (t = 1.1)**. That is positive
  but not proven. Expect about 10 entries an hour, a few dollars a day at the
  default size, and single losses of about $4 when a favorite fails.
- **Resting bids lose.** You get filled when price moves against you.

## The rule

At **25, 20, 15 and 10 minutes before each hourly close**, for every strike whose
favorite side (YES or NO) has its mid between **75¢ and 95¢** and a spread of
**2¢ or less**, it buys **5 contracts** of the favorite at the ask
(immediate-or-cancel) and holds to settlement. It puts at most **$60** into any
one hour. All of this is under `strategy` in `runtime/config.json`, re-read
every 5 seconds. It trades BTC only by default; `r` on the ETH view adds ETH,
which was only tested on 14 days.

## Paper and live

Paper fills at the touch, limited to the size shown there, and pays the taker
fee rounded up per order, as Kalshi charges. Paper always runs the rule, even
while live is on, so its record keeps growing.

**Going live:** press `L` and type `live`. That sets `live.enabled` in the
config. The engine then authenticates with your key, and the header turns red
once it has. Press `L` again to return to paper. Once live:

- **The rule goes live only after its paper record passes the gate** (`g`):
  14 days, 300 settled entries, and P&L ≥ $0 after fees. Days are counted as
  hours the rule actually traded ÷ 24, so time with the engine off does not
  count. A 0.6¢ edge is too
  small for 14 days to prove. The gate catches a broken rule, not a weak one.
  Set `live.auto_needs_gate` to `false` to skip it.
- **Caps:** `live.capital_usd` ($100) is the most at risk at once.
  `live.max_order_usd` ($25) is the largest single order. Orders that only
  reduce a position are exempt from both.
- **Kill switch:** a live loss of `live.max_daily_loss` ($30) since local
  midnight turns live off. Open positions are left to settle. Every order is
  immediate-or-cancel, so nothing is left resting on the book.
- **Kalshi is the record.** Every 15 seconds the engine reads your positions
  and overwrites its own when they differ. It reads the balance every 10
  seconds. Each order carries a unique `client_order_id`. If the reply is lost,
  the engine looks the order up by that id instead of sending it again.

## Looking for a better edge

The engine also runs these on paper, side by side, each with its own record
and real fees. They were written down before seeing any data, so what they
show after a few weeks is out of sample. `python3 -m tern report` (or `g` on
the screen) compares them. None of them touches the paper or live book.

| Variant | What it does |
|---|---|
| `base` | the rule as configured |
| `band90` | only favorites at 90–95¢ |
| `stop20` | the rule, but sells at the bid once the favorite's bid is 20¢ under the entry price |
| `arb` | when a higher strike's YES bid is above a lower strike's YES ask by more than both fees, buys YES on the lower and NO on the higher. The pair pays at least $1 |

It also records what it sees to `runtime/ticks/`: every 5 seconds, each
near-the-money strike's bid, ask and sizes with spot, plus each hour's results
(about 2 MB a day, gzipped). Kalshi keeps only minute candles, so this is the
data for testing ideas the first research could not.

## Keys

At kalshi.com, under Account → API keys, create a key and download the private
key file. Ed25519 is Kalshi's default; RSA works too. Then:

```
mkdir -p ~/.config/tern && chmod 700 ~/.config/tern
cat > ~/.config/tern/kalshi.env <<EOF
KALSHI_KEY_ID=your-key-id
KALSHI_KEY_FILE=~/.config/tern/kalshi.pem
EOF
chmod 600 ~/.config/tern/kalshi.env ~/.config/tern/kalshi.pem
python3 -m tern live-check
```

## The screen

| Box | What it shows |
|---|---|
| header | paper/live, spot and 1-hour change, the event and its countdown, the rule's next slot, engine health |
| ¹ spot | Coinbase spot (a BRTI constituent), 15 minutes at 1 s or 24 hours at 1 min (`c`), with the selected strike as a dotted line |
| ² contract | the selected strike's YES price this hour, seeded from Kalshi's minute candles, with the rule's band edges marked |
| ³ ladder | every strike, high to low: bid, ask, probability bar, spread, volume, your position. ★ marks strikes in the rule's band |
| ⁴ book | equity, P&L, cash, at risk, fees, the rule's paper record, and open positions. `v` switches between paper and live |
| ⁵ equity | that book's equity by the minute |
| log | what the engine did |

Keys: `↑↓` / `PgUp PgDn` move in the ladder · `1`–`6` show/hide a box · `tab` BTC/ETH · `r` rule trades this coin ·
`c` chart range · `v` book · `L` arm/disarm live · `g` gate · `R` reset paper ·
`s` start the engine · `?` help · `q` close (the engine keeps running).

## Files

`runtime/` (gitignored) holds `config.json`, `state.json` (books, the rule's
record, slots taken; written only when it changes, with an hourly
`state.json.bak` that is loaded if the main file is damaged), `tern.log` and
`cmd/` (settings commands from the screen). The live
status file and the screen's selection live in tmpfs (`$XDG_RUNTIME_DIR/tern-*`),
because they are rewritten every second.

| Module | What it does |
|---|---|
| `strategy.py` | the favorite rule: which strikes qualify, when a slot is due |
| `book.py` | the ledger for paper and live, with Kalshi's YES-leg netting and fee |
| `engine.py` | polling, the rule, orders, settlement, live reconcile, kill switch, status |
| `kalshi.py` | public and signed REST, V2 orders (`/portfolio/events/orders`) |
| `sign.py` | Ed25519 and RSA-PSS signing, PEM/DER parsing |
| `spot.py` | Coinbase ticker and candles |
| `shadow.py` | the variants and the arbitrage scan, each with its own paper record |
| `record.py` | writes quotes and results to `runtime/ticks/` |
| `dash.py` | the curses screen |
| `research/` | the scripts and data behind the numbers above |

## Limits

- The edge is small and unproven. Run paper for the full gate period before
  trusting it with money.
- Live order handling follows Kalshi's V2 docs, but no real order has been sent.
  It has only been checked with a made-up key, which Kalshi's auth rejected as
  expected. Start live with small caps and watch the first fills against
  kalshi.com.
- If the engine isn't running, nothing is watched. Positions still settle on
  Kalshi, because nothing rests on the book.
