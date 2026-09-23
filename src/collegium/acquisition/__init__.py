"""Knowledge acquisition: how the organization reads the outside world.

Three capabilities, each behind a protocol:

- discovery finds leads for a query (a search engine, Hacker News, ...);
- crawling reads leads from a source the owner approved (a feed);
- extraction reads the pages leads point to.

Roles use `Acquisition`, which holds the named discovery providers, a
crawler and one extractor, and never a vendor directly. It can record every external call,
so the organization knows how it found each source and what it has
revealed to providers.

All text from outside is sanitized here, at the boundary (see `untrusted`):
control tokens and hidden characters are removed, and injection signals are
kept in the item's metadata under "injection_signals".
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID

from collegium.config import Settings, require
from collegium.untrusted import sanitize


@dataclass(frozen=True)
class SearchResult:
    """A lead: something a discovery provider found."""

    url: str
    title: str
    snippet: str
    published_at: str | None = None
    score: float | None = None
    provider: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    acquisition_id: UUID | None = None  # the recorded call that found it


@dataclass(frozen=True)
class Document:
    url: str
    title: str
    content: str
    published_at: str | None = None
    provider: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    acquisition_id: UUID | None = None  # the recorded call that found it


class Discovery(Protocol):
    name: str

    def discover(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        """Find leads. With recent_days, only from that many days back."""
        ...


class Extractor(Protocol):
    name: str

    def extract(self, urls: list[str]) -> list[Document]: ...


class Crawler(Protocol):
    name: str

    def crawl(self, url: str, max_items: int) -> list[SearchResult]: ...


# Leads with less text than this are enriched from the page they point to,
# up to ENRICH_TOP per call, so the model can judge them on content rather
# than on a headline.
THIN_SNIPPET_CHARS = 200
ENRICH_TOP = 3
ENRICHED_SNIPPET_CHARS = 600

# (capability, provider, request, result_count, error) -> acquisition id
Recorder = Callable[[str, str, dict[str, Any], int, str | None], UUID | None]


class UnknownSource(ValueError):
    pass


class Acquisition:
    def __init__(
        self,
        discovery: Mapping[str, Discovery],
        extractor: Extractor,
        *,
        default: str,
        crawler: Crawler | None = None,
        recorder: Recorder | None = None,
    ):
        if default not in discovery:
            raise UnknownSource(default)
        self._discovery = dict(discovery)
        self._extractor = extractor
        self._crawler = crawler
        self.default = default
        self._recorder = recorder

    @property
    def sources(self) -> list[str]:
        return sorted(self._discovery)

    def with_recorder(self, recorder: Recorder) -> "Acquisition":
        return Acquisition(
            self._discovery,
            self._extractor,
            default=self.default,
            crawler=self._crawler,
            recorder=recorder,
        )

    def discover(
        self,
        query: str,
        *,
        max_results: int,
        recent_days: int | None = None,
        sources: list[str] | None = None,
    ) -> list[SearchResult]:
        """Leads from each source, interleaved so no source crowds out the
        others. With no sources given, the default one is used."""
        per_source = []
        for name in sources or [self.default]:
            provider = self._discovery.get(name)
            if provider is None:
                raise UnknownSource(name)
            request = {"query": query, "max_results": max_results, "recent_days": recent_days}
            results, acquisition_id = self._call(
                "discover",
                name,
                request,
                lambda p=provider: p.discover(query, max_results, recent_days=recent_days),
            )
            results = [
                _clean_lead(replace(r, provider=name, acquisition_id=acquisition_id))
                for r in results
            ]
            if getattr(provider, "thin_leads", False):
                results = self._enrich(results)
            per_source.append(results)
        return interleave(per_source)

    def crawl(self, url: str, *, max_items: int) -> list[SearchResult]:
        """Leads from an approved source, such as the newest items of a feed."""
        if self._crawler is None:
            raise UnknownSource("no crawler configured")
        crawler = self._crawler
        results, acquisition_id = self._call(
            "crawl",
            crawler.name,
            {"url": url, "max_items": max_items},
            lambda: crawler.crawl(url, max_items),
        )
        results = [
            _clean_lead(replace(r, provider=crawler.name, acquisition_id=acquisition_id))
            for r in results
        ]
        return self._enrich(results) if getattr(crawler, "thin_leads", False) else results

    def _enrich(self, results: list[SearchResult]) -> list[SearchResult]:
        """Prefix the top thin leads with the opening of their page. A failed
        extraction leaves the leads as they were; it is recorded anyway."""
        thin = [r.url for r in results if len(r.snippet) < THIN_SNIPPET_CHARS][:ENRICH_TOP]
        try:
            pages = {d.url: d for d in self.extract(thin)}
        except Exception:
            return results
        enriched = []
        for r in results:
            page = pages.get(r.url)
            text = clean_text(page.content)[:ENRICHED_SNIPPET_CHARS] if page else ""
            if text:
                r = replace(
                    r,
                    snippet=f"{text}\n{r.snippet}",
                    metadata=with_signals(r.metadata, injection_signals(page)),
                )
            enriched.append(r)
        return enriched

    def extract(self, urls: list[str]) -> list[Document]:
        if not urls:
            return []
        documents, _ = self._call(
            "extract", self._extractor.name, {"urls": urls}, lambda: self._extractor.extract(urls)
        )
        return [_clean_document(d) for d in documents]

    def gather(
        self,
        queries: list[str],
        *,
        max_results: int,
        max_documents: int,
        max_chars: int,
    ) -> list[Document]:
        """Discover with the default source for each query, then extract the
        best distinct results.

        Documents are cleaned and truncated to max_chars so they fit a local
        model's context. Results that cannot be extracted fall back to their
        snippet.
        """
        best: dict[str, SearchResult] = {}
        for query in queries:
            for result in self.discover(query, max_results=max_results):
                current = best.get(result.url)
                if current is None or (result.score or 0) > (current.score or 0):
                    best[result.url] = result
        chosen = sorted(best.values(), key=lambda r: r.score or 0, reverse=True)[:max_documents]
        extracted = {d.url: d for d in self.extract([r.url for r in chosen])}
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
                    provider=result.provider,
                    metadata=with_signals(result.metadata, injection_signals(doc) if doc else []),
                    acquisition_id=result.acquisition_id,
                )
            )
        return documents

    def _call(self, capability: str, provider: str, request: dict[str, Any], fn):
        try:
            result = fn()
        except Exception as e:
            self._record(capability, provider, request, 0, f"{type(e).__name__}: {e}")
            raise
        return result, self._record(capability, provider, request, len(result), None)

    def _record(self, capability, provider, request, count, error) -> UUID | None:
        if self._recorder is None:
            return None
        return self._recorder(capability, provider, request, count, error)


def injection_signals(item: SearchResult | Document) -> list[str]:
    return list(item.metadata.get("injection_signals", []))


def with_signals(metadata: Mapping[str, Any], signals: list[str]) -> Mapping[str, Any]:
    if not signals:
        return metadata
    merged = sorted(set(metadata.get("injection_signals", [])) | set(signals))
    return {**metadata, "injection_signals": merged}


def _clean_lead(r: SearchResult) -> SearchResult:
    title, in_title = sanitize(r.title)
    snippet, in_snippet = sanitize(r.snippet)
    return replace(
        r, title=title, snippet=snippet, metadata=with_signals(r.metadata, in_title + in_snippet)
    )


def _clean_document(d: Document) -> Document:
    title, in_title = sanitize(d.title)
    content, in_content = sanitize(d.content)
    return replace(
        d, title=title, content=content, metadata=with_signals(d.metadata, in_title + in_content)
    )


def interleave(lists: list[list[SearchResult]]) -> list[SearchResult]:
    """Round-robin across lists, dropping repeated urls."""
    seen: set[str] = set()
    merged = []
    for i in range(max((len(x) for x in lists), default=0)):
        for results in lists:
            if i < len(results) and results[i].url not in seen:
                seen.add(results[i].url)
                merged.append(results[i])
    return merged


def acquisition_from_settings(settings: Settings) -> Acquisition:
    from collegium.acquisition.feeds import FeedReader
    from collegium.acquisition.hackernews import HackerNewsDiscovery

    discovery: dict[str, Discovery] = {"hackernews": HackerNewsDiscovery()}
    if settings.search_provider == "tavily":
        from collegium.acquisition.tavily import TavilyProvider

        tavily = TavilyProvider(require(settings.tavily_api_key, "TAVILY_API_KEY"))
        discovery["tavily"] = tavily
        extractor: Extractor = tavily
    else:
        raise SystemExit(f"unknown COLLEGIUM_SEARCH_PROVIDER {settings.search_provider!r}")
    return Acquisition(discovery, extractor, default=settings.search_provider, crawler=FeedReader())


_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Page text without markdown images and link targets, which fill a
    small model's context with navigation rather than content."""
    text = _LINK.sub(r"\1", _IMAGE.sub("", text))
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()
