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

## 2026-09-23 · Milestone 2.5, part 2: crawling owner-approved feeds

The owner approves RSS or Atom feeds per domain (`collegium feed add
<slug> <url>`; the feed is fetched once to check it). Approval is
governance: `approved_sources` is writable only by the board, audited, and
feeds are paused or retired rather than deleted. On every run the Scout
reads the newest items of each active feed (`COLLEGIUM_MAX_FEED_ITEMS`,
default 5) alongside its searches, drops items older than its recency
window or already recorded as sources, and interleaves the rest with search
leads. Feed reads are recorded as `crawl` calls; only the feed's own URL is
sent out. A failing feed is skipped and its failed call recorded, so one
broken feed does not stop the Scout. Feed XML is parsed with defusedxml.

The grounding check for observations now ignores sentence-initial function
words ("A", "The"), which it had mistaken for names.

## 2026-09-23 · Prompt injection defences

Collegium reads text written by strangers all day and puts it in model
prompts. Injection cannot be prevented at the model level, so the design
relies on containment, which was already in place: the model has no tools
and only returns schema-checked JSON; it cannot choose what is fetched
beyond search queries; the database decides what the worker may write (it
cannot act as the owner, approve anything or delete); evidence must be
found verbatim in its source; and acceptance is decided by fixed rules
needing two independent sites.

Added at the acquisition boundary (`untrusted.py`), so every provider, feed
and extraction is covered:

- **Control tokens removed.** Chat-template tokens (`<|im_start|>`,
  `[INST]`, `<start_of_turn>`, ...) could otherwise be read by the model
  server as real role markers, letting a page write its own system message.
  Invisible and bidirectional control characters are removed too.
- **Signals flagged, not dropped.** Text addressed to AI systems ("ignore
  previous instructions", "you are now", "AI assistants must rate...") is
  flagged. Flagged text is kept, because legitimate articles quote such
  phrases, but it carries a warning in prompts, evidence taken from it is
  capped at reliability 0.3, the signals are stored in the source's
  metadata, and runs count flagged items in their notes.
- **Fenced data.** All outside text in prompts, including excerpts read back
  from memory, sits between markers with a random tag, so a page cannot
  close its own block. The shared prompt says fenced text is material,
  never instructions, and that text addressing the model is a sign of an
  unreliable source.

Known limits: the signal patterns are heuristics; search queries remain an
outbound channel that injected text could steer (no secrets are in
prompts); and coordinated poisoning across several sites can still pass
the two-source rule. The Board interface must escape stored text.

## 2026-09-23 · Milestone 2.5, part 2: related entities

After research that stored evidence, a `map` job (run as the Researcher,
with its own prompt) asks the model which specific organisations, people,
products, models, technologies and places the observation, its quote and
the new evidence mention. Each mention must be found as whole words in the
text it is attributed to; entities are matched to known ones by name or
alias, ignoring case, and linked by `mentions` relationships from the
observation or evidence. The Scout is shown the domain's most-mentioned
entities as starting points for exploring what is related to them, and
`collegium entities` lists them.

This is the organization's own reading of "discover related entities": it
builds a map of who and what its knowledge is about, rather than asking a
provider for similar pages. Entity resolution is by exact name or alias;
merging spelling variants the model does not reconcile is left for later.

## 2026-09-23 · Milestone 2.5, part 2: citation tracking

Every belief can now be traced end to end: hypothesis or observation →
evidence (the source's own words, stance, reliability, injection flags) →
source (title, URL, publication date) → the external call that found it
(which role, which provider, which query or approved feed, when).
`memory.citations` returns that chain, and `collegium why <id>` prints it
for hypotheses and observations, together with the observations a
hypothesis derives from, the hypotheses derived from an observation, and
the entities an observation mentions.

## 2026-09-23 · Backups

Memory cannot be deleted by the application, but a disk, a Docker volume or
a mistake at the server level can still lose it. A `backup` service in
Compose runs `pg_dump` (custom format) every `COLLEGIUM_BACKUP_INTERVAL_HOURS`
(24) into `COLLEGIUM_BACKUP_DIR`, together with the server's roles without
passwords (roles are server-wide, and a restore needs them; `db/logins.sql`
sets passwords). A dump is checked with `pg_restore --list` and only then
renamed into place, and only then are backups older than
`COLLEGIUM_BACKUP_KEEP_DAYS` (30) pruned, so a failing backup never removes
a good one. `db/restore.sh` restores into a new database, never over the
live one.

Tested by backing up a database with data, restoring it, and comparing
every table's row count, and by checking that the worker role and the
provenance triggers still hold in the restored copy.

The backup directory should live outside the repository, on storage that is
itself backed up (for example the home folder under Time Machine) or copied
off the machine.

## 2026-09-23 · First real run, and a cap on model replies

The organization's first run (domain `ai-agents`, Tavily, Hacker News and
one approved feed, Qwen2.5 14B) took 27 minutes: 2 grounded observations,
4 hypotheses under review with 2 to 3 independent supporting sites each,
13 pieces of evidence, 8 critiques, 10 entities, 35 external calls. One
review timed out after 10 minutes and succeeded on retry. The machine was
swapping heavily, which slows generation to a crawl.

Model replies are now capped at `COLLEGIUM_LLM_MAX_TOKENS` (2048). A reply
cut off at the cap is not fed back to the model; the original prompt is
sent again with a request for a shorter answer, and the call fails after
the usual attempts. A runaway generation now costs minutes, not an hour.

Seen in the run and left for Milestone 3, where the critique-resolution
loop has to judge them: counter-evidence about an older product version was
used against a claim about newer ones (the Researcher and Skeptic search
without a time window, and nothing checks relevance); the model's
reliability scores are uncalibrated (a forum complaint scored 1.00); and
some hypotheses are too broad to be informative.

## 2026-09-23 · Milestone 3 plan and the owner's decisions

Milestone 3 in four parts: (1) the critique-resolution loop, with a budget
for paid calls; (2) evidence quality: relevance of counter-evidence,
rule-based reliability ceilings per source type, more specific hypotheses;
(3) the Strategist: knowledge-gap detection, goals and research programs,
queueing work within the budget; (4) owner review of what the Strategist
proposes.

Decided by the owner:

- **Acceptance follows resolved critiques.** The Historian accepts a
  hypothesis at confidence ≥ 0.6 with ≥ 2 independent supporting sites and
  no open or upheld critique of severity 3 or more. The Skeptic's verdict
  counts only as a veto (reject), not as a required "accept".
- **Strategist autonomy.** It may create goals and queue investigations
  itself, within the budget. New research programs are proposed as
  decisions for the owner to approve or reject.
- **Budget.** At most 50 paid external calls in any 24 hours. Free sources
  (Hacker News, approved feeds) do not count.

## 2026-09-23 · Milestone 3, part 1: critique resolution and the budget

**The loop.** After a review, the Historian applies rules-3: accept at
confidence ≥ 0.6 with ≥ 2 independent supporting sites and no open or
upheld critique of severity ≥ 3; reject on the Skeptic's veto, confidence
≤ 0.25, or an upheld critique of severity 5; otherwise under review. If
serious critiques are still open, it queues a `resolve` job: the Researcher
searches for evidence that settles each open critique (C1, C2, ...),
whichever way it falls, and links it to the critique ("supports" backs the
objection, "contradicts" answers it). The Skeptic then re-reviews, sees each
critique with its evidence, and marks it upheld, addressed or dismissed
with a reason. In later rounds it may raise at most one new critique, and
the loop stops after two rounds, so a disputed hypothesis cannot absorb the
budget. An upheld serious critique blocks acceptance but is not
re-investigated. `collegium resolve --all` starts the loop for hypotheses
reviewed before it existed.

**The budget.** Providers declare whether they are paid (`metered`; Tavily
is, Hacker News and feeds are not). A paid call is refused once
`COLLEGIUM_DAILY_CALL_BUDGET` (50) calls were made in the last 24 hours. The
worker does not start a searching job with fewer than 5 paid calls left,
and a job stopped by the budget is deferred until the window frees, without
using one of its attempts.

## 2026-09-23 · Milestone 3, part 2: evidence quality

- **Reliability ceilings by source type** (`reliability.py`). The model
  still scores reliability, but evidence cannot score above its source's
  ceiling: social media and video 0.3, forums and community sites 0.4,
  press-release wires and blog platforms 0.5, everything else 1.0. Flagged
  documents stay capped at 0.3. The source type is kept in the source's
  metadata. Classification is by host; a claimant's own site is not yet
  recognised as such.
- **Relevance.** Both the Researcher and the Skeptic are told that evidence
  about another product, an older version or an earlier period neither
  supports nor contradicts a claim about the current one. This is a prompt
  rule; documents from basic search carry no dates, so it cannot yet be
  checked mechanically.
- **Specific hypotheses.** The Researcher must name who or what, direction
  or size, and period, and state what would show the hypothesis wrong. The
  falsification condition is stored with the rationale, where the Skeptic
  sees it.
- **Budget and plan.** The owner's Tavily plan is 1,000 credits a month;
  a daily budget of 50 calls would allow about 1,500. A daily budget of 30
  fits the plan. Compose now passes `COLLEGIUM_DAILY_CALL_BUDGET` and the
  other tuning settings to the worker, so the owner's env file controls
  them.

## 2026-09-23 · Milestone 3, part 3: the Strategist

The scheduler now queues a `strategize` job per active domain each day
(`COLLEGIUM_STRATEGY_INTERVAL_HOURS`) instead of a fixed scout. The
Strategist starts from deterministic gap checks (`strategy.py`): live
hypotheses short of independent support, serious critiques still open after
the loop's two rounds, entities mentioned at least twice that no hypothesis
or goal is about, and a domain not scouted for 20 hours. The model sees the
domain's hypotheses, entities, goals, programs, gaps and remaining budget
under labels, and returns an assessment, at most three goals with actions
(scout with a focus, corroborate, resolve) and at most one program
proposal. The code then decides:

- goals are created active (owner's decision), matched to existing goals
  by label or statement so they are continued rather than duplicated, and
  linked to what they investigate; goals whose hypotheses have all been
  decided are marked achieved;
- actions are queued most important goal first, each costed at about 8
  paid calls, only while they fit the remaining budget minus a reserve of 5;
  invalid targets are skipped; one scout a day is kept whenever the domain
  is due for one and the budget allows;
- a program is created only as `proposed`, with a decision that `concerns`
  it; the owner approves (program opens) or rejects (program closes, and a
  given reason is kept as an upheld critique of the proposal) with
  `collegium approve|reject`.

A `corroborate` job has the Researcher look for independent evidence either
way, excluding sites already cited, then sends the hypothesis for review.

## 2026-09-24 · Working hours

The owner wants to watch the organization work, so it works on its own only
within working hours (`COLLEGIUM_WORK_HOURS`, `COLLEGIUM_WORK_DAYS`,
`COLLEGIUM_TIMEZONE`; default Monday to Friday 08:00-16:00, Europe/Oslo;
`always` restores round-the-clock running). The worker starts jobs only
within them, and a job still running at closing time finishes. The
scheduler plans each domain once per working day, at the first check after
opening, replacing `COLLEGIUM_STRATEGY_INTERVAL_HOURS`. Work queued outside
hours, by the owner, the scheduler or the budget, waits for the next
opening. A manual `collegium worker --drain` ignores working hours.

## 2026-09-24 · Milestone 4 plan and the owner's decisions

Milestone 4 in four parts: (1) a read-only board: programs, goals,
hypotheses with their full `why` chain and confidence over time, recent
discoveries and operations; (2) the owner's actions: the decisions inbox,
challenging a hypothesis, managing domains and feeds; (3) Mission and
Contradictions; (4) Ask the Organization.

Decided by the owner:

- **One web service.** A single FastAPI app serves the server-rendered
  board (Jinja and htmx) and is the API. JSON endpoints are added only when
  something other than the board needs them. This merges VISION.md's API
  and Web UI components into one service for now.
- **No authentication yet.** It is added before the board is deployed where
  anyone else can reach it. Until then the web service listens on
  localhost only.
- **The mission is a decision.** The organization's mission and each
  domain's mission are approved decisions linked with `governs`; changing a
  mission supersedes the old decision, so its history is kept. The
  Strategist reads the domain's mission.
- **The owner's critiques go through the loop.** A critique written by the
  owner queues a `resolve` job and starts a fresh two rounds. The Skeptic
  may dismiss it, but must give a reason, and the board shows it
  prominently.
- **Ask the Organization** answers from memory only, with no external
  search, citing what it uses. `ask` jobs run whenever the owner asks,
  outside working hours too, since the owner is waiting and they make no
  paid calls. Questions and answers are stored, and a question memory could
  not answer becomes a knowledge gap the Strategist sees.
- **Contradictions** start with what is already stored: live hypotheses
  with both supporting and contradicting evidence, serious critiques upheld
  or still open after the loop, and hypotheses the Skeptic rejected at high
  confidence. Detecting contradictions between two hypotheses waits for
  Milestone 5.

## 2026-09-24 · Milestone 4, part 1: the read-only board

`collegium web` (and the `web` Compose service, published on
127.0.0.1 only) serves the board: overview, programs, goals, hypotheses,
each hypothesis's full explanation with a confidence chart, observations,
recent discoveries and operations (refreshed every 15 seconds with htmx).
The read queries moved from `cli.py` into `board.py`, so the CLI and the
board show the same things.

- **One connection per request**, opened with the board login. There is
  one user, so a pool is not worth it yet.
- **Outside text stays inert.** Excerpts, titles and statements come from
  the web and are escaped by Jinja. Source addresses are linked only if
  they are http or https. A strict content security policy lets the page
  load nothing but its own stylesheet and the vendored htmx, so even a
  missed escape could not run a script.
- **Charts are drawn on the server** as plain SVG, so no chart library is
  needed and the policy stays strict.
- **The web service holds no provider keys.** It shows the budget by
  counting calls to the paid providers (`metered_provider_names`), without
  building the providers.

## 2026-09-24 · Milestone 4, part 2: the owner's actions

The board can now do what the owner's CLI commands do: approve or reject
decisions (a rejection's reason is kept as an upheld critique), challenge
a hypothesis, add, pause and resume domains, choose their discovery
sources, approve, pause and retire feeds, and ask for a scout or planning
run now. The actions live in `owner.py`, used by both the CLI and the
board, so the rules are the same in both. `collegium challenge` and
`collegium domain pause|resume|retire` are new on the CLI.

- **The owner's challenge** is a critique by the owner actor. It queues a
  `resolve` job at round 1, so it gets a fresh two rounds whatever happened
  before. Both the Researcher and the Skeptic see "raised by the owner" on
  it, and the Skeptic's prompt says it may dismiss one only with a reason
  citing the evidence. The board lists the owner's challenges on the
  overview and the decisions page, with dismissed ones marked. A severity
  of 3 or more blocks acceptance while open, so challenging an accepted
  hypothesis sends it back under review until the challenge is settled.
  Changing the two prompts changes the Researcher's and Skeptic's
  `role_version`.
- **Forms without a login.** Any page the owner visits could post a form to
  localhost. Posts are accepted only when the browser marks them as coming
  from the board itself (`Sec-Fetch-Site: same-origin`, or a matching
  `Origin`), and the board answers only to host names in
  `COLLEGIUM_WEB_ALLOWED_HOSTS`, so a rebound DNS name cannot reach it.
- **Messages after an action** are chosen from a fixed list by name, never
  taken from the address, so a crafted link cannot put words on the board.
  Refused actions say why and change nothing, since they fail inside the
  owner's transaction.
