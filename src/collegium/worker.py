"""The worker: one process that runs every role.

It claims a job, records a run for the role that handles it, lets the role
prepare its work, then commits the role's knowledge, the run's outcome and
the job's completion in one transaction.
"""

import logging
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from collegium import jobs, memory
from collegium.acquisition import Budget, BudgetExhausted, Recorder
from collegium.db import Database
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
    if role.searches and ctx.acquisition is not None:
        reopens = _budget_reopens(ctx, MIN_BUDGET_TO_START)
        if reopens is not None:
            with ctx.db.reading() as conn:
                jobs.defer(conn, job, reopens, "waiting for the daily call budget")
            log.info("%s job %s deferred until %s: budget", job.kind, job.id, reopens)
            return True

    with ctx.db.acting_as(role.name) as conn:
        run_id = jobs.start_run(
            conn,
            ctx.db.actor_id(role.name),
            model=ctx.llm.model if role.uses_llm else None,
            role_version=role.version(),
            input={"job_id": job.id, **job.payload},
        )
    log.info("%s job %s: run %s", job.kind, job.id, run_id)

    if ctx.acquisition is not None:
        ctx = replace(
            ctx,
            acquisition=ctx.acquisition.for_run(_recorder(ctx.db, role.name, run_id), _budget(ctx)),
        )
    try:
        persist = role.prepare(ctx, job)
        with ctx.db.acting_as(role.name, run_id) as conn:
            notes = persist(conn)
            jobs.finish_run(conn, run_id, "succeeded", notes)
            jobs.succeed(conn, job.id, run_id)
        log.info("%s job %s succeeded: %s", job.kind, job.id, notes)
    except BudgetExhausted as e:
        # Not the job's fault: put it back without using up an attempt.
        reopens = _budget_reopens(ctx, 1) or datetime.now(UTC) + timedelta(hours=1)
        log.info("%s job %s stopped by the budget; deferred to %s", job.kind, job.id, reopens)
        with ctx.db.acting_as(role.name, run_id) as conn:
            jobs.finish_run(conn, run_id, "failed", f"{e}; deferred to {reopens:%Y-%m-%d %H:%M}")
            jobs.defer(conn, job, reopens, str(e))
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


# A job that searches is not started with fewer paid calls left than this:
# it would most likely be cut off halfway, wasting what it had spent.
MIN_BUDGET_TO_START = 5


def _budget(ctx: Context) -> Budget:
    providers = ctx.acquisition.metered_providers

    def remaining() -> int:
        with ctx.db.reading() as conn:
            used, _ = memory.paid_calls_in_window(conn, providers)
        return ctx.settings.daily_call_budget - used

    return remaining


def _budget_reopens(ctx: Context, needed: int) -> datetime | None:
    """None if at least `needed` paid calls are allowed now; otherwise when
    the oldest call in the window leaves it."""
    providers = ctx.acquisition.metered_providers
    if not providers:
        return None
    with ctx.db.reading() as conn:
        used, oldest = memory.paid_calls_in_window(conn, providers)
    if ctx.settings.daily_call_budget - used >= needed:
        return None
    return (oldest or datetime.now(UTC)) + timedelta(hours=24, minutes=1)


def _recorder(db: Database, actor: str, run_id: UUID) -> Recorder:
    """Records each external call in its own transaction, so calls are kept
    even when the run later fails: what was sent out cannot be unsent."""

    def record(
        capability: str, provider: str, request: dict[str, Any], count: int, error: str | None
    ) -> UUID:
        with db.acting_as(actor, run_id) as conn:
            return memory.record_acquisition(
                conn,
                capability=capability,
                provider=provider,
                request=request,
                result_count=count,
                error=error,
            )

    return record
