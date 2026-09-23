"""Tavily adapter: discovery (web and news search) and extraction through one API."""

import httpx

from collegium.acquisition import Document, SearchResult

API = "https://api.tavily.com"


class TavilyProvider:
    name = "tavily"
    metered = True  # every call costs credits

    def __init__(self, api_key: str, *, timeout: float = 60, transport=None):
        self._client = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def discover(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        body = {"query": query, "max_results": max_results, "search_depth": "basic"}
        if recent_days:
            # Only the news topic returns publication dates and honours `days`.
            body |= {"topic": "news", "days": recent_days}
        response = self._client.post("/search", json=body)
        response.raise_for_status()
        return [
            SearchResult(
                url=r["url"],
                title=r.get("title") or r["url"],
                snippet=r.get("content") or "",
                published_at=r.get("published_date"),
                score=r.get("score"),
            )
            for r in response.json().get("results", [])
        ]

    def extract(self, urls: list[str]) -> list[Document]:
        if not urls:
            return []
        response = self._client.post("/extract", json={"urls": urls})
        response.raise_for_status()
        return [
            Document(url=r["url"], title=r["url"], content=r.get("raw_content") or "")
            for r in response.json().get("results", [])
        ]
