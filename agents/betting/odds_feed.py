"""the-odds-api v4 client. https://the-odds-api.com/liveapi/guides/v4/

Quota: the free plan is 500 requests a month, counted per region x market, so one poll of one
sport for one region and h2h costs one credit. `/events` is free and tells us kickoffs, so the
loop lists events for free and spends credits only near kickoff and around team news.
Remaining quota comes back in `x-requests-remaining` on every response and is stored.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agents.common.http import Http
from agents.common.store import Event, OddsTick
from agents.trading.feeds import parse_iso

BASE = "https://api.the-odds-api.com/v4"


@dataclass
class OddsApi:
    key: str
    regions: str = "eu"
    markets: str = "h2h"
    odds_format: str = "decimal"
    http: Http = field(default_factory=Http)
    clock: Callable[[], float] = time.time
    last_remaining: int | None = None
    last_used: int | None = None

    def _quota(self, headers: Any) -> None:
        h = {k.lower(): v for k, v in dict(headers).items()}
        try:
            self.last_remaining = int(h.get("x-requests-remaining")) if h.get("x-requests-remaining") is not None else self.last_remaining
            self.last_used = int(h.get("x-requests-used")) if h.get("x-requests-used") is not None else self.last_used
        except ValueError:
            pass

    def sports(self, all_sports: bool = False) -> list[dict[str, Any]]:
        data, headers = self.http.get_json(f"{BASE}/sports/", {"apiKey": self.key, "all": "true" if all_sports else None})
        self._quota(headers)
        return data

    def soccer_keys(self) -> list[str]:
        return [s["key"] for s in self.sports() if s.get("group") == "Soccer" and s.get("active")]

    def events(self, sport: str) -> list[Event]:
        """Free: does not spend quota."""
        data, headers = self.http.get_json(f"{BASE}/sports/{sport}/events/", {"apiKey": self.key})
        self._quota(headers)
        return [self.parse_event(e) for e in data]

    def odds(self, sport: str, event_ids: list[str] | None = None) -> tuple[list[Event], list[OddsTick]]:
        """One credit per call (per region x market). `event_ids` narrows the payload, not the cost."""
        params: dict[str, Any] = {"apiKey": self.key, "regions": self.regions, "markets": self.markets,
                                  "oddsFormat": self.odds_format, "dateFormat": "iso"}
        if event_ids:
            params["eventIds"] = ",".join(event_ids)
        data, headers = self.http.get_json(f"{BASE}/sports/{sport}/odds/", params)
        self._quota(headers)
        now = self.clock()
        events, ticks = [], []
        for e in data:
            events.append(self.parse_event(e))
            ticks.extend(self.parse_ticks(e, now))
        return events, ticks

    @staticmethod
    def parse_event(e: dict[str, Any]) -> Event:
        return Event(id=e["id"], sport=e.get("sport_key", ""), commence=parse_iso(e.get("commence_time")) or 0.0,
                     home=e.get("home_team", ""), away=e.get("away_team", ""))

    @staticmethod
    def parse_ticks(e: dict[str, Any], seen_at: float) -> list[OddsTick]:
        out = []
        for b in e.get("bookmakers", []):
            for m in b.get("markets", []):
                upd = parse_iso(m.get("last_update") or b.get("last_update")) or seen_at
                for o in m.get("outcomes", []):
                    name = o.get("name", "")
                    # normalise 1X2 to home/draw/away so the store does not depend on team spelling
                    if name == e.get("home_team"):
                        name = "home"
                    elif name == e.get("away_team"):
                        name = "away"
                    elif name.lower() == "draw":
                        name = "draw"
                    out.append(OddsTick(event_id=e["id"], bookmaker=b.get("key", "?"), market=m.get("key", "h2h"),
                                        outcome=name, price=float(o["price"]), book_update=upd, seen_at=seen_at))
        return out
