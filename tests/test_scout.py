"""Unit tests for scout.py helpers that don't need the full pipeline."""

from conftest import FakeProvider

from collegium.acquisition import Acquisition
from collegium.config import Settings
from collegium.roles.base import Context
from collegium.roles.scout import _search


def test_known_company_and_organization_entities_are_tried_against_name_search_sources(
    worker_db, llm
):
    """A name-search source (a company registry such as brreg or prh)
    answers a name, not a topic: the Scout's own topical queries almost
    never match one there, so known company/organization entities are
    tried against it directly too -- but only against that source, and
    only entities of a kind a registry could actually hold."""
    web = FakeProvider({"https://news.example/a": ("A story", "Some text about the market.")})
    registry = FakeProvider({})
    registry.name_search = True
    acquisition = Acquisition(
        {"fake": web, "registry": registry}, web, default="fake", crawler=None
    )
    ctx = Context(db=worker_db, llm=llm, acquisition=acquisition, settings=Settings())
    entities = [
        {"name": "Acme AS", "entity_type": "company"},
        {"name": "Nordic Consulting", "entity_type": "organization"},
        {"name": "Jane Doe", "entity_type": "person"},
    ]

    _search(ctx, ["topical market search"], ["fake", "registry"], entities)

    web_queries = {q for q, _ in web.searches}
    registry_queries = {q for q, _ in registry.searches}
    # The registry also gets the Scout's own topical query, as before --
    # this fix adds the name round, it does not remove that one.
    assert web_queries == {"topical market search"}
    assert registry_queries == {"topical market search", "Acme AS", "Nordic Consulting"}


def test_no_extra_round_without_a_name_search_source(worker_db, llm):
    """Entities are only tried against sources that actually search by
    name; with none configured, there is nothing extra to run."""
    web = FakeProvider({"https://news.example/a": ("A story", "Some text about the market.")})
    acquisition = Acquisition({"fake": web}, web, default="fake", crawler=None)
    ctx = Context(db=worker_db, llm=llm, acquisition=acquisition, settings=Settings())
    entities = [{"name": "Acme AS", "entity_type": "company"}]

    _search(ctx, ["topical market search"], ["fake"], entities)

    assert {q for q, _ in web.searches} == {"topical market search"}
