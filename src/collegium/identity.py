"""How the organization introduces itself to the sites it reads.

Every request Collegium makes itself carries the same User-Agent, with a
link where a site's owner can find out what it is and who runs it. Sites
can address it in robots.txt as "Collegium". (Requests SearXNG makes to
search engines on its behalf are SearXNG's own.)
"""

import os

VERSION = "0.1"
CONTACT = os.environ.get("COLLEGIUM_CONTACT") or "https://github.com/ketilaa/collegium"


def user_agent(purpose: str = "research agent") -> str:
    return f"Collegium/{VERSION} (+{CONTACT}; {purpose})"


USER_AGENT = user_agent()
