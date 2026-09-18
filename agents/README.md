# agents: ingestion loops

Python 3.11, stdlib only (urllib, sqlite3, xml.etree). No new dependencies. The model, signal,
risk and EV code is the scaffold (`common/typesafe_client.py`, `trading/{config,judge,signal,risk,execute}.py`,
`betting/{market,judge,ev,backtest}.py`); this directory adds the loops that feed it.

Principle kept: the loops move text and timestamps. They never compute a probability, a price or a
stake. Every number about money lives in `trading/config.py` and `betting/ev.py`.

## Layout

    common/http.py        GET with retries, quota headers passed through
    common/store.py       SQLite: news, judgments, events, odds, team_news, bets, quota, polls
    trading/feeds.py      CryptoPanic (v2 developer API) and Binance CMS announcements -> NewsItem
    trading/loop.py       poll -> dedup -> staleness -> judge -> signal -> risk -> execute
    betting/odds_feed.py  the-odds-api v4: free /events for kickoffs, /odds only when it pays
    betting/news_feed.py  RSS/Atom team news with the source pubDate, API-Football lineups
    betting/matching.py   headline -> event ids by team name and alias
    betting/latency.py    the measurement: news published -> first odds move, per bookmaker
    betting/loop.py       scheduler: events, odds near kickoff, news, burst after matched news
    run_trading.py        wiring to the scaffold names
    run_betting.py        wiring, MEASURE_ONLY=1 needs no model at all
    tests/                unittest, stubs for every hook

## Run

    python3 -m unittest discover -s agents/tests -t .
    DRY_FEEDS=1 python3 -m agents.run_trading          # print what the feeds return, no model
    python3 -m agents.run_trading                      # needs the scaffold, RISK.mode as in config.py
    LIST_SPORTS=1 python3 -m agents.run_betting        # sport keys for ODDS_SPORTS
    MEASURE_ONLY=1 python3 -m agents.run_betting       # collect odds + team news, no model
    python3 -m agents.betting.latency --db data/agents.db --threshold 0.02

## Trading loop

Each poll: new items only (id dedup in SQLite), drop anything older than `NEWS_MAX_AGE_S` on first
sight (a stale headline is priced in), optional symbol filter, then `judge_news(text)`,
`compose(features)`, `risk.check(signal)` (raises `Halted` on a kill switch, which ends the loop),
`execute(decision)`. Every step is logged in `judgments` with judge latency. One failing item is
logged and skipped; only `Halted` stops the loop.

Feeds: CryptoPanic free tier is about 100 requests a day, so the default poll is 120 s. Binance CMS
is public and can 403 from cloud IPs.

## Betting loop and the measurement

the-odds-api free tier: 500 credits a month, one credit per sport per poll (one region, h2h).
`/events` is free and gives kickoffs, so odds are polled only inside `ODDS_WINDOW_S` of a kickoff
at `ODDS_INTERVAL_S`, and at `ODDS_BURST_INTERVAL_S` for `BURST_S` after team news matched to one
of those events. The burst cadence is the resolution of the latency measurement. Polling stops
below `ODDS_MIN_QUOTA` credits.

`latency.py` reports per bookmaker: how many news items, share that moved the line by at least
`threshold` implied probability within the window, median and quartiles of the move lag (bookmaker
`last_update` minus source `published_at`), our own detect lag (`seen_at` minus `published_at`),
and `edge_s = move_lag - detect_lag - judge_s`. Bet only where `edge_s` is clearly positive and
`moved_share` is high. That table is the go/no-go for the whole betting agent.

RSS `pubDate` is the source timestamp. API-Football lineups carry none, so their `published_at` is
null and they count for judging but not for the latency table; keep the poll cadence tight near
kickoff if you rely on them.

With `COEF` zeros in `ev.py` every bet records `stake 0, placed 0` into `bets`. p_market vs p_model
rows accumulate for the fit either way.

## Wiring

`run_trading.py` and `run_betting.py` import the scaffold by name (`judge.judge_news`,
`signal.compose`, `risk.check`, `execute.execute`, `judge.judge_match`, `market.from_ticks`,
`ev.evaluate`). If the scaffold uses other names, change those lambdas; the loops take callables and
nothing else.
