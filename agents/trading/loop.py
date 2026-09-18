"""Trading ingestion loop: feeds -> judge_news -> signal -> risk -> execute.

The loop owns polling, dedup, staleness and bookkeeping. It owns no numbers about money: those
live in agents/trading/config.py (RISK) and are enforced by risk.py. Everything model related is
injected through `Hooks`, so this file runs against stubs in tests and against the scaffold in
agents/run_trading.py.
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from agents.common.store import NewsItem, Store
from agents.trading.feeds import Feed

log = logging.getLogger("agents.trading.loop")


@dataclass
class Hooks:
    """Seams into the scaffold. Signatures:
      judge(item) -> features            trading/judge.py: judge_news(text) -> typed features
      compose(features, item) -> signal  trading/signal.py: weighted composition + confidence gates
      check(signal, item) -> decision    trading/risk.py: deny by default, raises `halted` to stop the loop
      execute(decision, item) -> result  trading/execute.py: shadow | testnet | live
      action_of(result_or_decision) -> str | None   for the log only
    `halted` is the exception class risk.py raises on a kill switch. Anything else that the hooks
    raise is logged against the news item and the loop moves on."""
    judge: Callable[[NewsItem], Any]
    compose: Callable[[Any, NewsItem], Any]
    check: Callable[[Any, NewsItem], Any]
    execute: Callable[[Any, NewsItem], Any]
    action_of: Callable[[Any], str | None] = lambda x: getattr(x, "action", None) if not isinstance(x, dict) else x.get("action")
    halted: type[BaseException] = KeyboardInterrupt


@dataclass
class LoopConfig:
    poll_interval_s: float = 120.0
    # News older than this when first seen is not news. Whoever reacts in the first minutes has the
    # move; we do not chase an hour old headline.
    max_age_s: float = 900.0
    # Items with no published timestamp: judge them only if the source is trusted for freshness.
    accept_undated: bool = False
    symbols: set[str] = field(default_factory=set)  # empty = judge everything


@dataclass
class Stats:
    polled: int = 0
    new: int = 0
    stale: int = 0
    filtered: int = 0
    judged: int = 0
    executed: int = 0
    errors: int = 0


def is_stale(item: NewsItem, now: float, cfg: LoopConfig) -> bool:
    if item.published_at is None:
        return not cfg.accept_undated
    return (now - item.published_at) > cfg.max_age_s


def is_relevant(item: NewsItem, cfg: LoopConfig) -> bool:
    if not cfg.symbols:
        return True
    return bool(cfg.symbols.intersection(s.upper() for s in item.symbols))


def process_item(item: NewsItem, store: Store, hooks: Hooks, clock: Callable[[], float]) -> str | None:
    """Judge one item end to end. Returns the action string for logging. Raises hooks.halted through."""
    t0 = time.monotonic()
    action: str | None = None
    features = signal = decision = result = None
    try:
        features = hooks.judge(item)
        latency_ms = (time.monotonic() - t0) * 1000.0
        signal = hooks.compose(features, item)
        decision = hooks.check(signal, item)
        result = hooks.execute(decision, item) if decision is not None else None
        action = hooks.action_of(result if result is not None else decision)
        store.add_judgment(item.id, clock(), latency_ms, features, {"signal": signal, "decision": decision, "result": result}, action)
        return action
    except hooks.halted:
        store.add_judgment(item.id, clock(), (time.monotonic() - t0) * 1000.0, features, {"signal": signal, "decision": decision}, "halted", "halted")
        raise
    except Exception as e:  # one bad item never stops the loop
        store.add_judgment(item.id, clock(), (time.monotonic() - t0) * 1000.0, features, {"signal": signal, "decision": decision}, None,
                           f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        log.exception("item %s failed", item.id)
        return None


def run_once(feeds: Iterable[Feed], store: Store, hooks: Hooks, cfg: LoopConfig,
             clock: Callable[[], float] = time.time, stats: Stats | None = None) -> Stats:
    stats = stats or Stats()
    for feed in feeds:
        t0 = time.monotonic()
        try:
            items = feed.poll()
            store.add_poll(feed.name, True, len(items), time.monotonic() - t0, ts=clock())
        except Exception as e:
            store.add_poll(feed.name, False, 0, time.monotonic() - t0, f"{type(e).__name__}: {e}", ts=clock())
            log.warning("poll %s failed: %s", feed.name, e)
            stats.errors += 1
            continue
        stats.polled += len(items)
        # oldest first so the judgment log reads in time order
        for item in sorted(items, key=lambda i: i.published_at or i.seen_at):
            if not store.add_news(item):
                continue
            stats.new += 1
            now = clock()
            if is_stale(item, now, cfg):
                stats.stale += 1
                store.add_judgment(item.id, now, None, None, None, "skip_stale")
                continue
            if not is_relevant(item, cfg):
                stats.filtered += 1
                store.add_judgment(item.id, now, None, None, None, "skip_symbol")
                continue
            stats.judged += 1
            action = process_item(item, store, hooks, clock)
            if action is None:
                stats.errors += 1
            elif action not in ("hold", "skip", "deny", "none"):
                stats.executed += 1
            log.info("%s %s -> %s", item.source, item.title[:80], action)
    return stats


def run_forever(feeds: Iterable[Feed], store: Store, hooks: Hooks, cfg: LoopConfig,
                clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> Stats:
    feeds = list(feeds)
    stats = Stats()
    log.info("trading loop: %d feeds, every %.0fs, max_age %.0fs", len(feeds), cfg.poll_interval_s, cfg.max_age_s)
    while True:
        t0 = time.monotonic()
        try:
            run_once(feeds, store, hooks, cfg, clock, stats)
        except hooks.halted as e:
            log.error("HALTED: %s", e)
            return stats
        sleep(max(0.0, cfg.poll_interval_s - (time.monotonic() - t0)))
