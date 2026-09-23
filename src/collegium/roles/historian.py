"""The Historian: records what the organization now believes.

It never researches and uses no model. After the Skeptic has reviewed a
hypothesis, the Historian applies fixed rules to decide its status, and
retires hypotheses that an accepted one refines. It is the only role that
changes a hypothesis's status, so every belief change follows the same rules.
"""

from urllib.parse import urlsplit
from uuid import UUID

from collegium import memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.roles.base import Context, Persist, Role

ACCEPT_AT = 0.6
REJECT_AT = 0.25
# A claim is not accepted on one publisher's word, least of all the word of
# the company making the claim.
MIN_INDEPENDENT_SOURCES = 2


def decide(verdict: str, confidence: float | None, independent_sources: int) -> str:
    if confidence is None:
        return "under_review"
    if verdict == "reject" or confidence <= REJECT_AT:
        return "rejected"
    if (
        verdict == "accept"
        and confidence >= ACCEPT_AT
        and independent_sources >= MIN_INDEPENDENT_SOURCES
    ):
        return "accepted"
    return "under_review"


def site(uri: str) -> str:
    """The site a source belongs to; pages on one site are one voice."""
    host = urlsplit(uri).hostname or uri
    return host.removeprefix("www.")


class Historian(Role):
    name = "historian"
    job_kind = "record"
    uses_llm = False

    def version(self) -> str:
        return f"rules-2:accept>={ACCEPT_AT},reject<={REJECT_AT},sources>={MIN_INDEPENDENT_SOURCES}"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        hypothesis_id = UUID(job.payload["hypothesis_id"])
        verdict = job.payload["verdict"]

        def persist(conn: Connection) -> str:
            h = memory.hypothesis(conn, hypothesis_id)
            if h is None:
                raise LookupError(f"hypothesis {hypothesis_id} not found")
            if h["status"] not in memory.LIVE_HYPOTHESIS_STATUSES:
                return f"unchanged: hypothesis is {h['status']}"
            confidence = float(h["confidence"]) if h["confidence"] is not None else None
            sites = {site(u) for u in memory.supporting_source_uris(conn, hypothesis_id)}
            status = decide(verdict, confidence, len(sites))
            notes = [
                f"{h['status']} -> {status} (verdict {verdict}, confidence {confidence}, "
                f"supported by {len(sites)} independent sources: {sorted(sites)})"
            ]
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
