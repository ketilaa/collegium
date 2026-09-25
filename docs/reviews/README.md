# Security reviews

Nothing is pushed unless the `security-reviewer` subagent
(`.claude/agents/security-reviewer.md`) has reviewed what is being pushed
and reported PASS. The reports are kept here, one per review, and are
pushed with the code they cleared.

## The loop

1. `scripts/security-review-bundle.sh` gathers the commits not yet on the
   remote (all of history before the first push) into `.review/<hash>/`:
   the diff, a secret scan of every commit in the range, `pip-audit` of the
   locked dependencies, the direct dependencies as PyPI knows them, and the
   container images. The reviewer can only read, so everything that needs
   a command or the network happens here.
2. The `security-reviewer` subagent reads the bundle and the code and
   returns its report. It saves nothing itself.
3. The report is saved here as `YYYY-MM-DD-<short hash>-<iteration>.md`
   (the name is in the bundle's `meta.txt`) and committed.
4. On FAIL: fix, commit, and go back to 1. The next report is the next
   iteration (002, 003, ...) of the same push, and says what became of each
   earlier finding.
5. On PASS: push. The pre-push hook (`.githooks/pre-push`) checks that a
   committed PASS report covers the pushed commit: the reviewed commit is
   an ancestor, only reports were committed since, and the review reaches
   back to what the remote already has. Enable it in each clone with
   `git config core.hooksPath .githooks`. Never push with `--no-verify`.

## Findings

Each finding is `SEC-n-i`:

- `n` numbers findings across all reviews ever, starting from 1. A new
  finding takes the next number; a number is never reused.
- `i` is the iteration of the current push loop, three digits: 001 for the
  first review of a push, 002 after the first round of fixes, and so on. A
  finding that is still open in a later iteration keeps its `n`
  (SEC-4-001, then SEC-4-002).

Severity: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFO`.

Disposition:

| Disposition | Meaning | Default for |
|---|---|---|
| BLOCKING | Fix before this push. | CRITICAL, HIGH |
| ACTION_REQUIRED | Fix; the push may go ahead only if the owner explicitly defers it. | MEDIUM |
| RECOMMENDATION | Worth doing; does not hold up the push. | LOW |
| INFORMATIONAL | For the record. | INFO |

A review passes when nothing is BLOCKING and every ACTION_REQUIRED finding
is fixed or deferred by the owner.

## What is reviewed

Secrets (in any commit, even if later removed), privacy (personal data
made public, including commit metadata), injection (SQL, shell, paths,
templates, deserialization), prompt injection, excessive agency (roles
acting beyond organizational memory or without the owner), SSRF and data
leaving the organization, the board (XSS, CSRF, CSP, hosts), the supply
chain (fabricated or typo-squatted packages, known vulnerabilities,
unpinned dependencies and images), insecure configuration, and resource
abuse (unbounded work, the paid budget). The categories are defined in the
subagent's instructions.
