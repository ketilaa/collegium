"""The scheduler: keeps the organization working between owner sessions.

For now it only asks the Scout to explore each active domain at a fixed
interval. From Milestone 3 the Strategist decides where attention goes.
"""

import logging
import time
from datetime import timedelta

from collegium import jobs, memory
from collegium.db import Database

log = logging.getLogger(__name__)

ACTOR = "scheduler"


def tick(db: Database, scout_interval: timedelta) -> int:
    """Enqueue due scout jobs. Returns how many were enqueued."""
    enqueued = 0
    with db.acting_as(ACTOR) as conn:
        for domain in memory.active_domains(conn):
            busy_or_recent = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = 'scout' AND payload->>'domain_id' = %s "
                "AND (status IN ('pending', 'running') OR created_at > now() - %s) LIMIT 1",
                (str(domain["id"]), scout_interval),
            ).fetchone()
            if busy_or_recent:
                continue
            jobs.enqueue(conn, "scout", {"domain_id": domain["id"]})
            log.info("scheduled scouting of %s", domain["slug"])
            enqueued += 1
    return enqueued


def run_forever(db: Database, scout_interval: timedelta, poll_seconds: float = 60) -> None:
    while True:
        tick(db, scout_interval)
        time.sleep(poll_seconds)
