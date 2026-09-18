"""Betting ingestion loop: odds + team news -> store, latency measured, judge/ev only on fresh news.

Budget is the design constraint: the-odds-api free tier is 500 credits a month. So:
  every EVENTS_INTERVAL_S    list events per sport (free) and keep kickoffs
  every ODDS_INTERVAL_S      one odds call per sport that has a kickoff inside ODDS_WINDOW_S
  every NEWS_INTERVAL_S      poll team news (RSS is free), match to events inside the window
  after matched news         burst: odds every ODDS_BURST_INTERVAL_S for BURST_S, that is the
                             resolution of the latency measurement
No bookmaker API here, so `execute` records the stake into `bets` with placed=0. With COEF all
zero in ev.py the stake is 0 by construction; the loop still logs p_market vs p_model so the
fit has data. The loop never computes a probability itself.
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from agents.betting.matching import match_events
from agents.betting.news_feed import TeamNewsFeed
from agents.betting.odds_feed import OddsApi
from agents.common.store import Event, Store, TeamNews

log = logging.getLogger("agents.betting.loop")


@dataclass
class Hooks:
    """Seams into the scaffold. Signatures:
      judge(news, event) -> features          betting/judge.py: text -> absences/motivation/freshness
      price(event_id, store) -> market         betting/market.py: latest 1X2 per book -> devigged probs
      ev(features, market, event) -> bets      betting/ev.py: p_model, EV, Kelly stake per outcome
      execute(bets, event, news) -> None       records or places; default writes to store.bets
    `halted` stops the loop, anything else is logged per news item."""
    judge: Callable[[TeamNews, Event], Any]
    price: Callable[[str, Store], Any]
    ev: Callable[[Any, Any, Event], Any]
    execute: Callable[[Any, Event, TeamNews], Any] | None = None
    halted: type[BaseException] = KeyboardInterrupt


@dataclass
class LoopConfig:
    sports: list[str] = field(default_factory=lambda: ["soccer_epl"])
    events_interval_s: float = 600.0
    odds_interval_s: float = 300.0
    odds_window_s: float = 3 * 3600.0     # start polling odds this long before kickoff
    news_interval_s: float = 60.0
    odds_burst_interval_s: float = 30.0
    burst_s: float = 900.0
    news_max_age_s: float = 1800.0        # older news on first sight: store for latency, do not judge
    min_quota: int = 20                   # stop spending below this many credits
    judge: bool = True                    # False = measurement only


@dataclass
class State:
    next_events: float = 0.0
    next_odds: float = 0.0
    next_news: float = 0.0
    burst_until: float = 0.0
    events: dict[str, Event] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=lambda: {"odds_calls": 0, "odds_rows": 0, "news": 0, "matched": 0, "judged": 0, "errors": 0})


def in_window(events: Iterable[Event], now: float, window_s: float, grace_s: float = 7200.0) -> list[Event]:
    """Kickoff within window ahead, or up to grace after (odds keep moving into the match)."""
    return [e for e in events if now - grace_s <= e.commence <= now + window_s]


def refresh_events(api: OddsApi, store: Store, cfg: LoopConfig, st: State, now: float) -> None:
    for sport in cfg.sports:
        t0 = time.monotonic()
        try:
            evs = api.events(sport)
            for e in evs:
                store.upsert_event(e, now)
                st.events[e.id] = e
            store.add_poll(f"odds_api/events/{sport}", True, len(evs), time.monotonic() - t0, ts=now)
        except Exception as e:
            store.add_poll(f"odds_api/events/{sport}", False, 0, time.monotonic() - t0, str(e), ts=now)
            log.warning("events %s: %s", sport, e)
            st.stats["errors"] += 1
    # forget events long gone
    st.events = {k: v for k, v in st.events.items() if v.commence > now - 4 * 3600}


def poll_odds(api: OddsApi, store: Store, cfg: LoopConfig, st: State, now: float) -> None:
    active = in_window(st.events.values(), now, cfg.odds_window_s)
    sports = sorted({e.sport for e in active})
    if not sports:
        return
    rem = api.last_remaining if api.last_remaining is not None else store.last_quota("odds_api")
    if rem is not None and rem < cfg.min_quota:
        log.warning("odds quota %s below floor %d, not polling", rem, cfg.min_quota)
        return
    for sport in sports:
        ids = [e.id for e in active if e.sport == sport]
        t0 = time.monotonic()
        try:
            evs, ticks = api.odds(sport, ids)
            for e in evs:
                store.upsert_event(e, now)
                st.events[e.id] = e
            n = store.add_odds(ticks)
            st.stats["odds_calls"] += 1
            st.stats["odds_rows"] += n
            store.add_quota("odds_api", api.last_remaining, api.last_used, now)
            store.add_poll(f"odds_api/odds/{sport}", True, n, time.monotonic() - t0, ts=now)
            log.info("odds %s: %d events, %d new ticks, quota left %s", sport, len(evs), n, api.last_remaining)
        except Exception as e:
            store.add_poll(f"odds_api/odds/{sport}", False, 0, time.monotonic() - t0, str(e), ts=now)
            log.warning("odds %s: %s", sport, e)
            st.stats["errors"] += 1


def handle_news(n: TeamNews, store: Store, hooks: Hooks | None, cfg: LoopConfig, st: State, now: float) -> bool:
    """Store, match, and judge when fresh. Returns True if matched (caller starts a burst)."""
    candidates = in_window(st.events.values(), now, cfg.odds_window_s)
    n.event_ids = match_events(n.text, candidates)
    if not store.add_team_news(n):
        return False
    st.stats["news"] += 1
    if not n.event_ids:
        return False
    st.stats["matched"] += 1
    log.info("team news -> %s: %s", n.event_ids, n.title[:80])
    fresh = n.published_at is not None and (now - n.published_at) <= cfg.news_max_age_s
    if not (hooks and cfg.judge and fresh):
        return True
    for eid in n.event_ids:
        ev = st.events.get(eid) or store.event(eid)
        if ev is None:
            continue
        try:
            st.stats["judged"] += 1
            features = hooks.judge(n, ev)
            market = hooks.price(eid, store)
            bets = hooks.ev(features, market, ev)
            if hooks.execute:
                hooks.execute(bets, ev, n)
            else:
                record_bets(store, bets, ev, n, now)
        except hooks.halted:
            raise
        except Exception as e:
            st.stats["errors"] += 1
            store.add_bet(now, eid, n.id, None, None, None, None, None, None, None, False,
                          f"error {type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            log.exception("judge/ev failed for %s on %s", n.id, eid)
    return True


def record_bets(store: Store, bets: Any, ev: Event, n: TeamNews, now: float) -> None:
    """Default execute: write whatever ev.py returned. Accepts an iterable of objects or dicts with
    outcome, p_market, p_model, ev, stake, price, bookmaker. Nothing is placed."""
    if bets is None:
        return
    for b in (bets if isinstance(bets, (list, tuple)) else [bets]):
        g = (lambda k: b.get(k)) if isinstance(b, dict) else (lambda k: getattr(b, k, None))
        store.add_bet(now, ev.id, n.id, g("outcome"), g("p_market"), g("p_model"), g("ev"), g("stake"), g("price"),
                      g("bookmaker"), False, "shadow" if (g("stake") or 0) > 0 else "no_bet")


def poll_news(feeds: Iterable[TeamNewsFeed], store: Store, hooks: Hooks | None, cfg: LoopConfig, st: State, now: float) -> bool:
    matched = False
    for f in feeds:
        t0 = time.monotonic()
        try:
            items = f.poll()
            store.add_poll(f.name, True, len(items), time.monotonic() - t0, ts=now)
        except Exception as e:
            store.add_poll(f.name, False, 0, time.monotonic() - t0, str(e), ts=now)
            log.warning("news %s: %s", f.name, e)
            st.stats["errors"] += 1
            continue
        for n in sorted(items, key=lambda i: i.published_at or i.seen_at):
            if store.has_team_news(n.id):
                continue
            matched = handle_news(n, store, hooks, cfg, st, now) or matched
    return matched


def tick(api: OddsApi, feeds: Iterable[TeamNewsFeed], store: Store, hooks: Hooks | None, cfg: LoopConfig, st: State, now: float) -> float:
    """One scheduler pass. Returns seconds until the next thing is due."""
    if now >= st.next_events:
        refresh_events(api, store, cfg, st, now)
        st.next_events = now + cfg.events_interval_s
    if now >= st.next_news:
        if poll_news(feeds, store, hooks, cfg, st, now):
            st.burst_until = now + cfg.burst_s
            st.next_odds = now  # first post news snapshot right away
        st.next_news = now + cfg.news_interval_s
    if now >= st.next_odds:
        poll_odds(api, store, cfg, st, now)
        st.next_odds = now + (cfg.odds_burst_interval_s if now < st.burst_until else cfg.odds_interval_s)
    return max(0.5, min(st.next_events, st.next_news, st.next_odds) - now)


def run_forever(api: OddsApi, feeds: Iterable[TeamNewsFeed], store: Store, hooks: Hooks | None, cfg: LoopConfig,
                clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> State:
    feeds = list(feeds)
    st = State()
    log.info("betting loop: sports=%s odds every %.0fs inside %.1fh of kickoff, news every %.0fs, burst %.0fs/%.0fs",
             cfg.sports, cfg.odds_interval_s, cfg.odds_window_s / 3600, cfg.news_interval_s, cfg.odds_burst_interval_s, cfg.burst_s)
    halted = hooks.halted if hooks else KeyboardInterrupt
    while True:
        try:
            wait = tick(api, feeds, store, hooks, cfg, st, clock())
        except halted as e:
            log.error("HALTED: %s", e)
            return st
        sleep(wait)
