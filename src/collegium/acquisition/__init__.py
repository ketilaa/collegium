"""Knowledge acquisition: how the organization reads the outside world.

Roles depend on the `AcquisitionProvider` capabilities, never on a vendor.
Vendor adapters live in this package and are chosen by configuration.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from collegium.config import Settings, require


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    published_at: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class Document:
    url: str
    title: str
    content: str
    published_at: str | None = None


class AcquisitionProvider(Protocol):
    name: str

    def search(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        """Search the web. With recent_days, only news from that many days."""
        ...

    def extract(self, urls: list[str]) -> list[Document]: ...


def provider_from_settings(settings: Settings) -> AcquisitionProvider:
    if settings.search_provider == "tavily":
        from collegium.acquisition.tavily import TavilyProvider

        return TavilyProvider(require(settings.tavily_api_key, "TAVILY_API_KEY"))
    raise SystemExit(f"unknown COLLEGIUM_SEARCH_PROVIDER {settings.search_provider!r}")


def gather(
    provider: AcquisitionProvider,
    queries: list[str],
    *,
    max_results: int,
    max_documents: int,
    max_chars: int,
) -> list[Document]:
    """Search each query, then extract the best distinct results.

    Documents are truncated to max_chars so they fit a local model's context.
    Results that cannot be extracted fall back to their search snippet.
    """
    best: dict[str, SearchResult] = {}
    for query in queries:
        for result in provider.search(query, max_results):
            current = best.get(result.url)
            if current is None or (result.score or 0) > (current.score or 0):
                best[result.url] = result
    chosen = sorted(best.values(), key=lambda r: r.score or 0, reverse=True)[:max_documents]
    if not chosen:
        return []
    extracted = {d.url: d for d in provider.extract([r.url for r in chosen])}
    documents = []
    for result in chosen:
        doc = extracted.get(result.url)
        content = doc.content if doc and doc.content.strip() else result.snippet
        documents.append(
            Document(
                url=result.url,
                title=result.title,
                content=clean_text(content)[:max_chars],
                published_at=result.published_at,
            )
        )
    return documents


_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Page text without markdown images and link targets, which fill a
    small model's context with navigation rather than content."""
    text = _LINK.sub(r"\1", _IMAGE.sub("", text))
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()
