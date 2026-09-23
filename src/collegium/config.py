"""Settings, read from the environment."""

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


@dataclass(frozen=True)
class Settings:
    # Worker and scheduler connect as collegium_worker_app; owner-facing
    # commands (adding domains, requesting work) as collegium_board_app.
    worker_database_url: str | None = field(
        default_factory=lambda: _env("COLLEGIUM_WORKER_DATABASE_URL")
    )
    board_database_url: str | None = field(
        default_factory=lambda: _env("COLLEGIUM_BOARD_DATABASE_URL")
    )

    # Any OpenAI-compatible chat endpoint: Ollama, llama.cpp, vLLM.
    llm_base_url: str = field(
        default_factory=lambda: _env("COLLEGIUM_LLM_BASE_URL", "http://localhost:11434/v1")
    )
    llm_model: str = field(default_factory=lambda: _env("COLLEGIUM_LLM_MODEL", "qwen3:14b"))
    llm_api_key: str | None = field(default_factory=lambda: _env("COLLEGIUM_LLM_API_KEY"))
    llm_timeout_seconds: float = field(
        default_factory=lambda: float(_env("COLLEGIUM_LLM_TIMEOUT_SECONDS", "600"))
    )

    search_provider: str = field(
        default_factory=lambda: _env("COLLEGIUM_SEARCH_PROVIDER", "tavily")
    )
    tavily_api_key: str | None = field(default_factory=lambda: _env("TAVILY_API_KEY"))

    # Limits that keep prompts within a local model's context window.
    max_search_results: int = field(
        default_factory=lambda: int(_env("COLLEGIUM_MAX_SEARCH_RESULTS", "5"))
    )
    max_documents: int = field(default_factory=lambda: int(_env("COLLEGIUM_MAX_DOCUMENTS", "3")))
    max_document_chars: int = field(
        default_factory=lambda: int(_env("COLLEGIUM_MAX_DOCUMENT_CHARS", "4000"))
    )

    # The Scout looks for news from this many days; older results are dropped.
    scout_recent_days: int = field(
        default_factory=lambda: int(_env("COLLEGIUM_SCOUT_RECENT_DAYS", "30"))
    )

    scout_interval_hours: float = field(
        default_factory=lambda: float(_env("COLLEGIUM_SCOUT_INTERVAL_HOURS", "24"))
    )


def require(value: str | None, name: str) -> str:
    if not value:
        raise SystemExit(f"{name} is not set")
    return value
