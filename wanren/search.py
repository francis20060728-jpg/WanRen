from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from .config import Settings


class WebSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchReference:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class WebSearchProvider(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[SearchReference]:
        ...


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "div" and "result" in classes:
            self.current = {}
        elif self.current is not None and tag == "a" and "result__a" in classes:
            self.current["url"] = attributes.get("href", "")
            self.capture = "title"
            self.current.setdefault("title", "")
        elif self.current is not None and tag == "a" and "result__snippet" in classes:
            self.capture = "snippet"
            self.current.setdefault("snippet", "")

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.capture:
            self.capture = None
        elif tag == "div" and self.current is not None:
            if self.current.get("title") and self.current.get("url"):
                self.results.append(self.current)
            self.current = None


class DuckDuckGoSearchProvider:
    name = "duckduckgo"

    def __init__(self, http_client: httpx.Client, timeout_seconds: float = 15) -> None:
        self.http = http_client
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, limit: int) -> list[SearchReference]:
        if not query.strip():
            raise WebSearchError("Search query is empty")
        response = self.http.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "WanRen-Template-Research/1.0"},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise WebSearchError(f"Search returned HTTP {response.status_code}")
        parser = _DuckDuckGoParser()
        try:
            parser.feed(response.text)
        except Exception as exc:
            raise WebSearchError(f"Search result parsing failed: {exc}") from exc
        references: list[SearchReference] = []
        seen_urls: set[str] = set()
        for item in parser.results:
            url = self._normalize_url(unescape(item.get("url", "")))
            title = " ".join(unescape(item.get("title", "")).split())
            snippet = " ".join(unescape(item.get("snippet", "")).split())
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            references.append(SearchReference(title=title, url=url, snippet=snippet))
            if len(references) >= max(1, limit):
                break
        if not references:
            raise WebSearchError("Search returned no usable references")
        return references

    @staticmethod
    def _normalize_url(raw_url: str) -> str:
        parsed = urlparse(raw_url)
        if parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            return target
        if raw_url.startswith("//"):
            return f"https:{raw_url}"
        return raw_url


def build_search_provider(settings: Settings) -> WebSearchProvider | None:
    if settings.search_provider == "disabled":
        return None
    return DuckDuckGoSearchProvider(
        httpx.Client(follow_redirects=True),
        timeout_seconds=settings.search_timeout_seconds,
    )
