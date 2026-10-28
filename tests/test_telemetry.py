"""The telemetry module's own logic: the JSON formatter and the
disabled-by-default switch. Not `setup_telemetry` itself end to end --
every test already runs with `OTEL_SDK_DISABLED=true` (see conftest.py),
so real export is never exercised here; that only happens against a
real collector."""

import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from collegium.telemetry import JsonFormatter, _disabled


def _record(logger_name="test", message="hello", exc_info=None):
    return logging.LogRecord(
        name=logger_name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


def test_json_formatter_has_no_trace_fields_with_no_active_span():
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["message"] == "hello"
    assert payload["logger"] == "test"
    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_json_formatter_carries_trace_context_from_an_active_span(monkeypatch):
    """A provider instance used directly, not registered globally via
    `trace.set_tracer_provider` (which only ever takes effect on its
    first call in a process -- every later call, anywhere, is silently
    ignored) -- so this stays correct regardless of what some other
    test, run earlier in the same process, already registered.

    `OTEL_SDK_DISABLED` matters here even though nothing is exported:
    the OTel SDK itself (not just this module) reads it at
    `TracerProvider()` construction time and, left at the conftest.py
    default (true, for test isolation), makes every span it creates
    invalid -- not just unexported. This test is the one place that
    must turn it back off, to prove the formatter itself still does its
    job whenever a real span actually is active."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    tracer = TracerProvider().get_tracer("test_telemetry")
    with tracer.start_as_current_span("a-span"):
        payload = json.loads(JsonFormatter().format(_record()))
    assert len(payload["trace_id"]) == 32
    assert len(payload["span_id"]) == 16


def test_disabled_by_default(monkeypatch):
    """collegium bundles no collector of its own, unlike some reference
    deployments: a plain `docker compose up`, or a bare CLI invocation,
    with no OTEL_SDK_DISABLED set at all must not try to export."""
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    assert _disabled() is True


def test_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    assert _disabled() is False


def test_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert _disabled() is True
