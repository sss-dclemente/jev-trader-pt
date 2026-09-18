"""Measure the edge before betting on it.

For each piece of team news matched to an event, two lags:
  detect_lag = seen_at - published_at            how late WE are
  move_lag   = first bookmaker update after published_at whose implied probability moved by
               >= `threshold` from the last price before the news, minus published_at
The edge exists only where detect_lag + judge time < move_lag, per bookmaker. Pure functions
over store rows, plus a CLI report. Implied probability uses the raw 1/price; overround is
constant across the move and cancels for the threshold, so no devig needed here.
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass

from agents.common.store import OddsTick, Store, TeamNews


@dataclass
class MoveObs:
    news_id: str
    event_id: str
    bookmaker: str
    outcome: str
    published_at: float
    detect_lag_s: float
    move_lag_s: float | None  # None = no move inside the window
    before: float
    after: float | None


def implied(price: float) -> float:
    return 1.0 / price if price > 0 else 0.0


def first_move(ticks: list[OddsTick], t_news: float, threshold: float, window_s: float) -> tuple[float | None, float, float | None]:
    """ticks: one (bookmaker, outcome) series ordered by book_update.
    Returns (move_lag_s | None, price_before, price_after | None)."""
    before = [t for t in ticks if t.book_update <= t_news]
    if not before:
        return None, float("nan"), None
    base = before[-1].price
    p0 = implied(base)
    for t in ticks:
        if t.book_update <= t_news:
            continue
        if t.book_update - t_news > window_s:
            break
        if abs(implied(t.price) - p0) >= threshold:
            return t.book_update - t_news, base, t.price
    return None, base, None


def observe(store: Store, threshold: float = 0.02, window_s: float = 3600.0, market: str = "h2h") -> list[MoveObs]:
    out: list[MoveObs] = []
    for n in store.team_news_all():
        if n.published_at is None:
            continue
        for eid in n.event_ids:
            series: dict[tuple[str, str], list[OddsTick]] = defaultdict(list)
            for t in store.odds_for(eid, market):
                series[(t.bookmaker, t.outcome)].append(t)
            for (book, outcome), ticks in series.items():
                lag, before, after = first_move(ticks, n.published_at, threshold, window_s)
                if before != before:  # nan: no price before the news, nothing to measure
                    continue
                out.append(MoveObs(n.id, eid, book, outcome, n.published_at, n.seen_at - n.published_at, lag, before, after))
    return out


def summarize(obs: list[MoveObs]) -> dict[str, dict[str, float]]:
    """Per bookmaker: n, moved share, median and p25/p75 of move lag, median detect lag."""
    by: dict[str, list[MoveObs]] = defaultdict(list)
    for o in obs:
        by[o.bookmaker].append(o)
    by["ALL"] = list(obs)
    rep: dict[str, dict[str, float]] = {}
    for book, xs in by.items():
        moved = [o.move_lag_s for o in xs if o.move_lag_s is not None]
        row = {"n": len(xs), "moved": len(moved), "moved_share": (len(moved) / len(xs)) if xs else 0.0,
               "detect_lag_med_s": statistics.median(o.detect_lag_s for o in xs) if xs else 0.0}
        if moved:
            q = statistics.quantiles(moved, n=4) if len(moved) >= 2 else [moved[0]] * 3
            row.update({"move_lag_med_s": statistics.median(moved), "move_lag_p25_s": q[0], "move_lag_p75_s": q[2]})
        rep[book] = row
    return rep


def edge_seconds(rep: dict[str, dict[str, float]], judge_s: float = 2.0) -> dict[str, float]:
    """Median seconds of head start per bookmaker after our own lag. Negative = no edge there."""
    return {b: r["move_lag_med_s"] - r["detect_lag_med_s"] - judge_s for b, r in rep.items() if "move_lag_med_s" in r}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="team news -> odds move latency report")
    ap.add_argument("--db", default="data/agents.db")
    ap.add_argument("--threshold", type=float, default=0.02, help="implied prob move that counts (default 0.02)")
    ap.add_argument("--window", type=float, default=3600.0, help="seconds after news to look for a move")
    ap.add_argument("--judge-s", type=float, default=2.0, help="our judge + placement time, for the edge column")
    a = ap.parse_args(argv)
    store = Store(a.db)
    obs = observe(store, a.threshold, a.window)
    rep = summarize(obs)
    edge = edge_seconds(rep, a.judge_s)
    print(f"{'bookmaker':16} {'n':>5} {'moved':>6} {'share':>6} {'detect_med':>10} {'move_med':>9} {'p25':>7} {'p75':>7} {'edge_s':>7}")
    for book in sorted(rep, key=lambda b: (b == "ALL", b)):
        r = rep[book]
        print(f"{book:16} {r['n']:5d} {r['moved']:6d} {r['moved_share']:6.2f} {r['detect_lag_med_s']:10.1f} "
              f"{r.get('move_lag_med_s', float('nan')):9.1f} {r.get('move_lag_p25_s', float('nan')):7.1f} "
              f"{r.get('move_lag_p75_s', float('nan')):7.1f} {edge.get(book, float('nan')):7.1f}")
    if not obs:
        print("no observations yet: need team news with published_at matched to events that have odds before and after it", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
