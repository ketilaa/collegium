"""Feeds approved by the owner: parsing, fetching and use by the Scout."""

import re
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from conftest import PAGES_FOR_FEEDS
from defusedxml import EntitiesForbidden

from collegium import jobs, worker
from collegium.acquisition import SearchResult
from collegium.acquisition.feeds import FeedError, FeedReader, parse_feed
from collegium.roles.base import SearchPlan
from collegium.roles.scout import ProposedObservation, ScoutReport

NOW = datetime.now(UTC)

RSS = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Lab blog</title>
  <item><title>Agents &amp; costs</title><link>https://lab.example/agents</link>
    <description>&lt;p&gt;We measured &lt;b&gt;agent&lt;/b&gt; costs.&lt;/p&gt;</description>
    <pubDate>{NOW:%a, %d %b %Y %H:%M:%S} GMT</pubDate></item>
  <item><title>No link</title></item>
</channel></rss>""".encode()

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Papers</title>
  <entry><title>A paper</title>
    <link rel="alternate" href="https://papers.example/1"/>
    <link rel="related" href="https://elsewhere.example"/>
    <summary>Abstract text.</summary><updated>2026-09-20T10:00:00Z</updated></entry>
</feed>"""


def test_rss_items_are_leads_without_html():
    [item] = parse_feed(RSS)
    assert item.url == "https://lab.example/agents"
    assert item.title == "Agents & costs"
    assert item.snippet == "We measured agent costs."
    assert item.published_at.endswith("GMT")


def test_atom_entries_use_the_alternate_link():
    [entry] = parse_feed(ATOM)
    assert (entry.url, entry.title, entry.snippet, entry.published_at) == (
        "https://papers.example/1",
        "A paper",
        "Abstract text.",
        "2026-09-20T10:00:00Z",
    )


def test_non_feeds_are_refused():
    with pytest.raises(FeedError):
        parse_feed(b"<html><body>Not a feed</body></html>")
    with pytest.raises(FeedError):
        parse_feed(b"not xml at all")


def test_entity_expansion_is_refused():
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title><link>https://x</link></item></channel></rss>"""
    with pytest.raises(EntitiesForbidden):
        parse_feed(bomb)


def test_feed_reader_fetches_with_its_own_user_agent():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=RSS)

    items = FeedReader(transport=httpx.MockTransport(handler)).crawl("https://lab.example/rss", 5)
    assert [i.url for i in items] == ["https://lab.example/agents"]
    assert seen[0].headers["user-agent"].startswith("Collegium/")


class FakeCrawler:
    name = "feed"

    def __init__(self, feeds: dict[str, list[SearchResult]], broken: set[str] = frozenset()):
        self.feeds, self.broken, self.crawled = feeds, broken, []

    def crawl(self, url, max_items):
        self.crawled.append(url)
        if url in self.broken:
            raise httpx.ConnectError("feed down")
        return self.feeds[url][:max_items]


FEED = "https://lab.example/rss"
BROKEN = "https://broken.example/rss"


def _item(url, text, published=None):
    return SearchResult(
        url=url, title=url, snippet=text, published_at=(published or NOW).isoformat()
    )


def _approve(board_db, domain_id, *urls):
    with board_db.acting_as("owner") as conn:
        for url in urls:
            conn.execute(
                "INSERT INTO approved_sources (domain_id, kind, url) VALUES (%s, 'feed', %s)",
                (domain_id, url),
            )


def test_scout_reads_approved_feeds_and_skips_known_and_old_items(
    make_context, llm, board_db, worker_db, add_domain
):
    domain_id = add_domain()
    _approve(board_db, domain_id, FEED, BROKEN)
    fresh = _item(
        "https://lab.example/new",
        "Our lab measured that agent runs cost 30 times more than single prompts on average.",
    )
    old = _item(
        "https://lab.example/old", "An old post about agents.", datetime(2024, 1, 1, tzinfo=UTC)
    )
    crawler = FakeCrawler({FEED: [fresh, old]}, broken={BROKEN})
    ctx = make_context(PAGES_FOR_FEEDS, crawler=crawler)

    llm.add(SearchPlan, SearchPlan(queries=["agent costs"]))
    llm.add(
        ScoutReport,
        lambda system, user: ScoutReport(
            observations=[
                ProposedObservation(
                    source=_number_of("https://lab.example/new", user),
                    quote="agent runs cost 30 times more than single prompts",
                    statement="A lab found agent runs cost 30 times more than single prompts.",
                    why_it_matters="w",
                    investigate=False,
                )
            ]
        ),
    )
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "scout", {"domain_id": domain_id})
    worker.drain(ctx)

    assert crawler.crawled == [FEED, BROKEN]
    prompt = llm.prompts_for(ScoutReport)[0]
    assert "https://lab.example/new" in prompt
    assert "https://lab.example/old" not in prompt

    with worker_db.reading() as conn:
        source = conn.execute(
            "SELECT s.uri, s.metadata, q.capability FROM observations o "
            "JOIN sources s ON s.id = o.source_id JOIN acquisitions q ON q.id = s.acquisition_id"
        ).fetchone()
        assert source["uri"] == "https://lab.example/new"
        assert source["metadata"]["feed"] == FEED
        assert source["capability"] == "crawl"
        crawls = conn.execute(
            "SELECT request->>'url' AS url, error FROM acquisitions WHERE capability = 'crawl' "
            "ORDER BY requested_at"
        ).fetchall()
        assert [c["url"] for c in crawls] == [FEED, BROKEN]
        assert crawls[1]["error"].startswith("ConnectError")
        assert "(1 feeds failed)" in conn.execute("SELECT notes FROM runs").fetchone()["notes"]

    # Next run: the recorded item is not shown again.
    llm.add(SearchPlan, SearchPlan(queries=["agent costs"]))
    llm.add(ScoutReport, ScoutReport(observations=[]))
    with board_db.acting_as("owner") as conn:
        jobs.enqueue(conn, "scout", {"domain_id": domain_id})
    worker.drain(ctx)
    assert "https://lab.example/new" not in llm.prompts_for(ScoutReport)[1]


def _number_of(url: str, prompt: str) -> int:
    """The R<n> number the Scout was shown for a lead (its title is its url)."""
    return int(re.search(rf"<<<R(\d+) \w+>>>\n{re.escape(url)}", prompt).group(1))


def test_only_the_board_approves_sources(worker_db, add_domain):
    domain_id = add_domain()
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        worker_db.acting_as("strategist") as conn,
    ):
        conn.execute(
            "INSERT INTO approved_sources (domain_id, kind, url) VALUES (%s, 'feed', 'x')",
            (domain_id,),
        )
