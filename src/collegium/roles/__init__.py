"""The organization's roles, keyed by the job kind each one handles."""

from collegium.roles.answerer import Answerer
from collegium.roles.base import Role
from collegium.roles.corroborator import Corroborator
from collegium.roles.historian import Historian
from collegium.roles.mapper import Mapper
from collegium.roles.researcher import Researcher
from collegium.roles.resolver import Resolver
from collegium.roles.scout import Scout
from collegium.roles.skeptic import Skeptic
from collegium.roles.strategist import Strategist

ROLES: dict[str, Role] = {
    r.job_kind: r
    for r in (
        Scout(),
        Researcher(),
        Skeptic(),
        Historian(),
        Mapper(),
        Resolver(),
        Corroborator(),
        Strategist(),
        Answerer(),
    )
}
