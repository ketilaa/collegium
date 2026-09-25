"""The HTTP adapters, against mock transports."""

import contextlib
import json

import httpx
import pytest
from conftest import FakeProvider

from collegium.acquisition.hackernews import HackerNewsDiscovery
from collegium.acquisition.tavily import TavilyProvider
from collegium.llm import LLMError, OpenAICompatibleLLM
from collegium.roles.base import SearchPlan


def _chat_reply(content: str, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
    )


def test_llm_sends_schema_and_parses_reply():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _chat_reply('<think>hmm</think>{"queries": ["ai pricing"]}')

    llm = OpenAICompatibleLLM(
        "http://llm/v1", "qwen3:14b", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert llm.generate("sys", "user", SearchPlan).queries == ["ai pricing"]
    body = requests[0]
    assert body["model"] == "qwen3:14b"
    assert body["max_tokens"] == 2048
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["queries"]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_llm_retries_with_the_validation_error():
    replies = iter(['{"queries": []}', '{"queries": ["second try"]}'])
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _chat_reply(next(replies))

    llm = OpenAICompatibleLLM(
        "http://llm/v1", "m", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert llm.generate("sys", "user", SearchPlan).queries == ["second try"]
    retry = requests[1]["messages"]
    assert retry[-1]["role"] == "user" and "not valid" in retry[-1]["content"]


def test_llm_gives_up_after_max_attempts():
    llm = OpenAICompatibleLLM(
        "http://llm/v1",
        "m",
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: _chat_reply("no json"))),
    )
    with pytest.raises(LLMError):
        llm.generate("sys", "user", SearchPlan)


def test_tavily_search_and_extract():
    requests = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer key"
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path == "/search":
            assert body["query"] == "ai pricing" and body["max_results"] == 2
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://a.example",
                            "title": "A",
                            "content": "snippet",
                            "score": 0.9,
                            "published_date": "2026-09-01",
                        }
                    ]
                },
            )
        assert request.url.path == "/extract"
        assert body["urls"] == ["https://a.example"]
        return httpx.Response(
            200, json={"results": [{"url": "https://a.example", "raw_content": "full text"}]}
        )

    provider = TavilyProvider("key", transport=httpx.MockTransport(handler))
    [result] = provider.discover("ai pricing", 2)
    assert "topic" not in requests[0]
    assert (result.url, result.snippet, result.score, result.published_at) == (
        "https://a.example",
        "snippet",
        0.9,
        "2026-09-01",
    )
    [doc] = provider.extract(["https://a.example"])
    assert doc.content == "full text"


def test_tavily_recent_search_uses_news_topic():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    TavilyProvider("key", transport=httpx.MockTransport(handler)).discover("q", 3, recent_days=30)
    assert requests[0]["topic"] == "news" and requests[0]["days"] == 30


def test_hacker_news_leads_point_at_the_linked_article():
    requests = []

    def handler(request):
        requests.append(request.url.params)
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "101",
                        "title": "Agents are getting cheaper",
                        "url": "https://blog.example/agents",
                        "points": 312,
                        "num_comments": 140,
                        "created_at": "2026-09-20T10:00:00Z",
                        "story_text": None,
                    },
                    {
                        "objectID": "102",
                        "title": "Ask HN: Who runs agents in production?",
                        "url": None,
                        "points": 50,
                        "num_comments": 80,
                        "created_at": "2026-09-21T10:00:00Z",
                        "story_text": "<p>Curious what it costs &amp; what breaks.</p>",
                    },
                ]
            },
        )

    hn = HackerNewsDiscovery(transport=httpx.MockTransport(handler))
    article, ask = hn.discover("ai agents", 5, recent_days=30)

    params = requests[0]
    assert params["query"] == params["optionalWords"] == "ai agents"
    assert params["tags"] == "story"
    assert params["hitsPerPage"] == "5"
    points, since = params["numericFilters"].split(",")
    assert points == "points>=30" and since.startswith("created_at_i>")

    assert article.url == "https://blog.example/agents"
    assert article.snippet == "Hacker News: 312 points, 140 comments"
    assert article.published_at == "2026-09-20T10:00:00Z"
    assert article.metadata == {
        "hn_discussion": "https://news.ycombinator.com/item?id=101",
        "hn_points": 312,
        "hn_comments": 140,
    }
    # A text post has no article: the discussion is the lead.
    assert ask.url == "https://news.ycombinator.com/item?id=102"
    assert ask.snippet.startswith("Curious what it costs & what breaks.")


def test_hacker_news_without_window_filters_only_on_points():
    requests = []

    def handler(request):
        requests.append(request.url.params)
        return httpx.Response(200, json={"hits": []})

    HackerNewsDiscovery(min_points=100, transport=httpx.MockTransport(handler)).discover("q", 3)
    assert requests[0]["numericFilters"] == "points>=100"


def test_llm_asks_again_briefly_when_a_reply_is_cut_off():
    replies = iter(
        [_chat_reply('{"queries": ["a", "a", "a", "a', "length"), _chat_reply('{"queries": ["a"]}')]
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return next(replies)

    llm = OpenAICompatibleLLM(
        "http://llm/v1",
        "m",
        max_tokens=50,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert llm.generate("sys", "user", SearchPlan).queries == ["a"]
    retry = requests[1]
    assert retry["max_tokens"] == 50
    # The runaway reply is not sent back; only the original prompt and a note.
    assert [m["role"] for m in retry["messages"]] == ["system", "user", "user"]
    assert "cut off" in retry["messages"][2]["content"]


def test_llm_gives_up_on_replies_that_keep_running_away():
    llm = OpenAICompatibleLLM(
        "http://llm/v1",
        "m",
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: _chat_reply('{"q', "length"))),
    )
    with pytest.raises(LLMError, match="cut off at 2048 tokens"):
        llm.generate("sys", "user", SearchPlan)


def test_searxng_returns_web_leads_within_the_time_window():
    from collegium.acquisition.searxng import SearXNGDiscovery, time_range

    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://news.example/juniors",
                        "title": "Færre juniorstillinger",
                        "content": "Antallet utlysninger falt.",
                        "publishedDate": "2026-09-20T08:00:00",
                        "score": 2.5,
                        "engines": ["bing", "duckduckgo"],
                    },
                    {"url": "javascript:alert(1)", "title": "bad"},
                    {"url": "https://news.example/untitled", "title": ""},
                    {"url": "https://blog.example/b", "title": "B", "content": "b"},
                ]
            },
        )

    searxng = SearXNGDiscovery(
        "http://searxng:8080/", transport=httpx.MockTransport(handler), pause=0
    )
    results = searxng.discover("juniorutviklere KI", 5, recent_days=30)
    assert [r.url for r in results] == ["https://news.example/juniors", "https://blog.example/b"]
    assert results[0].metadata == {"engines": ["bing", "duckduckgo"]}
    assert seen[0] | {} == {
        "q": "juniorutviklere KI",
        "format": "json",
        "language": "all",
        "safesearch": "0",
        "time_range": "month",
    }
    assert [time_range(d) for d in (None, 1, 7, 30, 90)] == [None, "day", "week", "month", "year"]
    assert len(searxng.discover("x", 1)) == 1


def test_blocked_search_engines_are_an_outage_not_an_empty_result():
    from collegium.acquisition import Acquisition, SearchUnavailable
    from collegium.acquisition.searxng import SearXNGDiscovery

    def blocked(request):
        return httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [["brave", "too many requests"], ["duckduckgo", "CAPTCHA"]],
            },
        )

    def empty(request):
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    searxng = SearXNGDiscovery("http://s", transport=httpx.MockTransport(blocked), pause=0)
    with pytest.raises(SearchUnavailable, match="duckduckgo"):
        searxng.discover("q", 5)
    assert SearXNGDiscovery("http://s", transport=httpx.MockTransport(empty)).discover("q", 5) == []

    # No paid search stands in for an outage, but it does for finding nothing.
    paid = FakeProvider({"https://p.example": ("P", "paid result")})
    acquisition = Acquisition(
        {"searxng": searxng, "tavily": paid}, paid, default="searxng", fallback_search="tavily"
    )
    with pytest.raises(SearchUnavailable):
        acquisition.discover("q", max_results=5)
    assert paid.searches == []
    # A scout with other sources goes on without it.
    other = FakeProvider({"https://hn.example": ("HN", "a lead")})
    acquisition = Acquisition({"searxng": searxng, "hackernews": other}, other, default="searxng")
    leads = acquisition.discover("q", max_results=5, sources=["searxng", "hackernews"])
    assert [lead.url for lead in leads] == ["https://hn.example"]


def test_a_spent_budget_skips_the_paid_fallback_search_when_search_is_free():
    from collegium.acquisition import Acquisition

    class Free(FakeProvider):
        name = "free"

    class Paid(FakeProvider):
        metered = True

    free = Free({})
    paid = Paid({"https://p.example": ("P", "paid result")})
    acquisition = Acquisition(
        {"searxng": free, "tavily": paid}, free, default="searxng", fallback_search="tavily"
    ).for_run(lambda *a: None, budget=lambda: 0)
    assert acquisition.discover("q", max_results=5) == []
    assert paid.searches == []


def test_searxng_is_asked_at_a_measured_pace(monkeypatch):
    from collegium.acquisition import searxng as module

    slept = []
    monkeypatch.setattr(module.time, "sleep", slept.append)
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
    searxng = module.SearXNGDiscovery("http://s", transport=ok, pause=3)
    searxng.discover("a", 5)
    searxng.discover("b", 5)
    assert len(slept) == 1 and 2.5 < slept[0] <= 3


def test_forums_and_linkedin_are_not_paid_for_but_forums_are_researched():
    from collegium.reliability import NOT_WORTH_PAYING, NOT_WORTH_RESEARCH, classify

    assert classify("https://www.linkedin.com/posts/someone-activity-1")[0] == "social or video"
    assert classify("https://www.reddit.com/r/x/comments/1")[0] == "forum"
    assert "forum" in NOT_WORTH_PAYING and "forum" not in NOT_WORTH_RESEARCH


def test_moltbook_reads_posts_in_full_without_a_key():
    from collegium.acquisition.moltbook import MoltbookDiscovery
    from collegium.reliability import NOT_WORTH_PAYING, classify

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"type": "comment", "id": "c1", "post_id": "p0"},
                        {"type": "post", "id": "p1", "post_id": "p1", "content": "they automa"},
                        {"type": "post", "id": "p2", "post_id": "p2"},
                        {"type": "post", "id": "p3", "post_id": "p3"},
                    ]
                },
            )
        post_id = request.url.path.rsplit("/", 1)[1]
        posts = {
            "p1": {
                "id": "p1",
                "title": "they automated the entry-level job",
                "content": "The junior role was also the training program. " * 5,
                "author": {"name": "pyclaw001"},
                "submolt": {"name": "general"},
                "upvotes": 12,
                "comment_count": 3,
                "created_at": "2026-09-20T10:00:00Z",
            },
            "p2": {"id": "p2", "title": "spam", "content": "buy", "is_spam": True},
        }
        if post_id in posts:
            return httpx.Response(200, json={"post": posts[post_id]})
        return httpx.Response(404)

    leads = MoltbookDiscovery(transport=httpx.MockTransport(handler)).discover("juniors", 5)
    assert [lead.url for lead in leads] == ["https://www.moltbook.com/post/p1"]
    lead = leads[0]
    assert lead.snippet.startswith("The junior role was also the training program.")
    assert "Moltbook post by agent pyclaw001 in m/general: 12 upvotes, 3 comments" in lead.snippet
    assert all("authorization" not in r.headers for r in seen)  # no key, ever
    assert seen[0].url.params["type"] == "posts"
    # A low-trust kind of source: capped, never paid for, never researched.
    assert classify(lead.url) == ("AI-agent forum", 0.3)
    assert "AI-agent forum" in NOT_WORTH_PAYING


def test_every_adapter_says_who_it_is():
    from urllib import robotparser

    from collegium.acquisition.feeds import FeedReader
    from collegium.acquisition.moltbook import MoltbookDiscovery
    from collegium.acquisition.searxng import SearXNGDiscovery
    from collegium.identity import USER_AGENT, user_agent
    from collegium.publisher import MoltbookClient

    # The software, and the operator only if configured: nobody running a
    # copy speaks as its authors.
    assert user_agent(contact=None) == (
        "Collegium/0.1 (+https://github.com/ketilaa/collegium; research agent)"
    )
    assert user_agent("community agent", "https://github.com/someone") == (
        "Collegium/0.1 (+https://github.com/ketilaa/collegium; community agent; "
        "operated by https://github.com/someone)"
    )
    agents = []

    def record(request):
        agents.append(request.headers["user-agent"])
        return httpx.Response(200, json={"results": [], "hits": [], "comments": []})

    t = httpx.MockTransport(record)
    SearXNGDiscovery("http://s", transport=t, pause=0).discover("q", 1)
    HackerNewsDiscovery(transport=t).discover("q", 1)
    MoltbookDiscovery(transport=t).discover("q", 1)
    TavilyProvider("key", transport=t).discover("q", 1)
    with contextlib.suppress(Exception):  # not a feed; only the request matters
        FeedReader(transport=t).crawl("https://feed.example/rss", 1)
    assert set(agents) == {USER_AGENT} and len(agents) == 5
    MoltbookClient("key", transport=t)._client.get("/x")
    assert agents[-1].startswith("Collegium/0.1 (+https://github.com/ketilaa/collegium;")

    # A site can address it by name in robots.txt.
    rules = robotparser.RobotFileParser()
    rules.parse(["User-agent: Collegium", "Disallow: /private"])
    assert not rules.can_fetch(USER_AGENT, "https://site.example/private/page")
    assert rules.can_fetch(USER_AGENT, "https://site.example/public")
