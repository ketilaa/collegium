"""Mapping the entities that observations and evidence mention."""

from conftest import FakeProvider

from collegium import jobs, worker
from collegium.roles.base import EvidenceItem, SearchPlan, Stance
from collegium.roles.mapper import EntityMap, MentionedEntity
from collegium.roles.researcher import ProposedHypothesis, ResearchFindings
from collegium.roles.scout import ProposedObservation, ScoutReport
from collegium.roles.skeptic import SkepticReview

URL = "https://news.example/acme"
PAGE = (
    "Acme AI said its Frontier model now runs on Nvidia hardware at half the cost. "
    "Chief executive Jane Doe called it a turning point."
)


def _script_chain(llm, entity_map):
    llm.add(SearchPlan, SearchPlan(queries=["acme"]))
    llm.add(
        ScoutReport,
        ScoutReport(
            observations=[
                ProposedObservation(
                    source=1,
                    quote="Acme AI said its Frontier model now runs on Nvidia hardware",
                    statement="Acme AI said its Frontier model now runs on Nvidia hardware.",
                    why_it_matters="w",
                    investigate=True,
                )
            ]
        ),
    )
    llm.add(SearchPlan, SearchPlan(queries=["acme costs"]))
    llm.add(
        ResearchFindings,
        ResearchFindings(
            hypotheses=[
                ProposedHypothesis(
                    label="H1",
                    statement="Hardware choice drives cost.",
                    rationale="r",
                    falsified_if="contrary evidence",
                    confidence=0.5,
                )
            ],
            evidence=[
                EvidenceItem(
                    document=1,
                    excerpt="Chief executive Jane Doe called it a turning point.",
                    summary="s",
                    reliability=0.5,
                    bears_on=[Stance(hypothesis="H1", stance="supports", rationale="r")],
                )
            ],
        ),
    )
    llm.add(SearchPlan, SearchPlan(queries=["acme doubts"]))
    llm.add(
        SkepticReview,
        SkepticReview(
            critiques=[], evidence=[], confidence=0.5, confidence_rationale="r", verdict="undecided"
        ),
    )
    llm.add(EntityMap, entity_map)


def test_mentioned_entities_are_grounded_linked_and_reused(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    ctx = make_context(web=FakeProvider({URL: ("Acme news", PAGE)}))
    _script_chain(
        llm,
        EntityMap(
            entities=[
                MentionedEntity(name="Acme AI", entity_type="company", mentioned_in=[1]),
                MentionedEntity(name="Nvidia", entity_type="company", mentioned_in=[1, 2]),
                MentionedEntity(name="Jane Doe", entity_type="person", mentioned_in=[3]),
                MentionedEntity(name="OpenAI", entity_type="company", mentioned_in=[1]),  # absent
            ]
        ),
    )
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "scout", {"domain_id": domain_id})
    worker.drain(ctx)

    # T1 is the observation, T2 the Scout's quote, T3 the Researcher's evidence.
    [map_prompt] = llm.prompts_for(EntityMap)
    assert "Entities the organization already knows: none yet" in map_prompt
    assert "<<<T3 " in map_prompt

    with worker_db.reading() as conn:
        mentions = conn.execute(
            "SELECT e.name, n.kind FROM relationships r JOIN entities e ON e.id = r.object_id "
            "JOIN nodes n ON n.id = r.subject_id WHERE r.predicate = 'mentions' "
            "ORDER BY e.name, n.kind"
        ).fetchall()
        assert [(m["name"], m["kind"]) for m in mentions] == [
            ("Acme AI", "observation"),
            ("Jane Doe", "evidence"),
            ("Nvidia", "evidence"),  # the Scout's quote names Nvidia too
            ("Nvidia", "observation"),
        ]
        assert conn.execute("SELECT count(*) AS n FROM entities").fetchone()["n"] == 3
        notes = conn.execute(
            "SELECT notes FROM runs WHERE notes LIKE '%%entities proposed%%'"
        ).fetchone()["notes"]
        assert "3 new" in notes and "1 not found in their texts" in notes

    # A second chain naming a known entity differently does not duplicate it,
    # and the Scout is shown what the domain mentions most.
    _script_chain(
        llm,
        EntityMap(
            entities=[MentionedEntity(name="NVIDIA", entity_type="company", mentioned_in=[1])]
        ),
    )
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "scout", {"domain_id": domain_id})
    ctx = make_context(
        web=FakeProvider(
            {
                "https://news.example/acme-2": (
                    "Acme news 2",
                    "Acme AI said its Frontier model now runs on Nvidia hardware in Europe too.",
                )
            }
        )
    )
    worker.drain(ctx)

    assert "- Nvidia (company, 2 mentions)" in llm.prompts_for(SearchPlan)[3]
    with worker_db.reading() as conn:
        assert conn.execute("SELECT count(*) AS n FROM entities").fetchone()["n"] == 3
