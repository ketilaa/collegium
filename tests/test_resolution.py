"""The critique-resolution loop and the budget for paid calls."""

from dataclasses import replace
from datetime import UTC, datetime

from conftest import FakeProvider
from test_pipeline import (
    PAGES,
    enqueue_scout,
    script_research,
    script_scout,
)

from collegium import worker
from collegium.acquisition import Acquisition, BudgetExhausted
from collegium.roles.base import EvidenceItem, SearchPlan, Stance
from collegium.roles.resolver import ResolutionFindings
from collegium.roles.skeptic import CritiqueResolution, ProposedCritique, SkepticReview

OBJECTION = "The price cuts may be temporary promotions rather than a trend."


def review(llm, *, critiques=(), resolutions=(), confidence=0.7, verdict="undecided"):
    llm.add(SearchPlan, SearchPlan(queries=["price cut promotions"]))
    llm.add(
        SkepticReview,
        SkepticReview(
            resolutions=list(resolutions),
            critiques=list(critiques),
            evidence=[],
            confidence=confidence,
            confidence_rationale="r",
            verdict=verdict,
        ),
    )


def resolve(llm, stance="contradicts"):
    llm.add(SearchPlan, SearchPlan(queries=["are AI price cuts permanent"]))
    llm.add(
        ResolutionFindings,
        ResolutionFindings(
            evidence=[
                EvidenceItem(
                    document=3,
                    excerpt="the price of running frontier models fell by roughly half",
                    summary="Measurements across providers show a lasting fall.",
                    reliability=0.7,
                    bears_on=[
                        Stance(hypothesis="C1", stance=stance, rationale="r"),
                        Stance(hypothesis="H", stance="supports", rationale="r"),
                    ],
                )
            ]
        ),
    )


def test_answered_critique_lets_the_hypothesis_be_accepted(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    review(llm, critiques=[ProposedCritique(argument=OBJECTION, severity=4)])
    resolve(llm)
    review(
        llm,
        resolutions=[
            CritiqueResolution(
                critique="C1", status="addressed", resolution="Measured across twelve providers."
            )
        ],
        confidence=0.75,
    )
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    # The Skeptic saw the critique with the evidence gathered about it.
    second_review = [u for s, u in llm.calls if s is SkepticReview][1]
    assert f"[C1] (severity 4) {OBJECTION}" in second_review
    assert "Evidence contradicts C1: Measurements across providers" in second_review

    with worker_db.reading() as conn:
        h = conn.execute("SELECT status FROM hypotheses").fetchone()
        assert h["status"] == "accepted"
        critique = conn.execute("SELECT status, resolution FROM critiques").fetchone()
        assert (critique["status"], critique["resolution"]) == (
            "addressed",
            "Measured across twelve providers.",
        )
        link = conn.execute(
            "SELECT stance FROM evidence_links WHERE target_kind = 'critique'"
        ).fetchone()
        assert link["stance"] == "contradicts"
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM jobs WHERE kind <> 'map' ORDER BY created_at, kind"
            )
        ]
        assert kinds == ["scout", "research", "review", "record", "resolve", "review", "record"]
        notes = [
            r["notes"]
            for r in conn.execute(
                "SELECT r.notes FROM runs r JOIN actors a ON a.id = r.actor_id "
                "WHERE a.name = 'historian' ORDER BY r.started_at"
            )
        ]
        assert "1 blocking critiques" in notes[0]
        assert "sent for critique resolution, round 1" in notes[0]
        assert notes[1].startswith("under_review -> accepted")


def test_upheld_critique_keeps_blocking_and_unsettled_ones_stop_after_max_rounds(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    review(llm, critiques=[ProposedCritique(argument=OBJECTION, severity=4)])
    for _ in range(2):  # two rounds, neither settles the critique
        resolve(llm, stance="context")
        review(llm)
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        assert conn.execute("SELECT status FROM hypotheses").fetchone()["status"] == "under_review"
        assert conn.execute("SELECT status FROM critiques").fetchone()["status"] == "open"
        assert (
            conn.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'resolve'").fetchone()["n"]
            == 2
        )
        last = conn.execute(
            "SELECT r.notes FROM runs r JOIN actors a ON a.id = r.actor_id "
            "WHERE a.name = 'historian' ORDER BY r.started_at DESC LIMIT 1"
        ).fetchone()["notes"]
        assert "critiques still open after 2 rounds" in last


def test_later_rounds_add_at_most_one_new_critique(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm)
    review(llm, critiques=[ProposedCritique(argument=OBJECTION, severity=4)])
    resolve(llm)
    review(
        llm,
        resolutions=[CritiqueResolution(critique="C1", status="upheld", resolution="Stands.")],
        critiques=[ProposedCritique(argument=f"New objection {i}", severity=2) for i in range(3)],
    )
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)

    with worker_db.reading() as conn:
        rows = conn.execute("SELECT argument, status FROM critiques ORDER BY argument").fetchall()
        assert [(r["argument"], r["status"]) for r in rows] == [
            ("New objection 0", "open"),
            (OBJECTION, "upheld"),
        ]
        # An upheld serious critique blocks acceptance but is not re-investigated.
        assert conn.execute("SELECT status FROM hypotheses").fetchone()["status"] == "under_review"
        assert (
            conn.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'resolve'").fetchone()["n"]
            == 1
        )


class _Paid(FakeProvider):
    metered = True


def test_paid_calls_stop_when_the_budget_is_spent():
    paid = _Paid({"https://a.example": ("A", "text")})
    free = FakeProvider({"https://b.example": ("B", "text")})
    acquisition = Acquisition({"paid": paid, "free": free}, paid, default="paid").for_run(
        lambda *a: None, budget=lambda: 0
    )
    # "paid" as registered for discovery; "fake" is its own name as the extractor.
    assert acquisition.metered_providers == ["fake", "paid"]
    assert acquisition.discover("q", max_results=5, sources=["free"])  # free still works
    try:
        acquisition.discover("q", max_results=5, sources=["paid"])
        raise AssertionError("expected BudgetExhausted")
    except BudgetExhausted:
        pass


def test_job_is_deferred_without_using_an_attempt_when_the_budget_is_spent(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    paid = _Paid(PAGES)
    ctx = make_context(web=paid)
    ctx = replace(ctx, settings=replace(ctx.settings, daily_call_budget=2))
    # Two paid calls already made today by an earlier run.
    with worker_db.acting_as("scout") as conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO acquisitions (capability, provider, request, result_count) "
                "VALUES ('discover', 'fake', '{}', 1)"
            )
    enqueue_scout(board_db, domain_id)
    assert worker.drain(ctx) == 1  # claimed and deferred

    with worker_db.reading() as conn:
        job = conn.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "pending"
        assert job["attempts"] == 0
        assert job["run_after"] > datetime.now(UTC)
        assert "budget" in job["last_error"]
        assert conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0
    assert not llm.calls
