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

## 2026-09-23 · Milestone 2 runtime design

**Model access.** One client for any OpenAI-compatible chat endpoint
(Ollama, llama.cpp, vLLM), defaulting to Ollama with `qwen3:14b`. Replies
are constrained with a JSON schema and validated with Pydantic; an invalid
reply is sent back with the validation error, up to three attempts.

**Grounding over trust.** A 14B model will invent or tidy quotes. The model
cites search results and documents by number, and evidence is stored only
when its excerpt can be found in the retrieved text (allowing for case,
whitespace, quote style and light paraphrase). The stored excerpt is the
source's own wording. Ungrounded evidence is dropped and counted in the
run's notes.

**Prepare, then persist.** Roles do slow work outside transactions and
return a function that writes everything at once. The worker commits a
role's knowledge, its follow-up jobs, the run's outcome and the job's
completion in one transaction, so a crash never leaves half a result or a
lost step.

**The Historian is rules, not a model.** It is the only role that changes a
hypothesis's status: accepted at confidence ≥ 0.6 with the Skeptic's
agreement, rejected at ≤ 0.25 or on the Skeptic's rejection, otherwise under
review. When a hypothesis that `refines` another is accepted, the older one
is superseded. Keeping belief changes deterministic makes them explainable
and lets the thresholds be versioned (`role_version`).

**Refinement through labels.** The Researcher sees live hypotheses in the
domain as `E1..En` and can attach evidence to them or propose a refinement,
rather than creating near-duplicates. The model never handles UUIDs.

**Queue in Postgres.** Jobs are claimed with `FOR UPDATE SKIP LOCKED`, retried
with exponential backoff, and taken over if a worker dies mid-job. The
scheduler is a separate `service` actor, so recurring work is attributed to
it rather than to an agent or the owner.

**Tavily first.** It provides search and extraction through one API, which
is what Milestone 2 needs. Other providers come in Milestone 2.5.

**Login users outside migrations.** `db/logins.sql` creates the worker and
board logins with passwords from the environment, and Compose runs it after
every migration.

## 2026-09-23 · Lessons from the first real runs

The first runs used Qwen2.5 7B on llama.cpp and live Tavily search, in
throwaway databases. The pipeline worked end to end, but the first run's
knowledge was weak: hypotheses restated observations, three of four had no
grounded evidence, irrelevant excerpts were marked as contradicting, and all
observations came from one vendor blog. Changes made:

- **A new hypothesis needs grounded support.** It is stored only if at
  least one excerpt found in the sources supports it. Grounding now happens
  before anything is written.
- **Grounding tolerates harmless differences.** Quote marks and markdown
  emphasis are ignored. If the model cites the wrong document number, the
  other retrieved documents are tried, and the evidence is attributed to the
  one that actually contains the excerpt. Excerpts over 600 characters are
  rejected as not being a passage.
- **Dropped excerpts are recorded** in the run's notes, so what the
  organization discarded can be inspected.
- **Queries are cleaned** of labels, quotes and exclusion operators.
- **Page text is cleaned** of markdown link targets and images before
  truncation, which otherwise filled the context with navigation.
- **Prompts** now define "contradicts" (incompatible, not merely silent),
  require hypotheses to explain rather than restate, ask the Scout for
  source diversity, and tell the Skeptic that thin evidence means undecided.

After these changes, the observations came from independent sources and
every hypothesis had grounded support. A 14B-class model, as VISION.md
assumes, should quote more accurately than the 7B used here.

## 2026-09-23 · Recency for the Scout, independence for acceptance

Two problems from the third trial run:

- **Old news recorded as new.** Basic search returns undated results, so
  the Scout recorded 2024 model launches as current events. The Scout now
  searches news (`recent_days`, 30 by default, `COLLEGIUM_SCOUT_RECENT_DAYS`)
  and drops any result dated before the window. An observation without a
  date from the model takes the publication date. The Researcher and
  Skeptic still search without a window, since background and history are
  part of an investigation.
- **Accepting a claim on the claimant's word.** A hypothesis was accepted at
  0.90 on the vendor's own benchmark claims. The Historian now also
  requires active supporting evidence from at least two independent sites
  (pages on one host count as one) before accepting. Otherwise the
  hypothesis stays under review. The Historian's `role_version` is now
  `rules-2`.

## 2026-09-23 · 14B model run: duplicates, and how beliefs get accepted

Trial 4 ran on Qwen2.5 14B (llama.cpp, 16k context). Recency and the
independent-source rule worked: all observations were from the previous
five weeks and from established outlets, and grounding improved sharply
(1 of 13 research excerpts dropped).

- **Duplicate hypotheses.** The model re-proposed a hypothesis it had been
  shown, word for word. The Researcher now looks for a live hypothesis in
  the same domains with the same statement (ignoring case, spacing and a
  final period) inside the writing transaction. On a match it links the new
  evidence to the existing hypothesis, records it as also derived from the
  new observation, and sends it for review again, instead of creating a
  copy. Near-duplicates with different wording are still possible; semantic
  matching can come with memory search.
- **Nothing was accepted.** The Skeptic left critiques open and answered
  "undecided" on 10 of 11 hypotheses, including well-supported ones, and
  nothing resolves an open critique. Decided by the owner: acceptance will
  come from a critique-resolution loop in Milestone 3, where open critiques
  are sent back for investigation and resolved, rather than from loosening
  the Historian's rules now.

## 2026-09-23 · Planned for Milestone 2.5: Hacker News as a discovery source

Hacker News is a strong early-signal source for technology domains:
papers, launches and practitioner write-ups often appear there before the
press covers them, and points and comment counts measure attention. The
Algolia HN Search API (`hn.algolia.com/api/v1/search_by_date`) is free,
needs no key and filters by date, so it fits the Scout's recency window
and the rule against purchasing services.

Design, to be built in Milestone 2.5:

- **Discovery, not evidence.** A story is a pointer. The linked article is
  recorded as the source; the HN discussion URL, points and comment count
  go in the source's metadata. Otherwise every finding would count as the
  single site `news.ycombinator.com` under the independent-sources rule.
  Text-only posts (Ask HN, Show HN) are their own source.
- **Split capabilities.** `AcquisitionProvider` becomes separate discover
  and extract capabilities, so the Scout can discover through HN and Tavily
  together while pages are still extracted by Tavily.
- **Per-domain sources.** HN is useful for AI, software and startups and
  close to useless for domains such as Norwegian consulting or energy. Each
  domain lists its discovery sources as data (a column on `domains`, one
  migration), so adding a domain still needs no code change.
- **Comments later.** Practitioner comments could serve the Skeptic as
  counter-arguments, but they are opinion rather than evidence; they are
  left out at first.

## 2026-09-23 · Milestone 2.5, part 1: discovery, extraction and the call log

Built as planned above, with these details settled on the way:

- **Two capabilities.** `Discovery` providers find leads and one
  `Extractor` reads pages. Roles use the `Acquisition` facade, never a
  provider. Tavily implements both; Hacker News is discovery only.
- **Per-domain sources.** `domains.discovery_sources` names the Scout's
  providers (`collegium domain sources <slug> tavily hackernews`); empty
  means the default. Leads from several sources are interleaved so none
  crowds out the others. The Researcher and Skeptic use the default source.
- **Every external call is recorded** in the append-only `acquisitions`
  table with the query or urls, the result count or error, the actor and
  the run, in its own transaction, so calls survive a failed run. Each
  source points at the call that found it (`sources.acquisition_id`).
  `collegium acquisitions` shows what has been sent to providers.
- **Hacker News leads.** HN search matches keywords, so the adapter drops
  filler words and marks the rest optional; it only returns stories with at
  least 30 points, since HN's value is practitioner attention. HN leads are
  headlines, so the acquisition layer enriches the top three thin leads of
  any source with the opening of their page (logged as extract calls; a
  failed enrichment keeps the leads).
- **TLS through the OS trust store.** The CLI verifies certificates against
  the operating system's store (`truststore`), so networks that inspect
  HTTPS with a locally trusted certificate, such as Zscaler, work.

Observed in live runs: with both sources, the model chose mainstream news
leads over HN leads for broad queries in every run. HN is wired in and its
leads carry content; whether it earns its place should be judged over time
and in narrower queries.

Found and not yet fixed: Scout observations are the model's own sentences
and are not checked against the lead they cite. One run produced "Anthropic
CEO Sam Altman", which is wrong. Observations should be grounded the way
evidence is.

## 2026-09-23 · Milestone 2.5, part 2: grounded observations

Scout observations were the model's own sentences, and one read "Anthropic
CEO Sam Altman". Observations are now held to the same standard as
evidence, in three layers:

1. **Quote.** The Scout copies the words from the result that state the
   observation. The quote must be found in the result's title or snippet.
2. **Names and numbers.** Every capitalised word and number in the
   statement must occur in the quote or title. This catches invented names
   and figures cheaply, but not a wrong association of names that both
   occur.
3. **Check by the model.** The surviving statements are sent back, each
   with its quote, in one call: is every name, role, number and date stated
   by the quote? A statement without a positive verdict is dropped. A
   focused yes/no check on a short quote is far more reliable than the
   original generation.

The quote is stored as evidence supporting the observation, so every
observation shows the source's own words. Rejections are counted in the
run's notes. In the first live run, 5 of 5 quotes were found and 1
statement failed the model's check.
