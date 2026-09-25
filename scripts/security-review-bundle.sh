#!/usr/bin/env bash
# Gathers what the security-reviewer subagent needs to review the commits
# not yet pushed, into .review/<head>/ (git-ignored). The reviewer can only
# read, so everything that needs a command or the network is run here:
# the diff, the commit list, a secret scan of the history, a dependency
# audit, and facts about each direct dependency and container image.
#
#   scripts/security-review-bundle.sh [remote-ref]    (default: origin/main)
#
# Prints the bundle directory. See docs/reviews/README.md.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

REMOTE_REF="${1:-origin/main}"
HEAD_SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short=7 HEAD)"
EMPTY_TREE="$(git hash-object -t tree /dev/null)"

# What is not pushed yet: everything since the remote branch, or the whole
# history when nothing has been pushed (the first push publishes it all).
if git rev-parse --verify -q "$REMOTE_REF" >/dev/null &&
    BASE="$(git merge-base HEAD "$REMOTE_REF" 2>/dev/null)"; then
    RANGE="$BASE..HEAD"
    DIFF_BASE="$BASE"
else
    BASE="none"
    RANGE="HEAD"
    DIFF_BASE="$EMPTY_TREE"
fi

OUT=".review/$SHORT"
rm -rf "$OUT"
mkdir -p "$OUT"

# The next finding number and this push's review iteration.
LAST_N="$(grep -ohE 'SEC-[0-9]+-[0-9]{3}' docs/reviews/*.md 2>/dev/null |
    sed -E 's/SEC-([0-9]+)-.*/\1/' | sort -n | tail -1 || true)"
NEXT_N=$(( ${LAST_N:-0} + 1 ))
ITERATION=$(( $( (grep -lE "^- Base: $BASE\$" docs/reviews/[0-9]*.md 2>/dev/null || true) | wc -l) + 1 ))

{
    echo "Head: $HEAD_SHA"
    echo "Base: $BASE"
    echo "Range: $RANGE"
    echo "Remote ref: $REMOTE_REF"
    echo "Iteration: $(printf '%03d' "$ITERATION")"
    echo "Next finding number: $NEXT_N"
    echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Report file: docs/reviews/$(date +%Y-%m-%d)-$SHORT-$(printf '%03d' "$ITERATION").md"
} >"$OUT/meta.txt"

git log --format='%h %ad %an <%ae>%n    %s' --date=short $RANGE >"$OUT/commits.txt"
git diff --stat "$DIFF_BASE" HEAD >"$OUT/diffstat.txt"
git diff --name-status "$DIFF_BASE" HEAD >"$OUT/files.txt"
git diff "$DIFF_BASE" HEAD -- . ':(exclude)uv.lock' >"$OUT/diff.patch"
git diff "$DIFF_BASE" HEAD -- uv.lock >"$OUT/uv-lock.patch"
git ls-files >"$OUT/tracked-files.txt"

# Secrets: anything in any commit being pushed, including lines later
# removed, since the whole history becomes public.
PATTERNS='(tvly-[A-Za-z0-9_-]{10,}|moltbook_[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9_-]{20,}|gh[opsu]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(api[_-]?key|secret|token|passw(or)?d)["'"'"' ]*[:=] *["'"'"']?[A-Za-z0-9_./+-]{12,})'
{
    echo "# Lines matching secret patterns in the history being pushed ($RANGE)"
    git log -p --format='commit %h' $RANGE | grep -nEi "$PATTERNS" || echo "(none)"
    echo
    echo "# Tracked files that usually hold secrets or local settings"
    git ls-files | grep -Ei '(^|/)\.env($|\.)|\.pem$|\.key$|id_rsa|compose\.override\.yaml$|credentials' ||
        echo "(none)"
    echo
    echo "# Author and committer identities that become public"
    git log --format='%an <%ae> | %cn <%ce>' $RANGE | sort | uniq -c
} >"$OUT/secrets-scan.txt"

# Dependencies: known vulnerabilities in the locked set.
uv export --frozen --no-dev --no-hashes --no-emit-project -q >"$OUT/requirements.txt"
uvx pip-audit -r "$OUT/requirements.txt" --no-deps --disable-pip --progress-spinner off \
    >"$OUT/pip-audit.txt" 2>&1 || true

# Direct dependencies: do they exist, how old, where from (against
# fabricated or typo-squatted names).
python3 - "$OUT/direct-dependencies.txt" <<'EOF'
import json, re, sys, tomllib, urllib.request
out = open(sys.argv[1], "w")
project = tomllib.load(open("pyproject.toml", "rb"))
deps = list(project["project"].get("dependencies", []))
for group in project.get("dependency-groups", {}).values():
    deps += [d for d in group if isinstance(d, str)]
for spec in deps:
    name = re.split(r"[\s<>=!~\[;]", spec, maxsplit=1)[0]
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as r:
                info = json.load(r)
            break
        except Exception as e:
            info, error = None, e
    try:
        if info is None:
            raise error
        uploads = sorted(
            f["upload_time"] for files in info["releases"].values() for f in files
        )
        urls = info["info"].get("project_urls") or {}
        home = urls.get("Source") or urls.get("Homepage") or info["info"].get("home_page") or "?"
        out.write(
            f"{spec}: on PyPI; first upload {uploads[0][:10] if uploads else '?'}, "
            f"{len(info['releases'])} releases, latest {info['info']['version']}, source {home}\n"
        )
    except Exception as e:
        out.write(f"{spec}: NOT FOUND or unreadable on PyPI ({e})\n")
EOF

# Container images: what each service runs, and whether it is pinned.
{
    echo "# Dockerfile"
    grep -nE '^\s*FROM|curl|wget|pip install|uv (pip|sync)' Dockerfile || true
    echo
    echo "# Compose images"
    grep -nE '^\s*image:' compose*.yaml || true
} >"$OUT/images.txt"

echo "$OUT"
