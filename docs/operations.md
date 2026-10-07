# Operations: the owner's running instance

How the live organization is run, checked and changed. For what the code
does, see CLAUDE.md; for why, docs/decisions.md.

## The machine

Since 2026-10-07 the organization runs on a dedicated hosting server on
the owner's own private (Tailscale) network, not the owner's laptop (see
`docs/decisions.md`). The laptop is kept as a cold fallback: fully
stopped, not decommissioned, holding its own copy of everything as of
the cutover. Where this section used to describe the laptop specifically
(Colima, the corporate TLS-inspecting proxy), that is now historical:
see "The laptop (cold fallback)" below.

- **Docker Engine and the Compose plugin run natively** (the host is
  Linux), not through Colima or Docker Desktop: there is no VM layer to
  size.
- **The model** runs on the host outside Docker, built from source:
  llama.cpp's `llama-server` at `http://localhost:8080/v1`
  (`host.docker.internal:8080` from the containers). It is a CPU-only
  build for now (a CUDA build is a planned follow-up on this host's GPU);
  it serves `Qwen/Qwen3-8B-GGUF:q4_K_M`, the same model and sampling
  settings as the laptop ran (see "Recommended startup settings" below):

  ```sh
  llama-server -hf Qwen/Qwen3-8B-GGUF:q4_K_M --offline -c 16384 -ctk q8_0 \
    -ctv q8_0 -fa on -t <this host's own core count> -to 3600 --jinja \
    -n 2048 -np 1 --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0 \
    --repeat-penalty 1.15 --repeat-last-n 256 --port 8080 \
    --host <see below>
  ```

  `COLLEGIUM_LLM_MODEL` in the env file must match the model actually
  running, since every run records it.
- **`--host` matters on native Linux Docker, where the laptop's Colima
  setup needed no such flag.** `host.docker.internal` (the `host-gateway`
  special value in compose.yaml's `extra_hosts`) always resolves to the
  *default* bridge's own gateway address, not to whichever network a
  container actually runs on, and never to `127.0.0.1`. Docker Desktop
  and Colima reach a loopback-bound host service through their own
  VM-boundary networking; native Linux Docker does not. Find the right
  address with
  `docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'`
  (typically `172.17.0.1`) and give it to `llama-server`'s `--host`.
  Binding to `0.0.0.0` instead would also answer on this host's LAN and
  any other network interface it has; binding to the default bridge's
  own gateway answers only Docker's own containers, confirmed by testing
  a container's reach against each address directly.
- **Recommended startup settings (2026-10-03) — always start the model
  this way, including for local testing, not just the live instance:**
  - `--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0`: Qwen3's own model card
    for thinking mode (the `--jinja` chat template's default). Its card
    warns that greedy (near-0 temperature) decoding "can lead to
    performance degradation and endless repetitions" — the client
    (`collegium.llm.OpenAICompatibleLLM`) also sends `temperature: 0.6`
    itself (`COLLEGIUM_LLM_TEMPERATURE`), so these server-side flags are a
    second line of defence for anything that calls the server directly.
  - `-c 16384`, not 8192: raised once the host turned out to have 32GB to
    spare. A multi-document Research/Review prompt (up to 3 documents) plus
    a reasoning model's own "thinking" tokens can otherwise get close to
    the old ceiling.
  - `-t`, `-np 1`: thread count is this host's own, not part of this
    round of tuning (see "The hold-back" below for the laptop's old
    value); `-np 1` is correct as long as only one worker process ever
    calls the server at a time.
  - Per-call tuning (temperature aside) lives in code, not here: see
    `collegium.roles.base.QUICK` and each role's `ctx.llm.generate(...,
    max_tokens=..., enable_thinking=...)` call. Mechanical, extractive
    calls (turning a question into search terms, the Moltbook verification
    solver) skip thinking mode and get a small reply budget; the calls
    that actually judge or compose (`StrategyPlan`, `ScoutReport`,
    `SkepticReview`) keep thinking on and get a larger one (3072, not the
    default 2048), since those are the ones a cut-off reply costs the most
    (a wasted call, then a retry).
- **The hold-back (2026-09-28, applied to the laptop only):** running
  everything on that laptop — Postgres, the board, the worker, SearXNG,
  the publisher and the model — forced a reboot under load, so Colima
  ran smaller (`colima start --cpu 3 --memory 6`, not the default 6/12),
  `-t 4` (not more) on `llama-server`, and
  `COLLEGIUM_WORKER_REST_SECONDS=120` in the env file paused the worker
  after each job so the model server was not driven back to back. See
  `docs/decisions.md`, 2026-09-28 and 2026-10-07. Now that nothing runs on
  the laptop, this host has no Colima to size and a thread count already
  set to its own core count (see "Recommended startup settings" above);
  whether to also raise or remove `COLLEGIUM_WORKER_REST_SECONDS` on this
  host is a deliberate decision not yet made, kept separate from the move
  itself (see `docs/decisions.md`, 2026-10-07).
- **No opening-hours restriction (2026-10-03, the owner's decision):**
  `COLLEGIUM_WORK_HOURS=always` in the env file, so the worker and
  scheduler now work around the clock rather than only Mon-Fri 08:00-16:00
  Europe/Oslo (`hours.py`'s own `"always"` value, no code change). The
  owner's questions, drafts and replies always ran at any hour regardless
  (`worker.ANY_HOUR`); this removes the restriction for everything else
  too. Reinstating it: set `COLLEGIUM_WORK_HOURS` back to `08:00-16:00`
  (and `COLLEGIUM_WORK_DAYS`, default `mon-fri`) and recreate the worker
  and scheduler.
- **Secrets** are in `~/.collegium/.env`: never read or print
  it; only pass it to commands. Every Compose command uses it:

  ```sh
  docker compose --env-file ~/.collegium/.env ps
  ```

  The shell is zsh, which does not split an unquoted `$E` into words: write
  the flag out in full (below, `$E` stands for it). **Never run
  `docker compose config`** with the env file, or grep its output: it
  expands every secret into the output.

  It holds, among others, the Tavily and Moltbook keys, the database
  passwords, the LLM settings and `COLLEGIUM_CONTACT`. If something fails
  because of a value in it, describe the symptom and ask the owner.

### The laptop (cold fallback)

Stopped, not decommissioned, holding its own copy of everything as of
the 2026-10-07 cutover. If it is ever brought back as the live instance:

- **Docker ran in Colima**, not Docker Desktop: `colima ssh`, `colima
  restart`, provisioning in `~/.colima/default/colima.yaml`.
- **The network re-signs HTTPS (a TLS-inspecting proxy).** The Colima VM
  trusted the proxy's root (a provision step), and the git-ignored
  `compose.override.yaml` mounted `~/.collegium/ca-bundle.pem` into the
  services (see `compose.override.example.yaml`). On the host, Python
  tools that fail with certificate errors need
  `SSL_CERT_FILE=$HOME/.collegium/ca-bundle.pem`.
- It is a Mac M1, 32GB unified RAM; `llama-server` ran natively on the
  host, not in Colima, with no `--host` flag needed (Colima's
  `host.docker.internal` reaches a loopback-bound host service through
  its own VM-boundary networking, unlike native Linux Docker — see "The
  machine" above).
- `host.docker.internal:8080` from the containers; `http://localhost:8080/v1`
  on the host itself. The hold-back values above (`-t 4`,
  `colima start --cpu 3 --memory 6`, `COLLEGIUM_WORKER_REST_SECONDS=120`)
  were this machine's own, not a property of Collegium in general.

## Addresses

| What | Where |
|---|---|
| The board | http://localhost:8000, or `COLLEGIUM_WEB_BIND_ADDR`'s address if set |
| SearXNG | http://localhost:8888 (`/search?q=...&format=json`) |
| Postgres | localhost:5432; as superuser: `docker compose $E exec -T db psql -U collegium` |
| The model | http://localhost:8080/v1 |
| Backups | `~/.collegium/backups` (`COLLEGIUM_BACKUP_DIR`), daily |

## Deploying a change

1. Tests and lint pass: `set -a; . ~/.collegium/.env; set +a; uv run pytest -q` (the tests need `POSTGRES_PASSWORD`), `uv run ruff check . && uv run ruff format .`
2. Commit (when the owner has asked for it).
3. **A migration only with the owner's explicit approval:**
   `docker compose $E run --rm migrate`. Then, if logins changed:
   `docker compose $E run --rm --no-deps logins`.
4. Build: `docker compose $E build worker web scheduler publisher`.
5. Recreate everything except the worker, **always with `--no-deps`**
   (otherwise Compose runs `migrate` as a dependency and applies any
   pending migration unasked):
   `docker compose $E up -d --no-deps web scheduler publisher`
6. Recreate the worker **on its own, in the background**: it finishes its
   current job first (up to `stop_grace_period: 20m`), and Compose waits
   for it, so combining it with the others keeps the board down meanwhile:
   `docker compose $E up -d --no-deps worker` (run in background).
7. SearXNG settings changes: `docker compose $E up -d --no-deps --force-recreate searxng`.

## Looking at the organization

```sh
docker compose $E exec -T web collegium jobs          # recent jobs
docker compose $E exec -T web collegium acquisitions  # external calls
docker compose $E logs --since 10m worker | grep -E "succeeded|failed|deferred"
docker compose $E logs publisher | tail               # what the publisher did
```

Useful queries (as superuser):

```sql
-- what is waiting, and why
SELECT kind, status, count(*), min(run_after), left(last_error, 70)
FROM jobs WHERE status IN ('pending', 'running') GROUP BY 1, 2, 5;

-- paid calls in the last 24 hours, by provider and role
SELECT a.provider, a.capability, ac.name, count(*) FILTER (WHERE a.error IS NULL)
FROM acquisitions a JOIN actors ac ON ac.id = a.requested_by
WHERE a.requested_at > now() - interval '24 hours' GROUP BY 1, 2, 3;
```

Owner commands inside the running system (they act as the owner through
the board login): `docker compose $E exec -T web collegium <command>`, e.g.
`domain sources <slug> searxng hackernews moltbook`.

## Changing operational state by hand

Jobs are not knowledge, but writes still need an actor. As superuser, in
one transaction, declare the system actor first:

```sql
BEGIN;
SELECT set_config('collegium.actor_id', '00000000-0000-0000-0000-000000000001', true);
UPDATE jobs SET run_after = now() WHERE status = 'pending' AND ...;
COMMIT;
```

Never write knowledge (memory tables) by hand; it goes through the roles
or the owner's actions, with provenance.

## Moltbook

- Account `drargus`, the community agent. Its key is
  `COLLEGIUM_MOLTBOOK_API_KEY` in the env file; send it only to
  `https://www.moltbook.com`. Only the `publisher` service gets it.
- Nothing posted there, or on the profile, says that the owner approved it
  (the owner's instruction), although every post is approved.
- Stop switch: `COLLEGIUM_MOLTBOOK_PUBLISHING=off` in the env file, then
  recreate `publisher` and `web`.
- Profile changes by hand: `PATCH /api/v1/agents/me` with the key from the
  env file, only when the owner asks.

## Pushing

See docs/reviews/README.md: nothing is pushed without a PASS security
review. The GitHub repository is https://github.com/ketilaa/collegium
(public).
