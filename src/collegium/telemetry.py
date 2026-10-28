"""Wires up OTel traces, metrics and logs for one process. Called once,
at the very start of each long-running component (`collegium worker`,
`scheduler`, `publisher`, `web`), replacing the plain
`logging.basicConfig()` every command used before.

All three signal types go to the OTel Collector over OTLP; collegium
never talks Prometheus/Loki/Tempo protocols directly, so there is
exactly one place (the collector, run as shared infrastructure, not
part of this project) that fans out to the three backends. Where that
collector is, and everything else about the shared stack, is a
deployment decision outside this repo; collegium only reads the
standard `OTEL_*` environment variables below.

`OTEL_SDK_DISABLED=true` is read twice over, not just by this module:
the OTel SDK itself (`opentelemetry.sdk.trace.TracerProvider`) checks
the same standard variable and, when it is literally "true", makes
every span it creates invalid -- not merely unexported -- regardless of
what provider this module constructs. So disabled means more than "not
sent anywhere": logs still go to stdout as JSON, but with no trace_id/
span_id either, since there is no real span to read one from. Tests
set this (see conftest.py) precisely for that stronger guarantee --
nothing span-shaped happens at all, not just nothing exported -- and
never call `setup_telemetry` either, so instrumented code runs
unchanged against the OTel API's own default (also no-op) providers
with nothing to configure."""

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from opentelemetry import _logs as logs_api
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.metrics import Counter, Histogram, Meter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialized = False


class JsonFormatter(logging.Formatter):
    """So `docker compose logs` stays useful with no collector reachable
    at all. Each record carries the OTel trace/span ids when a span is
    active, so a log line found in Loki and the same line found in
    `docker compose logs` carry the same correlation id -- without
    touching the many existing `log.info(...)` call sites, which only
    need a span active around them, not a trace id threaded through."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        payload = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def flush() -> None:
    """Force-exports any buffered spans/metrics/logs. `worker --drain`
    and `scheduler --once` are short-lived; without this, the batch
    processors' own background interval may not have fired before the
    process exits, and nothing shows up. Reads the global providers
    `setup_telemetry` set, so this can be called from anywhere, not
    just where that call's own return value is in scope."""
    trace.get_tracer_provider().force_flush()
    metrics.get_meter_provider().force_flush()
    provider = logs_api.get_logger_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()


@dataclass(frozen=True)
class Telemetry:
    tracer: trace.Tracer
    meter: metrics.Meter


def _disabled() -> bool:
    # Opt-in, not opt-out: collegium bundles no collector of its own
    # (unlike some reference deployments that run one alongside the
    # app), so defaulting to enabled would make a plain `docker compose
    # up`, or a bare `collegium worker` outside Docker entirely, spend
    # every job retrying a host that was never configured to exist.
    return os.environ.get("OTEL_SDK_DISABLED", "true").lower() != "false"


def setup_telemetry(service_name: str) -> Telemetry:
    global _initialized

    resource = Resource.create({"service.name": service_name})

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(JsonFormatter())
    root.addHandler(stdout_handler)

    if _disabled():
        tracer_provider = TracerProvider(resource=resource)
        meter_provider = MeterProvider(resource=resource)
    else:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint), export_interval_millis=15_000
                )
            ],
        )

        logger_provider = LoggerProvider(resource=resource)
        log_endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://otel-collector:4318/v1/logs"
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=log_endpoint))
        )
        otlp_handler = LoggingHandler(logger_provider=logger_provider)
        root.addHandler(otlp_handler)
        logs_api.set_logger_provider(logger_provider)

        if not _initialized:
            HTTPXClientInstrumentor().instrument()
            PsycopgInstrumentor().instrument()
            _initialized = True

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)

    return Telemetry(
        tracer=trace.get_tracer(service_name),
        meter=metrics.get_meter(service_name),
    )


# ---------------------------------------------------------------------------
# Metric instruments: one place to check when a dashboard panel's query
# doesn't match what the code actually emits. OTel instrument names are
# dotted (`collegium.job`); after the collector's export they land in
# Prometheus with underscores and a unit/type suffix
# (`collegium_job_total`) -- verify the exact translated name in
# Prometheus itself before trusting a comment here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerInstruments:
    job_total: Counter  # -> collegium_job_total{kind,outcome}
    job_duration_seconds: Histogram  # -> collegium_job_duration_seconds{kind,outcome}


def build_worker_instruments(meter: Meter) -> WorkerInstruments:
    return WorkerInstruments(
        job_total=meter.create_counter(
            "collegium.job", unit="{job}", description="Worker job attempts by outcome"
        ),
        job_duration_seconds=meter.create_histogram(
            "collegium.job.duration", unit="s", description="Worker job latency"
        ),
    )


@dataclass(frozen=True)
class PublisherInstruments:
    publish_total: Counter  # -> collegium_publish_total{kind,outcome}


def build_publisher_instruments(meter: Meter) -> PublisherInstruments:
    return PublisherInstruments(
        publish_total=meter.create_counter(
            "collegium.publish", unit="{attempt}", description="Publish attempts by outcome"
        )
    )


@dataclass(frozen=True)
class SchedulerInstruments:
    planned_total: Counter  # -> collegium_planned_total


def build_scheduler_instruments(meter: Meter) -> SchedulerInstruments:
    return SchedulerInstruments(
        planned_total=meter.create_counter(
            "collegium.planned", unit="{domain}", description="Domains queued for planning"
        )
    )
