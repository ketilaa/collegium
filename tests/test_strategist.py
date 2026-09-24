"""The Strategist: gap detection, goals, budgeted actions and proposals."""

from dataclasses import replace

import pytest
from conftest import FakeProvider
from test_pipeline import (
    HYPOTHESIS,
    INDEPENDENT,
    PAGES,
    enqueue_scout,
    script_research,
    script_review,
    script_scout,
)

from collegium import cli, jobs, memory, strategy, worker
from collegium.roles.base import EvidenceItem, SearchPlan, Stance
from collegium.roles.mapper import EntityMap, MentionedEntity
from collegium.roles.resolver import ResolutionFindings
from collegium.roles.scout import ScoutReport
from collegium.roles.strategist import (
    MAX_FREE_ACTIONS,
    Action,
    GoalAbandonment,
    PlannedGoal,
    ProgramProposal,
    StrategyPlan,
)


@pytest.fixture
def domain_with_weak_hypothesis(make_context, llm, board_db, worker_db, add_domain):
    """One hypothesis supported by a single site, and a much-mentioned entity."""
    domain_id = add_domain()
    ctx = make_context(PAGES)
    script_scout(llm)
    script_research(llm, independent=False)
    script_review(llm, verdict="undecided", severity=2)
    llm.add(
        EntityMap,
        EntityMap(
            entities=[MentionedEntity(name="Acme AI", entity_type="company", mentioned_in=[1])]
        ),
    )
    enqueue_scout(board_db, domain_id)
    worker.drain(ctx)
    # A second mention of Acme AI, as a later mapping job would record it.
    with worker_db.acting_as("researcher") as conn:
        entity = conn.execute("SELECT id FROM entities WHERE name = 'Acme AI'").fetchone()["id"]
        evidence = conn.execute("SELECT id FROM evidence LIMIT 1").fetchone()["id"]
        memory.add_relationship(conn, subject_id=evidence, predicate="mentions", object_id=entity)
    return domain_id, ctx


def _strategize(board_db, domain_id):
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "strategize", {"domain_id": domain_id})


def test_gaps_are_found(domain_with_weak_hypothesis, worker_db):
    domain_id, _ = domain_with_weak_hypothesis
    with worker_db.reading() as conn:
        gaps = strategy.find_gaps(conn, domain_id)
    kinds = sorted(g.kind for g in gaps)
    # Scouted just now, so no "not scouted" gap.
    assert kinds == ["unstudied entity", "weak support"]
    assert "1 independent site" in next(g for g in gaps if g.kind == "weak support").description


def test_plan_sets_goals_queues_work_within_budget_and_proposes_programs(
    domain_with_weak_hypothesis, llm, board_db, worker_db, make_context
):
    domain_id, ctx = domain_with_weak_hypothesis
    # Paid search, with budget for one action beyond the reserve (5 + 8) on
    # top of the calls the fixture already made.
    paid = FakeProvider(PAGES)
    paid.metered = True
    with worker_db.reading() as conn:
        used, _ = memory.paid_calls_in_window(conn, ["fake"])
    ctx = replace(
        make_context(web=paid), settings=replace(ctx.settings, daily_call_budget=used + 13)
    )
    llm.add(
        StrategyPlan,
        StrategyPlan(
            assessment="One weakly supported hypothesis about prices.",
            goals=[
                PlannedGoal(
                    statement="Establish whether frontier inference prices really halve yearly",
                    success_criteria="H1 accepted or rejected",
                    priority=1,
                    about=["H1", "N1"],
                    actions=[
                        Action(kind="corroborate", target="H1"),
                        Action(kind="scout", focus="price lists of frontier model APIs"),
                        Action(kind="resolve", target="H9"),  # no such hypothesis
                    ],
                )
            ],
            program=ProgramProposal(
                name="Economics of AI inference",
                charter="Track prices and costs of running models.",
                rationale="Most observations so far concern prices.",
            ),
        ),
    )
    _strategize(board_db, domain_id)
    assert worker.run_once(ctx)  # just the planning job

    [brief] = llm.prompts_for(StrategyPlan)
    assert f"[H1] (under_review, confidence 0.70, 1 open critiques) {HYPOTHESIS}" in brief
    assert "- weak support: [H1] supported by 1 independent site(s)" in brief
    assert "- unstudied entity: [N1] mentioned 2 times" in brief

    with worker_db.reading() as conn:
        goal = conn.execute("SELECT * FROM goals").fetchone()
        assert goal["status"] == "active"
        about = conn.execute(
            "SELECT n.kind FROM relationships r JOIN nodes n ON n.id = r.object_id "
            "WHERE r.subject_id = %s AND r.predicate = 'investigates' ORDER BY n.kind",
            (goal["id"],),
        ).fetchall()
        assert [a["kind"] for a in about] == ["entity", "hypothesis"]
        queued = conn.execute(
            "SELECT kind, payload FROM jobs WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        # Only the first action fits the budget; the scout and the invalid one are skipped.
        assert [q["kind"] for q in queued] == ["corroborate"]
        assert queued[0]["payload"]["goal_id"] == str(goal["id"])
        program = conn.execute("SELECT name, status FROM programs").fetchone()
        assert (program["name"], program["status"]) == ("Economics of AI inference", "proposed")
        decision = conn.execute(
            "SELECT d.statement, d.status FROM decisions d JOIN relationships r "
            "ON r.subject_id = d.id AND r.predicate = 'concerns'"
        ).fetchone()
        assert decision["statement"] == "Open research program: Economics of AI inference"
        assert decision["status"] == "proposed"
        notes = conn.execute("SELECT notes FROM runs WHERE notes LIKE 'assessment:%%'").fetchone()[
            "notes"
        ]
        assert "queued corroborate; 2 actions skipped (budget or invalid)" in notes

    # A second plan continues the goal and does not propose the program again.
    llm.add(
        StrategyPlan,
        StrategyPlan(
            assessment="Same.",
            goals=[
                PlannedGoal(
                    existing="G1",
                    statement="Establish whether frontier inference prices really halve yearly",
                    success_criteria="H1 accepted or rejected",
                    priority=1,
                    about=["H1"],
                )
            ],
            program=ProgramProposal(name="economics of AI inference", charter="c", rationale="r"),
        ),
    )
    _strategize(board_db, domain_id)
    ctx = replace(ctx, settings=replace(ctx.settings, daily_call_budget=0))  # plan only
    worker.drain(ctx)  # the corroboration waits for budget; the plan runs
    with worker_db.reading() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM runs WHERE notes LIKE 'assessment: Same.%%'"
            ).fetchone()["n"]
            == 1
        )
        assert conn.execute("SELECT count(*) AS n FROM goals").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM programs").fetchone()["n"] == 1


def _goal(statement, existing=None, priority=2):
    return PlannedGoal(
        existing=existing,
        statement=statement,
        success_criteria="c",
        priority=priority,
        about=["H1"],
    )


def test_free_search_limits_plans_by_actions_not_budget(
    domain_with_weak_hypothesis, llm, board_db, worker_db
):
    domain_id, ctx = domain_with_weak_hypothesis  # free search: the fakes are not metered
    ctx = replace(ctx, settings=replace(ctx.settings, daily_call_budget=0))
    many = [Action(kind="corroborate", target="H1")] * 3
    llm.add(
        StrategyPlan,
        StrategyPlan(
            assessment="a",
            goals=[
                PlannedGoal(
                    statement=f"Goal {i}",
                    success_criteria="c",
                    priority=i,
                    about=["H1"],
                    actions=many,
                )
                for i in (1, 2, 3)
            ],
        ),
    )
    _strategize(board_db, domain_id)
    worker.run_once(ctx)
    brief = llm.prompts_for(StrategyPlan)[-1]
    assert "Search is free" in brief and "Paid searches left" not in brief
    with worker_db.reading() as conn:
        queued = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE kind = 'corroborate' AND status = 'pending'"
        ).fetchone()["n"]
    assert queued == MAX_FREE_ACTIONS  # nine planned, spent budget irrelevant


def test_goals_that_no_longer_serve_the_mission_are_abandoned(
    domain_with_weak_hypothesis, llm, board_db, worker_db
):
    domain_id, ctx = domain_with_weak_hypothesis
    ctx = replace(ctx, settings=replace(ctx.settings, daily_call_budget=0))  # plan only
    llm.add(
        StrategyPlan,
        StrategyPlan(
            assessment="a",
            goals=[
                _goal("Follow OpenAI's safety work", priority=1),
                _goal("Measure agent reliability", priority=2),
            ],
        ),
    )
    _strategize(board_db, domain_id)
    worker.run_once(ctx)

    # G1 is off-mission; G2 is both continued and abandoned (kept); G7 does not exist.
    llm.add(
        StrategyPlan,
        StrategyPlan(
            assessment="b",
            goals=[_goal("Measure agent reliability", existing="G2")],
            abandon=[
                GoalAbandonment(goal="G1", reason="The mission is about agent reliability."),
                GoalAbandonment(goal="G2", reason="Contradicts the plan."),
                GoalAbandonment(goal="G7", reason="No such goal."),
            ],
        ),
    )
    _strategize(board_db, domain_id)
    worker.run_once(ctx)
    with worker_db.reading() as conn:
        goals = {
            g["statement"]: g for g in conn.execute("SELECT statement, status, outcome FROM goals")
        }
        notes = conn.execute("SELECT notes FROM runs WHERE notes LIKE 'assessment: b%%'")
        notes = notes.fetchone()["notes"]
    assert goals["Follow OpenAI's safety work"]["status"] == "abandoned"
    assert goals["Follow OpenAI's safety work"]["outcome"] == (
        "The mission is about agent reliability."
    )
    assert goals["Measure agent reliability"]["status"] == "active"
    assert "abandoned goal: Follow OpenAI's safety work" in notes

    # The next plan is told what was dropped and why, so it is not set again.
    llm.add(StrategyPlan, StrategyPlan(assessment="c", goals=[]))
    _strategize(board_db, domain_id)
    worker.run_once(ctx)
    brief = llm.prompts_for(StrategyPlan)[-1]
    assert "Recently abandoned goals" in brief
    assert "- Follow OpenAI's safety work Reason: The mission is about agent reliability." in brief
    assert "[G1] (priority 2) Measure agent reliability" in brief


def test_owner_approves_or_rejects_proposals(
    domain_with_weak_hypothesis, llm, board_db, worker_db, db_url, monkeypatch, capsys
):
    domain_id, ctx = domain_with_weak_hypothesis
    for name in ("Economics of AI inference", "Agent security"):
        llm.add(
            StrategyPlan,
            StrategyPlan(
                assessment="a",
                goals=[],
                program=ProgramProposal(name=name, charter="c", rationale="r"),
            ),
        )
        _strategize(board_db, domain_id)
        worker.run_once(ctx)

    monkeypatch.setenv("COLLEGIUM_BOARD_DATABASE_URL", db_url)
    with worker_db.reading() as conn:
        ids = {
            r["statement"].split(": ", 1)[1]: str(r["id"])
            for r in conn.execute("SELECT id, statement FROM decisions")
        }
    cli.main(["approve", ids["Economics of AI inference"][:8]])
    cli.main(["reject", ids["Agent security"][:8], "--reason", "Out of scope for now."])
    out = capsys.readouterr().out
    assert "program Economics of AI inference is now active" in out
    assert "program Agent security is now closed" in out

    with worker_db.reading() as conn:
        rows = conn.execute(
            "SELECT d.statement, d.status, a.name AS resolved_by FROM decisions d "
            "JOIN actors a ON a.id = d.resolved_by ORDER BY d.statement"
        ).fetchall()
        assert [(r["status"], r["resolved_by"]) for r in rows] == [
            ("rejected", "owner"),
            ("approved", "owner"),
        ]
        objection = conn.execute(
            "SELECT argument, status FROM critiques WHERE target_id = %s",
            (ids["Agent security"],),
        ).fetchone()
        assert (objection["argument"], objection["status"]) == ("Out of scope for now.", "upheld")


def test_corroboration_ignores_sites_already_cited(
    domain_with_weak_hypothesis, llm, board_db, worker_db
):
    domain_id, ctx = domain_with_weak_hypothesis
    with worker_db.reading() as conn:
        hid = conn.execute("SELECT id FROM hypotheses").fetchone()["id"]
    llm.add(SearchPlan, SearchPlan(queries=["frontier model prices"]))
    llm.add(
        ResolutionFindings,
        lambda system, user: ResolutionFindings(
            evidence=[
                EvidenceItem(
                    document=_doc_number(INDEPENDENT, user),
                    excerpt="the price of running frontier models fell by roughly half",
                    summary="Independent measurements.",
                    reliability=0.7,
                    bears_on=[Stance(hypothesis="H", stance="supports", rationale="r")],
                )
            ]
        ),
    )
    script_review(llm, verdict="undecided", severity=2)
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "corroborate", {"hypothesis_id": hid})
    worker.drain(ctx)

    prompt = llm.prompts_for(ResolutionFindings)[0]
    assert "Already cited (excluded): news.example" in prompt
    assert "news.example/acme-price-cut" not in prompt.split("Documents:", 1)[1]
    with worker_db.reading() as conn:
        h = conn.execute("SELECT status FROM hypotheses").fetchone()
        # A second independent site: the Historian can now accept it.
        assert h["status"] == "accepted"


def _doc_number(url: str, prompt: str) -> int:
    import re

    return int(re.search(rf"<<<D(\d+) \w+>>>\n[^\n]*\n{re.escape(url)}", prompt).group(1))


def test_scout_follows_the_strategists_focus(make_context, llm, board_db, add_domain):
    domain_id = add_domain()
    ctx = make_context(PAGES)
    llm.add(SearchPlan, SearchPlan(queries=["api price lists"]))
    llm.add(ScoutReport, ScoutReport(observations=[]))
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "scout", {"domain_id": domain_id, "focus": "API price lists"})
    worker.drain(ctx)
    assert "The Strategist asks you to look into: API price lists" in llm.prompts_for(SearchPlan)[0]
