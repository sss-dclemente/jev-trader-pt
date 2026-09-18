"""News feeds for the trading loop. Each feed returns NewsItems, newest first, with the source's
own published timestamp. Dedup happens in the store, not here.

CryptoPanic developer API v2 (default) or v1, and Binance CMS announcements (no key). Both are
plain GETs. Config by environment, see agents/README.md.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from agents.common.http import Http
from agents.common.store import NewsItem


class Feed(Protocol):
    name: str

    def poll(self) -> list[NewsItem]: ...


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


_PAREN = re.compile(r"\(([A-Z0-9]{2,10})\)")
_TICKER = re.compile(r"\b([A-Z]{2,10})\b")


def tickers(title: str) -> list[str]:
    """Symbols in parentheses first (Binance style), else bare uppercase words. Deduped, stables dropped."""
    found = _PAREN.findall(title) or _TICKER.findall(title)
    out: list[str] = []
    for m in found:
        if m in _STOPWORDS or m in out:
            continue
        out.append(m)
    return out


@dataclass
class CryptoPanicFeed:
    """https://cryptopanic.com/developers/api/ . Free tier: ~100 req/day on v2, so poll every few minutes.
    `filter=important` cuts noise. `currencies` narrows to the pairs we trade."""
    token: str
    currencies: list[str] = field(default_factory=list)
    filter: str = "important"
    base: str = "https://cryptopanic.com/api/developer/v2/posts/"
    http: Http = field(default_factory=Http)
    clock: Callable[[], float] = time.time
    name: str = "cryptopanic"

    def poll(self) -> list[NewsItem]:
        params: dict[str, Any] = {"auth_token": self.token, "public": "true"}
        if self.filter:
            params["filter"] = self.filter
        if self.currencies:
            params["currencies"] = ",".join(self.currencies)
        data, _ = self.http.get_json(self.base, params)
        now = self.clock()
        return [self.parse(p, now) for p in data.get("results", [])]

    @staticmethod
    def parse(p: dict[str, Any], seen_at: float) -> NewsItem:
        syms = [c.get("code") for c in (p.get("currencies") or p.get("instruments") or []) if c.get("code")]
        src = p.get("source") or {}
        return NewsItem(
            id=f"cryptopanic:{p['id']}",
            source=f"cryptopanic/{src.get('domain') or src.get('title') or '?'}",
            title=p.get("title") or "",
            body=p.get("description") or "",
            url=p.get("original_url") or p.get("url") or "",
            published_at=parse_iso(p.get("published_at")),
            seen_at=seen_at,
            symbols=syms,
        )


@dataclass
class BinanceAnnouncementsFeed:
    """Binance support CMS, public. catalogId 48 = new listings, 161 = delistings. No auth, but
    the endpoint sits behind bot protection and can return 403 from cloud IPs; then run from a
    residential box or drop this feed. Tickers are extracted from the title in parentheses."""
    catalog_ids: tuple[int, ...] = (48, 161)
    page_size: int = 20
    base: str = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    http: Http = field(default_factory=Http)
    clock: Callable[[], float] = time.time
    name: str = "binance_announcements"

    def poll(self) -> list[NewsItem]:
        out: list[NewsItem] = []
        now = self.clock()
        for cid in self.catalog_ids:
            data, _ = self.http.get_json(self.base, {"type": 1, "pageNo": 1, "pageSize": self.page_size, "catalogId": cid})
            out.extend(self.parse(data, cid, now))
        return out

    @staticmethod
    def parse(data: dict[str, Any], catalog_id: int, seen_at: float) -> list[NewsItem]:
        d = data.get("data") or {}
        articles: list[dict[str, Any]] = list(d.get("articles") or [])
        for cat in d.get("catalogs") or []:
            articles.extend(cat.get("articles") or [])
        items = []
        for a in articles:
            title = a.get("title") or ""
            rel = a.get("releaseDate")
            items.append(NewsItem(
                id=f"binance:{a.get('id') or a.get('code')}",
                source=f"binance/catalog{catalog_id}",
                title=title,
                body="",
                url=f"https://www.binance.com/en/support/announcement/{a.get('code')}" if a.get("code") else "",
                published_at=(rel / 1000.0) if isinstance(rel, (int, float)) else None,
                seen_at=seen_at,
                symbols=tickers(title),
            ))
        return items


_STOPWORDS = {"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "BTC", "ETH", "BNB", "API", "USD", "EUR", "NEW", "ON", "AND", "THE", "AI", "P2P", "ETF", "FAQ", "APR", "APY"}


def feeds_from_env(env: dict[str, str] | None = None, http: Http | None = None) -> list[Feed]:
    env = env if env is not None else dict(os.environ)
    http = http or Http()
    feeds: list[Feed] = []
    tok = env.get("CRYPTOPANIC_TOKEN")
    if tok:
        cur = [c for c in env.get("CRYPTOPANIC_CURRENCIES", "").split(",") if c]
        feeds.append(CryptoPanicFeed(token=tok, currencies=cur, filter=env.get("CRYPTOPANIC_FILTER", "important"),
                                     base=env.get("CRYPTOPANIC_BASE", CryptoPanicFeed.base), http=http))
    if env.get("BINANCE_ANNOUNCEMENTS", "1") not in ("0", "false", "no"):
        feeds.append(BinanceAnnouncementsFeed(http=http))
    return feeds
