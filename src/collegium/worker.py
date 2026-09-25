"""The worker: one process that runs every role.

It claims a job, records a run for the role that handles it, lets the role
prepare its work, then commits the role's knowledge, the run's outcome and
the job's completion in one transaction.
"""

import logging
import signal
import threading
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from collegium import jobs, memory
from collegium.acquisition import Budget, BudgetExhausted, Recorder
from collegium.db import Database
from collegium.hours import WorkingHours
from collegium.roles import ROLES
from collegium.roles.base import Context

log = logging.getLogger(__name__)


# Jobs taken at any hour: the owner is waiting for them, and they make no
# external calls.
ANY_HOUR = ("ask", "draft", "reply")


def run_once(ctx: Context, kinds: tuple[str, ...] | None = None) -> bool:
    """Process one job (only of these kinds, if given). Returns False when
    there was nothing to do."""
    with ctx.db.reading() as conn:
        job = jobs.claim(conn, kinds)
    if job is None:
        return False

    role = ROLES[job.kind]
    # Wait for the budget only when the job's main route is paid. With free
    # search and reading first, paid fallbacks are skipped instead.
    if role.searches_for(job) and ctx.acquisition is not None and ctx.acquisition.paid_first:
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


def run_forever(ctx: Context, hours: WorkingHours | None = None, idle_seconds: float = 5) -> None:
    """Process jobs, but start new ones only within working hours, except
    the owner's questions, which are answered at any hour.

    Stops gracefully: on SIGTERM (a container stop or recreate) or SIGINT it
    takes no new job, finishes the one it is running, and returns. A job
    cut off halfway would lose its work and one of its attempts, and model
    jobs run for minutes, so Compose gives the worker a long grace period.
    """
    stopping = threading.Event()

    def stop(signum, frame) -> None:
        if not stopping.is_set():
            log.info("%s: stopping after the current job", signal.Signals(signum).name)
        stopping.set()

    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        if hours is not None:
            log.info("working hours: %s", hours.describe())
        while not stopping.is_set():
            closed = hours is not None and not hours.is_open()
            if not run_once(ctx, ANY_HOUR if closed else None):
                stopping.wait(idle_seconds)
        log.info("stopped")
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


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
