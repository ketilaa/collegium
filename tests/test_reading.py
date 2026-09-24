"""Reading pages for free, with a paid fallback, and never reading inside."""

import httpx
import pytest

from collegium.acquisition import Acquisition, BudgetExhausted, Document, SearchResult
from collegium.acquisition.web import WebExtractor

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
