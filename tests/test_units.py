from datetime import timedelta

import pytest

from collegium import scheduler
from collegium.acquisition import Document, clean_text
from collegium.grounding import locate_excerpt
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


@pytest.mark.parametrize(
    ("verdict", "confidence", "sources", "status"),
    [
        ("accept", 0.8, 2, "accepted"),
        ("accept", 0.8, 1, "under_review"),  # one publisher's word is not enough
        ("accept", 0.5, 3, "under_review"),  # the Skeptic is convinced, the numbers are not
        ("undecided", 0.9, 3, "under_review"),
        ("reject", 0.9, 3, "rejected"),
        ("undecided", 0.1, 3, "rejected"),
        ("accept", None, 3, "under_review"),
    ],
)
def test_historian_rules(verdict, confidence, sources, status):
    assert decide(verdict, confidence, sources) == status


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
