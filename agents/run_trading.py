"""Wire the trading ingestion loop to the scaffold. Run: python3 -m agents.run_trading

Imports the scaffold by the names it was written with. If a name differs, change the four
lines in `hooks()` and nothing else. RISK.mode stays whatever config.py says; this file does not
touch it.
"""
from __future__ import annotations

import logging
import os
import sys

from agents.common.store import NewsItem, Store
from agents.trading.feeds import feeds_from_env
from agents.trading.loop import Hooks, LoopConfig, run_forever


def hooks() -> Hooks:
    try:
        from agents.trading import execute, judge, risk, signal  # scaffold
    except ImportError as e:
        sys.exit(f"scaffold missing ({e}). Commit agents/trading/{{judge,signal,risk,execute}}.py first.")
    return Hooks(
        judge=lambda item: judge.judge_news(item.text),
        compose=lambda features, item: signal.compose(features),
        check=lambda sig, item: risk.check(sig),
        execute=lambda decision, item: execute.execute(decision),
        halted=getattr(risk, "Halted", KeyboardInterrupt),
    )


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store(os.environ.get("AGENTS_DB", "data/agents.db"))
    cfg = LoopConfig(
        poll_interval_s=float(os.environ.get("NEWS_POLL_S", "120")),
        max_age_s=float(os.environ.get("NEWS_MAX_AGE_S", "900")),
        accept_undated=os.environ.get("NEWS_ACCEPT_UNDATED", "0") == "1",
        symbols={s.strip().upper() for s in os.environ.get("TRADE_SYMBOLS", "").split(",") if s.strip()},
    )
    feeds = feeds_from_env()
    if not feeds:
        sys.exit("no feeds: set CRYPTOPANIC_TOKEN and/or BINANCE_ANNOUNCEMENTS=1")
    if os.environ.get("DRY_FEEDS") == "1":  # print what the feeds return and exit, no judge
        for f in feeds:
            for it in f.poll()[:10]:
                print(f.name, it.published_at, it.symbols, it.title[:90])
        return 0
    run_forever(feeds, store, hooks(), cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
