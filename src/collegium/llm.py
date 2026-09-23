"""Structured output from a language model.

Roles ask for a Pydantic model and get a validated instance back. The
default client talks to any OpenAI-compatible chat endpoint (Ollama,
llama.cpp, vLLM), which keeps the organization on local models.
"""

import json
import re
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    model: str

    def generate(self, system: str, user: str, schema: type[T]) -> T: ...


class OpenAICompatibleLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 600,
        max_attempts: int = 3,
        temperature: float = 0.2,
        client: httpx.Client | None = None,
    ):
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._max_attempts = max_attempts
        self._temperature = temperature

    def generate(self, system: str, user: str, schema: type[T]) -> T:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
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
                    "response_format": response_format,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"] or ""
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
