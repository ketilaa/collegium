"""How the organization introduces itself to the sites it reads.

Every request Collegium makes itself carries the same User-Agent. It names
the software, with a link saying what Collegium is, and, when the operator
has set COLLEGIUM_CONTACT, who runs this copy: the software is public, so
without that a site owner could only reach its authors, who do not run
other people's copies. Sites can address it in robots.txt as "Collegium".
(Requests SearXNG makes to search engines on its behalf are SearXNG's own.)
"""

import os

VERSION = "0.1"
SOFTWARE = "https://github.com/ketilaa/collegium"
# Who runs this copy: a URL or mailto: address the operator is happy to
# have sent to every site the organization reads.
CONTACT = os.environ.get("COLLEGIUM_CONTACT") or None


def user_agent(purpose: str = "research agent", contact: str | None = CONTACT) -> str:
    operator = f"; operated by {contact}" if contact else ""
    return f"Collegium/{VERSION} (+{SOFTWARE}; {purpose}{operator})"


USER_AGENT = user_agent()

MISSING_CONTACT = (
    "COLLEGIUM_CONTACT is not set: sites Collegium reads cannot tell who runs this "
    "copy. Set it to a URL or mailto: address of yours."
)
