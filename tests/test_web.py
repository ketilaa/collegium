"""The board: every page renders from real memory, and outside text stays inert."""

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from test_pipeline import PAGES, enqueue_scout, script_research, script_review, script_scout

from collegium import memory, worker
from collegium.config import Settings
from collegium.db import Database
from collegium.web import create_app, http_url


@pytest.fixture
def client(db_url):
    app = create_app(Settings(), lambda: Database(db_url, set_role="collegium_board"))
    with TestClient(app) as c:
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
