"""Checks that model output is grounded in what was actually retrieved.

A local model will sometimes invent a quote or tidy one up. Evidence is only
stored when its excerpt can be found in the source text, and what is stored
is the source's own wording, not the model's version of it.
"""

import re
from difflib import SequenceMatcher

MIN_EXCERPT_CHARS = 20
MAX_EXCERPT_CHARS = 600  # evidence is a passage, not a page

_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


# Quote marks, markdown emphasis, list bullets and dashes: models add, drop
# and swap them freely when quoting, and they carry no meaning for whether a
# passage was quoted.
_IGNORED = set("\"'*_`#>-\u2022")


def _normalize(text: str) -> tuple[str, list[int]]:
    """Lowercased text without quote marks or markdown emphasis, with
    collapsed whitespace, plus the index in `text` of each normalized
    character."""
    chars: list[str] = []
    index: list[int] = []
    pending_space = False
    for i, ch in enumerate(text.translate(_TRANSLATE)):
        if ch in _IGNORED:
            continue
        if ch.isspace():
            pending_space = bool(chars)
            continue
        if pending_space:
            chars.append(" ")
            index.append(i - 1)
            pending_space = False
        chars.append(ch.lower())
        index.append(i)
    return "".join(chars), index


def locate_excerpt(excerpt: str, document: str, min_ratio: float = 0.85) -> str | None:
    """The passage of `document` that `excerpt` quotes, or None.

    Exact matches (ignoring case, whitespace, quote marks and markdown
    emphasis) are accepted
    directly. Otherwise the closest passage must share at least `min_ratio`
    of the excerpt's characters, in order.
    """
    ex, _ = _normalize(excerpt)
    doc, index = _normalize(document)
    if not MIN_EXCERPT_CHARS <= len(ex) <= MAX_EXCERPT_CHARS or not doc:
        return None

    pos = doc.find(ex)
    if pos >= 0:
        return _original(document, index, pos, pos + len(ex))

    anchor = SequenceMatcher(None, doc, ex, autojunk=False).find_longest_match(
        0, len(doc), 0, len(ex)
    )
    if anchor.size < min(MIN_EXCERPT_CHARS, len(ex) // 3):
        return None
    slack = len(ex) // 10
    start = max(0, anchor.a - anchor.b - slack)
    end = min(len(doc), anchor.a + (len(ex) - anchor.b) + slack)
    blocks = [
        b
        for b in SequenceMatcher(None, doc[start:end], ex, autojunk=False).get_matching_blocks()
        if b.size
    ]
    if sum(b.size for b in blocks) / len(ex) < min_ratio:
        return None
    return _original(document, index, start + blocks[0].a, start + blocks[-1].a + blocks[-1].size)


def _original(document: str, index: list[int], start: int, end: int) -> str:
    """The span of `document` behind normalized [start, end), widened to
    whole words."""
    a, b = index[start], index[end - 1] + 1
    while a > 0 and document[a - 1].isalnum():
        a -= 1
    while b < len(document) and document[b].isalnum():
        b += 1
    return document[a:b].strip()


# Capitalised words (names, organisations, roles) and numbers: the parts of a
# statement a model most often gets wrong when paraphrasing.
_NAME_OR_NUMBER = re.compile(r"\b[A-Z][\w&.-]*|\b\d[\d.,]*")
_POSSESSIVE = re.compile(r"['’]s\b")
# Capitalised only because they start a sentence; not names.
_COMMON = frozenset(
    [
        "a",
        "about",
        "according",
        "after",
        "an",
        "as",
        "at",
        "before",
        "by",
        "during",
        "for",
        "from",
        "in",
        "it",
        "its",
        "many",
        "most",
        "new",
        "on",
        "over",
        "some",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "under",
        "with",
    ]
)


def unsupported_terms(statement: str, support: str) -> list[str]:
    """Names and numbers in `statement` that do not occur in `support`.

    A cheap check that a paraphrase has not introduced a name or a figure.
    It cannot tell whether names are correctly associated ("Anthropic CEO
    Sam Altman" passes if both names occur), so it complements, not
    replaces, a check by the model.
    """
    text, _ = _normalize(_POSSESSIVE.sub("", support).replace(",", ""))
    missing = []
    for term in _NAME_OR_NUMBER.findall(_POSSESSIVE.sub("", statement)):
        key, _ = _normalize(term.replace(",", "").rstrip("."))
        if key in _COMMON:
            continue
        if key and not re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):
            missing.append(term.rstrip("."))
    return missing


def mentions(text: str, name: str) -> bool:
    """Whether `name` occurs in `text` as whole words, ignoring case, quote
    marks and spacing."""
    haystack, _ = _normalize(text)
    needle, _ = _normalize(name)
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
