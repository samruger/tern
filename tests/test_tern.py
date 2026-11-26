import base64
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tern.book import Book, taker_fee
from tern.sign import load_key, signature
from tern import sign
from tern.strategy import due_slot, signals
from tern.dash import braille, by_time
from tern import config as cfgmod, devices, webauth, webctl

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
            k.live.trade("HELD-T1", 1, 0.5, 0.0, {}, "x")
            k.shadow.rows = [{"v": "base", "ts": now, "event": "OWED", "close": now, "ticker": "OWED-T1", "side": "yes", "n": 1,
                              "cost": 0.5, "fee": 0, "slot": 10, "pnl": None}]
            k.slots = {"live|E-1": {25}, "live|GONE": {25}}
            k.hour_cost = {"live|E-1": 1.0, "live|GONE": 1.0}
            k._save_state()
            self.assertEqual(set(k.meta), {"E-1-T1", "HELD-T1", "OWED-T1"})
            self.assertEqual(set(k.slots), {"live|E-1"})
            self.assertEqual(set(k.hour_cost), {"live|E-1"})
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
            k.live.trade("A-T1", 2, 0.5, 0.01, k.meta["A-T1"], "auto")
            k.live.trade("A-T2", 2, 0.5, 0.01, k.meta["A-T2"], "auto")
            k.live.mark_curve(1000.0, now)
            k._settle(); k._save_state()
            st = json.load(open(os.path.join(d, "state.json")))
            self.assertNotIn("trades", st["live"]); self.assertNotIn("curve", st["live"])
            self.assertEqual(set(st["meta"]), {"A-T2"})                        # only meta still owed
            lines = lambda n: open(os.path.join(d, n)).read().splitlines()
            self.assertEqual(len(lines("live-trades.jsonl")), 3)               # two fills and a settlement
            self.assertEqual(len(lines("live-curve.jsonl")), 1)
            k._save_state()
            self.assertEqual(len(lines("live-trades.jsonl")), 3)               # nothing new: nothing appended
            k2 = self.desk(d)                                                  # a restart reads it all back
            self.assertEqual([t["kind"] for t in k2.live.trades], ["fill", "fill", "settle"])
            self.assertEqual(k2.live.curve, k.live.curve)
            self.assertEqual(k2.live.new_trades, [])                           # read back, not queued again

    def test_a_state_file_from_before_the_split_seeds_the_history_files(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            trade = {"ts": 5.0, "ticker": "A-T1", "delta": 1, "price": 0.5, "fee": 0.0, "realized": 0.0, "tag": "auto", "kind": "fill"}
            old = {"live": {"name": "live", "cash": 990.0, "start": 1000.0, "pos": {}, "realized": -10.0, "fees": 0.0,
                            "trades": [trade], "curve": [[60, 1000.0], [120, 990.0]]}}
            json.dump(old, open(os.path.join(d, "state.json"), "w"))
            k = self.desk(d)
            self.assertEqual(k.live.trades, [trade]); self.assertEqual(len(k.live.curve), 2)
            k._save_state()
            self.assertLess(os.path.getsize(os.path.join(d, "state.json")), 600)
            k2 = self.desk(d)                                                  # and nothing is seeded twice
            self.assertEqual(k2.live.trades, [trade])
            self.assertAlmostEqual(k2.live.cash, 990.0)

    def test_the_paper_book_of_before_is_kept_in_a_file(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            paper = {"name": "paper", "cash": 990.0, "start": 1000.0, "pos": {"A-T1": {"q": 2, "avg": 0.5}}, "realized": -10.0, "fees": 0.0}
            json.dump({"paper": paper, "auto": [{"ticker": "A-T1", "pnl": None}]}, open(os.path.join(d, "state.json"), "w"))
            k = self.desk(d); k._save_state()
            with open(os.path.join(d, "paper-book.json")) as fh:
                kept = json.load(fh)
            self.assertEqual((kept["paper"], kept["open"]), (paper, [{"ticker": "A-T1", "pnl": None}]))
            self.assertNotIn("paper", json.load(open(os.path.join(d, "state.json"))))
            self.desk(d)._save_state()
            with open(os.path.join(d, "paper-book.json")) as fh:
                self.assertEqual(json.load(fh), kept)

    def test_damaged_state_falls_back_to_backup_and_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.live.trade("A-T1", 2, 0.5, 0.0, {}, "x")
            k._save_state()
            p = os.path.join(d, "state.json")
            open(p, "w").write('{"paper": {"na')               # cut off mid-write
            k2 = self.desk(d)
            self.assertAlmostEqual(k2.live.pos["A-T1"]["q"], 2)
            self.assertTrue(any(".damaged-" in f for f in os.listdir(d)))

    def test_gate_days_count_hours_traded(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.shadow.agg = {"base": {f"KXBTCD-H{h}": [0.1, 3, 3, 3, 0] for h in range(12)}}
            self.assertIn("0.5 days", k.checks("base")["checks"][0]["detail"])   # 12 hours, however long ago they started

    def test_unreadable_config_keeps_the_one_in_use(self):
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            cur = config.Config(); cur.strategy.contracts = 9
            with open(os.path.join(d, "config.json"), "w") as fh:
                fh.write("{bad")
            self.assertEqual(config.load(d, cur).strategy.contracts, 9)


class P256(unittest.TestCase):
    def sh(self, d, *args):
        subprocess.run(["openssl", *args], cwd=d, check=True,
                       capture_output=True)

    def keypair(self, d, name):
        self.sh(d, "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                "-out", name + ".pem")
        self.sh(d, "pkey", "-in", name + ".pem", "-pubout",
                "-outform", "DER", "-out", name + ".der")
        return pathlib.Path(d, name + ".der").read_bytes()

    def test_verifies_openssl_signatures(self):
        n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

        def enc(r, s):
            def i(v):
                b = v.to_bytes(33, "big").lstrip(b"\0") or b"\0"
                if b[0] & 0x80:
                    b = b"\0" + b
                return b"\x02" + bytes([len(b)]) + b
            body = i(r) + i(s)
            return b"\x30" + bytes([len(body)]) + body

        with tempfile.TemporaryDirectory() as d:
            pub = self.keypair(d, "k")
            other = self.keypair(d, "o")
            for _ in range(20):
                msg = os.urandom(40)
                pathlib.Path(d, "msg").write_bytes(msg)
                self.sh(d, "dgst", "-sha256", "-sign", "k.pem",
                        "-out", "sig.der", "msg")
                sig = pathlib.Path(d, "sig.der").read_bytes()
                self.assertTrue(sign.p256_verify(pub, msg, sig))
                self.assertFalse(sign.p256_verify(pub, msg + b"x", sig))
                bad = bytearray(sig); bad[-1] ^= 1
                self.assertFalse(sign.p256_verify(pub, msg, bytes(bad)))
                self.assertFalse(sign.p256_verify(other, msg, sig))
            self.assertFalse(sign.p256_verify(pub, msg, enc(0, 5)))
            self.assertFalse(sign.p256_verify(pub, msg, enc(5, n)))
            self.assertFalse(sign.p256_verify(pub, msg, b""))
            self.assertFalse(sign.p256_verify(b"junk", msg, sig))

    def test_rejects_bad_keys(self):
        with tempfile.TemporaryDirectory() as d:
            pub = self.keypair(d, "k")
            self.sh(d, "genpkey", "-algorithm", "ed25519", "-out", "e.pem")
            self.sh(d, "pkey", "-in", "e.pem", "-pubout",
                    "-outform", "DER", "-out", "e.der")
            ed = pathlib.Path(d, "e.der").read_bytes()
        self.assertEqual(len(sign.p256_key(pub)), 2)
        with self.assertRaises(ValueError):
            sign.p256_key(ed)
        off = bytearray(pub); off[-1] ^= 1
        with self.assertRaises(ValueError):
            sign.p256_key(bytes(off))
        # compressed point: 33 bytes, so the two length bytes shrink
        head = bytearray(pub[:26]); head[1] = 0x39; head[24] = 0x22
        comp = bytes(head) + bytes([2 + (pub[-1] & 1)]) + pub[27:59]
        with self.assertRaises(ValueError):
            sign.p256_key(comp)
        with self.assertRaises(ValueError):
            sign.p256_key(pub[:-5])


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
                k.live.trade("A-T1", 2, 0.5, 0.0, {}, "x")   # the rest of the step still runs
                seen.append(1)
            k.step = step
            k.run()
            self.assertEqual(seen, [1])                       # finished once, no second step
            self.assertAlmostEqual(self.desk(d).live.pos["A-T1"]["q"], 2)
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
        return {"op": "set", "key": "alerts", "value": False, **kw}

    def test_manual_orders_are_gone(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("unknown command", k._command({"op": "order", "ticker": self.T, "action": "buy_yes", "n": 5}))
            self.assertFalse(k.shadow.rows or k.live.pos)

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
            self.assertIn("desktop alerts: on → off", k.results["new"])
            self.assertEqual(os.listdir(k.cmd_dir), [])

    def test_strikes_and_prices_read_right_for_every_coin(self):
        from tern.dash import strike_of, px
        for k, want in ((66799.99, 66800), (74.4999, 74.5), (74.7499, 74.75), (0.8599, 0.86), (604.99, 605), (70000, 70000)):
            self.assertAlmostEqual(strike_of(k), want, places=6)
        self.assertEqual([px(66800), px(604.99), px(74.75), px(2.5), px(0.86)], ["66,800", "604.99", "74.75", "2.500", "0.8600"])

    def test_unknown_series_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("unknown command", k._command({"op": "series", "series": "KXFAKED"}))
            k.cfg.strategy.series = ["KXFAKED"]; k.view = {"series": "KXFAKED", "ticker": None}
            k.last["cfg"] = __import__("time").time()
            k._read_view = lambda: None
            for name in ("_live_ready", "_commands", "_auto", "_shadow", "_settle", "_status"):
                setattr(k, name, lambda *a: None)
            k.step()                                          # no KeyError, no network
            self.assertTrue(any("unknown series KXFAKED" in m for _, _, m in k.logs))



    def test_settings_change_only_listed_fields_within_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            from tern import config
            self.assertIn("300 → 350", k._command({"op": "set", "key": "gate.min_entries", "value": 350}))
            self.assertEqual(config.load(d).gate.min_entries, 350)
            for key, v in (("live.capital_usd", 90), ("live.max_order_usd", 20), ("live.max_daily_loss", 20), ("strategy.contracts", 3), ("strategy.max_hour_cost", 50), ("paper_capital", 2000), ("strategy.band_lo", 0.8)):
                self.assertIn("not set", k._command({"op": "set", "key": key, "value": v}), key)   # a variant's
            self.assertEqual((k.cfg.strategy, k.cfg.paper_capital), (config.Strategy(), 1000.0))
            self.assertIn("not set", k._command({"op": "set", "key": "live.enabled", "value": 1}))
            self.assertIn("not set", k._command({"op": "set", "key": "gate.min_entries", "value": -5}))
            self.assertIn("not set", k._command({"op": "set", "key": "gate.min_entries", "value": "400"}))
            self.assertFalse(config.load(d).live.enabled)
            self.assertEqual((config.load(d).live.capital_usd, config.load(d).gate.min_entries), (100.0, 350))

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
            sent = []
            k._exec = lambda book, t, delta, *a: sent.append(t) or {"delta": delta, "fee": 0.0}
            k.ev_close["KXBTCD-E1"] = __import__("time").time() + 600
            k.taken = {"live|KXBTCD-E1": {self.T: 1}}
            k._auto_book(k.live, k.cfg.strategy, "cap1", __import__("time").time(), k.shadow.own)
            self.assertEqual(sent, [])
            k.slots = {}
            k._auto_book(k.live, k.cfg.strategy, "base", __import__("time").time(), k.shadow.own)
            self.assertEqual((sent, k.taken["live|KXBTCD-E1"][self.T]), ([self.T], 2))

    def test_gate_judges_the_variant_on_its_own_record(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.shadow.agg = {"base": {f"KXBTCD-H{h}": [1.0, 5, 2, 2, 0] for h in range(24 * 15)},
                            "cap1": {f"KXBTCD-H{h}": [1.0, 5, 2, 2, 0] for h in range(24)}}
            k.mode = "live"
            g = k.gate()                                       # no variant picked: never passes
            self.assertFalse(g["passed"] or g["ok"] or k._live_rule_on(g))
            k._command({"op": "live", "on": True, "variant": "base"})
            self.assertTrue(k.gate()["passed"] and k._live_rule_on(k.gate()))
            k._command({"op": "live", "on": True, "variant": "cap1"})
            g = k.gate()
            self.assertFalse(g["passed"]); self.assertEqual(g["variant"], "cap1")
            self.assertFalse(k._live_rule_on(g))               # moving live never skips the gate
            k.cfg.live.auto_needs_gate = False
            self.assertTrue(k._live_rule_on(k.gate()))
            k._command({"op": "variant_on", "variant": "cap1", "on": False})
            self.assertFalse(k._live_rule_on(k.gate()))        # off: not even without the gate


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
            S.minutes = [20]; k.cfg.strategy.contracts = 10.0
            sent = []
            k._exec = lambda book, t, delta, *a: sent.append(t) or {"delta": delta, "fee": 0.0}
            k.ev_close["E1"] = now + 1200
            k.hour_cost["live|E1"] = S.max_hour_cost - 0.8
            k._auto_book(k.live, S, "base", now, k.shadow.own)
            self.assertEqual(sent, ["E1-B"], "a cheaper favorite after an unaffordable one is still bought")


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
        k.cfg.live.variant = "base"
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

    def test_live_buys_the_variants_own_size(self):
        with tempfile.TemporaryDirectory() as d:
            got = []
            class Api:
                def order(self, t, side, n, price, cid):
                    got.append(n)
                    return {"order_id": "o1", "fill_count": str(n), "average_fill_price": "0.82", "average_fee_paid": "0"}
            k = self.desk(d, Api())
            k.cfg.live.auto_needs_gate = False
            k.cfg.strategy.contracts = 2.0
            k._auto()
            self.assertEqual(got, [2.0])
            self.assertAlmostEqual(k.live.pos[self.T]["q"], 2)
            k._shadow()
            self.assertEqual(next(r["n"] for r in k.shadow.rows if r["v"] == "base"), 2)   # as paper does

    def live_api(self):
        got = []
        class Api:
            def order(self, t, side, n, price, cid):
                got.append(n)
                return {"order_id": "o1", "fill_count": str(n), "average_fill_price": str(price), "average_fee_paid": "0"}
        return got, Api()

    def test_live_trades_only_its_variant_and_only_past_its_gate(self):
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            got, api = self.live_api()
            k = self.desk(d, api)                                              # 80-82c: base's band, not band90's
            k._command({"op": "variant_new", "from": "band90", "name": "wide", "rule": {"band_lo": 0.6, "max_spread": 0.05}})
            k.cfg.live.enabled = True
            for bad in (None, "nope", 5):
                self.assertIn("live not changed", k._command({"op": "live", "on": True, "variant": bad}))
            self.assertIn("live not changed", k._command({"op": "live", "on": True}))   # a screen from before: no variant named
            self.assertEqual(k.cfg.live.variant, "base")
            self.assertIn("live moved from base to wide", k._command({"op": "live", "on": True, "variant": "wide"}))
            self.assertEqual((config.load(d).live.variant, k.live_target()["variant"]), ("wide", "wide"))
            self.assertIn("already trades wide", k._command({"op": "live", "on": True, "variant": "wide"}))
            g = k.gate()
            self.assertEqual((g["variant"], g["passed"], g["ok"]), ("wide", False, True))
            k._auto()
            self.assertEqual(got, [])                                          # its record hasn't passed the gate
            k.cfg.live.auto_needs_gate = False
            k.slots.clear(); k._auto()
            self.assertEqual(got, [5.0])
            self.assertTrue(any(m.startswith("live: 20m slot") and "(wide)" in m for _, _, m in k.logs))
            self.assertEqual([v["name"] for v in self.status(k)["variants"] if v["live"]], ["wide"])
            k.slots.clear(); k.cfg.live.variant = "band90"; k._auto()
            self.assertEqual(got, [5.0])                                       # band90 does not buy at 81c

    def status(self, k):
        import json
        k.seeded.add(self.T); k._status()
        with open(os.path.join(k.fast, "status.json")) as fh:
            return json.load(fh)

    def test_live_off_turns_it_off_and_arming_names_the_variant(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.cfg.live.enabled = False
            self.assertIn("live armed with band90", k._command({"op": "live", "on": True, "variant": "band90"}))
            self.assertEqual((k.cfg.live.enabled, k.cfg.live.variant), (True, "band90"))
            self.assertIn("live off", k._command({"op": "live", "on": False}))
            self.assertEqual((k.cfg.live.enabled, k.mode), (False, "paper"))

    def test_the_first_start_keeps_what_live_would_have_traded(self):
        import json
        from tern import config
        for old, want in (({"strategy": {"variant": "late"}, "live": {"profile": "1"}}, "late"),
                          ({"strategy": {"variant": "late"}}, "late"), ({"strategy": {"variant": "late"}, "live": {"profile": "p2"}}, ""),
                          ({"strategy": {"variant": "late"}, "live": {"variant": "cap1"}}, "cap1"), ({}, "")):
            self.assertEqual(config.Config.from_dict(old).live.variant, want, old)

    def test_live_variant_off_or_gone_enters_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            got, api = self.live_api()
            k = self.desk(d, api)
            k.cfg.live.auto_needs_gate = False                                 # even without the gate
            k._command({"op": "variant_new", "from": "band80", "name": "wide", "rule": {"band_lo": 0.6}})
            k.cfg.live.enabled = True
            k._command({"op": "live", "on": True, "variant": "wide"})
            self.assertIn("live enters nothing while it is off", k._command({"op": "variant_on", "variant": "wide", "on": False}))
            g = k.gate()
            self.assertFalse(g["passed"]); self.assertIn("is off", g["checks"][0]["name"])
            k._auto()
            self.assertEqual(got, [])
            self.assertIn("what live trades", k._command({"op": "variant_delete", "variant": "wide"}))
            del k.shadow.own.all["wide"]                                       # gone some other way
            self.assertIn("not there any more", k.gate()["checks"][0]["name"])
            k.slots.clear(); k._auto()
            k.cfg.live.variant = ""
            self.assertIn("no variant is picked", k.gate()["checks"][0]["name"])
            k.slots.clear(); k._auto()
            self.assertEqual(got, [])                                          # never falls back to another alone

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
            k._command({"op": "live", "on": True, "variant": "band90"})
            self.assertEqual(k.day, {})
            k.day = {"date": "x", "pnl": 1.0}
            k._command({"op": "live", "on": True, "variant": "base"})
            self.assertEqual(k.day, {"date": "x", "pnl": 1.0})   # moving it does not start the day over
            k.day = {}
            k.cfg.live.enabled = True
            k._daily_loss()                                   # anchors at the current P&L, no KILL
            self.assertTrue(k.cfg.live.enabled)

    def test_voided_market_returns_the_stake(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.live.trade(self.T, 10, 0.80, 0.05, k.meta[self.T], "auto")
            cash = k.live.cash
            k.live.pos[self.T]["close"] = time.time() - 100
            k.pub.market = lambda t: {"status": "finalized", "result": "void"}
            k._settle()
            self.assertNotIn(self.T, k.live.pos)
            self.assertAlmostEqual(k.live.cash, cash + 8.0)
            k.live.trade(self.T, 10, 0.80, 0.0, k.meta[self.T], "auto")
            k.live.pos[self.T]["close"] = time.time() - 100; k.last.clear()
            k.pub.market = lambda t: {"status": "closed", "result": ""}   # not decided yet: left alone
            k._settle()
            self.assertIn(self.T, k.live.pos)

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
                k.live.pos[f"A{i}"] = {"q": 1.0, "avg": 0.5, "close": time.time() - 100}
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
            k.cfg.strategy.series = ["E1"]                                 # (the event's own name, in this test)
            k.shadow.step("E1", now + 1200, k.events["KXBTCD"]["markets"], k.cfg.strategy, now, paper=1000)
            self.assertEqual({r["n"] for r in k.shadow.rows}, {3.0})       # 3.5 on offer: 3

    def test_gate_hours_are_not_doubled_by_a_second_series(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, self.api())
            k.shadow.agg = {"base": {f"{s}-H{h}": [0.1, 1, 1, 1, 0] for h in range(12) for s in ("KXBTCD", "KXETHD")}}
            self.assertIn("0.5 days", k.checks("base")["checks"][0]["detail"])


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
            self.assertEqual(all_coins(S), ["KXBTCD", "KXETHD", "KXSOLD", "KXXRPD"])
            for ev in ("KXBTCD-H1", "KXSOLD-H1"):
                sh.step(ev, 1000.0, [self.m(ev + "-T1", 0.92, 0.93, 1)], S, 1000.0 - 25 * 60 + 1)
            on = lambda v: sorted(r["event"] for r in sh.rows if r["v"] == v)
            self.assertEqual(on("base"), ["KXBTCD-H1"])
            self.assertEqual(on("multi"), ["KXBTCD-H1", "KXSOLD-H1"])
            sh.settle({"KXBTCD-H1-T1": True, "KXSOLD-H1-T1": True})
            self.assertEqual(sh.summary(["multi"])[0]["hours"], 1)         # two coins in one hour are one hour

    def test_summary_is_worked_out_again_only_when_the_records_change(self):
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh = Shadow(d)
            row = lambda i, v="base", pnl=1.0: {"v": v, "event": f"KXBTCD-H{i}", "pnl": pnl, "n": 5, "fee": 0.1, "close": float(i)}
            for i in range(12):
                sh._count(row(i))
            calls, real = [], sh._summarize
            sh._summarize = lambda name, sv: calls.append((name, sv)) or real(name, sv)
            first = sh.summary()
            n = len(calls)
            self.assertEqual(n, len(sh.order()))
            first[0]["pnl"] = 999; first[0]["extra"] = 1                       # a caller's copy is its own
            again = sh.summary(); sh.summary(["base"])
            self.assertEqual((len(calls), again[0]["pnl"], "extra" in again[0]), (n, 12.0, False))
            sh._count(row(12, pnl=-3.0))                                       # settled: counted at once
            self.assertEqual((sh.summary(["base"])[0]["pnl"], sh.summary(["base"])[0]["entries"]), (9.0, 13))
            sh.rows.append({"v": "base", "event": "KXBTCD-H99", "close": 99.0, "ticker": "x"}); sh.changes += 1
            self.assertEqual(sh.summary(["base"])[0]["open"], 1)               # bought: open at once
            self.assertEqual(sh.summary(["base"], sv=0)[0]["entries"], 13)     # each settings period on its own
            self.assertEqual(sh.summary(["base"], sv=7)[0]["entries"], 0)

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
        s = dash.Screen.__new__(dash.Screen)                               # help_sections needs only these
        s.st, s.raw, s.variant, s.P = {}, {}, None, type("P", (), {"__call__": lambda *a, **k: 0, "c": lambda *a: 0, "border": lambda *a: 0, "grey": 0, "many": False})()
        text = "\n".join(l if isinstance(l, str) else "".join(t for t, _ in l) for _, ls in s.help_sections(120) for l in ls)
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


class Rig:
    """An engine on a runtime folder of its own, and a screen on its status, for the classes below."""
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

    def new(self, k, frm="base", name="", **rule):
        """Save frm's settings with changes as a new variant (s in E's box)."""
        return k._command({"op": "variant_new", "from": frm, "name": name, "rule": rule or {"cap": 6}})

    def status(self, k):
        import json
        k.seeded.add(self.T); k._status()
        with open(os.path.join(k.fast, "status.json")) as fh:
            return json.load(fh)

    def setup(self, d, h=40, w=120):
        import curses
        from unittest import mock
        from tern import dash, web
        k = self.desk(d, bid="0.58", ask="0.60")
        k.seeded.add(self.T)                                                   # no network for its history
        with mock.patch.object(dash, "Pal", web.WebPal):
            scr = dash.Screen(web.VTerm(h, w), d)
        self.k, self.scr, self.curses = k, scr, curses
        self.sync()
        return k, scr

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

    def run_cmds(self):
        out = self.cmds()
        for c in out:
            self.k.results[c["id"]] = self.k._command(c)
        self.sync()
        return out

    def open(self, name="band80"):
        """e on a variant, on their view."""
        self.press("5")
        self.scr.var_sel = name
        self.cmds()
        self.press("e")

    def row(self, key):
        self.scr.set_sel = [r.name for r in self.scr.set_rows()].index(key)


class MouseRig(Rig):
    """Clicks, as curses reports them."""

    def at(self, text, row=None, nth=0):
        """Where the text is on screen: (y, x)."""
        rows = ["".join(r) for r in self.scr.s.ch]
        found = [(y, r.index(text)) for y, r in enumerate(rows) if text in r and (row is None or y == row)]
        self.assertTrue(found, f"{text!r} is not on screen")
        return found[nth]

    def mouse(self, y, x, button=None):
        """A press there, as curses reports it. Returns what key() does: False closes the screen."""
        return self.events((y, x, button or self.curses.BUTTON1_PRESSED))

    def events(self, *evs):
        """One KEY_MOUSE that stands for these, oldest first: curses hands them out newest first."""
        from unittest import mock
        c = self.curses
        queue = [(0, x, y, 0, b) for y, x, b in reversed(evs)] + [c.error("none left")]
        with mock.patch.object(c, "getmouse", side_effect=queue):
            out = self.scr.key(c.KEY_MOUSE)
        self.sync()
        return out

    def click(self, text, **kw):
        return self.mouse(*self.at(text, **kw))

    def set_row(self, key):
        """Where a settings row is: its line, in the list."""
        i = [r.key for r in self.scr.set_rows()].index(key)
        return next((y, x0) for y, x0, x1, what, arg in self.scr.hits if what == "set" and arg == (i, None))


class Mouse(MouseRig, unittest.TestCase):
    """A click does what the key would."""

    def test_tabs(self):
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            for name in reversed(dash.BOXES):
                self.assertTrue(self.click(name, row=0))
                self.assertEqual(scr.tab, name)

    def test_keys_on_the_borders(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.click("hart 15m"); self.assertEqual(scr.chart, "1h")            # the whole hint, not just its letter
            self.press("5")
            names = [v["name"] for v in scr.st["variants"]]
            scr.var_sel = names[0]; self.sync()
            y, x = self.at("↑↓", row=38)
            self.mouse(y, x + 1); self.assertEqual(scr.var_sel, names[1])        # each arrow is its own
            self.mouse(y, x); self.assertEqual(scr.var_sel, names[0])
            self.click("on/off", row=38); self.press("y")
            self.assertEqual([c["op"] for c in self.cmds()], ["variant_on"])
            self.press("h", "0"); theme = scr.P.name
            self.press("6"); self.click("heme", row=38); self.assertNotEqual(scr.P.name, theme)
            self.assertTrue(self.click("uit", row=38))                           # quit asks
            self.assertFalse(self.scr.key(ord("y")))                             # and closes the screen

    def test_page_keys_on_the_border(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, h=30, w=100)
            self.press("h")
            secs = scr.help_sections(78)
            scr.help_sel = max(range(len(secs)), key=lambda i: len(secs[i][1])); self.sync()
            self.click("PgDn", row=28); down = scr.offs["help"]
            self.assertGreater(down, 0)
            self.click("PgUp", row=28); self.assertLess(scr.offs["help"], down)

    def test_rows(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5")
            self.assertEqual(scr.variant or "base", "base")
            self.click("band90", nth=0); self.assertEqual(scr.var_sel, "band90")
            self.assertNotEqual(scr.variant, "band90")                           # highlighted, and not selected yet
            self.click("band90", nth=0); self.assertEqual(scr.variant, "band90")
            self.assertEqual(self.cmds(), [])                                    # selecting sends nothing
            m = k.events["KXBTCD"]["markets"][0]
            k.events["KXBTCD"]["markets"].append({**m, "ticker": "KXBTCD-E1-T200", "floor_strike": 200.0})
            self.press("3")
            for y, t in [(h[0], h[4]) for h in scr.hits if h[3] == "ladder"] * 2:
                self.mouse(y, 10); self.assertEqual(scr.sel, t)
            self.press("0"); self.click("tokyo night"); self.assertEqual(scr.P.name, "tokyo night")
            self.press("4"); self.click(" live ", row=2); self.assertEqual(scr.book_view, "live")
            self.click("on paper", row=2); self.assertEqual(scr.book_view, "paper")

    def test_reports_and_help(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("h")
            y = next(y for y, x0, x1, what, arg in scr.hits if what == "help" and arg == 2)
            self.mouse(y, 5); self.assertEqual(scr.help_sel, 2)
            self.press("8")
            y, rid = next((y, arg) for y, x0, x1, what, arg in scr.hits if what == "report" and arg != scr.rep_sel)
            self.mouse(y, 5); self.assertEqual(scr.rep_sel, rid)
            self.mouse(y, 60); self.assertEqual(scr.rep_sel, rid)                # the report's text is not a row

    def test_settings(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("9")
            rows = [r.key for r in scr.set_rows()]
            y, x = self.set_row("web.port")
            self.mouse(y, x + 3); self.assertEqual(rows[scr.set_sel], "web.port")
            self.assertIsNone(scr.set_buf)
            y, x = self.set_row("web.port"); self.mouse(y, x + 3); self.assertEqual(scr.set_buf, "")   # again: enter
            self.press("9", "0", "0", "0")
            y, x = self.set_row("gate.min_days"); self.mouse(y, x + 3)           # typing: another row does nothing
            self.assertEqual((rows[scr.set_sel], scr.set_buf), ("web.port", "9000"))
            self.assertEqual(self.cmds(), [])
            self.click("log", row=0); self.assertEqual((scr.tab, scr.set_buf), ("log", None))   # a tab leaves, as tab does
            self.assertEqual(self.cmds(), [])
            self.press("9"); self.click("→", row=38)
            self.assertEqual((self.cmds(), scr.modal["type"]), ([], "confirm"))  # ← → step it, once it is sure
            self.press("y"); self.assertEqual([c["key"] for c in self.cmds()], ["web.port"])

    def test_settings_scrolled(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, h=30, w=100)
            self.press("5"); scr.var_sel = "band80"; self.press("e")
            rows = scr.set_rows()
            scr.set_sel = len(rows) - 1; self.sync()
            for y, x0, x1, what, (i, j) in [h for h in scr.hits if h[3] == "set" and h[4][1] is None]:
                self.assertIn(rows[i].label, self.text().split("\n")[y])         # the row drawn there is the one picked
                self.assertTrue(1 < y < 28)

    def test_variant_editor(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            rows = scr.set_rows()
            i = next(i for i, r in enumerate(rows) if r.kind == "slots")
            spots = [(y, x0, arg[1]) for y, x0, x1, what, arg in scr.hits if what == "set" and arg[0] == i and arg[1] is not None]
            self.assertEqual(len(spots), len(scr.set_choices(rows[i])))          # each choice is its own
            for y, x, j in spots:
                self.assertEqual(self.text().split("\n")[y][x:].split()[0], str(scr.set_choices(rows[i])[j]))
            y, x, j = spots[1]
            before = list(scr.set_value(rows[i].key))
            self.mouse(y, x); self.assertEqual((scr.set_sel, scr.set_sub), (i, j))
            self.assertEqual(list(scr.set_value(rows[i].key)), before)
            self.mouse(y, x); self.assertNotEqual(list(scr.set_value(rows[i].key)), before)   # again: on / off
            draft = dict(scr.draft); self.assertTrue(draft)
            self.click("spot", row=0)                                            # the tabs don't drop a draft
            self.assertEqual((scr.tab, scr.overlay, scr.draft), ("variants", "edit", draft))
            self.assertIn("esc", scr.flash)
            scr.flash = ""; self.sync()
            self.click("discard", row=38)                                        # changes: it asks
            self.press("n"); self.assertEqual((scr.overlay, scr.draft), ("edit", draft))
            scr.flash = ""; self.sync()
            self.click("discard", row=38); self.press("y"); self.assertIsNone(scr.overlay)
            self.open(); self.click("discard", row=38); self.assertIsNone(scr.overlay)   # none: it doesn't
            self.assertEqual(self.cmds(), [])

    def test_questions_take_no_clicks(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5"); self.click("ive", row=38)
            self.assertEqual(scr.modal["type"], "arm")                           # L, as the key: live is to be typed
            modal = dict(scr.modal)
            self.click("spot", row=0)
            y, x = self.at("╯"); self.mouse(y, x)
            self.assertEqual((scr.modal, scr.tab), (modal, "variants"))
            self.assertEqual(self.cmds(), [])

    def test_other_buttons_and_empty_places(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            y, x = self.at("ladder", row=0)
            self.mouse(y, x, self.curses.BUTTON3_PRESSED); self.assertEqual(scr.tab, "spot")
            self.mouse(y, x, self.curses.BUTTON1_RELEASED); self.assertEqual(scr.tab, "spot")
            self.mouse(39, 119); self.mouse(20, 60); self.assertEqual(scr.tab, "spot")
            scr.s.resize(20, 80); scr.draw()                                     # too small: nothing is drawn, so nothing is there
            self.mouse(y, x); self.assertEqual(scr.tab, "spot")

    def test_events_that_came_together_all_count_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            c, (y, x), (_, x2) = self.curses, self.at("ladder", row=0), self.at("book", row=0)
            self.events((y, x2, c.BUTTON1_PRESSED), (y, x2, c.BUTTON1_RELEASED), (y, x, c.BUTTON1_PRESSED), (y, x, c.BUTTON1_RELEASED))
            self.assertEqual(scr.tab, "ladder")
            self.press("5"); names = [v["name"] for v in scr.st["variants"]]; scr.var_sel = names[0]; self.sync()
            self.events(*[(10, 10, c.BUTTON5_PRESSED)] * 3); self.assertEqual(scr.var_sel, names[3])
            self.assertTrue(self.events())                                       # a KEY_MOUSE with nothing behind it

    def test_no_press_or_release_is_left_out_of_the_mask(self):
        """getch() blocks on an event outside it: a click's release froze the screen until the next key."""
        import curses
        from tern import dash
        for n in range(1, 6):
            for what in ("PRESSED", "RELEASED"):
                self.assertTrue(dash.MOUSE_EVENTS & getattr(curses, f"BUTTON{n}_{what}"))
            self.assertFalse(dash.MOUSE_EVENTS & getattr(curses, f"BUTTON{n}_CLICKED"))   # a press and release made one

    def test_before_the_engines_first_status(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            scr.raw = {}; scr.st = {}
            from tern import dash
            for name in dash.BOXES:
                scr.tab = name; scr.draw()
                for y, x0, x1, what, arg in list(scr.hits):
                    if what not in ("tab", "key"):
                        scr.click(y, x0); scr.draw()

    def test_the_web_clicks_settings_and_changes_nothing(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            k, _ = self.setup(d)
            hub = Hub(d)
            f = hub.handle({"w": 120, "h": 40, "prefs": {"tab": "settings"}})
            scr = hub.sessions[f["sid"]][0]
            go = lambda y, x: hub.handle({"sid": f["sid"], "w": 120, "h": 40, "click": {"y": y, "x": x}})
            for i, r in enumerate(scr.set_rows()):
                if r.kind in ("bool", "choice") and r.key != "screen.log":
                    y, x = next((y, x0) for y, x0, x1, what, arg in scr.hits if what == "set" and arg == (i, None))
                    go(y, x + 3); go(y, x + 3)
                    self.assertEqual(scr.set_sel, i)
            self.assertIn("view only", scr.flash)
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])


class StrayClicks(MouseRig, unittest.TestCase):
    """What would change something asks first."""

    def asks(self, *want):
        """A question is open that only y answers with yes; any other key, and clicks, change nothing."""
        scr = self.scr
        self.assertEqual((scr.modal or {}).get("type"), "confirm")
        self.assertEqual(self.cmds(), [])
        text = " ".join(str(l) for l in scr.modal["lines"])
        for w in want:
            self.assertIn(w, text)
        self.assertIn("any other key", text)

    def test_variant_on_off(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5"); scr.var_sel = "band90"; self.sync()
            self.press(" "); self.asks("band90", "off")                          # the key asks too
            for other in ("n", 27, " ", 10):
                self.press(other); self.assertIsNone(scr.modal); self.assertEqual(self.cmds(), [])
                self.assertIn("kept on", scr.flash)
                scr.flash = ""; self.sync()
                self.click("on/off", row=38); self.asks("off")
            y, x = self.at("ladder", row=0); self.mouse(y, x); self.asks("off")  # a second stray click
            self.press("y"); [c] = self.run_cmds()
            self.assertEqual((c["op"], c["variant"], c["on"]), ("variant_on", "band90", False))
            self.press(" "); self.asks("band90", "on"); self.press("Y")
            self.assertEqual([c["on"] for c in self.cmds()], [True])

    def test_turning_off_the_one_live_trades_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.live.enabled = True; k.cfg.live.variant = "base"; self.sync()
            self.press("5"); scr.var_sel = "base"; self.sync()
            self.press(" "); self.asks("base", "live trades it")
            self.press("y"); self.run_cmds()
            self.press(" "); self.assertEqual(scr.modal["type"], "livecmd")      # on again: live is to be typed, as before

    def test_live_off(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.live.enabled = True; k.cfg.live.variant = "base"; scr.variant = "base"; self.sync()
            self.press("4"); self.click("ive", row=38); self.asks("live off")
            self.press("x"); self.assertIn("live kept on", scr.flash); self.assertEqual(self.cmds(), [])
            scr.flash = ""; self.sync()
            self.click("ive", row=38); self.press("y")
            self.assertEqual([(c["op"], c["on"]) for c in self.cmds()], [("live", False)])
            self.press("L")                                                      # the key is at once: the way out of live
            self.assertIsNone(scr.modal)
            self.assertEqual([(c["op"], c["on"]) for c in self.cmds()], [("live", False)])

    def test_engine_start(self):
        from unittest import mock
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            with mock.patch.object(dash, "engine_alive", return_value=False), mock.patch.object(dash, "start_engine", return_value="started") as start:
                scr.alive = False
                self.press("6", "P"); self.asks("Start the engine?")
                self.press("n"); self.assertIn("not started", scr.flash); start.assert_not_called()
                self.press("P", "y"); start.assert_called_once(); self.assertEqual(scr.flash, "started")

    def test_start_says_when_live_is_on(self):
        from unittest import mock
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.live.enabled = True; k.cfg.live.variant = "base"; self.sync()
            with mock.patch.object(dash, "engine_alive", return_value=False):
                self.press("P"); self.asks("Live is on")

    def test_clicked_settings(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("9")
            for key in ("alerts", "web.network"):
                y, x = self.set_row(key); self.mouse(y, x + 3); self.assertIsNone(scr.modal)
                y, x = self.set_row(key); self.mouse(y, x + 3); self.asks("from")
                self.press("n"); self.assertEqual(self.cmds(), [])
            y, x = self.set_row("alerts"); self.mouse(y, x + 3)
            y, x = self.set_row("alerts"); self.mouse(y, x + 3); self.press("y")
            self.assertEqual([c["key"] for c in self.cmds()], ["alerts"])
            self.press(10); self.assertIsNone(scr.modal)                         # keys change it as before
            self.assertEqual(len(self.cmds()), 1)
            y, x = self.set_row("screen.log"); self.mouse(y, x + 3)
            y, x = self.set_row("screen.log"); self.mouse(y, x + 3)              # this screen's own: nothing to ask
            self.assertIsNone(scr.modal); self.assertNotEqual(scr.log_filter, "all")

    def test_clicked_settings_while_armed_still_have_live_typed(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.live.enabled = True; k.cfg.live.variant = "base"; self.sync()
            self.press("9"); scr.set_sel = [r.key for r in scr.set_rows()].index("gate.min_days"); self.sync()
            self.click("←", row=38); self.assertEqual(scr.modal["type"], "setlive")

    def test_quit(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("6"); self.click("uit", row=38); self.asks("Close")
            self.assertTrue(scr.key(ord("n"))); self.assertIsNone(scr.modal)
            self.assertFalse(scr.key(ord("q")))                                  # the key closes it, as before

    def test_a_question_gone_stale_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5"); self.new(k, "base", "mine"); self.sync()
            mine = next(v["name"] for v in scr.st["variants"] if v["own"])
            scr.var_sel = mine; self.sync(); self.press(" "); self.asks("mine")
            k._command({"op": "variant_delete", "variant": mine}); self.sync()
            self.press("y"); self.assertEqual(self.cmds(), [])
            self.assertIn("gone", scr.flash)


class Renames(Rig, unittest.TestCase):
    """N: variants get new names; a record keeps its own."""

    def test_rename_variant_is_display_only(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("band90 is now high", k._command({"op": "rename", "variant": "band90", "name": "high"}))
            self.assertIn("unknown variant", k._command({"op": "rename", "variant": "stop20", "name": "x"}))
            self.assertIn("already base's name", k._command({"op": "rename", "variant": "late", "name": "BASE"}))
            self.assertIn("already band90's name", k._command({"op": "rename", "variant": "late", "name": "High"}))
            self.assertIn("at most 12", k._command({"op": "rename", "variant": "late", "name": "x" * 13}))
            st = self.status(k)
            self.assertEqual(st["variant_names"], {"band90": "high"})
            self.assertIn("band90", [v["name"] for v in st["variants"]])     # the record keeps its key
            from tern import config
            self.assertIn("live armed with high", k._command({"op": "live", "on": True, "variant": "band90"}))
            self.assertEqual(config.load(d).live.variant, "band90")          # config.json keeps the key too
            self.assertEqual(self.desk(d).names.variant("band90"), "high")    # kept across a restart
            self.assertIn("is now band90 again", k._command({"op": "rename", "variant": "band90", "name": ""}))
            self.assertEqual(self.status(k)["variant_names"], {})

    def test_bad_names_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "names.json"), "w") as fh:
                fh.write('{"profile": 5, "variants": {"nope": "x", "base": "  ", "late": "slow"}}')
            k = self.desk(d)
            self.assertEqual(k.names.variants, {"late": "slow"})


class SettingsBox(Rig, unittest.TestCase):
    """The desk's settings and the screen's, and nothing of a variant's."""

    def test_it_holds_the_desks_settings_only(self):
        from tern import config as cfgmod
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("9")
            rows = [r.key for r in scr.set_rows()]
            self.assertEqual(rows, [r.key for r in cfgmod.SETTINGS] + ["app.pair", "screen.log"])
            self.assertFalse([x for x in rows if x.startswith(("rule.", "strategy.")) or x == "paper_capital"])
            self.assertNotIn("older than this screen", self.text())
            self.assertIsNone(scr.set_sent.get("web.port"))
            scr.set_sel = rows.index("web.port"); self.press(self.curses.KEY_RIGHT)
            [c] = self.cmds()
            self.assertEqual((c["op"], c["key"], c["value"]), ("set", "web.port", 8788))

    def test_lowering_a_gate_asks_while_armed(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.live.enabled = True; k.cfg.live.variant = "base"; self.sync()
            scr.variant = "band90"; self.sync(); self.cmds()                   # whichever variant is selected
            self.press("9")
            rows = [r.key for r in scr.set_rows()]
            for key in ("gate.min_days", "gate.min_entries", "gate.min_pnl"):
                scr.set_sel = rows.index(key)
                self.press(self.curses.KEY_LEFT)
                self.assertEqual(scr.modal["type"], "setlive", key)
                self.press(27)
            self.assertEqual(self.cmds(), [])
            scr.set_sel = rows.index("gate.min_days")
            self.press(self.curses.KEY_RIGHT)                                  # raising it does not
            self.assertEqual([(c["op"], c["key"], c["value"]) for c in self.cmds()], [("set", "gate.min_days", 15)])
            scr.set_sel = rows.index("gate.min_days")
            self.press(self.curses.KEY_LEFT, *"live", 10)
            self.assertEqual([c["op"] for c in self.cmds()], ["set"])

    def test_it_scrolls_on_a_short_screen_and_shows_the_web_service(self):
        from unittest import mock
        from tern import dash, web
        with tempfile.TemporaryDirectory() as d, mock.patch.object(webctl, "systemctl", lambda *a: (0, "active")):
            k = self.desk(d); k.seeded.add(self.T)
            with mock.patch.object(dash, "Pal", web.WebPal):
                scr = dash.Screen(web.VTerm(31, 104), d)
            k._read_view(); k._status(); scr.read(); scr.key(ord("9"))
            text = lambda: "\n".join("".join(r) for r in scr.s.ch)
            scr.set_sel = [r.key for r in scr.set_rows()].index("web.port"); scr.draw()
            self.assertIn("▸ port", text())
            self.assertIn("WEB SCREEN NOW", text()); self.assertIn("running", text()); self.assertIn("http", text())
            self.assertIn("sudo ufw allow PORT/tcp", " ".join(" ".join(l.split("│")[2] for l in text().splitlines() if l.count("│") == 3).split()))   # its description, beside it
            scr.set_sel = 0; scr.draw()
            self.assertIn("▸ days of hours traded", text())
            self.assertNotIn("WEB SCREEN NOW", text())

    def test_no_change_before_the_engine_has_said_anything(self):
        from unittest import mock
        from tern import dash, web
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(dash, "Pal", web.WebPal):
                scr = dash.Screen(web.VTerm(40, 120), d)
            scr.draw()
            for key in "reDL ":
                scr.key(ord(key)); scr.draw()
                self.assertIsNone(scr.overlay, key); self.assertIsNone(scr.modal, key)
            scr.key(ord("9"))
            rows = [r.key for r in scr.set_rows()]
            for key, ch in (("alerts", ord(" ")), ("gate.min_days", 261)):
                scr.set_sel = rows.index(key); scr.key(ch)
            scr.draw()
            self.assertEqual(os.listdir(scr.cmd_dir), [])

    def test_numbers_are_the_tabs_until_a_value_is_being_typed(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("9", self.curses.KEY_DOWN)                              # settled entries
            self.press("3"); self.assertEqual((scr.tab, scr.set_buf), ("ladder", None))
            self.press("9", 10, "4", "0", 10)
            [c] = self.cmds()
            self.assertEqual((c["key"], c["value"], scr.tab), ("gate.min_entries", 40, "settings"))
            self.press(10, "7", 27)                                            # esc leaves it as it was
            self.assertEqual((scr.set_buf, scr.tab, self.cmds()), (None, "settings", []))
            self.press(10, "7", 9)                                             # so does leaving the box
            self.assertEqual((scr.set_buf, scr.tab, self.cmds()), (None, "themes", []))
            self.press("9", "q" if False else "x", "e", "s")                   # nothing else is its
            self.assertEqual((scr.tab, scr.overlay, self.cmds()), ("settings", None, []))


class AppRows(Rig, unittest.TestCase):
    """The settings box's APP rows: pairing a phone and the phones paired."""

    def screen(self, d):
        self.dpath = os.path.join(d, "app-devices.json")
        p = mock.patch.object(devices, "FILE", self.dpath); p.start(); self.addCleanup(p.stop)
        k, scr = self.setup(d)
        self.press("9")
        return k, scr

    def row(self, scr, key):
        scr.set_sel = [r.key for r in scr.set_rows()].index(key); scr.draw()

    def test_rows_pairing_question_and_removal(self):
        from tern import config as cfgmod
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.screen(d)
            keys = [r.key for r in scr.set_rows()]
            self.assertEqual(keys, [r.key for r in cfgmod.SETTINGS] + ["app.pair", "screen.log"])
            self.assertIn("APP", self.text()); self.assertIn("pair a phone", self.text())
            self.assertIn("the app", self.text()); self.assertIn("browsers", self.text())
            self.row(scr, "app.pair"); self.press(10)
            m = scr.modal
            self.assertEqual(m["type"], "pair"); self.assertRegex(m["code"], r"^\d{6}$")
            self.assertIn("it lasts 5 minutes", self.text()); self.assertIn("esc closes", self.text())
            self.press(27); self.assertIsNone(scr.modal)
            self.row(scr, "app.pair"); self.press(10)
            code = scr.modal["code"]
            self.tmp = mock.Mock(); self.tmp.name = d
            e, l, signer = Devices._phone(self)
            live = scr.fast
            oid = devices.offer(live, code, "pixel", e, l, signer(f"pair\n{code}\npixel\n{e}\n{l}", "everyday"), time.time())
            scr.pair_at = 0; self.sync()
            self.assertEqual(scr.modal["type"], "offer")                         # the code box gave way to the question
            self.assertIn("pair pixel?", self.text()); self.assertIn(devices.words(e, l), self.text())
            self.press("n")
            self.assertEqual(devices.offer_state(live, oid, time.time())["state"], "declined")
            self.assertIsNone(scr.modal)
            self.row(scr, "app.pair"); self.press(10); code = scr.modal["code"]
            oid = devices.offer(live, code, "pixel", e, l, signer(f"pair\n{code}\npixel\n{e}\n{l}", "everyday"), time.time())
            scr.tab = "log"; scr.pair_at = 0; self.sync()                                         # on any tab
            self.assertIn("pair pixel?", self.text())
            self.press("y")
            self.assertEqual(devices.offer_state(live, oid, time.time())["state"], "paired")
            scr.tab = "settings"; scr.phones_at = 0; self.sync()
            [phone] = devices.Devices(self.dpath).all()
            self.assertIn(f"app.phone.{phone['id']}", [r.key for r in scr.set_rows()])
            self.assertIn("pixel", self.text()); self.assertIn("paired", self.text())
            self.row(scr, f"app.phone.{phone['id']}"); self.press("D")
            self.assertEqual(scr.modal["type"], "confirm")
            self.press("x")                                                      # any other key keeps it
            self.assertEqual(len(devices.Devices(self.dpath).all()), 1)
            self.press("D", "y")
            self.assertEqual(devices.Devices(self.dpath).all(), [])
            self.assertNotIn(f"app.phone.{phone['id']}", [r.key for r in scr.set_rows()])


class Tabs(Rig, unittest.TestCase):
    """One box fills the screen; the others are tabs on its top border, its keys are on its bottom border."""

    def size(self, H, W):
        self.scr.s.resize(H, W); self.sync()

    def rows(self):
        H, _ = self.scr.s.getmaxyx()
        return "".join(self.scr.s.ch[0]), "".join(self.scr.s.ch[H - 2])

    def test_numbers_and_tab_show_a_box(self):
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.assertEqual(scr.tab, "spot")
            self.assertEqual(len(dash.BOXES), len(dash.BOX_KEYS))
            for key, name in zip(dash.BOX_KEYS, dash.BOXES):
                self.press(key)
                self.assertEqual(scr.tab, name)
                self.assertTrue(all(r[1] + r[3] <= 120 and r[0] + r[2] <= 39 for r in scr.rects.values()))
                top, _ = self.rows()
                for other, sup in zip(dash.BOXES, [*dash.SUPER, ""]):           # every box has a tab, whichever is shown
                    self.assertIn(f"{sup}{other} ", top)
                at = [top.index("help") - 1 if name == "help" else top.index(name) for name in dash.BOXES]
                self.assertEqual(at, sorted(at))
                lit = {n for n, x in zip(dash.BOXES, at) if scr.s.at[0][x + 1] & self.curses.A_REVERSE}
                self.assertEqual(lit, {name})                                   # the one on screen is the bright one
                dark = {(scr.s.at[0][x + 1] >> 9) & 0x1FF for n, x in zip(dash.BOXES, at) if n != name}
                self.assertEqual(dark, {scr.P.t["tab"] + 1})                     # the others all on one background,
                for i, (n, x) in enumerate(zip(dash.BOXES, at)):                # their numbers too
                    bg = (lambda a: (a >> 9) & 0x1FF)
                    if n != "help":                                             # (its key is its h)
                        self.assertEqual(bg(scr.s.at[0][x - 1]), bg(scr.s.at[0][x + 1]))
                    self.assertEqual(scr.s.ch[0][x + len(n) + (1 if n != "help" else 2)], "─")   # the border joins them
                self.assertNotIn("▏", top); self.assertNotIn("│", top)
            seen = []
            for key in (9, 9, self.curses.KEY_BTAB, self.curses.KEY_BTAB, self.curses.KEY_BTAB):
                self.press(key); seen.append(scr.tab)
            self.assertEqual(seen, ["spot", "contract", "spot", "help", "themes"])   # tab wraps, shift+tab goes back
            self.press("5")
            self.press("v", "p", "n", "N", "R", "g", "o", "s", "?", 27, 260, 261)   # nothing there any more
            self.assertEqual((scr.tab, scr.overlay, scr.modal, self.cmds()), ("variants", None, None, []))

    def test_tabs_fit_the_smallest_screen(self):
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            for W in (100, 104, 108, 111, 148):
                self.size(30, W)
                for key in dash.BOX_KEYS:
                    self.press(key)
                    top = self.rows()[0]
                    self.assertTrue(top.endswith("╮") and top.startswith("╭"), (W, top))
                    self.assertIn("help ", top)

    def test_each_box_has_its_keys_below_and_the_log_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            want = {"1": ["Coin", "chart 15m"], "2": ["Coin", "↑↓ strike"], "3": ["Coin", "↑↓ strike"], "4": ["View", "Live", "↑↓"],
                    "5": ["⏎ select", "edit", "space on/off", "rename", "Delete", "Live", "↑↓"],
                    "6": ["Pause" if scr.alive else "P start", "quit", "↑↓"], "7": ["Live", "↑↓"], "8": ["save", "↑↓ pick", "PgUp PgDn scroll"],
                    "9": ["↑↓ pick", "←→ step", "enter type"], "0": ["↑↓ ←→ pick"], "h": ["↑↓"]}
            for H, W in ((30, 100), (31, 104), (50, 160)):
                self.size(H, W)
                for key, hints in want.items():
                    scr.flash = ""; self.press(key)
                    top, low = self.rows()
                    for h in hints:
                        if h != "↑↓":                                           # (the first to go when they don't all fit)
                            self.assertIn(h, low, (key, W, low))
                    for x in ("Coin", "View", "Delete", "quit", "save", "enter type"):
                        if x not in hints:
                            self.assertNotIn(x, low, (key, W, low))

    def test_the_contract_says_the_bet(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("2")                                                     # without having seen the ladder
            self.assertIn("BTC above $", self.text().splitlines()[2])

    def test_the_gate_and_the_help_scroll(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            for _ in range(12):                                                 # the gate lists every variant
                self.new(k)
            c = self.curses
            for key, down, up in (("7", c.KEY_DOWN, c.KEY_UP), ("h", c.KEY_NPAGE, c.KEY_PPAGE)):
                self.press(key)
                if key == "h":
                    self.press(c.KEY_DOWN)                                      # the variants: more than fits
                    self.assertIn("VARIANTS", self.text()); self.assertNotIn("ABOUT", self.text())
                first = self.text().splitlines()[1:-3]
                self.assertRegex(self.rows()[1], r" 1-\d+ of \d+ ")
                self.press(down)
                self.assertNotEqual(self.text().splitlines()[1:-3], first)
                self.press(c.KEY_END, down); end = self.text().splitlines()[:-1]
                self.assertNotIn("▼", "\n".join(end))
                self.press(up)                                                  # no slack built up past the end
                self.assertNotEqual(self.text().splitlines()[:-1], end)
                self.press(c.KEY_HOME)
                self.assertEqual(self.text().splitlines()[1:-3], first)
            for _ in range(9):
                self.press(c.KEY_DOWN)                                          # stops at the last section
            self.assertIn("COMMANDS", self.text()); self.assertIn("tern live-check", self.text())

    def test_the_gates_variants_scroll_under_its_checks(self):
        for H, W in ((30, 100), (32, 148)):
            with tempfile.TemporaryDirectory() as d:
                k, scr = self.setup(d, H, W)
                c = self.curses
                for n in range(24):
                    self.new(k, name=f"v{n}")
                self.sync(); self.press("7")
                fixed = ("PAPER CHECKS", "GO LIVE", "LIMITS", "VARIANTS", "variant ")
                for keys in ((), (c.KEY_DOWN,), (c.KEY_NPAGE, c.KEY_NPAGE), (c.KEY_END,)):
                    self.press(*keys) if keys else None
                    for want in fixed:
                        self.assertIn(want, self.text(), (H, keys))
                self.assertIn(" v23 ", self.text()); self.assertNotRegex(self.text(), r"base +\d+ +\d+ +\d+ "); self.assertNotIn("▼", self.text())
                self.assertRegex(self.rows()[1], r" \d+-34 of 34 ")
                self.press(c.KEY_HOME); self.assertRegex(self.text(), r"base +\d+ +\d+ +\d+ "); self.assertIn("▼", self.text())

    def test_every_help_section_draws_at_every_size(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            for H, W in ((30, 100), (31, 104), (44, 160), (60, 240)):
                self.size(H, W); self.press("h", self.curses.KEY_HOME)
                for j, (title, _) in enumerate(scr.help_sections(W - 22)):
                    scr.help_sel = j; scr.draw()
                    self.assertIn(title, self.text())
                    for r in scr.s.ch[1:H - 2]:                                  # nothing runs past the box
                        self.assertIn("".join(r)[-1], "│")


    def test_t_steps_the_theme_on_every_box(self):
        from tern import dash, themes
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 31, 104)
            for key in dash.BOX_KEYS:
                self.press(key); at = themes.NAMES.index(scr.P.name)
                self.press("t"); self.assertEqual(scr.P.name, themes.NAMES[at + 1], key)
                self.press("t", "T"); self.assertEqual((scr.P.name, scr.tab), (themes.NAMES[at + 1], dash.BOXES[dash.BOX_KEYS.index(key)]))
            at = scr.P.name
            self.press("5", "e", "t")                                           # while editing a variant too
            self.assertNotEqual(scr.P.name, at); self.assertEqual((scr.overlay, self.cmds()), ("edit", []))
            at = scr.P.name
            self.press("s", "t"); self.assertEqual((scr.modal["buf"], scr.P.name), ("t", at))   # not while typing a name
            self.press(27, 27, "9", 10, "t"); self.assertEqual(scr.P.name, at)  # or a value
            self.press(27, "6"); self.assertIn(f"─theme {scr.P.name}─", self.rows()[1])

    def test_every_box_starts_a_line_under_its_tabs(self):
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 31, 104)
            for key in dash.BOX_KEYS:
                self.press(key)
                self.assertEqual("".join(scr.s.ch[1]).strip("│ ▲").replace("│", "").strip(), "", key)

    def test_every_box_draws_in_8_colors(self):
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            scr.P.many = False
            for key in dash.BOX_KEYS:
                self.press(key)
                top = self.rows()[0]
                self.assertNotIn("│", top)
                self.assertGreaterEqual(top.count(" ─"), len(dash.BOXES) - 1, key)
            self.press("5", "e")
            self.assertIn("CHANGES", self.text())

    def test_a_boxs_keys_work_only_on_it(self):
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            for key, name in zip(dash.BOX_KEYS, dash.BOXES):
                self.press(key); self.cmds()
                state = lambda: (scr.tab, scr.flash, scr.modal, scr.overlay, scr.series, scr.chart, scr.book_view, scr.variant, scr.var_sel, self.cmds())
                for ch, boxes in [*dash.KEYS_ON.items(), (self.curses.KEY_ENTER, ("variants",))]:
                    if name in boxes or name == "settings" and ch in ("\n", "\r", " ", self.curses.KEY_ENTER):   # (settings' own too)
                        continue
                    before = state()
                    self.press(ch)
                    self.assertEqual(state(), before, (name, ch))
            self.press("5", self.curses.KEY_ENTER); self.assertEqual(scr.variant, scr.var_sel)
            self.press("1", "c"); self.assertEqual(scr.chart, "1h")
            self.press("3", "C"); self.assertEqual(scr.series, "KXETHD")
            self.press("7", "L"); self.assertIsNotNone(scr.modal); self.press(27)
            self.press("4", "V"); self.assertEqual(scr.book_view, "live")
            self.press("h", "t"); self.assertIn("theme", scr.flash)             # the log's work everywhere

    def test_the_web_says_view_only_only_there(self):
        from tern import dash
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            hub = Hub(d); sid = hub.handle({"w": 120, "h": 40})["sid"]
            w = hub.sessions[sid][0]
            for key in dash.BOX_KEYS:
                hub.handle({"sid": sid, "w": 120, "h": 40, "key": key})
                for ch in ("r", "D", "L", "s", " ", "P"):
                    w.flash = ""
                    hub.handle({"sid": sid, "w": 120, "h": 40, "key": ch})
                    there = w.tab in dash.KEYS_ON.get(ch, dash.BOXES)
                    if not (w.tab == "settings" and ch == " "):
                        self.assertEqual("view only" in w.flash, there, (w.tab, ch))

    def test_the_variants_graph_keeps_its_size(self):
        for H, W, room in ((30, 100, 13), (32, 148, 14), (44, 160, 23)):
            with tempfile.TemporaryDirectory() as d:
                k, scr = self.setup(d, H, W)
                self.press("5")
                at, labels = [], lambda: [i for i, l in enumerate(self.text().splitlines()) if "the curve starts after" in l]
                for n in range(24):                                            # 10 built in, up to 24 of your own
                    at.append((labels(), next(i for i, l in enumerate(self.text().splitlines()) if "base: the rule" in l or ": " in l and "every slot" in l)))
                    self.new(k, name=f"v{n}"); self.sync(); scr.draw()
                self.assertEqual(len(set(map(str, at))), 1, (H, W, at))        # in one place whatever the list holds
                names = [v["name"] for v in scr.st["variants"]]
                self.assertEqual(len(names), 34)
                rows = lambda: [l for l in self.text().splitlines()[3:3 + room]]
                self.assertTrue(names[0] in rows()[0] and names[room - 1] in rows()[-1], rows())
                self.assertIn("▼", rows()[-1]); self.assertIn(f" 1-{room} of 34 ", self.rows()[1])
                for name in names[1:]:                                         # every one can be reached
                    self.press(self.curses.KEY_DOWN)
                    self.assertEqual(scr.var_sel, name); self.assertTrue(any(f" {name} " in l or f"+{name} " in l for l in rows()), name)
                self.assertNotIn("▼", rows()[-1]); self.assertIn(f" {35 - room}-34 of 34 ", self.rows()[1])
                self.press(self.curses.KEY_HOME); self.assertEqual(scr.var_sel, names[0])

    def test_the_palettes_are_kept_apart(self):
        with tempfile.TemporaryDirectory() as d:
            for H, W in ((30, 100), (31, 104), (44, 160)):
                k, scr = self.setup(d, H, W)
                self.press("0")
                got = [r for r, row in enumerate(scr.s.ch) if "█" in row]
                self.assertEqual(len(got), 14)
                self.assertTrue(all(b - a == 2 for a, b in zip(got, got[1:])))   # a blank line between
                for r in got:
                    self.assertNotIn("█▸", "".join(scr.s.ch[r]))

    def test_the_book_is_side_by_side(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            now = time.time()
            k.trades = None
            self.sync(); scr.raw = dict(scr.raw)
            book = {"equity": 5100.0, "start": 5000.0, "cash": 4900.0, "at_risk": 200.0, "fees": 3.5,
                    "pos": [{"t": "KXHYPED-E-T64.2499", "k": 64.2499, "q": -25, "avg": 0.94, "mark": 0.99, "pnl": 1.25, "close": now + 3000}] * 30,
                    "trades": [{"kind": "fill", "ticker": "KXETHD-E-T1689.99", "delta": 5, "price": 0.8, "fee": 0.06, "realized": 0, "ts": now},
                               {"kind": "settle", "ticker": "KXETHD-E-T1689.99", "delta": -5, "result": "yes", "realized": 1.0, "ts": now}]}
            st = {**scr.st, "paper": book, "mode": "paper"}
            scr.st = st; scr.tab = "book"; scr.draw()
            text = self.text()
            row = next(l for l in text.splitlines() if "OPEN 30" in l)
            self.assertLess(row.index("OPEN 30"), row.index("SETTLED"))
            for want in ("$5,100.00", "+$100.00 +2.00%", "$4,900.00", "$200.00", "$3.50"):
                self.assertIn(want, text)
            self.assertRegex(text, r"HYPE +64\.25 NO +25 +94\.0 +99\.0 +\+\$1\.25")
            self.assertRegex(text, r"ETH +1,690 YES +5 +won +\+\$1\.00"); self.assertRegex(text, r"ETH +1,690 YES +5 +80 +\$0\.06")
            self.assertIn("▼", text)
            scr.scroll("book", 10 ** 6); scr.draw()
            self.assertNotIn("▼", self.text()); self.assertIn("▲", self.text())

    def test_the_themes_box_picks_as_it_moves(self):
        import json
        from tern import themes
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 31, 104)
            self.press("0")
            for name in themes.NAMES:
                self.assertIn(f" {name} ", self.text())
            self.assertIn(f"▸ {scr.P.name}", self.text())
            at = themes.NAMES.index(scr.P.name)
            self.press(self.curses.KEY_DOWN)
            self.assertEqual(scr.P.name, themes.NAMES[at + 1])
            self.press(self.curses.KEY_RIGHT)
            self.assertEqual(scr.P.name, themes.NAMES[at + 1 + scr.theme_rows])
            self.press(self.curses.KEY_LEFT, self.curses.KEY_UP)
            with open(os.path.join(d, "dash.json")) as fh:
                self.assertEqual(json.load(fh)["theme"], themes.NAMES[at])
            self.press(self.curses.KEY_HOME, self.curses.KEY_UP); self.assertEqual(scr.P.name, themes.NAMES[0])
            self.press(self.curses.KEY_END, self.curses.KEY_DOWN, self.curses.KEY_RIGHT); self.assertEqual(scr.P.name, themes.NAMES[-1])
            self.press("3", self.curses.KEY_LEFT); self.assertEqual(scr.P.name, themes.NAMES[-1])   # only on that box

    def test_coins_and_the_chart(self):
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            seen = []
            for _ in config.SERIES:
                self.press("C"); seen.append(scr.series)
            self.assertEqual(seen, list(config.SERIES)[1:] + list(config.SERIES)[:1])
            self.press("c"); self.assertEqual(scr.chart, "1h")
            self.press("4", "c"); self.assertEqual(scr.chart, "1h")             # spot's key
            for want in ("live", "paper"):
                self.press("V"); self.assertEqual(scr.book_view, want)
            self.press("1", "V"); self.assertEqual(scr.book_view, "paper")      # the book's key

    def test_arrows_move_in_the_box_on_screen(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5", self.curses.KEY_DOWN)
            self.assertEqual(scr.var_sel, "band90")
            self.press("3", self.curses.KEY_DOWN); self.press("2", self.curses.KEY_UP)
            self.assertEqual((scr.var_sel, scr.sel), ("band90", self.T))
            for key in "16":
                self.press(key, self.curses.KEY_UP, self.curses.KEY_DOWN)
            self.assertEqual(scr.var_sel, "band90")

    def test_the_box_is_remembered_and_old_prefs_load(self):
        import json
        from unittest import mock
        from tern import dash, web
        with tempfile.TemporaryDirectory() as d:
            def screen(prefs):
                with open(os.path.join(d, "dash.json"), "w") as fh:
                    json.dump(prefs, fh)
                with mock.patch.object(dash, "Pal", web.WebPal):
                    return dash.Screen(web.VTerm(33, 111), d)
            for prefs, want in (({"tab": "book"}, "book"), ({"tab": "equity"}, "spot"), ({"tab": 5}, "spot"),
                                ({"page": 2, "hidden": ["log"], "zoom": "log", "profile": "no2", "watch": "band90"}, "spot")):
                s = screen(prefs); s.draw()
                self.assertEqual((s.tab, s.variant), (want, None), prefs)
            s.show_tab("log")
            with open(os.path.join(d, "dash.json")) as fh:
                saved = json.load(fh)
            self.assertEqual(saved["tab"], "log")
            self.assertFalse({"page", "hidden", "zoom", "profile", "watch", "slot3"} & set(saved))

    def test_a_long_list_of_variants_scrolls_to_the_highlighted_one(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            for _ in range(23):                                                 # more variants than a small screen has rows
                self.new(k)
            self.press("5")
            for H, W in ((30, 100), (33, 111)):
                self.size(H, W)
                for sel in ("base", "base-24"):
                    scr.var_sel = sel; self.sync()
                    self.assertIn(f" {sel} " if sel == "base" else f"+{sel}", self.text())

    def test_the_book_fits_a_small_screen(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            self.press("4")
            self.assertIn("P&L closes", self.text())

    def test_enter_selects_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5"); self.cmds()
            for name in ("band90", "late"):
                scr.var_sel = name
                self.press(10)
                self.assertEqual((scr.variant, scr.st["strategy"]["variant"]), (name, name))
            self.assertEqual(self.cmds(), [])
            self.press(10)
            self.assertIn("already selected", scr.flash)

    def test_a_flash_covers_the_keys_it_reaches(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 31, 104)
            self.press("6")
            scr.say("a message " * 8); self.sync()
            low = self.rows()[1]
            self.assertIn("a message a message", low)
            self.assertNotIn("Pause" if scr.alive else "P start", low)
            self.assertIn("quit", low)
            scr.say("x" * 300); self.sync()
            self.assertEqual(len(self.rows()[1]), 104)

    def test_status_line_fits_every_width(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            ev = k.events["KXBTCD"]
            ev["close"] = time.time() + 1400; ev["fetched"] = time.time() - 120     # a slot ahead, a stale ladder
            for v, armed in (("band90", False), ("base", True)):
                scr.variant, k.cfg.live.enabled, k.cfg.live.variant = v, armed, "band90"
                self.sync()
                for H, W in ((30, 100), (33, 111), (37, 151)):
                    self.size(H, W)
                    row = "".join(scr.s.ch[H - 1])
                    self.assertRegex(row, r"\S  +● (engine )?ok( +\d\d:\d\d(:\d\d)?)? *$")   # the engine's state, whole, at the end
                    self.assertIn("ladder 2m", row)                                  # stale data is never dropped
                    self.assertIn("rule", row)
                    self.assertIn("in ", row)                                   # when the next slot is
                    self.assertIn("closes", row)

    def flat(self, rows, ts):
        return ["".join(t for t, _ in r[4]).rstrip() for r in rows if r[0] == ts and r[3] and r[4]]

    def test_an_hours_shadow_lines_are_one_entry(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k._command({"op": "rename", "variant": "band90", "name": "high favs"})
            self.sync()
            line = lambda coin, hour, *v: f"shadow: KX{coin}D-{hour} settled · " + " · ".join(
                f"{n} {x}" for n, x in zip(("base", "band90", "multi", "calm"), v))
            logs = [[1.0, "info", line("BTC", "H1", "+10.00", "+5.00", "-1,130.00", "+1.00")],   # (as they were before 2026-11-24)
                    [2.0, "info", line("ETH", "H1", "+10.00", "+5.00", "-1,132.43", "+1.00")],
                    [3.0, "info", "paper: something between"],
                    [4.0, "info", line("BTC", "H2", "+8.50", "+5.00", "-1,132.43", "+1.00") + " · late +2.00"],
                    [4.5, "info", "live: a line in the middle"],
                    [5.0, "info", line("ETH", "H2", "+8.50", "+5.00", "-1,100.00", "+1.00") + " · late +2.00"],
                    [6.0, "info", line("BTC", "H3", "+8.50", "+5.00", "-1,100.00", "+1.00")],
                    [6.5, "info", line("BTC", "H4", "+9.50 (2/2 +1.00)", "+5.00", "-1,150.00 (3/5 -20.00)", "+1.00")],
                    [6.7, "info", line("ETH", "H4", "+9.50", "+5.00", "-1,140.00 (4/5 +10.00)", "+1.00")],
                    [7.0, "info", "shadow: odd line · no cells"]]
            rows = scr.log_rows(logs)
            self.assertEqual([(r[0], r[2], r[3]) for r in rows if not r[3]], [
                (2.0, "shadow: settled BTC ETH", 0), (3.0, "paper: something between", 0), (4.5, "live: a line in the middle", 0),
                (5.0, "shadow: settled BTC ETH", 0), (6.0, "shadow: settled BTC · no variant changed", 0),
                (6.7, "shadow: settled BTC ETH · 9 of 12 won", 0), (7.0, "shadow: odd line · no cells", 0)])
            self.assertEqual([x.split() for x in self.flat(rows, 2.0)[1:]], [["base", "+10.00"], ["high", "favs", "+5.00"],
                                                                              ["multi", "-1,132.43"], ["calm", "+1.00"]])   # nothing before it
            self.assertEqual([x.split() for x in self.flat(rows, 5.0)], [["variant", "this", "hour", "record"], ["base", "-1.50", "+8.50"],
                                                                          ["late", "+2.00", "+2.00"], ["multi", "+32.43", "-1,100.00"]])
            self.assertEqual([x.split() for x in self.flat(rows, 6.7)], [["variant", "won", "this", "hour", "record"],
                             ["base", "2", "of", "2", "+1.00", "+9.50"], ["multi", "7", "of", "10", "-10.00", "-1,140.00"]])
            for ts in (2.0, 5.0, 6.7):
                self.assertEqual(len({len(x) for x in self.flat(rows, ts)}), 1)   # aligned
            blank = [k for k, r in enumerate(rows) if r[3] and not r[2] and not r[4]]
            self.assertTrue(blank and all(b - a > 1 for a, b in zip(blank, blank[1:])) and blank[-1] < len(rows) - 1)   # one at a time
            self.press("6")
            for ts, lvl, msg in logs[3:]:
                k.log(msg)
            scr.flash = ""; self.sync()
            text = self.text()
            self.assertIn("shadow: settled BTC ETH · 9 of 12 won", text)
            self.assertRegex(text, r"multi +7 of 10 +-10\.00 +-1,140\.00")

    def test_a_long_line_wraps_under_its_text(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            msg = "new variant multi-5, from multi-2: max spread 9¢ → 5¢, coins BTC ETH SOL XRP → BTC ETH SOL, most an hour $200 → $30"
            rows = scr.log_rows([[1.0, "cmd", msg]], 60)
            self.assertGreater(len(rows), 2); self.assertEqual(" ".join(r[2] for r in rows), msg)
            self.assertEqual([r[3] for r in rows], list(range(len(rows))))
            self.press("6"); k.log(msg, "cmd"); self.sync()
            lines = [l for l in self.text().splitlines()]
            at = next(n for n, l in enumerate(lines) if "new variant multi-5" in l)
            self.assertRegex(lines[at + 1], r"^│ {12}\S")                       # no time, under the text
            self.assertEqual(len({l[scr.rects["log"][3] - scr.log_side(100)] for l in lines[1:28]}), 1)   # nothing crosses the line

    def test_the_logs_right_side(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            for H, W in ((30, 100), (32, 148), (44, 200)):
                k, scr = self.setup(d, H, W)
                now = time.time()
                for n, pnl in (("base", 5.0), ("multi", -7.5)):
                    k.shadow._count({"v": n, "event": f"KXBTCD-{n}", "pnl": pnl, "n": 5, "fee": 0, "close": now, "ticker": "KXBTCD-E-T1", "side": "yes",
                                     "cost": 0.8, "ts": now})
                k.log("Wi-Fi is out of date", "warn")
                self.sync(); scr.raw["spot"] = {"BTC-USD": {"now": 60000.0, "min": [[now - 60 * i, 59400.0] for i in range(90, 0, -1)]}}
                scr.apply_selected(); scr.read = lambda: None; self.press("6"); text = self.text()
                for want in ("COINS", "BTC", "+1.01%", "0.0 bp", "TODAY", "WARNINGS", "Wi-Fi is out of date"):
                    self.assertIn(want, text, (H, W))
                self.assertRegex(text, r"base +\+5\.00 +1 of 1"); self.assertRegex(text, r"multi +-7\.50 +0 of 1")
                self.assertLess(text.index("+5.00"), text.index("-7.50"))       # best first
                for r in scr.s.ch[1:H - 2]:
                    self.assertIn("".join(r)[-1], "│")

    def test_today_is_what_settled_since_midnight(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            t = time.localtime(); noon = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))
            row = lambda ev, close, pnl: {"v": "base", "event": ev, "pnl": pnl, "n": 1, "fee": 0, "close": close}
            k.shadow._count(row("A", noon - 86400, 9.0)); k.shadow._count(row("B", noon - 43200, 4.0))   # yesterday, and its last hour
            k.shadow._count(row("C", noon - 3600, -2.0)); k.shadow._count(row("C", noon - 3600, 3.0))
            self.assertEqual(k.shadow.today("base", noon), [1.0, 2, 1])
            k.shadow._count(row("D", noon, 1.0)); self.assertEqual(k.shadow.today("base", noon + 60), [2.0, 3, 2])
            self.assertEqual(k.shadow.today("base", noon + 86400), [0, 0, 0])   # tomorrow

    def test_a_slots_lines_are_one_entry(self):
        from tern import dash
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k._command({"op": "rename", "variant": "band90", "name": "high favs"})
            self.sync()
            logs = [[1.0, "info", "paper: 25m slot on KXBTCD-H1 bought · base 5 (25 for $21.25) · band90 12 (60 for $1,055.20) · multi 3 (15 for $12.00)"],
                    [1.1, "info", "paper: 25m slot on KXETHD-H1 bought · cap1 2 (10 for $8.00) · multi 4 (20 for $18.00)"],
                    [1.2, "info", "paper: 20m slot on KXBTCD-H1 bought · base 1"],      # another slot, as before 2026-11-24
                    [9.0, "info", "paper: 25m slot on KXBTCD-H2 bought · base 2"],      # another hour
                    [9.5, "info", "paper: 25m slot on KXBTCD-H2 bought nothing it should"]]
            self.assertTrue(all(dash.TRADE_LINE.match(l[2]) for l in logs[:4]))   # each one of the log filter's trades
            rows = scr.log_rows(logs)
            self.assertEqual([(r[0], r[2]) for r in rows if not r[3]], [
                (1.1, "paper: 25m slot bought BTC ETH · 26 buys · $1,114.45"), (1.2, "paper: 20m slot bought BTC · 1 buy"),
                (9.0, "paper: 25m slot bought BTC · 2 buys"), (9.5, "paper: 25m slot on KXBTCD-H2 bought nothing it should")])
            self.assertEqual([x.split() for x in self.flat(rows, 1.1)], [
                ["variant", "buys", "contracts", "cost", "avg", "coins"], ["base", "5", "25", "$21.25", "85¢", "BTC"],
                ["high", "favs", "12", "60", "$1,055.20", "1759¢", "BTC"], ["cap1", "2", "10", "$8.00", "80¢", "ETH"],
                ["multi", "7", "35", "$30.00", "86¢", "BTC", "3", "·", "ETH", "4"]])
            self.assertEqual([x.split() for x in self.flat(rows, 1.2)], [["variant", "buys", "coins"], ["base", "1", "BTC"]])
            self.press("6")
            for n in range(30):                                                # more than the box holds
                k.log(f"paper: 25m slot on KXBTCD-E{n} bought · base 5 (25 for $21.25) · band90 12 (60 for $55.00) · cap1 1 (5 for $4.00)")
            self.sync()
            self.assertRegex(self.text(), r"high favs +12 +60 +\$55\.00 +92¢ +BTC")
            for sh in (k.shadow,):                                              # and the engine writes them so
                import time
                now = time.time()
                [note] = sh.step("KXBTCD-N1", now + 25 * 60 - 1, [{"ticker": "KXBTCD-N1-T1", "floor_strike": 1.0, "yes_bid_dollars": "0.79", "yes_ask_dollars": "0.80",
                                 "yes_bid_size_fp": "500", "yes_ask_size_fp": "500"}], k.cfg.strategy, now, paper=1000.0)
                self.assertIn("base 1 (5 for $4.00)", note); self.assertTrue(dash.SLOT_LINE.match(note))
            for r in scr.s.ch:                                                  # nothing runs past the box
                self.assertIn("".join(r)[-1], "│╯╮ ")
            for _ in range(10):
                scr.scroll("log", -1)
            self.assertIsNotNone(scr.log_anchor)
            for _ in range(10):
                scr.scroll("log", 1)
            self.assertIsNone(scr.log_anchor)                                  # back to following new lines


class OwnVariants(Rig, unittest.TestCase):
    """Variants made on the screen: one set of settings a record, none changes once saved."""

    def test_saving_makes_one_new_variant_and_the_old_one_stays(self):
        from tern import shadow
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._shadow()
            before, rec = shadow.rule_of("band80", k.cfg, k.shadow.own), k.shadow.summary(["band80"])
            self.assertEqual(rec[0]["open"], 1)
            msg = self.new(k, "band80", band_lo=0.83, contracts=9, max_hour_cost=45, paper_capital=250)
            for want in ("new variant band80-2, from band80", "band low 80¢ → 83¢", "contracts a strike 5 → 9", "most an hour $60 → $45",
                         "paper start $1,000 → $250"):
                self.assertIn(want, msg)
            want = {**before, "band_lo": 0.83, "contracts": 9.0, "max_hour_cost": 45.0, "paper_capital": 250.0}
            self.assertEqual((k.shadow.own.active(), k.shadow.own.rule("band80-2")), (["band80-2"], want))
            self.assertEqual((shadow.rule_of("band80", k.cfg, k.shadow.own), k.shadow.summary(["band80"])), (before, rec))
            self.assertEqual(k.shadow.summary(["band80-2"])[0]["open"], 0)     # an empty record
            self.new(k, "band80-2", band_lo=0.84)                              # one of your own is copied too, never changed
            self.assertEqual((k.shadow.own.rule("band80-2"), k.shadow.own.rule("band80-3")["band_lo"]), (want, 0.84))
            self.assertEqual((k.slog.sv, k.cfg.live.variant), (1, ""))         # no settings period; live has no part in it
            self.assertEqual(self.desk(d).shadow.own.active(), ["band80-2", "band80-3"])   # kept across a restart

    def test_note_op_and_by_prefix(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._shadow()
            def run(c):
                with open(os.path.join(k.cmd_dir, f"{time.time():.6f}-{c['id']}.json"), "w") as fh:
                    json.dump({"ts": time.time(), **c}, fh)
                k._commands()
            run({"id": "n1", "op": "note", "text": "engine stopping\x07 now", "by": "app pixel"})
            self.assertEqual(k.results["n1"], "engine stopping now")        # the phone's result: no prefix
            self.assertIn("app pixel: engine stopping now", [str(x) for l in k.logs for x in l])
            run({"id": "n2", "op": "note", "text": "x" * 500})
            self.assertEqual(len(k.results["n2"]), 200)
            run({"id": "n3", "op": "variant_on", "variant": "nope", "on": True, "by": "app pixel"})
            self.assertEqual(k.results["n3"], "no variant 'nope'")
            self.assertIn("app pixel: no variant 'nope'", [str(x) for l in k.logs for x in l])
            run({"id": "n4", "op": "note", "text": "plain"})
            self.assertNotIn("None", k.results["n4"])
            self.assertFalse([1 for l in k.logs for x in l if str(x).startswith("None:") or str(x).startswith(": ")])

    def test_shutdown_logs_a_pending_note_and_leaves_no_file(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            with open(os.path.join(k.cmd_dir, f"{time.time():.6f}-n9.json"), "w") as fh:
                json.dump({"id": "n9", "op": "note", "text": "stopping the engine from the app", "by": "app pixel", "ts": time.time()}, fh)
            k._shutdown(quiet=True)
            self.assertIn("app pixel: stopping the engine from the app", [str(x) for l in k.logs for x in l])
            self.assertEqual(os.listdir(k.cmd_dir), [])

    def test_the_band_may_move_past_where_it_was(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, "band75", band_lo=0.85, band_hi=0.90)             # 75-80: each end alone would be refused
            self.assertEqual([k.shadow.own.rule("band75-2")[f] for f in ("band_lo", "band_hi")], [0.85, 0.90])

    def test_each_variant_buys_at_its_own_size_and_hourly_limit(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, bid="0.80", ask="0.81")
            for n, hour in ((2, 60), (9, 60), (50, 10)):
                self.new(k, contracts=n, max_hour_cost=hour)
            k._shadow()
            got = {r["v"]: r["n"] for r in k.shadow.rows}
            self.assertEqual([got[v] for v in ("base", "base-2", "base-3", "base-4")], [5, 2, 9, 12])   # $10 buys 12 at 81c
            self.assertTrue(any("25m slot" in m or "10m slot" in m for _, _, m in k.logs if m.startswith("paper:")))
            self.assertTrue(any(" bought · base 1 (" in m and " · base-4 1 (" in m for _, _, m in k.logs))

    def test_paper_start_is_the_variants(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, paper_capital=250)
            st = self.status(k)
            self.assertEqual([st["views"][v]["start"] for v in ("base", "base-2")], [1000, 250])
            self.assertEqual([v["cash"] for v in st["variants"] if v["name"] in ("base", "base-2")], [1000, 250])

    def test_variants_saved_before_sizes_get_the_configs_once(self):
        import json
        from tern import config, shadow
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, band_lo=0.80); self.new(k, band_lo=0.81)
            k.shadow.own.delete("base-3", used=True)
            path = os.path.join(d, "variants.json")
            with open(path) as fh:
                old = json.load(fh)
            for v in old["variants"].values():
                for f in ("contracts", "max_hour_cost", "paper_capital"):
                    del v["rule"][f]
            del old["off"]                                                     # (there was none yet)
            with open(path, "w") as fh:
                json.dump(old, fh)
            cfg = config.Config(paper_capital=100); cfg.strategy.contracts, cfg.strategy.max_hour_cost = 19.0, 100.0
            config.save(cfg, d)
            own = shadow.own_set(d)                                            # a report reads it: writes nothing
            with open(path) as fh:
                self.assertEqual(json.load(fh), old)
            self.assertEqual((own.active(), own.deleted(), own.damaged), (["base-2"], ["base-3"], False))
            self.desk(d)                                                       # the engine keeps them
            cfg.strategy.contracts = 3.0; config.save(cfg, d)                  # (a later change of config.json is not theirs)
            for key in ("base-2", "base-3"):
                rule = self.desk(d).shadow.own.rule(key)
                self.assertEqual([rule[f] for f in ("contracts", "max_hour_cost", "paper_capital")], [19, 100, 100])

    def test_commands_from_an_older_screen_change_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for op in ("variant_rule", "series", "variant", "reset_paper", "profile_set", "profile_new", "profile_delete", "live_profile"):
                msg = k._command({"op": op, "key": "band_lo", "value": 0.8, "series": "KXETHD", "name": "late", "profile": "1"})
                self.assertIn("unknown command", msg, op)
            self.assertEqual((k.shadow.own.all, k.cfg.live.variant), ({}, ""))

    def test_bad_changes_make_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for rule in ({"band_lo": 0.96}, {"band_lo": True}, {"band_lo": float("nan")}, {"minutes": []}, {"minutes": [12]},
                         {"series": ["KXFAKED"]}, {"cap": 9}, {"nope": 1}, {"max_spread": "x"}, {"band_lo": 0.8, "band_hi": "x"},
                         {"band_lo": 0.9, "band_hi": 0.8}, {"contracts": 0}, {"contracts": 101}, {"contracts": "5"},
                         {"contracts": True}, {"max_hour_cost": 4}, {"paper_capital": 1}, {"paper_capital": float("nan")},
                         {"contracts": 9, "cap": 9}, [1], "x"):
                msg = k._command({"op": "variant_new", "from": "base", "rule": rule})
                self.assertIn("no new variant", msg, rule)
            self.assertIn("unknown variant", self.new(k, "gone", band_lo=0.8))
            self.assertIn("when there were profiles", k._command({"op": "variant_new", "from": "base", "rule": {"cap": 1}, "profile": "no2"}))
            self.assertEqual(k.shadow.own.all, {})

    def test_own_variant_buys_by_its_own_rule_and_slots(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d, bid="0.58", ask="0.60", close=5 * 60 - 1)         # 59c at 5 minutes: neither is base's
            self.new(k, band_lo=0.55, minutes=[5])
            k._shadow()
            self.assertEqual({r["v"] for r in k.shadow.rows}, {"base-2"})
            self.assertEqual({r["slot"] for r in k.shadow.rows}, {5})

    def test_caps_are_the_rules_own(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, cap=1, max_vol=7.6)
            S = k._strat("base-2", k.cfg.strategy, k.shadow.own)
            self.assertEqual((S["cap"], S["max_vol"]), (1, 7.6))
            self.new(k, "base-2", cap=0)
            self.assertEqual((k.shadow.own.rule("base-3")["cap"], k.shadow.own.rule("base-2")["cap"]), (None, 1))

    def test_new_rename_delete(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("new variant My Rule", self.new(k, "band80", "My Rule"))
            self.assertEqual(k.shadow.own.active(), ["my-rule"])
            for bad in ("my rule", "BASE", "stop20", "x" * 13):
                self.assertIn("no new variant", self.new(k, "band80", bad), bad)
            k._command({"op": "rename", "variant": "band80", "name": "wide"})
            self.assertIn("no new variant", self.new(k, "band80", "wide"))     # a built-in one's display name
            self.assertIn("not renamed", k._command({"op": "rename", "variant": "late", "name": "My rule"}))
            self.assertIn("is now slow", k._command({"op": "rename", "variant": "my-rule", "name": "slow"}))
            self.assertEqual((k.shadow.own.active(), k.shadow.own.title("slow")), (["slow"], "slow"))
            dele = lambda v: k._command({"op": "variant_delete", "variant": v})
            self.assertIn("built in", dele("base"))
            self.assertIn("no variant", dele("gone"))
            k.cfg.live.enabled, k.cfg.live.variant = True, "slow"
            self.assertIn("what live trades", dele("slow"))
            k.cfg.live.enabled = False
            self.assertIn("slow deleted", dele("slow"))
            self.assertEqual(k.shadow.own.all, {})                             # it bought nothing: nothing is left

    def test_a_deleted_one_settles_stays_on_file_and_never_lends_its_key(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._command({"op": "variant_new", "name": "x1", "from": "base", "rule": {"cap": 6}})
            k._shadow()
            self.assertTrue(k.shadow.bought("x1"))
            k._command({"op": "variant_delete", "variant": "x1"})
            k.shadow.save()
            self.assertNotIn("x1", [r["name"] for r in k.shadow.summary()])
            k = self.desk(d)
            self.assertIn("x1", {r["v"] for r in k.shadow.rows})
            k.shadow.settle({self.T: True})
            self.assertEqual(k.shadow.summary(["x1"])[0]["entries"], 1)
            k._command({"op": "variant_new", "name": "x1", "from": "base", "rule": {"cap": 6}})
            self.assertEqual(k.shadow.own.active(), ["x1-2"])
            self.assertEqual(k.shadow.summary(["x1-2"])[0]["entries"], 0)

    def test_a_rename_moves_the_record_and_leaves_the_old_name_nowhere(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._command({"op": "variant_new", "name": "y", "from": "base", "rule": {"cap": 6}})
            k._shadow()                                                        # y buys: an open row and a day
            k.shadow.days["y"] = ["2026-11-25", 0.0, False]
            k.shadow.settle({self.T: True}); k.shadow.save()                   # settled on file
            k._command({"op": "variant_new", "from": "y", "rule": {"cap": 5}})  # made from y before the rename
            k._shadow()                                                        # an open row again
            k._command({"op": "variant_on", "variant": "y", "on": False})
            k.cfg.live.variant = "y"
            entries = k.shadow.summary(["y"])[0]["entries"]
            self.assertIn("variant y is now base-6", k._command({"op": "rename", "variant": "y", "name": "base-6"}))
            own = k.shadow.own
            self.assertEqual(own.active(), ["base-6", "y-2"])                  # in the order they were
            self.assertEqual((own.all["y-2"]["from"], own.off, k.cfg.live.variant), ("base-6", {"base-6"}, "base-6"))
            self.assertEqual(k.shadow.summary(["base-6"])[0]["entries"], entries)
            self.assertEqual(k.renamed, {"y": "base-6"})
            files = ["shadow.json", "shadow-settled.jsonl", "variants.json", "config.json", "settings-log.jsonl"]
            for f in files:
                self.assertNotRegex(open(os.path.join(d, f)).read(), r'"(on\.)?y"', f)
            k = self.desk(d)                                                   # and after a restart
            self.assertEqual(k.shadow.summary(["base-6"])[0]["entries"], entries)
            self.assertFalse(k.shadow.bought("y"))
            self.assertIn("keeps its name", k._command({"op": "rename", "variant": "base-6", "name": "base-6"}))
            # y-2 was a typo's copy: renamed, and a copy made from it later takes the new name, not y's
            k._command({"op": "rename", "variant": "y-2", "name": "base-7"})
            k._command({"op": "variant_new", "from": "base-7", "rule": {"cap": 4}})
            self.assertEqual(k.shadow.own.active(), ["base-6", "base-7", "base-2"])  # the first free base-N
            k._command({"op": "rename", "variant": "band90", "name": "high favs"})   # a built-in one's display name too
            k._command({"op": "variant_new", "from": "band90", "rule": {"cap": 4}})
            self.assertIn("high-favs-2", k.shadow.own.active())

    def test_the_screen_follows_a_rename(self):
        from tern import dash
        s = dash.Screen.__new__(dash.Screen)
        s.raw = {"variants": [{"name": "base-6"}, {"name": "base"}], "renamed": {"y": "base-6"}}
        s.variant = s.var_sel = s.edit_var = "y"
        s.var_new, s.save_prefs, s.say = None, lambda: None, lambda m: None
        s.apply_selected()
        self.assertEqual((s.variant, s.var_sel, s.edit_var), ("base-6", "base-6", "base-6"))

    def test_only_so_many(self):
        from tern import variants
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for _ in range(variants.MAX_OWN):
                k._command({"op": "variant_new", "from": "base", "rule": {"cap": 6}})
            self.assertEqual(len(k.shadow.own.active()), variants.MAX_OWN)
            self.assertIn("at most", k._command({"op": "variant_new", "from": "base", "rule": {"cap": 6}}))
            self.assertIn("at most", self.new(k, band_lo=0.8))

    def test_the_gate_judges_the_new_variants_own_record(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.cfg.gate.min_days, k.cfg.gate.min_entries, k.cfg.gate.min_pnl = 0, 0, -1000
            k.shadow._count({"v": "base", "event": "E-1", "pnl": 5.0, "n": 5, "fee": 0.1, "close": 1})
            self.new(k, band_lo=0.80)
            for v, want in (("base", True), ("base-2", False)):                # nothing settled yet
                k.cfg.live.variant = v
                self.assertEqual((k.gate()["variant"], k.gate()["passed"]), (v, want))

    def test_unreadable_variants_file_never_lets_live_trade_base(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, band_lo=0.80); k.cfg.live.variant = "base-2"; __import__("tern.config").config.save(k.cfg, d)
            k._shadow(); k.shadow.save()
            self.assertIn("base-2", {r["v"] for r in k.shadow.rows})
            with open(os.path.join(d, "variants.json"), "w") as fh:
                fh.write("{bad")
            k = self.desk(d)
            self.assertFalse(k.live_target()["ok"])
            self.assertFalse(k.gate()["passed"])
            self.assertIn("base-2", {r["v"] for r in k.shadow.rows})           # its open entries are not lost
            k._command({"op": "variant_new", "name": "n", "from": "base", "rule": {"cap": 6}})
            self.assertTrue([f for f in os.listdir(d) if f.startswith("variants.json.damaged")])

    def test_one_bad_variant_is_skipped_not_run(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, band_lo=0.80)
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

    def test_report_lists_own_variants(self):
        import contextlib, io
        from unittest import mock
        from tern import __main__ as cli, config as cfgmod
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "RUNTIME", d):
            k = self.desk(d)
            self.new(k, band_lo=0.80)
            k._shadow(); k.shadow.settle({self.T: True})
            def run(*argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["report", *argv]), 0)
                return out.getvalue()
            self.assertIn("base-2: 80-95¢", run())
            self.assertIn("base-2: 80-95¢", run("--periods"))
            self.assertNotIn("← live", run())
            k._command({"op": "live", "on": True, "variant": "base-2"}); k._command({"op": "variant_on", "variant": "late", "on": False})
            self.assertRegex(run(), r"base-2: .*← live"); self.assertRegex(run(), r"late: .* · off")
            k._command({"op": "live", "on": False})
            k._command({"op": "variant_delete", "variant": "base-2"})
            self.assertIn("deleted, kept for the record", run())


class ScreenVariants(Rig, unittest.TestCase):
    """e, D and r, and the draft e opens, on a real engine's status."""

    def test_a_draft_is_saved_once_as_a_new_variant(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.assertEqual([r.group for r in scr.set_rows() if r.group], ["RULE", "SIZE", "LIMITS", "PAPER"])
            self.assertIn("RULE  band80  built in", self.text())
            self.press(self.curses.KEY_RIGHT, self.curses.KEY_RIGHT)           # band low is the first row
            self.row("contracts"); self.press("1", "9", 10)
            self.row("max_hour_cost"); self.press(self.curses.KEY_RIGHT)
            self.row("series"); scr.set_sub = 1; self.press(" ")
            self.assertEqual(self.cmds(), [])                                  # nothing is sent until s
            self.assertEqual(scr.draft, {"band_lo": 0.82, "contracts": 19.0, "max_hour_cost": 65, "series": ["KXBTCD", "KXETHD"]})
            for want in ("80¢ → 82¢ *", "5 → 19 *", "$60 → $65 *", "4 changes, not saved", "save as new"):
                self.assertIn(want, self.text())
            self.press("s")
            for want in ("save as a new variant", "band low  80¢ → 82¢", "coins  BTC → BTC ETH"):
                self.assertIn(want, self.text())
            self.press(*"mine", 10)
            [c] = self.run_cmds()
            self.assertEqual((c["op"], c["from"], c["name"]), ("variant_new", "band80", "mine"))
            rule = k.shadow.own.rule("mine")
            self.assertEqual([rule[f] for f in ("band_lo", "band_hi", "contracts", "max_hour_cost", "series")],
                             [0.82, 0.90, 19, 65, ["KXBTCD", "KXETHD"]])
            self.assertEqual((k.shadow.own.active(), k.shadow.own.rule("mine")["paper_capital"]), (["mine"], 1000))
            self.assertEqual((scr.overlay, scr.edit_var, scr.draft, scr.var_sel, scr.variant), (None, None, {}, "mine", "base"))
            self.assertIn("+mine", self.text())
            self.press(10)                                                     # enter selects it
            self.assertEqual((self.cmds(), scr.variant), ([], "mine"))

    def test_e_edits(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            scr.variant, scr.var_sel = "late", "band90"; self.sync()
            self.press("e")
            self.assertEqual((scr.overlay, scr.flash), (None, ""))             # only on the variants
            self.press("5", "e")
            self.assertEqual((scr.overlay, scr.edit_var), ("edit", "band90"))
            self.assertIn("RULE  band90", self.text()); self.assertIn("─save as new─", self.text())
            self.assertNotIn("┤save", self.text()); self.assertIn("⁵variants", self.text())
            self.press(self.curses.KEY_RIGHT, "s", *"mine", 10); self.run_cmds()
            self.assertEqual((k.shadow.own.rule("mine")["band_lo"], k.shadow.own.all["mine"]["from"]), (0.91, "band90"))
            scr.var_sel = "mine"; self.press("e")                              # one of your own too
            self.assertEqual((scr.edit_var, scr.set_value("rule.band_lo")), ("mine", 0.91))
            self.press(27); self.press("n", "E")                               # there is only e
            self.assertEqual((scr.overlay, self.cmds()), (None, []))
            self.press("9")                                                    # the settings box is the desk's again
            self.assertIn("days of hours traded", self.text())

    def test_esc_throws_the_draft_away_and_other_keys_do_not(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.press(self.curses.KEY_RIGHT, self.curses.KEY_LEFT)
            self.assertEqual(scr.draft, {})                                    # back where it was: not a change
            self.press(self.curses.KEY_RIGHT, "e", "q", "x", "p")
            self.assertEqual((scr.overlay, scr.draft), ("edit", {"band_lo": 0.81}))
            self.press("s", 27)                                                # out of the name: back in the draft
            self.assertEqual((scr.modal, scr.draft), (None, {"band_lo": 0.81}))
            self.press(27)
            self.assertEqual((scr.overlay, scr.edit_var, scr.draft), (None, None, {}))
            self.assertIn("thrown away", scr.flash)
            self.assertEqual((self.cmds(), k.shadow.own.all), ([], {}))
            self.press("e")
            self.assertIn("80¢ ", self.text()); self.assertEqual(scr.draft, {})

    def test_a_refused_save_keeps_the_draft(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.press(self.curses.KEY_RIGHT, "s", *"late", 10); self.run_cmds()   # a name already taken
            self.assertIn("already a variant's name", scr.flash)
            self.assertEqual((scr.overlay, scr.edit_var, scr.draft, scr.var_new), ("edit", "band80", {"band_lo": 0.81}, None))
            self.press("s", *"later", 10); self.run_cmds()
            self.assertEqual((k.shadow.own.active(), scr.overlay), (["later"], None))

    def test_the_variant_under_the_draft_goes_away(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.new(k, "late", "mine"); self.sync()
            self.open("mine")
            self.press(self.curses.KEY_RIGHT, "s")
            k._command({"op": "variant_delete", "variant": "mine"}); self.sync()
            self.assertEqual((scr.overlay, scr.edit_var, scr.draft, scr.modal), (None, None, {}, None))
            self.assertIn("gone", scr.flash)

    def test_delete_asks_and_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k._command({"op": "variant_new", "name": "mine", "from": "late", "rule": {"cap": 6}})
            self.press("5"); scr.focus = "variants"; self.sync(); self.cmds()
            for name, say in (("base", "built in"), ("late", "built in")):
                scr.var_sel = name
                self.press("D")
                self.assertIsNone(scr.modal); self.assertIn(say, scr.flash)
            scr.var_sel = "mine"
            self.press("D", "x")
            self.assertEqual(self.cmds(), [])
            self.press("D", "y"); self.run_cmds()
            self.assertEqual(k.shadow.own.active(), [])

    def test_help_says_the_rule_in_use_and_the_variant_keys(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k._command({"op": "variant_new", "from": "base", "name": "mine", "rule": {"band_lo": 0.69, "band_hi": 0.83, "cap": 1,
                                                                                      "contracts": 7, "paper_capital": 300}})
            scr.variant = "mine"; self.sync()
            text = " ".join(" ".join(l if isinstance(l, str) else "".join(t for t, _ in l) for _, ls in scr.help_sections(120) for l in ls).split())
            for want in ("With mine, the variant selected", "mid is 69-83c", "1 a strike an hour", "the base rule (75-95c",
                         "7 contracts a strike", "mine starts with $300", "edit the highlighted one's settings", "marked +", "tern report --periods"):
                self.assertIn(want, text)

    def test_off_can_be_stepped_and_typed(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open("base")
            self.row("cap")
            self.press(self.curses.KEY_RIGHT); self.assertEqual(scr.draft, {"cap": 1})
            self.assertIn("off → 1 *", self.text())
            self.press(self.curses.KEY_LEFT); self.assertEqual(scr.draft, {})
            self.row("max_vol")
            self.press("7", ".", "6", 10); self.assertEqual(scr.draft, {"max_vol": 7.6})
            self.row("band_lo")
            self.press("9", "7", 10)                                           # over band high: refused on the screen
            self.assertIn("below 95", scr.flash)
            self.row("contracts")
            for typed in ("0", "101"):
                self.press(*typed, 10)
                self.assertIn("must be 1 to 100", scr.flash)
            self.assertEqual(scr.draft, {"max_vol": 7.6})
            self.press("s", 10); self.run_cmds()
            self.assertEqual(k.shadow.own.rule("base-2")["max_vol"], 7.6)


class VariantReviewFixes(Rig, unittest.TestCase):
    """Findings from the 2026-11-23 code review of the own-variants work."""

    ROW = {"v": "base", "ts": 1.0, "event": "KXBTCD-X", "close": 2.0, "ticker": "x", "side": "yes", "n": 5,
           "cost": 0.9, "fee": 0.02, "slot": 25, "pnl": 1.0, "sv": 9}

    # 2
    def test_live_never_trades_a_fallback_variant(self):
        with tempfile.TemporaryDirectory() as d:
            for how in ("bad", "list", "gone"):
                k = self.desk(d)
                self.new(k, band_lo=0.80)
                k._command({"op": "live", "on": True, "variant": k.shadow.own.active()[-1]})
                path = os.path.join(d, "variants.json")
                if how == "gone":
                    os.remove(path)
                else:
                    with open(path, "w") as fh:
                        fh.write("{bad" if how == "bad" else "[]")
                k = self.desk(d)
                self.assertFalse(k.live_target()["ok"], how)
                self.new(k, "band80", "n" + how)
                self.assertFalse(k.live_target()["ok"], how)                   # an unrelated change does not lift it
                self.assertFalse(self.desk(d).live_target()["ok"], how)        # nor does a restart after it
                self.assertFalse(k.gate()["passed"])
                self.assertEqual(k.cfg.live.enabled, how == "gone")            # which are off could not be read: not armed
                self.assertRegex(k._command({"op": "live", "on": True, "variant": "base"}), "live (moved|armed)")   # picking one does
                self.assertTrue(k.live_target()["ok"], how)

    # 3
    def test_a_lost_variants_key_and_record_are_never_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._command({"op": "variant_new", "from": "base", "rule": {"band_lo": 0.78}})
            k._shadow(); k.shadow.save()
            self.assertTrue(k.shadow.bought("base-2"))
            with open(os.path.join(d, "variants.json"), "w") as fh:
                fh.write("{bad")
            k = self.desk(d)
            for name in ("", "base-2"):
                k._command({"op": "variant_new", "name": name, "from": "base", "rule": {"cap": 6}})
            self.assertNotIn("base-2", k.shadow.own.all)
            self.assertFalse([v for v in k.shadow.own.active() if k.shadow.bought(v)])
            k.shadow.save()
            k = self.desk(d)                                                   # the damaged flag is gone by now
            self.assertIn("base-2", {r["v"] for r in k.shadow.rows})           # its open entries still settle
            k.shadow.settle({self.T: True})
            self.assertEqual(k.shadow.summary(["base-2"])[0]["entries"], 1)

    # 6
    def test_a_rule_that_would_not_load_again_is_never_saved(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.cfg.strategy.minutes = [25, 20]
            self.assertIn("no new variant", k._command({"op": "variant_new", "name": "x", "from": "late"}))
            self.assertIn("no new variant", k._command({"op": "variant_new", "from": "late", "rule": {"band_lo": 0.8}}))
            self.assertEqual(k.shadow.own.all, {})
            k._command({"op": "variant_new", "from": "late", "rule": {"minutes": [10]}})   # this gives it one
            self.assertEqual(self.desk(d).shadow.own.active(), ["late-2"])

    # 7, 14
    def test_variants_json_by_hand_is_checked(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._command({"op": "variant_new", "from": "base", "rule": {"band_lo": 0.80}})
            path = os.path.join(d, "variants.json")
            with open(path) as fh:
                good = json.load(fh)["variants"]["base-2"]
            def bad(**rule):
                return {**good, "rule": {**good["rule"], **rule}}
            items = {"base-2": good, "a1": bad(band_lo="0.80"), "a2": bad(band_lo=float("nan")), "a3": bad(band_lo=True),
                     "a4": bad(max_vol="7"), "a5": bad(minutes=[float("inf")]), "a6": bad(cap=0.5), "a7": {**good, "deleted": "no"},
                     "a8": bad(band_lo=0.97), "a9": 5, "Bad Key": good,
                     "b1": bad(contracts="5"), "b2": bad(contracts=-1), "b3": bad(max_hour_cost=None), "b4": bad(paper_capital=-1)}
            with open(path, "w") as fh:
                json.dump({"variants": items}, fh)
            k = self.desk(d)
            self.assertEqual(list(k.shadow.own.all), ["base-2"])
            self.assertTrue(k.shadow.own.damaged)
            k._shadow(); k.seeded.add(self.T); k._status()
            with open(path, "w") as fh:
                json.dump({"variants": {"base-2": {**good, "title": 5, "from": None, "made": "x"}}}, fh)
            self.assertEqual(self.desk(d).shadow.own.title("base-2"), "base-2")
            for text in ("[]", "5", '{"variants": []}', "{}"):
                with open(path, "w") as fh:
                    fh.write(text)
                k = self.desk(d)
                self.assertTrue(k.shadow.own.damaged, text)
                for f in [f for f in os.listdir(d) if ".damaged" in f]:
                    os.remove(os.path.join(d, f))
                k._command({"op": "variant_new", "name": "y", "from": "band80", "rule": {"cap": 6}})
                self.assertTrue([f for f in os.listdir(d) if ".damaged" in f], text)   # kept, not written over

    # 9
    def test_toggling_a_slot_keeps_the_ones_the_box_does_not_list(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.strategy.minutes = [25, 20, 12]; self.sync()
            self.press("5"); scr.focus, scr.var_sel = "variants", "base"; self.cmds()
            self.press("e")
            row = next(r for r in scr.set_rows() if r.key == "rule.minutes")
            scr.set_sel = scr.set_rows().index(row)
            self.assertIn(12, scr.set_choices(row))
            scr.set_sub = scr.set_choices(row).index(10)
            self.press(" ", "s", 10); self.run_cmds()
            self.assertEqual(k.shadow.own.rule("base-2")["minutes"], [25, 20, 12, 10])
            self.assertIn("no new variant", k._command({"op": "variant_new", "from": "base", "rule": {"minutes": [25, 13]}}))

    # 12
    # 13
    def test_report_columns_line_up(self):
        import contextlib, io
        from unittest import mock
        from tern import __main__ as cli, config as cfgmod
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "RUNTIME", d):
            k = self.desk(d)
            k._command({"op": "variant_new", "name": "twelve-chars", "from": "base", "rule": {"cap": 6}})
            k._command({"op": "variant_new", "name": "x1", "from": "base", "rule": {"cap": 6}})
            k._shadow(); k.shadow.settle({self.T: True})
            k._command({"op": "variant_delete", "variant": "x1"})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["report"])
            lines = out.getvalue().splitlines()
            at = lines[0].index("hours") + len("hours")
            row = next(l for l in lines if l.startswith("x1 ") and ":" not in l)
            self.assertEqual(row[at - 1], "1"); self.assertEqual(row[at], " ")



class VariantSettingsReview(Rig, unittest.TestCase):
    """Findings from the code review of the draft-and-save work (2026-11-23)."""

    def old_file(self, d, *keys, **config):
        """variants.json from before sizes, in d, and config.json with the given strategy values."""
        import json
        from tern import config as cfgmod
        cfg = cfgmod.Config()
        for f, v in config.items():
            setattr(cfg if f == "paper_capital" else cfg.strategy, f, v)
        cfgmod.save(cfg, d)
        rule = {"band_lo": 0.8, "band_hi": 0.9, "minutes": [25], "max_spread": 0.02, "series": ["KXBTCD"], "cap": None, "max_vol": None}
        v = {k: {"rule": rule, "title": k, "from": "base", "made": 1, "deleted": False} for k in keys}
        with open(os.path.join(d, "variants.json"), "w") as fh:
            json.dump({"variants": v}, fh)

    def saved(self, d):
        import json
        with open(os.path.join(d, "variants.json")) as fh:
            return {k: v["rule"].get("contracts") for k, v in json.load(fh)["variants"].items()}

    # 1
    def test_a_number_typed_on_a_row_that_has_none_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            for key in ("minutes", "series"):
                self.row(key); self.press("2", "0", 10)                        # (enter turns the choice on or off)
                self.assertIsNone(scr.set_buf)
                self.assertTrue(all(isinstance(v, list) for v in scr.draft.values()), scr.draft)
            self.press(27); self.press("9")
            scr.set_sel = [r.key for r in scr.set_rows()].index("alerts")
            self.press("1")
            self.assertEqual((scr.set_buf, scr.overlay, self.cmds()), (None, None, []))

    # 2, 9
    def test_a_save_still_on_its_way_never_closes_another_draft(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.press(self.curses.KEY_RIGHT, "s", *"mine", 10)                # sent, not answered yet
            [c] = self.cmds()
            self.press(27)
            self.assertNotIn("nothing was saved", scr.flash)
            self.assertIn("being saved", scr.flash)
            scr.var_sel = "band90"; self.press("e", self.curses.KEY_RIGHT)
            k.results[c["id"]] = k._command(c); self.sync()
            self.assertEqual((scr.overlay, scr.edit_var, scr.draft), ("edit", "band90", {"band_lo": 0.91}))
            self.assertEqual(k.shadow.own.active(), ["mine"])

    def test_changes_made_after_the_save_stay_open(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.press(self.curses.KEY_RIGHT, "s", *"mine", 10)
            [c] = self.cmds()
            self.press(self.curses.KEY_RIGHT)
            k.results[c["id"]] = k._command(c); self.sync()
            self.assertEqual((scr.edit_var, scr.draft), ("band80", {"band_lo": 0.82}))

    def test_a_slow_answer_still_closes_the_box_it_saved(self):
        from unittest import mock
        import time
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.press(self.curses.KEY_RIGHT, "s", 10)
            [c] = self.cmds()
            real = time.time
            with mock.patch.object(time, "time", lambda: real() + 12):
                self.sync()
                self.press("s")
                self.assertIsNone(scr.modal)                                   # no second save while one is on its way
                k.results[c["id"]] = k._command(c); self.sync()
            self.assertEqual((scr.overlay, scr.draft, k.shadow.own.active()), (None, {}, ["band80-2"]))

    def test_no_answer_at_all_lets_it_be_saved_again(self):
        from unittest import mock
        import time
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.press(self.curses.KEY_RIGHT, "s", 10); self.cmds()
            real = time.time
            with mock.patch.object(time, "time", lambda: real() + 20):
                self.sync()
                self.assertIn("no answer", scr.flash)
                self.press("s")
                self.assertEqual((scr.modal["type"], scr.draft), ("vname", {"band_lo": 0.81}))

    def test_the_gate_column_says_the_pnl_it_needs(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5")
            for need, say in ((100, "P&L<$100"), (-50, "✓P&L"), (0, "P&L<$0")):
                k.cfg.gate.min_pnl = need
                k.shadow.agg = {}; k.shadow.recent = {}
                k.shadow._count({"v": "base", "event": "E-1", "pnl": 73.78 if need else -1.0, "n": 5, "fee": 0.1, "close": 1,
                                 "ticker": "E-1-T1", "side": "yes", "cost": 0.8, "ts": 1.0, "slot": 25})
                self.sync()
                self.assertIn(say, next(l for l in self.text().splitlines() if " base " in l and "d " in l))

    # 3
    def test_nothing_is_saved_through_an_engine_older_than_the_screen(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.open()
            self.press(self.curses.KEY_RIGHT)
            for v in scr.raw["variants"]:
                v.pop("on")
            scr.key(ord("s"))
            self.assertIsNone(scr.modal); self.assertIn("older than this screen", scr.flash)
            scr.key(27)
            for key in "eLD ":
                scr.flash = ""; scr.key(ord(key))
                self.assertEqual((scr.overlay, scr.modal), (None, None), key); self.assertIn("older than this screen", scr.flash, key)
            self.assertEqual(self.cmds(), [])

    # 4
    def test_a_slot_off_and_on_again_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.strategy.minutes = [10, 15, 20, 25]; self.sync()
            self.open("base")
            self.row("minutes"); scr.set_sub = scr.set_choices(scr.set_rows()[scr.set_sel]).index(10)
            self.press(" "); self.assertEqual(scr.draft, {"minutes": [25, 20, 15]})
            self.press(" "); self.assertEqual(scr.draft, {})

    # 5
    def test_an_unreadable_config_never_settles_a_variants_size(self):
        with tempfile.TemporaryDirectory() as d:
            self.old_file(d, "mine", contracts=20.0)
            path = os.path.join(d, "config.json")
            with open(path) as fh:
                good = fh.read()
            with open(path, "w") as fh:
                fh.write(good[:40])
            k = self.desk(d)
            self.assertEqual(self.saved(d), {"mine": None})                    # nothing written from a guess
            k._command({"op": "variant_new", "name": "n2", "from": "base", "rule": {"cap": 1}})
            self.assertEqual(self.saved(d), {"mine": None, "n2": 5.0})         # nor by another change
            with open(path, "w") as fh:
                fh.write(good)
            k._reload_cfg(force=True)                                          # readable again
            self.assertEqual((k.shadow.own.rule("mine")["contracts"], self.saved(d)["mine"]), (20.0, 20.0))

    # 6
    def test_a_size_of_nothing_in_config_still_loads_and_buys_nothing(self):
        for bad in (0.0, 0.5, -3.0):
            with tempfile.TemporaryDirectory() as d:
                self.old_file(d, "mine", contracts=bad)
                k = self.desk(d)
                self.assertEqual((k.shadow.own.active(), k.shadow.own.damaged), (["mine"], False), bad)
                k._shadow()
                self.assertEqual(k.shadow.rows, [])
        with tempfile.TemporaryDirectory() as d:
            self.old_file(d, "mine", max_hour_cost=float("inf"))
            self.assertEqual(self.desk(d).shadow.own.active(), ["mine"])

    # 8
    def test_one_bad_entry_does_not_keep_the_others_from_their_own_size(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            self.old_file(d, "mine", contracts=5.0)
            path = os.path.join(d, "variants.json")
            with open(path) as fh:
                j = json.load(fh)
            j["variants"]["bad1"] = 5
            with open(path, "w") as fh:
                json.dump(j, fh)
            k = self.desk(d)
            self.assertTrue(any("unreadable in it" in m for _, _, m in k.logs))
            kept = [f for f in os.listdir(d) if f.startswith("variants.json.damaged")]
            self.assertEqual(len(kept), 1)                                     # the file as it was is kept
            k.cfg.strategy.contracts = 20.0
            from tern import config as cfgmod
            cfgmod.save(k.cfg, d)
            self.assertEqual(self.desk(d).shadow.own.rule("mine")["contracts"], 5.0)


class CashLimit(Rig, unittest.TestCase):
    """No variant buys more than the cash it has left."""

    def test_afford(self):
        from tern.book import afford, taker_fee
        for cash, unit, want in ((100, 0.81, 100), (4.10, 0.81, 4), (4.05, 0.81, 4), (0.80, 0.81, 0), (0, 0.5, 0), (-9, 0.5, 0), (1e9, 0.5, 100)):
            n = afford(100, unit, unit, cash, 0.07)
            self.assertLessEqual(n, want)
            self.assertTrue(n == 0 or n * unit + taker_fee(n, unit, 0.07) <= cash + 1e-9, (cash, n))
            self.assertTrue(n == 100 or (n + 1) * unit + taker_fee(n + 1, unit, 0.07) > cash + 1e-9, (cash, n))   # and no fewer

    def ms(self, n, ask="0.81"):
        m = {"floor_strike": 100.0, "yes_bid_dollars": f"{float(ask) - 0.01:.2f}", "yes_ask_dollars": ask, "yes_bid_size_fp": "500",
             "yes_ask_size_fp": "500", "status": "active"}
        return [{**m, "ticker": f"{{}}-T{i}", "floor_strike": 100.0 + i} for i in range(n)]

    def step(self, sh, cfg, ev, n, slot=25, now=1000.0):
        ms = [{**m, "ticker": m["ticker"].format(ev)} for m in self.ms(n)]
        sh.step(ev, now, ms, cfg.strategy, now - slot * 60 + 1, paper=cfg.paper_capital)

    def spent(self, sh, v):
        return sum(r["n"] * r["cost"] + r["fee"] for r in sh.rows if r["v"] == v)

    def test_every_coin_and_slot_shares_one_cash(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, contracts=19, max_hour_cost=100, paper_capital=100, series=["KXBTCD", "KXETHD", "KXSOLD"])
            for c in ("KXBTCD", "KXETHD", "KXSOLD"):
                self.step(k.shadow, k.cfg, c + "-H1", 4)
                self.step(k.shadow, k.cfg, c + "-H1", 4, slot=20)
            self.assertLessEqual(self.spent(k.shadow, "base-2"), 100)
            self.assertGreater(self.spent(k.shadow, "base-2"), 98.5)            # it uses what it has: fewer contracts, not none
            self.assertAlmostEqual(k.shadow.cash("base-2", 100), 100 - self.spent(k.shadow, "base-2"))

    def test_wins_and_losses_change_what_it_can_spend(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, contracts=100, max_hour_cost=500, paper_capital=100, max_risk=10000, max_order=1000)
            for pnl, want in ((-60.0, 40), (-50.0, 0), (1000.0, 500)):          # 40 left, then under water, then plenty (the hour's limit)
                k.shadow._count({"v": "base-2", "event": f"E{pnl}", "pnl": pnl, "n": 1, "fee": 0, "close": 1.0})
                ev = f"KXBTCD-{pnl}"
                self.step(k.shadow, k.cfg, ev, 9)
                got = sum(r["n"] * r["cost"] + r["fee"] for r in k.shadow.rows if r["v"] == "base-2" and r["event"] == ev)
                if want < 500:
                    self.assertLessEqual(got, want)
                self.assertGreaterEqual(got, want - 1.5)
                k.shadow.rows = [r for r in k.shadow.rows if r["event"] != ev]

    def test_settling_gives_the_money_back(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, contracts=100, max_hour_cost=500, paper_capital=100, max_risk=10000, max_order=1000)
            self.step(k.shadow, k.cfg, "KXBTCD-H1", 2)
            self.assertLess(k.shadow.cash("base-2", 100), 1.5)
            k.shadow.settle({r["ticker"]: True for r in k.shadow.rows})
            self.assertGreater(k.shadow.cash("base-2", 100), 100)

    def test_built_in_variants_use_the_configs_paper_start(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.cfg.strategy.contracts, k.cfg.strategy.max_hour_cost, k.cfg.paper_capital = 100.0, 500.0, 100.0
            k._shadow()                                                        # through the engine: 100 at 81c is $81, then the quote's 50
            self.step(k.shadow, k.cfg, "KXETHD-H1", 9)
            for v in k.shadow.order():
                self.assertLessEqual(self.spent(k.shadow, v), 100, v)
            self.assertGreater(self.spent(k.shadow, "multi"), 98)

    def test_live_is_not_held_to_a_variants_paper_cash(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, paper_capital=100)
            k.shadow._count({"v": "base-2", "event": "E1", "pnl": -100.0, "n": 1, "fee": 0, "close": 1.0})
            sent = []
            k._exec = lambda book, t, delta, *a: sent.append((book.name, delta))
            k._auto_book(k.live, k.cfg.strategy, "base-2", __import__("time").time(), k.shadow.own)
            self.assertEqual(sent, [("live", k.cfg.strategy.contracts)])           # (live's own caps are checked in _exec)

    def test_the_variants_box_says_when_one_is_out_of_cash(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.new(k, paper_capital=100)
            k.shadow._count({"v": "base-2", "event": "E1", "pnl": -99.9, "n": 1, "fee": 0, "close": 1.0, "ticker": "E1-T1",
                             "side": "yes", "cost": 0.8, "ts": 1.0, "slot": 25})
            self.press("5")
            scr.var_sel = "base-2"; self.sync()
            self.assertIn("base-2 has $0.10 left: it buys nothing", self.text())
            scr.var_sel = "band90"; self.sync()
            self.assertNotIn("buys nothing", self.text())

    def test_a_note_in_the_settings_log_heads_its_period(self):
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.slog.append("note", None, "cash limit on", bump=True)
            self.assertRegex("\n".join(reports.periods(d)), r"settings 2, .*: cash limit on")


class OneList(Rig, unittest.TestCase):
    """One list of variants: enter selects, L goes live with the selected one, space turns one on or off, R starts over."""

    def test_the_selected_one_is_what_every_box_shows(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.assertEqual(scr.variant, "base")                              # nothing picked yet: the first
            self.new(k, "base", "wide", band_lo=0.55, band_hi=0.65, paper_capital=300, contracts=3)
            k._shadow()
            self.press("5"); scr.var_sel = "wide"; self.press(10)
            self.assertEqual([x["t"] for x in scr.st["paper"]["pos"]], [self.T])
            self.assertEqual((scr.st["paper"]["start"], scr.st["gate"]["open"], scr.strat()["band_lo"]), (300, 1, 0.55))
            self.assertIn(" wide ", "".join(scr.s.ch[39]))                     # the status line
            self.assertIn("● +wide", self.text())
            self.press("4")
            self.assertIn("wide on paper", self.text())
            self.press("7")
            self.assertIn("PAPER CHECKS   wide needs all of them", self.text())
            import json
            with open(os.path.join(d, "dash.json")) as fh:
                self.assertEqual(json.load(fh)["variant"], "wide")             # the screen remembers it
            self.assertEqual((self.cmds(), k.cfg.live.variant), ([], ""))      # the engine never heard of it
            k._command({"op": "variant_delete", "variant": "wide"}); self.sync()
            self.assertEqual(scr.variant, "base"); self.assertIn("gone", scr.flash)

    def test_L_goes_live_with_the_selected_one(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            scr.variant = "band90"; self.sync()
            self.press("7", "L")
            for want in ("ARM LIVE", "It would trade band90", "has not passed its gate", "type live"):
                self.assertIn(want, self.text())
            self.press(*"yes", 10)
            self.assertEqual(self.cmds(), [])
            self.press("L", *"live", 10)
            [c] = self.run_cmds()
            self.assertEqual((c["op"], c["on"], c["variant"]), ("live", True, "band90"))
            self.assertEqual((k.cfg.live.enabled, k.cfg.live.variant), (True, "band90"))

    def test_selecting_another_never_moves_live(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.live.enabled, k.cfg.live.variant = True, "band90"; k.mode = "live"; self.sync()
            self.press("5"); scr.var_sel = "late"; self.press(10)
            self.assertIn("live still trades band90", scr.flash)
            self.assertEqual((self.cmds(), k.cfg.live.variant, scr.book_view), ([], "band90", "paper"))
            self.assertIn(" L band90", self.text())
            self.assertIn("live: band90" if scr.st["auto_live"] else "late", "".join(scr.s.ch[39]))
            self.press("L")
            for want in ("MOVE LIVE", "Live trades band90. Move it to late?", "off to turn live off"):
                self.assertIn(want, self.text())
            self.press(27)
            self.assertEqual((self.cmds(), scr.flash), ([], "live not moved"))
            self.press("L", *"live", 10)
            [c] = self.run_cmds()
            self.assertEqual((c["variant"], k.cfg.live.variant, k.cfg.live.enabled), ("late", "late", True))
            scr.var_sel = "cap1"; self.press(10, "L", *"off", 10)              # off, from the same question
            [c] = self.run_cmds()
            self.assertEqual((c["on"], k.cfg.live.enabled), (False, False))

    def test_L_with_lives_own_selected_turns_it_off_at_once(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            k.cfg.live.enabled, k.cfg.live.variant = True, "late"; scr.variant = "late"; self.sync()
            self.press("4", "L")
            self.assertIsNone(scr.modal)
            self.assertEqual([(c["op"], c["on"]) for c in self.run_cmds()], [("live", False)])
            self.assertFalse(k.cfg.live.enabled)

    def test_off_takes_no_entries_and_still_settles(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k._shadow()
            self.assertIn("late off", k._command({"op": "variant_on", "variant": "late", "on": False}))
            self.assertIn("already off", k._command({"op": "variant_on", "variant": "late", "on": False}))
            self.assertIn("no variant", k._command({"op": "variant_on", "variant": "gone", "on": False}))
            k = self.desk(d)                                                   # kept across a restart
            rows = {r["v"] for r in k.shadow.rows}
            k.shadow.slots, k.shadow.rows = {}, []
            k._command({"op": "variant_on", "variant": "base", "on": False})
            k._shadow()
            self.assertFalse({"base", "late"} & {r["v"] for r in k.shadow.rows})
            self.assertTrue(k.shadow.rows)
            k._command({"op": "variant_on", "variant": "base", "on": True})
            k.shadow.slots = {}; k.events["KXBTCD"].pop("shadowed"); k._shadow()
            self.assertIn("base", {r["v"] for r in k.shadow.rows})
            st = {v["name"]: v["on"] for v in self.status(k)["variants"]}
            self.assertEqual((st["base"], st["late"]), (True, False))

    def test_a_coin_only_an_off_variant_trades_is_not_fetched(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertIn("KXXRPD", k._wanted())
            k._command({"op": "variant_on", "variant": "multi", "on": False})
            self.assertEqual(k._wanted(), {"KXBTCD"})

    def test_space_on_the_screen(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("5"); scr.var_sel = "late"
            self.press(" ", "y"); [c] = self.run_cmds()
            self.assertEqual((c["op"], c["variant"], c["on"]), ("variant_on", "late", False))
            self.assertIn("late is off: no new entries", self.text())
            k.cfg.live.enabled, k.cfg.live.variant = True, "late"; self.sync()
            self.press(" ")                                                    # on again would let live trade it
            self.assertEqual((self.cmds(), scr.modal["type"]), ([], "livecmd"))
            self.press(*"live", 10); self.run_cmds()
            self.assertNotIn("late", k.shadow.own.off)
            self.press(" "); self.assertEqual(self.cmds(), [])                 # off asks for y
            self.press("y"); self.run_cmds()
            self.assertIn("late", k.shadow.own.off)

    def test_off_by_hand_is_checked(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            self.new(self.desk(d), name="mine")
            path = os.path.join(d, "variants.json")
            with open(path) as fh:
                j = json.load(fh)
            for off, want in ((["late", 5, None], {"late"}), (5, set()), ({}, set()), ("late", set())):
                j["off"], j["restarts"] = off, []                              # (restarts: there was an R, for an hour)
                with open(path, "w") as fh:
                    json.dump(j, fh)
                own = self.desk(d).shadow.own
                self.assertEqual((own.off, own.active(), own.off_lost), (want, ["mine"], not isinstance(off, list)))

    def test_r_renames_and_R_is_gone(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("r")
            self.assertEqual((scr.modal, scr.flash), (None, ""))               # only on the variants
            self.assertNotIn("rule on/off", self.text()); self.assertNotIn("Restart", self.text())
            self.press("5"); self.assertIn("─rename─", self.text())
            scr.var_sel = "late"; self.press("r", 21, *"slow", 10)
            [c] = self.run_cmds()
            self.assertEqual((c["op"], c["variant"], c["name"], k.names.variant("late")), ("rename", "late", "slow", "slow"))
            for key in "RNE":
                self.press(key)
                self.assertEqual((self.cmds(), scr.modal, scr.overlay), ([], None, None), key)
            self.assertIn("unknown command", k._command({"op": "variant_restart", "variant": "base"}))

    def test_e_then_s_is_a_fresh_copy(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.new(k, "base", "wide", band_lo=0.55, band_hi=0.65); k._shadow()
            self.open("wide"); self.press("s")
            self.assertIn("the same settings", self.text())
            self.press(10); [c] = self.run_cmds()
            self.assertEqual((c["from"], c["rule"]), ("wide", {}))
            self.assertEqual(k.shadow.own.rule("wide-2"), k.shadow.own.rule("wide"))
            self.assertEqual([r["open"] for r in k.shadow.summary(["wide", "wide-2"])], [1, 0])


class Merge(Rig, unittest.TestCase):
    """Profiles of before move into the one list, once, with everything they bought."""

    RULE = {"band_lo": 0.6, "band_hi": 0.9, "minutes": [25, 5], "max_spread": 0.05, "series": ["KXBTCD"], "cap": None, "max_vol": None,
            "contracts": 19.0, "max_hour_cost": 100.0, "paper_capital": 100.0}

    def entry(self, v, i, pnl=None, sv=7):
        e = f"KXBTCD-H{i}"
        return {"v": v, "ts": 100.0 + i, "event": e, "close": 200.0 + i, "ticker": f"{e}-T1", "side": "yes", "n": 2, "cost": 0.8, "fee": 0.02,
                "slot": 25, "pnl": pnl, "sv": sv}

    def folder(self, d, pid, name, own, settled, open_=(), created=1.0, enabled=True, variant="base", slots=None, **more):
        import json
        p = os.path.join(d, "profiles", pid); os.makedirs(p)
        def put(n, o):
            with open(os.path.join(p, n), "w") as fh:
                fh.write(o if isinstance(o, str) else json.dumps(o))
        put("profile.json", {"name": name, "created": created, "enabled": enabled, "sv": 7})
        put("config.json", {"paper_capital": 100.0, "strategy": {"variant": variant, "contracts": 19.0, "max_hour_cost": 100.0}})
        put("variants.json", {"variants": {k: {"rule": self.RULE, "title": more.get("titles", {}).get(k, k), "from": "base", "made": 1,
                                               "deleted": x == "deleted"} for k, x in own.items()}})
        put("shadow.json", {"rows": list(open_), "slots": slots or {}})
        put("shadow-settled.jsonl", "".join(json.dumps(r) + "\n" for r in settled) + "cut sho")
        return p

    def two(self, d, **more):
        """Profile 1 with two entries; profile 2 and 3 with their own, deleted and built-in ones."""
        import json
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "shadow-settled.jsonl"), "w") as fh:
            fh.write("".join(json.dumps(self.entry("base", i, 1.0, 1)) + "\n" for i in range(2)))
        with open(os.path.join(d, "shadow.json"), "w") as fh:
            json.dump({"rows": [self.entry("base", 5, None, 1)], "slots": {"KXBTCD-H5": [25]}}, fh)
        self.folder(d, "no2", "profile 2", {"mine": "", "shared": "deleted"},
                    [self.entry("mine", 1, 2.0), self.entry("shared", 1, 3.0), self.entry("base", 1, 4.0)],
                    [self.entry("mine", 5), self.entry("late", 5)], slots={"KXBTCD-H5": [25, 10]}, created=1.0, variant="mine")
        self.folder(d, "p3", "profile 3", {"shared": "", "mine": "deleted"},
                    [self.entry("shared", 2, 5.0), self.entry("mine", 2, 6.0), self.entry("base", 2, 7.0)],
                    [self.entry("shared", 6)], created=2.0, enabled=more.get("on3", True), variant="base")
        return {"base": (2, 2.0, 1), "mine": (1, 2.0, 1), "shared": (1, 5.0, 1), "shared-p2": (1, 3.0, 0), "base-p2": (1, 4.0, 0),
                "late-p2": (0, 0.0, 1), "mine-p3": (1, 6.0, 0), "base-p3": (1, 7.0, 0)}

    def check(self, k, want):
        sh = k.shadow
        got = {r["name"]: (r["entries"], r["pnl"], r["open"]) for r in sh.summary(list(want))}
        self.assertEqual(got, want)
        self.assertEqual(sh.own.active(), ["mine", "shared"])
        self.assertEqual(set(sh.own.deleted()), {"shared-p2", "base-p2", "late-p2", "mine-p3", "base-p3"})
        self.assertFalse(sh.own.damaged)

    def test_everything_moves_and_is_counted_once(self):
        import json
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            want = self.two(d)
            k = self.desk(d)
            self.check(k, want)
            self.assertEqual(k.shadow.order()[-2:], ["mine", "shared"])
            self.assertEqual(sorted(k.shadow.slots["KXBTCD-H5"]), [10, 25])    # a slot either took is taken
            self.assertTrue(os.path.exists(os.path.join(d, "profiles-merged", "no2", "variants.json")))
            self.assertFalse(os.path.exists(os.path.join(d, "profiles")) or os.path.exists(os.path.join(d, "merging")))
            self.assertTrue(any("profile 2: mine moved" in m for _, _, m in k.logs))
            k.shadow.settle({"KXBTCD-H5-T1": True, "KXBTCD-H6-T1": False}); k.shadow.save()
            self.assertEqual(k.shadow.summary(["late-p2"])[0]["entries"], 1)   # open ones settle where they belong
            rule = k.shadow.own.rule("base-p2")                                # its copy of a built-in one: the rule it traded
            self.assertEqual((rule["band_lo"], rule["contracts"], rule["paper_capital"]), (0.75, 19.0, 100.0))
            for _ in range(2):
                k = self.desk(d)                                               # once only
                self.assertEqual(k.shadow.summary(["mine"])[0]["entries"], 2)
            with open(os.path.join(d, "shadow-settled.jsonl")) as fh:
                rows = [json.loads(l) for l in fh]
            mine = next(r for r in rows if r["v"] == "mine")
            self.assertEqual((mine["p"], mine["psv"], mine["sv"]), ("no2", 7, 1))
            self.assertIn("was in profile 2", "\n".join(reports.periods(d)))
            self.assertIn("base-p2", "\n".join(reports.desk(d)))

    def test_cut_short_anywhere_it_starts_again_from_the_files_as_they_were(self):
        from unittest import mock
        from tern import merge, variants
        for where in range(8):
            with tempfile.TemporaryDirectory() as d:
                want = self.two(d)
                n = [0]
                def cut(real):
                    def f(*a, **kw):
                        n[0] += 1
                        if n[0] == where + 1:
                            raise OSError("cut short")
                        return real(*a, **kw)
                    return f
                with mock.patch.object(merge, "_put", cut(merge._put)), mock.patch.object(variants.VariantSet, "save", cut(variants.VariantSet.save)), \
                        mock.patch.object(merge.os, "replace", cut(os.replace)):
                    try:
                        self.desk(d)
                    except OSError:
                        pass
                self.check(self.desk(d), want)
                self.check(self.desk(d), want)

    def test_a_profile_that_was_off_comes_in_off(self):
        with tempfile.TemporaryDirectory() as d:
            self.two(d, on3=False)
            self.assertEqual(self.desk(d).shadow.own.off, {"shared"})

    def test_live_keeps_the_variant_it_would_have_traded(self):
        import json
        from tern import config
        for prof, strat, want in (("1", "late", "late"), ("no2", "late", "mine"), ("p3", "late", "base-p3"), ("gone", "late", "")):
            with tempfile.TemporaryDirectory() as d:
                self.two(d)
                with open(os.path.join(d, "config.json"), "w") as fh:
                    json.dump({"strategy": {"variant": strat}, "live": {"profile": prof, "enabled": True}}, fh)
                k = self.desk(d)
                self.assertEqual(k.cfg.live.variant, want)
                self.assertEqual(k.live_target()["ok"], prof in ("1", "no2"))  # a copy that stopped: live waits for a pick
                k._command({"op": "set", "key": "alerts", "value": False})     # a save keeps it
                self.assertEqual(config.load(d).live.variant, want)

    def test_a_name_profile_1_already_has_is_not_taken(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            self.two(d)
            with open(os.path.join(d, "variants.json"), "w") as fh:
                json.dump({"variants": {"mine": {"rule": self.RULE, "title": "mine", "from": "base", "made": 1, "deleted": False}}}, fh)
            own = self.desk(d).shadow.own
            self.assertEqual(own.active(), ["mine", "mine-p2", "shared"])
            self.assertEqual(len({own.title(k).lower() for k in own.active()}), 3)

    def test_a_folder_that_cannot_be_read_is_kept_and_said(self):
        with tempfile.TemporaryDirectory() as d:
            self.two(d)
            with open(os.path.join(d, "profiles", "no2", "config.json"), "w") as fh:
                fh.write("{bad")
            k = self.desk(d)
            self.assertEqual(k.shadow.own.active(), ["shared"])
            self.assertTrue(any("no2 could not be read" in m for _, _, m in k.logs))
            self.assertTrue(os.path.exists(os.path.join(d, "profiles-merged", "no2", "shadow-settled.jsonl")))

    def test_not_while_another_engine_runs(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            self.two(d)
            with open(os.path.join(d, "engine.lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaises(SystemExit):                            # it does not go on without them
                    self.desk(d)
                self.assertTrue(os.path.isdir(os.path.join(d, "profiles", "no2")))
                import threading
                threading.Timer(0.3, lambda: fcntl.flock(lock, fcntl.LOCK_UN)).start()   # a screen looking if it is up
                self.desk(d)
            self.assertFalse(os.path.isdir(os.path.join(d, "profiles")))

    def test_what_stops_it_leaves_everything_and_the_engine_starts(self):
        from unittest import mock
        from tern import merge
        def snap(d):
            out = {}
            for n in ("variants.json", "shadow.json", "shadow-settled.jsonl"):
                p = os.path.join(d, n)
                out[n] = open(p).read() if os.path.exists(p) else None
            return out
        for bad in ("{bad", "[]", "disk"):
            with tempfile.TemporaryDirectory() as d:
                self.two(d)
                before = snap(d)
                path = os.path.join(d, "config.json")
                if bad == "disk":
                    put = merge._put
                    def full(p, text):
                        if p.endswith("shadow.json"):
                            raise OSError("No space left on device")
                        put(p, text)
                    with mock.patch.object(merge, "_put", full):
                        k = self.desk(d)
                else:
                    with open(path, "w") as fh:
                        fh.write(bad)
                    k = self.desk(d)
                self.assertTrue(any("the next start tries again" in m for _, _, m in k.logs), bad)
                self.assertEqual(snap(d), before, bad)                         # as they were
                self.assertFalse(os.path.exists(os.path.join(d, "merging")), bad)
                self.assertTrue(os.path.isdir(os.path.join(d, "profiles", "no2")), bad)
                if bad != "disk":
                    os.remove(path)
                self.assertIn("shared", self.desk(d).shadow.own.active(), bad)
                self.assertFalse(os.path.isdir(os.path.join(d, "profiles")), bad)

    def test_two_lost_variants_of_one_name_stay_apart(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(d, exist_ok=True)
            self.folder(d, "a1", "A", {}, [self.entry("wide", 1, 1.0)], created=1.0)      # its variants.json lost it
            self.folder(d, "b2", "B", {"wide": "deleted"}, [self.entry("wide", 2, 5.0)], created=2.0)
            k = self.desk(d)
            rows = {r["name"]: r["pnl"] for r in k.shadow.summary(list(k.shadow.agg)) if r["entries"]}
            self.assertEqual(sorted(rows.values()), [1.0, 5.0], rows)

    def test_reading_a_report_moves_nothing(self):
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            self.two(d)
            reports.desk(d); reports.periods(d)
            self.assertTrue(os.path.isdir(os.path.join(d, "profiles", "no2")))


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
              "variants": [{"name": "base", "on": True, "open": 0, "hours": 1, "entries": 1, "pnl": 1.0, "curve": []},
                           {"name": "band90", "on": True, "open": 0, "hours": 1, "entries": 1, "pnl": 2.0, "curve": []}],
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
            hub.handle({"sid": sid, "w": 120, "h": 40, "key": "5"})
            for k in ("q", "P", "L", "r", "D", " ", "e", "ArrowDown", "Enter", "L"):
                f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": k})
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])          # no command reached the engine
            self.assertFalse(os.path.exists(os.path.join(d, "dash.json")))     # the screen's prefs are its own
            self.assertIn("view only", "".join(t for t, _ in f["rows"][38]))
            for k in ("0", "ArrowDown", "1", "c", "Tab", "BackTab", "Escape", "C"):
                f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": k})
            self.assertEqual(f["prefs"]["variant"], "band90")                  # ↓ then enter: selected, for this viewer
            self.assertEqual(f["prefs"]["theme"], "tokyo night")                # the themes box, for this viewer too
            self.assertEqual((f["prefs"]["chart"], f["prefs"]["tab"], f["prefs"]["series"]), ("1h", "spot", "KXETHD"))
            self.assertFalse({"hidden", "zoom", "page"} & set(f["prefs"]))
            row = "".join(t for t, _ in f["rows"][0])
            f = hub.handle({"sid": sid, "w": 120, "h": 40, "click": {"y": 0, "x": row.index("ladder")}})
            self.assertEqual(hub.sessions[sid][0].tab, "ladder")                # clicking a tab shows that box
            self.assertEqual(len(f["rows"]), 40)

    def test_web_tabs_are_each_viewers_own(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            self.status(d)
            hub = Hub(d)
            f = hub.handle({"w": 111, "h": 33, "prefs": {"page": 2, "hidden": {"page2": ["log"]}, "zoom": "log", "tab": "book"}})
            sid = f["sid"]
            scr = hub.sessions[sid][0]
            self.assertEqual((scr.tab, f["prefs"]["tab"]), ("book", "book"))   # a reload keeps it
            self.assertFalse({"page", "hidden", "zoom", "slot3"} & set(f["prefs"]))
            go = lambda **req: ["".join(t for t, _ in r) for r in hub.handle({"sid": sid, "w": 111, "h": 33, **req})["rows"]]
            for name in ("spot", "contract", "ladder", "book", "variants", "log", "gate", "reports", "settings", "themes", "help"):
                rows = go()
                go(click={"y": 0, "x": rows[0].index(name)})
                self.assertEqual(scr.tab, name)
                go(key="1"); self.assertEqual(scr.tab, "spot")
            go(key="5")
            self.assertIn("view only", go(key="e")[31])
            self.assertIsNone(scr.overlay)
            go(key="6"); scr.flash = ""; rows = go()
            for hint in ("Pause", "start", "quit"):                            # the keys that change something are not drawn
                self.assertNotIn(hint, rows[31])
            go(key="1"); rows = go()
            go(click={"y": 31, "x": rows[31].index("chart")})                  # the rest are clicked as they are pressed
            self.assertEqual(scr.chart, "1h")
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])
            self.assertFalse(os.path.exists(os.path.join(d, "dash.json")))

    def test_web_select_hint_click_selects_the_highlighted_one(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            self.status(d)
            hub = Hub(d)
            f = hub.handle({"w": 111, "h": 33})
            sid = f["sid"]
            go = lambda **req: hub.handle({"sid": sid, "w": 111, "h": 33, **req})
            go(key="5")
            self.assertEqual(f["prefs"]["variant"], "base")
            f = go(key="ArrowDown")
            rows = ["".join(t for t, _ in r) for r in f["rows"]]
            y = 31
            f = go(click={"y": y, "x": rows[y].index("⏎")})
            self.assertEqual(f["prefs"]["variant"], "band90")
            for hint in ("edit", "on/off", "Delete"):                   # the keys that change something are not drawn
                self.assertNotIn(hint, rows[y])
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])
            f = hub.handle({"w": 111, "h": 33, "prefs": {"page": 2, "variant": "band90"}})   # a reload keeps it
            self.assertEqual(f["prefs"]["variant"], "band90")
            f = hub.handle({"w": 111, "h": 33, "prefs": {"page": 2, "variant": "gone"}})
            self.assertEqual(f["prefs"]["variant"], "base")

    def test_settings_box_opens_but_changes_nothing(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            self.status(d)
            hub = Hub(d)
            sid = hub.handle({"w": 120, "h": 40})["sid"]
            for k in ("9", "ArrowDown", "ArrowRight", "ArrowLeft", "Enter", "5", "Enter", " "):
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
            for k in ("9", "ArrowUp", "ArrowRight"):                           # the last row: log shows
                f = hub.handle({"sid": sid, "w": 120, "h": 40, "key": k})
            self.assertEqual(f["prefs"]["log_filter"], "no_shadow")
            self.assertIn("Saved at once", "\n".join("".join(t for t, _ in r) for r in f["rows"]))
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])
            self.assertFalse(os.path.exists(os.path.join(d, "dash.json")))
            f = hub.handle({"w": 120, "h": 40, "prefs": {"log_filter": "trades"}})   # a reload brings it back
            self.assertEqual(f["prefs"]["log_filter"], "trades")

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
            self.assertEqual(k.web, {"series": ["KXETHD"], "tickers": [self.T]})
            k._status()
            with open(os.path.join(k.fast, "status.json")) as fh:
                self.assertIn(self.T, json.load(fh)["hist"])
            old = time.time() - 60                                             # tern web gone: stop serving it
            os.utime(os.path.join(k.fast, "view-web.json"), (old, old))
            k._read_view()
            self.assertEqual(k.web, {"series": [], "tickers": []})


class Phone(Rig, unittest.TestCase):
    """The web's phone pages."""

    IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    ANDROID = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
    LINUX = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

    def hub(self, d):
        from tern.web import Hub
        k, scr = self.setup(d)
        return k, Hub(d)

    def test_which_browsers_are_phones(self):
        from tern.phone import is_phone
        ff = "Mozilla/5.0 (Android 14; {}; rv:140.0) Gecko/140.0 Firefox/140.0"
        tablet = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        for ua, want in ((self.IPHONE, True), (self.ANDROID, True), (ff.format("Mobile"), True), (ff.format("Tablet"), False), (tablet, False),
                         (self.IPHONE.replace("iPhone; CPU iPhone", "iPad; CPU"), False), (self.LINUX, False), ("", False), ("curl/8", False),
                         ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15", False)):
            self.assertEqual(is_phone({"User-Agent": ua}), want, ua)
        self.assertFalse(is_phone({}))
        self.assertTrue(is_phone({"User-Agent": self.LINUX, "Sec-CH-UA-Mobile": "?1"}))      # what Chromium says goes first
        self.assertFalse(is_phone({"User-Agent": self.ANDROID, "Sec-CH-UA-Mobile": "?0"}))   # (the desktop site was asked for)
        self.assertTrue(is_phone({"User-Agent": self.ANDROID, "Sec-CH-UA-Mobile": "junk"}))

    def test_each_browser_gets_its_page_and_can_switch(self):
        import http.client, threading
        from unittest import mock
        from tern import web, webauth
        with tempfile.TemporaryDirectory() as d:
            self.setup(d)
            made = []
            real = web.Server
            class Srv(real):
                def __init__(s, *a, **kw):
                    super().__init__(*a, **kw); made.append(s)
            gone = os.path.join(d, "none")
            with mock.patch.object(web, "Server", Srv), mock.patch.multiple(webauth, AUTH_FILE=gone, CERT_FILE=gone, KEY_FILE=gone), mock.patch("builtins.print"):
                th = threading.Thread(target=web.serve, args=(d, "127.0.0.1", 0), daemon=True); th.start()
                for _ in range(100):
                    if made:
                        break
                    import time; time.sleep(0.02)
                port = made[0].server_address[1]

                def ask(path, ua, cookie=None, body=None):
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    c.request("POST" if body is not None else "GET", path, body, {"User-Agent": ua, **({"Cookie": cookie} if cookie else {})})
                    r = c.getresponse(); out = r.status, r.read().decode(), r
                    c.close()
                    return out
                is_phones = lambda text: 'id="menub"' in text
                self.assertTrue(is_phones(ask("/", self.IPHONE)[1]))
                self.assertTrue(is_phones(ask("/", self.ANDROID)[1]))
                self.assertFalse(is_phones(ask("/", self.LINUX)[1]))
                code, _, r = ask("/ui/full", self.IPHONE)
                self.assertEqual((code, r.headers["Location"]), (303, "/"))
                cookie = r.headers["Set-Cookie"].split(";")[0]
                self.assertEqual(cookie, "tern_ui=full")
                self.assertFalse(is_phones(ask("/", self.IPHONE, cookie)[1]))                # what was picked goes before what was detected
                self.assertTrue(is_phones(ask("/", self.LINUX, "tern_ui=phone")[1]))
                self.assertTrue(is_phones(ask("/", self.IPHONE, "tern_ui=zzz")[1]))
                self.assertEqual(ask("/ui/zzz", self.IPHONE)[0], 404)
                self.assertEqual(ask("/phone", self.IPHONE, body='{"page": "book"}')[0], 200)
                for bad in ("[]", "nonsense", '{"sid": []}', '{"page": {}, "set": 5, "arg": [1], "view": {}, "look": 3}'):
                    self.assertLess(ask("/phone", self.IPHONE, body=bad)[0], 500, bad)
                made[0].shutdown(); th.join(5)

    def test_signed_out_gets_nothing(self):
        import http.client, threading, time
        from unittest import mock
        from tern import web, webauth
        with tempfile.TemporaryDirectory() as d:
            self.setup(d)
            made = []
            class Srv(web.Server):
                def __init__(s, *a, **kw):
                    super().__init__(*a, **kw); made.append(s)
            auth, gone = os.path.join(d, "web.json"), os.path.join(d, "none")
            open(auth, "w").close()
            with mock.patch.object(web, "Server", Srv), mock.patch.multiple(webauth, AUTH_FILE=auth, CERT_FILE=gone, KEY_FILE=gone), \
                    mock.patch.object(webauth, "Auth", return_value=mock.Mock(valid=lambda v: v == "good")), mock.patch("builtins.print"):
                th = threading.Thread(target=web.serve, args=(d, "127.0.0.1", 0), daemon=True); th.start()
                for _ in range(100):
                    if made:
                        break
                    time.sleep(0.02)

                def ask(method, path, cookie):
                    c = http.client.HTTPConnection("127.0.0.1", made[0].server_address[1], timeout=5)
                    c.request(method, path, "{}" if method == "POST" else None, {"User-Agent": self.IPHONE, "Cookie": cookie})
                    r = c.getresponse(); r.read(); c.close()
                    return r.status, r.headers.get("Location")
                for cookie in ("", webauth.COOKIE + "=bad", "tern_ui=phone"):
                    self.assertEqual(ask("POST", "/phone", cookie)[0], 401)
                    self.assertEqual(ask("GET", "/", cookie), (303, "/login"))
                    self.assertEqual(ask("GET", "/ui/full", cookie)[0], 401)
                self.assertEqual(ask("POST", "/phone", webauth.COOKIE + "=good")[0], 200)
                made[0].shutdown(); th.join(5)

    def test_every_page_before_and_after_the_engines_first_status(self):
        import json
        from tern import phone
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            hub = Hub(d)
            for p in phone.PAGES:
                json.dumps(hub.phone({"page": p}))
            k, scr = self.setup(d)
            self.new(k, "base", "mine"); self.sync()
            hub.checked = 0; sid = None
            for p in phone.PAGES:
                for arg in (None, "base", "nonsense", self.T, "../../etc/passwd", "saved:../x"):
                    out = json.loads(json.dumps(hub.phone({"sid": sid, "page": p, "arg": arg}), allow_nan=False)); sid = out["sid"]
                    self.assertEqual(out["page"], p)
                    self.assertIn("data", out)
            self.assertEqual(hub.phone({"sid": sid, "page": "zzz"})["page"], "home")

    def test_the_numbers_are_the_screens(self):
        with tempfile.TemporaryDirectory() as d:
            k, hub = self.hub(d)
            out = hub.phone({"page": "variants"}); sid = out["sid"]
            self.assertEqual([r["name"] for r in out["data"]["rows"]], [v["name"] for v in self.scr.st["variants"]])
            self.assertEqual([r["name"] for r in out["data"]["rows"] if r["selected"]], ["base"])
            self.assertEqual((out["mode"], out["engine"]["ok"]), ("paper", True))
            c = hub.phone({"sid": sid, "page": "contract", "arg": self.T})["data"]
            self.assertEqual((c["t"], c["yes"], c["no"]), (self.T, ["58", "60"], ["40", "42"]))
            self.assertEqual([r["t"] for r in hub.phone({"sid": sid, "page": "ladder"})["data"]["rows"]], [self.T])
            g = hub.phone({"sid": sid, "page": "gate"})["data"]
            self.assertEqual([c["name"] for c in g["checks"]], ["days traded", "settled entries", "paper P&L"])

    def test_a_variant_and_a_report_have_their_neighbors_to_swipe_to(self):
        with tempfile.TemporaryDirectory() as d:
            k, hub = self.hub(d)
            one = hub.phone({"page": "variant", "arg": "band90"})
            self.assertEqual([n for n, _ in one["data"]["all"]], [v["name"] for v in self.scr.st["variants"]])
            self.assertIn(one["data"]["name"], dict(one["data"]["all"]))
            listed = hub.phone({"sid": one["sid"], "page": "reports"})["data"]["rows"]
            rep = hub.phone({"sid": one["sid"], "page": "report", "arg": "desk"})["data"]
            self.assertEqual(rep["all"], [[r["id"], r["label"]] for r in listed])
            self.assertIn(rep["id"], dict(rep["all"]))

    def test_picking_is_each_viewers_own_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            k, hub = self.hub(d)
            read = lambda: pathlib.Path(d, "dash.json").read_text()           # the rig's own screen wrote it
            before = read()
            a, b = hub.phone({"page": "home"}), hub.phone({"page": "home"})
            out = hub.phone({"sid": a["sid"], "page": "book", "set": {"variant": "band90", "theme": "synthwave", "chart": "1h", "series": "KXETHD", "log_filter": "trades"}})
            self.assertEqual((out["selected"]["name"], out["theme"], out["chart"], out["series"]), ("band90", "synthwave", "1h", "KXETHD"))
            self.assertEqual(out["look"]["name"], "synthwave")
            self.assertEqual(out["data"]["paper"], "band90")
            other = hub.phone({"sid": b["sid"], "page": "book", "look": "classic"})
            self.assertEqual((other["selected"]["name"], other["theme"]), ("base", "classic"))
            self.assertNotIn("look", other)                                    # (it has those colors)
            kept = hub.phone({"page": "book", "prefs": out["prefs"]})          # a new tab, with what the browser kept
            self.assertEqual((kept["selected"]["name"], kept["theme"], kept["series"]), ("band90", "synthwave", "KXETHD"))
            for bad in ({"variant": 5}, {"variant": "gone"}, {"theme": "zzz"}, {"series": "ZZZ", "chart": []}, {"live": True}, {"op": "live"}):
                out = hub.phone({"sid": b["sid"], "page": "home", "set": bad})
                self.assertEqual((out["selected"]["name"], out["theme"], out["series"]), ("base", "classic", "KXBTCD"), bad)
            self.assertEqual(hub.phone({"sid": b["sid"], "page": "book", "view": "live"})["data"]["view"], "live")
            self.assertEqual(hub.phone({"sid": b["sid"], "page": "book", "set": {"variant": "band90"}})["data"]["view"], "paper")
            self.assertEqual(self.cmds(), [])
            self.assertEqual(read(), before)

    def test_what_the_browser_has_is_not_sent_again(self):
        with tempfile.TemporaryDirectory() as d:
            k, hub = self.hub(d)
            out = hub.phone({"page": "themes"})
            again = hub.phone({"sid": out["sid"], "page": "themes", "etag": out["etag"]})
            self.assertNotIn("data", again); self.assertEqual(again["etag"], out["etag"])
            self.assertIn("data", hub.phone({"sid": out["sid"], "page": "reports", "etag": out["etag"]}))

    def test_the_log_has_its_tables(self):
        with tempfile.TemporaryDirectory() as d:
            k, hub = self.hub(d)
            import time
            now = time.time()
            logs = [[now - 30, "info", "paper: 25m slot on KXBTCD-26NOV2408 bought · base 2 (10 for $8.85) · late 1 (5 for $4.40)"],
                    [now - 20, "warn", "spot feed lost"]]
            hub.phone({"page": "home"})
            hub.st["logs"] = logs; hub.checked = now + 60
            e = hub.phone({"page": "log"})["data"]["entries"]
            self.assertEqual([(x["lvl"], len(x["rows"])) for x in e], [("warn", 0), ("info", 2)])   # newest first
            self.assertEqual([[c[0] for c in r][:4] for r in e[1]["rows"]], [["base", "2", "10", "$8.85"], ["late", "1", "5", "$4.40"]])
            self.assertEqual(len(e[1]["rows"][0]), len(e[1]["head"]))

    def test_the_engine_gone_quiet_is_said(self):
        with tempfile.TemporaryDirectory() as d:
            k, hub = self.hub(d)
            hub.phone({"page": "home"}); hub.st["heartbeat"] -= 60; hub.checked = hub.st["heartbeat"] + 200
            hub.alive = lambda: True
            e = hub.phone({"page": "home"})["engine"]
            self.assertFalse(e["ok"]); self.assertIn("nothing for", e["text"])
            hub.alive = lambda: False
            self.assertIn("paused", hub.phone({"page": "home"})["engine"]["text"])

    def test_the_page_keeps_to_its_policy(self):
        """Nothing from elsewhere, and text is put in as text."""
        from tern import phone
        with open(phone.PAGE) as fh:
            text = fh.read()
        for no in ("http://", "https://", "innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(no, text)


class AndroidApp(unittest.TestCase):
    """The Android app's parts that need no Android: what it takes for an address, and which server it takes for the desk."""

    HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "android")

    @classmethod
    def setUpClass(cls):
        import shutil, subprocess
        if not (shutil.which("javac") and shutil.which("openssl")):
            raise unittest.SkipTest("needs a JDK and openssl")
        cls.tmp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.tmp.cleanup)
        subprocess.run(["javac", "--release", "17", "-d", cls.tmp.name, os.path.join(cls.HERE, "src", "tern", "desk", "Link.java"), os.path.join(cls.HERE, "LinkTest.java")],
                       check=True, capture_output=True)

    def java(self, *a) -> list:
        import subprocess
        return subprocess.run(["java", "-cp", self.tmp.name, "tern.desk.LinkTest", *a], check=True, capture_output=True, text=True, timeout=60).stdout.split("\n")[:-1]

    def server(self, name, hold=False):
        """An HTTPS server with a certificate of its own; `hold` answers no handshake."""
        import socket, ssl, threading
        from unittest import mock
        from tern import webauth
        cert, key = (os.path.join(self.tmp.name, f"{name}-{n}.pem") for n in ("cert", "key"))
        with mock.patch.multiple(webauth, DIR=self.tmp.name, CERT_FILE=cert, KEY_FILE=key):
            pin = webauth.make_cert(["127.0.0.1"])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
        sock = socket.create_server(("127.0.0.1", 0)); self.addCleanup(sock.close)
        got: list = []

        def one(c):
            try:
                with ctx.wrap_socket(c, server_side=True) as t:
                    t.settimeout(5); got.append(t.recv(100))
            except OSError:
                pass

        def run():
            kept = []
            while True:
                try:
                    c, _ = sock.accept()
                except OSError:
                    return
                kept.append(c)
                if not hold:
                    threading.Thread(target=one, args=(c,), daemon=True).start()
        threading.Thread(target=run, daemon=True).start()
        return f"https://127.0.0.1:{sock.getsockname()[1]}", pin, got

    def test_what_is_taken_for_an_address(self):
        cases = {"192.0.2.7": "https://192.0.2.7:8787", " 192.0.2.7:99 ": "https://192.0.2.7:99", "https://Desk.Example/": "https://desk.example:8787", "": "null", "  ": "null",
                 "[2001:db8::1]": "https://[2001:db8::1]:8787", "http://192.0.2.7": "no", "ftp://x": "no", "https://me@192.0.2.7": "no", "192.0.2.7/x": "no", "192.0.2.7?a": "no",
                 "javascript:alert(1)": "no", "x:70000": "no", "x:0": "no", "a b": "no", "file:///etc/passwd": "no", "x#y": "no"}
        self.assertEqual(dict(zip(cases, self.java("base", *cases))), cases)

    def test_a_page_is_the_desks_only_at_its_address(self):
        base = "https://192.0.2.7:8787"
        cases = {base + "/#/spot": "true", base: "true", "http://192.0.2.7:8787/": "false", "https://192.0.2.7/": "false", "https://192.0.2.7:8788/": "false",
                 "https://192.0.2.7:8787.evil.example/": "false", "https://192.0.2.7:8787@evil.example/": "false", "https://evil.example/192.0.2.7:8787": "false", "about:blank": "false"}
        self.assertEqual(dict(zip(cases, self.java("at", base, *cases))), cases)

    def test_only_the_approved_certificate_is_the_desk(self):
        desk, pin, got = self.server("desk")
        other, other_pin, other_got = self.server("other")
        self.assertEqual(self.java("find", pin, desk)[:-1], [f"{desk} {pin}"])
        self.assertEqual(self.java("find", pin, other, desk)[:-1], [f"{desk} {pin}"])                         # another house's machine at the home address
        self.assertEqual(self.java("find", pin, other)[:-1], [f"{other} {other_pin}"])                        # not it: the app says whose it got
        self.assertEqual(self.java("find", "", desk)[:-1], [f"{desk} {pin}"])                                 # none approved yet: it is asked about
        self.assertEqual([x for x in got + other_got if x], [])                                              # nothing is sent to either

    def test_home_goes_first_and_an_address_that_is_dead_or_silent_holds_up_nothing(self):
        desk, pin, _ = self.server("desk")
        silent, _, _ = self.server("silent", hold=True)
        import socket
        with socket.create_server(("127.0.0.1", 0)) as s:
            dead = f"https://127.0.0.1:{s.getsockname()[1]}"
        *out, ms = self.java("find", pin, dead, desk)
        self.assertEqual(out, [f"{desk} {pin}"]); self.assertLess(int(ms), 1500)
        *out, ms = self.java("find", pin, silent, desk)
        self.assertEqual(out, [f"{desk} {pin}"]); self.assertLess(int(ms), 4000)
        self.assertEqual(self.java("find", pin, desk, silent)[:-1], [f"{desk} {pin}"])
        *out, ms = self.java("find", pin, dead, silent)
        self.assertEqual(out, [f"{dead} -", f"{silent} -"]); self.assertLess(int(ms), 14000)

    def test_it_asks_for_the_network_only_and_trusts_no_authority(self):
        import re
        with open(os.path.join(self.HERE, "AndroidManifest.xml")) as fh:
            man = fh.read()
        self.assertEqual(sorted(re.findall(r'uses-permission android:name="android.permission.(\w+)"', man)), ["ACCESS_NETWORK_STATE", "INTERNET"])
        self.assertIn('android:allowBackup="false"', man)
        with open(os.path.join(self.HERE, "res", "xml", "network.xml")) as fh:
            net = fh.read()
        self.assertIn("<trust-anchors />", net); self.assertNotIn("<certificates", net); self.assertNotIn('Permitted="true"', net)


class Reports(Rig, unittest.TestCase):
    """The reports screen (o): the reports tern works out from its records, and the saved ones."""

    def save(self, d, name, text):
        with open(os.path.join(d, name), "w") as fh:
            fh.write(text)

    def until(self, what, where=None):
        """Draw until `what` is on the screen: reports are worked out on a thread."""
        import time
        end = time.time() + 10
        where = where or (lambda: (self.sync(), self.text())[1])
        while time.time() < end:
            if what in where():
                return
            time.sleep(0.02)
        self.assertIn(what, where())

    def test_index_lists_live_reports_then_saved_ones_newest_first(self):
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            self.desk(d)
            for n in ("report-2026-11-09.txt", "report-2026-11-23.txt", "notes.txt", "report-x.json"):
                self.save(d, n, "x\n")
            ix = reports.index(d)
            self.assertEqual([r.id for r in ix], ["desk", "periods",
                                                  "saved:report-2026-11-23.txt", "saved:report-2026-11-09.txt"])
            self.assertEqual([r.label for r in ix if r.saved], ["2026-11-23", "2026-11-09"])

    def test_the_screen_and_the_command_line_give_the_same_report(self):
        import contextlib, io
        from unittest import mock
        from tern import __main__ as cli, config as cfgmod, reports
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "RUNTIME", d):
            k = self.desk(d)
            k._shadow(); k.shadow.settle({self.T: True})
            for argv, rid in (([], "desk"), (["--periods"], "periods")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["report", *argv]), 0)
                self.assertEqual("\n".join(reports.lines(d, rid)) + "\n", out.getvalue(), rid)

    def test_a_saved_report_is_shown_as_written(self):
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            self.save(d, "report-2026-11-23.txt", "one\n\n  two\n")
            self.assertEqual(reports.lines(d, "saved:report-2026-11-23.txt"), ["one", "", "  two"])

    def test_a_saved_report_has_no_control_characters_and_stays_in_the_runtime_folder(self):
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            self.save(d, "report-a.txt", "a\tb\x1b[31mc\x00\n")
            self.assertEqual(reports.lines(d, "saved:report-a.txt"), ["a       b[31mc"])
            for rid in ("saved:../report-a.txt", "saved:/etc/passwd", "saved:config.json", "saved:report-/../x.txt"):
                with self.assertRaises(LookupError):
                    reports.lines(d, rid)

    def test_feed_works_out_a_report_again_only_when_its_files_change(self):
        from unittest import mock
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            feed = reports.Feed(check=0)
            self.save(d, "report-a.txt", "first\n")
            get = lambda: "\n".join((feed.get(d, "saved:report-a.txt") or reports.Report([], 0)).lines)
            self.until("first", get)
            with mock.patch.object(reports, "lines", side_effect=AssertionError("worked out again")):
                for _ in range(5):
                    self.assertEqual(get(), "first")
            self.save(d, "report-a.txt", "second, longer\n")
            self.until("second, longer", get)

    def test_feed_says_why_a_report_cannot_be_read(self):
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            feed = reports.Feed(check=0)
            self.until("this report could not be read", lambda: "\n".join(
                (feed.get(d, "gone") or reports.Report([], 0)).lines))

    def test_the_reports_box(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.save(d, "report-2026-11-23.txt", "saved on a Monday\n")
            status = "".join(scr.s.ch[39])
            self.cmds(); self.press("8")
            self.until("base: the rule")
            for what in ("LIVE", "SAVED", "variants", "by period", "2026-11-23", "tern report · worked out"):
                self.assertIn(what, self.text())
            self.assertEqual("".join(scr.s.ch[39])[:20], status[:20])          # the status line stays
            self.press(27, "o")
            self.assertEqual((scr.tab, self.cmds()), ("reports", []))          # left as any box is

    def test_arrows_pick_a_report_and_pages_scroll_it(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.save(d, "report-2026-11-23.txt", "".join(f"line {i:03}\n" for i in range(200)))
            c = self.curses
            self.press("8", c.KEY_DOWN)
            self.until("settings 1")
            self.press(c.KEY_DOWN)                                             # the last report
            self.until("line 000")
            self.press(c.KEY_NPAGE)
            self.assertNotIn("line 000", self.text())
            self.assertIn("line 010", self.text())
            self.press(c.KEY_END)
            self.assertIn("line 199", self.text())
            self.assertIn("of 200", self.text())
            self.press(c.KEY_NPAGE, c.KEY_NPAGE, c.KEY_PPAGE)                  # no slack built up past the end
            self.assertNotIn("line 199", self.text())
            self.press(c.KEY_HOME)
            self.assertIn("line 000", self.text())
            self.press(c.KEY_NPAGE, c.KEY_UP)
            self.until("settings 1")                                           # a new pick starts at its top
            self.assertEqual(scr.rep_off, 0)

    def test_a_small_screen_shows_every_column_and_wraps_sentences(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            self.press("8")
            self.until("multi: every coin at once")
            self.press(self.curses.KEY_NPAGE)
            self.assertIn("2026-11-20)", self.text())                          # calm's description, wrapped, not cut
            row = next(l for l in self.text().splitlines() if "calm" in l and "+0.00" in l) if "+0.00" in self.text() else "+0.00"
            self.assertIn("+0.00", row)

    def test_the_wheel_scrolls_the_box_under_the_pointer(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.save(d, "report-2026-11-23.txt", "".join(f"line {i:03}\n" for i in range(200)))
            self.press("8", self.curses.KEY_DOWN, self.curses.KEY_DOWN)
            self.until("line 000")
            y, x, h, w = scr.rects["report"]
            scr.wheel(y + 3, x + 3, 1); self.sync()
            self.assertNotIn("line 000", self.text())
            y, x, h, w = scr.rects["reports"]
            scr.wheel(y + 3, x + 3, -1); self.sync()
            self.until("settings 1")

    def test_a_report_that_is_gone_falls_back_to_the_first(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.save(d, "report-2026-11-23.txt", "old one\n")
            self.press("8", self.curses.KEY_DOWN, self.curses.KEY_DOWN)
            self.until("old one")
            os.remove(os.path.join(d, "report-2026-11-23.txt"))
            scr.rep_at = 0
            self.until("base: the rule")
            self.assertEqual(scr.rep_sel, "desk")

    def test_gains_are_green_and_losses_red(self):
        from tern.dash import GREEN, RED
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            spans = scr.report_spans(["variant  hours entries open       P&L ¢/contract   win      t",
                                      "base         1       5    0     +1.82     +12.13  100%  -2.10"])
            color = {t.strip(): a for t, a in spans[1]}
            self.assertEqual(color["+1.82"], scr.P(GREEN, bold=True))
            self.assertEqual(color["-2.10"], scr.P(RED, bold=True))
            self.assertEqual("".join(t for t, _ in spans[1]), "base         1       5    0     +1.82     +12.13  100%  -2.10")
            self.assertEqual(color["base"], scr.P(scr.P.c("variant"), bold=True))

    def test_the_web_reads_reports_and_sends_nothing(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            Web.status(self, d)
            self.save(d, "report-2026-11-23.txt", "saved on a Monday\n")
            hub = Hub(d)
            sid = hub.handle({"w": 120, "h": 40})["sid"]
            go = lambda **req: ["".join(t for t, _ in r) for r in hub.handle({"sid": sid, "w": 120, "h": 40, **req})["rows"]]
            go(key="8")
            self.until("base: the rule", lambda: "\n".join(go()))
            rows = go()
            y = next(i for i, r in enumerate(rows) if "2026-11-23" in r[:24])
            go(click={"y": y, "x": rows[y].index("2026-11-23")})               # a click picks it
            self.until("saved on a Monday", lambda: "\n".join(go()))
            self.assertEqual(os.listdir(os.path.join(d, "cmd")), [])
            self.assertEqual(sorted(os.listdir(d)), ["cmd", "report-2026-11-23.txt"])

    def test_save_writes_a_new_file_each_time_and_lists_it(self):
        import time
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            self.desk(d)
            now = time.mktime((2026, 11, 30, 9, 5, 0, 0, 0, -1))
            one = reports.save(d, "periods", now=now)
            two = reports.save(d, "periods", now=now)
            self.assertEqual([one, two], ["report-2026-11-30-0905-by-period.txt", "report-2026-11-30-0905-by-period-2.txt"])
            got = reports.lines(d, "saved:" + one)
            self.assertEqual(got[:2], ["2026-11-30 09:05 · tern report --periods", ""])
            self.assertEqual(got[2:], reports.lines(d, "periods"))
            three = reports.save(d, "desk", now=now + 60)
            self.save(d, "report-2026-11-30.txt", "x\n")
            self.save(d, "report-2026-11-23.txt", "x\n")
            self.assertEqual([r.label for r in reports.index(d) if r.saved],
                             ["11-30 09:06 variants", "11-30 09:05 by-period-2", "11-30 09:05 by-period",
                              "2026-11-30", "2026-11-23"])
            self.assertFalse([n for n in os.listdir(d) if n.endswith(".tmp")])
            self.assertEqual(reports.index(d)[2].id, "saved:" + three)

    def test_s_saves_the_report_shown(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            saved = lambda: [n for n in os.listdir(d) if n.startswith("report-")]
            self.cmds(); self.press("8", self.curses.KEY_DOWN)
            self.assertIn("─save─", self.text())
            self.press("s")
            self.until("saved runtime/report-")
            [name] = saved()
            self.assertTrue(name.endswith("-by-period.txt"), name)
            self.until(f"{name[12:17]} {name[18:20]}:{name[20:22]} by-period")     # listed, whole
            self.press(self.curses.KEY_DOWN)
            self.until("· tern report --periods")
            self.press("s")
            self.assertIn("already saved", self.text())
            self.assertEqual(saved(), [name])
            self.assertEqual(self.cmds(), [])

    def test_P_pauses_the_engine(self):
        from unittest import mock
        from tern import dash
        with tempfile.TemporaryDirectory() as d, mock.patch.object(dash, "engine_alive", lambda r: True), \
                mock.patch.object(dash, "stop_engine", lambda *a: "asked to stop"):
            k, scr = self.setup(d)
            self.press("6"); self.assertIn("─Pause─", self.text())
            self.cmds(); self.press("S")
            self.assertIsNone(scr.modal)                                       # S is gone
            self.press("P")
            self.assertEqual(scr.modal["type"], "stop")
            self.assertIn("Pause the engine?", self.text())
            self.press("n")
            self.press("8", "P")                                               # from any box
            self.assertIn("Pause the engine?", self.text())
            self.press("y")
            self.assertIn("asked to stop", self.text())
            self.assertEqual((scr.tab, self.cmds()), ("reports", []))
            with mock.patch.object(dash, "engine_alive", lambda r: False):
                scr.alive_at = 0; self.press("6")
                self.assertIn("─P start─", self.text())

    def test_a_save_that_fails_says_so(self):
        from unittest import mock
        from tern import reports
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("8")
            with mock.patch.object(reports, "lines", side_effect=OSError(28, "No space left on device")):
                self.press("s")
                self.until("not saved: No space left on device")
            self.assertFalse(scr.rep_saving)
            self.assertEqual([n for n in os.listdir(d) if n.startswith("report-")], [])

    def test_the_web_never_saves(self):
        from tern.web import Hub
        with tempfile.TemporaryDirectory() as d:
            Web.status(self, d)
            hub = Hub(d)
            sid = hub.handle({"w": 120, "h": 40})["sid"]
            go = lambda **req: "\n".join("".join(t for t, _ in r) for r in hub.handle({"sid": sid, "w": 120, "h": 40, **req})["rows"])
            self.assertNotIn("─save─", go(key="8"))
            self.assertIn("view only", go(key="s"))
            self.assertIn("view only", go(key="P"))                            # nor pauses the engine from in here
            self.assertIsNone(hub.sessions[sid][0].modal)
            go(key="6")
            for k in "P":
                self.assertIn("view only", go(key=k))
                self.assertIsNone(hub.sessions[sid][0].modal)
                hub.sessions[sid][0].flash = ""
            self.assertEqual(os.listdir(d), ["cmd"])

    def test_report_save_on_the_command_line(self):
        import contextlib, io
        from unittest import mock
        from tern import __main__ as cli, config as cfgmod, reports
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "RUNTIME", d):
            k = self.desk(d); k._status()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["report", "--save"]), 0)
            [name] = [n for n in os.listdir(d) if n.startswith("report-")]
            self.assertIn(name, out.getvalue())
            self.assertTrue(name.endswith("-summary.txt"), name)
            text = "\n".join(reports.lines(d, "saved:" + name))
            for what in ("mode paper", "variants on paper", "base: the rule", "the live gate"):
                self.assertIn(what, text)

    def test_report_timer_writes_the_units(self):
        import contextlib, io
        from unittest import mock
        from tern import __main__ as cli, config as cfgmod
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}), \
                mock.patch.object(cfgmod, "RUNTIME", "/r s/100%"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["report-timer"]), 0)
            u = os.path.join(home, ".config", "systemd", "user")
            self.assertEqual(sorted(os.listdir(u)), ["tern-save-report.service", "tern-save-report.timer"])
            with open(os.path.join(u, "tern-save-report.service")) as fh:
                svc = fh.read()
            with open(os.path.join(u, "tern-save-report.timer")) as fh:
                tim = fh.read()
            self.assertIn("-m tern report --save", svc)
            self.assertIn('Environment="TERN_RUNTIME=/r s/100%%"', svc)
            self.assertIn('ExecStartPre=/usr/bin/test -d "/r s/100%%"', svc)
            self.assertIn("Type=oneshot", svc)
            self.assertIn("OnCalendar=Mon *-*-* 10:00:00", tim)
            self.assertIn("Persistent=true", tim)
            self.assertIn("systemctl --user enable --now tern-save-report.timer", out.getvalue())

    def test_the_hint_and_the_help_name_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.assertIn("⁸reports", self.text())
            text = "\n".join(l if isinstance(l, str) else "".join(t for t, _ in l) for _, ls in scr.help_sections(200) for l in ls)
            self.assertIn("every record: worked out now, or saved", text)


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
                link = f'<a href="{web.SOURCE}" target="_blank" rel="noopener noreferrer">source</a>'.encode()
                r, body = ask("GET", "/login")
                self.assertIn(link, body)                                    # where this tern's code is (AGPL)
                r, body = ask("POST", "/login", b"password=nope", {"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(r.status, 401)
                self.assertIn(link, body)
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


class WebApp(unittest.TestCase):
    """tern web for the paired app: its sign-in, signed commands, and the browsers / app switches."""

    def setUp(self):
        import http.client, socket, ssl, threading, time
        from unittest import mock
        from tern import config as cfgmod, devices, web, webauth
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.d = self.tmp.name
        files = {"AUTH_FILE": os.path.join(d, "web.json"), "CERT_FILE": os.path.join(d, "c.pem"),
                 "KEY_FILE": os.path.join(d, "k.pem")}
        p = mock.patch.multiple(webauth, **files); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(devices, "FILE", os.path.join(d, "app-devices.json")); p.start(); self.addCleanup(p.stop)
        webauth.set_password("correct horse battery")
        webauth.make_cert([])
        self.run_dir = os.path.join(d, "run"); os.makedirs(self.run_dir)
        self.cfg = cfgmod.Config()
        self.cfg.web.app = True
        self.set_web()
        pubs = {}
        for which in ("everyday", "live"):
            subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", which + ".pem"],
                           cwd=d, check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", which + ".pem", "-pubout", "-outform", "DER", "-out", which + ".der"],
                           cwd=d, check=True, capture_output=True)
            pubs[which] = base64.b64encode(pathlib.Path(d, which + ".der").read_bytes()).decode()
        self.dev = devices.Devices(devices.FILE).add("pixel", pubs["everyday"], pubs["live"])
        self.n = 0
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
        threading.Thread(target=web.serve, args=(self.run_dir, "127.0.0.1", port), daemon=True).start()
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        ctx = ssl.create_default_context(cafile=files["CERT_FILE"])
        self.c = http.client.HTTPSConnection("localhost", port, context=ctx, timeout=10)
        self.addCleanup(self.c.close)

    def set_web(self, **kw):
        from tern import config as cfgmod
        for k, v in kw.items():
            setattr(self.cfg.web, k, v)
        cfgmod.save(self.cfg, self.run_dir)
        st = os.stat(os.path.join(self.run_dir, "config.json"))
        os.utime(os.path.join(self.run_dir, "config.json"), ns=(st.st_atime_ns, st.st_mtime_ns + 10**9 * getattr(self, "bump", 1)))
        self.bump = getattr(self, "bump", 1) + 1

    def ask(self, method, path, body=None, headers=None):
        self.c.request(method, path, body, headers or {})
        r = self.c.getresponse(); return r, r.read()

    def sign(self, text, which="everyday", dev=None):
        pathlib.Path(self.d, "msg").write_bytes(text.encode())
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", which + ".pem", "-out", "sig.der", "msg"],
                       cwd=self.d, check=True, capture_output=True)
        return base64.b64encode(pathlib.Path(self.d, "sig.der").read_bytes()).decode()

    def signed(self, cmd, n=None, ts=None, which="everyday", key=None):
        import time
        if n is None:
            self.n += 1; n = self.n
        payload = json.dumps({"d": self.dev, "n": n, "ts": time.time() if ts is None else ts, "cmd": cmd})
        return json.dumps({"payload": payload, "sig": self.sign(payload, which), "key": key or which}).encode()

    def post(self, path, body, cookie=None):
        r, b = self.ask("POST", path, body, {"Cookie": cookie} if cookie else {})
        return r, json.loads(b) if b[:1] == b"{" else b

    def app_cookie(self):
        r, b = self.post("/app/session", self.signed({"op": "session"}))
        self.assertEqual((r.status, b.get("ok"), b.get("name")), (200, True, "pixel"))
        ck = r.getheader("Set-Cookie")
        for flag in ("__Host-tern_a=", "HttpOnly", "Secure", "SameSite=Strict", f"Max-Age={30 * 86400}"):
            self.assertIn(flag, ck)
        return ck.split(";", 1)[0]

    def browser_cookie(self):
        r, _ = self.ask("POST", "/login", b"password=correct+horse+battery", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 303)
        return r.getheader("Set-Cookie").split(";", 1)[0]

    def test_app_session_then_view(self):
        ck = self.app_cookie()
        r, b = self.post("/phone", b"{}", ck)
        self.assertEqual((r.status, b.get("control"), r.getheader("X-Tern-App")), (200, True, "1"))
        r, page = self.ask("GET", "/", headers={"Cookie": ck, "User-Agent": "Mozilla/5.0 X11 Linux"})   # a desktop UA still gets the phone page
        self.assertEqual(r.status, 200)
        self.assertIn(b"phone", page.lower())
        r, b = self.post("/phone", b"{}", self.browser_cookie())
        self.assertEqual(r.status, 200)
        self.assertNotIn("control", b)
        self.assertIsNone(r.getheader("X-Tern-App"))

    def test_act_needs_both_cookie_and_signature(self):
        ck = self.app_cookie()
        r, b = self.post("/act", self.signed({"op": "x"}), ck)            # past auth: only the command is unknown
        self.assertEqual((r.status, b["error"]), (403, "unknown command"))
        body = json.loads(self.signed({"op": "x"}))
        body["sig"] = self.sign(body["payload"], "live")                  # signed by another key
        r, b = self.post("/act", json.dumps(body).encode(), ck)
        self.assertEqual((r.status, b["error"]), (403, "not signed by this phone's key"))
        r, b = self.post("/act", self.signed({"op": "x"}), self.browser_cookie())   # the password cookie does not act
        self.assertEqual((r.status, b["error"]), (403, "sign in again"))
        r, b = self.post("/act", self.signed({"op": "x"}))
        self.assertEqual(r.status, 403)
        one = self.signed({"op": "x"})
        self.post("/act", one, ck)
        r, b = self.post("/act", one, ck)
        self.assertEqual((r.status, b["error"]), (403, "sent twice: refused"))

    def test_clock_message(self):
        import time
        r, b = self.post("/app/session", self.signed({"op": "session"}, ts=time.time() - 90))
        self.assertEqual(r.status, 403)
        self.assertIn("90 s", b["error"])
        self.assertAlmostEqual(b["now"], time.time(), delta=5)

    def test_browsers_off(self):
        ck, bck = self.app_cookie(), self.browser_cookie()
        self.set_web(enabled=False)
        r, b = self.ask("GET", "/")
        self.assertEqual((r.status, b), (403, b"the browser page is turned off (settings, WEB)"))
        self.assertEqual(self.ask("GET", "/login")[0].status, 403)
        self.assertEqual(self.ask("POST", "/login", b"password=correct+horse+battery")[0].status, 403)
        self.assertEqual(self.post("/phone", b"{}", bck)[0].status, 403)
        r, b = self.post("/phone", b"{}", ck)
        self.assertEqual((r.status, b.get("control")), (200, True))
        r, b = self.post("/act", self.signed({"op": "x"}), ck)
        self.assertEqual((r.status, b["error"]), (403, "unknown command"))

    def test_app_off(self):
        ck = self.app_cookie()
        self.set_web(app=False)
        for path in ("/app/pair", "/app/paired", "/app/session", "/act"):
            r, b = self.post(path, b"{}", ck)
            self.assertEqual((r.status, "off" in b["error"]), (403, True), path)
        self.assertEqual(self.post("/phone", b"{}", ck)[0].status, 401)   # the app cookie is dead too
        self.assertEqual(self.post("/phone", b"{}", self.browser_cookie())[0].status, 200)

    def test_config_arms_live_without_a_status_file(self):
        ck = self.app_cookie()
        self.cfg.live.enabled = True
        self.set_web()
        r, b = self.post("/act", self.signed({"op": "set", "key": "gate.min_days", "value": 1}), ck)
        self.assertEqual((r.status, b["error"]), (403, "live needs your fingerprint"))

    def cmd_files(self):
        d = os.path.join(self.run_dir, "cmd")
        out = []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if n.endswith(".json"):
                with open(os.path.join(d, n)) as fh:
                    out.append(json.load(fh))
        return out

    def act(self, ck, cmd, which="everyday"):
        return self.post("/act", self.signed(cmd, which=which), ck)

    def test_act_writes_engine_commands_like_the_screen(self):
        from unittest import mock
        from tern import dash
        ck = self.app_cookie()
        cases = [
            ({"op": "live", "on": True, "variant": "base", "junk": 1}, {"op": "live", "on": True, "variant": "base"}),
            ({"op": "variant_new", "from": "base", "name": "mine", "rule": {"band_lo": 0.8}, "id": "evil", "x": 1},
             {"op": "variant_new", "from": "base", "name": "mine", "rule": {"band_lo": 0.8}}),
            ({"op": "rename", "variant": "a", "name": "b", "z": 1}, {"op": "rename", "variant": "a", "name": "b"}),
            ({"op": "variant_delete", "variant": "a", "z": 1}, {"op": "variant_delete", "variant": "a"}),
            ({"op": "variant_on", "variant": "a", "on": False}, {"op": "variant_on", "variant": "a", "on": False}),
            ({"op": "set", "key": "gate.min_days", "value": 1}, {"op": "set", "key": "gate.min_days", "value": 1}),
        ]
        with mock.patch.object(dash, "engine_alive", return_value=True):
            for cmd, want in cases:
                before = len(self.cmd_files())
                r, b = self.act(ck, cmd, "live" if cmd["op"] == "live" else "everyday")
                self.assertEqual(r.status, 200, b)
                files = self.cmd_files()
                self.assertEqual(len(files), before + 1)
                got = files[-1]
                self.assertEqual(got.pop("by"), "app pixel")
                self.assertEqual(got.pop("id"), b["id"])
                self.assertNotEqual(got["ts"], None)
                got.pop("ts")
                self.assertEqual(got, want)
                self.assertEqual(set(b), {"id", "say"})
            names = os.listdir(os.path.join(self.run_dir, "cmd"))
            self.assertFalse([n for n in names if n.endswith(".tmp")])

    def test_act_unknown_and_engine_down(self):
        from unittest import mock
        from tern import dash
        ck = self.app_cookie()
        with mock.patch.object(dash, "engine_alive", return_value=True):
            for bad in ({"op": "rm"}, {"op": 5}, {}, {"op": "engine"}, {"op": "report_save"}):
                r, b = self.act(ck, bad)
                self.assertEqual((r.status, b["error"]), (403, "unknown command"), bad)
        with mock.patch.object(dash, "engine_alive", return_value=False):
            r, b = self.act(ck, {"op": "live", "on": False})
        self.assertEqual((r.status, b["say"]), (200, "the engine isn't running: start it first"))
        self.assertEqual(self.cmd_files(), [])

    def test_act_engine_start_stop_and_report_note(self):
        from unittest import mock
        from tern import dash
        ck = self.app_cookie()
        with mock.patch.object(dash, "engine_alive", return_value=True), \
                mock.patch.object(dash, "stop_engine", return_value="stopping the engine: it saves first") as stop:
            r, b = self.act(ck, {"op": "engine", "run": False})
            self.assertEqual((r.status, b["say"]), (200, "stopping the engine: it saves first"))
            stop.assert_called_once_with(self.run_dir, None)
        notes = self.cmd_files()
        self.assertEqual([(c["op"], c["by"]) for c in notes], [("note", "app pixel")])
        self.assertIn("stopping", notes[0]["text"])
        for n in os.listdir(os.path.join(self.run_dir, "cmd")):
            os.remove(os.path.join(self.run_dir, "cmd", n))
        with mock.patch.object(dash, "engine_alive", return_value=False), \
                mock.patch.object(dash, "start_engine", return_value="engine started in the background") as start:
            r, b = self.act(ck, {"op": "engine", "run": True})
            self.assertEqual((r.status, b["say"]), (200, "engine started in the background"))
            start.assert_called_once_with(self.run_dir)
        self.assertEqual([c["op"] for c in self.cmd_files()], ["note"])
        self.assertIn("engine started", self.cmd_files()[0]["text"])
        for n in os.listdir(os.path.join(self.run_dir, "cmd")):
            os.remove(os.path.join(self.run_dir, "cmd", n))
        for said in ("engine already running", "systemctl start failed: boom"):
            with mock.patch.object(dash, "engine_alive", return_value=False), \
                    mock.patch.object(dash, "start_engine", return_value=said):
                r, b = self.act(ck, {"op": "engine", "run": True})
            self.assertEqual((r.status, b["say"]), (200, said))
            self.assertEqual(self.cmd_files(), [])

    def test_act_report_save(self):
        from unittest import mock
        from tern import reports
        ck = self.app_cookie()
        with mock.patch.object(reports, "save", return_value="report-x.txt") as sv:
            r, b = self.act(ck, {"op": "report_save", "report": "r1"})
            sv.assert_called_once_with(self.run_dir, "r1")
        self.assertEqual((r.status, b["say"]), (200, "saved report report-x.txt"))
        self.assertEqual([c.get("text") for c in self.cmd_files()], ["saved report report-x.txt"])
        with mock.patch.object(reports, "save", side_effect=LookupError("no report 'q'")):
            r, b = self.act(ck, {"op": "report_save", "report": "q"})
        self.assertEqual((r.status, b["error"]), (403, "no report 'q'"))
        r, b = self.act(ck, {"op": "report_save", "report": 5})
        self.assertEqual((r.status, b["error"]), (403, "unknown command"))

    def test_phone_returns_the_results_of_this_apps_commands(self):
        import time
        from unittest import mock
        from tern import dash
        from tern.engine import live_dir
        ck = self.app_cookie()
        with mock.patch.object(dash, "engine_alive", return_value=True):
            r, b = self.act(ck, {"op": "live", "on": False})
        cid = b["id"]
        r, p = self.post("/phone", b"{}", ck)
        self.assertEqual(p["results"], {})                      # the engine hasn't answered
        with open(os.path.join(live_dir(self.run_dir), "status.json"), "w") as fh:
            json.dump({"results": {cid: "disarmed", "other": "not mine"}}, fh)
        time.sleep(0.3)
        r, p = self.post("/phone", b"{}", ck)
        self.assertEqual(p["results"], {cid: "disarmed"})
        r, p = self.post("/phone", b"{}", self.browser_cookie())
        self.assertNotIn("results", p)

    def test_pairing_endpoints(self):
        r, b = self.post("/app/pair", b'{"code": 1}')
        self.assertEqual(r.status, 403)
        self.assertEqual(self.post("/app/paired", b'{"offer": "nope"}')[1], {"state": "unknown"})
        self.assertEqual(self.post("/app/pair", b"[]")[0].status, 400)


class ReviewFixes2(Rig, unittest.TestCase):
    """From the review of 2026-11-23: one list, tabs, themes."""

    def test_a_coin_still_held_is_fetched_and_its_hour_settles(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            for name in k.shadow.own.active():
                k.shadow.own.off.add(name)
            for name in ("multi",):
                k.shadow.own.off.add(name)
            self.assertEqual(k._wanted(), {"KXBTCD"})
            k.shadow.rows.append({"v": "multi", "ts": 1.0, "event": "KXETHD-E9", "close": time.time() + 60, "ticker": "KXETHD-E9-T1",
                                  "side": "yes", "n": 1, "cost": 0.8, "fee": 0.01, "slot": 25})
            self.assertEqual(k._wanted(), {"KXBTCD", "KXETHD"})                # it is off, its entry is open
            k.live.pos["KXSOLD-E9-T1"] = {"q": 1, "avg": 0.8}
            self.assertIn("KXSOLD", k._wanted())                               # live's loss limit needs its price
            k.live.pos.clear(); k.shadow.rows[:] = [r for r in k.shadow.rows if r["v"] != "multi"]
            k.events["KXETHD"] = {"event": "KXETHD-E9", "close": time.time() - 1, "markets": [], "fetched": 0.0, "title": ""}
            k._refresh_event = k._commands = k._auto = lambda *a: None
            k._shadow = k._settle = k._live_ready = lambda *a: None
            k.step()
            self.assertNotIn("KXETHD", k.events); self.assertIn("KXETHD-E9", k.closed)   # nobody fetches it, its results are owed

    def test_live_is_off_when_what_is_off_is_lost(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, name="mine")
            k._command({"op": "live", "on": True, "variant": "base"})
            k._command({"op": "variant_on", "variant": "base", "on": False})
            path = os.path.join(d, "variants.json")
            with open(path) as fh:
                text = fh.read()
            with open(path, "w") as fh:
                fh.write(text[:len(text) // 2])                                # cut short by a crash
            k = self.desk(d)
            self.assertFalse(k.cfg.live.enabled)
            self.assertTrue(any("live switched off" in m for _, _, m in k.logs))
            self.assertFalse(self.desk(d).cfg.live.enabled)                    # and stays so

    def test_a_hand_edit_of_the_paper_start_is_a_settings_period(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            sv = k.slog.sv
            from tern import config
            c = config.load(d); c.paper_capital = 100.0; config.save(c, d)
            k._reload_cfg(force=True)
            self.assertEqual(k.slog.sv, sv + 1)
            self.assertEqual(k.slog.lines()[-1]["key"], "paper_capital")
            self.assertTrue(any("paper_capital 1000.0 → 100.0" in m for _, _, m in k.logs))

    def test_the_book_keeps_both_kinds_of_trade(self):
        from tern.book import latest
        tr = [{"kind": "settle", "ts": i} for i in range(5)] + [{"kind": "fill", "ts": 10 + i} for i in range(60)]
        out = latest(tr)
        self.assertEqual([sum(t["kind"] == x for t in out) for x in ("settle", "fill")], [5, 20])
        self.assertEqual(out, sorted(out, key=lambda t: t["ts"]))
        self.assertEqual(latest([]), [])

    def test_the_wheel_leaves_a_value_being_typed(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.press("9", self.curses.KEY_DOWN, 10, "5")
            at = scr.set_sel
            scr.wheel(10, 5, 1); scr.wheel(10, 5, -1); scr.wheel(10, 5, -1)
            self.assertEqual((scr.set_sel, scr.set_buf), (at, "5"))

    def test_the_settings_page_and_jump_to_their_ends(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            c, n = self.curses, len(scr.set_rows())
            self.press("9", c.KEY_HOME); self.assertEqual(scr.set_sel, 0)
            self.press(c.KEY_NPAGE); self.assertEqual(scr.set_sel, 0)          # (they page what the setting is)
            self.press(c.KEY_END); self.assertEqual(scr.set_sel, n - 1)
            self.press(c.KEY_END); self.assertEqual(scr.set_sel, n - 1)
            self.press(c.KEY_DOWN); self.assertEqual(scr.set_sel, 0)           # one at a time goes round

    def test_the_web_draws_readable_rows_too(self):
        from tern import themes, web
        P = web.WebPal()
        for name in themes.NAMES:
            P.set_theme(name)
            for fg in P.t["rows"] + [P.t["key"]]:
                got = (P.on(fg, P.t["fill"]) & 0x1FF) - 1
                if got > 15:
                    self.assertGreaterEqual(themes.contrast(got, P.t["fill"]), 4, (name, fg))


class Limits(Rig, unittest.TestCase):
    """Most at risk, largest order and daily loss limit are a variant's: paper and live both keep to them."""

    T2 = "KXBTCD-H1-T1"

    def step(self, k, ev, slot=25, n=1, size=1000.0, price=0.80):
        import time
        now = time.time()
        ms = [{"ticker": f"{ev}-T{i}", "floor_strike": 100.0 + i, "yes_bid_dollars": f"{price - .01:.2f}", "yes_ask_dollars": f"{price:.2f}",
               "yes_bid_size_fp": str(size), "yes_ask_size_fp": str(size)} for i in range(n)]
        k.shadow.step(ev, now + slot * 60 - 1, ms, k.cfg.strategy, now, paper=k.cfg.paper_capital)
        return [r for r in k.shadow.rows if r["event"] == ev]

    def mine(self, rows, v="base-2"):
        return [r for r in rows if r["v"] == v]

    def test_paper_cuts_a_buy_to_the_largest_order(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, contracts=100, max_hour_cost=2000, paper_capital=100000, max_order=40, max_risk=10000)
            [r] = self.mine(self.step(k, "KXBTCD-H1"))
            self.assertEqual(r["n"], 50)                                        # $40 at 80¢
            self.assertEqual(self.mine(k.shadow.rows, "base")[0]["n"], 5)       # the built-in ones as before

    def test_paper_stops_at_most_at_risk_over_all_coins_until_something_settles(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, contracts=50, max_hour_cost=2000, paper_capital=100000, max_order=1000, max_risk=100,
                     series=["KXBTCD", "KXETHD"])
            self.assertEqual([r["n"] for r in self.mine(self.step(k, "KXBTCD-H1", n=2))], [50, 50])   # $40 + $40
            self.assertEqual([r["n"] for r in self.mine(self.step(k, "KXETHD-H1", n=2))], [25])       # $20 left: 25, then none
            k.shadow.settle({r["ticker"]: True for r in k.shadow.rows if r["event"] == "KXBTCD-H1"})
            self.assertEqual([r["n"] for r in self.mine(self.step(k, "KXETHD-H2", n=1))], [50])

    def test_paper_stops_for_the_day_at_its_loss_limit(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, contracts=50, max_hour_cost=2000, paper_capital=100000, max_order=1000, max_risk=10000, day_loss=30)
            now = time.time()
            self.assertEqual(k.shadow.watch({}, now), [])                       # the day begins here
            [r] = self.mine(self.step(k, "KXBTCD-H1"))
            self.assertEqual(k.shadow.watch({r["ticker"]: 0.60}, now), [])      # $10 down, with the fee
            got = k.shadow.watch({r["ticker"]: 0.20}, now)                      # $30 and more
            self.assertEqual([(n, lim) for n, _, lim in got], [("base-2", 30)])
            self.assertEqual(k.shadow.watch({r["ticker"]: 0.20}, now), [])      # said once
            self.assertEqual(self.mine(self.step(k, "KXBTCD-H2")), [])          # nothing more today, even if it comes back
            self.assertTrue(self.mine(k.shadow.rows, "base"))                   # the built-in ones go on
            k.shadow.save()
            k2 = self.desk(d)                                                   # a restart remembers
            self.assertTrue(k2.shadow.stopped("base-2", now))
            self.assertFalse(k2.shadow.stopped("base-2", now + 86400))
            k2.shadow.watch({}, now + 86400)                                    # tomorrow it starts again
            self.assertFalse(k2.shadow.days["base-2"][2])
            k2._shadow()

    def test_the_engine_says_so_and_the_screen_shows_it(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.new(k, name="mine", day_loss=5)
            k._shadow()
            k.shadow._count({"v": "mine", "event": "E1", "pnl": -6.0, "n": 1, "fee": 0, "close": 1.0, "ticker": "E1-T1", "side": "yes",
                             "cost": 0.8, "ts": 1.0})
            k._shadow()
            self.assertTrue(any("paper: mine is down $6.00 today, past its daily loss limit $5.00" in m for _, _, m in k.logs))
            self.sync(); scr.var_sel = "mine"; self.press("5")
            self.assertIn("past its daily loss limit: no new entries until midnight", self.text())

    def old(self, d, **rule):
        """variants.json as the engine before limits wrote it."""
        import json
        with open(os.path.join(d, "variants.json"), "w") as fh:
            json.dump({"variants": {"mine": {"rule": {"band_lo": 0.75, "band_hi": 0.95, "minutes": [25], "max_spread": 0.02, "series": ["KXBTCD"],
                                                      "cap": None, "max_vol": None, "contracts": 100.0, "max_hour_cost": 2000.0,
                                                      "paper_capital": 100000.0, **rule}, "title": "mine", "from": "base", "made": 1,
                                             "deleted": False, "was": ""}}, "off": []}, fh)

    def test_one_from_before_keeps_its_paper_and_live_keeps_to_the_desks_limits(self):
        import json
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            c = config.Config(); c.live.capital_usd, c.live.max_order_usd, c.live.max_daily_loss = 90.0, 20.0, 15.0
            config.save(c, d); self.old(d)
            for _ in range(2):                                                  # (the second start reads what the first saved)
                k = self.desk(d)
                self.assertFalse(k.shadow.own.paper_limits("mine"))
                self.assertEqual(k._limits("mine"), (90, 20, 15))
                self.assertEqual(k._limits("base"), (90, 20, 15))
            with open(os.path.join(d, "variants.json")) as fh:
                v = json.load(fh)["variants"]["mine"]
            self.assertEqual((v["paper_limits"], v["rule"]["max_risk"], v["rule"]["day_loss"]), (False, 90, 15))
            c.live.capital_usd = 5.0; config.save(c, d)
            self.assertEqual(self.desk(d)._limits("mine"), (90, 20, 15))        # its own from then on
            self.assertEqual(self.mine(self.step(k, "KXBTCD-H1"), "mine")[0]["n"], 100)   # $80: over both, on paper as before
            k.shadow.watch({}, 1e9); k.shadow._count({"v": "mine", "event": "E", "pnl": -500.0, "n": 1, "fee": 0, "close": 1.0})
            self.assertEqual(k.shadow.watch({}, 1e9), [])
            self.new(k, "mine", "fresh", cap=2)                                 # a new one from it keeps to them
            self.assertTrue(k.shadow.own.paper_limits("fresh"))
            self.assertTrue(self.desk(d).shadow.own.paper_limits("fresh"))      # after a restart too
            self.assertIn("risk $90 · order $20 · day -$15", k.status_desc("fresh") if hasattr(k, "status_desc") else
                          __import__("tern.shadow", fromlist=["x"]).desc_of("fresh", k.shadow.own))

    def test_live_keeps_to_the_variant_it_trades(self):
        got = []
        class Api:
            def order(self, t, side, n, price, cid):
                got.append((n, price))
                return {"order_id": "o", "fill_count": str(n), "average_fill_price": str(price), "average_fee_paid": "0"}
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, name="mine", contracts=40, max_hour_cost=2000, max_order=20, max_risk=30, band_lo=0.55, max_spread=0.05)
            k.api, k.mode = Api(), "live"
            k.cfg.live.enabled, k.cfg.live.variant, k.cfg.live.auto_needs_gate = True, "mine", False
            k._auto()
            self.assertTrue(got)
            n, price = got[0]
            unit = k.live.at_risk() / n
            self.assertEqual(n, int(20 / unit + 1e-9))                          # cut to $20, as paper cuts it
            from tern.kalshi import KalshiError
            with self.assertRaisesRegex(KalshiError, "most at risk"):           # and the last check before any order
                k._exec(k.live, "KXBTCD-E1-T9", 20, 0.8, 100, "auto")
            with self.assertRaisesRegex(KalshiError, "largest order"):
                k._exec(k.live, "KXBTCD-E1-T9", 30, 0.8, 100, "auto")

    def test_a_built_in_one_can_have_its_own(self):
        import json
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            c = config.Config(); c.live.limits = {"multi": {"max_risk": 300, "day_loss": 500}, "base": {"day_loss": 200, "max_order": "x"}, "late": 5}
            config.save(c, d)
            k = self.desk(d)
            self.assertEqual([k._limits(n) for n in ("multi", "base", "late", "cap1")],
                             [(300, 25, 500), (100, 25, 200), (100, 25, 30), (100, 25, 30)])   # what isn't a number is not taken
            rules = {v["name"]: v["rule"] for v in self.status(k)["variants"]}
            self.assertEqual((rules["multi"]["max_risk"], rules["base"]["day_loss"], rules["cap1"]["day_loss"]), (300, 200, 30))
            self.new(k, "multi", "mine", cap=2)
            self.assertEqual(k._limits("mine"), (300, 25, 500))                # a copy starts from them
            with self.assertRaises(TypeError):
                config.Config.from_dict({"live": {"limits": [1]}})
            self.assertEqual(config.load(d).live.limits["multi"]["max_risk"], 300)   # and they are kept through a save

    def test_lives_loss_limit_is_the_variants(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.new(k, name="mine", day_loss=5)
            k.cfg.live.enabled, k.cfg.live.variant, k.mode = True, "mine", "live"
            k._daily_loss()
            k.live.realized -= 4.0; k._daily_loss(); self.assertTrue(k.cfg.live.enabled)
            k.live.realized -= 2.0; k._daily_loss(); self.assertFalse(k.cfg.live.enabled)   # $30 is the built-in ones'
            self.assertTrue(any("KILL" in m and "limit $5.00" in m for _, _, m in k.logs))

    def test_the_live_prompt_says_the_variants_own(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.new(k, name="mine", contracts=12, max_order=33, max_risk=77, day_loss=44); self.sync()
            scr.variant = "mine"; self.sync(); self.press("7")
            for want in ("$77 at risk", "$33 per order", "-$44 a day"):
                self.assertIn(want, self.text())
            self.press("L")
            for want in ("12 contracts a strike", "$33.00 an order", "$77.00 at risk", "-$44.00 in a day"):
                self.assertIn(want, self.text())

    def test_every_setting_is_explained(self):
        from tern import about, config, variants
        from tern.dash import SCREEN_SETTINGS
        rule = {"band_lo": 0.75, "band_hi": 0.95, "minutes": [25, 20], "max_spread": 0.02, "series": ["KXBTCD", "KXETHD"], "cap": None, "max_vol": None,
                "contracts": 25.0, "max_hour_cost": 200.0, "max_risk": 100.0, "max_order": 20.0, "day_loss": 30.0, "paper_capital": 1000.0}
        for r in variants.RULE_SETTINGS:
            for more in ({}, {"cap": 1, "max_vol": 5.0, "contracts": 1.0, "minutes": [5], "band_lo": 0.5, "band_hi": 0.51, "max_order": 1.0},
                         {"band_lo": 0.98, "band_hi": 0.99, "contracts": 100.0, "minutes": [75, 30]}):
                for c in ({}, {"vol": {"KXBTCD": 7.2, "KXETHD": None}, "price": 60000.0, "limits": False, "name": "base"}):
                    out = about.rule(r.name, {**rule, **more}, c)
                    self.assertGreater(sum(len(t) for _, t in out), 150, r.name)
                    self.assertTrue(all(s in ("text", "head", "note", "fact") for s, _ in out))
                    self.assertFalse([t for _, t in out if "edge" in t.lower().split()])
        for r in config.SETTINGS + SCREEN_SETTINGS:
            self.assertIn(r.key, about.DESK)
        text = " ".join(t for _, t in about.rule("cap", rule, {}))
        self.assertIn("bought 2 times: 50 contracts for $42.50", text)
        text = " ".join(t for _, t in about.rule("max_vol", {**rule, "max_vol": 5.0}, {"vol": {"KXBTCD": 7.2, "KXETHD": None}, "price": 60000.0}))
        self.assertIn("BTC 7.2 (skipped)", text); self.assertIn("ETH: a gap", text); self.assertIn("$60 in a minute", text)

    def test_the_editor_explains_and_says_who_uses_what(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 32, 148)
            self.press("5"); scr.var_sel = "cap1"; self.press("e")
            for n, r in enumerate(scr.set_rows()):
                scr.set_sel = n; scr.draw()
                text = self.text()
                self.assertIn("WHO USES WHAT", text, r.name); self.assertIn("CHANGES", text)
                for row in scr.s.ch[1:30]:
                    self.assertIn("".join(row)[-1], "│")
            scr.set_sel = [r.name for r in scr.set_rows()].index("cap"); scr.draw()
            self.assertRegex(self.text(), r"▸ 1 +cap1"); self.assertRegex(self.text(), r"off +base")
            self.press(self.curses.KEY_RIGHT); self.assertRegex(self.text(), r"▸ 2 +none yet")
            scr.set_sel = [r.name for r in scr.set_rows()].index("day_loss"); scr.draw()
            self.assertIn("live keeps to this one", " ".join(self.text().replace("│", " ").split()))

    def test_the_right_side_scrolls(self):
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d, 30, 100)
            c = self.curses
            self.press("5"); self.press("e")
            box = lambda: self.text().splitlines()[:-1]                        # (without the status line and its clock)
            first = box()
            self.assertRegex("".join(scr.s.ch[28]), r" 1-\d+ of \d+ ")
            self.press(c.KEY_NPAGE); self.assertNotEqual(box(), first); self.assertEqual((scr.set_sel, scr.overlay), (0, "edit"))
            self.press(c.KEY_DOWN); self.press(c.KEY_UP); self.assertEqual(box(), first)   # another setting starts at its top
            x = scr.rects["about"][1] + 3
            scr.wheel(10, x, 1); scr.draw(); self.assertNotEqual(box(), first)

    def test_a_hole_in_the_last_hour_is_filled_once(self):
        import collections, time
        from unittest import mock
        from tern import engine, shadow
        f = engine.SpotFeed()
        now = time.time(); m0 = now // 60 * 60
        have = [[m0 - 60 * i, 100.0 + i] for i in range(70, -1, -1) if not 3 <= i <= 8]
        f.minute["BTC-USD"] = collections.deque(have, maxlen=2880); f.products.add("BTC-USD")
        self.assertTrue(f.holes("BTC-USD", now)); self.assertIsNone(shadow.vol(list(f.minute["BTC-USD"]), now))
        with mock.patch.object(engine, "history", lambda p, g: [[m0 - 60 * i, 5.0 if i == 0 else 200.0] for i in range(80, -1, -1)]):
            f.fill("BTC-USD")
        rows = list(f.minute["BTC-USD"])
        self.assertEqual([r[0] for r in rows], sorted({r[0] for r in rows}))
        self.assertFalse(f.holes("BTC-USD", now)); self.assertIsNotNone(shadow.vol(rows, now))
        self.assertEqual(dict(map(tuple, rows))[m0 - 120], 102.0)               # what the feed saw stays
        self.assertEqual(rows[-1], [m0, 100.0])                                 # and the running minute is its own


class GoneCoins(Rig, unittest.TestCase):
    """BNB and HYPE: not traded since 2026-11-24. What was bought settles, and records keep their names."""

    def test_they_are_nowhere_to_pick(self):
        from tern import config
        with tempfile.TemporaryDirectory() as d:
            k, scr = self.setup(d)
            self.assertEqual([config.coin(s) for s in config.SERIES], ["BTC", "ETH", "SOL", "XRP"])
            seen = set()
            for _ in range(8):
                self.press("1", "C"); seen.add(scr.series)
            self.assertEqual(seen, set(config.SERIES))
            self.press("5", "e"); self.assertNotIn("HYPE", self.text()); self.assertNotIn("BNB", self.text())
            self.assertIn("must be chosen", k._command({"op": "variant_new", "from": "base", "rule": {"series": ["KXHYPED"]}}))
            self.assertEqual(config.Config.from_dict({"strategy": {"series": ["KXHYPED", "KXETHD"]}}).strategy.series, ["KXETHD"])
            self.assertEqual(config.Config.from_dict({"strategy": {"series": ["KXHYPED"]}}).strategy.series, ["KXBTCD"])
            self.assertEqual(config.coin("KXHYPED"), "HYPE")                    # in what is past

    def test_a_variant_loses_them_and_a_deleted_one_still_says(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            self.new(self.desk(d), name="mine"); self.new(self.desk(d), name="gone"); self.new(self.desk(d), name="only")
            path = os.path.join(d, "variants.json")
            with open(path) as fh:
                j = json.load(fh)
            j["variants"]["mine"]["rule"]["series"] = ["KXBTCD", "KXBNBD", "KXHYPED"]
            j["variants"]["gone"]["rule"]["series"] = ["KXHYPED"]; j["variants"]["gone"]["deleted"] = True
            j["variants"]["only"]["rule"]["series"] = ["KXHYPED"]
            with open(path, "w") as fh:
                json.dump(j, fh)
            k = self.desk(d)
            own = k.shadow.own
            self.assertEqual((own.rule("mine")["series"], own.rule("gone")["series"], own.rule("only")["series"]),
                             (["KXBTCD"], ["KXHYPED"], ["KXHYPED"]))
            self.assertTrue(any("mine no longer trades BNB HYPE" in m for _, _, m in k.logs))
            k2 = self.desk(d)                                                   # saved: said once
            self.assertEqual((k2.shadow.own.trimmed, k2.shadow.own.rule("mine")["series"]), ({}, ["KXBTCD"]))
            k2._shadow()                                                        # one with nothing left to trade just buys nothing
            self.assertNotIn("only", {r["v"] for r in k2.shadow.rows})

    def test_open_entries_settle_though_the_coin_is_not_fetched(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            k.shadow.rows.append({"v": "multi", "ts": 1.0, "event": "KXHYPED-E9", "close": time.time() + 0.2, "ticker": "KXHYPED-E9-T1",
                                  "side": "yes", "n": 1, "cost": 0.8, "fee": 0.01, "slot": 25})
            self.assertNotIn("KXHYPED", k._wanted())
            k._refresh_event = k._commands = k._auto = k._settle = k._live_ready = lambda *a: None
            real, k._shadow = k._shadow, lambda: None
            k.step(); self.assertNotIn("KXHYPED-E9", k.closed)                  # still open
            k.shadow.rows[-1]["close"] = time.time() - 100
            k.step(); self.assertIn("KXHYPED-E9", k.closed)
            k.pub.event_markets = lambda ev: [{"ticker": "KXHYPED-E9-T1", "result": "yes", "status": "finalized", "floor_strike": 1.0}]
            real()
            self.assertFalse([r for r in k.shadow.rows if r["event"] == "KXHYPED-E9"]); self.assertNotIn("KXHYPED-E9", k.closed)

    def test_records_from_here_are_another_period_once(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.desk(d)
            self.assertEqual(k.slog.sv, 1)                                      # nothing recorded yet: nothing to split
            k.shadow._count({"v": "multi", "event": "E1", "pnl": 1.0, "n": 1, "fee": 0, "close": 1.0})
            k.shadow._close({"v": "multi", "ts": 1.0, "event": "E2", "close": 2.0, "ticker": "E2-T1", "side": "yes", "n": 1, "cost": 0.8,
                             "fee": 0.0, "slot": 25}, 0.2)
            self.assertEqual([self.desk(d).slog.sv for _ in range(2)], [2, 2])
            from tern import reports
            self.assertIn("BNB and HYPE are not traded any more", "\n".join(reports.periods(d)))


class Themes(unittest.TestCase):
    """Every theme has every role, a color for every box, and text that can be read where it is drawn."""

    def test_every_theme_has_every_role(self):
        from tern import dash, themes
        roles = set(themes.THEMES["classic"])
        for name, t in themes.THEMES.items():
            self.assertEqual(set(t), roles, name)
            self.assertEqual(list(t["border"]), dash.BOXES, name)
            self.assertEqual(set(t["grads"]), {"spot", "yes", "eq_up", "eq_dn"}, name)
        self.assertEqual(set(dash.FEW_BORDER), set(dash.BOXES))

    def test_tabs_can_be_read_and_told_apart(self):
        from tern import themes
        for name, t in themes.THEMES.items():
            colors = [c for c in t["border"].values() if c > 15]
            self.assertTrue(all(themes.contrast(c, themes.BACK) >= 4 for c in colors), name)   # dark text on the bright tab
            self.assertTrue(all(themes.distance(c, t["tab"]) >= 26 for c in colors), name)      # which is not like the others
            self.assertGreaterEqual(themes.contrast(t["tab"], themes.BACK), 1.9, name)          # and they are not black
            self.assertGreaterEqual(themes.contrast(t["tab_text"], t["tab"]), 4, name)
            self.assertGreaterEqual(themes.distance(t["fill"], themes.BACK), 8, name)
            for a, b in zip(colors, colors[1:]):
                self.assertTrue(a != b or len(set(colors)) == 1, name)      # nor the one next to it, unless all are one

    def test_color_pairs_never_pass_255(self):
        # curses.color_pair(256) is pair 0: past 255 a pair is drawn as another one, or as no color at all
        from unittest import mock
        from tern import dash, themes
        made = {}
        with mock.patch.multiple(dash.curses, start_color=lambda: None, use_default_colors=lambda: None, create=True, COLORS=256,
                                 COLOR_PAIRS=65536, init_pair=lambda n, f, b: made.__setitem__(n, (f, b)), color_pair=lambda n: n << 8):
            P = dash.Pal()
            for i, name in enumerate(themes.NAMES):
                P.set_theme(name); P.new_frame("variants")
                for fg in range(16, 256):                                      # more than there are
                    P(fg); P.on(fg, 232)
                self.assertTrue(P.full)
                P.new_frame("variants")                                        # it starts over, with the tabs' own
                a = P.on(themes.THEMES[name]["tab_text"], themes.THEMES[name]["tab"])
                self.assertEqual(made[(a >> 8) & 0xFF], (themes.THEMES[name]["tab_text"], themes.THEMES[name]["tab"]), name)
            self.assertLessEqual(max(made), 255)
            P.new_frame("variants"); first = P(200)
            P.new_frame("variants"); self.assertEqual(P(200), first)           # the same box keeps its pairs

    def test_readable_only_moves_what_needs_it(self):
        from tern import themes
        self.assertEqual(themes.readable(231, 17), 231)
        self.assertEqual(themes.readable(7, 237), 7)                           # the terminal's own colors: left alone
        for name, t in themes.THEMES.items():
            for c in t["rows"] + [t["key"], t["variant"], t["grey"]]:
                if c > 15:
                    self.assertGreaterEqual(themes.contrast(themes.readable(c, t["fill"]), t["fill"]), 4.0, (name, c))

    def test_smooth_keeps_the_steps_and_their_order(self):
        from tern import themes
        for name, t in themes.THEMES.items():
            for ramp in [*t["grads"].values(), t["rows"]]:
                out = themes.smooth(tuple(ramp))
                it = iter(out)
                self.assertTrue(all(any(c == o for o in it) for c in ramp), name)   # the theme's own, in order
                self.assertLess(len(out), 4 * len(ramp), name)
            self.assertGreaterEqual(len(themes.visible(tuple(t["grads"]["spot"]))), 3, name)
        self.assertGreater(len(themes.smooth((17, 231))), 2)


class Devices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "app-devices.json")
        self.dev = devices.Devices(path=self.path)

    def _phone(self):
        """Two openssl keys: (everyday b64, live b64, sign(text, which))."""
        d = self.tmp.name
        pubs = {}
        for which in ("everyday", "live"):
            subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                            "-out", which + ".pem"], cwd=d, check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", which + ".pem", "-pubout", "-outform", "DER",
                            "-out", which + ".der"], cwd=d, check=True, capture_output=True)
            pubs[which] = base64.b64encode(pathlib.Path(d, which + ".der").read_bytes()).decode()

        def signer(text, which):
            pathlib.Path(d, "msg").write_bytes(text.encode())
            subprocess.run(["openssl", "dgst", "-sha256", "-sign", which + ".pem", "-out", "sig.der", "msg"],
                           cwd=d, check=True, capture_output=True)
            return base64.b64encode(pathlib.Path(d, "sig.der").read_bytes()).decode()
        return pubs["everyday"], pubs["live"], signer

    def _paired(self):
        e, l, signer = self._phone()
        return self.dev.add("pixel", e, l), signer

    def _send(self, dev_id, signer, n, cmd, which="everyday", ts=1000.0, **kw):
        payload = json.dumps({"d": dev_id, "n": n, "ts": ts, "cmd": cmd})
        return self.dev.check(payload, signer(payload, which), which, 1000.0, **kw)

    def test_words_match_the_formula(self):
        a, b = base64.b64encode(b"everyday"), base64.b64encode(b"live")
        h = hashlib.sha256(b"everyday" + b"live").hexdigest()[:12].upper()
        want = f"{h[:4]} {h[4:8]} {h[8:]}"
        self.assertEqual(devices.words(a.decode(), b.decode()), want)
        self.assertEqual(len(want), 14)

    def test_add_remove_and_the_file_is_private(self):
        e, l, _ = self._phone()
        ids = [self.dev.add(f"p{i}", e, l) for i in range(3)]
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        rows = self.dev.all()
        self.assertEqual([r["id"] for r in rows], ids)
        self.assertEqual(set(rows[0]), {"id", "name", "paired", "used"})
        self.assertNotIn(e, json.dumps(rows))
        with self.assertRaises(devices.Refused):
            self.dev.add("fourth", e, l)
        self.assertTrue(self.dev.remove(ids[1]))
        with self.assertRaises(devices.Refused):
            self.dev.add("bad", "AAAA", l)
        self.assertFalse(self.dev.remove(ids[1]))
        self.assertEqual(len(self.dev.secret()), 32)
        self.assertEqual(self.dev.secret(), devices.Devices(path=self.path).secret())

    def test_check_accepts_then_refuses_a_replay(self):
        i, signer = self._paired()
        who, cmd = self._send(i, signer, 5, {"op": "live", "on": False})
        self.assertEqual(cmd, {"op": "live", "on": False})
        self.assertEqual(who["id"], i)
        with self.assertRaisesRegex(devices.Refused, "sent twice"):
            self._send(i, signer, 5, {"op": "live", "on": False})
        self._send(i, signer, 6, {"op": "live", "on": False})
        again = devices.Devices(path=self.path)          # the counter is kept in the file
        payload = json.dumps({"d": i, "n": 6, "ts": 1000.0, "cmd": {"op": "x"}})
        with self.assertRaisesRegex(devices.Refused, "sent twice"):
            again.check(payload, signer(payload, "everyday"), "everyday", 1000.0)

    def test_check_refusals(self):
        i, signer = self._paired()
        cmd = {"op": "live", "on": False}

        def refused(text, regex, sig=None, key="everyday", now=1000.0):
            with self.assertRaisesRegex(devices.Refused, regex):
                self.dev.check(text, sig or signer(text, "everyday"), key, now)
        for bad in ("nope", "[1]", json.dumps({"d": i, "n": 1, "ts": 1000, "cmd": []}),
                    json.dumps({"d": i, "n": "1", "ts": 1000, "cmd": cmd}),
                    json.dumps({"d": 7, "n": 1, "ts": 1000, "cmd": cmd}),
                    json.dumps({"d": i, "n": True, "ts": 1000, "cmd": cmd}),
                    json.dumps({"d": i, "n": 1, "ts": None, "cmd": cmd})):
            refused(bad, "")
        good = json.dumps({"d": i, "n": 1, "ts": 1000, "cmd": cmd})
        other = json.dumps({"d": "ffffffffffff", "n": 1, "ts": 1000, "cmd": cmd})
        refused(other, "isn't paired: pair it again from the desk")
        refused(good, "not signed by this phone's key", key="other")
        refused(good, "not signed by this phone's key", sig=signer(good, "live"))
        refused(good, "not signed by this phone's key", sig="!!!")
        late = json.dumps({"d": i, "n": 1, "ts": 960, "cmd": cmd})
        refused(late, r"clock is 40 s off")
        self.dev.check(good, signer(good, "everyday"), "everyday", 1029.0)
        refused(good, "sent twice")

    def test_live_needs_the_live_key(self):
        i, signer = self._paired()
        cfg = cfgmod.Config()
        on = {"op": "live", "on": True, "variant": "base"}
        with self.assertRaisesRegex(devices.Refused, "fingerprint"):
            self._send(i, signer, 1, on, cfg=cfg)
        self._send(i, signer, 2, on, which="live", cfg=cfg)
        self._send(i, signer, 3, {"op": "live", "on": False}, cfg=cfg)
        low = {"op": "set", "key": "gate.min_days", "value": cfg.gate.min_days - 1}
        with self.assertRaisesRegex(devices.Refused, "fingerprint"):
            self._send(i, signer, 4, low, cfg=cfg, armed=True)
        self._send(i, signer, 5, low, cfg=cfg, armed=False)
        up = dict(low, value=cfg.gate.min_days + 1)
        self._send(i, signer, 6, up, cfg=cfg, armed=True)
        self.assertTrue(devices.needs_live(low, cfg, True))
        self.assertFalse(devices.needs_live(low, cfg, False))
        self.assertFalse(devices.needs_live({"op": "set", "key": "nope", "value": 1}, cfg, True))

    def test_variant_on_of_the_live_variant_needs_the_live_key(self):
        i, signer = self._paired()
        cfg = cfgmod.Config()
        cfg.live.variant = "base"
        on = {"op": "variant_on", "variant": "base", "on": True}
        with self.assertRaisesRegex(devices.Refused, "fingerprint"):
            self._send(i, signer, 1, on, cfg=cfg, armed=True)
        self._send(i, signer, 2, on, which="live", cfg=cfg, armed=True)
        self._send(i, signer, 3, on, cfg=cfg, armed=False)
        self._send(i, signer, 4, dict(on, variant="other"), cfg=cfg, armed=True)
        self._send(i, signer, 5, dict(on, on=False), cfg=cfg, armed=True)
        with self.assertRaisesRegex(devices.Refused, "malformed"):
            self._send(i, signer, 6, dict(on, on="false"), cfg=cfg, armed=True)

    def test_live_on_must_be_a_real_bool(self):
        i, signer = self._paired()
        cfg = cfgmod.Config()
        for n, on in enumerate((1, "yes", [1], 1.0), 1):
            with self.assertRaises(devices.Refused):
                self._send(i, signer, n, {"op": "live", "on": on, "variant": "base"}, cfg=cfg)
        self.assertTrue(devices.needs_live({"op": "live", "on": 1}, cfg, False))

    def test_a_removal_during_a_check_sticks(self):
        i, signer = self._paired()
        a, b = self.dev, devices.Devices(path=self.path)
        real = sign.p256_verify

        def verify_then_remove(*args):
            ok = real(*args)
            self.assertTrue(b.remove(i))          # the desk removes the phone while a check is mid-way
            return ok
        with mock.patch.object(sign, "p256_verify", verify_then_remove):
            with self.assertRaisesRegex(devices.Refused, "isn't paired"):
                self._send(i, signer, 1, {"op": "live", "on": False})
        self.assertEqual(devices.Devices(path=self.path).all(), [])

    def test_session_tokens(self):
        i, _ = self._paired()
        tok = self.dev.session_token(i, 1000.0)
        self.assertEqual(self.dev.session_device(tok, 1001.0), i)
        self.assertIsNone(self.dev.session_device(tok, 1000.0 + webauth.TTL + 1))
        self.assertIsNone(self.dev.session_device(tok[:-1] + ("0" if tok[-1] != "0" else "1"), 1001.0))
        self.assertIsNone(self.dev.session_device(None, 1001.0))
        self.assertIsNone(self.dev.session_device("junk", 1001.0))
        self.dev.remove(i)
        self.assertIsNone(self.dev.session_device(tok, 1001.0))

    # --- pairing on the desk
    def _live(self):
        d = os.path.join(self.tmp.name, "live"); os.makedirs(d, exist_ok=True)
        return d

    def _offer(self, live, code, now=100.0, name="pixel", sig_for=None):
        e, l, signer = self._phone()
        sig = signer(f"pair\n{sig_for or code}\n{name}\n{e}\n{l}", "everyday")
        return devices.offer(live, code, name, e, l, sig, now), (e, l)

    def test_pairing_flow(self):
        live = self._live()
        code = devices.new_code(live, 100.0)
        self.assertRegex(code, r"^\d{6}$")
        self.assertIsNone(devices.offered(live, 100.0))
        oid, (e, l) = self._offer(live, code)
        o = devices.offered(live, 101.0)
        self.assertEqual((o["id"], o["name"], o["words"]), (oid, "pixel", devices.words(e, l)))
        self.assertEqual(devices.offer_state(live, oid, 101.0), {"state": "waiting"})
        self.assertIn("paired", devices.answer(live, oid, True, self.dev, now=101.0))
        st = devices.offer_state(live, oid, 102.0)
        self.assertEqual(st["state"], "paired")
        self.assertIn(st["device"], [p["id"] for p in self.dev.all()])
        self.assertIsNone(devices.offered(live, 102.0))
        with self.assertRaisesRegex(devices.Refused, "no pairing code"):   # the code is used up
            self._offer(live, code)
        self.assertEqual(devices.offer_state(live, "nope", 102.0), {"state": "unknown"})
        c2 = devices.new_code(live, 200.0)
        with self.assertRaises(devices.Refused):
            self._offer(live, c2, now=200.0 + 301)

    def test_pairing_refusals(self):
        live = self._live()
        with self.assertRaisesRegex(devices.Refused, "no pairing code on the desk: open settings, APP, pair a phone"):
            self._offer(live, "123456")
        code = devices.new_code(live, 100.0)
        with self.assertRaisesRegex(devices.Refused, "no pairing code"):          # expired
            self._offer(live, code, now=100.0 + 301)
        code = devices.new_code(live, 100.0)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            with self.assertRaises(devices.Refused):
                self._offer(live, wrong)
        with self.assertRaisesRegex(devices.Refused, "too many wrong codes: wait 10 min"):
            self._offer(live, code)                                                 # even the right one now
        code = devices.new_code(live, 2000.0)                                       # past the lockout
        with self.assertRaisesRegex(devices.Refused, "bad signature|signature"):
            self._offer(live, code, now=2000.0, sig_for="999999")
        oid, _ = self._offer(live, code, now=2000.0, name="a" * 40 + "\x07")
        self.assertEqual(devices.offered(live, 2001.0)["name"], "a" * 24)
        with self.assertRaisesRegex(devices.Refused, "another phone is waiting to be paired"):
            self._offer(live, code, now=2001.0)
        self.assertIn("no", devices.answer(live, oid, False, self.dev, now=2001.0).lower())
        self.assertEqual(devices.offer_state(live, oid, 2002.0)["state"], "declined")
        self.assertEqual(self.dev.all(), [])
        code = devices.new_code(live, 3000.0)
        oid, _ = self._offer(live, code, now=3000.0)
        self.assertEqual(devices.offer_state(live, oid, 3000.0 + 301)["state"], "expired")
        self.assertIsNone(devices.offered(live, 3000.0 + 301))
        self.assertEqual(oct(os.stat(os.path.join(live, "app-pair.json")).st_mode & 0o777), "0o600")

    def test_answer_after_expiry_adds_nothing(self):
        live = self._live()
        code = devices.new_code(live, 100.0)
        oid, _ = self._offer(live, code, now=100.0)
        self.assertIn("ran out", devices.answer(live, oid, True, self.dev, now=100.0 + 301))
        self.assertEqual(self.dev.all(), [])
        self.assertEqual(devices.offer_state(live, oid, 100.0 + 302)["state"], "expired")
        self.assertEqual(devices.offer_state(live, oid, 100.0 + 303), {"state": "expired"})

    def test_switches(self):
        from tern import __main__ as cli
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            for enabled, app, serves in ((False, False, False), (False, True, True), (True, False, True)):
                cfg = cfgmod.Config(); cfg.web.enabled, cfg.web.app = enabled, app
                cfgmod.save(cfg, d)
                with mock.patch("tern.web.serve") as serve, mock.patch.object(cfgmod, "RUNTIME", d):
                    self.assertEqual(cli.main(["web", "--config"]), 0)
                self.assertEqual(serve.called, serves, (enabled, app))
        calls = []
        with mock.patch.object(webctl, "systemctl", lambda *a: (calls.append(a) or (0, ""))):
            w = cfgmod.Web(); w.enabled, w.app = False, True
            self.assertIn("restarting", webctl.apply(w))
            w.app = False
            self.assertEqual(webctl.apply(w), "web screen off")
        self.assertEqual([c[0] for c in calls], ["restart", "stop"])


if __name__ == "__main__":
    unittest.main()
