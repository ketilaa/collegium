# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Milestones 1 (institutional memory), 2 (research workflow), 2.5 (acquisition layer), 3 (strategy layer) and 4 (the board, including Mission, Contradictions and Ask the Organization) are done. Since then: free-first acquisition (own SearXNG and web reader, Tavily only as a paid fallback), source proposals, and the community agent on Moltbook (reads; drafts posts and replies that the owner approves; a separate publisher sends them). The organization runs live on the owner's laptop; see `docs/operations.md`. The code is public at https://github.com/ketilaa/collegium under the Apache License 2.0 (first pushed 2026-09-25); every push needs a PASS review first (see Pushing).

Where to look, and how much to read:
- `VISION.md` is the source of truth for intent; read the relevant section before design decisions.
- `docs/decisions.md` records every technical decision and why, newest last (about 60 KB). Do not read it whole: `grep -n "^## " docs/decisions.md` for the headings, then read the entries that bear on the task. Add an entry for each new decision.
- `docs/operations.md`: how the live instance is run, deployed, inspected and changed by hand.
- `docs/reviews/README.md`: the security review before a push.
- The owner's secrets live in `~/.collegium/.env`: never read or print that file, only pass it to commands (`docker compose --env-file ...`, or `set -a; . file; set +a` in a command).

Open threads (update this list as they close):
- **To propose, from the first push's reviews** (`docs/reviews/2026-09-25-*`): SEC-8 (DNS rebinding in the web reader), SEC-12 (push-guard gaps), SEC-14 (the bundle should list every path and binary file ever in the range) and SEC-16 (a history scan for sensitive terms, the terms kept in an untracked local file, never committed).
- **Offered, not yet approved:** an adapter for NAV/SSB (Norwegian statistics); Milestone 5.

## Working with the owner

- The owner is the board chair: propose, with a recommendation, before building anything substantial; build once they agree. Ask only about decisions that are theirs to make.
- Commit, apply migrations, rebuild and push only when the owner asks (they usually say which: "commit, apply migration and rebuild"). A migration is never applied without explicit approval; deploy with `--no-deps` (see `docs/operations.md`).
- Before committing: `uv run pytest -q` and `uv run ruff check . && uv run ruff format .` pass, and `docs/decisions.md` has an entry for any decision made. Commit messages say what and why, and end with the Co-Authored-By line.
- Report what was done plainly, including what failed or was skipped.

## Pushing

Nothing is pushed without a PASS from the `security-reviewer` subagent: run `scripts/security-review-bundle.sh`, give the subagent the bundle directory it prints, save its report under `docs/reviews/` with the file name from the bundle's `meta.txt`, and commit it. On FAIL, fix and repeat (the next iteration). The pre-push hook in `.githooks/` enforces it (`git config core.hooksPath .githooks` in each clone); never bypass it. Process, finding IDs (`SEC-n-i`), severities and dispositions: `docs/reviews/README.md`.

## Stack

- **Python 3.12+** for API, worker and scheduler, managed with `uv`. FastAPI for the API, psycopg 3 with plain SQL (no ORM), Pydantic for validating LLM output. Web UI server-rendered (Jinja + htmx).
- **dbmate** for migrations: plain SQL files in `db/migrations/`, each with `-- migrate:up` and an empty `-- migrate:down`. dbmate wraps each file in a transaction, so files contain no `BEGIN`/`COMMIT`. Never edit an applied migration; add a new one.
- The scheduler is a jobs table in Postgres read with `FOR UPDATE SKIP LOCKED`, not Redis or Celery.

## Commands

```sh
# The live instance: always --env-file ~/.collegium/.env, and see docs/operations.md.
docker compose up -d db                  # Postgres on 127.0.0.1:5432; passwords from .env
docker compose run --rm migrate          # apply pending migrations (only with the owner's approval)
docker compose run --rm --no-deps logins # create/update the logins (without --no-deps it runs migrate first)
docker compose up -d                     # a fresh setup only: this also runs migrate
docker compose up -d --no-deps <service> # a running setup: recreate without migrating

docker compose up -d backup              # daily pg_dump into COLLEGIUM_BACKUP_DIR (live: ~/.collegium/backups)

uv run pytest                            # all tests (needs the db; live: load the env first, for POSTGRES_PASSWORD)
uv run pytest tests/test_pipeline.py::test_skeptic_rejection_is_recorded   # one test
uv run ruff check . && uv run ruff format .

uv run collegium --help                  # CLI; needs env vars from .env.example
uv run collegium web                     # the board on http://localhost:8000 (no login yet)
uv run collegium domain add ai-agents "AI and agents"   # owner commands use the board login
uv run collegium scout ai-agents
uv run collegium worker --drain          # process due jobs, then exit
uv run collegium hypotheses
uv run collegium why <id-prefix>          # hypothesis or observation, with its citation chain
uv run collegium jobs
uv run collegium domain sources ai-agents tavily hackernews   # Scout's discovery sources
uv run collegium acquisitions            # recent calls to external providers
uv run collegium entities [slug]         # entities mentioned most
uv run collegium resolve --all           # send hypotheses with open critiques through the loop
uv run collegium strategize ai-agents    # plan now (otherwise daily)
uv run collegium goals | programs | decisions
uv run collegium approve <id> | reject <id> --reason "..."   # the owner's decisions
uv run collegium challenge <id> "objection" --severity 3      # the owner's critique; goes through the loop
uv run collegium domain pause|resume|retire ai-agents
uv run collegium mission ["statement"] [--domain ai-agents]   # show, or set, the owner's missions
uv run collegium ask "question" [--domain ai-agents]          # Ask the Organization (answered from memory)
uv run collegium questions                                    # questions and their answers
uv run collegium draft <hypothesis-id>    # a Moltbook post about it, for the owner to approve
uv run collegium posts | withdraw <id>    # the Moltbook outbox; publisher: `collegium publisher`
uv run collegium feed add ai-agents <url> # approve a feed for a domain (also list/pause/resume/retire)
```

Tests create a migrated template database and give each test a fresh copy (memory tables cannot be emptied). They connect as the `collegium` superuser with `SET ROLE collegium_worker`/`collegium_board`, so table grants are exercised, but the owner/system impersonation checks (which look at the login user) are not. Tests use `ScriptedLLM` and `FakeProvider` from `tests/conftest.py`; no model or API key is needed.

## What Collegium is

A persistent research organization with institutional memory, not a request/response chatbot. It keeps working between user sessions. Its core products are accumulated, structured, explainable knowledge and the history of how that knowledge changed. The user acts as board chair: they set research areas and priorities, review findings and challenge conclusions. They do not assign individual tasks.

## Architecture constraints (from VISION.md)

- **Deployment:** Docker Compose with five components: PostgreSQL, API, Worker, Scheduler and Web UI (API and Web UI are one `web` service for now), plus the owner-approved `searxng` search service and the Moltbook `publisher`. It must run on a laptop, a small VM or a cloud VM with minimal changes.
- **PostgreSQL is the only datastore.** Do not add Kubernetes, extra databases, graph databases, event streaming or complex cloud services until a real need is shown. Model relationships as tables in Postgres.
- **Agents are roles, not services.** One worker process runs every role (Scout, Researcher, Skeptic, Strategist). Roles differ by role definition and memory, not by infrastructure.
- **One local LLM for every role** (for example Qwen3 14B or Gemma 3 12B). Do not specialize models per role.
- **Least privilege:** agents may read and search external sources, analyze them and write to organizational memory. They must not send email, post, publish, buy anything or modify any external system. The one exception is the community agent on Moltbook (see VISION.md and `docs/decisions.md`): posts and replies drafted from memory, approved one by one by the owner, published by a separate component (`publisher.py`, its own service and database login, which reaches only the outbox) that alone holds the key. Posts never say that the owner approved them.
- **Provider-agnostic acquisition:** agents call abstract capabilities (`search`, `extract`, `crawl`, `discover_related`). Vendor APIs (Tavily, Exa, Firecrawl, Brave) are reached only through adapters behind those capabilities, never directly from role code. The Historian never searches externally, and the Strategist searches only when memory shows a real knowledge gap. Over time, memory should be consulted before external search.
- **Domain-agnostic:** AI is the first research domain, but adding a new domain (for example Norwegian consulting companies or energy) must not require schema or code changes.
- Prefer clarity and simplicity over scalability.

## Roles

| Role | Output | Note |
|---|---|---|
| Historian | Stored memory and audit trail | Never does research. It is the organization's identity and must survive replacement of every other role. |
| Scout | Observation proposals | Broad, breadth-first exploration; looks for weak signals. |
| Researcher | Findings, hypotheses, evidence | Investigates observations. |
| Skeptic | Critiques, confidence adjustments | Nothing enters long-term knowledge without passing the Skeptic. |
| Strategist | Goals, research programs | Finds knowledge gaps and allocates attention. |

Core workflow: Scout → Researcher → Skeptic → Historian. The Strategist sits above it and creates the investigations.

## Code architecture

- **Pipeline:** work is a chain of jobs in the `jobs` table: `scout` (per domain) → `research` (per observation) → `review` (per hypothesis) → `record`, The scheduler queues a `strategize` job per domain once per working day (`hours.py`: default Mon-Fri 08:00-16:00 Europe/Oslo; the worker also starts jobs only within working hours, except `worker --drain`); the Strategist (`roles/strategist.py`, gaps in `strategy.py`) sets goals, abandons ones that no longer serve the missions (reason kept in `goals.outcome`), and queues `scout` (with a focus), `corroborate` and `resolve` jobs within the budget, and proposes programs as decisions for the owner. Also `ask` (the owner's question, answered by the Researcher from memory only, at any hour: `worker.ANY_HOUR`), `draft` (a Moltbook post asking about a hypothesis; queued by the owner, or by the Strategist for a stalled one) and `reply` (a reply to a Moltbook post or comment from what memory holds, held to Ask's firmness rules; queued by the owner, or by the Scout), both from memory only, at any hour, and both ending as proposed decisions (topics `post`, `reply`) that the owner approves into the `community_posts` outbox, `map` (entities mentioned, after research) and the critique loop: `record` → `resolve` (Researcher investigates open critiques) → `review` (Skeptic settles them) → `record`, at most 2 rounds. Each role enqueues the next step. Owner commands and the scheduler enqueue `scout` jobs.
- **Prepare/persist:** a role's `prepare()` (`src/collegium/roles/`) does the slow work (reading memory, searching, calling the model) outside any transaction, and returns a `persist(conn)` function. `worker.run_once` runs that function in one transaction acting as the role, together with finishing the run and the job, so knowledge and follow-up jobs commit atomically. On failure, the job is retried with backoff and nothing is written.
- **Runs:** every job execution creates a `runs` row with the model and `role_version` (a hash of the role's prompt files), and all rows written carry that run id.
- **Grounding** (`grounding.py`): the model cites search results and documents by number, not URL. Out-of-range citations are dropped, and evidence is stored only if its excerpt is found in the document. The stored excerpt is the source's own wording. Scout observations must quote their lead; names and numbers in the statement must occur in the quote (`unsupported_terms`; numbers compared by digits, and for quotes that are not English only acronyms and mixed-case names are checked), and a model call checks each statement against its quote. Memory is written in English; excerpts and quotes stay in the source's language. The quote is stored as evidence supporting the observation.
- **Historian:** deterministic rules, no model. It is the only role that changes a hypothesis's status (accept/reject/under review, and superseding hypotheses that an accepted one `refines`). Accepting needs confidence ≥ 0.6, supporting evidence from at least two independent sites, and no open or upheld critique of severity ≥ 3; the Skeptic's verdict is only a veto (owner's decision). Changing a rule means bumping `Historian.version()`.
- **Free first:** search goes to the organization's own SearXNG and pages are read by `acquisition/web.py` (direct fetch plus trafilatura). Pages and feeds are fetched through `acquisition/fetching.py` (`SafeFetcher`): non-public addresses refused at every redirect, robots.txt respected, bodies capped. Tavily is the paid fallback for both: pages the web reader cannot read (never social media, forums or Moltbook: `NOT_WORTH_PAYING`), and searches SearXNG fails or finds nothing for. When SearXNG's engines are blocked (`SearchUnavailable`: no results and unresponsive engines), nothing is paid instead: the job waits 30 minutes without using an attempt. `searxng.py` leaves 3 seconds between queries, and `searxng/settings.yml` spreads them over Bing, Mojeek, Yahoo, Qwant, Brave and DuckDuckGo.
- **Budget:** paid providers set `metered = True`; at most `COLLEGIUM_DAILY_CALL_BUDGET` (50) paid calls per 24 hours. When the main route is paid (`Acquisition.paid_first`), jobs wait for budget (`jobs.defer`, no attempt used); when it is free, a spent budget only skips the paid fallbacks. The Strategist plans within the paid budget, or up to `MAX_FREE_ACTIONS` actions when search is free. Roles that make no external calls set `searches = False` (`searches_for(job)` per job: the Skeptic does not search in rounds after a resolve).
- **Recency:** the Scout searches news within `COLLEGIUM_SCOUT_RECENT_DAYS` and drops older results. The Researcher and Skeptic search without a window.
- **Reliability** (`reliability.py`): evidence reliability is the model's score capped by a ceiling per source type (social/video, including LinkedIn, 0.3; forum 0.4; press release and blog platform 0.5). Observations from social media and Moltbook are not researched (`NOT_WORTH_RESEARCH`).
- **Identity** (`identity.py`): every request the organization makes itself carries `USER_AGENT`: `Collegium/0.1 (+https://github.com/ketilaa/collegium; research agent; operated by <COLLEGIUM_CONTACT>)`. The link names the software; the operator part appears only when `COLLEGIUM_CONTACT` is set (the worker and publisher warn when it is not), so no copy speaks as its authors. New adapters must set it. Sites can address it as "Collegium" in robots.txt, which the web reader obeys.
- **Untrusted text** (`untrusted.py`): all outside text is sanitized in the acquisition layer (control tokens and hidden characters removed; injection signals stored in item metadata as `injection_signals`). Any outside text put into a prompt, including excerpts read back from memory, must be wrapped with `fence()`. Evidence from flagged documents is capped at reliability 0.3.
- **Labels in prompts:** existing hypotheses are shown to the model as `E1..En`, new ones are `H1..Hn`, and the Skeptic's target is `H`. Code maps labels to ids; the model never sees UUIDs.
- **Acquisition** (`acquisition/`): roles use the `Acquisition` facade (`discover`, `extract`, `gather`), never a provider. `Discovery` and `Extractor` are the provider protocols; vendor code lives only in adapters (`searxng.py`, `web.py`, `tavily.py`, `hackernews.py`, `moltbook.py`, `feeds.py`). `moltbook.py` is read-only and keyless (it also reads replies to the organization's own posts); Moltbook is an "AI-agent forum" source: capped at 0.3, never paid for, never sent for research. The worker gives each run a recording `Acquisition`, so every external call lands in `acquisitions` with its run, and sources carry the `acquisition_id` that found them. Providers with `thin_leads = True` get their top leads enriched from the page. `memory.py` is the only module that writes knowledge rows.
- **Board** (`web/`): one FastAPI app, server-rendered with Jinja and htmx, connecting as the board login. Read queries live in `board.py` and the owner's actions in `owner.py`, both shared with the CLI. Forms post to `web/actions.py`; posts are accepted only from the board's own origin, and only for host names in `COLLEGIUM_WEB_ALLOWED_HOSTS`. Outside text is shown only through Jinja autoescaping, source links only when `http_url` allows them, and a strict CSP forbids anything not served by the app. There is no login yet, so it listens on localhost only.
- **Prompts** live in `src/collegium/roles/prompts/`: `organization.md` is shared and each role has its own file. Changing a prompt changes that role's `role_version`.

## Memory model

Memory is the most important asset. Store structured records, not free-form reports. The core concepts are Entity, Observation, Hypothesis, Evidence, Relationship, Program, Goal and Decision.

Every belief must be explainable. The schema must be able to answer:
- Why do we believe this, and since when?
- Which evidence supports it, and which contradicts it?
- Who (which role) proposed it?
- How has confidence changed over time?

This means history and provenance must be kept, not overwritten.

### Schema conventions (enforced by the database)

- Every writing transaction must first run `SELECT set_config('collegium.actor_id', '<actor uuid>', true)` and, optionally, the same for `collegium.run_id`. Writes without an actor are rejected. Provenance columns (`created_by`, `retracted_by`, `resolved_by`, ...) default to that actor; omit them, since any other value is rejected.
- Each knowledge record is a `nodes` row plus a row with the same id in its own table (`entities`, `hypotheses`, and so on). Insert both in one statement: `WITH n AS (INSERT INTO nodes (kind) VALUES ('hypothesis') RETURNING id) INSERT INTO hypotheses ...`.
- `relationships` and `critiques` are nodes too, so they can be critiqued, linked and backed by evidence. `evidence_links` and `confidence_assessments` target a node by `(target_id, target_kind)`.
- Nothing is deleted: knowledge is retired through `status`, and links are retracted with `retracted_at`. Statements are never edited; a changed claim is a new record that supersedes the old one. `confidence_assessments` and `audit_log` are append-only. Every table is audited automatically.
- Access goes through group roles: `collegium_reader`, `collegium_worker` (agents: insert knowledge, update only lifecycle columns) and `collegium_board` (the owner's interface: also domains and resolving decisions). Only board logins may act as the `owner` actor. Any table added in a later migration must be granted to these roles explicitly. Login users (`collegium_worker_app`, `collegium_board_app`) are created by `db/logins.sql`, not by migrations, because passwords are deployment secrets.
- Only the owner decides: a decision that is not `proposed` may only be written by the `owner` actor (trigger), so agents can propose but never approve. The owner's missions are approved decisions with `topic = 'mission'`, tagged to their domain through `node_domains` (none for the organization's); a new mission supersedes the old.
- Actors: `owner`, `system`, one per role, and `scheduler` (kind `service`). Unaudited tables that need actor checks call `assert_session_actor()` from a trigger, as `jobs` does.
- Domains and vocabularies are data. Adding a research area never needs a migration.

## Milestones

All of 1-4 are done; 5 is next when the owner chooses.

1. Institutional memory: Postgres schema and audit history. Knowledge must survive restarts and agent replacement.
2. Research workflow: Scout, Researcher, Skeptic and Historian, with a minimal acquisition layer (`search`/`extract`, one Tavily adapter).
2.5. Knowledge acquisition layer: discover/extract split, Hacker News, SearXNG, web reader, feeds, Moltbook, per-domain discovery sources, call log, grounded Scout observations, related entities (`map` jobs) and citation tracking (`collegium why`).
3. Strategy layer: Strategist, knowledge-gap detection and research programs. Includes the critique-resolution loop: open critiques are investigated and resolved, which is how hypotheses come to be accepted. Until then almost nothing is accepted, by design.
4. Board interface: a dashboard with Mission, Programs, Goals, Hypotheses, Contradictions, Recent Discoveries and "Ask the Organization".
5. Long-term evolution: cross-domain knowledge and belief revision.
