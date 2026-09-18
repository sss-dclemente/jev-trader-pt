import unittest

from agents.betting import loop
from agents.betting.latency import edge_seconds, first_move, observe, summarize
from agents.betting.matching import match_events, mentions, team_keys
from agents.betting.news_feed import ApiFootballLineups, parse_rss
from agents.betting.odds_feed import OddsApi
from agents.common.store import Event, OddsTick, Store, TeamNews

EV = Event(id="e1", sport="soccer_epl", commence=10000.0, home="Manchester United", away="Newcastle United")
EV2 = Event(id="e2", sport="soccer_epl", commence=10000.0, home="Arsenal", away="Chelsea")


class MatchingTest(unittest.TestCase):
    def test_keys_and_aliases(self):
        self.assertIn("man utd", team_keys("Manchester United"))
        self.assertTrue(mentions("Injury blow for Man Utd ahead of weekend", "Manchester United"))
        self.assertFalse(mentions("Newcastle confirm Isak absence", "Manchester United"))
        self.assertTrue(mentions("Newcastle confirm Isak absence", "Newcastle United"))
        self.assertFalse(mentions("United Airlines strike", "Manchester United"))

    def test_match_events(self):
        self.assertEqual(match_events("Spurs vs Chelsea preview", [EV, EV2]), ["e2"])
        self.assertEqual(match_events("Arsenal v Chelsea: team news", [EV, EV2], require_both=True), ["e2"])
        self.assertEqual(match_events("Arsenal boss on injuries", [EV, EV2], require_both=True), [])


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Newcastle without Isak for United trip</title><description>Striker out.</description>
<link>https://bbc.example/1</link><guid>g1</guid><pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>Chelsea sign nobody</title><link>https://bbc.example/2</link><pubDate>Fri, 18 Sep 2026 09:00:00 +0000</pubDate></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Arsenal XI confirmed</title>
<link href="https://club.example/a1"/><published>2026-09-18T11:00:00Z</published><summary>Saka starts</summary></entry></feed>"""


class NewsFeedTest(unittest.TestCase):
    def test_rss(self):
        items = parse_rss(RSS, "rss/test", 5.0)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].id, "rss/test:g1")
        self.assertEqual(items[0].published_at, 1789725600.0)
        self.assertEqual(items[1].published_at, 1789722000.0)
        self.assertEqual(items[0].body, "Striker out.")

    def test_atom(self):
        items = parse_rss(ATOM, "rss/club", 5.0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://club.example/a1")
        self.assertEqual(items[0].published_at, 1789729200.0)

    def test_lineups(self):
        d = {"response": [{"team": {"name": "Arsenal"}, "formation": "4-3-3",
                           "startXI": [{"player": {"name": "Raya"}}], "substitutes": [{"player": {"name": "Neto"}}]}]}
        items = ApiFootballLineups.parse(d, 99, 7.0)
        self.assertEqual(len(items), 1)
        self.assertIn("Raya", items[0].body)
        self.assertIsNone(items[0].published_at)
        self.assertEqual(items[0].seen_at, 7.0)
        self.assertEqual(items[0].id, ApiFootballLineups.parse(d, 99, 8.0)[0].id)  # stable across polls


ODDS_PAYLOAD = [{"id": "e1", "sport_key": "soccer_epl", "commence_time": "2026-09-19T14:00:00Z",
                 "home_team": "Manchester United", "away_team": "Newcastle United",
                 "bookmakers": [{"key": "pinnacle", "last_update": "2026-09-18T09:00:00Z", "markets": [
                     {"key": "h2h", "last_update": "2026-09-18T09:00:00Z", "outcomes": [
                         {"name": "Manchester United", "price": 2.1}, {"name": "Newcastle United", "price": 3.4}, {"name": "Draw", "price": 3.5}]}]}]}]


class OddsFeedTest(unittest.TestCase):
    def test_parse(self):
        e = OddsApi.parse_event(ODDS_PAYLOAD[0])
        self.assertEqual((e.id, e.home), ("e1", "Manchester United"))
        ticks = OddsApi.parse_ticks(ODDS_PAYLOAD[0], 1.0)
        self.assertEqual({t.outcome for t in ticks}, {"home", "away", "draw"})
        self.assertEqual(ticks[0].book_update, 1789722000.0)

    def test_store_ignores_unchanged(self):
        s = Store()
        ticks = OddsApi.parse_ticks(ODDS_PAYLOAD[0], 1.0)
        self.assertEqual(s.add_odds(ticks), 3)
        self.assertEqual(s.add_odds(ticks), 0)


class LatencyTest(unittest.TestCase):
    def series(self, prices):
        return [OddsTick("e1", "pinnacle", "h2h", "home", p, t, t) for t, p in prices]

    def test_first_move(self):
        s = self.series([(0, 2.0), (50, 2.0), (120, 2.02), (300, 2.5)])
        lag, before, after = first_move(s, 100.0, 0.02, 3600)
        self.assertEqual((lag, before, after), (200.0, 2.0, 2.5))  # 2.02 is 0.005 of implied, below threshold
        self.assertEqual(first_move(s, 100.0, 0.02, 150)[0], None)  # window too short
        self.assertTrue(first_move(s, -1.0, 0.02, 10)[1] != first_move(s, -1.0, 0.02, 10)[1])  # nan: no price before

    def test_observe_and_summarize(self):
        st = Store()
        st.upsert_event(EV, 0.0)
        st.add_odds(self.series([(0, 2.0), (400, 2.6)]))
        st.add_odds([OddsTick("e1", "bet365", "h2h", "home", 2.0, 0, 0), OddsTick("e1", "bet365", "h2h", "home", 2.0, 900, 900)])
        st.add_team_news(TeamNews(id="n1", source="rss", title="Man Utd lose Fernandes", published_at=100.0, seen_at=130.0, event_ids=["e1"]))
        obs = observe(st, threshold=0.02, window_s=3600)
        self.assertEqual(len(obs), 2)
        rep = summarize(obs)
        self.assertEqual(rep["pinnacle"]["move_lag_med_s"], 300.0)
        self.assertEqual(rep["pinnacle"]["detect_lag_med_s"], 30.0)
        self.assertEqual(rep["bet365"]["moved"], 0)
        self.assertAlmostEqual(edge_seconds(rep, judge_s=2.0)["pinnacle"], 268.0)
        self.assertNotIn("bet365", edge_seconds(rep))


class FakeApi:
    last_remaining = 400
    last_used = 100

    def __init__(self):
        self.odds_calls = []

    def events(self, sport):
        return [EV, EV2]

    def odds(self, sport, ids=None):
        self.odds_calls.append((sport, ids))
        return [EV], OddsApi.parse_ticks(ODDS_PAYLOAD[0], 1.0)


class FakeNews:
    name = "fake"

    def __init__(self):
        self.items = []

    def poll(self):
        return list(self.items)


class BettingLoopTest(unittest.TestCase):
    def test_schedule_and_burst(self):
        api, news, store = FakeApi(), FakeNews(), Store()
        cfg = loop.LoopConfig(sports=["soccer_epl"], events_interval_s=600, odds_interval_s=300, odds_window_s=7200,
                              news_interval_s=60, odds_burst_interval_s=30, burst_s=120, judge=False)
        st = loop.State()
        now = 9000.0  # kickoff at 10000, inside the window
        loop.tick(api, [news], store, None, cfg, st, now)
        self.assertEqual(len(api.odds_calls), 1)
        self.assertEqual(st.next_odds, now + 300)
        # matched team news starts a burst: next odds due now, then every 30s
        news.items = [TeamNews(id="n1", source="fake", title="Newcastle lose Isak", published_at=now + 50, seen_at=now + 60)]
        loop.tick(api, [news], store, None, cfg, st, now + 60)
        self.assertEqual(len(api.odds_calls), 2)
        self.assertEqual(st.next_odds, now + 90)
        self.assertEqual(store.team_news_all()[0].event_ids, ["e1"])
        self.assertEqual(st.stats["matched"], 1)
        # after the burst, back to the slow cadence
        loop.tick(api, [news], store, None, cfg, st, now + 60 + 200)
        self.assertEqual(st.next_odds, now + 260 + 300)

    def test_no_odds_outside_window(self):
        api, store = FakeApi(), Store()
        cfg = loop.LoopConfig(odds_window_s=3600, judge=False)
        loop.tick(api, [], store, None, cfg, loop.State(), 1000.0)  # kickoff 9000s away
        self.assertEqual(api.odds_calls, [])

    def test_quota_floor(self):
        api, store = FakeApi(), Store()
        api.last_remaining = 5
        loop.tick(api, [], store, None, loop.LoopConfig(min_quota=20, judge=False), loop.State(), 9000.0)
        self.assertEqual(api.odds_calls, [])

    def test_judge_hooks_and_shadow_bets(self):
        api, news, store = FakeApi(), FakeNews(), Store()
        seen = []
        hooks = loop.Hooks(
            judge=lambda n, e: seen.append((n.id, e.id)) or {"absences": 1},
            price=lambda eid, s: {"home": 0.45},
            ev=lambda f, m, e: [{"outcome": "home", "p_market": 0.45, "p_model": 0.45, "ev": 0.0, "stake": 0.0, "price": 2.1, "bookmaker": "pinnacle"}],
        )
        cfg = loop.LoopConfig(odds_window_s=7200, news_max_age_s=600)
        st = loop.State()
        news.items = [TeamNews(id="n1", source="fake", title="Man Utd XI: Fernandes out", published_at=9000.0, seen_at=9010.0),
                      TeamNews(id="old", source="fake", title="Arsenal news from yesterday", published_at=1000.0, seen_at=9010.0)]
        loop.tick(api, [news], store, hooks, cfg, st, 9010.0)
        self.assertEqual(seen, [("n1", "e1")])  # stale one stored for latency, not judged
        bets = store.db.execute("SELECT event_id, stake, placed, note FROM bets").fetchall()
        self.assertEqual([tuple(b) for b in bets], [("e1", 0.0, 0, "no_bet")])
        self.assertEqual(len(store.team_news_all()), 2)


if __name__ == "__main__":
    unittest.main()
