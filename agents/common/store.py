"""SQLite store for everything the loops see. Append only, timestamps are unix seconds.

Two timestamps everywhere: `published_at` (what the source claims) and `seen_at`
(when our poll returned it). The difference is our own detection lag, which has
to be small for any latency edge to be real. Keep both, never merge them.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, published_at REAL, seen_at REAL NOT NULL,
  title TEXT NOT NULL, body TEXT, url TEXT, symbols TEXT
);
CREATE TABLE IF NOT EXISTS judgments (
  news_id TEXT NOT NULL, ts REAL NOT NULL, latency_ms REAL, features TEXT, signal TEXT, action TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, sport TEXT NOT NULL, commence REAL NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL, seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS odds (
  event_id TEXT NOT NULL, bookmaker TEXT NOT NULL, market TEXT NOT NULL, outcome TEXT NOT NULL,
  price REAL NOT NULL, book_update REAL NOT NULL, seen_at REAL NOT NULL,
  PRIMARY KEY (event_id, bookmaker, market, outcome, book_update)
);
CREATE INDEX IF NOT EXISTS odds_event ON odds (event_id, bookmaker, outcome, book_update);
CREATE TABLE IF NOT EXISTS team_news (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, published_at REAL, seen_at REAL NOT NULL,
  title TEXT NOT NULL, body TEXT, url TEXT, event_ids TEXT
);
CREATE TABLE IF NOT EXISTS bets (
  ts REAL NOT NULL, event_id TEXT NOT NULL, news_id TEXT, outcome TEXT, p_market REAL, p_model REAL,
  ev REAL, stake REAL, price REAL, bookmaker TEXT, placed INTEGER NOT NULL DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS quota (ts REAL NOT NULL, api TEXT NOT NULL, remaining INTEGER, used INTEGER);
CREATE TABLE IF NOT EXISTS polls (ts REAL NOT NULL, source TEXT NOT NULL, ok INTEGER NOT NULL, items INTEGER, elapsed_s REAL, error TEXT);
"""


@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    body: str = ""
    url: str = ""
    published_at: float | None = None
    seen_at: float = 0.0
    symbols: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


@dataclass
class Event:
    id: str
    sport: str
    commence: float
    home: str
    away: str


@dataclass
class OddsTick:
    event_id: str
    bookmaker: str
    market: str
    outcome: str
    price: float
    book_update: float
    seen_at: float


@dataclass
class TeamNews:
    id: str
    source: str
    title: str
    body: str = ""
    url: str = ""
    published_at: float | None = None
    seen_at: float = 0.0
    event_ids: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # news
    def has_news(self, news_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM news WHERE id=?", (news_id,)).fetchone() is not None

    def add_news(self, n: NewsItem) -> bool:
        """Insert if new. Returns True when inserted."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO news (id, source, published_at, seen_at, title, body, url, symbols) VALUES (?,?,?,?,?,?,?,?)",
            (n.id, n.source, n.published_at, n.seen_at, n.title, n.body, n.url, json.dumps(n.symbols)))
        return cur.rowcount == 1

    def add_judgment(self, news_id: str, ts: float, latency_ms: float | None, features: Any, signal: Any,
                     action: str | None, error: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO judgments (news_id, ts, latency_ms, features, signal, action, error) VALUES (?,?,?,?,?,?,?)",
            (news_id, ts, latency_ms, _dump(features), _dump(signal), action, error))

    # betting
    def upsert_event(self, e: Event, seen_at: float) -> None:
        self.db.execute(
            "INSERT INTO events (id, sport, commence, home, away, seen_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET commence=excluded.commence, seen_at=excluded.seen_at",
            (e.id, e.sport, e.commence, e.home, e.away, seen_at))

    def events_between(self, t0: float, t1: float) -> list[Event]:
        rows = self.db.execute("SELECT id, sport, commence, home, away FROM events WHERE commence BETWEEN ? AND ? ORDER BY commence",
                               (t0, t1)).fetchall()
        return [Event(**dict(r)) for r in rows]

    def event(self, event_id: str) -> Event | None:
        r = self.db.execute("SELECT id, sport, commence, home, away FROM events WHERE id=?", (event_id,)).fetchone()
        return Event(**dict(r)) if r else None

    def add_odds(self, ticks: Iterable[OddsTick]) -> int:
        """Insert ticks. Same (event, book, market, outcome, book_update) is ignored, so a poll that
        returns an unchanged book costs nothing. Returns number of new rows."""
        n = 0
        for t in ticks:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO odds (event_id, bookmaker, market, outcome, price, book_update, seen_at) VALUES (?,?,?,?,?,?,?)",
                (t.event_id, t.bookmaker, t.market, t.outcome, t.price, t.book_update, t.seen_at))
            n += cur.rowcount
        return n

    def odds_for(self, event_id: str, market: str = "h2h") -> list[OddsTick]:
        rows = self.db.execute(
            "SELECT event_id, bookmaker, market, outcome, price, book_update, seen_at FROM odds WHERE event_id=? AND market=? ORDER BY book_update",
            (event_id, market)).fetchall()
        return [OddsTick(**dict(r)) for r in rows]

    def has_team_news(self, news_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM team_news WHERE id=?", (news_id,)).fetchone() is not None

    def add_team_news(self, n: TeamNews) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO team_news (id, source, published_at, seen_at, title, body, url, event_ids) VALUES (?,?,?,?,?,?,?,?)",
            (n.id, n.source, n.published_at, n.seen_at, n.title, n.body, n.url, json.dumps(n.event_ids)))
        return cur.rowcount == 1

    def team_news_all(self) -> list[TeamNews]:
        rows = self.db.execute("SELECT id, source, published_at, seen_at, title, body, url, event_ids FROM team_news ORDER BY seen_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["event_ids"] = json.loads(d["event_ids"] or "[]")
            out.append(TeamNews(**d))
        return out

    def add_bet(self, ts: float, event_id: str, news_id: str | None, outcome: str | None, p_market: float | None,
                p_model: float | None, ev: float | None, stake: float | None, price: float | None,
                bookmaker: str | None, placed: bool, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO bets (ts, event_id, news_id, outcome, p_market, p_model, ev, stake, price, bookmaker, placed, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, event_id, news_id, outcome, p_market, p_model, ev, stake, price, bookmaker, int(placed), note))

    # ops
    def add_quota(self, api: str, remaining: int | None, used: int | None, ts: float | None = None) -> None:
        self.db.execute("INSERT INTO quota (ts, api, remaining, used) VALUES (?,?,?,?)", (ts or time.time(), api, remaining, used))

    def last_quota(self, api: str) -> int | None:
        r = self.db.execute("SELECT remaining FROM quota WHERE api=? ORDER BY ts DESC LIMIT 1", (api,)).fetchone()
        return r[0] if r else None

    def add_poll(self, source: str, ok: bool, items: int, elapsed_s: float, error: str | None = None, ts: float | None = None) -> None:
        self.db.execute("INSERT INTO polls (ts, source, ok, items, elapsed_s, error) VALUES (?,?,?,?,?,?)",
                        (ts or time.time(), source, int(ok), items, elapsed_s, error))


def _dump(x: Any) -> str | None:
    if x is None:
        return None
    if hasattr(x, "__dataclass_fields__"):
        x = asdict(x)
    try:
        return json.dumps(x, default=str)
    except TypeError:
        return json.dumps(str(x))
