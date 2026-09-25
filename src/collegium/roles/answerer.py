"""Answering the owner's questions from memory: Ask the Organization.

The Researcher answers, but never searches: the answer rests only on what
the organization already holds. The model first turns the question into
search terms (the question may be in Norwegian; memory is in English), full
text search finds what memory holds, and the model answers citing labels.
Only citations of records it was shown are kept, and an answer that cites
nothing is not an answer: the question is then recorded as unanswered, and
becomes a knowledge gap for the Strategist.
"""

import re
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import memory
from collegium.db import Connection
from collegium.grounding import is_english
from collegium.jobs import Job
from collegium.roles.base import Context, Persist, Role
from collegium.untrusted import fence

NOTHING_FOUND = "Memory holds nothing on this yet."
_LABEL = re.compile(r"\b([HOXN]\d+)\b")
# Citations the model wrapped itself: "(H1)", "[H1]", "(H1, X2)".
_WRAPPED = re.compile(r"[(\[]\s*((?:\[\d+\](?:\s*[,;]\s*)?)+)\s*[)\]]")


class SearchTerms(BaseModel):
    terms: list[str] = Field(
        min_length=1,
        max_length=8,
        description="Words and short phrases that records answering the question would contain",
    )


class AnswerPoint(BaseModel):
    statement: str = Field(description="One thing memory says that answers the question")
    sources: list[str] = Field(
        min_length=1, description="Labels (H1, O2, X3, N4) of the records it rests on"
    )


class Answer(BaseModel):
    points: list[AnswerPoint] = Field(
        max_length=6,
        description="What memory says, most important first; empty if nothing bears on it",
    )
    missing: str | None = Field(None, description="What memory lacks to answer fully")


class Answerer(Role):
    name = "researcher"
    job_kind = "ask"
    prompt_file = "ask.md"
    searches = False  # memory only, never the outside world

    def prepare(self, ctx: Context, job: Job) -> Persist:
        question_id = UUID(job.payload["question_id"])
        with ctx.db.reading() as conn:
            q = memory.question(conn, question_id)
            if q is None:
                raise LookupError(f"question {question_id} not found")
            if q["status"] != "pending":
                return lambda conn: f"already {q['status']}"
            domain_ids = memory.node_domain_ids(conn, question_id)
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system,
            f"The owner asks: {q['text']}\n\nWhich search terms would find the answer in memory?",
            SearchTerms,
        )
        with ctx.db.reading() as conn:
            found = memory.recall(conn, plan.terms, domain_ids[0] if domain_ids else None)
        labels, brief = _brief(found)
        if not labels:

            def nothing(conn: Connection) -> str:
                memory.answer_question(
                    conn,
                    question_id,
                    answer=NOTHING_FOUND,
                    answered=False,
                    missing=q["text"],
                    cites=[],
                )
                return f"terms={plan.terms}; nothing in memory"

            return nothing

        language = "English" if is_english(q["text"]) else "the language of the question"
        reply = ctx.llm.generate(
            system,
            f"The owner asks: {q['text']}\n\nWhat memory holds:\n\n{brief}\n\n"
            f"What is your answer? Write it in {language}.",
            Answer,
        )
        text, cited, dropped = compose(reply, labels)
        answered = bool(cited)

        def persist(conn: Connection) -> str:
            memory.answer_question(
                conn,
                question_id,
                answer=text if cited else NOTHING_FOUND,
                answered=answered,
                missing=reply.missing or (None if answered else q["text"]),
                cites=cited,
            )
            return (
                f"terms={plan.terms}; {len(labels)} records shown; "
                f"{'answered' if answered else 'unanswered'}, citing {len(cited)}"
                + (f"; {dropped} points without a known source dropped" if dropped else "")
            )

        return persist


def compose(reply: Answer, labels: dict[str, UUID]) -> tuple[str, list[UUID], int]:
    """The answer as text with numbered sources, the records it cites in
    that order, and how many points were dropped for citing nothing known."""
    order: list[str] = []
    lines = []
    dropped = 0
    for point in reply.points:
        known = [x.strip().strip("[]()").upper() for x in point.sources]
        known = [label for label in known if label in labels]
        if not known:
            dropped += 1
            continue
        # Labels the model also wrote into the sentence count as sources.
        mentioned = [m.group(1) for m in _LABEL.finditer(point.statement)]
        for label in mentioned + known:
            if label in labels and label not in order:
                order.append(label)
        number = {label: n for n, label in enumerate(order, 1)}
        sentence = _renumber(point.statement.strip(), number)
        refs = "".join(
            f"[{n}]" for n in sorted({number[label] for label in known}) if f"[{n}]" not in sentence
        )
        lines.append(f"{sentence} {refs}".strip())
    return "\n".join(lines), [labels[label] for label in order], dropped


def _renumber(text: str, number: dict[str, int]) -> str:
    """Labels replaced by their source numbers; "(H1, X2)" becomes "[1][2]"."""

    def replace(m: re.Match) -> str:
        return f"[{number[m.group(1)]}]" if m.group(1) in number else m.group(0)

    text = _LABEL.sub(replace, text)
    return _WRAPPED.sub(lambda m: "".join(re.findall(r"\[\d+\]", m.group(1))), text)


def _brief(found: dict) -> tuple[dict[str, UUID], str]:
    """Records as labelled lines; excerpts are outside text and fenced."""
    labels: dict[str, UUID] = {}
    lines: list[str] = []
    label_of: dict[UUID, str] = {}

    def add(prefix: str, rows: list[dict]) -> list[tuple[str, dict]]:
        out = []
        for i, row in enumerate(rows, 1):
            label = f"{prefix}{i}"
            labels[label] = row["id"]
            label_of[row["id"]] = label
            out.append((label, row))
        return out

    hypotheses = add("H", found["hypotheses"])
    observations = add("O", found["observations"])
    evidence = add("X", found["evidence"])
    entities = add("N", found["entities"])
    if hypotheses:
        lines.append("Hypotheses:")
        for label, h in hypotheses:
            conf = f"{h['confidence']:.2f}" if h["confidence"] is not None else "unassessed"
            lines.append(f"[{label}] ({h['status']}, confidence {conf}) {h['statement']}")
    if observations:
        lines.append("\nObservations:")
        lines += [
            f"[{label}] ({o['status']}, {o['created_at']:%Y-%m-%d}) {o['statement']}"
            for label, o in observations
        ]
    if evidence:
        lines.append("\nEvidence:")
        for label, e in evidence:
            about = label_of.get(e["target_id"], f"a {e['target_kind']}")
            lines.append(
                f"[{label}] ({e['stance']} {about}, reliability {e['reliability']}) "
                f"{e['summary']} [{e['uri']}]\n" + fence(label, e["excerpt"] or "")
            )
    if entities:
        lines.append("\nEntities:")
        lines += [
            f"[{label}] {n['name']} ({n['entity_type']}, {n['mentions']} mentions)"
            for label, n in entities
        ]
    return labels, "\n".join(lines)
