"""The research workflow end to end: scout -> research -> review -> record."""

from datetime import UTC, datetime

import psycopg
import pytest
from conftest import FakeProvider

from collegium import jobs, worker
from collegium.roles.base import EvidenceItem, SearchPlan, Stance
from collegium.roles.researcher import ProposedHypothesis, ResearchFindings
from collegium.roles.scout import ProposedObservation, ScoutReport
from collegium.roles.skeptic import ProposedCritique, SkepticReview

PRICE_CUT = "https://news.example/acme-price-cut"
ANALYSIS = "https://research.example/price-analysis"
PAGES = {
    PRICE_CUT: (
        "Acme AI cuts prices",
        "Acme AI announced new pricing on Tuesday. The company said inference costs for "
        "its “Frontier” model fell by 50% compared with last year, citing new "
        "hardware and better batching.",
    ),
    ANALYSIS: (
        "Are AI prices really falling?",
        "Independent analysts found that several recent price reductions were temporary "
        "promotional offers limited to new customers. List prices for enterprise contracts "
        "were largely unchanged.",
    ),
}
INDEPENDENT = "https://benchmarks.example/inference-costs"
PAGES[INDEPENDENT] = (
    "Measuring inference costs",
    "Our measurements across twelve providers show that the price of running frontier "
    "models fell by roughly half over the past twelve months.",
)
HYPOTHESIS = "Inference costs for frontier models roughly halve every year."


def script_scout(llm, *, investigate=True):
    llm.add(SearchPlan, SearchPlan(queries=["AI inference price changes"]))
    llm.add(
        ScoutReport,
        ScoutReport(
            observations=[
                ProposedObservation(
                    statement="Acme AI halved inference prices for its Frontier model.",
                    source=1,
                    occurred_at="2026-09-01",
                    why_it_matters="Could signal falling costs across the industry.",
                    investigate=investigate,
                ),
                ProposedObservation(  # cites a result that does not exist
                    statement="A made-up observation.",
                    source=42,
                    why_it_matters="None.",
                    investigate=True,
                ),
            ]
        ),
    )


def script_research(llm, *, refines=None, statement=HYPOTHESIS, confidence=0.5, independent=True):
    llm.add(SearchPlan, SearchPlan(queries=["Acme AI inference costs"]))
    llm.add(
        ResearchFindings,
        ResearchFindings(
            hypotheses=[
                ProposedHypothesis(
                    label="H1",
                    statement=statement,
                    rationale="Acme reports a 50% drop year on year.",
                    refines=refines,
                    confidence=confidence,
                )
            ],
            evidence=[
                EvidenceItem(  # lightly paraphrased: grounded to the source's wording
                    document=1,
                    excerpt="inference costs for its Frontier model fell 50% compared to last year",
                    summary="Acme reports a 50% annual fall in inference cost.",
                    reliability=0.6,
                    bears_on=[Stance(hypothesis="H1", stance="supports", rationale="Direct.")],
                ),
                EvidenceItem(  # invented: must be dropped
                    document=1,
                    excerpt="Every AI company has halved its prices every year since 2020.",
                    summary="Invented.",
                    reliability=0.9,
                    bears_on=[Stance(hypothesis="H1", stance="supports", rationale="x")],
                ),
                *(
                    [
                        EvidenceItem(  # a second, independent source
                            document=3,
                            excerpt="the price of running frontier models fell by roughly half",
                            summary="Independent measurements show prices halving.",
                            reliability=0.7,
                            bears_on=[Stance(hypothesis="H1", stance="supports", rationale="x")],
                        )
                    ]
                    if independent
                    else []
                ),
            ],
        ),
    )


def script_review(llm, *, verdict="accept", confidence=0.7):
    llm.add(SearchPlan, SearchPlan(queries=["AI price cuts temporary promotions"]))
    llm.add(
        SkepticReview,
        SkepticReview(
            critiques=[
                ProposedCritique(
                    argument="Evidence comes from one vendor's own announcement.",
                    alternative_explanation="Temporary promotional pricing.",
                    severity=3,
                )
            ],
            evidence=[
                EvidenceItem(
                    document=2,
                    excerpt="several recent price reductions were temporary promotional offers",
                    summary="Some price cuts were temporary promotions.",
                    reliability=0.7,
                    bears_on=[Stance(hypothesis="H", stance="contradicts", rationale="x")],
                )
            ],
            confidence=confidence,
            confidence_rationale="One strong source, one partial rebuttal.",
            verdict=verdict,
        ),
    )


def enqueue_scout(board_db, domain_id):
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "scout", {"domain_id": domain_id})


def test_full_workflow_accepts_a_grounded_hypothesis(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    script_review(llm)

    enqueue_scout(board_db, domain_id)
    assert worker.drain(ctx) == 4

    with worker_db.reading() as conn:
        job_rows = conn.execute("SELECT kind, status FROM jobs ORDER BY created_at").fetchall()
        assert [(j["kind"], j["status"]) for j in job_rows] == [
            ("scout", "succeeded"),
            ("research", "succeeded"),
            ("review", "succeeded"),
            ("record", "succeeded"),
        ]

        obs = conn.execute("SELECT statement, status FROM observations").fetchall()
        assert [o["statement"] for o in obs] == [
            "Acme AI halved inference prices for its Frontier model."
        ]
        assert obs[0]["status"] == "accepted"

        h = conn.execute("SELECT * FROM hypothesis_overview").fetchone()
        assert h["statement"] == HYPOTHESIS
        assert h["status"] == "accepted"
        assert float(h["current_confidence"]) == 0.7
        assert (h["supporting_evidence"], h["contradicting_evidence"]) == (2, 1)
        assert h["open_critiques"] == 1
        assert h["first_accepted_at"] is not None

        history = conn.execute(
            "SELECT c.confidence, a.name FROM confidence_assessments c "
            "JOIN actors a ON a.id = c.assessed_by ORDER BY c.assessed_at"
        ).fetchall()
        assert [(float(c["confidence"]), c["name"]) for c in history] == [
            (0.5, "researcher"),
            (0.7, "skeptic"),
        ]

        # Excerpts are the source's own words, and the invented one is gone.
        excerpts = [r["excerpt"] for r in conn.execute("SELECT excerpt FROM evidence")]
        assert sorted(excerpts) == sorted(
            [
                "inference costs for its “Frontier” model fell by 50% compared with last year",
                "several recent price reductions were temporary promotional offers",
                "the price of running frontier models fell by roughly half",
            ]
        )

        # Status changes are made by the Historian and nobody else.
        changes = conn.execute(
            "SELECT s.to_status, a.name FROM hypothesis_status_history s "
            "JOIN actors a ON a.id = s.actor_id WHERE s.from_status IS NOT NULL"
        ).fetchall()
        assert [(c["to_status"], c["name"]) for c in changes] == [("accepted", "historian")]

        runs = conn.execute(
            "SELECT a.name, r.status, r.model, r.role_version FROM runs r "
            "JOIN actors a ON a.id = r.actor_id ORDER BY r.started_at"
        ).fetchall()
        assert [(r["name"], r["status"], r["model"]) for r in runs] == [
            ("scout", "succeeded", "scripted"),
            ("researcher", "succeeded", "scripted"),
            ("skeptic", "succeeded", "scripted"),
            ("historian", "succeeded", None),
        ]
        assert all(r["role_version"] for r in runs)

        # Every knowledge row points at the run that produced it.
        unattributed = conn.execute(
            "SELECT count(*) AS n FROM nodes WHERE created_in_run IS NULL"
        ).fetchone()["n"]
        assert unattributed == 0

        scout_source = conn.execute(
            "SELECT s.metadata FROM observations o JOIN sources s ON s.id = o.source_id"
        ).fetchone()
        assert scout_source["metadata"]["provider"] == "fake"
        assert scout_source["metadata"]["query"] == "AI inference price changes"


def test_skeptic_rejection_is_recorded(make_context, llm, board_db, worker_db, add_domain):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    script_review(llm, verdict="reject", confidence=0.2)

    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        assert conn.execute("SELECT status FROM hypotheses").fetchone()["status"] == "rejected"


def test_undecided_review_leaves_hypothesis_under_review(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    script_review(llm, verdict="undecided", confidence=0.5)

    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        assert conn.execute("SELECT status FROM hypotheses").fetchone()["status"] == "under_review"


def test_accepted_refinement_supersedes_the_original(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    script_review(llm)
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    refined = "List prices for frontier inference halve yearly, but promotions exaggerate it."
    llm.add(SearchPlan, SearchPlan(queries=["enterprise AI list prices"]))
    llm.add(
        ScoutReport,
        ScoutReport(
            observations=[
                ProposedObservation(
                    statement="Analysts say recent AI price cuts were partly promotional.",
                    source=2,
                    why_it_matters="Challenges the accepted view on falling costs.",
                    investigate=True,
                )
            ]
        ),
    )
    script_research(llm, refines="E1", statement=refined, confidence=0.6)
    script_review(llm, confidence=0.75)
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    # The Researcher was shown the accepted hypothesis as E1.
    assert f"[E1] (accepted, confidence 0.70) {HYPOTHESIS}" in llm.prompts_for(ResearchFindings)[1]

    with worker_db.reading() as conn:
        rows = {
            r["statement"]: r
            for r in conn.execute("SELECT id, statement, status, superseded_by FROM hypotheses")
        }
        assert rows[refined]["status"] == "accepted"
        assert rows[HYPOTHESIS]["status"] == "superseded"
        assert rows[HYPOTHESIS]["superseded_by"] == rows[refined]["id"]
        link = conn.execute(
            "SELECT count(*) AS n FROM relationships WHERE predicate = 'refines' "
            "AND subject_id = %s AND object_id = %s",
            (rows[refined]["id"], rows[HYPOTHESIS]["id"]),
        ).fetchone()
        assert link["n"] == 1


def test_failed_job_is_retried_later_and_writes_nothing(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    llm.add(SearchPlan, RuntimeError("model server unavailable"))

    enqueue_scout(board_db, domain_id)
    assert worker.drain(ctx) == 2  # scout, then the failing research attempt

    with worker_db.reading() as conn:
        research = conn.execute("SELECT * FROM jobs WHERE kind = 'research'").fetchone()
        assert research["status"] == "pending"
        assert research["attempts"] == 1
        assert "model server unavailable" in research["last_error"]
        assert conn.execute("SELECT count(*) AS n FROM hypotheses").fetchone()["n"] == 0
        run = conn.execute("SELECT status FROM runs WHERE id = %s", (research["run_id"],))
        assert run.fetchone()["status"] == "failed"
        obs = conn.execute("SELECT status FROM observations").fetchone()
        assert obs["status"] == "investigating"


def test_observation_not_worth_investigating_stops_at_the_scout(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm, investigate=False)

    enqueue_scout(board_db, domain_id)
    assert worker.drain(ctx) == 1

    with worker_db.reading() as conn:
        assert conn.execute("SELECT status FROM observations").fetchone()["status"] == "proposed"


def test_scout_skips_what_it_has_already_observed(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm, investigate=False)
    script_scout(llm, investigate=False)

    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    assert "Acme AI halved inference prices" in llm.prompts_for(SearchPlan)[1]
    with worker_db.reading() as conn:
        assert conn.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 1


def test_hypothesis_without_grounded_support_is_not_stored(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    llm.add(SearchPlan, SearchPlan(queries=["Acme AI inference costs"]))
    llm.add(
        ResearchFindings,
        ResearchFindings(
            hypotheses=[
                ProposedHypothesis(label="H1", statement=HYPOTHESIS, rationale="r", confidence=0.8),
                ProposedHypothesis(
                    label="H2",
                    statement="Only an invented quote backs this.",
                    rationale="r",
                    confidence=0.9,
                ),
            ],
            evidence=[
                EvidenceItem(
                    document=1,
                    excerpt="inference costs for its Frontier model fell by 50%",
                    summary="s",
                    reliability=0.6,
                    bears_on=[Stance(hypothesis="H1", stance="supports", rationale="r")],
                ),
                EvidenceItem(
                    document=1,
                    excerpt="Every AI company has halved its prices every year since 2020.",
                    summary="s",
                    reliability=0.9,
                    bears_on=[Stance(hypothesis="H2", stance="supports", rationale="r")],
                ),
            ],
        ),
    )
    script_review(llm)
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        statements = [r["statement"] for r in conn.execute("SELECT statement FROM hypotheses")]
        assert statements == [HYPOTHESIS]
        notes = conn.execute(
            "SELECT r.notes FROM runs r JOIN actors a ON a.id = r.actor_id "
            "WHERE a.name = 'researcher'"
        ).fetchone()["notes"]
        assert "1 without grounded support dropped" in notes
        assert "Every AI company has halved" in notes


def test_single_source_is_not_enough_to_accept(make_context, llm, board_db, worker_db, add_domain):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm, independent=False)
    script_review(llm, verdict="accept", confidence=0.9)

    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        assert conn.execute("SELECT status FROM hypotheses").fetchone()["status"] == "under_review"
        notes = conn.execute(
            "SELECT r.notes FROM runs r JOIN actors a ON a.id = r.actor_id "
            "WHERE a.name = 'historian'"
        ).fetchone()["notes"]
        assert "supported by 1 independent sources: ['news.example']" in notes


def test_scout_searches_recent_news_and_drops_old_results(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    old, recent = PRICE_CUT, ANALYSIS
    web = FakeProvider(
        {old: PAGES[old], recent: PAGES[recent]},
        dates={old: "Mon, 02 Dec 2024 10:00:00 GMT", recent: datetime.now(UTC).isoformat()},
    )
    ctx = make_context(web=web)
    llm.add(SearchPlan, SearchPlan(queries=["AI price changes"]))
    llm.add(
        ScoutReport,
        ScoutReport(
            observations=[
                ProposedObservation(
                    statement="Analysts say recent AI price cuts were partly promotional.",
                    source=1,
                    why_it_matters="w",
                    investigate=False,
                )
            ]
        ),
    )
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    assert web.searches == [("AI price changes", 30)]
    # The 2024 page was never shown; result 1 is the recent one.
    scout_prompt = llm.prompts_for(ScoutReport)[0]
    assert old not in scout_prompt
    assert "[R1] Are AI prices really falling?" in scout_prompt
    with worker_db.reading() as conn:
        row = conn.execute(
            "SELECT s.uri, o.occurred_at FROM observations o JOIN sources s ON s.id = o.source_id"
        ).fetchone()
    assert row["uri"] == recent
    assert row["occurred_at"] is not None  # taken from the publication date


def test_repeated_hypothesis_strengthens_the_existing_one(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    script_review(llm, verdict="undecided", confidence=0.5)
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    # A new observation leads the Researcher to propose the same hypothesis
    # again, differing only in case, spacing and the final period.
    llm.add(SearchPlan, SearchPlan(queries=["AI prices"]))
    llm.add(
        ScoutReport,
        ScoutReport(
            observations=[
                ProposedObservation(
                    statement="Analysts measured inference prices across providers.",
                    source=3,
                    why_it_matters="w",
                    investigate=True,
                )
            ]
        ),
    )
    script_research(
        llm, statement="  inference costs for frontier models  roughly halve every year"
    )
    script_review(llm, verdict="undecided", confidence=0.55)
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        hypotheses = conn.execute("SELECT id FROM hypotheses").fetchall()
        assert len(hypotheses) == 1
        hid = hypotheses[0]["id"]
        derived = conn.execute(
            "SELECT count(*) AS n FROM relationships "
            "WHERE subject_id = %s AND predicate = 'derived_from'",
            (hid,),
        ).fetchone()["n"]
        assert derived == 2
        reviews = conn.execute(
            "SELECT count(*) AS n FROM jobs "
            "WHERE kind = 'review' AND payload->>'hypothesis_id' = %s",
            (str(hid),),
        ).fetchone()["n"]
        assert reviews == 2
        notes = conn.execute(
            "SELECT r.notes FROM runs r JOIN actors a ON a.id = r.actor_id "
            "WHERE a.name = 'researcher' ORDER BY r.started_at DESC LIMIT 1"
        ).fetchone()["notes"]
        assert "1 matched existing" in notes


def test_scout_uses_the_domains_discovery_sources_and_records_how_it_found_them(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    with board_db.acting_as("owner") as conn:
        conn.execute(
            "UPDATE domains SET discovery_sources = %s WHERE id = %s",
            (["fake", "hn"], domain_id),
        )
    hn = FakeProvider({INDEPENDENT: PAGES[INDEPENDENT]})
    ctx = make_context({PRICE_CUT: PAGES[PRICE_CUT]}, extra_sources={"hn": hn})
    llm.add(SearchPlan, SearchPlan(queries=["AI prices"]))
    llm.add(
        ScoutReport,
        ScoutReport(
            observations=[
                ProposedObservation(
                    statement="Measurements show frontier inference prices halved in a year.",
                    source=2,  # interleaved: R1 from fake, R2 from hn
                    why_it_matters="w",
                    investigate=True,
                )
            ]
        ),
    )
    script_research(llm)
    script_review(llm, verdict="undecided")
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    assert hn.searches == [("AI prices", 30)]
    with worker_db.reading() as conn:
        source = conn.execute(
            "SELECT s.uri, s.metadata, q.provider, q.capability, q.run_id, a.name AS actor "
            "FROM observations o JOIN sources s ON s.id = o.source_id "
            "JOIN acquisitions q ON q.id = s.acquisition_id "
            "JOIN actors a ON a.id = q.requested_by"
        ).fetchone()
        assert source["uri"] == INDEPENDENT
        assert source["metadata"]["provider"] == "hn"
        assert (source["provider"], source["capability"], source["actor"]) == (
            "hn",
            "discover",
            "scout",
        )
        assert source["run_id"] is not None

        calls = conn.execute(
            "SELECT a.name, q.capability, q.provider, q.request FROM acquisitions q "
            "JOIN actors a ON a.id = q.requested_by ORDER BY q.requested_at"
        ).fetchall()
        assert [(c["name"], c["capability"], c["provider"]) for c in calls] == [
            ("scout", "discover", "fake"),
            ("scout", "discover", "hn"),
            ("researcher", "discover", "fake"),
            ("researcher", "extract", "fake"),
            ("skeptic", "discover", "fake"),
            ("skeptic", "extract", "fake"),
        ]
        assert calls[0]["request"]["query"] == "AI prices"

        # Evidence sources also point at the call that found them.
        unlinked = conn.execute(
            "SELECT count(*) AS n FROM evidence e JOIN sources s ON s.id = e.source_id "
            "WHERE s.acquisition_id IS NULL"
        ).fetchone()["n"]
        assert unlinked == 0


def test_external_calls_are_kept_when_the_run_fails(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    llm.add(SearchPlan, SearchPlan(queries=["AI prices"]))
    llm.add(ScoutReport, RuntimeError("model crashed after searching"))
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        run = conn.execute("SELECT id, status FROM runs").fetchone()
        assert run["status"] == "failed"
        calls = conn.execute(
            "SELECT count(*) AS n FROM acquisitions WHERE run_id = %s", (run["id"],)
        ).fetchone()["n"]
        assert calls == 1


def test_only_the_board_sets_discovery_sources(worker_db, add_domain):
    domain_id = add_domain()
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        worker_db.acting_as("strategist") as conn,
    ):
        conn.execute("UPDATE domains SET discovery_sources = '{hn}' WHERE id = %s", (domain_id,))
