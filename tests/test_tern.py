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
            k.auto = [{"ts": 0, "ticker": f"KXBTCD-H{h}-T1", "side": "yes", "n": 1, "cost": 0.9, "fee": 0, "slot": 10, "pnl": 0.1}
                      for h in range(12) for _ in range(3)]
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


class Shadows(unittest.TestCase):
    def m(self, t, bid, ask, k):
        return {"ticker": t, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
                "yes_bid_size_fp": "50", "yes_ask_size_fp": "40", "floor_strike": k, "status": "active"}

    def test_variants_stop_and_settle(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy()
            ms = [self.m("E-T1", 0.92, 0.93, 1), self.m("E-T2", 0.80, 0.81, 2), self.m("E-T3", 0.10, 0.11, 3)]
            sh.step("E", 1000.0, ms, S, 1000.0 - 25 * 60 + 1)
            by = lambda v: sorted(r["ticker"] for r in sh.rows if r["v"] == v)
            self.assertEqual(by("base"), ["E-T1", "E-T2", "E-T3"])
            self.assertEqual(by("band90"), ["E-T1"])                       # T3's NO mid is 89.5c: outside 90-95
            self.assertEqual(by("stop20"), ["E-T1", "E-T2", "E-T3"])
            sh.step("E", 1000.0, ms, S, 1000.0 - 25 * 60 + 3)              # same slot: no second entry
            self.assertEqual(len(sh.rows), 7)
            ms[1] = self.m("E-T2", 0.60, 0.62, 2)                          # T2 falls 21c: the stop sells at the 60c bid
            sh.step("E", 1000.0, ms, S, 1000.0 - 22 * 60)
            self.assertEqual(by("stop20"), ["E-T1", "E-T3"])
            stopped = sh.agg["stop20"]["E"]
            self.assertAlmostEqual(stopped[0], 5 * (0.60 - 0.81) - 0.06 - 0.09)   # entry fee 6c, exit fee 9c
            self.assertEqual(sh.settle({"E-T1": True, "E-T2": False, "E-T3": False}), 6)
            self.assertAlmostEqual(sh.agg["band90"]["E"][0], 5 * 0.07 - 0.03)
            self.assertAlmostEqual(sh.agg["base"]["E"][0], 5 * 0.07 - 0.03 - 5 * 0.81 - 0.06 + 5 * 0.10 - 0.04)
            sh.save()
            again = Shadow(d)                                              # the record survives a restart
            self.assertEqual(again.rows, [])
            self.assertAlmostEqual(again.agg["base"]["E"][0], sh.agg["base"]["E"][0])

    def test_arb_needs_more_than_the_fees(self):
        from tern.config import Strategy
        from tern.shadow import Shadow
        with tempfile.TemporaryDirectory() as d:
            sh, S = Shadow(d), Strategy()
            tight = [self.m("E-T1", 0.50, 0.52, 1), self.m("E-T2", 0.53, 0.55, 2)]   # 1c gross, fees eat it
            self.assertEqual(sh.step("E", 9e9, tight, S, 0.0), [])
            wide = [self.m("E-T1", 0.50, 0.52, 1), self.m("E-T2", 0.60, 0.62, 2)]    # 8c gross
            self.assertEqual(len(sh.step("E", 9e9, wide, S, 0.0)), 1)
            self.assertEqual(sh.step("E", 9e9, wide, S, 5.0), [])                    # not counted twice within a minute
            r = sh.rows[0]
            self.assertAlmostEqual(r["cost"], 0.52 + 0.40)
            sh.settle({"E-T1": True, "E-T2": False})                                 # between the strikes: both legs pay
            self.assertAlmostEqual(sh.agg["arb"]["E"][0], 5 * (2 - 0.92) - r["fee"])

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
