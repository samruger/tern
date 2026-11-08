"""Kalshi Trade API v2: public market data and signed portfolio/order calls.

Orders use the V2 endpoint, which quotes everything on the YES leg: side "bid" buys YES,
side "ask" sells YES (and, with no YES held, opens NO at 1 - price).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .sign import load_key, signature

BASE = "https://api.elections.kalshi.com"
PREFIX = "/trade-api/v2"
UA = "tern/1"


class KalshiError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Kalshi:
    def __init__(self, key_id: str | None = None, pem: str | None = None, base: str = BASE):
        self.base = base
        self.key_id = key_id
        self.key = load_key(pem) if pem else None
        self._last_429 = 0.0

    # ------------------------------------------------------------ transport
    def _req(self, method: str, path: str, query: dict | None = None, body: dict | None = None,
             signed: bool = False, timeout: float = 10.0, tries: int = 3):
        full = PREFIX + path
        url = self.base + full + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            headers = {"User-Agent": UA, "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if signed:
                if not self.key:
                    raise KalshiError("no API key loaded")
                ts = str(int(time.time() * 1000))
                headers.update({"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                                "KALSHI-ACCESS-SIGNATURE": signature(self.key, ts, method, full)})
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:300]
                if e.code == 429 and attempt + 1 < tries:
                    self._last_429 = time.time()
                    time.sleep(0.5 * 2 ** attempt); continue
                if e.code >= 500 and attempt + 1 < tries and method == "GET":
                    time.sleep(0.5 * 2 ** attempt); continue
                raise KalshiError(f"{method} {path}: HTTP {e.code} {text}", e.code)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt + 1 < tries and method == "GET":
                    time.sleep(0.5 * 2 ** attempt); continue
                raise KalshiError(f"{method} {path}: {e}")

    # ------------------------------------------------------------ public
    def open_events(self, series: str) -> list[dict]:
        d = self._req("GET", "/events", {"series_ticker": series, "status": "open", "limit": 200})
        return d.get("events", [])

    def event_markets(self, event: str) -> list[dict]:
        out, cur = [], None
        while True:
            q = {"event_ticker": event, "limit": 1000}
            if cur:
                q["cursor"] = cur
            d = self._req("GET", "/markets", q)
            out += d.get("markets", [])
            cur = d.get("cursor")
            if not cur or not d.get("markets"):
                return out

    def market(self, ticker: str) -> dict:
        return self._req("GET", f"/markets/{ticker}").get("market", {})

    # ------------------------------------------------------------ private
    def balance(self) -> float:
        d = self._req("GET", "/portfolio/balance", signed=True)
        if d.get("balance_dollars") is not None:
            return float(d["balance_dollars"])
        return float(d.get("balance", 0)) / 100.0

    def positions(self) -> dict[str, float]:
        """ticker -> signed contracts (+YES / -NO), unsettled only."""
        out, cur = {}, None
        while True:
            q = {"limit": 1000, "count_filter": "position", "settlement_status": "unsettled"}
            if cur:
                q["cursor"] = cur
            d = self._req("GET", "/portfolio/positions", q, signed=True)
            for p in d.get("market_positions", []):
                n = _num(p.get("position_fp"), _num(p.get("position"), 0.0))
                if n:
                    out[p["ticker"]] = n
            cur = d.get("cursor")
            if not cur or not d.get("market_positions"):
                return out

    def order(self, ticker: str, side: str, count: float, price: float, client_order_id: str,
              tif: str = "immediate_or_cancel") -> dict:
        """side 'bid' (buy YES) or 'ask' (sell YES / buy NO); price is the YES price in dollars."""
        body = {"ticker": ticker, "side": side, "count": f"{count:.2f}", "price": f"{price:.4f}",
                "time_in_force": tif, "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": client_order_id}
        return self._req("POST", "/portfolio/events/orders", body=body, signed=True, tries=1)

    def find_order(self, ticker: str, client_order_id: str) -> dict | None:
        d = self._req("GET", "/portfolio/orders", {"ticker": ticker, "limit": 200}, signed=True)
        for o in d.get("orders", []):
            if o.get("client_order_id") == client_order_id:
                return o
        return None

    def fills(self, min_ts: int) -> list[dict]:
        out, cur = [], None
        while True:
            q = {"min_ts": min_ts, "limit": 1000}
            if cur:
                q["cursor"] = cur
            d = self._req("GET", "/portfolio/fills", q, signed=True)
            out += d.get("fills", [])
            cur = d.get("cursor")
            if not cur or not d.get("fills"):
                return out


def quote(m: dict) -> tuple[float | None, float | None, float, float]:
    """(yes_bid, yes_ask, bid_size, ask_size) from a market dict; None when that side is empty."""
    bid = _num(m.get("yes_bid_dollars")); ask = _num(m.get("yes_ask_dollars"))
    bs = _num(m.get("yes_bid_size_fp"), 0.0); az = _num(m.get("yes_ask_size_fp"), 0.0)
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask >= 1:
        ask = None
    return bid, ask, bs or 0.0, az or 0.0
