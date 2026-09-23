"""The Historian: records what the organization now believes.

It never researches and uses no model. After the Skeptic has reviewed a
hypothesis, the Historian applies fixed rules to decide its status, and
retires hypotheses that an accepted one refines. It is the only role that
changes a hypothesis's status, so every belief change follows the same rules.

Acceptance follows from evidence and settled critiques (the owner's
decision): enough confidence, support from independent sites, and no
serious critique left open or upheld. The Skeptic's verdict counts only as
a veto. While serious critiques are open, the Historian sends the
hypothesis back for another round of critique resolution, up to MAX_ROUNDS.
"""

from urllib.parse import urlsplit
from uuid import UUID

from collegium import jobs, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.roles.base import Context, Persist, Role
from collegium.roles.resolver import MAX_ROUNDS

ACCEPT_AT = 0.6
REJECT_AT = 0.25
# A claim is not accepted on one publisher's word, least of all the word of
# the company making the claim.
MIN_INDEPENDENT_SOURCES = 2


# Critiques this severe block acceptance while open or upheld; an upheld
# critique of FATAL severity rejects the hypothesis.
BLOCKING_SEVERITY = 3
FATAL_SEVERITY = 5


def decide(
    verdict: str,
    confidence: float | None,
    independent_sources: int,
    critiques: list[dict] = (),
) -> str:
    if confidence is None:
        return "under_review"
    fatal = any(c["status"] == "upheld" and c["severity"] >= FATAL_SEVERITY for c in critiques)
    if verdict == "reject" or confidence <= REJECT_AT or fatal:
        return "rejected"
    if (
        confidence >= ACCEPT_AT
        and independent_sources >= MIN_INDEPENDENT_SOURCES
        and not blocking(critiques)
    ):
        return "accepted"
    return "under_review"


def blocking(critiques: list[dict]) -> list[dict]:
    """Critiques that stand in the way of acceptance."""
    return [
        c
        for c in critiques
        if c["status"] in ("open", "upheld") and c["severity"] >= BLOCKING_SEVERITY
    ]


def site(uri: str) -> str:
    """The site a source belongs to; pages on one site are one voice."""
    host = urlsplit(uri).hostname or uri
    return host.removeprefix("www.")


class Historian(Role):
    name = "historian"
    job_kind = "record"
    uses_llm = False
    searches = False

    def version(self) -> str:
        return (
            f"rules-3:accept>={ACCEPT_AT},reject<={REJECT_AT},"
            f"sources>={MIN_INDEPENDENT_SOURCES},blocking>={BLOCKING_SEVERITY},"
            f"fatal={FATAL_SEVERITY},rounds={MAX_ROUNDS}"
        )

    def prepare(self, ctx: Context, job: Job) -> Persist:
        hypothesis_id = UUID(job.payload["hypothesis_id"])
        verdict = job.payload["verdict"]
        round_ = int(job.payload.get("round", 0))

        def persist(conn: Connection) -> str:
            h = memory.hypothesis(conn, hypothesis_id)
            if h is None:
                raise LookupError(f"hypothesis {hypothesis_id} not found")
            if h["status"] not in memory.LIVE_HYPOTHESIS_STATUSES:
                return f"unchanged: hypothesis is {h['status']}"
            confidence = float(h["confidence"]) if h["confidence"] is not None else None
            sites = {site(u) for u in memory.supporting_source_uris(conn, hypothesis_id)}
            critiques = memory.critiques_of(conn, hypothesis_id)
            status = decide(verdict, confidence, len(sites), critiques)
            blockers = blocking(critiques)
            notes = [
                f"{h['status']} -> {status} (verdict {verdict}, confidence {confidence}, "
                f"supported by {len(sites)} independent sources: {sorted(sites)}; "
                f"{len(blockers)} blocking critiques)"
            ]
            open_blockers = [c for c in blockers if c["status"] == "open"]
            if status == "under_review" and open_blockers:
                if round_ < MAX_ROUNDS:
                    jobs.enqueue(
                        conn,
                        "resolve",
                        {"hypothesis_id": hypothesis_id, "round": round_ + 1},
                        parent_job_id=job.id,
                    )
                    notes.append(f"sent for critique resolution, round {round_ + 1}")
                else:
                    notes.append(f"critiques still open after {MAX_ROUNDS} rounds")
            if status != h["status"]:
                memory.set_hypothesis_status(conn, hypothesis_id, status)
            if status == "accepted":
                for old_id in memory.refined_hypotheses(conn, hypothesis_id):
                    old = memory.hypothesis(conn, old_id)
                    if old and old["status"] in memory.LIVE_HYPOTHESIS_STATUSES:
                        memory.set_hypothesis_status(
                            conn, old_id, "superseded", superseded_by=hypothesis_id
                        )
                        notes.append(f"superseded {old_id}")
            return "; ".join(notes)

        return persist
