"""Stdlib HTTP for the ingestion loops. No third party client.

One function, `get_json`, plus `get_text` for RSS. Retries on 5xx and network
errors with exponential backoff. Returns the parsed body and the response
headers, because the-odds-api reports quota in headers.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    text: str
    elapsed_s: float

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass
class Http:
    timeout_s: float = 10.0
    retries: int = 3
    backoff_s: float = 1.0
    user_agent: str = "jev-agents/0.1 (+ingestion)"
    default_headers: dict[str, str] = field(default_factory=dict)
    sleep: Any = time.sleep

    def get(self, url: str, params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None) -> Response:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json, text/xml, */*"}
        hdrs.update(self.default_headers)
        if headers:
            hdrs.update(headers)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    text = r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
                    return Response(r.status, dict(r.headers.items()), text, time.monotonic() - t0)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                if e.code >= 500 or e.code == 429:
                    last = HttpError(e.code, url, body)
                else:
                    raise HttpError(e.code, url, body) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            if attempt < self.retries:
                self.sleep(self.backoff_s * (2 ** attempt))
        assert last is not None
        raise last

    def get_json(self, url: str, params: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> tuple[Any, Mapping[str, str]]:
        r = self.get(url, params, headers)
        return r.json(), r.headers

    def get_text(self, url: str, params: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> str:
        return self.get(url, params, headers).text
