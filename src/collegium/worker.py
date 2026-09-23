"""The worker: one process that runs every role.

It claims a job, records a run for the role that handles it, lets the role
prepare its work, then commits the role's knowledge, the run's outcome and
the job's completion in one transaction.
"""

import logging
import time
import traceback

from collegium import jobs
from collegium.roles import ROLES
from collegium.roles.base import Context

log = logging.getLogger(__name__)


def run_once(ctx: Context) -> bool:
    """Process one job. Returns False when there was nothing to do."""
    with ctx.db.reading() as conn:
        job = jobs.claim(conn)
    if job is None:
        return False

    role = ROLES[job.kind]
    with ctx.db.acting_as(role.name) as conn:
        run_id = jobs.start_run(
            conn,
            ctx.db.actor_id(role.name),
            model=ctx.llm.model if role.uses_llm else None,
            role_version=role.version(),
            input={"job_id": job.id, **job.payload},
        )
    log.info("%s job %s: run %s", job.kind, job.id, run_id)

    try:
        persist = role.prepare(ctx, job)
        with ctx.db.acting_as(role.name, run_id) as conn:
            notes = persist(conn)
            jobs.finish_run(conn, run_id, "succeeded", notes)
            jobs.succeed(conn, job.id, run_id)
        log.info("%s job %s succeeded: %s", job.kind, job.id, notes)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("%s job %s failed: %s", job.kind, job.id, error)
        with ctx.db.acting_as(role.name, run_id) as conn:
            jobs.finish_run(conn, run_id, "failed", traceback.format_exc(limit=5))
            jobs.fail(conn, job, run_id, error)
    return True


def run_forever(ctx: Context, idle_seconds: float = 5) -> None:
    while True:
        if not run_once(ctx):
            time.sleep(idle_seconds)


def drain(ctx: Context, limit: int = 1000) -> int:
    """Process jobs until none are due. Returns how many ran."""
    count = 0
    while count < limit and run_once(ctx):
        count += 1
    return count
