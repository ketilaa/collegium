"""Discovery through PRH's open data (avoindata.prh.fi), Finland's Business
Information System (YTJ): free, keyless, structured JSON, the same source
of truth ytj.fi itself reads from.

A hit is a registered legal entity, not a news item, the same as brreg.py:
there is no topic to rank by, so `query` is used as a name search (`name`),
which matches current names, previous names and auxiliary trade names.
This surfaces a real hit mainly when the Scout or Researcher is already
looking into a named company, not as a source of weak signals.

The API has no documented way to filter by industry or company form
server-side (unlike brreg's `naeringskode`/`organisasjonsform`), so the
narrowing brreg does with query parameters is done here after the fact:
only hits whose main business line falls in TOL 2008 division 62
(computer programming, consultancy and related activities -- the same
division NACE 62 covers; confirmed empirically against real companies,
e.g. Tietoevry returns "62030" and "62200") are kept. `recent_days`, when
given, narrows further to entities registered since then, the same
reasoning as brreg's `fraRegistreringsdatoEnhetsregisteret`.
"""

from datetime import UTC, datetime, timedelta

import httpx

from collegium.acquisition import SearchResult
from collegium.identity import USER_AGENT

API = "https://avoindata.prh.fi/opendata-ytj-api/v3"
LANDING = "https://tietopalvelu.ytj.fi/yritys/{}"
# TOL 2008 (Finland's NACE-aligned classification): division 62, computer
# programming, consultancy and related activities.
IT_DIVISION = "62"
_PREFERRED_LANGUAGE = "3"  # English, in this register's own language codes


class PRHDiscovery:
    name = "prh"
    # A name search, not a topic search: unlike brreg's own `navn`, this
    # API's `name` filter is a literal substring match, so a topical,
    # multi-word Scout query matches nothing almost by construction. See
    # name_search on brreg.py's BrregDiscovery.
    name_search = True

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
        params: dict[str, str | int] = {"name": query, "maxResults": max_results * 3}
        if recent_days:
            since = datetime.now(UTC) - timedelta(days=recent_days)
            params["registrationDateStart"] = since.date().isoformat()
        response = self._client.get("/companies", params=params)
        response.raise_for_status()
        hits = response.json().get("companies", [])
        leads = [lead for hit in hits if (lead := _lead(hit)) is not None]
        return leads[:max_results]


def _lead(hit: dict) -> SearchResult | None:
    business_id = (hit.get("businessId") or {}).get("value")
    line = hit.get("mainBusinessLine") or {}
    if not business_id or not line.get("type", "").startswith(IT_DIVISION):
        return None
    name = _current_name(hit.get("names") or [])
    if not name:
        return None
    return SearchResult(
        url=LANDING.format(business_id),
        title=name,
        snippet=_snippet(hit, line),
        published_at=(hit.get("businessId") or {}).get("registrationDate"),
        metadata={"prh_business_id": business_id},
    )


def _current_name(names: list[dict]) -> str | None:
    """The name with no end date is the one in force; registers keep
    every earlier one too, which is not what a lead's title should show."""
    for n in names:
        if not n.get("endDate"):
            return n.get("name")
    return names[0].get("name") if names else None


def _described(descriptions: list[dict]) -> str | None:
    by_language = {d.get("languageCode"): d.get("description") for d in descriptions}
    return by_language.get(_PREFERRED_LANGUAGE) or next(iter(by_language.values()), None)


def _snippet(hit: dict, line: dict) -> str:
    parts = []
    form = _described((hit.get("companyForms") or [{}])[0].get("descriptions") or [])
    industry = _described(line.get("descriptions") or [])
    if form and industry:
        parts.append(f"{form}: {industry}")
    else:
        parts.append(industry or form or "")
    offices = ((hit.get("addresses") or [{}])[0]).get("postOffices") or []
    by_language = {o.get("languageCode"): o.get("city") for o in offices}
    city = by_language.get(_PREFERRED_LANGUAGE) or next(iter(by_language.values()), None)
    if city:
        parts.append(f"Registered in {city.title()}")
    if since := (hit.get("businessId") or {}).get("registrationDate"):
        parts.append(f"since {since}")
    text = ". ".join(p for p in parts if p)
    if text:
        return text
    name = _current_name(hit.get("names") or []) or "this entity"
    return f"An entry in Finland's Business Information System (YTJ) for {name}"
