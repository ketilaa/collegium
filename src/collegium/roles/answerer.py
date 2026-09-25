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
from dataclasses import dataclass
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
        labels, records, shown = brief(found)
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

        # The model follows a named language far better than "the language
        # of the question"; the organization's languages are these two.
        language = "English" if is_english(q["text"]) else "Norwegian"
        reply = ctx.llm.generate(
            system,
            f"The owner asks: {q['text']}\n\nWhat memory holds:\n\n{shown}\n\n"
            f"What is your answer? Write it in {language}.",
            Answer,
        )
        composed = compose(reply, labels, records)
        answered = bool(composed.cited)

        def persist(conn: Connection) -> str:
            memory.answer_question(
                conn,
                question_id,
                answer=composed.text if answered else NOTHING_FOUND,
                answered=answered,
                missing=reply.missing or (None if answered else q["text"]),
                cites=composed.cited,
                points=composed.points,
            )
            return (
                f"terms={plan.terms}; {len(labels)} records shown; "
                f"{'answered' if answered else 'unanswered'}, citing {len(composed.cited)}"
                + (
                    f"; {composed.dropped} points without a known source dropped"
                    if composed.dropped
                    else ""
                )
                + "".join(
                    f"; wording {p['wording']}: {p['text'][:60]}"
                    for p in composed.points
                    if p["wording"]
                )
            )

        return persist


@dataclass
class Composed:
    text: str  # the answer, one point per line, sources as [1], [2], ...
    cited: list[UUID]  # the records cited, in source-number order
    points: list[dict]  # each point with its sources and firmness
    dropped: int  # points that cited nothing shown


# How firmly memory holds a point, from its sources. The model words the
# point; this decides what the wording may claim.
CONCLUDED, INVESTIGATING, JUDGED_FALSE, REPORTED = (
    "concluded",
    "investigating",
    "judged false",
    "reported",
)
# Openings that claim a conclusion, and what they become when memory holds
# the point less firmly.
_CLAIMS = {
    True: re.compile(
        r"^(?:we have concluded|we conclude|we know|it is established|it has been established)"
        r"\s+that\s+",
        re.IGNORECASE,
    ),
    False: re.compile(
        r"^(?:vi har konkludert med at|vi konkluderer med at|vi vet at|det er fastslått at)\s+",
        re.IGNORECASE,
    ),
}
_CLAIM_ANYWHERE = re.compile(
    r"\b(?:we have concluded|we conclude|it is established|vi har konkludert|det er fastslått)\b",
    re.IGNORECASE,
)
_OPENINGS = {
    (True, INVESTIGATING): "We are investigating whether ",
    (True, REPORTED): "A source reports that ",
    (False, INVESTIGATING): "Vi undersøker om ",
    (False, REPORTED): "En kilde melder at ",
}


def firmness(sources: list[dict]) -> tuple[str, float | None]:
    """How firmly the organization holds a point resting on these records:
    concluded if an accepted hypothesis supports it, investigating if a live
    one does, judged false if it rests on rejected ones, otherwise only
    reported by a source."""
    hypotheses = [r for r in sources if r["kind"] == "hypothesis"]
    for status, level in (
        (("accepted",), CONCLUDED),
        (("proposed", "under_review"), INVESTIGATING),
    ):
        held = [h for h in hypotheses if h["status"] in status]
        if held:
            known = [float(h["confidence"]) for h in held if h["confidence"] is not None]
            return level, max(known) if known else None
    return (JUDGED_FALSE if hypotheses else REPORTED), None


def _within(sentence: str, level: str) -> tuple[str, str | None]:
    """The sentence with a claimed conclusion it cannot make corrected where
    the opening is a known one, else flagged: (sentence, wording note)."""
    if level == CONCLUDED:
        return sentence, None
    english = is_english(sentence)
    opening = _OPENINGS.get((english, level))
    fixed, n = _CLAIMS[english].subn("", sentence, count=1)
    if n and opening:
        return opening + fixed, "corrected"
    if n or _CLAIM_ANYWHERE.search(sentence):
        return sentence, "overstated"
    return sentence, None


def compose(reply: Answer, labels: dict[str, UUID], records: dict[str, dict]) -> Composed:
    """The answer with numbered sources and each point's firmness. Points
    citing nothing shown are dropped."""
    order: list[str] = []
    lines: list[str] = []
    points: list[dict] = []
    dropped = 0
    for point in reply.points:
        known = [x.strip().strip("[]()").upper() for x in point.sources]
        known = [label for label in known if label in labels]
        if not known:
            dropped += 1
            continue
        # Labels the model also wrote into the sentence count as sources.
        mentioned = [m.group(1) for m in _LABEL.finditer(point.statement)]
        used = list(dict.fromkeys(label for label in mentioned + known if label in labels))
        for label in used:
            if label not in order:
                order.append(label)
        number = {label: n for n, label in enumerate(order, 1)}
        level, confidence = firmness([records[label] for label in used])
        sentence, wording = _within(_renumber(point.statement.strip(), number), level)
        numbers = sorted({number[label] for label in used})
        refs = "".join(f"[{n}]" for n in numbers if f"[{n}]" not in sentence)
        lines.append(f"{sentence} {refs}".strip())
        points.append(
            {
                "text": sentence,
                "sources": numbers,
                "firmness": level,
                "confidence": confidence,
                "wording": wording,
            }
        )
    return Composed("\n".join(lines), [labels[label] for label in order], points, dropped)


def _renumber(text: str, number: dict[str, int]) -> str:
    """Labels replaced by their source numbers; "(H1, X2)" becomes "[1][2]"."""

    def replace(m: re.Match) -> str:
        return f"[{number[m.group(1)]}]" if m.group(1) in number else m.group(0)

    text = _LABEL.sub(replace, text)
    return _WRAPPED.sub(lambda m: "".join(re.findall(r"\[\d+\]", m.group(1))), text)


def brief(found: dict) -> tuple[dict[str, UUID], dict[str, dict], str]:
    """Records as labelled lines; excerpts are outside text and fenced.
    Also, per label, the record's kind, status and confidence."""
    labels: dict[str, UUID] = {}
    records: dict[str, dict] = {}
    lines: list[str] = []
    label_of: dict[UUID, str] = {}

    kinds = {"H": "hypothesis", "O": "observation", "X": "evidence", "N": "entity"}

    def add(prefix: str, rows: list[dict]) -> list[tuple[str, dict]]:
        out = []
        for i, row in enumerate(rows, 1):
            label = f"{prefix}{i}"
            labels[label] = row["id"]
            records[label] = {
                "kind": kinds[prefix],
                "status": row.get("status"),
                "confidence": row.get("confidence"),
            }
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
    return labels, records, "\n".join(lines)
