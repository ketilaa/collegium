"""Ask the Organization: answers from memory only, with citations."""

# The board fixtures are imported from test_web; tests take them as arguments.
# ruff: noqa: F811

from uuid import uuid4

import pytest
from markupsafe import escape
from test_pipeline import HYPOTHESIS, PAGES
from test_web import client, crawler, researched  # noqa: F401 (fixtures)

from collegium import jobs, memory, strategy, worker
from collegium.roles.answerer import NOTHING_FOUND, Answer, AnswerPoint, SearchTerms, compose


def ask(client, text, domain=""):
    response = client.post("/ask", data={"question": text, "domain": domain})
    assert response.status_code == 200 and "Question received" in response.text
    return response


def only_question(worker_db):
    with worker_db.reading() as conn:
        return conn.execute("SELECT * FROM questions").fetchone()


def test_an_answer_cites_memory(client, researched, worker_db, llm, make_context):
    ask(client, "Hvor raskt faller prisene på inferens?", domain="ai-agents")
    with worker_db.reading() as conn:
        job = conn.execute("SELECT kind, priority FROM jobs WHERE kind = 'ask'").fetchone()
    assert job["priority"] == 1  # ahead of the organization's own work

    llm.add(SearchTerms, SearchTerms(terms=["inference costs", "prices", "prisene"]))
    llm.add(
        Answer,
        Answer(
            points=[
                AnswerPoint(
                    statement="Organisasjonen mener at kostnadene halveres hvert år (H1),",
                    sources=["H1"],
                ),
                AnswerPoint(
                    statement="støttet av målinger hos tolv leverandører.", sources=["[X1]", "H1"]
                ),
                AnswerPoint(statement="Noe helt annet.", sources=["H9"]),  # unknown: dropped
            ],
            missing="Tall for 2026.",
        ),
    )
    worker.drain(make_context(PAGES))

    brief = llm.prompts_for(Answer)[-1]
    assert f") {HYPOTHESIS}" in brief and "[H1]" in brief
    assert "Hvor raskt faller prisene" in brief
    assert "Write it in Norwegian." in brief
    q = only_question(worker_db)
    assert q["status"] == "answered"
    # Labels become numbered sources; an unknown label (H9) is left as written.
    assert q["answer"] == (
        "Organisasjonen mener at kostnadene halveres hvert år [1],\n"
        "støttet av målinger hos tolv leverandører. [1][2]"
    )
    assert q["missing"] == "Tall for 2026."

    page = client.get(f"/questions/{q['id']}").text
    assert str(escape(HYPOTHESIS)) in page and "Sources in memory" in page
    assert page.count('class="prio">[') == 2
    # The fixture's hypothesis was accepted by the Historian.
    assert page.count('class="firmness firmness-concluded">concluded · 0.70') == 2
    assert "Hvor raskt faller prisene" in client.get("/ask").text


def test_an_answer_without_valid_citations_is_not_an_answer(
    client, researched, worker_db, llm, make_context, add_domain
):
    ask(client, "What do agents cost?")
    llm.add(SearchTerms, SearchTerms(terms=["inference costs"]))
    llm.add(Answer, Answer(points=[AnswerPoint(statement="About $2 an hour.", sources=["H7"])]))
    worker.drain(make_context(PAGES))
    q = only_question(worker_db)
    assert (q["status"], q["answer"]) == ("unanswered", NOTHING_FOUND)

    # It becomes a gap for every domain the question could concern.
    with worker_db.reading() as conn:
        domain_id = memory.domain_by_slug(conn, "ai-agents")["id"]
        gaps = strategy.find_gaps(conn, domain_id)
    [gap] = [g for g in gaps if g.kind == "unanswered question"]
    assert gap.about == q["id"] and 'the owner asked "What do agents cost?"' in gap.description


def test_nothing_in_memory_needs_no_answer_from_the_model(
    client, researched, worker_db, llm, make_context
):
    ask(client, "What is the weather in Bergen?")
    llm.add(SearchTerms, SearchTerms(terms=["weather", "Bergen"]))
    worker.drain(make_context(PAGES))
    assert llm.prompts_for(Answer) == []
    q = only_question(worker_db)
    assert (q["status"], q["answer"]) == ("unanswered", NOTHING_FOUND)
    assert q["missing"] == "What is the weather in Bergen?"


def test_questions_are_answered_outside_working_hours(board_db, worker_db, add_domain):
    domain_id = add_domain()
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "scout", {"domain_id": domain_id}, priority=1)
        question_id = memory.add_question(conn, "Anything?")
        jobs.enqueue(conn, "ask", {"question_id": question_id}, priority=1)
    with worker_db.reading() as conn:
        job = jobs.claim(conn, worker.ANY_HOUR)
    assert job.kind == "ask"  # the scout, though first in line, waits for opening


def test_recall_finds_norwegian_quotes_and_respects_the_domain(worker_db, add_domain):
    energy, agents = add_domain("energy", "Energy"), add_domain()
    with worker_db.acting_as("scout") as conn:
        source = memory.record_source(conn, uri="https://digi.example/juniorer")
        obs = memory.add_observation(
            conn,
            statement="Fewer junior developer jobs are advertised.",
            source_id=source,
            recommendation=None,
        )
        memory.tag_domains(conn, obs, [agents])
        evidence = memory.add_evidence(
            conn,
            summary="A Norwegian report on junior hiring.",
            excerpt="Antallet utlyste juniorstillinger falt kraftig i fjor.",
            source_id=source,
            reliability=0.6,
        )
        memory.tag_domains(conn, evidence, [agents])
        memory.link_evidence(
            conn,
            evidence_id=evidence,
            target_id=obs,
            target_kind="observation",
            stance="supports",
            rationale=None,
        )
    with worker_db.reading() as conn:
        found = memory.recall(conn, ["juniorstillinger"], agents)
        assert [e["id"] for e in found["evidence"]] == [evidence]
        assert memory.recall(conn, ["juniorstillinger"], energy)["evidence"] == []
        found = memory.recall(conn, ["junior developer jobs", 'OR "x'], None)
        assert [o["id"] for o in found["observations"]] == [obs]


def record(kind, status=None, confidence=None):
    return {"kind": kind, "status": status, "confidence": confidence}


def test_citations_are_numbered_in_order_of_use():
    h, x = uuid4(), uuid4()
    labels = {"H1": h, "X2": x}
    records = {"H1": record("hypothesis", "accepted", 0.8), "X2": record("evidence")}
    reply = Answer(
        points=[
            AnswerPoint(statement="Both (H1, X2) agree.", sources=["H1"]),
            AnswerPoint(statement="Measured, too.", sources=["X2"]),
            AnswerPoint(statement="Invented.", sources=["H3"]),
        ]
    )
    composed = compose(reply, labels, records)
    assert composed.text == "Both [1][2] agree.\nMeasured, too. [2]"
    assert (composed.cited, composed.dropped) == ([h, x], 1)


@pytest.mark.parametrize(
    ("statement", "source", "firmness", "text", "wording"),
    [
        # Accepted: may be stated as a conclusion.
        (
            "We have concluded that prices halve.",
            record("hypothesis", "accepted", 0.8),
            "concluded",
            "We have concluded that prices halve. [1]",
            None,
        ),
        # Under review: a claimed conclusion becomes an investigation.
        (
            "We have concluded that prices halve.",
            record("hypothesis", "under_review", 0.55),
            "investigating",
            "We are investigating whether prices halve. [1]",
            "corrected",
        ),
        # Only a source says so.
        (
            "We know that agents fail often.",
            record("evidence"),
            "reported",
            "A source reports that agents fail often. [1]",
            "corrected",
        ),
        (
            "Vi har konkludert med at juniorer ansettes sjeldnere.",
            record("observation", "proposed"),
            "reported",
            "En kilde melder at juniorer ansettes sjeldnere. [1]",
            "corrected",
        ),
        # A claim that cannot be rewritten is flagged.
        (
            "Prices halve, as we have concluded.",
            record("evidence"),
            "reported",
            "Prices halve, as we have concluded. [1]",
            "overstated",
        ),
        (
            "We have concluded that prices halve.",
            record("hypothesis", "rejected", 0.1),
            "judged false",
            "We have concluded that prices halve. [1]",
            "overstated",
        ),
        # Honest wording is left alone.
        (
            "A report says prices halve.",
            record("evidence"),
            "reported",
            "A report says prices halve. [1]",
            None,
        ),
    ],
)
def test_the_code_decides_how_firmly_a_point_is_held(statement, source, firmness, text, wording):
    composed = compose(
        Answer(points=[AnswerPoint(statement=statement, sources=["A1"])]),
        {"A1": uuid4()},
        {"A1": source},
    )
    [point] = composed.points
    assert (point["firmness"], point["text"] + " [1]", point["wording"]) == (
        firmness,
        text,
        wording,
    )
    assert composed.text == text


def test_firmness_takes_the_strongest_source():
    points = compose(
        Answer(points=[AnswerPoint(statement="Prices halve.", sources=["H1", "H2", "X1"])]),
        {"H1": uuid4(), "H2": uuid4(), "X1": uuid4()},
        {
            "H1": record("hypothesis", "under_review", 0.55),
            "H2": record("hypothesis", "proposed", 0.4),
            "X1": record("evidence"),
        },
    ).points
    assert (points[0]["firmness"], points[0]["confidence"]) == ("investigating", 0.55)
