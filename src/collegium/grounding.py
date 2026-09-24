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

    Statements are written in English, but the support may not be. Numbers
    are compared by their digits, so "12,5" matches "12.5" and "1 200"
    matches "1,200". In a quote that is not English, only unmistakable names
    (acronyms, mixed case) are checked: an English statement capitalises
    words such as "Norwegian" that the quote's own language does not.
    """
    support = _POSSESSIVE.sub("", support)
    text, _ = _normalize(support.replace(",", ""))
    numbers = {_digits(n) for n in _NUMBER.findall(support)}
    english = is_english(support)
    missing = []
    for term in _NAME_OR_NUMBER.findall(_POSSESSIVE.sub("", statement)):
        term = term.rstrip(".")
        if term[0].isdigit():
            if _digits(term) not in numbers:
                missing.append(term)
            continue
        key, _ = _normalize(term)
        if key in _COMMON or (not english and not _UNMISTAKABLE_NAME.fullmatch(term)):
            continue
        if key and not re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):
            missing.append(term)
    return missing


# A number as written in English or Norwegian: 1,200 / 1 200 / 12.5 / 12,5.
_NUMBER = re.compile(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)|\d[\d.,]*")
# Acronyms (NAV, SSB) and mixed case (OpenAI, GitHub): names in any language.
_UNMISTAKABLE_NAME = re.compile(r"[A-Z][\w&.-]*[A-Z][\w&.-]*")


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number)


_ENGLISH = frozenset(
    [
        "the",
        "and",
        "of",
        "to",
        "in",
        "is",
        "that",
        "with",
        "on",
        "are",
        "was",
        "were",
        "has",
        "have",
        "by",
        "from",
        "it",
        "this",
        "be",
        "as",
        "will",
        "not",
        "an",
        "or",
        "their",
        "its",
        "which",
        "who",
    ]
)
_NORWEGIAN = frozenset(
    [
        "og",
        "i",
        "på",
        "er",
        "det",
        "som",
        "til",
        "med",
        "av",
        "ikke",
        "å",
        "en",
        "et",
        "har",
        "ble",
        "fra",
        "om",
        "kan",
        "vil",
        "etter",
        "ved",
        "også",
        "seg",
        "hun",
        "han",
        "de",
    ]
)


def is_english(text: str) -> bool:
    """A rough guess: more common English words than Norwegian ones. Words
    with æ, ø or å count as Norwegian. Text with neither counts as English."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    english = sum(w in _ENGLISH for w in words)
    norwegian = sum(w in _NORWEGIAN or bool(set(w) & set("æøå")) for w in words)
    return english >= norwegian


def mentions(text: str, name: str) -> bool:
    """Whether `name` occurs in `text` as whole words, ignoring case, quote
    marks and spacing."""
    haystack, _ = _normalize(text)
    needle, _ = _normalize(name)
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
