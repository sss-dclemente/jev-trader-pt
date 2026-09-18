"""Team news sources. What matters is `published_at` from the source itself: the latency edge is
measured from the moment the news exists, not from the moment we saw it.

RSS: any feed with pubDate (BBC Sport football, club sites, Sky Sports). Stdlib parser.
API-Football lineups: no publication timestamp on the payload, so published_at is our first
sighting and the error is bounded by the poll cadence. Keep the cadence tight near kickoff.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Protocol
from xml.etree import ElementTree as ET

from agents.common.http import Http
from agents.common.store import TeamNews
from agents.trading.feeds import parse_iso


class TeamNewsFeed(Protocol):
    name: str

    def poll(self) -> list[TeamNews]: ...


def parse_rfc822(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return parse_iso(s)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_rss(xml: str, source: str, seen_at: float) -> list[TeamNews]:
    root = ET.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
    items: list[TeamNews] = []
    for it in root.iter("item"):  # RSS 2.0
        link = _text(it.find("link"))
        guid = _text(it.find("guid")) or link or hashlib.sha1(_text(it.find("title")).encode()).hexdigest()
        items.append(TeamNews(
            id=f"{source}:{guid}", source=source, title=_text(it.find("title")), body=_text(it.find("description")),
            url=link, published_at=parse_rfc822(_text(it.find("pubDate")) or _text(it.find("dc:date", ns))), seen_at=seen_at))
    for en in root.iter("{http://www.w3.org/2005/Atom}entry"):  # Atom
        link_el = en.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        guid = _text(en.find("atom:id", ns)) or link
        items.append(TeamNews(
            id=f"{source}:{guid}", source=source, title=_text(en.find("atom:title", ns)),
            body=_text(en.find("atom:summary", ns)) or _text(en.find("atom:content", ns)), url=link,
            published_at=parse_iso(_text(en.find("atom:published", ns)) or _text(en.find("atom:updated", ns))), seen_at=seen_at))
    return items


@dataclass
class RssFeed:
    url: str
    name: str = "rss"
    http: Http = field(default_factory=Http)
    clock: Callable[[], float] = time.time

    def poll(self) -> list[TeamNews]:
        return parse_rss(self.http.get_text(self.url, headers={"Accept": "application/rss+xml, application/xml, text/xml"}),
                         self.name, self.clock())


@dataclass
class ApiFootballLineups:
    """https://www.api-football.com/documentation-v3#tag/Fixtures/operation/get-fixtures-lineups
    Free plan: 100 req/day. Call `poll_fixture` only for fixtures inside the pre kickoff window."""
    key: str
    fixture_ids: list[int] = field(default_factory=list)
    base: str = "https://v3.football.api-sports.io"
    http: Http = field(default_factory=Http)
    clock: Callable[[], float] = time.time
    name: str = "apifootball_lineups"

    def poll(self) -> list[TeamNews]:
        out = []
        for fid in self.fixture_ids:
            out.extend(self.poll_fixture(fid))
        return out

    def poll_fixture(self, fixture_id: int) -> list[TeamNews]:
        data, _ = self.http.get_json(f"{self.base}/fixtures/lineups", {"fixture": fixture_id},
                                     headers={"x-apisports-key": self.key})
        now = self.clock()
        return self.parse(data, fixture_id, now)

    @staticmethod
    def parse(data: dict[str, Any], fixture_id: int, seen_at: float) -> list[TeamNews]:
        out = []
        for side in data.get("response", []):
            team = (side.get("team") or {}).get("name", "?")
            xi = [p.get("player", {}).get("name", "?") for p in side.get("startXI", [])]
            bench = [p.get("player", {}).get("name", "?") for p in side.get("substitutes", [])]
            formation = side.get("formation") or "?"
            body = f"{team} starting XI ({formation}): {', '.join(xi)}. Bench: {', '.join(bench)}."
            digest = hashlib.sha1(body.encode()).hexdigest()[:12]
            out.append(TeamNews(id=f"apifootball:{fixture_id}:{team}:{digest}", source="apifootball_lineups",
                                title=f"{team} lineup confirmed", body=body, url="", published_at=None, seen_at=seen_at))
        return out


def feeds_from_env(env: dict[str, str] | None = None, http: Http | None = None) -> list[TeamNewsFeed]:
    env = env if env is not None else dict(os.environ)
    http = http or Http()
    feeds: list[TeamNewsFeed] = []
    urls = [u.strip() for u in env.get("TEAM_NEWS_RSS", "https://feeds.bbci.co.uk/sport/football/rss.xml").split(",") if u.strip()]
    for i, u in enumerate(urls):
        host = u.split("/")[2] if "//" in u else f"rss{i}"
        feeds.append(RssFeed(url=u, name=f"rss/{host}", http=http))
    return feeds
