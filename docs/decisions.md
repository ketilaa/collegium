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

## 2026-09-24 · Milestone 4, part 3: mission and contradictions

**Missions.** The owner sets a mission for the organization and one for
each domain, on the board's Mission page or with `collegium mission`. As
decided, a mission is an approved decision and a new one supersedes the
old, so every earlier mission stays on record. One detail changed from the
plan: domains are not nodes, so a mission cannot be linked to its domain
with a `governs` relationship. Instead migration 0008 adds
`decisions.topic` ('mission'), and a domain's mission is tagged to the
domain through `node_domains`, as other records are. The Strategist sees
both missions in its brief, and its prompt asks it to choose goals that
serve them (a new `role_version`). Missions are the owner's own words, so
they are not fenced as outside text.

**Only the owner decides.** Agents could not update decisions, but could
insert one already approved. Migration 0008 adds a trigger: a decision
that is not `proposed` may only be written by the `owner` actor, which the
audit trigger already restricts to board logins.

**Contradictions** (board page and overview count) shows, from what is
already stored:

- live hypotheses with both supporting and contradicting evidence;
- critiques of severity 3 or more that are upheld, or still open after the
  loop's last round (the same test the Strategist's gap check uses);
- rejected hypotheses the Researcher had rated at 0.6 or more, with the
  Researcher's peak and the final confidence.

## 2026-09-24 · Norwegian sources, and free search tried

The owner wants Norwegian as well as international sources, preferably
free ones.

- **One working language.** Statements, summaries, rationales and
  critiques are written in English (a rule in `organization.md`, so every
  role's `role_version` changes); excerpts and quotes stay in the source's
  own language. Memory stays comparable (duplicate detection compares
  statement text) and every claim still traces to its original wording.
- **Grounding tolerates Norwegian.** `unsupported_terms` compares numbers
  by their digits, so "12,5" matches "12.5" and "1 200" matches "1,200".
  For a quote that is not English (a rough guess by common words and
  æ/ø/å), only acronyms and mixed-case names are checked, since an English
  statement capitalises words such as "Norwegian" that Norwegian does not.
  The model's check of each statement against its quote still applies.
- **Free search: not yet.** Google News RSS searches in Norwegian, but its
  links are Google redirects that end at an EU consent page, and storing
  them would make every source the same site, defeating the Historian's
  independent-sites rule. GDELT's free API refused requests from this
  network even at one per 7 seconds, rejects words shorter than three
  letters ("AI", "KI"), and found nothing for a plain English query. Neither
  is built. Norwegian material comes from approved feeds for now (digi.no
  and NRK Teknologi; kode24, Computerworld Norge and Shifter publish none),
  and from Tavily at its usual cost. A self-hosted SearXNG would be the
  free option, but it adds a service, which needs the owner's decision.
- **Feeds approved.** developers-ai: digi.no, NRK Teknologi, Indeed Hiring
  Lab, GitHub Blog, The Pragmatic Engineer, Stack Overflow Blog, Martin
  Fowler, Addy Osmani. ai-agents: METR (Simon Willison was already there).

## 2026-09-24 · The Strategist abandons goals

With the missions set, the Strategist still kept two goals that did not
serve them: nothing let it drop a goal, and its prompt says to continue
existing goals rather than create similar ones. Its plan now has an
`abandon` list (G label and reason). The code abandons only active goals
of the domain being planned, never one the same plan also continues (when
the model contradicts itself, the goal stays), and ignores unknown labels.

Migration 0009 adds `goals.outcome`: the reason a goal was abandoned, or
that everything it investigated was decided. The status change itself was
already audited; the outcome keeps the reason with the goal. The next plan
is shown the five most recently abandoned goals with their reasons, so it
does not set them again. The board's Goals page and `collegium goals --all`
show the outcome.

## 2026-09-24 · Full text from feeds

Seven of the ten approved feeds carry the whole article (RSS
`content:encoded`, Atom `content`), but the parser read only the summary,
so their leads were thin and each feed cost a paid Tavily extraction per
scout. Leads now open with the article text when the feed has it, cut to
the same 600 characters an enriched lead gets, so prompts keep their size.
Measured on the approved feeds: feeds needing a paid call per scout fell
from 10 to 4 (digi.no, NRK, Stack Overflow Blog, Simon Willison).

## 2026-09-24 · Free first: our own search and page reading

Tavily was the bottleneck: every search and page read went through it,
about 4 paid calls per research, review or resolve job, so an observation
taken through the whole critique loop cost about 24 of the ~30 calls a day
the owner's plan allows. The owner decided to make the free route the main
one and keep Tavily as the fallback.

- **Reading pages** (`acquisition/web.py`): fetched directly and reduced to
  the article text with trafilatura (Apache-2.0). Pages it cannot read
  (errors, PDFs, cookie walls, JavaScript-built pages, under 400 characters
  of text) go to Tavily. Tested on real pages: 8 of 9 read free, including
  kode24, SINTEF and CNBC. Fetching ourselves means outside addresses are
  fetched from inside the organization's network, so only http(s) on ports
  80/443 to hosts that resolve to public addresses are fetched, checked
  again at every redirect (a DNS answer changing between check and fetch
  is not guarded against); robots.txt is respected; pages over 3 MB are
  refused.
- **Searching** (`acquisition/searxng.py` and the `searxng` Compose
  service, owner-approved as a sixth service): the organization's own
  SearXNG metasearch, free and without quota, published on localhost only.
  It is the default search when `COLLEGIUM_SEARXNG_URL` is set; Tavily
  answers when it fails or finds nothing. Tested: 10 results per query in
  about a second from Brave, Google and DuckDuckGo, including Norwegian
  sources (kode24, SINTEF, digi.no). The engines it asks can throttle it;
  DuckDuckGo showed a CAPTCHA on the first query.
- **Fewer searches in the critique loop.** The Skeptic does not search in
  a review that follows a resolve (the Researcher has just gathered the
  evidence on those critiques), and a resolve uses at most 2 queries.
- **The budget follows the route.** Jobs wait for budget only when their
  main route is paid; otherwise a spent budget just skips the paid
  fallbacks. The Strategist plans within the paid budget when search is
  paid, and up to 6 actions a plan when it is free, since the local model's
  time is then the limit.

## 2026-09-24 · Lenient feeds, and wiki.totto.org

Feeds written by templates sometimes contain a bare `&` (wiki.totto.org's
MkDocs feed has the category "AI Agents & the Agentic Web"), which made the
whole feed unreadable. Bare ampersands are now escaped before parsing, as
browsers and feed readers do; defusedxml still refuses entity declarations.

The owner approved a practitioner's site for developers-ai, about
experienced developers working with AI. Its
robots.txt declares `ai-input=yes, ai-train=no`. Its author
sells the method the site writes about, so its claims about it (such as
"25-66x productivity gains") are claims to be challenged, and reliability
ceilings do not yet recognise a claimant's own site.

## 2026-09-25 · Milestone 4, part 4: Ask the Organization

As decided: answers come from memory only, cite what they use, run at any
hour, and questions memory cannot answer become gaps for the Strategist.

- **A question is a node** (migration 0010: node kind `question`, table
  `questions`), asked by the owner through the board or `collegium ask`.
  Its answer's sources are `cites` relationships whose rationale is the
  source number ([1], [2], ...) used in the answer text.
- **The Researcher answers** (`roles/answerer.py`, job `ask`, no searching).
  The Historian stays deterministic. The model turns the question into
  search terms (English, plus the question's own key words, since excerpts
  keep their source's language); Postgres full-text search finds hypotheses,
  observations, evidence and entities (English configuration for
  statements and summaries, simple for excerpts and names; a term matches
  when all its words occur, in any order); the model answers.
- **Citations are structural.** Tried against the real model and a copy of
  live memory: with a free-text answer and a separate citation list, the
  14B model wrote good answers and cited nothing. The answer is now a list
  of points, each requiring at least one source label; points citing no
  record shown are dropped, and an answer with no points left is recorded
  as unanswered ("Memory holds nothing on this yet."). The answer is
  written in the question's language (named in the request, since the
  model otherwise answered Norwegian questions in English), and each point
  says how firmly it is held: accepted hypotheses as conclusions, others as
  "we are investigating whether ...". The model still sometimes overstates
  weak evidence; the answer page shows each source's kind and status.
- **Any hour.** The worker takes `ask` jobs outside working hours
  (`jobs.claim` with kinds) and queues them at priority 1.
- **Gaps.** Unanswered questions from the last 14 days, about the domain
  or about no domain, are gaps the Strategist sees ("unanswered question").

## 2026-09-25 · Firmness of answer points is decided by code

The 14B model sometimes wrote "we have concluded that ..." about a point
resting only on weak evidence. Each point's firmness is now set by code
from its sources, not by the model's wording: *concluded* if an accepted
hypothesis supports it, *investigating* (with the highest confidence) if a
proposed or under-review one does, *judged false* if it rests on rejected
hypotheses, otherwise *reported* (observations, evidence, entities).
Migration 0011 stores the points (`questions.points`), and the board and
`collegium questions` show each point with its firmness.

A point worded more firmly than its firmness allows is corrected when it
opens with a known claim ("We have concluded that", "We know that", "Vi har
konkludert med at", ...), becoming "We are investigating whether" / "A
source reports that" (or the Norwegian equivalents), and is otherwise
flagged on the board as worded too firmly. The answer language is now named
("Norwegian" or "English"): asked for "the language of the question", the
model answered a Norwegian question in English.

## 2026-09-25 · Working behind a TLS-inspecting proxy

The owner's network re-signs all HTTPS with the proxy's own root
certificate. The Mac trusts it; nothing else did. Image pulls failed in the Colima VM, and
from inside the containers every outside call failed
(`CERTIFICATE_VERIFY_FAILED`): Tavily, page reading, Hacker News, feeds and
SearXNG's engines. Both of that morning's scouts and two resolves used up
their attempts; they were re-queued with their original payloads.

- **Colima VM:** a provisioning step in the owner's Colima configuration
  installs the proxy's root (idempotent; restarts Docker only on change).
- **Containers:** a machine-specific `compose.override.yaml` (git-ignored;
  `compose.override.example.yaml` is the template) mounts a bundle of the
  standard certificates plus the proxy's root, kept outside the repository
  in `~/.collegium/ca-bundle.pem`, and points `SSL_CERT_FILE`,
  `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` at it for the worker, scheduler,
  web and SearXNG. Nothing company-specific goes into the image or the
  repository, and without a proxy the override is simply not used.

## 2026-09-25 · Scout yield and wasted paid calls

The first developers-ai scout read 47 feed items and 30 search results but
recorded one observation (from a tweet, sent for research), and paid for
four page reads. Four fixes:

- **The budget counts only successful paid calls.** 18 Tavily calls that
  failed on the proxy's certificate, never reaching Tavily, had counted
  against the day's budget.
- **Each page is read at most once per run** (a cache in the per-run
  `Acquisition`, including pages found unreadable): Hacker News returned the
  same tweet for three queries, and it was paid for three times. **Social
  and video pages never go to paid reading** (`NOT_WORTH_PAYING` in
  `reliability.py`): they need a browser, and evidence from them is capped
  at 0.3.
- **The Scout reads leads in batches of 15**, each allowed up to 5
  observations. With one report of at most 5 for ~77 leads (~45,000
  characters), the cap limited yield and the long prompt hurt the 14B
  model's quoting. The run notes now keep the rejected statements with
  their reason (for example `[unsupported 70] ...`), not only counts.
- **Leads are labelled with their kind of source** when weaker than
  ordinary reporting (social media or video, forum, press release, blog
  platform), and observations from social media or video are recorded but
  never sent for research: a weak signal, not a basis for an investigation.

Applied retroactively: the one affected observation (the tweet about a
Sanders AI bill) had already been researched; its hypothesis goes through
the Skeptic's review like any other and cannot be accepted on a single
site.

## 2026-09-25 · The worker stops gracefully

Recreating the worker for a rebuild cut off a 13-minute scout and two other
jobs: each lost its work and an attempt. The worker runs as PID 1, which
ignores SIGTERM without a handler, so Docker waited 10 seconds and killed
it. Now SIGTERM or SIGINT makes the worker take no new job, finish the one
it is running, and exit; Compose gives it `stop_grace_period: 20m`, longer
than any job. A job that still runs past that is taken over when stale, as
before.

## 2026-09-25 · Sources, source discovery, and a community agent on Moltbook

The owner's view: the organization is only as good as its sources. Decided:

- **More checked feeds**, several from an earlier project's checked
  list: kode24, ITavisen, Bekk fag, arXiv cs.SE (also for ai-agents), and
  Hacker News searches as feeds via hnrss.org ("junior developer", "AI
  coding"). Bouvet (timeout), Epoch AI and Stanford HAI (no feed found) and
  SSB (empty feed) did not work.
- **The organization proposes sources; the owner approves.** Candidates are
  chosen by code from what already proved useful; the feed is found and
  checked automatically; the Strategist proposes it as a decision. Nothing
  is added without the owner. (To be built.)
- **A community agent on Moltbook**, an exception to "write only to
  organizational memory", recorded in VISION.md. It uses the Moltbook
  account "drargus", registered for an earlier, retired project (so no
  confusion about who speaks). Stages, each a deliberate change: (1) read-only, as a
  low-trust source with the social-media ceiling, never sent for research on
  its own; (2) posts drafted from memory, approved one by one by the owner
  on the board, published by a separate component that alone holds the API
  key (`COLLEGIUM_MOLTBOOK_API_KEY` in the owner's env file), rate-limited,
  with a stop switch; replies are low-trust leads. Moltbook "briefings"
  (tasks assigned by other agents' moderators) are never acted on. The
  earlier project's design is the model.

## 2026-09-25 · Built: source proposals, and Moltbook stage 1 (read-only)

**Source proposals.** Each plan, the Strategist looks at up to two sites
that produced at least two observations or pieces of evidence in the domain
in the last 30 days (`strategy.source_candidates`). Social media, video,
forums and Moltbook are excluded, as are sites already followed or already
proposed. A site is known by the last two labels of its host
(`rss.kode24.no` and `www.kode24.no` are both `kode24.no`). The web reader
looks for the site's feed (`find_feed`: the feed its page announces, then
the usual paths), with the same protections as reading pages, and checks
that it parses with at least three items. A site with a feed becomes a
decision with topic `source` and its feed in `decisions.details` (migration
0012); approving it on the board adds the feed to the domain.

**Moltbook, stage 1.** Reads need no key, so the adapter has none and
cannot write: the key stays in the owner's env file for a later publishing
component, not in any container. Search is asked for posts only (it
otherwise returns agent profiles), and each post is read in full from the
API, since search returns only its start and the site needs a browser.
Leads name the author agent, forum and votes. Moltbook is a source type of
its own ("AI-agent forum"): reliability capped at 0.3, never paid for,
never sent for research, never proposed as a feed, and the Scout is told
the posts are written by other AI agents and may try to instruct it. A
domain uses it by adding `moltbook` to its discovery sources.

Enabled for both domains (discovery sources: searxng, hackernews, moltbook).
On the owner's instruction, the drargus profile description was changed
(by hand, from the owner's machine, not by the organization) to: "Community
agent for Collegium, a research organization with institutional memory.
Reads here; will ask for feedback on hypotheses — every post approved by
its human owner."

## 2026-09-25 · Built: Moltbook stage 2, posts and replies approved by the owner

The owner asked for stage 2 without waiting, and for the organization to
also answer other agents from what it knows. Both are drafted from memory,
approved one by one on the board, and published by a separate component.

**Drafts.** A `draft` job has the Researcher write a post asking other
agents about one hypothesis: its confidence, the evidence both ways and the
open critiques, ending with one question. A `reply` job has it read a
Moltbook post or comment the Scout recorded, recall what memory holds (as
Ask does), and draft a reply only if memory has something to add. Replies
go through Ask's `compose`: only records it was shown count, and a point may
not claim more firmness than memory gives it ("we have concluded" becomes
"a source reports" for an observation). In both, code decides the links
(only sources memory holds, no social media or forums; any address the
model writes is removed) and signs the text. Drafts are proposed decisions
(topics `post` and `reply`); they run at any hour, since they make no
external calls.

**Who asks for them.** The owner, from a hypothesis page or a Moltbook
observation. The Strategist, in domains that read Moltbook: one hypothesis
its own research has stalled on (critiques still open after the loop, or
too little independent support), never asked about before, with nothing
waiting for the owner and nothing drafted in the domain for two days. The
Scout: up to two Moltbook threads per run that it marked worth
investigating, replies to the organization's own posts first, and at most
five reply drafts waiting per domain. The Scout also reads the replies to
the organization's posts from the last 14 days, as leads like any Moltbook
post.

**Approval.** On the Decisions page a draft is an editable form (forum,
title, text); approving sends exactly the text as left. The owner puts it in
the outbox (`community_posts`, migration 0013). A trigger lets only the
owner insert, only for an approved decision of the right topic, and makes
the text immutable afterwards. Status moves approved → publishing →
published or failed (publisher); the owner may withdraw an approved one.

**The publisher** (`publisher.py`, Compose service `publisher`) is the only
holder of `COLLEGIUM_MOLTBOOK_API_KEY`. It connects as
`collegium_publisher_app`, whose role can read the outbox and record
outcomes, nothing else: no memory, no jobs. It sends the key only to
https://www.moltbook.com and follows no redirects. Pace: one post an hour
and one comment every ten minutes (the forum allows one per 30 minutes and
one per 20 seconds). It marks a post publishing before sending, and retries
only a 429: after a server error the post may exist, so it is marked failed
rather than risk posting twice. Stop switch: `COLLEGIUM_MOLTBOOK_PUBLISHING`
(on by default in Compose; `off` stops all posting). The forum's anti-spam
verification (an obfuscated arithmetic problem, five minutes to answer) is
solved by the local model reading only the two numbers and the operation,
code computing the answer; it solved the forum's examples. After three
failed verifications in a row the publisher halts (the forum suspends
accounts after ten).

**Not said on the forum.** On the owner's instruction, nothing published
says that the owner approved it: not the profile description (changed back
to "Community agent for Collegium, a research organization with
institutional memory. Reads here; will ask for feedback on hypotheses."),
not the post signature, not replies. Everything posted is approved; there
is no need to say so.

## 2026-09-25 · Free search blocked: wait, don't pay

The paid budget (30 a day) was spent by 09:44 although search and reading
are free first. Two causes. First, every engine behind SearXNG blocked the
organization: by default only Brave, DuckDuckGo and Google CSE answer web
queries, and about 85 searches in a day from one address got all three
suspended ("too many requests", CAPTCHA). SearXNG then returned nothing,
which the acquisition layer took as "nothing found" and handed to Tavily.
Second, Reddit (a "forum") and LinkedIn (then a "blog platform") block the
free reader and were not excluded from paid reading, so every page from
them was read by Tavily: about ten reads for sources capped at 0.4-0.5.

Decided with the owner: an outage of free search never spends the paid
budget.

- `SearXNGDiscovery` raises `SearchUnavailable` when there are no results
  and engines refused to answer. The acquisition layer does not fall back
  on it; a scout with other sources goes on without it; any other job is
  deferred 30 minutes without using an attempt. A search that really finds
  nothing still falls back to Tavily, as before.
- SearXNG asks more engines (Bing, Mojeek, Qwant, Yahoo enabled; Startpage
  left off, it asks for proof-of-work), leaves a blocked engine alone for
  30-60 minutes, and the adapter leaves 3 seconds between queries.
- Forums are not worth paying for (`NOT_WORTH_PAYING` now includes
  "forum"), but observations from them are still researched
  (`NOT_WORTH_RESEARCH` is the old list). LinkedIn counts as social media
  (ceiling 0.3).
- With free search first, a spent budget only skips the paid fallback
  search; it no longer defers the job to the next day. The 19 reviews and
  resolutions deferred that way were released by hand.

## 2026-09-25 · One User-Agent, with a contact

Every request the organization makes itself now says who it is, in one
place (`identity.py`): `Collegium/0.1 (+https://github.com/ketilaa/collegium;
research agent)`, and `community agent` for the Moltbook publisher. Before,
the web reader, feed reader and Moltbook reader each spelled out their own
string without a contact, and the Hacker News and Tavily adapters sent
httpx's default. The contact is the public GitHub repository, created for
this on the owner's instruction (no code pushed yet); `COLLEGIUM_CONTACT`
overrides it. Requests SearXNG makes to search engines on the
organization's behalf keep SearXNG's own headers.

Refined the same day: the repository is public, so anyone running a copy
would have named the owner as its contact. The link now only names the
software; who runs a copy is added as "operated by ..." from
`COLLEGIUM_CONTACT`, and nothing is claimed when it is unset (the worker
and publisher warn). The owner's copy uses https://github.com/ketilaa.

## 2026-09-25 · A security review before every push

Decided by the owner, before anything is pushed to the public repository:
a `security-reviewer` subagent must review the commits not yet pushed and
report PASS. It only reads (Read, Grep, Glob); a script gathers what needs
commands or the network into a bundle (diff, secret scan of every commit
in the range, `pip-audit`, the direct dependencies on PyPI, container
images). Reports are kept in `docs/reviews/`, findings numbered `SEC-n-i`
(n across all reviews, i the iteration of the push loop), each with a
severity and a disposition; see `docs/reviews/README.md`. A versioned
pre-push hook checks that a committed PASS report covers what is pushed,
and the Claude Code settings deny bypassing it. Categories added to the
owner's list (SQL injection, prompt injection, fabricated and vulnerable
dependencies): secrets in history, privacy of what becomes public,
excessive agency, SSRF and egress, the board's web security, container
images, insecure defaults and resource abuse.

## 2026-09-25 · Apache License 2.0

Decided by the owner for the first push. Permissive, so the software can
be reused and run by anyone, with an explicit patent grant; its trademark
clause grants no right to the name "Collegium", and its disclaimer fits
the README's point that the authors do not run or answer for other
people's copies. Every dependency allows it: all are permissive except
psycopg (LGPL-3.0, used unmodified as a library); htmx is 0BSD; SearXNG
(AGPL-3.0) runs as its own container and is not part of this code.
Considered: AGPL-3.0 (would oblige anyone hosting a modified copy to
publish it, but puts off reuse) and MIT (no patent grant).

## 2026-09-25 · Before the first push: review 68e0860-001

The first review (`docs/reviews/2026-09-25-68e0860-001.md`) failed. The
owner decided:

- **The vim swap file is removed from the whole history** (`git
  filter-repo`), not just from the tree: it held the user name, the host
  name and the file's path. `*.swp` is ignored.
- **The database listens on 127.0.0.1 only, and its passwords are
  required.** Compose refuses to start without `POSTGRES_PASSWORD` and the
  three login passwords; `.env.example` no longer carries known values. The
  tests take the superuser password from `POSTGRES_PASSWORD` when set.
- **Privacy:** the docs speak of a TLS-inspecting proxy rather than the
  owner's network and its vendor (generic setup advice still names
  Zscaler as an example), use `~` for the owner's home, and no longer name
  the retired earlier project or the author of an approved site. The
  author email is the owner's personal address, by choice.
- **Low findings (SEC-8 to SEC-12) follow the push** as proposals.
- **The old wording is rewritten in the whole history too** (review
  002 passed, but earlier commits and the 001 report still carried it):
  one `git filter-repo --replace-text` pass before the push.

## 2026-09-25 · Backups: zstd, and a failed backup is a failure

A backup at 06:00Z left a 0-byte dump that looked finished: the loop calls
`backup_once || echo ...`, and in that position the shell ignores `set -e`
inside the function, so after a failed `pg_dump` the script still renamed
the empty file and pruned older backups. Every step now checks its own
result (`|| return 1`). Dumps are compressed inside the custom format with
`--compress=zstd:19` (on the live database 526 KB with the default gzip,
430 KB with zstd; a tar.gz of the default dump was 415 KB but would need
unpacking before `pg_restore`, so the owner chose zstd). `restore.sh` is
unchanged; `pg_restore` reads either. On the live instance backups go to
`~/.collegium/backups` (`COLLEGIUM_BACKUP_DIR`), outside the repository.
One-off `run` commands for `backup` and `logins` need `--no-deps`, or they
run `migrate` first.

## 2026-09-25 · Security follow-ups: feeds fetched safely, no root, pinned images

The owner chose three of the low findings from the first push's reviews:

- **Feeds are fetched like pages (SEC-9).** The protected fetch (only
  http(s) on the usual ports, only public addresses, checked at every
  redirect, robots.txt obeyed, a size cap) moved from `web.py` into
  `acquisition/fetching.py` (`SafeFetcher`), used by the web reader and the
  feed reader alike. An approved feed that later redirects into the
  organization is refused. Feeds obey robots.txt too, as the README
  promises site owners; all 22 approved feeds were still readable. Feeds
  get their own cap, 20 MB (METR's feed carries whole articles, about
  9 MB), and any content type, since feeds are often served as text/html.
- **The services run as a system user without a shell (SEC-11)**, uid
  10001; the code and environment in `/app` stay owned by root.
- **Every image is pinned by digest (SEC-10)**, as `tag@sha256:...`, the
  tag kept for readers: the digests of the images that were running and
  tested. SearXNG moves from `latest` to its version tag. To update an
  image: pull the new tag, test it, replace tag and digest.

Still deferred: SEC-8 (DNS rebinding), SEC-12, SEC-14 and SEC-16.

## 2026-09-25 · A deadline for every fetch, and large feeds can be found

Review 1e4ac72-001 (SEC-17): the body was built with `bytes +=`, copying
everything read so far on each chunk, which at the 20 MB feed cap means
gigabytes of copying; and httpx's timeout is per read, so a site sending
a byte now and then could hold a job (or a board request approving a
feed) indefinitely. The body is now a `bytearray`, and every fetch has a
deadline for the whole of it, redirects included: 60 seconds for pages,
120 for feeds. Feed discovery (`find_feed`) uses the feed cap and
deadline too, so a feed with whole articles, like METR's, can be found
for source proposals.

## 2026-09-25 · The fetch deadline covers the whole fetch

Correcting the previous entry: its deadline was checked only between
chunks of the body, so robots.txt (read with no cap), the name lookups,
the handshake and the headers could still trickle past it (review
51408ea-001: SEC-18, SEC-19), and `find_feed` tried every feed a page
announced (SEC-20). Now:

- **A fetch runs under `asyncio.timeout`** (httpx's async client inside a
  blocking `SafeFetcher.fetch`), which cancels wherever the fetch waits:
  robots.txt, every redirect, handshake, headers, body. A thread-side
  timer closing the socket was considered, but closing a socket from
  another thread does not reliably wake a blocked read.
- **Name lookups** run in a small thread pool of their own, awaited under
  the same deadline (a stuck lookup is left to finish in its thread).
  This also closes the gap left for SEC-8.
- **robots.txt is read no further than 500 KB** (Google's limit); the
  rules read so far still apply.
- **`find_feed` tries at most 3 announced feeds**, then the usual places,
  all within one feed deadline (120 s) together.

## 2026-09-25 · Feed links found by a scan; a correction about SEC-8

Review 9e4d2af-001:

- **Correction:** the previous entry says the lookup thread pool "also
  closes the gap left for SEC-8". It does not. httpx looks the host up
  again when it connects (in the event loop's default executor, which
  `asyncio.run` waits for after the deadline, up to the resolver's own
  timeout; SEC-21), and that second, independent lookup is exactly what
  DNS rebinding (SEC-8) uses. Both stay open, to be fixed together by
  connecting to the address that was checked (a transport given the
  resolved IP, keeping SNI and the Host header).
- **Feed links on a page are found by a linear scan (SEC-22)**, not one
  regex over the page: on a page whose tags never close, the regex took
  35 s for 66 KB and would take many minutes for the 200 KB read, in the
  worker, after any deadline; `html.parser` was as slow (120 s on bare
  `<link ` fragments). Each `<link` tag ends at its `>`, the next `<` or
  2,000 characters, so tags never overlap; the attribute regexes run on
  the tag alone. Same results on real pages.
- **SEC-23 stays open:** a robots.txt that redirects, errors or times out
  is cached as "allow everything" until the process restarts.

The first version of that scan (review d87859f-001, SEC-24, HIGH) searched
a lowercased copy of the page and cut the page at the same positions; but
`str.lower()` can lengthen text ("İ" becomes two characters), so on a page
with enough "İ" before a late `<link` the loop never ended and grew memory
in the worker. It was deployed for about an hour before the review caught
it. Tags are now found with a case-insensitive literal (`<link`, which
cannot backtrack) on the page itself, and ended in the page itself; tests
cover the dotted capital I.
