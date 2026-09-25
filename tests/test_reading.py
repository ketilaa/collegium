"""Reading pages for free, with a paid fallback, and never reading inside."""

import asyncio
import re
import time

import httpx
import pytest

from collegium.acquisition import Acquisition, BudgetExhausted, Document, SearchResult
from collegium.acquisition.fetching import Refused, SafeFetcher
from collegium.acquisition.web import TEXT_TYPES, WebExtractor

PARAGRAPHS = [
    "Teams that adopted coding agents measured how often the agents' changes were merged "
    "without edits. Over three months the share rose from a fifth to nearly half.",
    "Review time per change stayed about the same, which surprised the engineering leads "
    "who had expected reviews to become the new bottleneck.",
    "Junior developers were given the agents last, after the pilot teams, and their first "
    "month shows the widest spread in results of any group.",
    "The company plans to publish the full measurements, including the changes that were "
    "rejected, once the second quarter of use is complete.",
]
ARTICLE = f"""<html><head><title>Agents at work</title></head><body>
<nav>Home | About</nav>
<article><h1>Agents at work</h1>{"".join(f"<p>{p}</p>" for p in PARAGRAPHS)}</article>
<footer>Copyright</footer></body></html>"""

PUBLIC = {"news.example": ["93.184.216.34"], "blog.example": ["93.184.216.35"]}


def resolver(table):
    def resolve(host):
        return table[host]

    return resolve


def extractor(handler, table=None):
    return WebExtractor(transport=httpx.MockTransport(handler), resolver=resolver(table or PUBLIC))


def html(body, status=200, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/html", **headers})


def test_reads_the_article_and_leaves_out_the_page_around_it():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return html(ARTICLE)

    [doc] = extractor(handler).extract(["https://news.example/agents"])
    assert doc.url == "https://news.example/agents"
    assert doc.title == "Agents at work"
    assert "merged without edits" in doc.content
    assert "Copyright" not in doc.content and "Home | About" not in doc.content


def test_pages_it_cannot_read_are_left_for_the_fallback():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/short":
            return html("<html><body><p>Enable JavaScript.</p></body></html>")
        if request.url.path == "/paper.pdf":
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        return httpx.Response(403)

    urls = [
        "https://news.example/short",
        "https://news.example/paper.pdf",
        "https://news.example/x",
    ]
    assert extractor(handler).extract(urls) == []


@pytest.mark.parametrize(
    ("url", "table"),
    [
        ("http://localhost/admin", {"localhost": ["127.0.0.1"]}),
        ("http://db:5432/", {"db": ["172.18.0.2"]}),
        ("http://metadata.example/latest", {"metadata.example": ["169.254.169.254"]}),
        ("http://intranet.example/", {"intranet.example": ["10.0.0.5"]}),
        ("http://v6.example/", {"v6.example": ["::1"]}),
        ("https://news.example:8443/agents", PUBLIC),
        ("file:///etc/passwd", PUBLIC),
    ],
)
def test_never_reads_inside_the_organization(url, table):
    requests = []

    def handler(request):
        requests.append(request)
        return html(ARTICLE)

    assert extractor(handler, table).extract([url]) == []
    assert requests == []


def test_a_redirect_into_the_organization_is_refused():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://localhost/admin"})

    table = {**PUBLIC, "localhost": ["127.0.0.1"]}
    assert extractor(handler, table).extract(["https://news.example/agents"]) == []
    assert not any("localhost" in r for r in requests)


def test_robots_txt_is_respected():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return html(ARTICLE)

    reader = extractor(handler)
    assert reader.extract(["https://news.example/private/agents"]) == []
    assert len(reader.extract(["https://news.example/agents"])) == 1


# ---------------------------------------------------------------------------
# Free first, paid as fallback
# ---------------------------------------------------------------------------


class Reader:
    def __init__(self, name, pages, metered=False):
        self.name, self.pages, self.metered, self.asked = name, pages, metered, []

    def extract(self, urls):
        self.asked.append(urls)
        return [Document(url=u, title=u, content=self.pages[u]) for u in urls if u in self.pages]


class Search:
    def __init__(self, name, results=(), metered=False, fails=False):
        self.name, self.results, self.metered, self.fails = name, list(results), metered, fails

    def discover(self, query, max_results, *, recent_days=None):
        if self.fails:
            raise httpx.ConnectError("down")
        return self.results[:max_results]


def lead(url):
    return SearchResult(url=url, title=url, snippet="x" * 300)


def acquisition(free, paid, *, budget=10, default="free", searches=None):
    calls = []
    searches = searches or {"free": Search("free", [lead("https://a.example")])}
    return (
        Acquisition(
            searches,
            free,
            default=default,
            fallback_extractor=paid,
            fallback_search="paid" if "paid" in searches else None,
            recorder=lambda cap, provider, request, n, error: calls.append((cap, provider, n)),
            budget=lambda: budget,
        ),
        calls,
    )


def test_the_paid_reader_only_reads_what_the_free_one_could_not():
    free = Reader("web", {"https://a.example": "free text"})
    paid = Reader("tavily", {"https://b.example": "paid text"}, metered=True)
    acq, calls = acquisition(free, paid)
    docs = acq.extract(["https://a.example", "https://b.example"])
    assert {d.url: d.content for d in docs} == {
        "https://a.example": "free text",
        "https://b.example": "paid text",
    }
    assert paid.asked == [["https://b.example"]]
    assert calls == [("extract", "web", 1), ("extract", "tavily", 1)]
    assert not acq.paid_first


def test_a_spent_budget_leaves_pages_unread_instead_of_stopping_free_work():
    free = Reader("web", {"https://a.example": "free text"})
    paid = Reader("tavily", {"https://b.example": "paid text"}, metered=True)
    acq, _ = acquisition(free, paid, budget=0)
    docs = acq.extract(["https://a.example", "https://b.example"])
    assert [d.url for d in docs] == ["https://a.example"]
    assert paid.asked == []

    # When the paid reader comes first, a spent budget still stops the job.
    paid_first, _ = acquisition(paid, None, budget=0)
    assert paid_first.paid_first
    with pytest.raises(BudgetExhausted):
        paid_first.extract(["https://b.example"])


def test_the_fallback_search_answers_when_the_default_fails_or_finds_nothing():
    for broken in (Search("free", fails=True), Search("free", [])):
        searches = {"free": broken, "paid": Search("paid", [lead("https://p.example")], True)}
        acq, calls = acquisition(Reader("web", {}), None, searches=searches)
        assert [r.url for r in acq.discover("agents", max_results=5)] == ["https://p.example"]
        assert calls[-1] == ("discover", "paid", 1)


def test_a_paid_source_with_no_budget_is_skipped_among_free_ones():
    searches = {
        "free": Search("free", [lead("https://a.example")]),
        "paid": Search("paid", [lead("https://p.example")], metered=True),
    }
    acq, _ = acquisition(Reader("web", {}), None, budget=0, searches=searches)
    results = acq.discover("agents", max_results=5, sources=["free", "paid"])
    assert [r.url for r in results] == ["https://a.example"]
    with pytest.raises(BudgetExhausted):
        acq.discover("agents", max_results=5, sources=["paid"])


def test_a_page_is_read_at_most_once_per_run():
    free = Reader("web", {"https://a.example": "free text"})
    paid = Reader("tavily", {"https://b.example": "paid text"}, metered=True)
    acq, calls = acquisition(free, paid)
    for _ in range(3):  # e.g. the same lead found by three queries
        acq.extract(["https://a.example", "https://b.example"])
    assert free.asked == [["https://a.example", "https://b.example"]]
    assert paid.asked == [["https://b.example"]]
    assert len(calls) == 2


def test_social_and_video_pages_are_not_worth_a_paid_read():
    free = Reader("web", {})
    paid = Reader(
        "tavily",
        {"https://twitter.com/x/status/1": "a tweet", "https://news.example/a": "news"},
        metered=True,
    )
    acq, _ = acquisition(free, paid)
    docs = acq.extract(
        ["https://twitter.com/x/status/1", "https://youtu.be/abc", "https://news.example/a"]
    )
    assert [d.url for d in docs] == ["https://news.example/a"]
    assert paid.asked == [["https://news.example/a"]]


def test_failed_paid_calls_do_not_count_against_the_budget(worker_db):
    from collegium import memory

    with worker_db.acting_as("scout") as conn:
        for error in (None, None, "ConnectError: certificate verify failed"):
            memory.record_acquisition(
                conn,
                capability="discover",
                provider="tavily",
                request={},
                result_count=0,
                error=error,
            )
    with worker_db.reading() as conn:
        used, _ = memory.paid_calls_in_window(conn, ["tavily"])
    assert used == 2


# ---------------------------------------------------------------------------
# Finding a site's feed
# ---------------------------------------------------------------------------

FEED_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>Lab news</title>
{items}</channel></rss>""".format(
    items="".join(
        f"<item><title>Post {i}</title><link>https://news.example/{i}</link></item>"
        for i in range(4)
    )
)


def test_finds_the_feed_a_page_announces():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return html(
                '<html><head><link rel="alternate" type="application/rss+xml" '
                'href="/news/rss.xml"></head><body>Home</body></html>'
            )
        if request.url.path == "/news/rss.xml":
            return httpx.Response(
                200, text=FEED_XML, headers={"content-type": "application/rss+xml"}
            )
        return httpx.Response(404)

    [feed] = extractor(handler).find_feed("https://news.example")
    assert (feed.url, feed.title, feed.metadata["items"]) == (
        "https://news.example/news/rss.xml",
        "Lab news",
        4,
    )


def test_tries_the_usual_places_and_gives_up_quietly():
    def handler(request):
        if request.url.path == "/feed":
            return httpx.Response(200, text=FEED_XML, headers={"content-type": "text/xml"})
        return httpx.Response(404)

    [feed] = extractor(handler).find_feed("https://news.example")
    assert feed.url == "https://news.example/feed"
    assert extractor(lambda r: httpx.Response(404)).find_feed("https://news.example") == []


def test_a_feed_link_into_the_organization_is_not_followed():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/":
            return html('<link type="application/rss+xml" href="http://localhost/admin/feed">')
        return httpx.Response(404)

    table = {**PUBLIC, "localhost": ["127.0.0.1"]}
    assert extractor(handler, table).find_feed("https://news.example") == []
    assert not any("localhost" in r for r in requests)


def test_a_feed_with_whole_articles_is_found_although_larger_than_a_page():
    padding = "<!-- " + "x" * 4_000_000 + " -->"

    def handler(request):
        if request.url.path == "/feed":
            return httpx.Response(
                200,
                text=FEED_XML.replace("<rss", padding + "<rss", 1),
                headers={"content-type": "application/rss+xml"},
            )
        return httpx.Response(404)

    [feed] = extractor(handler).find_feed("https://news.example")
    assert feed.url == "https://news.example/feed"


# ---------------------------------------------------------------------------
# The protected fetch itself
# ---------------------------------------------------------------------------


def fetcher(handler, table=None, resolve=None):
    return SafeFetcher(
        transport=httpx.MockTransport(handler), resolver=resolve or resolver(table or PUBLIC)
    )


def no_robots(handler):
    async def wrapped(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return await handler(request)

    return wrapped


def within(seconds, call):
    """Run `call`, which must give up with Refused, and within `seconds`."""
    started = time.monotonic()
    with pytest.raises(Refused, match="longer than"):
        call()
    assert time.monotonic() - started < seconds


async def trickle():
    for _ in range(100):
        await asyncio.sleep(0.05)
        yield b"<p>still coming</p>"


def test_a_page_that_trickles_in_is_given_up_at_its_deadline():
    @no_robots
    async def handler(request):
        return httpx.Response(200, content=trickle(), headers={"content-type": "text/html"})

    within(1, lambda: fetcher(handler).fetch("https://news.example/slow", TEXT_TYPES, deadline=0.3))


def test_headers_that_trickle_in_are_given_up_at_the_deadline():
    @no_robots
    async def handler(request):
        await asyncio.sleep(5)  # the server takes its time before answering at all
        return html(ARTICLE)

    within(1, lambda: fetcher(handler).fetch("https://news.example/slow", TEXT_TYPES, deadline=0.3))


def test_a_robots_txt_that_trickles_in_counts_against_the_deadline():
    async def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=trickle())
        return html(ARTICLE)

    within(
        1, lambda: fetcher(handler).fetch("https://news.example/agents", TEXT_TYPES, deadline=0.3)
    )


def test_a_slow_name_lookup_counts_against_the_deadline():
    def slow_resolve(host):
        time.sleep(2)
        return PUBLIC[host]

    async def handler(request):
        return html(ARTICLE)

    within(
        1,
        lambda: fetcher(handler, resolve=slow_resolve).fetch(
            "https://news.example/agents", TEXT_TYPES, deadline=0.3
        ),
    )


def test_a_huge_robots_txt_is_read_only_as_far_as_the_cap_and_still_obeyed():
    robots = "User-agent: *\nDisallow: /private/\n" + "# padding\n" * 100_000

    async def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return html(ARTICLE)

    with pytest.raises(Refused, match="robots.txt disallows"):
        fetcher(handler).fetch("https://news.example/private/x", TEXT_TYPES)
    _, _, body = fetcher(handler).fetch("https://news.example/agents", TEXT_TYPES)
    assert b"Agents at work" in body


def test_only_a_few_announced_feeds_are_tried():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/":
            return html(
                "".join(f'<link type="application/rss+xml" href="/feed{i}.xml">' for i in range(50))
            )
        return httpx.Response(404)

    assert extractor(handler).find_feed("https://news.example") == []
    announced = [p for p in requested if re.fullmatch(r"/feed\d+\.xml", p)]
    assert announced == ["/feed0.xml", "/feed1.xml", "/feed2.xml"]
