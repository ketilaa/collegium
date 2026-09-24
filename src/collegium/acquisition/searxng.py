"""Web search through SearXNG, a metasearch engine the organization runs
itself (the `searxng` Compose service): free and without a quota.

SearXNG asks several engines at once and merges their results. Results are
leads with a short snippet; the acquisition layer enriches the best of them
from their pages, which the free web extractor reads.
"""

import httpx

from collegium.acquisition import SearchResult


def time_range(recent_days: int | None) -> str | None:
    """SearXNG's coarse time filter that covers `recent_days`."""
    if not recent_days:
        return None
    for days, name in ((1, "day"), (7, "week"), (31, "month")):
        if recent_days <= days:
            return name
    return "year"


class SearXNGDiscovery:
    name = "searxng"
    thin_leads = True

    def __init__(
        self, base_url: str, *, timeout: float = 30, transport: httpx.BaseTransport | None = None
    ):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def discover(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        params = {"q": query, "format": "json", "language": "all", "safesearch": 0}
        if window := time_range(recent_days):
            params["time_range"] = window
        response = self._client.get("/search", params=params)
        response.raise_for_status()
        results = []
        for hit in response.json().get("results", []):
            url = hit.get("url") or ""
            if not url.startswith(("http://", "https://")) or not hit.get("title"):
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=hit["title"],
                    snippet=hit.get("content") or "",
                    published_at=hit.get("publishedDate"),
                    score=hit.get("score"),
                    metadata={"engines": hit.get("engines", [])},
                )
            )
            if len(results) == max_results:
                break
        return results
