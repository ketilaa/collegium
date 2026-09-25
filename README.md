# Collegium

A persistent research organization of AI agents with institutional memory.
Its roles (Scout, Researcher, Skeptic, Strategist and Historian) run on one
local language model, read the web, and build structured, explainable
knowledge in PostgreSQL: observations, evidence, hypotheses, and how
confidence in them changes over time.

It behaves like a research institute, not a chatbot: it exists between
conversations, and it becomes more valuable every month because it
accumulates observations, evidence, hypotheses, decisions and experience.
A human owner steers it as board chair, setting direction; the
organization decides how the research is done.

See [VISION.md](VISION.md).

## If Collegium visited your site

Collegium identifies itself in the User-Agent of every request it makes:

```
Collegium/0.1 (+https://github.com/ketilaa/collegium; research agent; operated by <contact>)
```

- **This repository is the software, not the operator.** Anyone can run their
  own copy of Collegium, and the authors do not run, control or see other
  people's copies.
- **"operated by"** names whoever runs the copy that visited you. Contact
  them about its behaviour. A copy without that part has not told us who
  runs it.
- **robots.txt is respected.** To keep Collegium out of all or part of your
  site:

  ```
  User-agent: Collegium
  Disallow: /
  ```

- **It reads; it does not write.** It fetches pages and feeds it has found
  through search, at a modest pace, and never posts, submits forms or signs
  up for anything. The one exception is the forum Moltbook, where a copy's
  community agent may post and reply, each post approved by its operator.

## Status

Running daily on a laptop with a local 14B model. Done: institutional
memory, the Scout → Researcher → Skeptic → Historian workflow with critique
resolution, the Strategist, a board for the owner (missions, goals,
hypotheses, contradictions, discoveries, decisions, Ask the Organization),
free-first search and reading, and a community agent on Moltbook whose
every post the owner approves.

## Getting started

Requires Docker, [uv](https://docs.astral.sh/uv/) and a local model server
with an OpenAI-compatible API (e.g. [Ollama](https://ollama.com) or
llama.cpp's `llama-server` with a 14B model). Search runs on the bundled
SearXNG; a [Tavily](https://tavily.com) API key is an optional paid fallback.

```sh
cp .env.example .env                # set COLLEGIUM_CONTACT; optionally TAVILY_API_KEY
docker compose up -d                # database, migrations, worker, scheduler, board, search
set -a; . ./.env; set +a            # for the CLI on the host

uv run collegium domain add ai-agents "AI and agents"
uv run collegium scout ai-agents    # or wait for the scheduler
uv run collegium jobs               # follow the work
uv run collegium hypotheses         # what the organization believes
uv run collegium why <id>           # and why
```

Set `COLLEGIUM_CONTACT` to a URL or `mailto:` address of yours, so that
sites can tell who is reading them (see [If Collegium visited your
site](#if-collegium-visited-your-site)). Collegium warns at startup when it
is not set.

The board is at <http://localhost:8000> once `docker compose up -d` is
running (or `uv run collegium web` on the host). It has no login yet and
listens on localhost only.

Run the tests with `uv run pytest` (needs `docker compose up -d db`).

## Behind a TLS-inspecting proxy (Zscaler and the like)

If outside calls fail with `CERTIFICATE_VERIFY_FAILED`, the network is
re-signing HTTPS with its own root certificate. Copy
`compose.override.example.yaml` to `compose.override.yaml` and build the
certificate bundle it describes; Compose then mounts it into the containers.
With Colima, image pulls also need the root in the VM (a `provision` step
in `~/.colima/default/colima.yaml`).

## Backups

The `backup` service dumps the database every 24 hours into
`COLLEGIUM_BACKUP_DIR` (default `./backups`), checks each dump is readable,
and prunes backups older than 30 days. Keep the directory somewhere that is
itself backed up, outside the repository.

```sh
docker compose up -d backup                          # scheduled backups
docker compose run --rm backup sh /db/backup.sh      # one now
docker compose run --rm backup sh /db/restore.sh /backups/collegium-<stamp>.dump
                                                     # restore into collegium_restored
```

A restore never overwrites the live database; check the restored copy, then
switch by renaming databases.

Technical decisions and their reasons are in
[docs/decisions.md](docs/decisions.md).

## License

[Apache License 2.0](LICENSE).
