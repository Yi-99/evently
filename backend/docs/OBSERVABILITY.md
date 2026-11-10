# Observability: OpenTelemetry → Dash0

*What the backend emits, how it gets to Dash0, where the hooks are, and which dashboards and alerts
to build. Dash0 specifics were checked against dash0.com docs on 2026-11-09; items we could not
confirm are marked "unverified".*

## 1. Turn it on

| Setting (env) | Meaning | Example |
|---|---|---|
| `OTEL_ENABLED` | master switch; `false` = zero overhead, the SDK is never imported | `true` |
| `OTEL_SERVICE_NAME` | `service.name` resource attribute | `evently-api` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/**gRPC** endpoint (`https://` = TLS, `http://` = plaintext) | `https://ingress.eu-west-1.aws.dash0.com:4317` or `http://otel-collector:4317` |
| `OTEL_EXPORTER_OTLP_HEADERS` | comma-separated `k=v`; Dash0 needs `Authorization=Bearer <token>` | `Authorization=Bearer auth_…` |
| `DASH0_DATASET` | added as the `Dash0-Dataset` header unless the headers already contain one | `dev`, `default` |
| `ENVIRONMENT` | becomes `deployment.environment` (+ `deployment.environment.name`) | `production` |
| `OTEL_RESOURCE_ATTRIBUTES` | merged in by the SDK (k8s metadata from the downward API in `deploy/k8s`) | `k8s.pod.name=…` |
| `OTEL_TRACES_SAMPLER` / `_ARG` | standard SDK sampler env; default parent-based always-on | `parentbased_traceidratio` / `0.25` |
| `OTEL_METRIC_EXPORT_INTERVAL` | ms between metric exports (default 60000) | `30000` |

Dash0 facts verified: the Python getting-started/configuration pages use
`OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $DASH0_AUTH_TOKEN,Dash0-Dataset=default"`; the
ingress endpoint "always starts with `ingress.` and ends in `dash0.com:4317`" (gRPC) — the
OTLP/HTTP variant is the same host on 4318 (`/v1/traces` etc. are appended by the exporters; do not
include them). Dash0's own Python distribution is `pip install dash0-opentelemetry` + run under
`opentelemetry-instrument`; it defaults to OTLP/HTTP and reads `DASH0_OTEL_COLLECTOR_BASE_URL`. We do
**not** use the distro: `app/observability/otel.py` configures the vanilla SDK with the gRPC exporter
because we want custom spans/metrics and an explicit shutdown flush. Switching is a matter of
installing the distro and setting `OTEL_ENABLED=false` (the distro then auto-instruments FastAPI and
httpx itself but you lose `agent.run`/`llm.request` spans and the `evently.*` metrics).

Routing options:

- **Direct to Dash0** (Kubernetes default in `deploy/k8s/base/configmap.yaml`): endpoint = Dash0
  ingress, token in the Secret.
- **Through a collector** (docker-compose default): endpoint = `http://otel-collector:4317`, no
  headers in the app; `deploy/otel/collector-config.yaml` adds the `Authorization` and
  `Dash0-Dataset` headers from `DASH0_*` env vars, batches, and prints a summary (`debug` exporter).
- **Through the Dash0 operator's collector**: the operator injects `OTEL_EXPORTER_OTLP_ENDPOINT` only
  into containers that do not set it. We set it, so the operator leaves it and records a `Warning`
  event on the pod (documented behaviour). If you prefer the operator's node-local collector, delete
  `OTEL_EXPORTER_OTLP_ENDPOINT` from the ConfigMap and check the injected value with
  `kubectl -n evently exec deploy/evently-api -- env | grep OTEL_` — note our exporter is gRPC, so the
  injected endpoint must be the collector's gRPC port (unverified which protocol the operator injects).

## 2. What `instrument_app()` does

`app/main.py` calls `app.observability.otel.instrument_app(app, settings)` when `OTEL_ENABLED` is
true. It:

1. builds the `Resource` (`service.name`, `service.namespace=evently`, `service.version` from the
   installed package, `deployment.environment[.name]`) and merges `OTEL_RESOURCE_ATTRIBUTES`;
2. installs a `TracerProvider` (`BatchSpanProcessor` → `OTLPSpanExporter`) and a `MeterProvider`
   (`PeriodicExportingMetricReader` → `OTLPMetricExporter`), both gRPC, both with the parsed
   headers (keys lowercased: gRPC metadata requires it);
3. auto-instruments **FastAPI** (`/api/v1/health` excluded; ASGI `receive`/`send` sub-spans
   disabled — they are noise on WebSockets), **httpx** (every outbound call: Claude via the
   Anthropic SDK, Google Places/Routes, Open-Meteo, Resend, Luma — all become `CLIENT` spans), and
   **SQLAlchemy** once the engine exists (the engine is created inside the app lifespan, so the
   instrumentor is attached by wrapping the lifespan; version check skipped because the project runs
   SQLAlchemy 2.1 and the instrumentation only uses stable engine events);
4. wraps the lifespan so that after `close_context()` both providers are flushed and shut down
   (`otel.shutdown()`); `otel.force_flush()` is available for short-lived workers.

Calling `instrument_app` twice in one process (tests, factories) only instruments the new app.

## 3. Spans

Instrumentation scope `evently`. Attribute values that are `None` are dropped.

### `agent.run` — `app/agents/runtime.py::AgentRuntime._execute`

One span per agent run, **current** for the whole run (so every Claude call, provider HTTP call and
DB query inside it is a child; child runs spawned with `spawn_child()` nest because asyncio tasks
inherit the context at creation). Attributes:

| Attribute | Value |
|---|---|
| `agent.kind` | `orchestrator`, `venue_scout`, `venue_research`, `slide_render`, … |
| `agent.run_id`, `agent.title`, `agent.limited` | run row id, title, whether a semaphore slot was taken |
| `event_id`, `card_id`, `parent_run_id` | ids (card_id is re-set at the end in case the agent attached a card) |
| `agent.status` | final status: `succeeded` / `failed` / `cancelled` |

Failures record the exception and set span status `ERROR`; cancellations stay `UNSET` (they are
normal). The span name is constant on purpose — group by `agent.kind` in Dash0.

### `llm.request` — `app/llm/telemetry.py`, used by `anthropic_client.py` and `openai_client.py` (`run()` per streamed turn, `parse()` once)

GenAI semantic-convention names (both the older `gen_ai.system` and the current
`gen_ai.provider.name` are set):

| Attribute | Value |
|---|---|
| `gen_ai.system` / `gen_ai.provider.name` | `anthropic` or `openai` |
| `gen_ai.operation.name` | `chat` |
| `gen_ai.request.model`, `gen_ai.request.max_tokens` | requested model / limit |
| `gen_ai.response.model`, `gen_ai.response.id` | what the API answered with |
| `gen_ai.response.finish_reasons` | `["end_turn"]`, `["tool_use"]`, `["max_tokens"]`, `["refusal"]`, `["pause_turn"]` (OpenAI statuses are normalised to these) |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | per response |
| `gen_ai.usage.cache_read_input_tokens`, `gen_ai.usage.cache_creation_input_tokens` | prompt-cache usage (Anthropic field names; OpenAI `cached_tokens` / `cache_write_tokens` map onto them, and `input_tokens` excludes both) |
| `evently.llm.purpose`, `evently.llm.tier`, `evently.llm.structured_output` | the agent's `purpose` string, `orchestrator`/`worker`, `parse()` vs `run()` |

API errors are recorded and set status `ERROR`. Prompts and completions are **not** attached
(privacy and size); the httpx child span carries the HTTP status and latency.

### Auto spans

`GET /api/v1/events/{event_id}` style server spans (FastAPI, route templates as names),
`POST api.anthropic.com` etc. (httpx), `SELECT evently` (SQLAlchemy, statement in `db.statement`).

## 4. Metrics (`app/observability/metrics.py`)

All helpers are cheap no-ops until `instrument_app()` calls `metrics.enable()`; they never raise.
Attribute sets are low-cardinality by design (no run/session/event ids).

| Instrument | Type | Attributes | Helper | Who calls it (file to edit) |
|---|---|---|---|---|
| `evently.agent_runs.active` | UpDownCounter | `agent.kind` | `agent_run_started` / `agent_run_finished` | **done** — `app/agents/runtime.py` |
| `evently.agent_runs.finished` | Counter | `agent.kind`, `agent.status` | `agent_run_finished` | **done** — runtime |
| `evently.llm.requests` | Counter | `gen_ai.request.model`, `gen_ai.response.finish_reason` | `llm_request` | **done** — `app/llm/telemetry.py` (both providers) |
| `evently.llm.tokens` | Counter | `gen_ai.request.model`, `gen_ai.token.type` (`input`/`output`/`cache_read`/`cache_creation`) | `llm_tokens(model, usage)` | **done** — `app/llm/telemetry.py` (both providers) |
| `evently.audio.frames` | Counter | `audio.codec`, `audio.sample_rate` | `audio_frames(codec, sample_rate)` | Package 14: `app/live/audio_hub.py` `AudioSource.publish()` after `self.frames_in += 1` |
| `evently.audio.frames_dropped` | Counter | – | `audio_frames_dropped(n)` | Package 14: `BoundedAudioQueue.push()` each time `_drop_oldest_audio()` succeeds (or `AudioHub.remove_listener()` with `listener.dropped` if you prefer one call per listener) |
| `evently.audio.listeners` | UpDownCounter | – | `audio_listener_joined` / `audio_listener_left` | Package 14: `AudioHub.add_listener()` / `remove_listener()` |
| `evently.stt.latency` | Histogram (s) | `stt.provider`, `stt.final` | `stt_latency(provider, seconds, final=…)` | Packages 10/11/14: `app/live/stt_pipeline.py` when a transcript segment arrives — `seconds = now − wall-clock time of the last audio frame the segment covers` (use the speaker frame `timestamp_ms` mapped to the server clock at stream start) |
| `evently.slide.decision_latency` | Histogram (s) | `slide.outcome` (`changed`/`no_match`/`ignored`) | `slide_decision_latency(seconds, outcome)` | Package 12: `app/live/slide_tracker.py`, around the match of one transcript against triggers |
| `evently.slide.changes` | Counter | `slide.trigger` (`voice_command`/`keyword`/`script_alignment`/`manual`) | `slide_changed(trigger)` | Package 12/13: where `slide.changed` is published |

Usage pattern in any module: `from app.observability import metrics` then call the helper; no
try/except, no feature flag needed.

## 5. Trace ids on realtime envelopes

The lead is adding an optional `trace_id` field to `Envelope` (`app/realtime/events.py`) which the
buses fill from the current span at `publish()` time. The tracer setup supports that because every
span here is made **current** via `trace.use_span()`: FastAPI request spans (auto), `agent.run` and
`llm.request` all install their context in the contextvar, and `ctx.bus.publish()` is called from
inside those contexts (REST handlers, `RunContext.step()`, `_finish()`, board service calls). Use

```python
from app.observability import tracing
tracing.current_trace_id()      # "4bf92f3577b34da6a3ce929d0e0e4736" or None
tracing.current_traceparent()   # "00-<trace_id>-<span_id>-01" or None
```

Both are safe when OTel is off or no span is active (they return `None`, never raise). Caveats:

- Work started with `asyncio.create_task()` *copies* the context at creation: the agent runtime's
  tasks therefore inherit the request span that spawned them (good), while fire-and-forget
  debounce tasks (e.g. `AudioHub._publish_state_later`) run outside any span → `trace_id` is `None`.
- The Redis bus delivers envelopes to subscribers on a reader task; `trace_id` must be captured on
  the publishing side (it is part of the JSON), not re-derived on delivery.

**Frontend use.** The browser receives the `trace_id` in every envelope. (a) Show it in the dev
console next to the event type, with a link
`https://app.dash0.com/tracing?trace_id=<id>` (the exact deep-link format should be copied from a
Dash0 trace page — unverified) so a planner clicking a stuck card lands on the agent's trace.
(b) Correlate client-side logs: log `trace_id` with any UI error raised while handling the envelope.
(c) Continue the trace from the browser: when the user re-prompts a card (`POST /cards/{id}/prompt`)
send the envelope's trace context as a `traceparent` header
(`00-<trace_id>-<16 hex span id>-01`); the FastAPI instrumentation extracts it and the new
`agent.run` becomes part of the same trace. Treat ids as opaque; do not build on their format.

## 6. Dash0 dashboards and alerts

Suggested panels (PromQL-style over Dash0 metrics; names as exported, Dash0 maps dots to the same
names in its query UI):

**Agents**
- Active runs by kind: `sum by (agent.kind) (evently.agent_runs.active)`
- Failure rate (5 min): `sum(rate(evently.agent_runs.finished{agent.status="failed"}[5m])) / sum(rate(evently.agent_runs.finished[5m]))`
- Run duration p50/p95: from `agent.run` spans, grouped by `agent.kind` (span-based panel).
- **Alert:** failure rate > 20 % for 10 min with at least 5 finished runs → warn; > 50 % → page.
- **Alert:** `evently.agent_runs.active` flat at `MAX_PARALLEL_AGENTS` for 15 min → runs are
  queueing (raise the limit or scale up).

**LLM cost**
- Tokens per hour by type: `sum by (gen_ai.token.type) (increase(evently.llm.tokens[1h]))`
- Cost estimate: `sum by (gen_ai.request.model) (increase(evently.llm.tokens{gen_ai.token.type="input"}[1d])) * <input $/token> + … output … + cache_read * <cache $/token>` (fill in the current price list per model).
- Cache hit ratio: `cache_read / (cache_read + input)` — should be high for the orchestrator
  (system prompt cached); a drop means the prompt prefix changed.
- Refusals / truncations: `evently.llm.requests{gen_ai.response.finish_reason=~"refusal|max_tokens"}`.
- **Alert:** daily token spend > budget (e.g. 50 M tokens) → warn; `max_tokens` finish reasons
  > 5 % of requests → slide/outline prompts need bigger limits.

**Audio**
- Drop rate: `sum(rate(evently.audio.frames_dropped[1m])) / sum(rate(evently.audio.frames[1m]))`
  (per frame relayed; a frame fanned out to N listeners counts once in `frames`, so compare with
  `evently.audio.listeners` to read it per listener).
- Listeners: `sum(evently.audio.listeners)`; frames/s should be ~50 at 20 ms frames per active speaker.
- **Alert:** drop rate > 2 % for 2 min while listeners > 0 → listeners' downlink or the pod is
  saturated; check pod CPU and the hub's `max_buffered_ms` counter (`AudioHub.stats()`, surfaced by the live-session API).

**Live transcription + slides**
- `evently.stt.latency` p95 by provider; **alert** > 2 s for 5 min (slide changes will feel late).
- `evently.slide.decision_latency` p95; **alert** > 300 ms (the matcher is CPU-bound and shares the
  pod with everything else).
- `evently.slide.changes` by trigger, to see whether voice commands or keywords drive sessions.

**Service health** (from auto-instrumentation)
- HTTP p95 by route, 5xx rate; WebSocket connection count can be derived from the ASGI span count
  for `/ws/*` routes.
- SQLAlchemy span duration p95 (slow queries once Postgres is remote).
- **Alert:** 5xx rate > 2 % for 5 min; httpx `api.anthropic.com` error rate > 10 % (Claude outage or
  rate limiting → the API returns 429/503 to users).

## 7. Operator auto-instrumentation vs. our SDK

With `operator.instrumentation.enablePythonAutoInstrumentation=true`, the Dash0 operator injects
its Python agent into pods in monitored namespaces. Its docs state that when it detects an existing
OpenTelemetry SDK it "deactivates itself safely" and logs a warning, so running both is harmless —
but the operator's instrumentation would not know about `agent.run`/`llm.request` or the
`evently.*` metrics, so keep `OTEL_ENABLED=true`. Everything else the operator does (k8s resource
attributes, pod logs, kubelet metrics, dashboards synced from `Dash0Monitoring`) complements our
SDK. Workloads labelled `dash0.com/enable: "false"` are skipped entirely.

## 8. Local verification

```sh
# 1. No OTel: must boot with zero imports of opentelemetry.*
.venv/bin/python -c "from app.main import create_app; create_app()"

# 2. OTel on, nothing listening: boots, logs "OpenTelemetry enabled", exporter retries then gives up
OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  .venv/bin/python -c "from app.main import create_app; create_app()"

# 3. With compose: watch the collector print batches
docker compose up otel-collector api
docker compose logs -f otel-collector | grep -E "Traces|Metrics"
```

Checked locally (2026-11-09): both boots; `agent.run` spans with status/attributes for succeeded,
failed and cancelled runs; `llm.request` spans with usage for `run()`, `parse()` and an API error
(stubbed Anthropic client); SQLAlchemy spans appear and `/api/v1/health` is excluded. Not checked:
end-to-end delivery into a Dash0 organisation (no token here) and the operator interaction.
