"""Database access.

Every write happens inside `Database.acting_as`, which opens a transaction
and declares the actor (and optionally the run) for it. The schema rejects
writes without an actor and fills provenance columns from it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

Connection = psycopg.Connection


class Database:
    def __init__(self, conninfo: str, *, set_role: str | None = None):
        # set_role switches to a group role after connecting. Tests use it to
        # run as collegium_worker without separate login users.
        self._conninfo = conninfo
        self._set_role = set_role
        self._conn: Connection | None = None
        self._actor_ids: dict[str, UUID] = {}

    @property
    def conn(self) -> Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._conninfo, row_factory=dict_row, autocommit=True)
            if self._set_role:
                self._conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self._set_role)))
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def actor_id(self, name: str) -> UUID:
        if name not in self._actor_ids:
            row = self.conn.execute("SELECT id FROM actors WHERE name = %s", (name,)).fetchone()
            if row is None:
                raise LookupError(f"unknown actor {name!r}")
            self._actor_ids[name] = row["id"]
        return self._actor_ids[name]

    @contextmanager
    def acting_as(self, actor: str, run_id: UUID | None = None) -> Iterator[Connection]:
        conn = self.conn
        with conn.transaction():
            conn.execute(
                "SELECT set_config('collegium.actor_id', %s, true)", (str(self.actor_id(actor)),)
            )
            if run_id is not None:
                conn.execute("SELECT set_config('collegium.run_id', %s, true)", (str(run_id),))
            yield conn

    @contextmanager
    def reading(self) -> Iterator[Connection]:
        conn = self.conn
        with conn.transaction():
            yield conn
