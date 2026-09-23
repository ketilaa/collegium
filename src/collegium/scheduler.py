"""The scheduler: keeps the organization working between owner sessions.

Once per interval it asks the Strategist to plan each active domain. The
Strategist decides what to scout, corroborate or resolve, within the
budget, and always keeps one scout a day.
"""

import logging
import time
from datetime import timedelta

from collegium import jobs, memory
from collegium.db import Database

log = logging.getLogger(__name__)

ACTOR = "scheduler"


def tick(db: Database, interval: timedelta) -> int:
    """Queue due planning jobs. Returns how many were queued."""
    enqueued = 0
    with db.acting_as(ACTOR) as conn:
        for domain in memory.active_domains(conn):
            busy_or_recent = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = 'strategize' AND payload->>'domain_id' = %s "
                "AND (status IN ('pending', 'running') OR created_at > now() - %s) LIMIT 1",
                (str(domain["id"]), interval),
            ).fetchone()
            if busy_or_recent:
                continue
            jobs.enqueue(conn, "strategize", {"domain_id": domain["id"]})
            log.info("scheduled planning for %s", domain["slug"])
            enqueued += 1
    return enqueued


def run_forever(db: Database, interval: timedelta, poll_seconds: float = 60) -> None:
    while True:
        tick(db, interval)
        time.sleep(poll_seconds)
