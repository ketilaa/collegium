"""The HTTP adapters, against mock transports."""

import json

import httpx
import pytest

from collegium.acquisition.tavily import TavilyProvider
from collegium.llm import LLMError, OpenAICompatibleLLM
from collegium.roles.base import SearchPlan


def _chat_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


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
    [result] = provider.search("ai pricing", 2)
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

    TavilyProvider("key", transport=httpx.MockTransport(handler)).search("q", 3, recent_days=30)
    assert requests[0]["topic"] == "news" and requests[0]["days"] == 30
