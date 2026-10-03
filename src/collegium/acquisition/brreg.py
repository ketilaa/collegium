"""Discovery through Brønnøysundregistrene (brreg.no), Norway's central
business register: free, keyless, structured JSON.

A hit is a registered legal entity, not a news item: there is no topic to
search for the way NVA's index ranks publications by relevance, so `query`
is used as a name search (`navn`), narrowed to the forms of company that
can be an IT consultancy (`AS`, `ASA`, `NUF`: not a sole proprietorship)
and to the industry codes that cover programming, IT consultancy, systems
operation and other IT services. This surfaces a real hit mainly when the
Scout or Researcher is already looking into a named company; it is not a
source of weak signals the way a news search is. `recent_days`, when
given, narrows further to entities registered since then, which can
surface a genuinely new entrant sharing the queried name's wording (a
subsidiary, a rebrand) rather than every company ever registered under it.
"""

import re
from datetime import UTC, datetime, timedelta

import httpx

from collegium.acquisition import SearchResult
from collegium.identity import USER_AGENT

API = "https://data.brreg.no/enhetsregisteret/api"
LANDING = "https://virksomhet.brreg.no/nb/oppslag/enheter/{}"
# Division 62 (SN2007, zero-padded to 3 digits in this register): computer
# programming, consultancy, systems operation and other IT services.
NACE_CODES = "62.100,62.200,62.300,62.900"
# Real companies, not a sole proprietor's one-person registration.
ORG_FORMS = "AS,ASA,NUF"
_ORG_NUMBER = re.compile(r"^\d{9}$")


class BrregDiscovery:
    name = "brreg"

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
        params: dict[str, str | int] = {
            "navn": query,
            "naeringskode": NACE_CODES,
            "organisasjonsform": ORG_FORMS,
            "size": max_results,
        }
        if recent_days:
            since = datetime.now(UTC) - timedelta(days=recent_days)
            params["fraRegistreringsdatoEnhetsregisteret"] = since.date().isoformat()
        response = self._client.get("/enheter", params=params)
        response.raise_for_status()
        hits = (response.json().get("_embedded") or {}).get("enheter", [])
        return [lead for hit in hits if (lead := _lead(hit)) is not None]


def _lead(hit: dict) -> SearchResult | None:
    org_number = hit.get("organisasjonsnummer")
    name = hit.get("navn")
    if not name or not org_number or not _ORG_NUMBER.match(org_number):
        return None
    return SearchResult(
        url=LANDING.format(org_number),
        title=name,
        snippet=_snippet(hit),
        published_at=hit.get("registreringsdatoEnhetsregisteret"),
        metadata={"brreg_org_number": org_number},
    )


def _snippet(hit: dict) -> str:
    parts = []
    form = (hit.get("organisasjonsform") or {}).get("beskrivelse")
    industry = (hit.get("naeringskode1") or {}).get("beskrivelse")
    if form and industry:
        parts.append(f"{form}: {industry}")
    else:
        parts.append(industry or form or "")
    if municipality := (hit.get("forretningsadresse") or {}).get("kommune"):
        parts.append(f"Registered in {municipality.title()}")
    if since := hit.get("registreringsdatoEnhetsregisteret"):
        parts.append(f"since {since}")
    if hit.get("harRegistrertAntallAnsatte") and hit.get("antallAnsatte") is not None:
        parts.append(f"{hit['antallAnsatte']} registered employees")
    if hit.get("konkurs"):
        parts.append("in bankruptcy proceedings")
    if hit.get("underAvvikling"):
        parts.append("under liquidation")
    text = ". ".join(p for p in parts if p)
    if text:
        return text
    return f"An entry in Brønnøysundregistrene, Norway's business register, for {hit['navn']}"
