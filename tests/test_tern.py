import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tern.book import Book, taker_fee
from tern.sign import load_key, signature
from tern.strategy import due_slot, signals
from tern.dash import braille, by_time

# Every engine keeps its status and view files in $XDG_RUNTIME_DIR/tern-<hash of its runtime>. Each test has a
# fresh temp runtime, so without this every run would leave dozens of folders there for good (until reboot).
_RUN = {}


def setUpModule():
    _RUN["old"] = os.environ.get("XDG_RUNTIME_DIR")
    _RUN["dir"] = tempfile.mkdtemp(prefix="tern-tests-")
    os.environ["XDG_RUNTIME_DIR"] = _RUN["dir"]


def tearDownModule():
    if _RUN["old"] is None:
        os.environ.pop("XDG_RUNTIME_DIR", None)
    else:
        os.environ["XDG_RUNTIME_DIR"] = _RUN["old"]
    shutil.rmtree(_RUN["dir"], ignore_errors=True)


class Fees(unittest.TestCase):
    def test_kalshi_examples(self):
        self.assertEqual(taker_fee(100, 0.50), 1.75)      # Kalshi's own example
        self.assertEqual(taker_fee(1, 0.50), 0.02)        # 1.75c rounds up
        self.assertEqual(taker_fee(10, 0.90), 0.07)       # 6.3c rounds up
        self.assertEqual(taker_fee(0, 0.5), 0.0)


class Ledger(unittest.TestCase):
    def test_yes_round_trip_and_settle(self):
        b = Book("t", 100.0)
        b.trade("A", 10, 0.80, 0.12, {}, "x")
        self.assertAlmostEqual(b.cash, 100 - 8 - 0.12)
        self.assertAlmostEqual(b.at_risk(), 8.0)
        b.settle("A", True)
        self.assertAlmostEqual(b.cash, 100 - 8 - 0.12 + 10)
        self.assertAlmostEqual(b.realized, 2.0 - 0.12)
        self.assertNotIn("A", b.pos)

    def test_no_side_via_selling_yes(self):
        b = Book("t", 100.0)
        b.trade("A", -5, 0.20, 0.0, {}, "x")      # sell YES at 20c with nothing held = buy NO at 80c
        self.assertAlmostEqual(b.pos["A"]["q"], -5)
        self.assertAlmostEqual(b.pos["A"]["avg"], 0.80)
        self.assertAlmostEqual(b.cash, 96.0)
        b.settle("A", False)                      # NO wins
        self.assertAlmostEqual(b.cash, 101.0)
        self.assertAlmostEqual(b.realized, 1.0)

    def test_netting_closes_first(self):
        b = Book("t", 100.0)
        b.trade("A", -5, 0.20, 0.0, {}, "x")      # NO 5 @ 80c
        b.trade("A", 8, 0.30, 0.0, {}, "x")       # buy 8 YES @ 30c: closes 5 NO at 70c, opens 3 YES
        self.assertAlmostEqual(b.pos["A"]["q"], 3)
        self.assertAlmostEqual(b.pos["A"]["avg"], 0.30)
        self.assertAlmostEqual(b.realized, 5 * (0.70 - 0.80))
        self.assertAlmostEqual(b.cash, 100 - 4 + 3.5 - 0.9)

    def test_equity_marks(self):
        b = Book("t", 100.0)
        b.trade("A", 10, 0.50, 0.0, {}, "x")
        b.trade("B", -10, 0.50, 0.0, {}, "x")
        self.assertAlmostEqual(b.equity({"A": 0.6, "B": 0.6}), 90 + 6 + 4)


class Rule(unittest.TestCase):
    def m(self, t, bid, ask, k=1.0):
        return {"ticker": t, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
                "yes_bid_size_fp": "50", "yes_ask_size_fp": "40", "floor_strike": k, "status": "active"}

    def test_band_and_sides(self):
        ms = [self.m("yes_fav", 0.85, 0.86), self.m("no_fav", 0.10, 0.11), self.m("coin", 0.50, 0.51),
              self.m("wide", 0.80, 0.85), self.m("lock", 0.97, 0.98)]
        s = {x["ticker"]: x for x in signals(ms, 0.75, 0.95, 0.02)}
        self.assertEqual(set(s), {"yes_fav", "no_fav"})
        self.assertEqual((s["yes_fav"]["delta_sign"], s["yes_fav"]["price"]), (1, 0.86))
        self.assertEqual((s["no_fav"]["delta_sign"], s["no_fav"]["price"]), (-1, 0.10))   # sell YES at the bid

    def test_slots(self):
        self.assertEqual(due_slot(25 * 60 - 3, [25, 20], set()), 25)
        self.assertIsNone(due_slot(25 * 60 + 3, [25, 20], set()))
        self.assertIsNone(due_slot(25 * 60 - 3, [25, 20], {25}))
        self.assertIsNone(due_slot(25 * 60 - 120, [25, 20], set()))       # missed slots are not chased


class State(unittest.TestCase):
    def desk(self, d):
        from tern.engine import Desk
        return Desk(d)

    def test_prunes_meta_and_skips_unchanged_saves(self):
        import json, time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            now = time.time()
            k.events["KXBTCD"] = {"event": "E-1", "close": now + 600, "markets": [{"ticker": "E-1-T1"}], "fetched": now}
            k.ev_close["E-1"] = now + 600
            k.meta = {"E-1-T1": {}, "OLD-T1": {}, "HELD-T1": {"close": now - 60}, "OWED-T1": {}}
            k.paper.trade("HELD-T1", 1, 0.5, 0.0, {}, "x")
            k.auto = [{"ts": now, "ticker": "OWED-T1", "side": "yes", "n": 1, "cost": 0.5, "fee": 0, "slot": 10, "pnl": None}]
            k.slots = {"paper|E-1": {25}, "paper|GONE": {25}}
            k.hour_cost = {"paper|E-1": 1.0, "paper|GONE": 1.0}
            k._save_state()
            self.assertEqual(set(k.meta), {"E-1-T1", "HELD-T1", "OWED-T1"})
            self.assertEqual(set(k.slots), {"paper|E-1"})
            self.assertEqual(set(k.hour_cost), {"paper|E-1"})
            p = os.path.join(d, "state.json")
            self.assertTrue(os.path.exists(p + ".bak"))
            os.remove(p)
            k._save_state()                                   # unchanged: not rewritten
            self.assertFalse(os.path.exists(p))

    def test_damaged_state_falls_back_to_backup_and_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.paper.trade("A-T1", 2, 0.5, 0.0, {}, "x")
            k._save_state()
            p = os.path.join(d, "state.json")
            open(p, "w").write('{"paper": {"na')               # cut off mid-write
            k2 = self.desk(d)
            self.assertAlmostEqual(k2.paper.pos["A-T1"]["q"], 2)
            self.assertTrue(any(".damaged-" in f for f in os.listdir(d)))

    def test_gate_days_count_hours_traded(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.shadow.agg = {"base": {f"KXBTCD-H{h}": [0.1, 3, 3, 3, 0] for h in range(12)}}
            self.assertIn("0.5 days", k.gate()["checks"][0]["detail"])   # 12 hours, however long ago they started

    def test_unreadable_config_keeps_the_one_in_use(self):
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            cur = config.Config(); cur.strategy.contracts = 9
            open(os.path.join(d, "config.json"), "w").write("{bad")
            self.assertEqual(config.load(d, cur).strategy.contracts, 9)


class Shutdown(unittest.TestCase):
    def setUp(self):
        import signal
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])

    def desk(self, d):
        from tern.engine import Desk
        k = Desk(d)
        k.spot.start = lambda: None
        return k

    def test_stop_signal_lets_the_step_finish_then_saves(self):
        import signal
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            seen = []
            def step():
                os.kill(os.getpid(), signal.SIGTERM)          # a stop lands mid-step
                os.kill(os.getpid(), signal.SIGINT)
                k.paper.trade("A-T1", 2, 0.5, 0.0, {}, "x")   # the rest of the step still runs
                seen.append(1)
            k.step = step
            k.run()
            self.assertEqual(seen, [1])                       # finished once, no second step
            self.assertAlmostEqual(self.desk(d).paper.pos["A-T1"]["q"], 2)
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)   # a second stop cannot cut the saves

    def test_one_failed_save_does_not_skip_the_others(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            done = []
            def boom():
                raise OSError("disk full")
            k._save_state = boom
            k.shadow.save = lambda: done.append("shadow")
            k.rec.flush = lambda: done.append("quotes")
            k._shutdown()
            self.assertEqual(done, ["shadow", "quotes"])
            self.assertIn("could not save: state", k.logs[-1][2])

    def test_failed_flush_does_not_repeat_written_files(self):
        import gzip
        from tern.record import Recorder
        with tempfile.TemporaryDirectory() as d:
            r = Recorder(d)
            r.buf = {"a.csv.gz": ["1\n"], "missing/b.csv.gz": ["2\n"]}
            with self.assertRaises(OSError):
                r.flush()
            self.assertEqual(list(r.buf), ["missing/b.csv.gz"])
            del r.buf["missing/b.csv.gz"]
            r.flush()
            self.assertEqual(gzip.open(os.path.join(r.dir, "a.csv.gz"), "rt").read().count("1\n"), 1)

    def test_unit_quotes_paths_and_waits_for_the_runtime(self):
        from tern.__main__ import _unit
        u = _unit("/run/media/d/My Card/tern", "/run/media/d/My Card/tern/runtime", "/usr/bin/python3")
        self.assertIn('Environment="TERN_RUNTIME=/run/media/d/My Card/tern/runtime"', u)
        self.assertIn('ExecStartPre=/usr/bin/test -d "/run/media/d/My Card/tern/runtime"', u)
        self.assertIn('ExecStart="/usr/bin/python3" -m tern run', u)


class Commands(unittest.TestCase):
    T = "KXBTCD-E1-T100"

    def desk(self, d, bid="0.58", ask="0.60", age=0.0, close=600.0):
        import time
        from tern.engine import Desk
        k = Desk(d)
        now = time.time()
        m = {"ticker": self.T, "floor_strike": 100.0, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
             "yes_bid_size_fp": "50", "yes_ask_size_fp": "50"}
        k.events["KXBTCD"] = {"event": "KXBTCD-E1", "title": "", "close": now + close, "markets": [m], "fetched": now - age}
        k.meta[self.T] = {"event": "KXBTCD-E1", "strike": 100.0, "close": now + close, "series": "KXBTCD"}
        return k

    def series(self, **kw):
        return {"op": "series", "series": "KXETHD", **kw}

    def test_manual_orders_are_gone(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("unknown command", k._command({"op": "order", "ticker": self.T, "action": "buy_yes", "n": 5}))
            self.assertFalse(k.paper.pos or k.live.pos)

    def test_old_command_files_are_ignored(self):
        import json, time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for name, c in ((f"{time.time() - 3600:.6f}-old.json", self.series(id="old")),
                            ("x-stamped.json", self.series(id="stamped", ts=time.time() - 3600)),
                            (f"{time.time():.6f}-new.json", self.series(id="new", ts=time.time()))):
                with open(os.path.join(k.cmd_dir, name), "w") as fh:
                    json.dump(c, fh)
            k._commands()
            self.assertIn("too old", k.results["old"])
            self.assertIn("too old", k.results["stamped"])
            self.assertIn("rule trades: KXBTCD, KXETHD", k.results["new"])
            self.assertEqual(os.listdir(k.cmd_dir), [])

    def test_strikes_and_prices_read_right_for_every_coin(self):
        from tern.dash import strike_of, px
        for k, want in ((66799.99, 66800), (74.4999, 74.5), (74.7499, 74.75), (0.8599, 0.86), (604.99, 605), (70000, 70000)):
            self.assertAlmostEqual(strike_of(k), want, places=6)
        self.assertEqual([px(66800), px(604.99), px(74.75), px(2.5), px(0.86)], ["66,800", "604.99", "74.75", "2.500", "0.8600"])

    def test_unknown_series_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("unknown series", k._command({"op": "series", "series": "KXFAKED"}))
            self.assertEqual(k.cfg.strategy.series, ["KXBTCD"])
            k.cfg.strategy.series = ["KXFAKED"]; k.view = {"series": "KXFAKED", "ticker": None}
            k.last["cfg"] = __import__("time").time()
            k._read_view = lambda: None
            for name in ("_live_ready", "_commands", "_auto", "_shadow", "_settle", "_status"):
                setattr(k, name, lambda *a: None)
            k.step()                                          # no KeyError, no network
            self.assertTrue(any("unknown series KXFAKED" in m for _, _, m in k.logs))

    def test_reset_paper_clears_the_hour_budget(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.hour_cost = {"paper|KXBTCD-E1": 60.0, "live|KXBTCD-E1": 10.0}
            k._command({"op": "reset_paper"})
            self.assertEqual(k.hour_cost, {"live|KXBTCD-E1": 10.0})


    def test_the_variant_in_use_is_the_one_paper_trades(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, bid="0.80", ask="0.81")
            self.assertIn("unknown variant", k._command({"op": "variant", "name": "stop20"}))
            self.assertIn("base → band90", k._command({"op": "variant", "name": "band90"}))
            from tern import config
            self.assertEqual(config.load(d).strategy.variant, "band90")
            k._auto()
            self.assertFalse(k.paper.pos)                      # 80c is under band90's 90c floor
            k2 = self.desk(d, bid="0.80", ask="0.81")
            k2._command({"op": "variant", "name": "base"})
            k2._auto()
            self.assertIn(self.T, k2.paper.pos)

    def test_cap1_skips_a_strike_already_held_this_hour(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, bid="0.80", ask="0.81")
            k.cfg.strategy.variant = "cap1"
            k.taken = {"paper|KXBTCD-E1": {self.T: 1}}
            k._auto()
            self.assertFalse(k.paper.pos)
            k.cfg.strategy.variant = "nonsense"                # unknown in config.json: falls back to base
            k.slots = {}
            k._auto()
            self.assertIn(self.T, k.paper.pos)
            self.assertEqual(k.taken["paper|KXBTCD-E1"][self.T], 2)

    def test_gate_judges_the_variant_on_its_own_record(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.shadow.agg = {"base": {f"KXBTCD-H{h}": [1.0, 5, 2, 2, 0] for h in range(24 * 15)},
                            "cap1": {f"KXBTCD-H{h}": [1.0, 5, 2, 2, 0] for h in range(24)}}
            self.assertTrue(k.gate()["passed"])
            k._command({"op": "variant", "name": "cap1"})
            g = k.gate()
            self.assertFalse(g["passed"]); self.assertEqual(g["variant"], "cap1")
            k.mode = "live"
            self.assertFalse(k._live_rule_on(g))               # switching never skips the gate


class Robust(unittest.TestCase):
    def test_bad_view_file_is_ignored(self):
        import json
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = Desk(d)
            before = dict(k.view)
            for bad in ("null", "[]", '{"series": "KXBTCD", "ticker": [1]}'):
                with open(os.path.join(k.fast, "view.json"), "w") as fh:
                    fh.write(bad)
                k._read_view()
                self.assertEqual(k.view, before)

    def test_log_history_survives_a_restart_and_the_stop_reaches_the_screen(self):
        import json
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = Desk(d)
            k.log("paper: something happened")
            k._shutdown()
            with open(os.path.join(k.fast, "status.json")) as fh:
                st = json.load(fh)
            self.assertTrue(st["stopped"])                             # the screen shows the engine down
            self.assertIn("engine stopped", st["logs"][-1][2])
            with open(os.path.join(d, "tern.log"), "a") as fh:
                fh.write("Traceback (most recent call last):\n")
            with open(os.path.join(d, "tern.log"), "a") as fh:       # a same-second burst, as settlements log
                for i in range(6):
                    fh.write(f"2026-11-16 10:00:00 info  burst {i}\n")
                fh.write("2026-11-16 10:00:01 info  paper: something happened\n2026-11-16 10:00:01 info  engine stopped, state saved\n")
            k2 = Desk(d)
            ts = [t for t, _, _ in k2.logs]
            self.assertEqual(ts, sorted(set(ts)))                    # unique and rising: scrollback anchors on them
            k2.log("two\nlines")
            self.assertEqual(k2.logs[-1][2], "two lines")
            msgs = [m for _, _, m in Desk(d).logs][:-1]              # a new engine
            self.assertEqual(msgs[-2:], ["paper: something happened", "engine stopped, state saved"])

    def test_minor_fixes(self):
        import fcntl, threading, time
        from tern import dash, engine
        from tern.book import Book
        self.assertEqual([dash.volume(v) for v in (0, 182103, 999999, 1234567, 45_600_000, 123_000_000)],
                         ["0", "182,103", "999,999", "1.23M", "45.60M", "123M"])
        self.assertTrue(all(len(dash.volume(v)) <= 7 for v in (999999.4, 999999.6, 99_994_999, 99_995_000, 9e9)))
        b = Book("paper", 1e9)
        for i in range(5005):
            b.trade("T", 1, 0.5, 0.0, {}, "x")
        self.assertEqual(len(b.trades), 5000)
        with tempfile.TemporaryDirectory() as d:
            k = engine.Desk(d)
            calls = []
            real = engine.cfgmod.keys
            engine.cfgmod.keys = lambda: calls.append(1)
            try:
                now = time.time()
                for dt_ in (0, 1, 2, 11):
                    k._has_keys(now + dt_)
            finally:
                engine.cfgmod.keys = real
            self.assertEqual(len(calls), 2)                              # once, then again after 10s
            # a screen probing the lock for a moment must not stop the engine from starting
            probe = open(os.path.join(d, "engine.lock"), "a")
            fcntl.flock(probe, fcntl.LOCK_EX)
            threading.Timer(0.3, lambda: fcntl.flock(probe, fcntl.LOCK_UN)).start()
            k.spot.start = lambda: None
            k.step = lambda: None
            k.run(once=True)
            self.assertTrue(any("engine started" in m for _, _, m in k.logs))
            probe.close()

    def test_stale_spot_is_not_a_price(self):
        import collections, time
        from tern.engine import SpotFeed
        f = SpotFeed()
        f.products.add("BTC-USD")
        f.sec["BTC-USD"] = collections.deque([(time.time() - 60, 100.0)])
        self.assertIsNone(f.fresh("BTC-USD"))                        # Coinbase unreachable for a minute
        f.sec["BTC-USD"].append((time.time(), 101.0))
        self.assertEqual(f.fresh("BTC-USD"), 101.0)
        self.assertIsNone(f.fresh("ETH-USD"))

    def test_spot_history_is_fetched_outside_the_lock(self):
        from tern import engine
        f = engine.SpotFeed()
        real = engine.history
        engine.history = lambda prod, g: [[0.0, 1.0]] if not f.lock.locked() else self.fail("history called under the lock")
        try:
            f.want("BTC-USD")
        finally:
            engine.history = real
        self.assertEqual(list(f.minute["BTC-USD"]), [[0.0, 1.0]])

    def test_config_with_a_wrong_type_keeps_the_one_in_use(self):
        import json
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            cur = config.load(d)
            cur.strategy.contracts = 7.0
            for bad in ({"strategy": {"minutes": "abc"}}, {"live": {"enabled": "yes"}}, {"gate": {"min_entries": "many"}}):
                with open(os.path.join(d, "config.json"), "w") as fh:
                    json.dump(bad, fh)
                self.assertIs(config.load(d, cur), cur)
            with open(os.path.join(d, "config.json"), "w") as fh:
                json.dump({"strategy": {"contracts": 3, "minutes": [20, 10]}, "gate": {"min_entries": 50.0}}, fh)
            c = config.load(d, cur)
            self.assertEqual((c.strategy.contracts, c.strategy.minutes, c.gate.min_entries), (3.0, [20, 10], 50))

    def test_closed_event_is_relooked_every_5s_not_every_step(self):
        import time
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = Desk(d)
            asked = []
            k.pub.open_events = lambda s: asked.append(s) or []
            k.pub.event_markets = lambda e: []
            k.events["KXBTCD"] = {"event": "E", "title": "", "close": time.time() - 1, "markets": [], "fetched": time.time()}
            for _ in range(5):
                k._refresh_event("KXBTCD")
            self.assertEqual(len(asked), 1)

    def test_budget_skips_a_signal_it_cannot_afford(self):
        import time
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = Desk(d)
            now = time.time()
            mk = lambda t, k_, b, a: {"ticker": t, "floor_strike": k_, "yes_bid_dollars": b, "yes_ask_dollars": a,
                                      "yes_bid_size_fp": "100", "yes_ask_size_fp": "100"}
            k.events["KXBTCD"] = {"event": "E1", "title": "", "close": now + 1200, "fetched": now,
                                  "markets": [mk("E1-A", 1, "0.93", "0.94"), mk("E1-B", 2, "0.75", "0.76")]}
            S = k.cfg.strategy
            S.minutes = [20]; S.contracts = 10
            from tern.strategy import signals
            sig = signals(k.events["KXBTCD"]["markets"], S.band_lo, S.band_hi, S.max_spread)
            first = sig[0]["price"] if sig[0]["delta_sign"] > 0 else 1 - sig[0]["price"]
            k.hour_cost["paper|E1"] = S.max_hour_cost - 0.8 if first > 0.8 else S.max_hour_cost - first + 0.01
            k._auto()
            bought = {a["ticker"] for a in k.auto}
            self.assertTrue(bought, "a cheaper favorite after an unaffordable one is still bought")


class Live(unittest.TestCase):
    T = "KXBTCD-E1-T100"

    def desk(self, d, api):
        import time
        from tern import engine
        engine.LOST_REPLY_WAITS = (0.0, 0.0)
        k = engine.Desk(d)
        now = time.time()
        m = {"ticker": self.T, "floor_strike": 100.0, "yes_bid_dollars": "0.80", "yes_ask_dollars": "0.82",
             "yes_bid_size_fp": "50", "yes_ask_size_fp": "50"}
        k.events["KXBTCD"] = {"event": "KXBTCD-E1", "title": "", "close": now + 1200, "markets": [m], "fetched": now}
        k.ev_close["KXBTCD-E1"] = now + 1200
        k.meta[self.T] = {"event": "KXBTCD-E1", "strike": 100.0, "close": now + 1200, "series": "KXBTCD"}
        k.mode = "live"; k.api = api; k.balance = 500.0
        return k

    def api(self, **kw):
        from tern.kalshi import KalshiError
        class Api:
            sent = 0
            looks = list(kw.get("looks", []))                  # per look: a reply, or an exception to raise
            def order(self, *a, **k):
                Api.sent += 1
                if "status" in kw:
                    raise KalshiError("HTTP", kw["status"])
                if "reply" in kw:
                    return kw["reply"]
                raise KalshiError("timed out")                 # no status: the reply was lost
            def find_order(self, t, cid):
                if self.looks:
                    x = self.looks.pop(0)
                    if isinstance(x, Exception):
                        raise x
                    return x
                if "found" not in kw:
                    raise KalshiError("timed out")
                return kw["found"]
            def order_fills(self, oid):
                return kw.get("fills", [])
        return Api()

    def test_reply_fields_are_read_as_documented(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api(reply={"order_id": "o1", "fill_count": "5.00", "remaining_count": "0.00",
                                             "average_fill_price": "0.8100", "average_fee_paid": "0.0120"}))
            r = k._exec(k.live, self.T, 5, 0.82, 50, "auto")
            self.assertAlmostEqual(r["price"], 0.81); self.assertAlmostEqual(r["fee"], 0.06)
            self.assertAlmostEqual(k.live.pos[self.T]["q"], 5)

    def test_lost_reply_is_booked_from_the_fills(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api(found={"order_id": "o1", "fill_count_fp": "5.00", "yes_price_dollars": "0.8200",
                                             "taker_fees_dollars": "0.0600"},
                                      fills=[{"count_fp": "2.00", "yes_price_dollars": "0.8000", "fee_cost": "0.0300"},
                                             {"count_fp": "3.00", "yes_price_dollars": "0.8200", "fee_cost": "0.0400"}]))
            r = k._exec(k.live, self.T, 5, 0.82, 50, "auto")
            self.assertAlmostEqual(r["price"], 0.812); self.assertAlmostEqual(r["fee"], 0.07)

    def test_gateway_error_looks_the_order_up(self):
        from tern import engine
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api(status=504, found={"order_id": "o1", "fill_count_fp": "5.00", "taker_fees_dollars": "0.06"}))
            r = k._exec(k.live, self.T, 5, 0.82, 50, "auto")          # 504: the order may have gone through
            self.assertAlmostEqual(r["delta"], 5); self.assertAlmostEqual(k.live.pos[self.T]["q"], 5)
            k = self.desk(d, self.api(status=400))
            with self.assertRaises(engine.KalshiError):               # 4xx: refused, no lookup
                k._exec(k.live, self.T, 5, 0.82, 50, "auto")

    def test_unreadable_reply_looks_the_order_up(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api(reply={"fill_count": "five"}, found={"order_id": "o1", "fill_count_fp": "5.00"}))
            r = k._exec(k.live, self.T, 5, 0.82, 50, "auto")
            self.assertAlmostEqual(r["delta"], 5)

    def test_not_found_then_no_answer_is_unconfirmed(self):
        from tern import engine
        from tern.kalshi import KalshiError
        engine.LOST_REPLY_WAITS = (0.0, 0.0, 0.0)
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api(looks=[None, KalshiError("down"), KalshiError("down")]))
            engine.LOST_REPLY_WAITS = (0.0, 0.0, 0.0)
            with self.assertRaises(engine.Unconfirmed):
                k._exec(k.live, self.T, 5, 0.82, 50, "auto")
            k = self.desk(d, self.api(looks=[KalshiError("down"), None]))
            self.assertIsNone(k._exec(k.live, self.T, 5, 0.82, 50, "auto"))   # the last look answered: unfilled

    def test_rearming_live_resets_the_daily_loss_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.day = {"date": __import__("time").strftime("%Y-%m-%d"), "pnl": 100.0}
            k._command({"op": "live", "on": True})
            self.assertEqual(k.day, {})
            k.cfg.live.enabled = True
            k._daily_loss()                                   # anchors at the current P&L, no KILL
            self.assertTrue(k.cfg.live.enabled)

    def test_voided_market_returns_the_stake(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.paper.trade(self.T, 10, 0.80, 0.05, k.meta[self.T], "auto")
            cash = k.paper.cash
            k.paper.pos[self.T]["close"] = time.time() - 100
            k.auto = [{"ts": 0, "ticker": self.T, "side": "yes", "n": 10, "cost": 0.8, "fee": 0.05, "slot": 20, "pnl": None}]
            k.pub.market = lambda t: {"status": "finalized", "result": "void"}
            k._settle()
            self.assertNotIn(self.T, k.paper.pos)
            self.assertAlmostEqual(k.paper.cash, cash + 8.0)
            self.assertEqual(k.auto[0]["pnl"], -0.05)
            k.paper.trade(self.T, 10, 0.80, 0.0, k.meta[self.T], "auto")
            k.paper.pos[self.T]["close"] = time.time() - 100; k.last.clear()
            k.pub.market = lambda t: {"status": "closed", "result": ""}   # not decided yet: left alone
            k._settle()
            self.assertIn(self.T, k.paper.pos)

    def test_failed_save_is_retried_at_once(self):
        from tern import engine
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            real = engine._atomic_write
            def boom(*a, **kw):
                raise OSError("disk full")
            engine._atomic_write = boom
            try:
                k.slots["live|KXBTCD-E1"] = {20}
                with self.assertRaises(OSError):
                    k._save_state()
            finally:
                engine._atomic_write = real
            k._save_state()                                    # same fingerprint, but it never reached disk
            self.assertEqual(engine.Desk(d).slots["live|KXBTCD-E1"], {20})

    def test_order_that_closes_a_position_is_not_counted_as_new_risk(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api(reply={"order_id": "o1", "fill_count": "10.00", "average_fill_price": "0.20",
                                             "average_fee_paid": "0"}))
            k.cfg.live.capital_usd = 10.0; k.cfg.live.max_order_usd = 5.0
            k.live.trade(self.T, 10, 0.95, 0.0, k.meta[self.T], "auto")     # $9.50 at risk, near the cap
            r = k._exec(k.live, self.T, -10, 0.20, 50, "auto")            # the favorite flipped: NO closes the YES
            self.assertAlmostEqual(r["delta"], -10)
            self.assertNotIn(self.T, k.live.pos)

    def test_a_lost_reply_skips_the_rest_of_the_live_slot(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            api = self.api(status=503, found={"order_id": "o1", "fill_count_fp": "0"})
            k = self.desk(d, api)
            m2 = dict(k.events["KXBTCD"]["markets"][0], ticker="KXBTCD-E1-T90", floor_strike=90.0)
            k.events["KXBTCD"]["markets"].append(m2)
            k.meta["KXBTCD-E1-T90"] = dict(k.meta[self.T], strike=90.0)
            k.cfg.live.auto_needs_gate = False
            k.cfg.strategy.minutes = [20]
            k.slots["paper|KXBTCD-E1"] = {20}
            type(api).sent = 0
            k._auto()
            self.assertEqual(type(api).sent, 1)                          # two favorites, one order
            self.assertTrue(any("rest are skipped" in m for _, _, m in k.logs))

    def test_lost_reply_not_on_kalshi_is_unfilled(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api(found=None))
            self.assertIsNone(k._exec(k.live, self.T, 5, 0.82, 50, "auto"))
            self.assertFalse(k.live.pos)

    def test_unconfirmed_order_spends_the_budget_and_stops_the_slot(self):
        from tern import engine
        with tempfile.TemporaryDirectory() as d:
            api = self.api()
            k = self.desk(d, api)
            with self.assertRaises(engine.Unconfirmed):
                k._exec(k.live, self.T, 5, 0.82, 50, "auto")
            k.cfg.live.auto_needs_gate = False
            k.cfg.strategy.minutes = [20]
            k.slots["paper|KXBTCD-E1"] = {20}
            type(api).sent = 0
            k._auto()
            self.assertEqual(type(api).sent, 1)
            from tern.book import taker_fee
            self.assertAlmostEqual(k.hour_cost["live|KXBTCD-E1"], 5 * 0.82 + taker_fee(5, 0.82, k.cfg.taker_fee))
            self.assertEqual(k.last["pos"], 0)
            self.assertEqual(engine.Desk(d).slots["live|KXBTCD-E1"], {20})    # on disk: a restart does not repeat it

    def test_kill_switch_ignores_when_the_balance_is_credited(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.cfg.live.enabled = True
            k.live.cash = 500.0
            k.live.trade(self.T, 50, 0.80, 0.0, k.meta[self.T], "x")
            k._daily_loss()
            for cash in (460.0, 510.0):                       # payout not credited yet, then credited
                k.live.settle(self.T, True) if self.T in k.live.pos else None
                k.live.cash = cash
                k._daily_loss()
                self.assertTrue(k.cfg.live.enabled)
            k.live.trade(self.T, 50, 0.80, 0.0, k.meta[self.T], "x")
            k.live.settle(self.T, False)                      # a real $40 loss
            k._daily_loss()
            self.assertFalse(k.cfg.live.enabled)

    def test_reconcile_leaves_other_markets_alone(self):
        with tempfile.TemporaryDirectory() as d:
            api = self.api()
            api.positions = lambda: {"AAAPRES-28-X": 10.0, self.T: 3.0}
            k = self.desk(d, api)
            k._reconcile()
            self.assertEqual(set(k.live.pos), {self.T})
            self.assertEqual(k.foreign, ["AAAPRES-28-X"])

    def test_unsettled_tickers_do_not_hold_the_queue(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            asked = []
            k.pub.market = lambda t: asked.append(t) or {}
            for i in range(8):
                k.paper.pos[f"A{i}"] = {"q": 1.0, "avg": 0.5, "close": time.time() - 100}
            k._settle(); k._settle()
            self.assertEqual(len(asked), 8); self.assertEqual(len(set(asked)), 8)

    def test_gate_hours_are_not_doubled_by_a_second_series(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.shadow.agg = {"base": {f"{s}-H{h}": [0.1, 1, 1, 1, 0] for h in range(12) for s in ("KXBTCD", "KXETHD")}}
            self.assertIn("0.5 days", k.gate()["checks"][0]["detail"])


class Shadows(unittest.TestCase):
    def m(self, t, bid, ask, k):
        return {"ticker": t, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
                "yes_bid_size_fp": "50", "yes_ask_size_fp": "40", "floor_strike": k, "status": "active"}

    def test_variants_enter_and_settle(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy(series=["E"])
            ms = [self.m("E-T1", 0.92, 0.93, 1), self.m("E-T2", 0.80, 0.81, 2), self.m("E-T3", 0.10, 0.11, 3)]
            sh.step("E", 1000.0, ms, S, 1000.0 - 25 * 60 + 1)
            by = lambda v: sorted(r["ticker"] for r in sh.rows if r["v"] == v)
            self.assertEqual(by("base"), ["E-T1", "E-T2", "E-T3"])
            self.assertEqual(by("band90"), ["E-T1"])                       # T3's NO mid is 89.5c: outside 90-95
            self.assertEqual(by("cap1"), ["E-T1", "E-T2", "E-T3"])
            self.assertEqual(by("band80"), ["E-T2", "E-T3"])               # T1's mid is 92.5c: outside 80-90
            self.assertEqual(by("late"), [])                               # 25m is not one of its slots
            self.assertEqual(by("tight1"), ["E-T1", "E-T2", "E-T3"])       # all three are 1c wide
            self.assertEqual(by("band75"), [])                             # 80.5c and up: none under 80
            self.assertEqual(by("early"), ["E-T1", "E-T2", "E-T3"])        # 25m is one of its slots
            self.assertEqual(by("multi"), [])                              # "E" is no coin's event
            sh.step("E", 1000.0, ms, S, 1000.0 - 25 * 60 + 3)              # same slot: no second entry
            self.assertEqual(len(sh.rows), 15)
            sh.step("E", 1000.0, [self.m("E-T1", 0.92, 0.93, 1), self.m("E-T2", 0.79, 0.81, 2)], S, 1000.0 - 15 * 60 + 1)
            self.assertEqual(sorted(r["ticker"] for r in sh.rows if r["v"] == "late"), ["E-T1", "E-T2"])
            self.assertEqual(sum(r["v"] == "early" and r["slot"] == 15 for r in sh.rows), 0)   # not its slot
            self.assertEqual(sorted(r["ticker"] for r in sh.rows if r["v"] == "band75"), [])  # 80c mid is band80's: 75-80 stops short of it
            self.assertEqual(sorted(r["ticker"] for r in sh.rows if r["v"] == "tight1" and r["slot"] == 15), ["E-T1"])
            sh.rows = [r for r in sh.rows if r["slot"] == 25]
            self.assertEqual(sh.settle({"E-T1": True, "E-T2": False, "E-T3": False}), 15)
            self.assertAlmostEqual(sh.agg["band90"]["E"][0], 5 * 0.07 - 0.03)
            self.assertAlmostEqual(sh.agg["base"]["E"][0], 5 * 0.07 - 0.03 - 5 * 0.81 - 0.06 + 5 * 0.10 - 0.04)
            sh.save()
            again = Shadow(d)                                              # the record survives a restart
            self.assertEqual(again.rows, [])
            self.assertAlmostEqual(again.agg["base"]["E"][0], sh.agg["base"]["E"][0])
            self.assertEqual(again.summary(["base"])[0]["curve"], [round(sh.agg["base"]["E"][0], 2)])

    def test_multi_trades_every_coin_and_the_rest_only_the_rule_coin(self):
        from tern.config import Strategy
        from tern.shadow import Shadow, all_coins
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy()
            self.assertEqual(all_coins(S)[0], "KXBTCD")
            self.assertIn("KXHYPED", all_coins(S))
            for ev in ("KXBTCD-H1", "KXSOLD-H1"):
                sh.step(ev, 1000.0, [self.m(ev + "-T1", 0.92, 0.93, 1)], S, 1000.0 - 25 * 60 + 1)
            on = lambda v: sorted(r["event"] for r in sh.rows if r["v"] == v)
            self.assertEqual(on("base"), ["KXBTCD-H1"])
            self.assertEqual(on("multi"), ["KXBTCD-H1", "KXSOLD-H1"])
            sh.settle({"KXBTCD-H1-T1": True, "KXSOLD-H1-T1": True})
            self.assertEqual(sh.summary(["multi"])[0]["hours"], 1)         # two coins in one hour are one hour

    def test_retired_variants_keep_their_record_but_stop_trading(self):
        import json
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "shadow-settled.jsonl"), "w") as fh:
                fh.write(json.dumps({"v": "stop20", "event": "E0", "close": 1, "ticker": "E0-T1", "n": 5, "pnl": -1.0}) + "\n")
            sh = Shadow(d)
            sh.step("E", 1000.0, [self.m("E-T1", 0.92, 0.93, 1)], Strategy(), 1000.0 - 25 * 60 + 1)
            self.assertNotIn("stop20", {r["v"] for r in sh.rows})
            self.assertNotIn("stop20", [r["name"] for r in sh.summary()])
            self.assertAlmostEqual(sh.summary(["stop20"])[0]["pnl"], -1.0)

    def test_cap1_takes_each_strike_once_per_hour(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy(series=["E"])
            ms = [self.m("E-T1", 0.92, 0.93, 1), self.m("E-T2", 0.80, 0.81, 2)]
            sh.step("E", 1000.0, ms, S, 1000.0 - 25 * 60 + 1)
            ms.append(self.m("E-T3", 0.10, 0.11, 3))                       # T3 comes into the band at the 20m slot
            sh.step("E", 1000.0, ms, S, 1000.0 - 20 * 60 + 1)
            sh.step("E", 1000.0, ms, S, 1000.0 - 15 * 60 + 1)
            by = lambda v: sorted(r["ticker"] for r in sh.rows if r["v"] == v)
            self.assertEqual(by("cap1"), ["E-T1", "E-T2", "E-T3"])
            self.assertEqual(len(by("base")), 8)                           # base buys every strike at every slot

    def test_void_closes_rows_and_a_settled_row_is_not_reopened(self):
        import json
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh = Shadow(d)
            row = lambda t, ts: {"v": "base", "ts": ts, "event": "E", "close": 0, "ticker": t, "side": "yes", "n": 5.0,
                                 "cost": 0.8, "fee": 0.06, "slot": 20, "stop": None, "pnl": None}
            sh.rows = [row("E-A", 1.0), row("E-B", 2.0)]
            self.assertEqual(sh.void({"E-A"}), 1)
            self.assertEqual([r["ticker"] for r in sh.rows], ["E-B"])
            self.assertAlmostEqual(sh.summary()[0]["pnl"], -0.06)
            # the engine dies after E-B reached the settled file, before shadow.json was saved without it
            with open(os.path.join(d, "shadow.json"), "w") as fh:
                json.dump({"rows": [row("E-B", 2.0)], "slots": {}}, fh)
            sh.settle({"E-B": True})
            again = Shadow(d)
            self.assertEqual(again.rows, [])
            self.assertEqual(again.summary()[0]["entries"], 2)

    def test_recorder_round_trip(self):
        import gzip
        from tern.record import Recorder
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder(d)
            ms = [self.m("E-T1", 0.50, 0.52, 74999.99), self.m("E-T2", 0.99, 1.00, 70000)]
            rec.snap("KXBTCD", 1000.0, 75001.5, ms, 400.0)
            rec.snap("KXBTCD", 1000.0, 75001.5, ms, 401.0)                           # under 5 s later: skipped
            rec.flush(); rec.snap("KXBTCD", 1000.0, None, ms, 460.0); rec.flush()
            f = [x for x in os.listdir(os.path.join(d, "ticks"))][0]
            lines = gzip.open(os.path.join(d, "ticks", f), "rt").read().splitlines()
            self.assertEqual(lines, ["ts,tau,spot,strike,bid,ask,bid_size,ask_size",
                                     "400,600,75001.5,74999.99,0.5,0.52,50,40", "460,540,,74999.99,0.5,0.52,50,40"])


class Signing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.ok = shutil.which("openssl") is not None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def run_ossl(self, *a):
        return subprocess.run(["openssl", *a], capture_output=True, text=True, cwd=self.d)

    def check(self, gen, verify, pkcs1=False):
        if not self.ok:
            self.skipTest("no openssl")
        self.run_ossl(*gen, "-out", "k.pem")
        self.run_ossl("pkey", "-in", "k.pem", "-pubout", "-out", "k.pub")
        src = "k.pem"
        if pkcs1:
            self.run_ossl("rsa", "-in", "k.pem", "-traditional", "-out", "k1.pem"); src = "k1.pem"
        key = load_key(open(os.path.join(self.d, src)).read())
        msg = b"1731000000000POST/trade-api/v2/portfolio/events/orders"
        open(os.path.join(self.d, "m"), "wb").write(msg)
        open(os.path.join(self.d, "s"), "wb").write(key.sign(msg))
        r = self.run_ossl(*verify)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_ed25519(self):
        self.check(["genpkey", "-algorithm", "ed25519"],
                   ["pkeyutl", "-verify", "-pubin", "-inkey", "k.pub", "-rawin", "-in", "m", "-sigfile", "s"])

    def test_rsa_pss_pkcs8_and_pkcs1(self):
        v = ["dgst", "-sha256", "-sigopt", "rsa_padding_mode:pss", "-sigopt", "rsa_pss_saltlen:32",
             "-verify", "k.pub", "-signature", "s", "m"]
        self.check(["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"], v)
        self.check(["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"], v, pkcs1=True)

    def test_signed_text_drops_query(self):
        class K:
            def sign(self, m):
                self.m = m; return b"x"
        k = K()
        signature(k, "1", "get", "/trade-api/v2/portfolio/orders?limit=5")
        self.assertEqual(k.m, b"1GET/trade-api/v2/portfolio/orders")


class Charts(unittest.TestCase):
    def test_braille_fill(self):
        codes, kind = braille([0.0, 1.0], 1, 1, 0.0, 1.0)
        self.assertEqual(codes[0][0], 0x40 | 0x08 | 0x10 | 0x20 | 0x80)    # left column bottom dot, right column full

    def test_by_time(self):
        pts = [[10, 1.0], [20, 2.0]]
        self.assertEqual(by_time(pts, 0, 20, 4), [1.0, 1.0, 2.0])          # columns end 5,10,15,20; the empty first is skipped
        self.assertEqual(by_time(pts, 0, 40, 4), [1.0, 2.0, 2.0, 2.0])     # the last value carries forward


if __name__ == "__main__":
    unittest.main()
