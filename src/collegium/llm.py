"""Structured output from a language model.

Roles ask for a Pydantic model and get a validated instance back. The
default client talks to any OpenAI-compatible chat endpoint (Ollama,
llama.cpp, vLLM), which keeps the organization on local models.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tool:
    """A function the model may call before giving its final answer. `call`
    runs it and returns the text result fed back to the model; `parameters`
    is its arguments' JSON schema."""

    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[..., str]


class LLM(Protocol):
    model: str

    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        max_tokens: int | None = None,
        enable_thinking: bool = True,
        tools: list[Tool] | None = None,
        max_tool_calls: int = 3,
    ) -> T: ...


class OpenAICompatibleLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 600,
        max_attempts: int = 3,
        # Qwen3's own card: thinking mode wants ~0.6, and warns that greedy
        # (near-0) decoding degrades it into repetition, not better answers.
        temperature: float = 0.6,
        max_tokens: int = 2048,
        client: httpx.Client | None = None,
    ):
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._max_attempts = max_attempts
        self._temperature = temperature
        # A bound on each reply. Structured replies need well under this; a
        # model stuck repeating itself otherwise generates until the context
        # is full, which on a busy machine can take an hour.
        self._max_tokens = max_tokens

    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        max_tokens: int | None = None,
        enable_thinking: bool = True,
        tools: list[Tool] | None = None,
        max_tool_calls: int = 3,
    ) -> T:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        budget = self._max_tokens if max_tokens is None else max_tokens
        # Only sent when a caller opts out: omitting it leaves a model that
        # does not support thinking mode unaffected.
        thinking = {} if enable_thinking else {"chat_template_kwargs": {"enable_thinking": False}}
        if tools:
            # A tool call and the final structured answer are never asked
            # for in the same request: combined, the model has been seen to
            # skip the tool and answer the schema directly from nothing,
            # fabricating sources (a known rough edge in llama.cpp's server,
            # undocumented either way). So the tool round runs with no
            # response_format at all, and only once it is done does the
            # existing schema-constrained call below run, on the messages
            # the tool round leaves behind.
            messages = self._run_tools(messages, tools, max_tool_calls, budget, thinking)
            messages.append(
                {
                    "role": "user",
                    "content": "Reply now with a single JSON object matching the schema, "
                    "and nothing else.",
                }
            )
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        }
        error: Exception | None = None
        for _ in range(self._max_attempts):
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self._temperature,
                    "max_tokens": budget,
                    "response_format": response_format,
                    **thinking,
                },
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            content = choice["message"]["content"] or ""
            if choice.get("finish_reason") == "length":
                # Do not feed a runaway reply back; ask again, more briefly.
                error = LLMError(f"reply cut off at {budget} tokens")
                messages = messages[:2] + [
                    {
                        "role": "user",
                        "content": "Your previous reply was cut off because it was too long. "
                        "Reply with a shorter JSON object: fewer items, briefer text.",
                    }
                ]
                continue
            try:
                return schema.model_validate_json(extract_json(content))
            except (ValidationError, ValueError) as e:
                error = e
                messages += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"That reply was not valid: {e}\n"
                        "Reply again with only a JSON object that matches the schema.",
                    },
                ]
        raise LLMError(f"no valid {schema.__name__} after {self._max_attempts} attempts: {error}")

    def _run_tools(
        self,
        messages: list[dict],
        tools: list[Tool],
        max_tool_calls: int,
        budget: int,
        thinking: dict,
    ) -> list[dict]:
        """Lets the model call tools, each turn feeding the result back as
        its own message, until it stops asking for one or the limit is
        reached. Returns the messages so far; the caller asks for the final
        answer separately (see `generate`)."""
        by_name = {tool.name: tool for tool in tools}
        specs = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]
        for _ in range(max_tool_calls):
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self._temperature,
                    "max_tokens": budget,
                    "tools": specs,
                    "tool_choice": "auto",
                    **thinking,
                },
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            calls = message.get("tool_calls")
            if not calls:
                if message.get("content"):
                    messages.append({"role": "assistant", "content": message["content"]})
                break
            messages.append(
                {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
            )
            for call in calls:
                name = call.get("function", {}).get("name")
                tool = by_name.get(name)
                if tool is None:
                    result = f"Unknown tool {name!r}."
                else:
                    try:
                        args = json.loads(call["function"]["arguments"])
                        result = tool.call(**args)
                    except Exception as e:
                        result = f"The tool failed: {e}"
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
        return messages


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def extract_json(text: str) -> str:
    """The JSON object in a reply, without reasoning blocks or code fences."""
    text = _THINK.sub("", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("reply contains no JSON object")
    candidate = text[start : end + 1]
    json.loads(candidate)
    return candidate
