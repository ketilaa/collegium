"""Discovery through NVA (Sikt/Cristin), Norway's archive of publicly
funded research: free, keyless, structured JSON.

A hit is a publication record. Its landing page (nva.sikt.no) is a
client-rendered app the free reader gets little from, and its full text is
often a large PDF; rather than lead the Scout to either, each hit's
abstract is fetched directly (a small JSON call the search index itself
does not carry) and put in the snippet, so grounding rests on the
author's own summary, not a multi-megabyte file. The lead's other
metadata (title, contributors, venue, date, DOI) is a real, quotable fact
in itself, kept as a trailing line, for the (rarer) hit with no abstract.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from collegium.acquisition import SearchResult
from collegium.identity import USER_AGENT

API = "https://api.nva.unit.no"
LANDING = "https://nva.sikt.no/registration/{}"
# An abstract is a paragraph or two; this keeps the Scout's prompt bounded
# without cutting off the finding itself mid-sentence too often.
ABSTRACT_CHARS = 700


class NVADiscovery:
    name = "nva"
    # For the rarer hit with no abstract, the acquisition layer enriches a
    # snippet that stays short despite the metadata line.
    thin_leads = True

    def __init__(self, *, timeout: float = 30, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=API,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def discover(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        params: dict[str, str | int] = {"query": query, "size": max_results}
        if recent_days:
            since = datetime.now(UTC) - timedelta(days=recent_days)
            params["published_since"] = since.date().isoformat()
        response = self._client.get("/search/resources", params=params)
        response.raise_for_status()
        leads = []
        for hit in response.json().get("hits", []):
            if (lead := _lead(hit)) is not None:
                leads.append(self._with_abstract(lead))
        return leads

    def _with_abstract(self, lead: SearchResult) -> SearchResult:
        """The publication's own abstract, ahead of the metadata line, when
        the record has one: a small JSON call, never the PDF itself."""
        try:
            response = self._client.get(f"/publication/{lead.metadata['nva_identifier']}")
            response.raise_for_status()
            abstract = (response.json().get("entityDescription") or {}).get("abstract")
        except Exception:
            return lead
        if not abstract:
            return lead
        text = " ".join(abstract.split())[:ABSTRACT_CHARS]
        return replace(lead, snippet=f"{text}\n{lead.snippet}")


def _lead(hit: dict) -> SearchResult | None:
    identifier = hit.get("identifier")
    description = hit.get("entityDescription") or {}
    title = description.get("mainTitle")
    if not identifier or not title:
        return None
    reference = description.get("reference") or {}
    return SearchResult(
        url=LANDING.format(identifier),
        title=title,
        snippet=_snippet(description, reference),
        published_at=_published(description.get("publicationDate")),
        metadata={
            "nva_identifier": identifier,
            **({"doi": reference["doi"]} if reference.get("doi") else {}),
        },
    )


def _snippet(description: dict, reference: dict) -> str:
    parts = []
    names = [c.get("identity", {}).get("name") for c in description.get("contributors") or []]
    names = [n for n in names if n]
    if names:
        parts.append(", ".join(names[:3]) + (" et al." if len(names) > 3 else ""))
    if venue := _venue(reference):
        parts.append(venue)
    if kind := (reference.get("publicationInstance") or {}).get("type"):
        parts.append(_LABELS.get(kind, kind))
    return ". ".join(parts) or "A publication in NVA, Norway's archive of publicly funded research."


def _venue(reference: dict) -> str | None:
    context = reference.get("publicationContext") or {}
    return context.get("name") or (context.get("agent") or {}).get("name")


def _published(date: dict | None) -> str | None:
    year = (date or {}).get("year")
    if not year:
        return None
    month = str(date.get("month") or 1).zfill(2)
    day = str(date.get("day") or 1).zfill(2)
    return f"{year}-{month}-{day}"


# A few common types, worded for a reader rather than the API's own labels.
_LABELS = {
    "AcademicArticle": "journal article",
    "AcademicMonograph": "monograph",
    "AcademicChapter": "book chapter",
    "ConferenceAbstract": "conference abstract",
    "ConferenceLecture": "conference talk",
    "OtherPresentation": "presentation",
    "ReportBasic": "report",
    "DegreeMaster": "master's thesis",
    "DegreePhd": "doctoral thesis",
}
