import unittest

from agents.common.store import NewsItem, Store
from agents.trading.feeds import BinanceAnnouncementsFeed, CryptoPanicFeed, parse_iso
from agents.trading.loop import Hooks, LoopConfig, run_once


class Halted(Exception):
    pass


class FakeFeed:
    name = "fake"

    def __init__(self, items):
        self.items = items

    def poll(self):
        return list(self.items)


def item(i, published, syms=("BTC",)):
    return NewsItem(id=f"n{i}", source="fake", title=f"headline {i}", published_at=published, seen_at=1000.0, symbols=list(syms))


class TradingLoopTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.calls = []
        self.hooks = Hooks(
            judge=lambda it: {"sentiment": "bullish"},
            compose=lambda f, it: {"score": 0.8},
            check=lambda s, it: {"action": "buy", "size": 1},
            execute=lambda d, it: self.calls.append(it.id) or {"action": d["action"], "status": "shadow"},
            halted=Halted,
        )
        self.cfg = LoopConfig(max_age_s=600)

    def test_dedup_and_stale(self):
        fresh, stale = item(1, 900.0), item(2, 100.0)
        feed = FakeFeed([fresh, stale])
        s = run_once([feed], self.store, self.hooks, self.cfg, clock=lambda: 1000.0)
        self.assertEqual((s.new, s.stale, s.judged, s.executed), (2, 1, 1, 1))
        self.assertEqual(self.calls, ["n1"])
        s = run_once([feed], self.store, self.hooks, self.cfg, clock=lambda: 1000.0)  # same items again
        self.assertEqual((s.new, s.judged), (0, 0))

    def test_symbol_filter(self):
        cfg = LoopConfig(max_age_s=600, symbols={"ETH"})
        s = run_once([FakeFeed([item(1, 900.0, ("BTC",)), item(2, 900.0, ("eth",))])], self.store, self.hooks, cfg, clock=lambda: 1000.0)
        self.assertEqual((s.filtered, s.judged), (1, 1))
        self.assertEqual(self.calls, ["n2"])

    def test_undated_skipped_unless_allowed(self):
        s = run_once([FakeFeed([item(1, None)])], self.store, self.hooks, self.cfg, clock=lambda: 1000.0)
        self.assertEqual(s.stale, 1)
        s = run_once([FakeFeed([item(2, None)])], self.store, self.hooks, LoopConfig(accept_undated=True), clock=lambda: 1000.0)
        self.assertEqual(s.judged, 1)

    def test_hook_error_is_logged_not_fatal(self):
        def boom(f, it):
            raise ValueError("bad features")
        hooks = Hooks(judge=self.hooks.judge, compose=boom, check=self.hooks.check, execute=self.hooks.execute, halted=Halted)
        s = run_once([FakeFeed([item(1, 900.0), item(2, 900.0)])], self.store, hooks, self.cfg, clock=lambda: 1000.0)
        self.assertEqual((s.judged, s.errors, s.executed), (2, 2, 0))
        rows = self.store.db.execute("SELECT error FROM judgments WHERE error IS NOT NULL").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("bad features", rows[0][0])

    def test_halted_propagates(self):
        def kill(s, it):
            raise Halted("daily loss")
        hooks = Hooks(judge=self.hooks.judge, compose=self.hooks.compose, check=kill, execute=self.hooks.execute, halted=Halted)
        with self.assertRaises(Halted):
            run_once([FakeFeed([item(1, 900.0)])], self.store, hooks, self.cfg, clock=lambda: 1000.0)
        self.assertEqual(self.store.db.execute("SELECT action FROM judgments").fetchone()[0], "halted")

    def test_feed_failure_counted(self):
        class Bad:
            name = "bad"

            def poll(self):
                raise ConnectionError("down")
        s = run_once([Bad(), FakeFeed([item(1, 900.0)])], self.store, self.hooks, self.cfg, clock=lambda: 1000.0)
        self.assertEqual((s.errors, s.judged), (1, 1))
        self.assertEqual(self.store.db.execute("SELECT ok FROM polls WHERE source='bad'").fetchone()[0], 0)


class FeedParseTest(unittest.TestCase):
    def test_cryptopanic(self):
        p = {"id": 42, "title": "Big news", "description": "body", "published_at": "2026-09-18T10:00:00Z",
             "instruments": [{"code": "BTC"}, {"code": "SOL"}], "source": {"domain": "coindesk.com"}, "original_url": "https://x"}
        n = CryptoPanicFeed.parse(p, 5.0)
        self.assertEqual(n.id, "cryptopanic:42")
        self.assertEqual(n.symbols, ["BTC", "SOL"])
        self.assertEqual(n.published_at, parse_iso("2026-09-18T10:00:00+00:00"))
        self.assertEqual(n.source, "cryptopanic/coindesk.com")

    def test_binance(self):
        d = {"data": {"catalogs": [{"articles": [{"id": 7, "code": "abc", "title": "Binance Will List Pudgy Penguins (PENGU) With Seed Tag", "releaseDate": 1758190000000}]}]}}
        items = BinanceAnnouncementsFeed.parse(d, 48, 1.0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].symbols, ["PENGU"])
        self.assertEqual(items[0].published_at, 1758190000.0)
        self.assertTrue(items[0].url.endswith("/abc"))

    def test_parse_iso(self):
        self.assertEqual(parse_iso("2026-01-01T00:00:00Z"), 1767225600.0)
        self.assertIsNone(parse_iso("garbage"))
        self.assertIsNone(parse_iso(None))


if __name__ == "__main__":
    unittest.main()
