"""Wire the betting ingestion loop to the scaffold. Run: python3 -m agents.run_betting

Measurement first: MEASURE_ONLY=1 stores odds and team news and never calls the model. Then
`python3 -m agents.betting.latency` prints the edge per bookmaker. COEF in ev.py stays as is.
"""
from __future__ import annotations

import logging
import os
import sys

from agents.betting.loop import Hooks, LoopConfig, run_forever
from agents.betting.news_feed import feeds_from_env
from agents.betting.odds_feed import OddsApi
from agents.common.store import Store


def hooks() -> Hooks:
    try:
        from agents.betting import ev, judge, market  # scaffold
    except ImportError as e:
        sys.exit(f"scaffold missing ({e}). Commit agents/betting/{{market,judge,ev}}.py first, or run with MEASURE_ONLY=1.")
    return Hooks(
        judge=lambda news, event: judge.judge_match(news.text, home=event.home, away=event.away),
        price=lambda event_id, store: market.from_ticks(store.odds_for(event_id)),
        ev=lambda features, mkt, event: ev.evaluate(features, mkt),
        halted=getattr(ev, "Halted", KeyboardInterrupt),
    )


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("set ODDS_API_KEY (https://the-odds-api.com)")
    api = OddsApi(key=key, regions=os.environ.get("ODDS_REGIONS", "eu"))
    store = Store(os.environ.get("AGENTS_DB", "data/agents.db"))
    cfg = LoopConfig(
        sports=[s.strip() for s in os.environ.get("ODDS_SPORTS", "soccer_epl").split(",") if s.strip()],
        events_interval_s=float(os.environ.get("EVENTS_INTERVAL_S", "600")),
        odds_interval_s=float(os.environ.get("ODDS_INTERVAL_S", "300")),
        odds_window_s=float(os.environ.get("ODDS_WINDOW_S", str(3 * 3600))),
        news_interval_s=float(os.environ.get("NEWS_INTERVAL_S", "60")),
        odds_burst_interval_s=float(os.environ.get("ODDS_BURST_INTERVAL_S", "30")),
        burst_s=float(os.environ.get("BURST_S", "900")),
        min_quota=int(os.environ.get("ODDS_MIN_QUOTA", "20")),
        judge=os.environ.get("MEASURE_ONLY", "0") != "1",
    )
    if os.environ.get("LIST_SPORTS") == "1":
        for k in api.soccer_keys():
            print(k)
        return 0
    h = None if not cfg.judge else hooks()
    run_forever(api, feeds_from_env(), store, h, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
