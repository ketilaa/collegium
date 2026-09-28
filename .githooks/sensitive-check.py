"""Refuses text that should never be committed to this public repository:
out-of-repository paths (a home directory, a temporary folder), email
addresses that are not examples, and the owner's sensitive terms (names of
people, the employer, machine names), which cannot be recognized by pattern
and so are kept in a local list that is never committed:
~/.collegium/review-terms.txt, or COLLEGIUM_REVIEW_TERMS. One term per
line, matched case-insensitively as whole words; lines starting with #
are comments.

    python3 .githooks/sensitive-check.py staged        # pre-commit
    python3 .githooks/sensitive-check.py message FILE  # commit-msg

A hit names the file, the line and the term's number, never the term.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "a home directory": re.compile(
        r"/Users/[A-Za-z0-9._-]+|/home/[a-z_][a-z0-9_-]*|/root/|[A-Za-z]:\\[Uu]sers\\"
    ),
    "a temporary folder": re.compile(r"/private/(?:tmp|var)/|/var/folders/"),
    "an email address": re.compile(
        r"[A-Za-z0-9._%+-]+@"
        r"(?!(?:example\.(?:com|org|net)|users\.noreply\.github\.com|anthropic\.com)\b(?!\.))"
        r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    ),
}


def terms() -> list[re.Pattern]:
    path = Path(
        os.environ.get("COLLEGIUM_REVIEW_TERMS") or Path.home() / ".collegium/review-terms.txt"
    )
    if not path.is_file():
        return []
    lines = [t.strip() for t in path.read_text().splitlines()]
    return [
        re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.IGNORECASE)
        for t in lines
        if t and not t.startswith("#")
    ]


# This script names the patterns it looks for, so its own lines are checked
# for addresses and listed terms only.
SELF = ".githooks/sensitive-check.py"


def problems(
    where: str, text: str, listed: list[re.Pattern], patterns: dict | None = None
) -> list[str]:
    patterns = PATTERNS if patterns is None else patterns
    found = [f"{where}: {what}" for what, p in patterns.items() if p.search(text)]
    found += [
        f"{where}: term {n} of the local list" for n, p in enumerate(listed, 1) if p.search(text)
    ]
    return found


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def staged(listed: list[re.Pattern]) -> list[str]:
    found: list[str] = []
    for path in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0"):
        if not path:
            continue
        patterns = {"an email address": PATTERNS["an email address"]} if path == SELF else None
        found += problems(f"{path} (its path)", path, listed)
        blob = subprocess.run(["git", "show", f":{path}"], capture_output=True).stdout
        if b"\0" in blob[:8000]:
            text = "\n".join(s.decode() for s in re.findall(rb"[ -~]{6,}", blob))
            found += problems(f"{path} (binary)", text, listed)
            continue
        diff = git("diff", "--cached", "-U0", "--no-color", "--", path)
        number, in_hunk = 0, False
        for line in diff.splitlines():
            if line.startswith("@@"):
                number, in_hunk = int(re.search(r"\+(\d+)", line).group(1)), True
            elif in_hunk and line.startswith("+"):  # the file header comes before any @@
                found += problems(f"{path}:{number}", line[1:], listed, patterns)
                number += 1
    return found


def main() -> int:
    listed = terms()
    if sys.argv[1:2] == ["message"]:
        found = problems("the commit message", Path(sys.argv[2]).read_text(), listed)
    else:
        found = staged(listed)
    if not listed:
        print(
            "sensitive-check: no local term list; names and employer are not checked",
            file=sys.stderr,
        )
    for f in found:
        print(f"sensitive-check: {f}", file=sys.stderr)
    if found:
        print(
            "sensitive-check: refused; remove it, or keep it out of the repository", file=sys.stderr
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
