"""The work queue and run records.

A job is a request for a role to do something ("scout this domain"). A run
is one execution of a role, and everything the role writes points at its
run. Enqueueing the next step happens in the same transaction as the
knowledge that motivated it, so the pipeline never loses or doubles a step.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from collegium.db import Connection

RETRY_BASE = timedelta(minutes=2)
STALE_AFTER = timedelta(hours=2)


@dataclass(frozen=True)
class Job:
    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    conn: Connection,
    kind: str,
    payload: dict[str, Any],
    *,
    parent_job_id: UUID | None = None,
    priority: int = 3,
) -> UUID:
    return conn.execute(
        "INSERT INTO jobs (kind, payload, parent_job_id, priority) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (kind, Jsonb(_jsonable(payload)), parent_job_id, priority),
    ).fetchone()["id"]


def claim(conn: Connection) -> Job | None:
    """Take the next due job, or None. Safe with several workers.

    A job still 'running' after STALE_AFTER is assumed to belong to a worker
    that died, and is taken over.
    """
    row = conn.execute(
        "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = now() "
        "WHERE id = (SELECT id FROM jobs "
        "            WHERE (status = 'pending' AND run_after <= now()) "
        "               OR (status = 'running' AND started_at < now() - %s "
        "                   AND attempts < max_attempts) "
        "            ORDER BY priority, run_after FOR UPDATE SKIP LOCKED LIMIT 1) "
        "RETURNING id, kind, payload, attempts, max_attempts",
        (STALE_AFTER,),
    ).fetchone()
    return Job(**row) if row else None


def succeed(conn: Connection, job_id: UUID, run_id: UUID | None) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'succeeded', run_id = %s, finished_at = now(), "
        "last_error = NULL WHERE id = %s",
        (run_id, job_id),
    )


def fail(conn: Connection, job: Job, run_id: UUID | None, error: str) -> None:
    """Retry with exponential backoff, or give up after max_attempts."""
    if job.attempts < job.max_attempts:
        conn.execute(
            "UPDATE jobs SET status = 'pending', run_id = %s, last_error = %s, "
            "run_after = now() + %s WHERE id = %s",
            (run_id, error, RETRY_BASE * 2 ** (job.attempts - 1), job.id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET status = 'failed', run_id = %s, last_error = %s, "
            "finished_at = now() WHERE id = %s",
            (run_id, error, job.id),
        )


def defer(conn: Connection, job: Job, until, reason: str) -> None:
    """Put a job back without counting the attempt, to run at `until`."""
    conn.execute(
        "UPDATE jobs SET status = 'pending', attempts = greatest(attempts - 1, 0), "
        "run_after = %s, last_error = %s WHERE id = %s",
        (until, reason, job.id),
    )


def start_run(
    conn: Connection,
    actor_id: UUID,
    *,
    model: str | None,
    role_version: str,
    input: dict[str, Any],
) -> UUID:
    return conn.execute(
        "INSERT INTO runs (actor_id, model, role_version, input) VALUES (%s, %s, %s, %s) "
        "RETURNING id",
        (actor_id, model, role_version, Jsonb(_jsonable(input))),
    ).fetchone()["id"]


def finish_run(conn: Connection, run_id: UUID, status: str, notes: str | None) -> None:
    conn.execute(
        "UPDATE runs SET status = %s, notes = %s, finished_at = now() WHERE id = %s",
        (status, notes, run_id),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value
