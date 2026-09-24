"""The board: every page renders from real memory, and outside text stays inert."""

from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from test_feeds import BROKEN, FEED, FakeCrawler
from test_pipeline import PAGES, enqueue_scout, script_research, script_review, script_scout

from collegium import board, memory, worker
from collegium.config import Settings
from collegium.db import Database
from collegium.web import create_app, http_url

BOARD = "http://testserver"


@pytest.fixture
def crawler():
    return FakeCrawler({FEED: []}, broken={BROKEN})


@pytest.fixture
def client(db_url, crawler):
    app = create_app(
        replace(Settings(), web_allowed_hosts="testserver"),
        lambda: Database(db_url, set_role="collegium_board"),
        crawler,
    )
    # Browsers send Origin with every form post; the board requires it.
    with TestClient(app, base_url=BOARD, headers={"Origin": BOARD}) as c:
        yield c


@pytest.fixture
def researched(make_context, llm, board_db, add_domain):
    """A domain taken once through scout, research and review."""
    domain_id = add_domain()
    script_scout(llm)
    script_research(llm)
    script_review(llm, verdict="undecided", severity=2)
    enqueue_scout(board_db, domain_id)
    worker.drain(make_context(PAGES))
    return domain_id


def _one(worker_db, sql):
    with worker_db.reading() as conn:
        return conn.execute(sql).fetchone()["id"]


def test_every_page_renders(client, researched, worker_db):
    hid = _one(worker_db, "SELECT id FROM hypotheses LIMIT 1")
    oid = _one(worker_db, "SELECT id FROM observations LIMIT 1")
    for path in [
        "/",
        "/programs",
        "/goals",
        "/goals?all=1",
        "/hypotheses",
        "/hypotheses?all=1",
        f"/hypotheses/{hid}",
        f"/observations/{oid}",
        "/discoveries",
        "/discoveries?days=30",
        "/operations",
        "/operations/live",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Content-Security-Policy" in response.headers


def test_hypothesis_page_explains_the_belief(client, researched, worker_db):
    with worker_db.reading() as conn:
        h = conn.execute("SELECT * FROM hypothesis_overview LIMIT 1").fetchone()
        critique = conn.execute("SELECT * FROM critiques WHERE target_id = %s", (h["id"],))
        critique = critique.fetchone()
        excerpt = memory.citations(conn, h["id"])[0]["excerpt"]
    page = client.get(f"/hypotheses/{h['id']}").text
    assert str(escape(h["statement"])) in page
    assert 'class="dot"' in page  # the confidence chart
    assert str(escape(critique["argument"])) in page
    assert "raised by the skeptic" in page
    assert str(escape(excerpt)) in page
    assert "Found by the" in page


def test_unknown_records_are_not_found(client):
    assert client.get("/hypotheses/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/hypotheses/not-a-uuid").status_code == 422


def test_outside_text_is_escaped_and_bad_links_are_not_links(client, worker_db, add_domain):
    add_domain()
    with worker_db.acting_as("scout") as conn:
        source = memory.record_source(
            conn, uri="javascript:alert(1)", title="<script>alert('title')</script>"
        )
        oid = memory.add_observation(
            conn, statement="<img src=x onerror=alert(1)>", source_id=source, recommendation=None
        )
        evidence = memory.add_evidence(
            conn,
            summary="A summary",
            excerpt="<script>alert('excerpt')</script>",
            source_id=source,
            reliability=0.5,
        )
        memory.link_evidence(
            conn,
            evidence_id=evidence,
            target_id=oid,
            target_kind="observation",
            stance="supports",
            rationale=None,
        )
    page = client.get(f"/observations/{oid}").text
    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;alert(&#39;excerpt&#39;)&lt;/script&gt;" in page
    assert 'href="javascript:' not in page


def test_only_web_addresses_are_linked():
    assert http_url("https://example.com/a") == "https://example.com/a"
    assert http_url("http://example.com") == "http://example.com"
    assert http_url("javascript:alert(1)") is None
    assert http_url("JaVaScRiPt:alert(1)") is None
    assert http_url("data:text/html,hi") is None
    assert http_url(None) is None


# ---------------------------------------------------------------------------
# The owner's actions
# ---------------------------------------------------------------------------


def test_other_host_names_are_refused(client):
    assert client.get("/", headers={"Host": "rebound.example"}).status_code == 400


def test_cross_site_posts_are_refused(client, add_domain, worker_db):
    add_domain()
    path = "/domains/ai-agents/status"
    for headers in [
        {"Origin": "https://evil.example"},
        {"Origin": "", "Sec-Fetch-Site": "cross-site"},
        {"Origin": BOARD, "Sec-Fetch-Site": "same-site"},
    ]:
        response = client.post(path, data={"status": "paused"}, headers=headers)
        assert response.status_code == 403, headers
    with worker_db.reading() as conn:
        assert conn.execute("SELECT status FROM domains").fetchone()["status"] == "active"
    # A post the browser marks as from the board itself is accepted.
    response = client.post(
        path, data={"status": "paused"}, headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert response.status_code == 200  # followed the redirect


def _proposed_program(worker_db, domain_id):
    with worker_db.acting_as("strategist") as conn:
        program = memory.add_program(conn, name="Inference economics", charter="Track costs.")
        decision = memory.add_decision(
            conn, statement="Open a program on inference economics", rationale="A gap."
        )
        memory.add_relationship(conn, subject_id=decision, predicate="concerns", object_id=program)
        memory.tag_domains(conn, program, [domain_id])
    return program, decision


def test_approving_a_decision_opens_its_program(client, worker_db, add_domain):
    program, decision = _proposed_program(worker_db, add_domain())
    assert "Open a program on inference economics" in client.get("/decisions").text
    response = client.post(f"/decisions/{decision}/approve")
    assert response.status_code == 200 and "Decision approved." in response.text
    with worker_db.reading() as conn:
        assert conn.execute(
            "SELECT p.status, d.status AS decided, a.name FROM programs p, decisions d "
            "JOIN actors a ON a.id = d.resolved_by WHERE p.id = %s AND d.id = %s",
            (program, decision),
        ).fetchone() == {"status": "active", "decided": "approved", "name": "owner"}
    # Deciding twice is refused, and says why.
    again = client.post(f"/decisions/{decision}/reject")
    assert again.status_code == 400 and "no longer waiting" in again.text


def test_rejecting_keeps_the_reason(client, worker_db, add_domain):
    program, decision = _proposed_program(worker_db, add_domain())
    client.post(f"/decisions/{decision}/reject", data={"reason": "Too early for this."})
    with worker_db.reading() as conn:
        assert memory.programs(conn, []) == []  # closed programs are not listed
        critique = conn.execute(
            "SELECT argument, status FROM critiques WHERE target_id = %s", (decision,)
        ).fetchone()
    assert critique == {"argument": "Too early for this.", "status": "upheld"}
    assert "Your reason: Too early for this." in client.get("/decisions").text


def test_owner_challenge_goes_through_the_loop(client, researched, worker_db, llm, make_context):
    from test_resolution import resolve, review

    from collegium.roles.skeptic import CritiqueResolution, SkepticReview

    hid = _one(worker_db, "SELECT id FROM hypotheses LIMIT 1")
    response = client.post(
        f"/hypotheses/{hid}/challenge",
        data={"argument": "These are list prices, not what buyers pay.", "severity": "3"},
    )
    assert response.status_code == 200 and "Your critique is recorded" in response.text
    with worker_db.reading() as conn:
        critique = conn.execute(
            "SELECT k.*, a.name FROM critiques k JOIN nodes n ON n.id = k.id "
            "JOIN actors a ON a.id = n.created_by WHERE k.target_id = %s AND a.name = 'owner'",
            (hid,),
        ).fetchone()
        job = conn.execute(
            "SELECT payload FROM jobs WHERE kind = 'resolve' AND status = 'pending'"
        ).fetchone()
    assert critique["status"] == "open" and critique["severity"] == 3
    assert job["payload"]["round"] == 1  # a fresh two rounds

    # The Skeptic's own earlier critique is C1; the owner's is C2.
    resolve(llm)
    review(
        llm,
        resolutions=[
            CritiqueResolution(critique="C1", status="addressed", resolution="Answered."),
            CritiqueResolution(
                critique="C2", status="dismissed", resolution="Buyers pay list prices here."
            ),
        ],
    )
    worker.drain(make_context(PAGES))
    brief = llm.prompts_for(SkepticReview)[-1]
    assert "[C2] (severity 3, raised by the owner) These are list prices" in brief
    with worker_db.reading() as conn:
        assert (
            conn.execute(
                "SELECT status FROM critiques WHERE id = %s", (critique["id"],)
            ).fetchone()["status"]
            == "dismissed"
        )
        # Settled, so the loop ends: no further round is queued.
        assert (
            conn.execute("SELECT count(*) AS n FROM jobs WHERE status = 'pending'").fetchone()["n"]
            == 0
        )

    page = client.get(f"/hypotheses/{hid}").text
    assert "your challenge" in page and "Buyers pay list prices here." in page
    overview = client.get("/").text
    assert "Your challenges" in overview and "Buyers pay list prices here." in overview


def test_a_challenge_needs_an_argument_and_a_live_hypothesis(client, researched, worker_db):
    hid = _one(worker_db, "SELECT id FROM hypotheses LIMIT 1")
    response = client.post(f"/hypotheses/{hid}/challenge", data={"argument": "  "})
    assert response.status_code == 400 and "Say what you object to" in response.text
    with worker_db.acting_as("historian") as conn:
        memory.set_hypothesis_status(conn, hid, "rejected")
    response = client.post(f"/hypotheses/{hid}/challenge", data={"argument": "No."})
    assert response.status_code == 400 and "rejected" in response.text
    with worker_db.reading() as conn:
        owner_critiques = conn.execute(
            "SELECT count(*) AS n FROM critiques k JOIN nodes n ON n.id = k.id "
            "JOIN actors a ON a.id = n.created_by WHERE a.name = 'owner'"
        ).fetchone()["n"]
    assert owner_critiques == 0


def test_managing_domains_and_feeds(client, worker_db, crawler):
    response = client.post("/domains", data={"slug": "energy", "name": "Energy"})
    assert response.status_code == 200 and "Domain added" in response.text
    duplicate = client.post("/domains", data={"slug": "energy", "name": "Energy again"})
    assert duplicate.status_code == 400 and "already a domain" in duplicate.text
    bad = client.post("/domains", data={"slug": "Not A Slug", "name": "x"})
    assert bad.status_code == 400 and "lowercase" in bad.text

    client.post("/domains/energy/sources", data={"source": ["hackernews"]})
    client.post("/domains/energy/feeds", data={"url": FEED, "title": "Lab news"})
    broken = client.post("/domains/energy/feeds", data={"url": BROKEN})
    assert broken.status_code == 400 and "Could not read" in broken.text
    client.post("/domains/energy/feeds/status", data={"url": FEED, "status": "paused"})
    client.post("/domains/energy/scout")
    unknown = client.post("/domains/energy/sources", data={"source": ["nowhere"]})
    assert unknown.status_code == 400

    with worker_db.reading() as conn:
        domain = memory.domain_by_slug(conn, "energy")
        feed = conn.execute("SELECT title, status FROM approved_sources").fetchone()
        job = conn.execute("SELECT kind, payload FROM jobs").fetchone()
    assert domain["discovery_sources"] == ["hackernews"]
    assert feed == {"title": "Lab news", "status": "paused"}
    assert job["kind"] == "scout" and job["payload"]["domain_id"] == str(domain["id"])
    assert crawler.crawled == [FEED, BROKEN]

    client.post("/domains/energy/status", data={"status": "paused"})
    refused = client.post("/domains/energy/strategize")
    assert refused.status_code == 400 and "resume it first" in refused.text
    page = client.get("/domains").text
    assert "Lab news" in page and "Resume" in page


def test_the_board_knows_the_sources_without_keys():
    from collegium.acquisition import (
        acquisition_from_settings,
        discovery_source_names,
        metered_provider_names,
    )

    settings = replace(Settings(), tavily_api_key="key")
    acquisition = acquisition_from_settings(settings)
    assert discovery_source_names(settings) == acquisition.sources
    assert metered_provider_names(settings) == acquisition.metered_providers


# ---------------------------------------------------------------------------
# Mission and contradictions
# ---------------------------------------------------------------------------


def test_missions_are_decisions_with_history(client, worker_db, add_domain, llm, make_context):
    from collegium.roles.strategist import StrategyPlan

    domain_id = add_domain()
    client.post("/mission", data={"statement": "Understand where AI is going."})
    client.post("/mission", data={"statement": "Understand AI's economics.", "domain": ""})
    response = client.post(
        "/mission", data={"statement": "Know what agents cost to run.", "domain": "ai-agents"}
    )
    assert response.status_code == 200 and "Mission set" in response.text
    same = client.post("/mission", data={"statement": "Understand AI's economics."})
    assert same.status_code == 400 and "already the mission" in same.text

    with worker_db.reading() as conn:
        organization = memory.missions(conn, None)
        domain = memory.missions(conn, domain_id)
    assert [m["statement"] for m in organization] == [
        "Understand AI's economics.",
        "Understand where AI is going.",
    ]
    assert [m["status"] for m in organization] == ["approved", "superseded"]
    assert organization[1]["superseded_by"] == organization[0]["id"]
    assert [m["statement"] for m in domain] == ["Know what agents cost to run."]

    page = client.get("/mission").text
    assert "Earlier missions (1)" in page and "Understand where AI is going." in page
    overview = client.get("/").text
    assert "Understand AI&#39;s economics." in overview
    assert "Know what agents cost to run." in overview

    # The Strategist plans by both.
    llm.add(StrategyPlan, StrategyPlan(assessment="a", goals=[]))
    with worker_db.acting_as("scheduler") as conn:
        from collegium import jobs

        jobs.enqueue(conn, "strategize", {"domain_id": domain_id})
    worker.drain(make_context(PAGES))
    brief = llm.prompts_for(StrategyPlan)[-1]
    assert "The organization's mission, set by the owner: Understand AI's economics." in brief
    assert "This domain's mission, set by the owner: Know what agents cost to run." in brief


def test_only_the_owner_decides(worker_db):
    import psycopg

    with (
        pytest.raises(psycopg.errors.RaiseException, match="only the owner may decide"),
        worker_db.acting_as("strategist") as conn,
    ):
        memory.set_mission(conn, "Whatever the Strategist likes.", None)
    with worker_db.acting_as("strategist") as conn:
        memory.add_decision(conn, statement="Proposed", rationale="r")  # proposing is fine


def test_contradictions_are_found(client, researched, worker_db):
    hid = _one(worker_db, "SELECT id FROM hypotheses LIMIT 1")
    with worker_db.acting_as("skeptic") as conn:
        evidence = conn.execute("SELECT id FROM evidence LIMIT 1").fetchone()["id"]
        memory.link_evidence(
            conn,
            evidence_id=evidence,
            target_id=hid,
            target_kind="hypothesis",
            stance="contradicts",
            rationale="r",
        )
        critique = memory.add_critique(
            conn,
            target_id=hid,
            argument="The measurements are from a single vendor.",
            alternative_explanation=None,
            severity=4,
        )
        memory.set_critique_status(conn, critique, "upheld", "No independent source was found.")
        rejected = memory.add_hypothesis(conn, statement="Agents replace analysts.", rationale="")
        memory.assess_confidence(
            conn, target_id=rejected, target_kind="hypothesis", confidence=0.4, rationale="r"
        )
    with worker_db.acting_as("researcher") as conn:
        memory.assess_confidence(
            conn, target_id=rejected, target_kind="hypothesis", confidence=0.7, rationale="r"
        )
    with worker_db.acting_as("historian") as conn:
        memory.set_hypothesis_status(conn, rejected, "rejected")

    with worker_db.reading() as conn:
        found = board.contradictions(conn)
    assert [h["id"] for h in found["contested"]] == [hid]
    assert [c["id"] for c in found["unsettled"]] == [critique]
    assert [h["id"] for h in found["overruled"]] == [rejected]
    assert found["overruled"][0]["researcher_confidence"] == Decimal("0.7")

    page = client.get("/contradictions").text
    assert "The measurements are from a single vendor." in page
    assert "Agents replace analysts." in page
    assert '<a href="/contradictions">3</a>' in client.get("/").text
