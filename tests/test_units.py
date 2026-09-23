from datetime import timedelta
from uuid import uuid4

import pytest

from collegium import scheduler
from collegium.acquisition import (
    Acquisition,
    Document,
    SearchResult,
    UnknownSource,
    clean_text,
)
from collegium.acquisition.hackernews import keywords
from collegium.grounding import locate_excerpt, unsupported_terms
from collegium.llm import extract_json
from collegium.roles.base import EvidenceItem, SearchPlan, Stance, ground_evidence
from collegium.roles.historian import decide, site

DOC = (
    "Acme AI  cut prices on Tuesday.\n\nThe company said inference costs for its "
    "“Frontier” model fell by 50% compared with last year, citing new hardware."
)


@pytest.mark.parametrize(
    "excerpt",
    [
        'inference costs for its "Frontier" model fell by 50% compared with last year',
        "INFERENCE COSTS for its Frontier model  fell by 50% compared with last year",
        "Inference costs for its Frontier model fell 50% compared to last year",
        '"inference costs for its Frontier model fell by 50% compared with last year"',
        "*inference costs for its Frontier model fell by 50% compared with last year*",
    ],
)
def test_excerpt_is_returned_in_the_sources_words(excerpt):
    assert locate_excerpt(excerpt, DOC) == (
        "inference costs for its “Frontier” model fell by 50% compared with last year"
    )


@pytest.mark.parametrize(
    "excerpt",
    [
        "The company said revenue tripled because of strong demand in Europe",
        "cut prices",  # too short to count as evidence
        "",
    ],
)
def test_excerpt_not_in_source_is_rejected(excerpt):
    assert locate_excerpt(excerpt, DOC) is None


def test_extract_json_ignores_reasoning_and_fences():
    reply = '<think>{"not": "this"}</think>\n```json\n{"queries": ["a"]}\n```'
    assert extract_json(reply) == '{"queries": ["a"]}'


def test_extract_json_rejects_prose():
    with pytest.raises(ValueError):
        extract_json("I could not find anything.")


def _critique(status, severity):
    return {"status": status, "severity": severity}


@pytest.mark.parametrize(
    ("verdict", "confidence", "sources", "critiques", "status"),
    [
        ("accept", 0.8, 2, [], "accepted"),
        ("accept", 0.8, 1, [], "under_review"),  # one publisher's word is not enough
        ("accept", 0.5, 3, [], "under_review"),  # the Skeptic is convinced, the numbers are not
        # The Skeptic's verdict is only a veto: undecided does not block.
        ("undecided", 0.9, 3, [], "accepted"),
        ("reject", 0.9, 3, [], "rejected"),
        ("undecided", 0.1, 3, [], "rejected"),
        ("accept", None, 3, [], "under_review"),
        # Serious critiques block while open or upheld, not once settled.
        ("undecided", 0.9, 3, [_critique("open", 3)], "under_review"),
        ("undecided", 0.9, 3, [_critique("upheld", 4)], "under_review"),
        ("undecided", 0.9, 3, [_critique("addressed", 4)], "accepted"),
        ("undecided", 0.9, 3, [_critique("dismissed", 5)], "accepted"),
        ("undecided", 0.9, 3, [_critique("open", 2)], "accepted"),  # a quibble
        ("undecided", 0.9, 3, [_critique("upheld", 5)], "rejected"),  # fatal
    ],
)
def test_historian_rules(verdict, confidence, sources, critiques, status):
    assert decide(verdict, confidence, sources, critiques) == status


def test_pages_on_one_site_count_as_one_source():
    assert site("https://www.reuters.com/a") == site("https://reuters.com/b") == "reuters.com"
    assert site("https://news.example/a") != site("https://research.example/b")


def test_scheduler_scouts_each_active_domain_once_per_interval(worker_db, add_domain):
    add_domain("ai-agents", "AI and agents")
    add_domain("energy", "Energy")
    assert scheduler.tick(worker_db, timedelta(hours=24)) == 2
    assert scheduler.tick(worker_db, timedelta(hours=24)) == 0
    with worker_db.reading() as conn:
        requested_by = conn.execute(
            "SELECT DISTINCT a.name FROM jobs j JOIN actors a ON a.id = j.requested_by"
        ).fetchall()
    assert [r["name"] for r in requested_by] == ["scheduler"]


def test_clean_text_keeps_link_text_and_drops_targets_and_images():
    page = (
        "![logo](/l.png)\n[Home](/) | [Blog](/blog)\n\n\n\n"
        "# Title\n\nPrices [fell](https://x.example) 50%."
    )
    assert clean_text(page) == "Home | Blog\n\n# Title\n\nPrices fell 50%."


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("web search query: 'ai training costs'", "ai training costs"),
        ("Search: AI prices -'$1,000 to $20,000'", "AI prices"),
        ('"enterprise AI adoption 2026"', "enterprise AI adoption 2026"),
        ("GPU rental prices -reddit", "GPU rental prices"),
    ],
)
def test_search_queries_are_made_plain(raw, clean):
    assert SearchPlan(queries=[raw]).queries == [clean]


def test_whole_page_excerpts_are_rejected():
    page = "A sentence about GPU prices that is long enough to count. " * 20
    assert locate_excerpt(page, page) is None


def test_evidence_citing_the_wrong_document_is_attributed_to_the_right_one():
    docs = [
        Document(url="https://a.example", title="A", content="Nothing relevant here at all."),
        Document(url="https://b.example", title="B", content=DOC),
    ]
    item = EvidenceItem(
        document=1,
        excerpt="inference costs for its Frontier model fell by 50%",
        summary="s",
        reliability=0.5,
        bears_on=[Stance(hypothesis="H1", stance="supports", rationale="r")],
    )
    grounding = ground_evidence([item], docs)
    assert [g.document.url for g in grounding.grounded] == ["https://b.example"]
    assert grounding.dropped == []


def test_list_markers_do_not_prevent_grounding():
    page = (
        "Key points:\n\n* The catch is that the system costs $100,000 per year.\n"
        "* Teams can opt out."
    )
    excerpt = "- The catch is that the system costs $100,000 per year.\n- Teams can opt out."
    assert locate_excerpt(excerpt, page) == (
        "The catch is that the system costs $100,000 per year.\n* Teams can opt out."
    )


class _Source:
    def __init__(self, name, urls, fail=False):
        self.name, self.urls, self.fail = name, urls, fail

    def discover(self, query, max_results, *, recent_days=None):
        if self.fail:
            raise RuntimeError("provider down")
        return [SearchResult(url=u, title=u, snippet="") for u in self.urls][:max_results]

    def extract(self, urls):
        return []


def test_acquisition_interleaves_sources_and_records_each_call():
    calls = []

    def record(capability, provider, request, count, error):
        calls.append((capability, provider, request["query"], count, error))
        return uuid4()

    a = _Source("a", ["https://1", "https://2", "https://shared"])
    b = _Source("b", ["https://shared", "https://3"])
    acquisition = Acquisition({"a": a, "b": b}, a, default="a").for_run(record)

    results = acquisition.discover("q", max_results=5, sources=["a", "b"])

    assert [r.url for r in results] == ["https://1", "https://shared", "https://2", "https://3"]
    assert [r.provider for r in results] == ["a", "b", "a", "b"]
    assert all(r.acquisition_id for r in results)
    assert calls == [("discover", "a", "q", 3, None), ("discover", "b", "q", 2, None)]


def test_failed_call_is_recorded_before_the_error_propagates():
    calls = []
    down = _Source("down", [], fail=True)
    acquisition = Acquisition({"down": down}, down, default="down").for_run(
        lambda *args: calls.append(args)
    )
    with pytest.raises(RuntimeError):
        acquisition.discover("q", max_results=5)
    assert calls[0][:2] == ("discover", "down")
    assert calls[0][4] == "RuntimeError: provider down"


def test_unknown_source_is_refused():
    a = _Source("a", [])
    with pytest.raises(UnknownSource):
        Acquisition({"a": a}, a, default="a").discover("q", max_results=5, sources=["nope"])


class _ThinSource(_Source):
    thin_leads = True

    def __init__(self, name, urls, pages, fail_extract=False):
        super().__init__(name, urls)
        self.pages, self.fail_extract, self.extracted = pages, fail_extract, []

    def discover(self, query, max_results, *, recent_days=None):
        return [SearchResult(url=u, title=u, snippet="10 points") for u in self.urls]

    def extract(self, urls):
        self.extracted.append(urls)
        if self.fail_extract:
            raise RuntimeError("extractor down")
        return [Document(url=u, title=u, content=self.pages[u]) for u in urls if u in self.pages]


def test_thin_leads_are_enriched_from_their_pages():
    urls = [f"https://{i}.example" for i in range(5)]
    thin = _ThinSource("hn", urls, {u: f"[Home](/)\n\nArticle {u} body." for u in urls})
    acquisition = Acquisition({"hn": thin}, thin, default="hn")

    results = acquisition.discover("q", max_results=5)

    assert thin.extracted == [urls[:3]]  # only the top ENRICH_TOP
    assert results[0].snippet == "Home\n\nArticle https://0.example body.\n10 points"
    assert results[4].snippet == "10 points"


def test_failed_enrichment_keeps_the_leads():
    thin = _ThinSource("hn", ["https://a.example"], {}, fail_extract=True)
    results = Acquisition({"hn": thin}, thin, default="hn").discover("q", max_results=5)
    assert [r.snippet for r in results] == ["10 points"]


def test_hacker_news_queries_are_reduced_to_keywords():
    assert keywords("recent developments in AI models") == "developments ai models"
    assert keywords("What's new with GPT-5.6 and C++?") == "gpt-5.6 c++"
    assert keywords("the latest") == "the latest"  # nothing left: keep the query


QUOTE = (
    "OpenAI CEO Sam Altman and Anthropic’s Dario Amodei urged the industry to slow "
    "down, citing 3 incidents in 2026 and 1,000 affected agents."
)


@pytest.mark.parametrize(
    ("statement", "missing"),
    [
        ("Anthropic's Dario Amodei cited 3 incidents.", []),
        ("An industry leader cited 3 incidents.", []),  # sentence-initial article
        ("OpenAI's Sam Altman mentioned 1000 affected agents.", []),
        (
            "Google CEO Sundar Pichai cited 5 incidents in 2025.",
            ["Google", "Sundar", "Pichai", "5", "2025"],
        ),
        # Wrong association of names that both occur: left to the model's check.
        ("Anthropic CEO Sam Altman urged a slowdown.", []),
    ],
)
def test_unsupported_terms(statement, missing):
    assert unsupported_terms(statement, QUOTE) == missing
