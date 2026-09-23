# Decisions

Technical decisions and why they were made. Newest last. When a decision
is reversed, add a new entry rather than editing the old one.

## 2026-09-23 · Memory schema: one node table for all knowledge

Every knowledge record (entity, observation, hypothesis, evidence, critique,
relationship, program, goal, decision) has a row in `nodes` and a row in its
own table with the same id. Relationships, critiques, evidence links,
confidence assessments and domain tags can then point at any record with a
real foreign key, without a graph database.

Relationships and critiques are records too, so they can be critiqued,
linked and backed by evidence like any claim.

Confidence is never stored on a record. It is the latest row in the
append-only `confidence_assessments` table, which is what lets the
organization answer "how has confidence changed over time?".

## 2026-09-23 · Enforcement lives in the database

Memory rules are enforced by Postgres, not by application code, so they
hold for any client in any language and cannot be skipped by a bug in one
code path.

- **Roles** decide what each process may do. `collegium_reader` reads.
  `collegium_worker` (the agents) inserts knowledge and changes only
  lifecycle columns. `collegium_board` (the owner's interface) can also
  manage domains and resolve decisions.
- **Triggers** enforce what must hold for everyone, including the schema
  owner: every change is audited, every write names an actor, provenance
  columns must match that actor, nothing is deleted, and history tables are
  append-only.
- Only board logins may act as the owner actor, so an agent cannot approve
  its own strategic proposal.

Trade-off: the triggers are Postgres-specific. VISION.md commits to
Postgres, so portability across application languages matters more than
portability across databases.

## 2026-09-23 · Python for application code

The hardest technical problem is getting reliable structured output from a
local 14B model. Python has the most mature tooling for that (Ollama and
llama.cpp clients, Pydantic validation) and for analysis work generally.

Stack: Python 3.12+, `uv`, FastAPI, psycopg 3 with plain SQL, Pydantic,
server-rendered web UI with Jinja and htmx. No ORM: the schema depends on
triggers, roles and session settings that an ORM would work against.

TypeScript was the alternative, giving one language for a richer web UI,
but its local-LLM tooling is less mature.

## 2026-09-23 · dbmate for migrations

Migrations are plain SQL, so the tool should not depend on the application
language. dbmate is a single binary with an official Docker image, runs
`.sql` files in transactions, and tracks what has been applied. It runs as
a one-shot `migrate` service in Compose.

Down migrations are left empty on purpose: institutional memory is never
rolled back. Mistakes are fixed with a new forward migration.

Alembic was rejected because its value is autogenerating migrations from
ORM models, and there are none.
