import json
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
from tern import webctl

webctl.ENABLED = False           # no test starts, stops or asks systemd about the real tern-web

# Every engine keeps its status and view files in $XDG_RUNTIME_DIR/tern-<hash of its runtime>. Each test has a
# fresh temp runtime, so without this every run would leave dozens of folders there for good (until reboot).
_RUN = {}


def setUpModule():
    _RUN["old"] = os.environ.get("XDG_RUNTIME_DIR")
    _RUN["dir"] = tempfile.mkdtemp(prefix="tern-tests-")
    os.environ["XDG_RUNTIME_DIR"] = _RUN["dir"]
    os.environ["TERN_NO_ALERTS"] = "1"          # no desktop pop-ups from test engines


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

    def test_history_is_appended_to_its_own_files_not_rewritten_with_the_state(self):
        import json, time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            now = time.time()
            k.meta = {"A-T1": {"close": now - 60}, "A-T2": {"close": now + 600}}   # T2 is still open
            k.pub.market = lambda t: {"result": "yes", "status": "finalized"}
            k.paper.trade("A-T1", 2, 0.5, 0.01, k.meta["A-T1"], "auto")
            k.paper.trade("A-T2", 2, 0.5, 0.01, k.meta["A-T2"], "auto")
            k.auto = [{"ts": now, "ticker": t, "side": "yes", "n": 2, "cost": 0.5, "fee": 0.01, "slot": 10, "pnl": None}
                      for t in ("A-T1", "A-T2")]
            k.paper.mark_curve(1000.0, now)
            k._settle(); k._save_state()
            st = json.load(open(os.path.join(d, "state.json")))
            self.assertNotIn("trades", st["paper"]); self.assertNotIn("curve", st["paper"])
            self.assertEqual([a["ticker"] for a in st["auto"]], ["A-T2"])      # only the open entry
            self.assertEqual(set(st["meta"]), {"A-T2"})                        # and only meta still owed
            lines = lambda n: open(os.path.join(d, n)).read().splitlines()
            self.assertEqual(len(lines("paper-trades.jsonl")), 3)              # two fills and a settlement
            self.assertEqual(len(lines("paper-curve.jsonl")), 1)
            self.assertEqual(len(lines("rule-entries.jsonl")), 1)
            k._save_state()
            self.assertEqual(len(lines("paper-trades.jsonl")), 3)              # nothing new: nothing appended
            k2 = self.desk(d)                                                  # a restart reads it all back
            self.assertEqual([t["kind"] for t in k2.paper.trades], ["fill", "fill", "settle"])
            self.assertEqual(k2.paper.curve, k.paper.curve)
            self.assertEqual([(a["ticker"], a["pnl"] is None) for a in k2.auto], [("A-T1", False), ("A-T2", True)])
            self.assertAlmostEqual(k2.gate()["pnl"], k.gate()["pnl"])
            self.assertEqual(k2.paper.new_trades, [])                          # read back, not queued again
            # the engine died after writing the entry's file and before saving state: it is not counted twice
            st["auto"].append(dict(k2.auto[0], pnl=None))
            json.dump(st, open(os.path.join(d, "state.json"), "w"))
            self.assertEqual(self.desk(d).gate()["entries"], 1)
            k2._command({"op": "reset_paper"}); k2._save_state()
            self.assertFalse(os.path.exists(os.path.join(d, "paper-trades.jsonl")))
            self.assertEqual(self.desk(d).auto, [])

    def test_a_state_file_from_before_the_split_seeds_the_history_files(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            trade = {"ts": 5.0, "ticker": "A-T1", "delta": 1, "price": 0.5, "fee": 0.0, "realized": 0.0, "tag": "auto", "kind": "fill"}
            row = {"ts": 5.0, "ticker": "A-T1", "side": "yes", "n": 1, "cost": 0.5, "fee": 0.0, "slot": 10}
            old = {"paper": {"name": "paper", "cash": 990.0, "start": 1000.0, "pos": {}, "realized": -10.0, "fees": 0.0,
                             "trades": [trade], "curve": [[60, 1000.0], [120, 990.0]]},
                   "auto": [dict(row, pnl=0.5), dict(row, ts=6.0, pnl=None)]}
            json.dump(old, open(os.path.join(d, "state.json"), "w"))
            k = self.desk(d)
            self.assertEqual(k.paper.trades, [trade]); self.assertEqual(len(k.paper.curve), 2)
            self.assertEqual([a["pnl"] for a in k.auto], [0.5, None])
            k._save_state()
            self.assertLess(os.path.getsize(os.path.join(d, "state.json")), 600)
            k2 = self.desk(d)                                                  # and nothing is seeded twice
            self.assertEqual(k2.paper.trades, [trade]); self.assertEqual([a["pnl"] for a in k2.auto], [0.5, None])
            self.assertAlmostEqual(k2.paper.cash, 990.0)

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
            with open(os.path.join(d, "config.json"), "w") as fh:
                fh.write("{bad")
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
            self.assertIn("coins BTC → BTC ETH", k.results["new"])
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

    def test_settings_change_only_listed_fields_within_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            from tern import config
            self.assertIn("25 → 40", k._command({"op": "set", "key": "live.max_order_usd", "value": 40}))
            self.assertEqual(config.load(d).live.max_order_usd, 40.0)
            self.assertIn("5 → 3", k._command({"op": "set", "key": "strategy.contracts", "value": 3}))
            self.assertEqual(k.cfg.strategy.contracts, 3.0)
            self.assertIn("next reset", k._command({"op": "set", "key": "paper_capital", "value": 2000}))
            self.assertIn("not set", k._command({"op": "set", "key": "live.enabled", "value": 1}))
            self.assertIn("not set", k._command({"op": "set", "key": "live.capital_usd", "value": -5}))
            self.assertIn("not set", k._command({"op": "set", "key": "live.capital_usd", "value": "100"}))
            self.assertFalse(config.load(d).live.enabled)
            self.assertEqual(config.load(d).live.capital_usd, 100.0)

    def test_web_settings_drive_the_service(self):
        from unittest import mock
        from tern import config
        calls = []
        def fake(*args):
            calls.append(args)
            return (0, "active") if args[0] == "is-active" else (0, "")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(webctl, "systemctl", fake):
            k = self.desk(d)
            self.assertEqual((k.cfg.web.enabled, k.cfg.web.port, k.cfg.web.network), (True, 8787, False))
            msg = k._command({"op": "set", "key": "web.port", "value": 9090})
            self.assertIn("web port: 8787 → 9090", msg)
            self.assertIn("restarting on port 9090", msg)
            self.assertIn("sudo ufw allow 9090/tcp", msg)
            self.assertEqual(calls[-1], ("restart", "tern-web"))
            self.assertEqual(config.load(d).web.port, 9090)
            self.assertIsInstance(config.load(d).web.port, int)
            self.assertIn("open to the network", k._command({"op": "set", "key": "web.network", "value": True}))
            self.assertIn("web screen: on → off · web screen off", k._command({"op": "set", "key": "web.enabled", "value": False}))
            self.assertEqual(calls[-1], ("stop", "tern-web"))
            n = len(calls)
            self.assertNotIn("restart", k._command({"op": "set", "key": "web.port", "value": 9091}))   # off: just saved
            self.assertEqual(len(calls), n)
            self.assertIn("restarting on port 9091", k._command({"op": "set", "key": "web.enabled", "value": True}))
            for bad in (80, 70000, "9000", True):
                self.assertIn("not set", k._command({"op": "set", "key": "web.port", "value": bad}))
            st = (k._status(), json.load(open(os.path.join(k.fast, "status.json"))))[1]
            self.assertEqual(st["web_state"]["state"], "active")
            self.assertIn(":9091/", st["web_state"]["url"])
            self.assertEqual(st["settings"]["web.port"], 9091)

    def test_web_section_hand_edit_restarts_the_service(self):
        from unittest import mock
        from tern import config
        calls = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(webctl, "systemctl", lambda *a: calls.append(a) or (0, "")):
            k = self.desk(d)
            cfg = config.load(d); cfg.web.port = 8800; config.save(cfg, d)
            k._reload_cfg(force=True)
            self.assertEqual(calls[-1], ("restart", "tern-web"))
            self.assertTrue(any("port 8800" in m for _, _, m in k.logs))
            n = len(calls); k._reload_cfg(force=True)
            self.assertEqual(len(calls), n)                                  # unchanged: left alone

    def test_web_config_mode_and_unit(self):
        from unittest import mock
        from tern import __main__ as cli, config
        with tempfile.TemporaryDirectory() as d:
            unit = cli._web_unit("/r", "/r/runtime", "/usr/bin/python3")
            self.assertIn("-m tern web --config", unit)
            self.assertIn("Restart=on-failure", unit)                       # off exits 0 and stays down
            cfg = config.Config(); cfg.web.enabled = False; config.save(cfg, d)
            with mock.patch.object(config, "RUNTIME", d), mock.patch.object(cli.cfgmod, "RUNTIME", d), \
                    mock.patch("tern.web.serve") as serve:
                self.assertEqual(cli.main(["web", "--config"]), 0)
                serve.assert_not_called()
                cfg.web.enabled, cfg.web.port, cfg.web.network = True, 9000, True; config.save(cfg, d)
                cli.main(["web", "--config"])
                serve.assert_called_once_with(d, "0.0.0.0", 9000)

    def test_gate_and_alert_settings(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            from tern import config
            self.assertIn("gate days of hours traded: 14 → 7", k._command({"op": "set", "key": "gate.min_days", "value": 7}))
            self.assertEqual(config.load(d).gate.min_days, 7.0)
            self.assertIn("-50", k._command({"op": "set", "key": "gate.min_pnl", "value": -50}))
            self.assertIn("not set", k._command({"op": "set", "key": "alerts", "value": 0}))
            self.assertIn("on → off", k._command({"op": "set", "key": "alerts", "value": False}))
            self.assertFalse(config.load(d).alerts)

    def test_alerts_only_for_what_matters(self):
        from tern import engine
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            sent, real = [], engine._run_quiet
            engine._run_quiet = lambda cmd: sent.append(cmd[-1])
            os.environ.pop("TERN_NO_ALERTS")
            try:
                k.log("paper: KXBTCD-E1-T100 settled YES, +$0.80")
                k.log("shadow: KXBTCD-E1 settled · base +1.00")
                k.log("KXBTCD market data: timed out", "warn")
                k.log("live: 20m slot on KXBTCD-E1 (base): 1 favorites in band, 0 bought")
                k.log("live: 20m slot on KXBTCD-E1 (base): 1 favorites in band, 1 bought")
                k.log("KILL: live down $31.00 today", "error")
                k.log("step failed: boom", "error"); k.log("step failed: boom", "error")
                import time as _t; _t.sleep(0.2)
                self.assertEqual(sent, ["live: 20m slot on KXBTCD-E1 (base): 1 favorites in band, 1 bought",
                                        "KILL: live down $31.00 today", "step failed: boom"])
                k.cfg.alerts = False
                k.log("KILL: something else", "error"); _t.sleep(0.1)
                self.assertEqual(len(sent), 3)
            finally:
                engine._run_quiet = real
                os.environ["TERN_NO_ALERTS"] = "1"

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

    def test_live_has_its_own_size(self):
        with tempfile.TemporaryDirectory() as d:
            got = []
            class Api:
                def order(self, t, side, n, price, cid):
                    got.append(n)
                    return {"order_id": "o1", "fill_count": str(n), "average_fill_price": "0.82", "average_fee_paid": "0"}
            k = self.desk(d, Api())
            k.cfg.live.auto_needs_gate = False
            k.cfg.live.contracts = 2.0
            k._auto()
            self.assertEqual(got, [2.0])
            self.assertAlmostEqual(k.live.pos[self.T]["q"], 2)
            self.assertAlmostEqual(k.paper.pos[self.T]["q"], 5)        # paper keeps strategy.contracts

    def live_profile_desk(self, d):
        """Profile 1's band (85-95¢) misses the 81¢ favorite; profile 2's (75-95¢) takes it."""
        from tern import profiles
        got = []
        class Api:
            def order(self, t, side, n, price, cid):
                got.append(n)
                return {"order_id": "o1", "fill_count": str(n), "average_fill_price": "0.82", "average_fee_paid": "0"}
        k = self.desk(d, Api())
        p = profiles.create(k.runtime, "wide", k.cfg, k.cfg.taker_fee)
        k.cfg.strategy.band_lo = 0.85
        k._profiles_reload(force=True)
        return k, next(x for x in k.profiles if x.id == p.id), got

    def test_live_trades_the_live_profiles_rule(self):
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            k, p, got = self.live_profile_desk(d)
            self.assertEqual(k.live_target()["id"], "1")                       # profile 1 until d says otherwise
            self.assertIn("no profile", k._command({"op": "live_profile", "profile": "nope"}))
            self.assertIn("live profile profile 1 → wide (base)", k._command({"op": "live_profile", "profile": p.id}))
            self.assertEqual(config.load(d).live.profile, p.id)
            self.assertIn("already", k._command({"op": "live_profile", "profile": p.id}))
            g = k.gate()
            self.assertEqual((g["profile"], g["variant"], g["passed"]), (p.id, "base", False))
            self.assertIn("wide's base", g["checks"][0]["name"])               # its own record, named as such
            k._auto()
            self.assertEqual(got, [])                                          # its record hasn't passed the gate
            k.cfg.live.auto_needs_gate = False
            k.slots.clear()
            k._auto()
            self.assertEqual(got, [5.0])                                       # live: the wide profile's band
            self.assertNotIn(self.T, k.paper.pos)                              # paper: profile 1's, which missed it
            self.assertTrue(any("live:" in m and "wide · base" in m for _, _, m in k.logs))
            rows = {r["id"]: r["live"] for r in k._profile_rows([], set())}
            self.assertEqual(rows, {"1": False, p.id: True})

    def test_live_profile_off_or_gone_enters_nothing(self):
        import shutil as sh
        with tempfile.TemporaryDirectory() as d:
            k, p, got = self.live_profile_desk(d)
            k.cfg.live.auto_needs_gate = False                                 # even without the gate
            k._command({"op": "live_profile", "profile": p.id})
            self.assertIn("live enters nothing while it is off", k._command({"op": "profile_enable", "profile": p.id, "on": False}))
            g = k.gate()
            self.assertFalse(g["passed"]); self.assertIn("is off", g["checks"][0]["name"])
            k._auto()
            self.assertEqual(got, [])
            self.assertIn("make another one live first", k._command({"op": "profile_delete", "profile": p.id}))
            sh.rmtree(p.dir); k._profiles_reload(force=True)                   # gone some other way
            self.assertIn("is gone", k.gate()["checks"][0]["name"])
            k.slots.clear(); k._auto()
            self.assertEqual(got, [])                                          # never falls back to profile 1 alone
            self.assertIn("wide → profile 1", k._command({"op": "live_profile", "profile": "1"}).replace(p.id, "wide"))

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

    def test_shadow_rows_kalshi_has_not_decided_are_asked_about_again(self):
        import time
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = Desk(d)
            now = time.time()
            row = lambda t: {"v": "base", "ts": now - 4000, "event": "KXBTCD-E1", "close": now - 2000, "ticker": t,
                             "side": "yes", "n": 5.0, "cost": 0.9, "fee": 0.04, "slot": 25, "pnl": None}
            k.shadow.rows = [row("KXBTCD-E1-T1"), row("KXBTCD-E1-T2")]
            k.closed["KXBTCD-E1"] = {"series": "KXBTCD", "close": now - 2000}     # over half an hour ago: "late"
            ms = [{"ticker": "KXBTCD-E1-T1", "floor_strike": 1, "result": "yes", "status": "finalized"},
                  {"ticker": "KXBTCD-E1-T2", "floor_strike": 2, "result": "", "status": "closed"}]
            k.pub.event_markets = lambda e: ms
            k._shadow()
            self.assertEqual([r["ticker"] for r in k.shadow.rows], ["KXBTCD-E1-T2"])
            self.assertIn("KXBTCD-E1", k.closed)                           # T2 is still owed: the event stays
            ms[1].update(result="no", status="finalized")
            k.closed["KXBTCD-E1"]["tried"] = 0
            k._shadow()
            self.assertEqual(k.shadow.rows, [])
            self.assertNotIn("KXBTCD-E1", k.closed)
            with open(os.path.join(d, "ticks", "results.csv")) as fh:
                self.assertEqual(len(fh.read().splitlines()), 1)           # results are written once, not per look

    def test_paper_buys_whole_contracts_no_more_than_quoted(self):
        import time
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = Desk(d)
            now = time.time()
            k.events["KXBTCD"] = {"event": "E1", "title": "", "close": now + 1200, "fetched": now, "markets": [
                {"ticker": "E1-A", "floor_strike": 1, "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91",
                 "yes_bid_size_fp": "50", "yes_ask_size_fp": "3.50"}]}
            k.cfg.strategy.minutes = [20]
            k._auto()
            self.assertEqual([a["n"] for a in k.auto], [3.0])              # 3.5 on offer: 3, as the shadow record takes

    def test_gate_hours_are_not_doubled_by_a_second_series(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.shadow.agg = {"base": {f"{s}-H{h}": [0.1, 1, 1, 1, 0] for h in range(12) for s in ("KXBTCD", "KXETHD")}}
            self.assertIn("0.5 days", k.gate()["checks"][0]["detail"])


class Shadows(unittest.TestCase):
    def m(self, t, bid, ask, k):
        return {"ticker": t, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
                "yes_bid_size_fp": "50", "yes_ask_size_fp": "40", "floor_strike": k, "status": "active"}

    def test_rows_carry_settings_version(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy(series=["E"])
            ms = [self.m("E-T1", 0.92, 0.93, 1)]
            sh.step("E", 1000.0, ms, S, 1000.0 - 25 * 60 + 1, sv=1)
            sh.step("E", 1000.0, ms, S, 1000.0 - 20 * 60 + 1, sv=2)
            self.assertEqual({r["sv"] for r in sh.rows if r["v"] == "base"}, {1, 2})
            sh.settle({"E-T1": True})
            self.assertEqual(sh.versions("base"), [1, 2])
            self.assertEqual(sh.summary(["base"], sv=1)[0]["entries"], 1)
            self.assertEqual(sh.summary(["base"])[0]["entries"], 2)          # no sv: everything, as before
            sh.save()
            again = Shadow(d)
            self.assertEqual(again.summary(["base"], sv=2)[0]["entries"], 1)  # survives a restart

    def test_old_rows_are_period_zero(self):
        import json
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "shadow-settled.jsonl"), "w") as fh:
                fh.write(json.dumps({"v": "base", "ts": 1.0, "event": "E", "close": 2.0, "ticker": "E-T1", "side": "yes",
                                     "n": 5, "cost": 0.9, "fee": 0.02, "slot": 25, "pnl": 0.48}) + "\n")
            sh = Shadow(d)
            self.assertEqual(sh.versions("base"), [0])
            self.assertEqual(sh.summary(["base"], sv=0)[0]["entries"], 1)

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

    def test_a_variant_can_be_watched_as_if_it_traded(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy(series=["E"])
            sh.step("E-H1", 1000.0, [self.m("E-H1-T1", 0.92, 0.93, 1), self.m("E-H1-T2", 0.10, 0.11, 2)], S, 1000.0 - 25 * 60 + 1)
            sh.settle({"E-H1-T1": True, "E-H1-T2": True})                   # T1's YES won, T2's NO lost
            sh.step("E-H2", 5000.0, [self.m("E-H2-T1", 0.80, 0.81, 1)], S, 5000.0 - 25 * 60 + 1)
            rec = sh.summary(["base"])[0]
            v = sh.view("base", rec, {"E-H2-T1": 0.85}, {"E-H2-T1": 65000.0}, 1000.0)
            self.assertEqual(len(v["pos"]), 1)
            p = v["pos"][0]
            self.assertEqual((p["q"], p["k"]), (5.0, 65000.0))
            self.assertAlmostEqual(p["pnl"], 5 * (0.85 - 0.81))
            self.assertAlmostEqual(v["realized"], rec["pnl"])
            self.assertAlmostEqual(v["equity"], 1000.0 + rec["pnl"] - sh.rows[0]["fee"] + 5 * (0.85 - 0.81))
            settled = [t for t in v["trades"] if t["kind"] == "settle"]
            self.assertEqual(sorted(t["result"] for t in settled), ["yes", "yes"])   # the market's result, as the book keeps it
            self.assertEqual(sum(t["kind"] == "fill" for t in v["trades"]), 3)
            self.assertEqual(v["curve"][-1][1], round(1000.0 + rec["pnl"], 2))

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

    def test_the_variant_table_is_the_only_list_and_the_readme_keeps_up(self):
        import re
        from tern import dash, shadow
        self.assertEqual(list(shadow.DESC), shadow.ORDER)
        self.assertFalse(set(shadow.VARIANTS) & set(shadow.RETIRED))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md")) as fh:
            readme = fh.read()
        section = readme.split("\n## Variants\n", 1)[1].split("\n## ", 1)[0]
        rows = re.findall(r"^\| `(\w+)` \|", section, re.M)                 # the README's variants table
        self.assertEqual(rows, shadow.ORDER)
        for name in shadow.RETIRED:
            self.assertIn(f"`{name}`", readme)
        # the help's lists are filled from the table, not typed in again
        prose = " ".join(p for _, p in dash.HELP)
        self.assertIn("{variants}", prose); self.assertIn("{retired}", prose)
        for name in list(shadow.VARIANTS) + list(shadow.RETIRED):
            self.assertNotIn(f"  {name} ", prose)
        s = dash.Screen.__new__(dash.Screen)                               # help_lines needs only these three
        s.st, s.raw, s.P = {}, {}, type("P", (), {"__call__": lambda *a, **k: 0, "c": lambda *a: 0, "grey": 0})()
        text = "\n".join(l if isinstance(l, str) else "".join(t for t, _ in l) for l in s.help_lines(120))
        for name, desc in list(shadow.DESC.items()) + list(shadow.RETIRED.items()):
            self.assertIn(f"{name:<8} {desc}", text)

    def test_vol_is_realized_bp_a_minute_and_none_with_gaps(self):
        import math
        from tern.shadow import vol
        rows = [[60.0 * i, 100.0 * math.exp(0.001 * (i % 2))] for i in range(70)]   # ±10 bp every minute
        self.assertAlmostEqual(vol(rows, 70 * 60), 10.0, places=6)
        self.assertIsNone(vol(rows[:30], 70 * 60))                                # under an hour of minutes
        gap = rows[:20] + [[r[0] + 600, r[1]] for r in rows[20:]]                 # ten missing minutes
        self.assertIsNone(vol(gap, 80 * 60))

    def test_calm_skips_a_slot_when_the_coin_is_wild_or_unknown(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        S = Strategy(series=["E"])
        ms = [self.m("E-T1", 0.92, 0.93, 1)]
        for v, want in ((5.0, 1), (9.0, 0), (None, 0)):
            with tempfile.TemporaryDirectory() as d:
                sh = Shadow(d)
                sh.step("E", 1000.0, ms, S, 1000.0 - 25 * 60 + 1, vol=v)
                self.assertEqual(sum(r["v"] == "calm" for r in sh.rows), want, v)
                self.assertEqual(sum(r["v"] == "base" for r in sh.rows), 1)      # the others never look at vol

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

    def test_a_slot_that_found_nothing_is_not_run_twice(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy(series=["KXBTCD"])
            now = 1_000_000.0
            for h in range(8):                                             # eight hours that are over
                sh.slots[f"KXBTCD-OLD{h}"] = [25]; sh.closes[f"KXBTCD-OLD{h}"] = now - 3600 * (h + 1)
            close = now + 25 * 60 - 5
            sh.step("KXBTCD-NOW", close, [], S, now)                       # the 25m slot: nothing in band, no rows
            self.assertEqual(sh.slots["KXBTCD-NOW"], [25])
            sh.settle({"KXBTCD-OLD0-T1": True}, now + 10)                  # a late settlement of an old hour
            self.assertEqual(sorted(sh.slots), ["KXBTCD-NOW", "KXBTCD-OLD0"])   # old hours go, the one trading stays
            sh.step("KXBTCD-NOW", close, [self.m("KXBTCD-NOW-T1", 0.92, 0.93, 1)], S, now + 11)
            self.assertEqual(sh.rows, [])                                  # still the same slot: no second look

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


class Profiles(unittest.TestCase):
    """Profiles: whole copies of the desk on paper, each running every variant under its own settings."""

    def m(self, t, bid, ask, k):
        return {"ticker": t, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
                "yes_bid_size_fp": "50", "yes_ask_size_fp": "40", "floor_strike": k, "status": "active"}

    def make(self, d, name="Profile 2", **strat):
        from tern import profiles
        from tern.config import Config, Strategy
        return profiles.create(d, name, Config(strategy=Strategy(**strat)), 0.07)

    def test_create_copies_settings_and_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.make(d, band_lo=0.8)
            self.assertEqual(p.id, "profile-2")
            self.assertEqual(p.name, "Profile 2")
            self.assertEqual(p.cfg.strategy.band_lo, 0.8)
            self.assertEqual(p.log.sv, 1)
            self.assertEqual(p.shadow.rows, [])
            self.assertTrue(p.enabled)
            for f in ("profile.json", "config.json", "settings-log.jsonl"):
                self.assertTrue(os.path.exists(os.path.join(d, "profiles", "profile-2", f)), f)

    def test_slug_is_safe_and_unique(self):
        from tern.profiles import slug
        self.assertEqual(slug("../x/", set()), "x")
        self.assertEqual(slug("", set()), "profile")
        self.assertEqual(slug("!!!", set()), "profile")
        self.assertEqual(slug("A b", {"a-b"}), "a-b-2")
        self.assertEqual(slug("A b", {"a-b", "a-b-2"}), "a-b-3")

    def test_set_bumps_sv_only_for_record_fields(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p = self.make(d)
            self.assertEqual(p.set("strategy.band_lo", 0.82), (0.75, 0.82))
            self.assertEqual(p.log.sv, 2)
            p.set("strategy.variant", "late")
            self.assertEqual(p.log.sv, 2)
            self.assertEqual(len(p.log.lines()), 3)                           # created, band low, variant
            with self.assertRaises(ValueError):
                p.set("strategy.band_lo", 0.99)                               # not below band high (0.95)
            with self.assertRaises(ValueError):
                p.set("strategy.band_hi", 0.80)                               # not above band low (0.82)
            with self.assertRaises(ValueError):
                p.set("strategy.variant", "nope")
            with self.assertRaises(ValueError):
                p.set("live.capital_usd", 50)                                 # no live settings on a profile
            with open(os.path.join(p.dir, "config.json")) as fh:
                self.assertEqual(json.load(fh)["strategy"]["band_lo"], 0.82)

    def test_slots_and_coins_need_one_on(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.make(d)
            for key, bad in (("strategy.minutes", []), ("strategy.series", []), ("strategy.minutes", [12]),
                             ("strategy.series", ["KXFAKED"])):
                with self.assertRaises(ValueError):
                    p.set(key, bad)
            p.set("strategy.minutes", [15, 10])
            self.assertEqual(p.cfg.strategy.minutes, [15, 10])

    def test_hand_edit_is_logged_and_bad_edit_kept_out(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p = self.make(d)
            path = os.path.join(p.dir, "config.json")
            with open(path) as fh:
                c = json.load(fh)
            c["strategy"]["band_hi"] = 0.92
            with open(path, "w") as fh:
                json.dump(c, fh)
            self.assertEqual(p.reload_cfg(), [("strategy.band_hi", 0.95, 0.92)])
            self.assertEqual(p.log.sv, 2)
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(p.reload_cfg(), [])
            self.assertEqual(p.cfg.strategy.band_hi, 0.92)

    def test_disabled_profile_takes_no_entries(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.make(d, series=["E"])
            p.enabled = False
            p.step("E", 1000.0, [self.m("E-T1", 0.92, 0.93, 1)], 1000.0 - 25 * 60 + 1, None)
            self.assertEqual(p.shadow.rows, [])
            p.enabled = True
            p.step("E", 1000.0, [self.m("E-T1", 0.92, 0.93, 1)], 1000.0 - 25 * 60 + 2, None)
            self.assertEqual({r["sv"] for r in p.shadow.rows}, {1})

    def test_restart_keeps_slots(self):
        from tern.profiles import Profile
        with tempfile.TemporaryDirectory() as d:
            p = self.make(d, series=["E"])
            ms = [self.m("E-T1", 0.92, 0.93, 1)]
            p.step("E", 1000.0, ms, 1000.0 - 25 * 60 + 1, None)
            n = len(p.shadow.rows)
            p.shadow.save()
            again = Profile.load(p.dir, 0.07)
            again.step("E", 1000.0, ms, 1000.0 - 25 * 60 + 5, None)
            self.assertEqual(len(again.shadow.rows), n)

    def test_unreadable_folder_is_skipped(self):
        from tern import profiles
        with tempfile.TemporaryDirectory() as d:
            self.make(d)
            bad = os.path.join(d, "profiles", "bad")
            os.makedirs(bad)
            with open(os.path.join(bad, "profile.json"), "w") as fh:
                fh.write("{bad")
            self.assertEqual([p.id for p in profiles.load_all(d, 0.07)], ["profile-2"])
            self.assertIn("bad", profiles.load_errors)

    def test_max_profiles(self):
        from tern import profiles
        with tempfile.TemporaryDirectory() as d:
            for i in range(profiles.MAX_PROFILES):
                self.make(d, f"p{i}")
            with self.assertRaises(ValueError):
                self.make(d, "one too many")

    def test_report_profiles(self):
        import contextlib, io, json
        from unittest import mock
        from tern import __main__ as cli, config as cfgmod
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "RUNTIME", d):
            p = self.make(d, series=["E"])
            row = lambda sv, pnl: json.dumps({"v": "base", "ts": 1.0, "event": f"E-{sv}", "close": 2.0 + sv, "ticker": f"E-{sv}-T1",
                                              "side": "yes", "n": 5, "cost": 0.9, "fee": 0.02, "slot": 25, "pnl": pnl, "sv": sv})
            p.set("strategy.band_lo", 0.82)
            with open(os.path.join(p.dir, "shadow-settled.jsonl"), "w") as fh:
                fh.write(row(1, 0.48) + "\n" + row(2, -4.52) + "\n")
            def run(*argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(["report", *argv])
                return code, out.getvalue()
            code, out = run("--profile", "Profile 2")
            self.assertEqual(code, 0)
            self.assertIn("settings 1", out)
            self.assertIn("settings 2", out)
            self.assertIn("band low 75¢ → 82¢", out)
            self.assertIn("+0.48", out)
            self.assertIn("-4.52", out)
            code, out = run("--profiles")
            self.assertEqual(code, 0)
            self.assertIn("profile 1", out)
            self.assertIn("Profile 2", out)
            code, out = run("--profile", "nope")
            self.assertEqual(code, 2)
            self.assertIn("Profile 2", out)                                    # the known ones
            from tern import profiles
            profiles.delete(d, p.id)
            code, out = run("--deleted")
            self.assertEqual(code, 0)
            self.assertIn("Profile 2", out)

    def test_delete_moves_folder(self):
        from tern import profiles
        with tempfile.TemporaryDirectory() as d:
            p = self.make(d)
            to = profiles.delete(d, p.id)
            self.assertTrue(os.path.isdir(to))
            self.assertEqual(os.path.dirname(to), os.path.join(d, "profiles-deleted"))
            self.assertFalse(os.path.exists(p.dir))
            for bad in ("../..", "", "nope"):
                with self.assertRaises(ValueError):
                    profiles.delete(d, bad)


class EngineProfiles(unittest.TestCase):
    """The engine runs every enabled profile beside profile 1, on the same snapshot, each isolated."""
    T = "KXBTCD-E1-T100"

    def desk(self, d, bid="0.80", ask="0.81", close=600.0):
        import time
        from tern.engine import Desk
        k = Desk(d)
        now = time.time()
        m = {"ticker": self.T, "floor_strike": 100.0, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
             "yes_bid_size_fp": "50", "yes_ask_size_fp": "50", "status": "active"}
        k.events["KXBTCD"] = {"event": "KXBTCD-E1", "title": "", "close": now + close, "markets": [m], "fetched": now}
        k.meta[self.T] = {"event": "KXBTCD-E1", "strike": 100.0, "close": now + close, "series": "KXBTCD"}
        return k

    def profile(self, k, name="profile 2", **strat):
        from tern import profiles
        p = profiles.create(k.runtime, name, k.cfg, k.cfg.taker_fee)
        for key, v in strat.items():
            p.set("strategy." + key, v)
        k._profiles_reload(force=True)
        return next(x for x in k.profiles if x.id == p.id)

    def test_profile_trades_same_snapshot_with_its_own_settings(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, bid="0.58", ask="0.60")
            p = self.profile(k, band_lo=0.55, band_hi=0.65)
            k._shadow()
            mine = [r for r in p.shadow.rows if r["v"] == "base"]
            self.assertEqual(len(mine), 1)
            self.assertEqual(mine[0]["sv"], p.log.sv)
            self.assertEqual([r for r in k.shadow.rows if r["v"] == "base"], [])   # 59c is outside 75-95

    def test_profile1_records_untouched_by_profiles(self):
        def run(d, with_profile):
            k = self.desk(d)
            if with_profile:
                self.profile(k, band_lo=0.55, band_hi=0.65)
            k._shadow(); k._auto()
            return k, sorted((r["v"], r["ticker"], r["slot"], r["n"], r["cost"]) for r in k.shadow.rows)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ka, ra = run(a, False)
            kb, rb = run(b, True)
            self.assertTrue(ra)
            self.assertEqual(ra, rb)
            self.assertEqual(ka.paper.pos.keys(), kb.paper.pos.keys())
            self.assertEqual(set(os.listdir(b)) - set(os.listdir(a)), {"profiles"})

    def test_broken_profile_does_not_stop_the_step(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            p = self.profile(k)
            def boom(*a, **kw):
                raise RuntimeError("boom")
            p.step = boom
            k._shadow(); k._shadow(); k._auto()
            self.assertTrue(k.shadow.rows)                                     # profile 1 still recorded
            self.assertTrue(k.paper.pos)                                       # and its paper book traded
            errs = [m for _, lvl, m in [(x[0], x[1], x[2]) for x in k.logs] if "profile 2" in m and "boom" in m]
            self.assertEqual(len(errs), 1)                                     # logged once, not every step

    def test_settle_reaches_every_profile(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            p = self.profile(k)
            k._shadow()
            self.assertTrue(p.shadow.rows)
            k.shadow.rows = []                                                 # profile 1 has nothing left to settle
            k.closed["KXBTCD-E1"] = {"series": "KXBTCD", "close": time.time() - 120}
            k.pub.event_markets = lambda e: [{"ticker": self.T, "result": "", "status": "active"}]
            k._shadow()
            self.assertIn("KXBTCD-E1", k.closed)                              # profile 2 still needs it
            k.closed["KXBTCD-E1"]["tried"] = 0
            k.pub.event_markets = lambda e: [{"ticker": self.T, "result": "yes", "status": "finalized", "floor_strike": 100.0}]
            k._shadow()
            self.assertEqual(p.shadow.rows, [])
            self.assertEqual(p.shadow.summary(["base"])[0]["entries"], 1)
            self.assertNotIn("KXBTCD-E1", k.closed)

    def test_profile_coins_join_the_fetch(self):
        from unittest import mock
        from tern import shadow
        with tempfile.TemporaryDirectory() as d, mock.patch.object(shadow, "all_coins", lambda S, own=None: list(S.series)):
            k = self.desk(d)                                                   # (multi makes every coin wanted today)
            self.assertNotIn("KXETHD", k._wanted())
            p = self.profile(k, series=["KXETHD"])
            self.assertIn("KXETHD", k._wanted())
            p.set("enabled", False)
            self.assertNotIn("KXETHD", k._wanted())                           # a profile that is off fetches nothing

    def test_profile1_settings_are_logged(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertEqual(k.slog.sv, 1)
            k._command({"op": "set", "key": "strategy.contracts", "value": 7})
            self.assertEqual(k.slog.sv, 2)
            k._command({"op": "set", "key": "live.capital_usd", "value": 50})
            self.assertEqual(k.slog.sv, 2)                                     # not a record field: logged only
            k._command({"op": "series", "series": "KXETHD"})
            self.assertEqual(k.slog.sv, 2)                                     # a new variant, not a settings period
            self.assertEqual((k.cfg.strategy.variant, k.cfg.strategy.series), ("base-2", ["KXBTCD"]))
            path = os.path.join(d, "config.json")
            with open(path) as fh:
                c = json.load(fh)
            c["strategy"]["band_lo"] = 0.80                                    # a hand edit
            with open(path, "w") as fh:
                json.dump(c, fh)
            k._reload_cfg(force=True)
            self.assertEqual(k.slog.sv, 3)
            self.assertEqual([x["key"] for x in k.slog.lines()],
                             ["strategy.contracts", "live.capital_usd", "variant", "rule.series", "strategy.band_lo"])
            k._shadow()
            self.assertEqual({r.get("sv") for r in k.shadow.rows}, {3})


class ProfileReviewFixes(unittest.TestCase):
    """Findings from the final review: a broken profile must never stop or mislead profile 1 or itself."""
    T, desk, profile = EngineProfiles.T, EngineProfiles.desk, EngineProfiles.profile

    def test_unreadable_profile_config_is_skipped_not_defaulted(self):
        from tern import profiles
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            p = self.profile(k, band_lo=0.82, band_hi=0.90)
            with open(os.path.join(p.dir, "config.json"), "w") as fh:
                fh.write("{oops")
            self.assertEqual(profiles.load_all(d, 0.07), [])                   # skipped on a restart, not run on defaults
            self.assertIn(p.id, profiles.load_errors)

    def test_bad_view_profile_does_not_stop_the_step(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.profile(k)
            with open(os.path.join(k.fast, "view.json"), "w") as fh:
                json.dump({"series": "KXBTCD", "ticker": None, "profile": []}, fh)
            k._read_view()
            self.assertNotIn("profile", k.view)
            with open(os.path.join(k.fast, "view-web.json"), "w") as fh:
                json.dump({"series": [], "tickers": [], "profiles": [[], {}]}, fh)
            k._read_view()
            self.assertEqual(k.web["profiles"], [])

    def test_bad_profile_rows_do_not_stop_the_engine(self):
        import json
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            p = self.profile(k)
            with open(os.path.join(p.dir, "shadow.json"), "w") as fh:
                json.dump({"rows": [{"v": "base"}], "slots": {}}, fh)
            k2 = Desk(d)                                                       # starts
            self.assertEqual(next(x for x in k2.profiles if x.id == p.id).shadow.rows, [])
            k2.events = k.events; k2.meta = k.meta
            k2._shadow(); k2._status()

    def test_status_does_not_redo_profile_summaries_every_step(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            a = self.profile(k, "a")
            k._status()
            with mock.patch.object(a.shadow, "summary", wraps=a.shadow.summary) as spy:
                k._status(); k._status()
                self.assertEqual(spy.call_count, 0)                            # nothing changed: cached
                k._shadow()                                                    # a new entry: recomputed once
                k._status(); k._status()
                self.assertEqual(spy.call_count, 1)
            import json
            with open(os.path.join(k.fast, "status.json")) as fh:
                st = json.load(fh)
            row = next(r for r in st["profiles"] if r["id"] == "a")
            self.assertTrue(all("curve" not in v for v in row["variants"]))   # no one is viewing it


class ProfileCommands(unittest.TestCase):
    """The screen's profile commands, and what status.json tells it."""
    T, desk, profile = EngineProfiles.T, EngineProfiles.desk, EngineProfiles.profile

    def status(self, k):
        import json
        k._status()
        with open(os.path.join(k.fast, "status.json")) as fh:
            return json.load(fh)

    def view(self, k, **v):
        import json
        with open(os.path.join(k.fast, "view.json"), "w") as fh:
            json.dump({"series": "KXBTCD", "ticker": None, **v}, fh)
        k._read_view()

    def test_profile_commands(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            msg = k._command({"op": "profile_new", "name": "try 80"})
            self.assertIn("try 80", msg)
            p = next(x for x in k.profiles if x.name == "try 80")
            self.assertIn("off", k._command({"op": "profile_enable", "profile": p.id, "on": False}))
            self.assertFalse(p.enabled)
            self.assertEqual(p.log.sv, 1)                                      # on/off is not a settings period
            msg = k._command({"op": "profile_set", "profile": p.id, "key": "strategy.band_lo", "value": 0.82})
            self.assertIn("new variant base-2", msg)                           # a rule change is a new variant
            self.assertEqual((p.cfg.strategy.band_lo, p.cfg.strategy.variant), (0.75, "base-2"))
            self.assertEqual(p.shadow.own.rule("base-2")["band_lo"], 0.82)
            self.assertIn("not changed", k._command({"op": "profile_set", "profile": p.id, "key": "strategy.band_lo", "value": 0.99}))
            self.assertIn("settings 2", k._command({"op": "profile_set", "profile": p.id, "key": "strategy.contracts", "value": 9}))
            for c in ({"op": "profile_set", "profile": "1", "key": "strategy.band_lo", "value": 0.8},
                      {"op": "profile_enable", "profile": "1", "on": False}, {"op": "profile_delete", "profile": "1"}):
                self.assertIn("profile 1", k._command(c))
            self.assertEqual(k.cfg.strategy.band_lo, 0.75)                     # profile 1 untouched
            self.assertIn("no profile", k._command({"op": "profile_set", "profile": "nope", "key": "enabled", "value": True}))
            msg = k._command({"op": "profile_delete", "profile": p.id})
            self.assertIn("profiles-deleted", msg)
            self.assertEqual(k.profiles, [])
            self.assertTrue(os.listdir(os.path.join(d, "profiles-deleted")))

    def test_pviews_only_for_viewed_profiles(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            a = self.profile(k, "a", band_lo=0.80)
            self.profile(k, "b")
            self.view(k, profile=a.id)
            st = self.status(k)
            self.assertEqual([p["id"] for p in st["profiles"]], ["1", "a", "b"])
            self.assertEqual(st["profiles"][0]["name"], "profile 1")
            self.assertEqual(st["profiles"][1]["rule"]["band_lo"], 0.80)               # the profiles box's rule line
            self.assertEqual(st["profiles"][0]["rule"]["band_lo"], 0.75)
            self.assertEqual(list(st["pviews"]), ["a"])
            pv = st["pviews"]["a"]
            self.assertNotIn("rule.band_lo", pv["settings"])                           # a rule is in its variant's row
            base = next(v for v in st["profiles"][1]["variants"] if v["name"] == "base")
            self.assertEqual(base["rule"]["band_lo"], 0.80)
            self.assertIn("base", pv["views"])
            self.assertEqual(pv["strategy"]["band_lo"], 0.80)
            self.assertEqual(pv["views"]["base"]["start"], a.cfg.paper_capital)
            self.assertIn("checks", pv["gate"])

    def test_deleted_viewed_profile_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            a = self.profile(k, "a")
            self.view(k, profile=a.id)
            self.assertEqual(k.view["profile"], a.id)
            k._command({"op": "profile_delete", "profile": a.id})
            self.view(k, profile=a.id)
            self.assertIsNone(k.view.get("profile"))
            self.assertEqual(self.status(k)["pviews"], {})


class Renames(unittest.TestCase):
    """N: profiles and variants get new names; a variant's record keeps its own."""
    T, desk, profile = EngineProfiles.T, EngineProfiles.desk, EngineProfiles.profile
    status = ProfileCommands.status

    def test_rename_profiles(self):
        from tern import profiles
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            p = self.profile(k, "profile 2")
            self.assertIn("profile 2 is now wide band", k._command({"op": "rename", "profile": p.id, "name": "  wide   band "}))
            self.assertIn("profile 1 is now the desk", k._command({"op": "rename", "profile": "1", "name": "the desk"}))
            self.assertIn("not renamed", k._command({"op": "rename", "profile": p.id, "name": "  "}))
            self.assertIn("not renamed", k._command({"op": "rename", "profile": p.id, "name": "x" * 41}))
            self.assertIn("no profile", k._command({"op": "rename", "profile": "nope", "name": "x"}))
            names = {r["id"]: r["name"] for r in self.status(k)["profiles"]}
            self.assertEqual(names, {"1": "the desk", p.id: "wide band"})
            self.assertEqual(profiles.Profile.load(p.dir, 0.07).name, "wide band")   # kept on disk, same folder
            self.assertEqual(p.id, "profile-2")
            self.assertEqual(p.log.sv, 1)                                      # a name is not a settings change
            self.assertEqual([l["key"] for l in p.log.lines()][-1], "name")
            k2 = self.desk(d)                                                  # a restart keeps profile 1's name
            self.assertEqual(k2.names.profile, "the desk")
            self.assertIn("the desk is now profile 1", k2._command({"op": "rename", "profile": "1", "name": ""}))

    def test_rename_variant_is_display_only(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("band90 is now high", k._command({"op": "rename", "variant": "band90", "name": "high"}))
            self.assertIn("unknown variant", k._command({"op": "rename", "variant": "stop20", "name": "x"}))
            self.assertIn("already base's name", k._command({"op": "rename", "variant": "late", "name": "BASE"}))
            self.assertIn("already band90's name", k._command({"op": "rename", "variant": "late", "name": "High"}))
            self.assertIn("at most 12", k._command({"op": "rename", "variant": "late", "name": "x" * 13}))
            self.assertIn("base → high", k._command({"op": "variant", "name": "band90"}))
            self.assertIn("high → base", k._command({"op": "variant", "name": "base"}))
            st = self.status(k)
            self.assertEqual(st["variant_names"], {"band90": "high"})
            self.assertIn("band90", [v["name"] for v in st["variants"]])     # the record keeps its key
            from tern import config
            k._command({"op": "variant", "name": "band90"})
            self.assertEqual(config.load(d).strategy.variant, "band90")      # config.json keeps the key too
            self.assertEqual(self.desk(d).names.variant("band90"), "high")    # kept across a restart
            self.assertIn("is now band90 again", k._command({"op": "rename", "variant": "band90", "name": ""}))
            self.assertEqual(self.status(k)["variant_names"], {})

    def test_bad_names_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "names.json"), "w") as fh:
                fh.write('{"profile": 5, "variants": {"nope": "x", "base": "  ", "late": "slow"}}')
            k = self.desk(d)
            self.assertEqual((k.names.profile, k.names.variants), ("profile 1", {"late": "slow"}))


class ScreenProfiles(unittest.TestCase):
    """The terminal screen's profiles box, viewing a profile, and its settings, against a real engine's status."""
    T, desk, profile = EngineProfiles.T, EngineProfiles.desk, EngineProfiles.profile

    def setup(self, d):
        import curses
        from unittest import mock
        from tern import dash, web
        k = self.desk(d, bid="0.58", ask="0.60")
        k.seeded.add(self.T)                                                   # no network for its history
        p = self.profile(k, "profile 2", band_lo=0.55, band_hi=0.65)
        k._shadow()
        with mock.patch.object(dash, "Pal", web.WebPal):
            scr = dash.Screen(web.VTerm(40, 120), d)
        self.k, self.p, self.scr, self.curses = k, p, scr, curses
        self.sync()
        return k, p, scr

    def sync(self):
        self.k._read_view(); self.k._status(); self.scr.read(); self.scr.draw()

    def text(self):
        return "\n".join("".join(r) for r in self.scr.s.ch)

    def press(self, *keys):
        for c in keys:
            self.scr.key(ord(c) if isinstance(c, str) else c)
        self.sync()

    def cmds(self):
        import json
        out = []
        for n in sorted(os.listdir(self.scr.cmd_dir)):
            with open(os.path.join(self.scr.cmd_dir, n)) as fh:
                out.append(json.load(fh))
            os.remove(os.path.join(self.scr.cmd_dir, n))
        return out

    def view2(self):
        c = self.curses
        self.press("p")
        self.assertEqual(self.scr.page, 2)
        self.scr.focus = "profiles"
        self.press(c.KEY_DOWN, 10)

    def test_profiles_box_and_view(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p")
            self.scr.focus = "profiles"
            self.assertIn("profiles", self.text())
            self.assertIn("profile 1", self.text())
            self.assertIn("profile 2", self.text())
            self.press(self.curses.KEY_DOWN, 10)
            self.assertEqual(scr.profile, p.id)
            self.assertEqual(k.view.get("profile"), p.id)                     # view.json told the engine
            self.assertEqual(scr.st["profile_name"], "profile 2")
            self.assertEqual([x["t"] for x in scr.st["paper"]["pos"]], [self.T])   # its base record as a book
            self.assertEqual(scr.st["strategy"]["band_lo"], 0.55)
            self.assertIn("profile 2", "".join(scr.s.ch[39]))                  # the status line says so
            self.press(10)                                                     # enter on profile 1's row? no: still on 2
            self.press(self.curses.KEY_UP, 10)
            self.assertIsNone(scr.profile)
            self.assertNotIn("profile_name", scr.st)

    def test_N_renames_the_focused_profile_or_variant(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); self.cmds()
            scr.focus = "ladder"
            self.press("N")
            self.assertIn("N renames", scr.flash)
            scr.focus = "profiles"
            self.press(self.curses.KEY_DOWN, "N")
            self.assertIn("rename profile 2", self.text())
            self.press(21, *"wide", 10)                                       # ctrl+u, then a new name
            [c] = self.cmds()
            self.assertEqual((c["op"], c["profile"], c["name"]), ("rename", p.id, "wide"))
            k._command(c); self.sync()
            self.assertIn("wide", self.text())
            scr.focus = "variants"
            scr.var_sel = "band90"
            self.press("N", *"hi", 10)                                        # typed after the old name
            [c] = self.cmds()
            self.assertEqual((c["op"], c["variant"], c["name"]), ("rename", "band90", "band90hi"))
            k._command(c); self.sync()
            self.assertIn("│   band90hi     0 ", self.text())                 # the row, its columns moved along
            self.press("N", 27)
            self.assertEqual(self.cmds(), [])                                  # esc sends nothing

    def test_settings_box_scrolls_on_a_short_screen_and_shows_the_web_service(self):
        from unittest import mock
        from tern import dash, web
        with tempfile.TemporaryDirectory() as d, mock.patch.object(webctl, "systemctl", lambda *a: (0, "active")):
            k = self.desk(d); k.seeded.add(self.T)
            with mock.patch.object(dash, "Pal", web.WebPal):
                scr = dash.Screen(web.VTerm(31, 104), d)
            k._read_view(); k._status(); scr.read(); scr.key(ord("e"))
            text = lambda: "\n".join("".join(r) for r in scr.s.ch)
            scr.set_sel = [r.key for r in scr.set_rows()].index("web.port"); scr.draw()
            self.assertIn("▸ port", text())
            self.assertIn("web screen now: running · http", text())
            self.assertIn("sudo ufw allow PORT/tcp", text())               # its description, pinned below
            scr.set_sel = 0; scr.draw()
            self.assertIn("▸ most at risk at once", text())
            self.assertIn("LIVE", text())
            self.assertNotIn("web screen now", text())
            scr.set_sel = [r.key for r in scr.set_rows()].index("web.port")
            scr.key(__import__("curses").KEY_RIGHT)
            [c] = [json.load(open(os.path.join(scr.cmd_dir, n))) for n in os.listdir(scr.cmd_dir)]
            self.assertEqual((c["op"], c["key"], c["value"]), ("set", "web.port", 8788))

    def test_d_picks_the_live_profile_and_asks_while_armed(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); self.cmds()
            scr.focus = "profiles"
            self.press(self.curses.KEY_DOWN, "d")
            [c] = self.cmds()
            self.assertEqual(c["op"], "live_profile"); self.assertEqual(c["profile"], p.id)   # not armed: no question
            k._command(c); self.sync()
            row = next(l for l in self.text().splitlines() if "profile 2" in l and " on " in l)
            self.assertRegex(row, r"^│ [● ]L profile 2")                       # L marks the live profile
            k.cfg.live.enabled = True; self.sync()                             # armed
            self.press(self.curses.KEY_UP, "d")
            self.assertIn("SWITCH LIVE PROFILE", self.text())
            self.assertEqual(self.cmds(), [])
            self.press(*"live", 10)
            [c] = self.cmds()
            self.assertEqual((c["op"], c["profile"]), ("live_profile", "1"))
            k._command({"op": "live_profile", "profile": p.id}); self.sync()
            self.press(self.curses.KEY_DOWN, 10)                               # view the live profile
            self.assertEqual(scr.profile, p.id); self.cmds()
            self.press(9, "r")                                                 # ETH into its coins: asks first
            self.assertIn("LIVE PROFILE", self.text())
            self.press(27)
            self.assertEqual(self.cmds(), [])
            self.press("r", *"live", 10)
            [c] = self.cmds()
            self.assertEqual((c["op"], c["key"]), ("variant_rule", "series"))

    def test_profile_settings_send_profile_set(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.view2(); self.cmds()
            self.press("e")
            self.assertIn("band low", self.text())
            self.assertNotIn("most at risk", self.text())                      # no live rows on a profile
            rows = [r.key for r in scr.set_rows()]
            self.assertIn("built in: a change makes a new variant", self.text())
            scr.set_sel = rows.index("rule.band_lo")
            self.press(self.curses.KEY_RIGHT)
            c = self.cmds()
            self.assertEqual([(x["op"], x["profile"], x["key"], "variant" in x) for x in c],
                             [("variant_rule", p.id, "band_lo", False)])       # no name: the one in use when it arrives
            self.assertAlmostEqual(c[0]["value"], 0.56)
            scr.set_sel = rows.index("rule.minutes")
            self.press(" ")                                                    # the first slot (30) turns on
            c = self.cmds()
            self.assertEqual(c[0]["key"], "minutes")
            self.assertEqual(c[0]["value"], [30, 25, 20, 15, 10])
            scr.set_sel = rows.index("strategy.variant")
            self.press(self.curses.KEY_RIGHT)
            self.assertEqual(self.cmds()[0]["value"], "band90")

    def test_desk_settings_show_under_a_profile_and_change_the_desk(self):
        from tern import config as cfgmod
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.view2(); self.cmds()
            self.press("e")
            rows = [r.key for r in scr.set_rows()]
            for key in ("alerts", "web.enabled", "web.port", "web.network"):
                self.assertEqual(rows.count(key), 1, key)
                self.assertEqual(scr.set_value(key), cfgmod.get_setting(k.cfg, key))   # the desk's value
            self.assertFalse([key for key in rows if key.startswith(("live.", "gate."))])
            self.assertEqual(rows[-1], "screen.log")
            scr.set_sel = rows.index("web.port"); scr.draw()
            self.assertIn("web screen now", self.text())
            self.assertNotIn("older than this screen", self.text())
            self.press(self.curses.KEY_RIGHT)
            [c] = self.cmds()
            self.assertEqual((c["op"], c["key"], c["value"]), ("set", "web.port", 8788))   # not profile_set
            scr.set_sel = rows.index("alerts")
            self.press(self.curses.KEY_RIGHT)
            [c] = self.cmds()
            self.assertEqual((c["op"], c["key"], c["value"]), ("set", "alerts", not k.cfg.alerts))

    def test_profile_box_keys(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); self.scr.focus = "profiles"; self.press(self.curses.KEY_DOWN); self.cmds()
            self.press(" ")
            self.assertEqual([(x["op"], x["profile"], x["on"]) for x in self.cmds()], [("profile_enable", p.id, False)])
            self.press("n", "t", "r", "y", 10)
            self.assertEqual([(x["op"], x["name"]) for x in self.cmds()], [("profile_new", "try")])
            self.press("D", "n", "o", 10)
            self.assertEqual(self.cmds(), [])                                  # not typed exactly: kept
            self.press("D", *"delete", 10)
            self.assertEqual([(x["op"], x["profile"]) for x in self.cmds()], [("profile_delete", p.id)])
            self.press(self.curses.KEY_UP, "D", *"delete", 10)
            self.assertEqual(self.cmds(), [])                                  # profile 1 has no delete

    def test_no_setting_change_before_the_profile_settings_arrive(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); self.scr.focus = "profiles"; self.press(self.curses.KEY_DOWN)
            scr.key(10)                                                        # view it; the engine hasn't answered yet
            self.assertTrue(scr.st.get("profile_wait"))
            self.cmds()
            for key in ("r",):
                scr.key(ord(key))
            scr.key(ord("e"))
            rows = [r.key for r in scr.set_rows()]
            for key, ch in (("rule.minutes", ord(" ")), ("rule.series", ord(" ")), ("rule.cap", self.curses.KEY_RIGHT),
                            ("rule.band_lo", self.curses.KEY_RIGHT), ("strategy.variant", self.curses.KEY_RIGHT)):
                scr.set_sel = rows.index(key)
                scr.key(ch)
            self.assertEqual(self.cmds(), [])
            self.assertIn("no value", scr.flash)

    def test_live_keys_refuse_on_profiles(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.view2(); self.cmds()
            for key in ("L", "R"):
                self.press(key)
                self.assertIsNone(scr.modal)
                self.assertIn("paper only", scr.flash)
            self.assertEqual(self.cmds(), [])

    def test_viewed_profile_gone_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.view2()
            k._command({"op": "profile_delete", "profile": p.id})
            self.sync()
            self.assertIsNone(scr.profile)
            self.assertIn("profile 1", scr.flash)


class Pages(unittest.TestCase):
    """Two fixed pages (p flips them) fitted to the screen, merged when it is big; a box's number zooms it."""
    T, desk, profile = EngineProfiles.T, EngineProfiles.desk, EngineProfiles.profile
    setup, sync, text, press, cmds = (ScreenProfiles.setup, ScreenProfiles.sync, ScreenProfiles.text,
                                      ScreenProfiles.press, ScreenProfiles.cmds)

    def size(self, H, W):
        self.scr.s.resize(H, W); self.sync()

    def rects_ok(self, H, W):
        r = self.scr.rects
        for n, (y, x, h, w) in r.items():
            self.assertTrue(y >= 0 and x >= 0 and y + h <= H - 1 and x + w <= W, (n, (y, x, h, w), H, W))
        cells = set()
        for n, (y, x, h, w) in r.items():
            mine = {(yy, xx) for yy in range(y, y + h) for xx in range(x, x + w)}
            self.assertFalse(cells & mine, n)
            cells |= mine
        return r

    def test_number_zooms_and_same_number_restores(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            H, W = scr.s.getmaxyx()
            scr.focus = "book"
            self.press("3")
            self.assertEqual(scr.layout(H, W), [("ladder", 0, 0, H - 1, W)])
            self.assertEqual(set(self.rects_ok(H, W)), {"ladder"})
            self.assertEqual(scr.focus, "ladder")
            self.assertIn("³ladder", self.text())                              # it keeps its number
            self.press("4")                                                     # another number: that box instead
            self.assertEqual(set(scr.rects), {"book"})
            self.press("4")
            self.assertEqual(set(scr.rects), {"spot", "contract", "ladder", "book", "log"})
            self.assertIsNone(scr.zoom)
            self.assertFalse(hasattr(scr, "hidden"))

    def test_numbers_follow_each_page(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            text = self.text()
            for n, name in zip("¹²³⁴⁵", ("spot", "contract", "ladder", "book", "log")):
                self.assertTrue(n in text and name in text, name)
            self.assertNotIn("equity ·", text)                                  # page 1 has no equity box
            self.press("6")
            self.assertIsNone(scr.zoom)
            self.assertIn("1-5", scr.flash)
            self.press("p")
            text = self.text()
            for n, name in zip("¹²³⁴", ("variants", "profiles", "equity", "log")):
                self.assertIn(n + name, text)
            for key, name in zip("1234", ("variants", "profiles", "equity", "log")):
                self.press(key)
                self.assertEqual(set(scr.rects), {name})
                self.press(key)
            self.press("5")
            self.assertIn("1-4", scr.flash)
            self.assertIsNone(scr.zoom)

    def test_zoomed_lists_stay_in_their_box(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.profile(k, "profile 3"); k._shadow()
            self.press("p")
            for H, W in ((30, 100), (33, 111)):
                self.size(H, W)
                for key in "12":
                    self.press(key)
                    self.rects_ok(H, W)
                    self.assertIn("base", self.text())
                    self.press(key)

    def test_focus_keys_while_zoomed_on_a_chart(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("1", 261)                                                # spot zoomed, then →
            self.assertIn("1 back", scr.flash)
            self.assertNotIn("brings one back", scr.flash)

    def test_p_while_zoomed_restores_and_flips(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("5", "p")                                                # log zoomed
            self.assertEqual((scr.page, scr.zoom), (2, None))
            self.assertEqual(set(scr.rects), {"variants", "profiles", "equity", "log"})

    def test_zoom_cleared_when_off_view(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.size(50, 150)
            self.press("5")                                                     # merged: 5 is variants
            self.assertEqual(set(scr.rects), {"variants"})
            self.size(33, 111)                                                  # page 1 has no variants box
            self.assertIsNone(scr.zoom)
            self.assertEqual(len(self.rects_ok(33, 111)), 5)

    def test_page2_fits_small_screens(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.profile(k, "profile 3"); k._shadow()
            self.press("p")
            n = len(scr.st.get("variants") or [])
            self.assertEqual(n, 10)
            for H, W in ((33, 111), (30, 100), (37, 151)):
                self.size(H, W)
                r = self.rects_ok(H, W)
                self.assertEqual(set(r), {"variants", "profiles", "equity", "log"}, (H, W))
                self.assertGreaterEqual(r["variants"][2], n + 3, (H, W))
                self.assertEqual(r["variants"][3], W)
                self.assertGreaterEqual(r["profiles"][2], 3 + 6, (H, W))
                self.assertEqual(r["profiles"][0], r["equity"][0], (H, W))           # side by side, not stacked
                self.assertGreaterEqual(r["profiles"][3], 70, (H, W))               # every column of the profiles table
                for name in ("calm", "band75", "base"):
                    self.assertIn(name, self.text())

    def test_page1_layout_at_small_size(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            for H, W in ((33, 111), (30, 100)):
                self.size(H, W)
                r = self.rects_ok(H, W)
                self.assertEqual(set(r), {"spot", "contract", "ladder", "book", "log"})
                if W < 111:
                    continue                                                # 100 wide: the book is 44, its tables cut
                y, x, h, w = r["book"]
                book = ["".join(scr.s.ch[yy][x:x + w]) for yy in range(y, y + h)]
                self.assertTrue(any("P&L closes│" in l for l in book), (W, book))  # the positions table's last column, whole
                self.assertFalse(any("¢/c│" in l or "win 8│" in l for l in book))

    def test_merged_when_big(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.size(50, 150)
            self.assertEqual(scr.view(50, 150), "merged")
            r = self.rects_ok(50, 150)
            self.assertEqual(set(r), {"spot", "contract", "ladder", "book", "variants", "profiles", "equity", "log"})
            self.press("p")
            self.assertIn("both pages fit", scr.flash)
            self.press("1", "p")                                                # zoomed: p can't flip, but 1 goes back
            self.assertIn("1 back", scr.flash)
            self.assertNotIn("every box is shown", scr.flash)
            self.press("1")
            self.press("8")
            self.assertEqual(scr.layout(50, 150), [("log", 0, 0, 49, 150)])
            self.size(33, 111)                                                  # log is on page 1 too: still zoomed
            self.assertEqual(scr.view(33, 111), "page1")
            self.assertEqual(set(scr.rects), {"log"})

    def test_old_prefs_load(self):
        import json
        from unittest import mock
        from tern import dash, web
        with tempfile.TemporaryDirectory() as d:
            def screen(prefs):
                with open(os.path.join(d, "dash.json"), "w") as fh:
                    json.dump(prefs, fh)
                with mock.patch.object(dash, "Pal", web.WebPal):
                    return dash.Screen(web.VTerm(33, 111), d)
            s = screen({"hidden": ["contract", "nope"], "slot3": "variants"})      # hiding is gone: all shown
            s.draw()
            self.assertEqual((s.page, s.zoom, len(s.rects)), (1, None, 5))
            s = screen({"page": 2, "hidden": {"page2": ["log", "spot"], "merged": "junk"}, "zoom": "log"})
            s.draw()
            self.assertEqual((s.page, s.zoom, len(s.rects)), (2, None, 4))      # the zoom is never loaded
            s = screen({"page": 7, "hidden": 5})
            self.assertEqual(s.page, 1)
            s.save_prefs()
            with open(os.path.join(d, "dash.json")) as fh:
                saved = json.load(fh)
            self.assertEqual(saved["page"], 1)
            self.assertNotIn("hidden", saved)
            self.assertNotIn("zoom", saved)
            self.assertNotIn("slot3", saved)

    def test_no_key_cycles(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            seen = []
            for _ in range(3):
                self.press("3")
                seen.append(sorted(scr.rects))
            self.assertEqual(seen, [["ladder"], sorted(["spot", "contract", "ladder", "book", "log"]), ["ladder"]])

    def test_enter_acts_on_focused_box(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); self.cmds()
            scr.focus, scr.var_sel = "variants", "band90"
            self.press(10)
            self.assertEqual([(c["op"], c.get("name")) for c in self.cmds()], [("variant", "band90")])
            self.assertIsNone(scr.profile)
            scr.focus, scr.prof_sel = "profiles", p.id
            self.press(10)
            self.assertEqual(scr.profile, p.id)
            self.assertEqual(self.cmds(), [])
            scr.focus = "log"
            self.press(10)
            self.assertIn("focus variants or profiles", scr.flash)
            self.assertEqual(self.cmds(), [])

    def test_page_hint_on_bottom_border(self):
        import curses
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            H, W = scr.s.getmaxyx()
            for page in (1, 2):
                scr.flash = ""; self.sync()
                row = "".join(scr.s.ch[H - 2])
                self.assertIn(f"page {page}/2", row)
                ly, lx, lh, lw = scr.rects["log"]
                self.assertEqual((ly + lh, lx, lw), (H - 1, 0, W))              # log across the bottom of both pages
                self.assertGreater(row.index(f"page {page}/2"), W - 16, row)    # the page hint at its right end
                self.assertNotIn("page", "".join(scr.s.ch[0]))
                self.assertNotIn("compare", self.text())
                self.assertNotIn(" trade ", self.text())
                self.assertFalse(any(a & curses.A_REVERSE for a in scr.s.at[H - 2]))   # nothing white above the status
                self.press("p")
            scr.say("hello"); self.sync()
            self.assertNotIn("page 1/2", "".join(scr.s.ch[H - 2]))             # a flash takes its place
            self.size(50, 150); scr.flash = ""; self.sync()
            self.assertNotIn("page ", "".join(scr.s.ch[48]))                  # one merged screen: no pages

    def test_zoom_hint_on_bottom_border(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            H, W = scr.s.getmaxyx()
            self.press("3"); scr.flash = ""; self.sync()
            row = "".join(scr.s.ch[H - 2])
            self.assertIn("─3 back─", row)
            self.assertGreater(row.index("3 back"), row.index("tab next coin"))  # at the right end, like the page hint
            self.assertNotIn("page 1/2", row)
            self.assertIn("tab next coin", row)                                 # the other boxes' hints moved here
            scr.say("hello"); self.sync()
            self.assertNotIn("3 back", "".join(scr.s.ch[H - 2]))               # a flash takes its place
            self.size(50, 150); self.press("8"); scr.flash = ""; self.sync()
            self.assertIn("─8 back─", "".join(scr.s.ch[48]))

    def test_watch_without_variants_box_says_page2(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("w")
            self.assertIn("page 2", scr.flash)
            self.press("p", "2", "w")                                           # profiles zoomed: no variants box
            self.assertIn("2 un-zooms", scr.flash)
            self.press("p", "1", "w")                                           # zoomed on page 1: un-zooming won't help
            self.assertIn("p for page 2", scr.flash)
            self.assertNotIn("un-zooms", scr.flash)

    def test_status_line_fits_every_width(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            ev = k.events["KXBTCD"]
            ev["close"] = time.time() + 1400; ev["fetched"] = time.time() - 120     # a slot ahead, a stale ladder
            for prof in (None, p.id):
                scr.profile, scr.watch = prof, "band90"
                scr.write_view(); self.sync()
                for H, W in ((30, 100), (33, 111), (37, 151)):
                    self.size(H, W)
                    row = "".join(scr.s.ch[H - 1])
                    self.assertRegex(row, r"\S  +● (engine )?ok( +\d\d:\d\d(:\d\d)?)? *$")   # the engine's state, whole, at the end
                    self.assertIn("ladder 2m", row)                                  # stale data is never dropped
                    self.assertIn("rule", row)
                    self.assertIn("in ", row)                                   # when the next slot is
                    self.assertIn("closes", row)

    def test_shadow_log_lines_print_as_columns(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            k._command({"op": "rename", "variant": "band90", "name": "high favs"})
            line = ("shadow: KXETHD-E1 settled · base +234.14 · band90 +98.04 · cap1 +71.67 · band80 +18.27 · late +28.65"
                    " · tight1 +4.96 · band75 -38.35 · early -38.79 · multi -1,132.43 · calm +47.12")
            for _ in range(4):                                                 # more than the box holds
                k.log(line)
            k.log("paper: after it"); scr.flash = ""; self.sync()
            rows = scr.log_rows([[1.0, "info", line], [2.0, "info", "shadow: odd line · no cells"]], 104)
            self.assertEqual(rows[0][:4], (1.0, "info", "shadow: KXETHD-E1 settled", 0))
            self.assertEqual([r[3] for r in rows], [0, 1, 2, 3, 0])             # 3 a row: 10 variants in 4 rows
            self.assertEqual([len(r[4]) for r in rows[:4]], [3, 3, 3, 1])
            self.assertEqual({len(r[4]) for r in scr.log_rows([[1.0, "info", line]], 400)}, {3, 1})   # 3 however wide
            self.assertEqual(rows[0][4][1], ("high favs ", "   +98.04"))       # display names, P&L right-aligned
            self.assertEqual(rows[-1][2:5], ("shadow: odd line · no cells", 0, None))   # not the shape: as it is
            text = self.text()
            self.assertNotIn("+234.14 · band90", text)
            lines = text.splitlines()
            first = next(l for l in lines if "shadow: KXETHD-E1 settled" in l)
            self.assertRegex(first, r"settled  base +\+234\.14   high favs +\+98\.04   cap1 +\+71\.67 +│$")
            under = lines[lines.index(first) + 1]
            self.assertNotRegex(under, r"\d\d:\d\d")                           # no time on the rows under it
            for a, b in (("base", "band80"), ("high favs", "late"), ("cap1", "tight1")):   # each under the one above
                self.assertEqual(first.index(a), under.index(b), (a, b))
            for r in scr.s.ch:                                                  # nothing runs past the box
                self.assertIn("".join(r)[-1], "│╯╮ ")
            scr.focus = "log"
            for _ in range(10):
                scr.scroll("log", -1)
            self.assertIsNotNone(scr.log_anchor)
            for _ in range(10):
                scr.scroll("log", 1)
            self.assertIsNone(scr.log_anchor)                                  # back to following new lines

    def test_page2_bottom_row_has_only_its_own_hints(self):
        from tern import themes
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            scr.P.set_theme(max(themes.THEMES, key=len))                        # the longest hint the bottom row moves
            self.press("p")
            for H, W in ((30, 100), (33, 111)):
                scr.flash = ""; self.size(H, W)
                row = "".join(scr.s.ch[H - 2])
                self.assertIn("page 2/2", row, (W, row))
                for page1 in ("tab next coin", "chart", "view", "Live", "Reset", "theme"):   # page 1's, not repeated
                    self.assertNotIn(page1, row, (W, row))

    def test_profile_keys_follow_the_focus(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); self.cmds()
            scr.focus, scr.prof_sel = "variants", p.id
            self.press(" ", "d")
            self.assertEqual(self.cmds(), [])
            self.assertIsNone(scr.modal)
            self.assertIn("profiles box's", scr.flash)
            self.press("n")
            self.assertEqual(scr.modal["type"], "vname")                       # n is a new variant there
            self.press(27)
            self.assertEqual(self.cmds(), [])
            scr.focus = "profiles"
            self.press(" ")
            self.assertEqual([c["op"] for c in self.cmds()], ["profile_enable"])

    def test_page_must_be_a_plain_number(self):
        import json
        from unittest import mock
        from tern import dash, web
        with tempfile.TemporaryDirectory() as d:
            for bad in (True, 1.0, 2.0, [], "2"):
                with open(os.path.join(d, "dash.json"), "w") as fh:
                    json.dump({"page": bad}, fh)
                with mock.patch.object(dash, "Pal", web.WebPal):
                    s = dash.Screen(web.VTerm(33, 111), d)
                s.draw()
                self.assertEqual(s.page, 1, bad)
                self.assertNotIn("page", web.clean_prefs({"page": bad}), bad)


class OwnVariants(unittest.TestCase):
    """Variants made on the screen: each profile's own, one rule a record, the built-in ones never change."""
    T, desk, profile = EngineProfiles.T, EngineProfiles.desk, EngineProfiles.profile

    def edit(self, k, key, value, **kw):
        return k._command({"op": "variant_rule", "key": key, "value": value, **kw})

    def test_changing_a_built_in_makes_one_new_variant_and_the_book_moves(self):
        from tern import shadow
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            p = self.profile(k)
            k._command({"op": "profile_set", "profile": p.id, "key": "strategy.variant", "value": "band80"})
            before = shadow.rule_of("band80", p.cfg.strategy, p.shadow.own)
            for v in (0.81, 0.82, 0.83):                                       # stepping makes one copy, not three
                self.edit(k, "band_lo", v, profile=p.id)
            self.assertEqual(p.shadow.own.active(), ["band80-2"])
            self.assertEqual(p.variant, "band80-2")
            self.assertEqual(p.shadow.own.rule("band80-2"), {**before, "band_lo": 0.83})
            self.assertEqual(shadow.rule_of("band80", p.cfg.strategy, p.shadow.own), before)
            self.assertEqual(p.log.sv, 1)                                      # no settings period: the record is new
            self.assertEqual((k.variant(), k.shadow.own.all), ("base", {}))    # profile 1 has no part in it
            self.assertEqual(self.desk(d).profiles[0].variant, "band80-2")     # kept across a restart

    def test_one_that_bought_something_is_copied_not_changed(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, bid="0.80", ask="0.81")
            p = self.profile(k)
            self.edit(k, "band_lo", 0.78, profile=p.id)
            k._shadow()
            self.assertTrue(p.shadow.bought("base-2"))
            msg = self.edit(k, "band_hi", 0.90, profile=p.id)
            self.assertIn("new variant base-3", msg)
            self.assertEqual(p.shadow.own.rule("base-2")["band_hi"], 0.95)
            self.assertEqual((p.variant, p.shadow.own.rule("base-3")["band_hi"]), ("base-3", 0.90))
            self.assertFalse(p.shadow.bought("base-3"))

    def test_editing_one_not_in_use_leaves_the_book_and_one_session_makes_one_copy(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for v in (0.81, 0.82):
                self.edit(k, "band_lo", v, variant="band80", session="a")
            self.assertEqual(k.variant(), "base")
            self.assertEqual(k.shadow.own.active(), ["band80-2"])
            self.assertEqual(k.shadow.own.rule("band80-2")["band_lo"], 0.82)
            self.edit(k, "band_lo", 0.85, variant="band80", session="b")       # the box opened again on band80
            self.assertEqual(k.shadow.own.active(), ["band80-2", "band80-3"])

    def test_bad_changes_make_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for key, v in (("band_lo", 0.96), ("band_lo", True), ("band_lo", float("nan")), ("minutes", []), ("minutes", [12]),
                           ("series", ["KXFAKED"]), ("cap", 9), ("nope", 1), ("band_lo", 0.75), (None, 1), ("max_spread", "x")):
                msg = self.edit(k, key, v)
                self.assertTrue("not changed" in msg or "already" in msg, (key, v, msg))
            self.assertIn("unknown variant", self.edit(k, "band_lo", 0.8, variant="gone"))
            self.assertIn("no profile", self.edit(k, "band_lo", 0.8, profile="gone"))
            self.assertEqual((k.shadow.own.all, k.variant()), ({}, "base"))

    def test_own_variant_buys_by_its_own_rule_and_slots_in_shadow_and_book(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, bid="0.58", ask="0.60", close=5 * 60 - 1)         # 59c at 5 minutes: neither is base's
            self.edit(k, "band_lo", 0.55)
            self.edit(k, "minutes", [5])
            k._shadow(); k._auto()
            self.assertEqual({r["v"] for r in k.shadow.rows}, {"base-2"})
            self.assertEqual({r["slot"] for r in k.shadow.rows}, {5})
            self.assertEqual(list(k.paper.pos), [self.T])

    def test_caps_are_the_rules_own(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.edit(k, "cap", 1); self.edit(k, "max_vol", 7.6)
            S = k._strat(k.variant(), k.cfg.strategy, k.shadow.own)
            self.assertEqual((S["cap"], S["max_vol"]), (1, 7.6))
            self.edit(k, "cap", 0)
            self.assertIsNone(k.shadow.own.rule("base-2")["cap"])

    def test_new_rename_delete(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            p = self.profile(k)
            new = lambda name, frm="band80", **kw: k._command({"op": "variant_new", "name": name, "from": frm, **kw})
            self.assertIn("new variant My Rule", new("My Rule", profile=p.id))
            self.assertEqual(p.shadow.own.active(), ["my-rule"])
            self.assertEqual(p.variant, "base")                                # made, not put in use
            for bad in ("my rule", "BASE", "stop20", "x" * 13):
                self.assertIn("no new variant", new(bad, profile=p.id), bad)
            self.assertIn("new variant my rule", new("my rule"))               # another profile's are its own
            k._command({"op": "rename", "variant": "band80", "name": "wide"})
            self.assertIn("no new variant", new("wide"))                       # a built-in one's display name
            self.assertIn("not renamed", k._command({"op": "rename", "variant": "late", "name": "My rule"}))
            self.assertIn("is now slow", k._command({"op": "rename", "variant": "my-rule", "name": "slow", "profile": p.id}))
            self.assertEqual((p.shadow.own.title("my-rule"), k.shadow.own.title("my-rule")), ("slow", "my rule"))
            dele = lambda v, **kw: k._command({"op": "variant_delete", "variant": v, **kw})
            self.assertIn("built in", dele("base", profile=p.id))
            k._command({"op": "profile_set", "profile": p.id, "key": "strategy.variant", "value": "my-rule"})
            self.assertIn("in use", dele("my-rule", profile=p.id))
            k._command({"op": "profile_set", "profile": p.id, "key": "strategy.variant", "value": "base"})
            self.assertIn("deleted", dele("my-rule", profile=p.id))
            self.assertEqual(p.shadow.own.all, {})                             # it bought nothing: nothing is left
            self.assertEqual(k.shadow.own.active(), ["my-rule"])

    def test_a_deleted_one_settles_stays_on_file_and_never_lends_its_key(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._command({"op": "variant_new", "name": "x1", "from": "base"})
            k._shadow()
            self.assertTrue(k.shadow.bought("x1"))
            k._command({"op": "variant_delete", "variant": "x1"})
            k.shadow.save()
            self.assertNotIn("x1", [r["name"] for r in k.shadow.summary()])
            k = self.desk(d)
            self.assertIn("x1", {r["v"] for r in k.shadow.rows})
            k.shadow.settle({self.T: True})
            self.assertEqual(k.shadow.summary(["x1"])[0]["entries"], 1)
            k._command({"op": "variant_new", "name": "x1", "from": "base"})
            self.assertEqual(k.shadow.own.active(), ["x1-2"])
            self.assertEqual(k.shadow.summary(["x1-2"])[0]["entries"], 0)

    def test_only_so_many(self):
        from tern import variants
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for _ in range(variants.MAX_OWN):
                k._command({"op": "variant_new", "from": "base"})
            self.assertEqual(len(k.shadow.own.active()), variants.MAX_OWN)
            self.assertIn("at most", k._command({"op": "variant_new", "from": "base"}))
            self.assertIn("at most", self.edit(k, "band_lo", 0.8))
            self.assertEqual(k.variant(), "base")

    def test_the_gate_judges_the_new_variants_own_record(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.cfg.gate.min_days, k.cfg.gate.min_entries, k.cfg.gate.min_pnl = 0, 0, -1000
            k.shadow._count({"v": "base", "event": "E-1", "pnl": 5.0, "n": 5, "fee": 0.1, "close": 1})
            self.assertTrue(k.gate()["passed"])
            self.edit(k, "band_lo", 0.80)
            g = k.gate()
            self.assertEqual((g["variant"], g["passed"]), ("base-2", False))   # nothing settled yet

    def test_unreadable_variants_file_never_lets_live_trade_base(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.edit(k, "band_lo", 0.80)
            k._shadow(); k.shadow.save()
            self.assertIn("base-2", {r["v"] for r in k.shadow.rows})
            with open(os.path.join(d, "variants.json"), "w") as fh:
                fh.write("{bad")
            k = self.desk(d)
            self.assertFalse(k.live_target()["ok"])
            self.assertFalse(k.gate()["passed"])
            self.assertIn("base-2", {r["v"] for r in k.shadow.rows})           # its open entries are not lost
            k._command({"op": "variant_new", "name": "n", "from": "base"})
            self.assertTrue([f for f in os.listdir(d) if f.startswith("variants.json.damaged")])

    def test_one_bad_variant_is_skipped_not_run(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.edit(k, "band_lo", 0.80)
            path = os.path.join(d, "variants.json")
            with open(path) as fh:
                j = json.load(fh)
            good = j["variants"]["base-2"]
            j["variants"]["bad1"] = {**good, "rule": {**good["rule"], "minutes": ["25"]}}
            j["variants"]["bad2"] = {**good, "rule": {**good["rule"], "band_lo": None}}
            j["variants"]["base"] = good                                       # a built-in name is never overridden
            with open(path, "w") as fh:
                json.dump(j, fh)
            k = self.desk(d)
            self.assertEqual(k.shadow.own.active(), ["base-2"])
            k._shadow()

    def test_a_new_profile_brings_the_variant_profile_1_trades(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.edit(k, "band_lo", 0.80)
            k._command({"op": "variant_new", "name": "other", "from": "base"})
            p = self.profile(k)
            self.assertEqual((p.variant, p.shadow.own.active()), ("base-2", ["base-2"]))
            self.edit(k, "band_lo", 0.82)
            self.assertEqual(p.shadow.own.rule("base-2")["band_lo"], 0.80)     # a copy, not shared

    def test_report_lists_own_variants(self):
        import contextlib, io
        from unittest import mock
        from tern import __main__ as cli, config as cfgmod
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "RUNTIME", d):
            k = self.desk(d)
            self.edit(k, "band_lo", 0.80)
            k._shadow(); k.shadow.settle({self.T: True})
            def run(*argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["report", *argv]), 0)
                return out.getvalue()
            self.assertIn("base-2: 80-95¢", run())
            self.assertIn("base-2: 80-95¢", run("--profile", "1"))
            k._command({"op": "variant", "name": "base"})
            k._command({"op": "variant_delete", "variant": "base-2"})
            self.assertIn("deleted, kept for the record", run())


class RuleUpgrade(unittest.TestCase):
    """A profile whose rule was changed before own variants existed: the rules become variants, records follow."""

    def old(self, d, variant="multi"):
        """Settings 1 was the standard rule; 2 and 3 widened the spread and added a slot."""
        import json
        from tern import profiles
        from tern.config import Config, Strategy
        p = profiles.create(d, "old", Config(strategy=Strategy(variant=variant)), 0.07)
        p.set("strategy.max_spread", 0.09)
        p.set("strategy.minutes", [30, 25, 20, 15, 10])
        self.assertEqual(p.log.sv, 3)
        row = lambda v, sv, t: {"v": v, "ts": 1.0, "event": "KXBTCD-H", "close": 2.0, "ticker": t, "side": "yes", "n": 5,
                                "cost": 0.9, "fee": 0.02, "slot": 25, "pnl": None, "sv": sv}
        with open(os.path.join(p.dir, "shadow-settled.jsonl"), "w") as fh:
            for v, sv, t in (("base", 1, "a"), ("base", 3, "b"), ("multi", 3, "c"), ("late", 3, "d"), ("late", 2, "e"),
                             ("tight1", 2, "f"), ("stop20", 3, "g")):
                fh.write(json.dumps({**row(v, sv, t), "pnl": 1.0}) + "\n")
            fh.write("cut short\n")
        with open(os.path.join(p.dir, "shadow.json"), "w") as fh:
            json.dump({"rows": [row("multi", 3, "o1"), row("cap1", 3, "o2")], "slots": {}}, fh)
        os.remove(os.path.join(p.dir, "variants.json"))
        return p.dir

    def test_records_follow_their_rules(self):
        from tern.profiles import Profile
        with tempfile.TemporaryDirectory() as d:
            path = self.old(d)
            p = Profile.load(path, 0.07, upgrade=True)
            own = p.shadow.own
            self.assertEqual((p.variant, sorted(own.active())), ("multi-2", ["base-2", "multi-2"]))
            self.assertEqual((p.cfg.strategy.max_spread, p.cfg.strategy.minutes), (0.02, [25, 20, 15, 10]))
            self.assertEqual((own.rule("multi-2")["max_spread"], len(own.rule("multi-2")["series"])), (0.09, 6))
            n = lambda v: sum(a[2] for a in p.shadow.agg.get(v, {}).values())
            self.assertEqual([n(v) for v in ("base", "base-2", "multi", "multi-2", "stop20")], [1, 1, 0, 1, 1])
            self.assertEqual(n("late"), 0)                                     # 9c wide spread at either version
            self.assertEqual(n("tight1"), 1)                                   # 1c whatever the spread was: its own rule
            self.assertEqual({r["v"] for r in p.shadow.rows}, {"multi-2", next(k for k in own.all if k.startswith("cap1-"))})
            self.assertEqual(sum(n(v) for v in p.shadow.agg), 7)               # none lost
            with open(os.path.join(path, "shadow-settled.jsonl")) as fh:
                self.assertIn("cut short\n", fh.readlines())

    def test_it_happens_once_and_restarts_from_the_files_as_they_were(self):
        from tern.profiles import Profile, upgrade_rules
        with tempfile.TemporaryDirectory() as d:
            path = self.old(d)
            first = upgrade_rules(path)
            self.assertEqual(upgrade_rules(path), [])
            p = Profile.load(path, 0.07)
            rules = {k: v["rule"] for k, v in p.shadow.own.all.items()}
            os.remove(os.path.join(path, "variants.json"))                     # cut short before the last step
            self.assertEqual(upgrade_rules(path), first)
            again = Profile.load(path, 0.07)
            self.assertEqual({k: v["rule"] for k, v in again.shadow.own.all.items()}, rules)
            self.assertEqual(again.shadow.summary(["base-2"]), p.shadow.summary(["base-2"]))
            self.assertEqual(again.log.lines()[-1]["key"], p.log.lines()[-1]["key"])

    def test_a_standard_profile_is_left_alone(self):
        from tern import profiles
        from tern.config import Config
        with tempfile.TemporaryDirectory() as d:
            p = profiles.create(d, "std", Config(), 0.07)
            os.remove(os.path.join(p.dir, "variants.json"))
            profiles.upgrade_rules(p.dir)
            self.assertEqual(sorted(os.listdir(p.dir)), ["config.json", "profile.json", "settings-log.jsonl", "variants.json"])

    def test_reading_a_report_never_upgrades(self):
        from tern import profiles
        with tempfile.TemporaryDirectory() as d:
            path = self.old(d)
            profiles.load_all(d, 0.07)
            self.assertFalse(os.path.exists(os.path.join(path, "variants.json")))


class ScreenVariants(unittest.TestCase):
    """n, E and D in the variants box, and the rule rows, on a real engine's status."""
    T, desk, profile = EngineProfiles.T, EngineProfiles.desk, EngineProfiles.profile
    setup, sync, text, press, cmds, view2 = (ScreenProfiles.setup, ScreenProfiles.sync, ScreenProfiles.text,
                                             ScreenProfiles.press, ScreenProfiles.cmds, ScreenProfiles.view2)

    def run_cmds(self):
        out = self.cmds()
        for c in out:
            self.k._command(c)
        self.sync()
        return out

    def test_new_opens_its_rule_and_changes_go_into_it(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); scr.focus = "variants"; self.cmds()
            scr.var_sel = "band80"
            self.press("n", *"mine", 10)
            [c] = self.run_cmds()
            self.assertEqual((c["op"], c["from"], c["name"], c["profile"]), ("variant_new", "band80", "mine", "1"))
            self.assertEqual((scr.overlay, scr.edit_var, scr.var_sel), ("settings", "mine", "mine"))
            self.assertIn("no entries yet: changes go into it", self.text())
            self.press(self.curses.KEY_RIGHT); self.run_cmds()                 # band low is the first row
            self.assertEqual(k.shadow.own.rule("mine")["band_lo"], 0.81)
            self.assertEqual((k.variant(), k.shadow.own.active()), ("base", ["mine"]))
            self.press(27)
            self.assertEqual((scr.overlay, scr.edit_var), (None, None))
            self.assertIn("+mine", self.text())

    def test_editing_a_built_in_goes_on_with_the_copy(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("p"); scr.focus = "variants"; self.cmds()
            scr.var_sel = "band80"
            self.press("E")
            self.assertIn("built in: a change makes a new variant", self.text())
            self.press(self.curses.KEY_RIGHT, self.curses.KEY_RIGHT)           # two steps before the engine answers
            self.run_cmds()
            self.assertEqual((scr.edit_var, scr.var_sel), ("band80-2", "band80-2"))
            self.press(self.curses.KEY_RIGHT); self.run_cmds()
            self.assertEqual(k.shadow.own.active(), ["band80-2"])
            self.assertEqual(k.shadow.own.rule("band80-2")["band_lo"], 0.83)
            self.assertEqual(k.variant(), "base")

    def test_settings_rows_change_the_one_in_use_on_both_kinds_of_profile(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            for view in (False, True):
                if view:
                    self.view2()
                self.cmds()
                self.press("e")
                rows = [r.key for r in scr.set_rows()]
                scr.set_sel = rows.index("rule.band_hi")
                self.press(self.curses.KEY_LEFT, self.curses.KEY_LEFT)
                self.run_cmds()
                own = (p if view else k).shadow.own
                self.assertEqual(own.active(), ["base-2"])
                self.assertEqual(scr.active(), "base-2")
                self.assertIn("base-2", self.text())
                self.press(27)
            self.assertEqual(k.shadow.own.rule("base-2")["band_hi"], 0.93)
            self.assertEqual(p.shadow.own.rule("base-2")["band_hi"], 0.63)     # 65 was this profile's

    def test_book_trades_picks_own_variants_and_the_rule_rows_follow(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            k._command({"op": "variant_new", "name": "mine", "from": "late"}); self.sync(); self.cmds()
            self.press("e")
            rows = [r.key for r in scr.set_rows()]
            scr.set_sel = rows.index("strategy.variant")
            self.press(self.curses.KEY_LEFT)                                   # one back from base: the last, mine
            self.assertEqual(scr.set_value("rule.minutes"), [15, 10])          # at once, before the engine answers
            [c] = self.run_cmds()
            self.assertEqual((c["op"], c["name"]), ("variant", "mine"))
            self.assertEqual(k.variant(), "mine")

    def test_delete_asks_and_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            k._command({"op": "variant_new", "name": "mine", "from": "late"})
            self.press("p"); scr.focus = "variants"; self.sync(); self.cmds()
            for name, say in (("base", "built in"), ("late", "built in")):
                scr.var_sel = name
                self.press("D")
                self.assertIsNone(scr.modal); self.assertIn(say, scr.flash)
            scr.var_sel = "mine"
            self.press("D", "x")
            self.assertEqual(self.cmds(), [])
            self.press("D", "y"); self.run_cmds()
            self.assertEqual(k.shadow.own.active(), [])

    def test_armed_live_asks_before_the_rule_it_trades_changes(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            k.cfg.live.enabled = True; self.sync()
            self.press("e"); self.cmds()
            rows = [r.key for r in scr.set_rows()]
            for key in ("rule.band_lo", "strategy.variant"):
                scr.set_sel = rows.index(key)
                self.press(self.curses.KEY_RIGHT)
                self.assertEqual(scr.modal["type"], "livecmd")
                self.press(27)
                self.assertEqual(self.cmds(), [])
            self.press(27)
            self.press(9, "r")                                                 # so does r: ETH into the rule's coins
            self.assertEqual(scr.modal["type"], "livecmd")
            self.press(*"live", 10)
            [c] = self.run_cmds()
            self.assertEqual((c["op"], c["key"]), ("variant_rule", "series"))
            self.press("p"); scr.focus = "variants"; scr.var_sel = "band80"    # not what live trades: no question
            self.press("E", self.curses.KEY_RIGHT)
            self.assertIsNone(scr.modal)
            self.assertEqual(len(self.run_cmds()), 1)

    def test_help_says_the_rule_in_use_and_the_variant_keys(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            for key, v in (("band_lo", 0.69), ("band_hi", 0.83), ("cap", 1)):
                k._command({"op": "variant_rule", "key": key, "value": v})
            k._command({"op": "rename", "variant": "base-2", "name": "mine"}); self.sync()
            text = " ".join(" ".join(l if isinstance(l, str) else "".join(t for t, _ in l) for l in scr.help_lines(120)).split())
            for want in ("With mine, the variant in use", "mid is 69-83c", "1 a strike an hour", "the base rule (75-95c",
                         "new variant: a copy", "change the highlighted variant's rule", "marked +", "tern report --profile NAME"):
                self.assertIn(want, text)

    def test_off_can_be_stepped_and_typed(self):
        with tempfile.TemporaryDirectory() as d:
            k, p, scr = self.setup(d)
            self.press("e"); self.cmds()
            rows = [r.key for r in scr.set_rows()]
            scr.set_sel = rows.index("rule.cap")
            self.press(self.curses.KEY_RIGHT); self.run_cmds()
            self.assertEqual(k.shadow.own.rule("base-2")["cap"], 1)
            self.press(self.curses.KEY_LEFT); self.run_cmds()
            self.assertIsNone(k.shadow.own.rule("base-2")["cap"])
            scr.set_sel = rows.index("rule.max_vol")
            self.press("7", ".", "6", 10); self.run_cmds()
            self.assertEqual(k.shadow.own.rule("base-2")["max_vol"], 7.6)
            scr.set_sel = rows.index("rule.band_lo")
            self.press("9", "7", 10)                                           # over band high: refused on the screen
            self.assertEqual(self.cmds(), [])
            self.assertIn("below 95", scr.flash)


class Net(unittest.TestCase):
    """The kept-open HTTPS connections, against a fake connection class."""

    def setUp(self):
        import gzip
        from tern import net
        test, self.net = self, net
        self.made, self.script = [], []           # connections opened; what each request does, in order

        class Reply:
            status, will_close = 200, False
            def __init__(self, body, gz): self.body, self.gz = (gzip.compress(body) if gz else body), gz
            def read(self): return self.body
            def getheader(self, name): return "gzip" if self.gz and name == "Content-Encoding" else None

        class Conn:
            def __init__(self, host, timeout=None):
                self.sock, self.closed, self.sent = None, False, []
                test.made.append(self)
            def request(self, method, target, body=None, headers=None):
                self.sock = self.sock or type("S", (), {"settimeout": lambda s, t: None})()
                self.sent.append((method, target))
                self.todo = test.script.pop(0)
                if isinstance(self.todo, Exception):
                    raise self.todo
            def getresponse(self): return Reply(*self.todo)
            def close(self): self.closed = True

        self.real = net.http.client.HTTPSConnection
        net.http.client.HTTPSConnection = Conn
        net._local.__dict__.pop("conns", None)

    def tearDown(self):
        self.net.http.client.HTTPSConnection = self.real
        self.net._local.__dict__.pop("conns", None)

    def test_requests_share_one_connection_and_gzip_is_unpacked(self):
        self.script += [(b"one", False), (b"two", True)]
        self.assertEqual(self.net.request("h", "GET", "/a"), (200, b"one"))
        self.assertEqual(self.net.request("h", "GET", "/b"), (200, b"two"))
        self.assertEqual(len(self.made), 1)
        self.assertEqual(self.made[0].sent, [("GET", "/a"), ("GET", "/b")])

    def test_a_kept_connection_the_server_closed_is_retried_once_on_a_new_one(self):
        self.script += [(b"one", False), ConnectionResetError("gone"), (b"two", False)]
        self.net.request("h", "GET", "/a")
        self.assertEqual(self.net.request("h", "GET", "/b"), (200, b"two"))
        self.assertEqual(len(self.made), 2)
        self.assertTrue(self.made[0].closed)
        self.script += [ConnectionResetError("gone"), ConnectionResetError("still gone")]
        with self.assertRaises(ConnectionResetError):          # the new one failing too is an error, not a loop
            self.net.request("h", "GET", "/c")
        self.script += [(b"x", False), TimeoutError("slow")]
        self.net.request("h", "GET", "/d")
        with self.assertRaises(TimeoutError):                  # a timeout is not retried: it already took its time
            self.net.request("h", "GET", "/e")
        self.assertEqual(self.script, [])

    def test_an_order_never_rides_a_kept_connection_and_is_never_resent(self):
        self.script += [(b"one", False), ConnectionResetError("gone")]
        self.net.request("h", "GET", "/a")
        with self.assertRaises(ConnectionResetError):
            self.net.request("h", "POST", "/orders", b"{}", reuse=False)
        self.assertEqual(len(self.made), 2)                    # its own connection,
        self.assertEqual(self.made[1].sent, [("POST", "/orders")])   # sent once,
        self.assertTrue(self.made[1].closed)                   # and not kept

    def test_kalshi_maps_statuses_and_lost_replies(self):
        from tern.kalshi import Kalshi, KalshiError
        real = self.net.request
        try:
            calls = []
            self.net.request = lambda host, method, target, body, headers, timeout, reuse: calls.append((method, reuse)) or (503, b"bad gateway")
            with self.assertRaises(KalshiError) as cm:
                Kalshi(tries=1)._req("POST", "/portfolio/events/orders", body={})
            self.assertEqual(cm.exception.status, 503)
            self.assertEqual(calls, [("POST", False)])
            def lost(*a, **k): raise TimeoutError("timed out")
            self.net.request = lost
            with self.assertRaises(KalshiError) as cm:
                Kalshi(tries=1)._req("GET", "/markets")
            self.assertIsNone(cm.exception.status)             # no status: the engine treats an order like this as maybe sent
            self.net.request = lambda *a, **k: (200, b'{"markets": []}')
            self.assertEqual(Kalshi()._req("GET", "/markets"), {"markets": []})
        finally:
            self.net.request = real


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


class Web(unittest.TestCase):
    """tern web: the screen in a browser, view only."""
    T = "KXBTCD-E1-T100"

    def status(self, d):
        import json, time
        from tern.engine import live_dir
        st = {"heartbeat": time.time(), "mode": "paper", "pid": os.getpid(), "strategy": {"variant": "base", "series": ["KXBTCD"],
              "band_lo": 0.75, "band_hi": 0.95, "max_spread": 0.02, "minutes": [25, 20, 15, 10]},
              "variants": [{"name": "base", "active": True, "hours": 1, "entries": 1, "pnl": 1.0, "curve": []},
                           {"name": "band90", "hours": 1, "entries": 1, "pnl": 2.0, "curve": []}],
              "events": {"KXBTCD": {"event": "KXBTCD-E1", "close": time.time() + 900, "age": 0,
                                    "ladder": [{"t": self.T, "k": 99.99, "b": 0.5, "a": 0.52, "v": 10}]}}}
        with open(os.path.join(live_dir(d), "status.json"), "w") as fh:
            json.dump(st, fh)

    def test_control_keys_send_nothing_and_view_keys_work(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            self.status(d)
            hub = Hub(d)
            f = hub.handle({"w": 120, "h": 40, "prefs": {"page": 2, "theme": "classic"}})
            sid = f["sid"]
            for k in ("q", "s", "R", "L", "r", "Enter", "ArrowDown", "Enter"):
                f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": k})
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])          # no command reached the engine
            self.assertFalse(os.path.exists(os.path.join(d, "dash.json")))     # the screen's prefs are its own
            self.assertIn("view only", "".join(t for t, _ in f["rows"][38]))
            for k in ("t", "c", "Tab", "BackTab", "w", "4"):
                f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": k})
            self.assertEqual(f["prefs"]["watch"], "band90")                    # ↓ then w: watching, trading unchanged
            self.assertNotEqual(f["prefs"]["theme"], "classic")
            self.assertEqual(f["prefs"]["chart"], "1h")
            self.assertNotIn("hidden", f["prefs"])
            self.assertNotIn("zoom", f["prefs"])
            self.assertEqual(hub.sessions[sid][0].zoom, "log")                   # page 2's 4: zoomed in this tab only
            hub.sessions[sid][0].flash = ""
            f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": "ArrowUp"})
            row = "".join(t for t, _ in f["rows"][38])
            f = hub.handle({"sid": sid, "w": 120, "h": 40, "click": {"y": 38, "x": row.index("4 back")}})
            self.assertIsNone(hub.sessions[sid][0].zoom)                         # clicking "4 back" restores
            self.assertEqual(len(f["rows"]), 40)

    def test_web_pages_prefs(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            self.status(d)
            hub = Hub(d)
            f = hub.handle({"w": 111, "h": 33, "prefs": {"page": 2, "hidden": {"page2": ["log"]}, "zoom": "log"}})
            self.assertEqual(f["prefs"]["page"], 2)
            self.assertNotIn("hidden", f["prefs"])
            self.assertIsNone(hub.sessions[f["sid"]][0].zoom)
            old = hub.handle({"w": 111, "h": 33, "prefs": {"hidden": ["contract"], "slot3": "variants"}})
            self.assertEqual(old["prefs"]["page"], 1)
            self.assertNotIn("hidden", old["prefs"])
            self.assertNotIn("slot3", old["prefs"])
            sid = f["sid"]
            go = lambda **req: hub.handle({"sid": sid, "w": 111, "h": 33, **req})
            f = go(key="Enter")                                                 # focus on variants: picking is control
            self.assertIn("view only", "".join(t for t, _ in f["rows"][31]))
            f = go(key="p")
            self.assertEqual(f["prefs"]["page"], 1)
            f = go(key="ArrowUp")                                               # let the flash go: any key but p
            hub.sessions[sid][0].flash = ""; f = go(key="ArrowUp")
            row = "".join(t for t, _ in f["rows"][31])
            f = go(click={"y": 31, "x": row.index("page 1/2")})                 # clicking the page hint flips it
            self.assertEqual(f["prefs"]["page"], 2)
            hub.sessions[sid][0].flash = ""; f = go(key="ArrowUp")
            row = "".join(t for t, _ in f["rows"][31])
            f = go(click={"y": 31, "x": row.index("page 2/2")})
            self.assertEqual(f["prefs"]["page"], 1)
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])
            self.assertFalse(os.path.exists(os.path.join(d, "dash.json")))

    def test_web_profiles_hint_click_views_without_focusing_first(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            k = EngineProfiles.desk(self, d, bid="0.58", ask="0.60")
            k.seeded.add(self.T)
            p = EngineProfiles.profile(self, k, "profile 2", band_lo=0.55, band_hi=0.65)
            k._shadow(); k._status()
            hub = Hub(d)
            f = hub.handle({"w": 111, "h": 33, "prefs": {"page": 2}})
            sid = f["sid"]
            go = lambda **req: hub.handle({"sid": sid, "w": 111, "h": 33, **req})
            f = go(key="ArrowRight"); f = go(key="ArrowDown"); f = go(key="ArrowLeft")   # pick profile 2, focus back on variants
            rows = ["".join(t for t, _ in r) for r in f["rows"]]
            y = next(i for i, r in enumerate(rows) if "profiles" in r and "view" in r)
            f = go(click={"y": y, "x": rows[y].index("⏎")})
            self.assertEqual(f["prefs"]["profile"], p.id)
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])

    def test_settings_box_opens_but_changes_nothing(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            self.status(d)
            hub = Hub(d)
            sid = hub.handle({"w": 120, "h": 40})["sid"]
            for k in ("e", "ArrowDown", "ArrowRight", "ArrowLeft", "Enter", "5", "Enter", " "):
                f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": k})
            text = "\n".join("".join(t for t, _ in r) for r in f["rows"])
            self.assertIn("settings", text)                                    # the box is up
            self.assertIn("view only", text)
            self.assertNotIn("saved at once", text)
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])          # nothing reached the engine
            self.assertFalse(os.path.exists(os.path.join(d, "dash.json")))

    def test_settings_log_filter_works_for_that_viewer(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            self.status(d)
            hub = Hub(d)
            sid = hub.handle({"w": 120, "h": 40})["sid"]
            for k in ("e", "ArrowUp", "ArrowRight"):                           # the last row: log shows
                f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": k})
            self.assertEqual(f["prefs"]["log_filter"], "no_shadow")
            self.assertIn("saved at once", "\n".join("".join(t for t, _ in r) for r in f["rows"]))
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])
            self.assertFalse(os.path.exists(os.path.join(d, "dash.json")))
            f = hub.handle({"w": 120, "h": 40, "prefs": {"log_filter": "trades"}})   # a reload brings it back
            self.assertEqual(f["prefs"]["log_filter"], "trades")

    def test_web_views_a_profile_and_changes_nothing(self):
        import json
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            k = EngineProfiles.desk(self, d, bid="0.58", ask="0.60")
            k.seeded.add(self.T)
            p = EngineProfiles.profile(self, k, "profile 2", band_lo=0.55, band_hi=0.65)
            k._shadow(); k._status()
            hub = Hub(d)
            sid = hub.handle({"w": 120, "h": 40})["sid"]
            go = lambda key: hub.handle({"sid": sid, "w": 120, "h": 40, "key": key})
            for key in ("p", "ArrowRight", "ArrowDown", "Enter"):
                f = go(key)
            self.assertEqual(f["prefs"]["profile"], p.id)
            hub.publish(force=True)
            with open(os.path.join(k.fast, "view-web.json")) as fh:
                self.assertEqual(json.load(fh)["profiles"], [p.id])
            k._read_view(); k._status()
            hub.checked = 0                                                    # reread the status now, not in 0.2 s
            f = go("ArrowUp")
            self.assertIn("profile 2", "".join(t for t, _ in f["rows"][39]))     # status line: the profile shown
            for key in (" ", "n", "d", "D", "N"):
                f = go(key)
                self.assertIn("view only", "".join(t for t, _ in f["rows"][38]), key)
            go("e"); go("ArrowDown"); go("ArrowDown")
            f = go("ArrowRight")                                               # band low, one step up
            self.assertIn("view only", "".join(t for t, _ in f["rows"][38]))
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])          # nothing reached the engine
            f = hub.handle({"w": 120, "h": 40, "prefs": {"profile": p.id, "page": 2}})   # a reload keeps it
            self.assertEqual(f["prefs"]["profile"], p.id)

    def test_engine_sends_history_for_strikes_web_viewers_picked(self):
        import collections, json, time
        from tern.engine import Desk
        with tempfile.TemporaryDirectory() as d:
            k = Desk(d)
            k.hist[self.T] = collections.deque([(time.time(), 0.5, 0.52)])
            k.seeded.add(self.T)                                               # no network
            with open(os.path.join(k.fast, "view-web.json"), "w") as fh:
                json.dump({"series": ["KXETHD", "KXFAKED"], "tickers": [self.T]}, fh)
            k._read_view()
            self.assertEqual(k.web, {"series": ["KXETHD"], "tickers": [self.T], "profiles": []})
            k._status()
            with open(os.path.join(k.fast, "status.json")) as fh:
                self.assertIn(self.T, json.load(fh)["hist"])
            old = time.time() - 60                                             # tern web gone: stop serving it
            os.utime(os.path.join(k.fast, "view-web.json"), (old, old))
            k._read_view()
            self.assertEqual(k.web, {"series": [], "tickers": [], "profiles": []})


class WebLogin(unittest.TestCase):
    """tern web's login: the password, the signed cookie, the limits on wrong guesses, and the server."""

    def test_password_and_cookie(self):
        import json, stat
        from tern import webauth
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "web.json")
            with self.assertRaises(ValueError):
                webauth.set_password("short", p)
            webauth.set_password("correct horse battery", p)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertNotIn("horse", open(p).read())
            a = webauth.Auth(p)
            self.assertTrue(a.check("correct horse battery"))
            self.assertFalse(a.check("correct horse batter"))
            tok = a.token(1000.0)
            self.assertTrue(a.valid(tok, 1001.0))
            self.assertFalse(a.valid(tok, 1000.0 + webauth.TTL + 1))         # expired
            exp, nonce, sig = tok.split(".")
            self.assertFalse(a.valid(f"{int(exp) + 9999}.{nonce}.{sig}", 1001.0))   # stretched expiry
            for bad in (None, "", "x", "a.b.c", "1.2.3.4"):
                self.assertFalse(a.valid(bad, 1001.0))
            webauth.set_password("correct horse battery", p)                  # a new secret signs everyone out
            self.assertFalse(webauth.Auth(p).valid(tok, 1001.0))

    def test_limiter(self):
        from tern import webauth
        lim = webauth.Limiter()
        for _ in range(webauth.PER_IP):                                      # counted as they start, so
            self.assertEqual(lim.attempt("1.1.1.1", 100.0)[0], 0)            # parallel guesses cannot all slip in
        self.assertGreater(lim.attempt("1.1.1.1", 100.0)[0], 0)
        self.assertEqual(lim.wait("2.2.2.2", 101.0), 0)                       # others still get in
        self.assertEqual(lim.wait("1.1.1.1", 100.0 + webauth.WINDOW + 1), 0)  # and it passes
        lim = webauth.Limiter()
        for i in range(webauth.PER_IP - 1):
            lim.attempt("1.1.1.1", 100.0)
        _, stamp = lim.attempt("1.1.1.1", 100.0)
        lim.ok("1.1.1.1", stamp)                                              # a right password clears the count
        self.assertEqual((lim.wait("1.1.1.1", 101.0), len(lim.all)), (0, webauth.PER_IP - 1))
        lim = webauth.Limiter()
        for i in range(webauth.ALL):
            lim.attempt(f"10.0.0.{i}", 100.0)
        self.assertGreater(lim.wait("3.3.3.3", 101.0), 0)                     # many addresses: everyone waits

    def test_server_needs_login_and_https(self):
        import http.client, json, socket, ssl, threading, time
        from unittest import mock
        from tern import web, webauth
        with tempfile.TemporaryDirectory() as d:
            files = {"AUTH_FILE": os.path.join(d, "web.json"), "CERT_FILE": os.path.join(d, "c.pem"),
                     "KEY_FILE": os.path.join(d, "k.pem")}
            with mock.patch.multiple(webauth, **files):
                with self.assertRaises(SystemExit):                          # off localhost, not without both
                    web.serve(d, "0.0.0.0", 0)
                webauth.set_password("correct horse battery")
                webauth.make_cert([])
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
                threading.Thread(target=web.serve, args=(d, "127.0.0.1", port), daemon=True).start()
                ctx = ssl.create_default_context(cafile=files["CERT_FILE"])
                for _ in range(50):
                    try:
                        socket.create_connection(("127.0.0.1", port), 0.2).close(); break
                    except OSError:
                        time.sleep(0.1)
                c = http.client.HTTPSConnection("localhost", port, context=ctx, timeout=10)

                def ask(method, path, body=None, headers=None):
                    c.request(method, path, body, headers or {})
                    r = c.getresponse(); return r, r.read()
                r, _ = ask("GET", "/")
                self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
                self.assertEqual(ask("GET", "/look")[0].status, 401)
                self.assertEqual(ask("POST", "/frame", b"{}")[0].status, 401)  # and the connection stays in step
                r, _ = ask("POST", "/login", b"password=nope", {"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(r.status, 401)
                r, _ = ask("POST", "/login", b"password=correct+horse+battery",
                           {"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(r.status, 303)
                ck = r.getheader("Set-Cookie")
                for flag in ("__Host-tern_s=", "HttpOnly", "Secure", "SameSite=Strict"):
                    self.assertIn(flag, ck)
                cookie = {"Cookie": ck.split(";", 1)[0]}
                r, body = ask("GET", "/look", headers=cookie)
                self.assertEqual(r.status, 200)
                self.assertIn("fg", json.loads(body))
                self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
                for bad in ("__Host-tern_s=1.2.\xe9", "__Host-tern_s=" + "9" * 12 + ".a.b", "garbage;;=="):
                    self.assertEqual(ask("GET", "/look", headers={"Cookie": bad})[0].status, 401)
                self.assertEqual(ask("POST", "/frame", b"{", cookie)[0].status, 400)
                self.assertEqual(ask("POST", "/frame", b"[]", cookie)[0].status, 400)
                self.assertEqual(ask("POST", "/frame", b'{"w": "x"}', cookie)[0].status, 400)
                r, body = ask("POST", "/frame", b'{"w": 120, "h": 40}', cookie)
                self.assertEqual((r.status, len(json.loads(body)["rows"])), (200, 40))
                r, _ = ask("GET", "/logout", headers=cookie)
                self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
                p = http.client.HTTPConnection("127.0.0.1", port, timeout=10)    # http:// on the https port
                p.request("GET", "/look")
                r = p.getresponse()
                self.assertEqual((r.status, r.getheader("Location")), (301, f"https://127.0.0.1:{port}/look"))
                wrong = []                                                    # guesses in parallel: only 5 are heard
                def guess():
                    g = http.client.HTTPSConnection("localhost", port, context=ctx, timeout=30)
                    g.request("POST", "/login", b"password=wrongwrongwrong", {"Content-Type": "application/x-www-form-urlencoded"})
                    wrong.append(g.getresponse().status)
                ts = [threading.Thread(target=guess) for _ in range(12)]
                [t.start() for t in ts]; [t.join() for t in ts]
                self.assertEqual(sorted(wrong).count(401), webauth.PER_IP)       # the right password cleared the earlier one


if __name__ == "__main__":
    unittest.main()
