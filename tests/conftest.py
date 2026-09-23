"""Test fixtures.

Tests need a Postgres server (`docker compose up -d db`). A template
database is migrated once per session; each test gets a fresh copy, since
memory tables cannot be emptied.
"""

import os
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel

from collegium.acquisition import Acquisition, Document, SearchResult
from collegium.config import Settings
from collegium.db import Database
from collegium.roles.base import Context

ADMIN_URL = os.environ.get(
    "COLLEGIUM_TEST_ADMIN_URL", "postgresql://collegium:collegium@localhost:5432/postgres"
)
MIGRATIONS = Path(__file__).parent.parent / "db" / "migrations"


def _up_section(text: str) -> str:
    """What dbmate runs: the part between migrate:up and migrate:down."""
    return text.split("-- migrate:up", 1)[1].split("-- migrate:down", 1)[0]


@pytest.fixture(scope="session")
def admin():
    try:
        conn = psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not available ({e}); run `docker compose up -d db`")
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def template(admin):
    name = f"collegium_template_{uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(make_conninfo(ADMIN_URL, dbname=name), autocommit=True) as conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            with conn.transaction():
                conn.execute(_up_section(path.read_text()))
    yield name
    admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def db_url(admin, template):
    name = f"collegium_test_{uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    yield make_conninfo(ADMIN_URL, dbname=name)
    admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def worker_db(db_url):
    db = Database(db_url, set_role="collegium_worker")
    yield db
    db.close()


@pytest.fixture
def board_db(db_url):
    db = Database(db_url, set_role="collegium_board")
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _approve_all_observations(user: str):
    from collegium.roles.scout import ObservationCheck, ObservationChecks

    numbers = [int(n) for n in re.findall(r"^\[(\d+)\] Statement:", user, re.MULTILINE)]
    return ObservationChecks(checks=[ObservationCheck(number=n, supported=True) for n in numbers])


# Responses used when a test has not scripted one: the Scout's check of its
# own observations passes unless a test says otherwise.
DEFAULTS = {"ObservationChecks": _approve_all_observations}


class ScriptedLLM:
    """Returns queued responses per schema, and records every prompt."""

    model = "scripted"

    def __init__(self):
        self._queue: dict[type, list] = defaultdict(list)
        self.calls: list[tuple[type, str]] = []

    def add(self, schema: type[BaseModel], response: BaseModel | Callable | Exception):
        self._queue[schema].append(response)

    def generate(self, system, user, schema):
        self.calls.append((schema, user))
        if not self._queue[schema] and schema.__name__ in DEFAULTS:
            return DEFAULTS[schema.__name__](user)
        if not self._queue[schema]:
            raise AssertionError(f"no scripted response for {schema.__name__}")
        response = self._queue[schema].pop(0)
        if isinstance(response, Exception):
            raise response
        return response(system, user) if callable(response) else response

    def prompts_for(self, schema: type) -> list[str]:
        return [user for s, user in self.calls if s is schema]


class FakeProvider:
    """Every search returns every page, highest score first."""

    name = "fake"

    def __init__(self, pages: dict[str, tuple[str, str]], dates: dict[str, str] | None = None):
        self.pages = pages  # url -> (title, content)
        self.dates = dates or {}  # url -> published date
        self.searches: list[tuple[str, int | None]] = []

    def discover(self, query, max_results, *, recent_days=None):
        self.searches.append((query, recent_days))
        return [
            SearchResult(
                url=url,
                title=title,
                snippet=content[:120],
                score=1 - i / 10,
                published_at=self.dates.get(url),
            )
            for i, (url, (title, content)) in enumerate(self.pages.items())
        ][:max_results]

    def extract(self, urls):
        return [Document(url=u, title=self.pages[u][0], content=self.pages[u][1]) for u in urls]


@pytest.fixture
def llm():
    return ScriptedLLM()


@pytest.fixture
def make_context(worker_db, llm):
    def make(
        pages: dict[str, tuple[str, str]] | None = None,
        dates: dict[str, str] | None = None,
        extra_sources: dict[str, "FakeProvider"] | None = None,
        web: "FakeProvider | None" = None,
        crawler=None,
    ) -> Context:
        web = web or FakeProvider(pages or {}, dates)
        acquisition = Acquisition(
            {"fake": web, **(extra_sources or {})}, web, default="fake", crawler=crawler
        )
        return Context(db=worker_db, llm=llm, acquisition=acquisition, settings=Settings())

    return make


@pytest.fixture
def add_domain(board_db):
    def add(slug: str = "ai-agents", name: str = "AI and agents"):
        with board_db.acting_as("owner") as conn:
            return conn.execute(
                "INSERT INTO domains (slug, name) VALUES (%s, %s) RETURNING id", (slug, name)
            ).fetchone()["id"]

    return add


# One searchable page, so the Scout has something besides feeds.
PAGES_FOR_FEEDS = {
    "https://news.example/agents": (
        "Agent costs in the news",
        "Reporters looked at what companies spend on AI agents this year.",
    )
}
