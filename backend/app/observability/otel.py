"""OpenTelemetry setup (traces + metrics) exported over OTLP/gRPC, e.g. to Dash0.

Wired from `create_app()` only when `settings.otel_enabled` is true, so the `otel`
extra is optional. What it does:

* Builds a `Resource` with `service.name`, `service.version`, `deployment.environment`
  (and `deployment.environment.name`, the current semconv spelling). Anything in
  `OTEL_RESOURCE_ATTRIBUTES` (k8s.pod.name… from the Deployment) is merged in by the SDK.
* Installs a `TracerProvider` + `MeterProvider` with OTLP gRPC exporters pointed at
  `settings.otel_exporter_otlp_endpoint`. Headers come from `settings.otel_exporter_otlp_headers`
  (`Authorization=Bearer <token>` for Dash0) plus `Dash0-Dataset` from `settings.dash0_dataset`.
* Auto-instruments FastAPI (health checks excluded), httpx (so Claude / OpenAI / Google / STT calls
  show up as client spans) and the SQLAlchemy engine once the app context exists.
* Flushes and shuts the providers down after the app's own lifespan has finished.

Custom spans and metrics live in `app.observability.tracing` / `.metrics`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib import metadata as importlib_metadata
from typing import Any

from fastapi import FastAPI

from app.core.config import Settings
from app.observability import metrics, tracing

log = logging.getLogger(__name__)

HEALTH_PATH_PATTERN = "/api/v1/health"  # regex fragment; `excluded_urls` is comma-separated
DASH0_DATASET_HEADER = "dash0-dataset"  # gRPC metadata keys are lowercase

_state: dict[str, Any] = {}  # tracer_provider / meter_provider, for shutdown and idempotence


def parse_headers(raw: str | None, dataset: str | None = None) -> dict[str, str]:
    """`Authorization=Bearer x,Dash0-Dataset=y` → lowercase-keyed dict, adding the dataset header.

    Values may be percent-encoded as the OTLP spec allows; keys are lowercased because gRPC
    metadata requires it. An explicit `Dash0-Dataset` header wins over `settings.dash0_dataset`.
    """
    headers: dict[str, str] = {}
    if raw:
        from opentelemetry.util.re import parse_env_headers

        headers.update(parse_env_headers(raw, liberal=True))
    if dataset and DASH0_DATASET_HEADER not in headers:
        headers[DASH0_DATASET_HEADER] = dataset
    return headers


def service_version() -> str:
    try:
        return importlib_metadata.version("evently")
    except importlib_metadata.PackageNotFoundError:
        return "0.0.0+local"


def build_resource(settings: Settings) -> Any:
    from opentelemetry.sdk.resources import Resource

    # `Resource.create` also runs the env detector, so OTEL_RESOURCE_ATTRIBUTES is merged in.
    return Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.namespace": "evently",
            "service.version": service_version(),
            "deployment.environment": settings.environment,
            "deployment.environment.name": settings.environment,
        }
    )


def instrument_app(app: FastAPI, settings: Settings) -> None:
    """Configure the OTel SDK and auto-instrument FastAPI, SQLAlchemy and httpx. No-op when disabled."""
    if not settings.otel_enabled:
        return
    if not settings.otel_exporter_otlp_endpoint:
        log.warning("OTEL_ENABLED is true but OTEL_EXPORTER_OTLP_ENDPOINT is empty; telemetry stays off")
        return
    if _state:
        log.debug("OpenTelemetry already configured in this process; instrumenting the new app only")
        _instrument_fastapi(app)
        return

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry import trace
        if settings.otel_exporter_otlp_protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover - depends on the environment
        log.error("OTEL_ENABLED is true but the `otel` extra is not installed (%s); telemetry stays off", exc)
        return

    endpoint = settings.otel_exporter_otlp_endpoint
    # OTLP/HTTP exporters use an explicit `endpoint=` verbatim, so the signal paths are added here.
    # (Useful when outbound 4317 is blocked: Dash0 accepts OTLP/HTTP on 443 but not gRPC.)
    http = settings.otel_exporter_otlp_protocol == "http/protobuf"
    traces_endpoint = f"{endpoint.rstrip('/')}/v1/traces" if http else endpoint
    metrics_endpoint = f"{endpoint.rstrip('/')}/v1/metrics" if http else endpoint
    headers = parse_headers(settings.otel_exporter_otlp_headers, settings.dash0_dataset)
    resource = build_resource(settings)

    # Traces. Sampler honours OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG (default parentbased_always_on).
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint, headers=headers)))
    trace.set_tracer_provider(tracer_provider)

    # Metrics. Export interval honours OTEL_METRIC_EXPORT_INTERVAL (default 60 s).
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=metrics_endpoint, headers=headers))
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
    otel_metrics.set_meter_provider(meter_provider)

    _state["tracer_provider"] = tracer_provider
    _state["meter_provider"] = meter_provider
    tracing.enable()
    metrics.enable(meter_provider)

    _instrument_httpx(tracer_provider, meter_provider)
    _instrument_fastapi(app, tracer_provider, meter_provider)
    _wrap_lifespan(app, tracer_provider, meter_provider)

    log.info(
        "OpenTelemetry enabled: service=%s version=%s env=%s endpoint=%s dataset=%s",
        settings.otel_service_name,
        resource.attributes.get("service.version"),
        settings.environment,
        endpoint,
        headers.get(DASH0_DATASET_HEADER, "-"),
    )


# -- instrumentations ---------------------------------------------------------


def _instrument_fastapi(app: FastAPI, tracer_provider: Any = None, meter_provider: Any = None) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        excluded_urls=HEALTH_PATH_PATTERN,
        # One span per request is plenty; the ASGI receive/send sub-spans are noise for WebSockets.
        exclude_spans=["receive", "send"],
    )


def _instrument_httpx(tracer_provider: Any, meter_provider: Any) -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    instrumentor = HTTPXClientInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(tracer_provider=tracer_provider, meter_provider=meter_provider)


def _instrument_sqlalchemy(engine: Any, tracer_provider: Any) -> None:
    """Instrument the app's engine. The engine is built inside the lifespan, hence the late call."""
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    sync_engine = getattr(engine, "sync_engine", engine)
    # The instrumentation declares `sqlalchemy < 2.1` while the project runs 2.1; it only relies on the
    # stable engine events (before/after_cursor_execute, handle_error), so the version check is skipped.
    SQLAlchemyInstrumentor().instrument(
        engine=sync_engine, tracer_provider=tracer_provider, enable_commenter=False, skip_dep_check=True
    )


def _wrap_lifespan(app: FastAPI, tracer_provider: Any, meter_provider: Any) -> None:
    """Run the app's own lifespan, instrument the DB engine it created, flush telemetry at exit."""
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        async with inner(app_) as state:
            ctx = getattr(app_.state, "ctx", None)
            if ctx is not None and getattr(ctx, "engine", None) is not None:
                try:
                    _instrument_sqlalchemy(ctx.engine, tracer_provider)
                except Exception:  # noqa: BLE001 - never block startup on telemetry
                    log.exception("SQLAlchemy instrumentation failed")
            try:
                yield state
            finally:
                shutdown()

    app.router.lifespan_context = lifespan


# -- lifecycle ----------------------------------------------------------------


def force_flush(timeout_millis: int = 5000) -> None:
    """Push buffered spans/metrics now (e.g. before a Job pod exits)."""
    for key in ("tracer_provider", "meter_provider"):
        provider = _state.get(key)
        if provider is not None:
            try:
                provider.force_flush(timeout_millis)
            except Exception:  # noqa: BLE001
                log.exception("%s force_flush failed", key)


def shutdown() -> None:
    """Flush and shut down both providers; idempotent."""
    tracer_provider = _state.pop("tracer_provider", None)
    meter_provider = _state.pop("meter_provider", None)
    for name, provider in (("tracer_provider", tracer_provider), ("meter_provider", meter_provider)):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001 - shutdown must continue
            log.exception("%s shutdown failed", name)
    tracing.disable()
    metrics.disable()
