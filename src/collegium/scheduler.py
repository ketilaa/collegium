"""The scheduler: keeps the organization working between owner sessions.

On each working day it asks the Strategist to plan each active domain, at
the first check after opening. The Strategist decides what to scout,
corroborate or resolve, within the budget, and always keeps one scout a day.
Outside working hours it does nothing.
"""

import logging
import time
from datetime import datetime

from opentelemetry import metrics, trace

from collegium import jobs, memory, telemetry
from collegium.db import Database
from collegium.hours import WorkingHours

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)
_instruments = telemetry.build_scheduler_instruments(metrics.get_meter(__name__))

ACTOR = "scheduler"


def tick(db: Database, hours: WorkingHours, now: datetime | None = None) -> int:
    """Queue today's planning jobs if within working hours. Returns how
    many were queued."""
    with _tracer.start_as_current_span("scheduler.tick") as span:
        if not hours.is_open(now):
            span.set_attribute("collegium.planned", 0)
            return 0
        since = hours.start_of_workday(now)
        enqueued = 0
        with db.acting_as(ACTOR) as conn:
            for domain in memory.active_domains(conn):
                planned_today = conn.execute(
                    "SELECT 1 FROM jobs WHERE kind = 'strategize' AND payload->>'domain_id' = %s "
                    "AND (status IN ('pending', 'running') OR created_at >= %s) LIMIT 1",
                    (str(domain["id"]), since),
                ).fetchone()
                if planned_today:
                    continue
                jobs.enqueue(conn, "strategize", {"domain_id": domain["id"]})
                log.info("scheduled planning for %s", domain["slug"])
                enqueued += 1
        span.set_attribute("collegium.planned", enqueued)
        if enqueued:
            _instruments.planned_total.add(enqueued)
        return enqueued


def run_forever(db: Database, hours: WorkingHours, poll_seconds: float = 60) -> None:
    log.info("working hours: %s", hours.describe())
    while True:
        tick(db, hours)
        time.sleep(poll_seconds)
