# Deploying Evently

*How to run the backend in a container, locally with Docker Compose, and in Kubernetes with
kustomize. Written for an engineer doing this for the first time; facts about third-party tools were
checked against their docs on 2026-11-09 and are marked "unverified" where we could not confirm them.*

Files: `Taskfile.yml`, `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `deploy/otel/collector-config.yaml`,
`deploy/k8s/**`, `.github/workflows/ci.yml`, `sonar-project.properties`, `.gitar/rules/*.md`.
Observability (OpenTelemetry → Dash0) has its own guide: [OBSERVABILITY.md](OBSERVABILITY.md).

## Quick start with Task

> **Container engine: Podman by default.** `task tools:install` installs go-task, kind, helm, podman and
> podman-compose and creates a rootful Podman machine (4 CPUs / 8 GB; rootful so ingress-nginx can map
> host ports 80/443). Every container task then runs through Podman: kind gets
> `KIND_EXPERIMENTAL_PROVIDER=podman`, Compose runs as `podman compose`, images build with `podman build`.
> Prefer Docker Desktop / OrbStack? Run tasks with `ENGINE=docker` (or `export ENGINE=docker`).
> minikube (`--driver=podman`) and Podman Desktop's Kubernetes extension also work; only the
> `k8s:cluster:*` tasks assume kind. CI still builds the image with Docker Buildx on GitHub runners —
> the Dockerfile is engine-neutral.

`Taskfile.yml` wraps everything below in single commands ([go-task](https://taskfile.dev), macOS +
Homebrew). `task` with no arguments lists every task, each with a one-line description.

```sh
brew install go-task            # once; `task tools:install` also installs kind and helm
task setup                      # .venv (python3.13), dependencies, .env from .env.example
# edit .env: at least ANTHROPIC_API_KEY (task env:check shows what is set, values masked)
task dev                        # API with reload on http://localhost:8000 (SQLite, no containers)
```

A container runtime (Docker Desktop or OrbStack) is only needed for the Compose and Kubernetes
paths; `task tools:check` reports what is installed.

```sh
task k8s:up                     # kind cluster + ingress-nginx + image + dev stack (Postgres, Redis, Elasticsearch)
# -> http://api.evently.localtest.me/api/v1/health   (ports 80/443 of your Mac must be free)
task k8s:status                 # what is running        task k8s:logs     # follow the API logs
task k8s:port-forward           # http://localhost:8000   task k8s:radar    # Radar UI on :9280
task k8s:cluster:down           # throw the cluster away
```

| Area | Tasks |
|---|---|
| Local | `dev`, `run`, `lint`, `format`, `db:reset`, `env:check`, `health` |
| Compose | `compose:up`, `compose:up:tools` (Kibana), `compose:down`, `compose:down:volumes`, `compose:logs SERVICE=api`, `compose:ps`, `compose:build` |
| Docker | `docker:build`, `docker:run` (`IMAGE=evently-api:dev` by default) |
| Cluster | `k8s:up`, `k8s:cluster:up`, `k8s:cluster:down`, `k8s:image:load`, `k8s:deploy:dev`, `k8s:deploy:prod`, `k8s:secrets:dev`, `k8s:secrets:prod`, `k8s:render`, `k8s:status`, `k8s:logs`, `k8s:port-forward`, `k8s:restart`, `k8s:nuke` |
| Optional | `k8s:dash0:install` + `k8s:dash0:apply` (needs `DASH0_AUTH_TOKEN` in `.env`), `k8s:scale-out:enable`, `k8s:job:venue-research RUN_ID=... EVENT_ID=... CARD_ID=...`, `k8s:radar` |
| Quality | `ci` (what CI runs), `sonar:scan` (needs `SONAR_TOKEN`), `search:status`, `search:reindex` (`ACCESS_TOKEN` env when auth is on) |

Things the tasks do that you would not guess from the manifests:

- **Images.** The overlays name `ghcr.io/OWNER/evently-api` and the dev overlay sets
  `imagePullPolicy: Always`, which cannot work with an image loaded into kind. `k8s:render` therefore
  runs `kubectl kustomize`, rewrites the image (dev: `IMAGE`, prod: `IMAGE_REPO:IMAGE_TAG`) and
  `k8s:deploy:*` pipe that to `kubectl apply -f -`. Nothing under `deploy/` is edited.
- **Secrets.** `base/secret.yaml` is a placeholder template; applying it would overwrite real values, so
  `k8s:render` leaves that one Secret out. `k8s:secrets:dev` builds `evently-api-secrets` from the API
  keys in `.env` (a fixed allow-list, never `.env` wholesale: its `DATABASE_URL` is a local SQLite path)
  plus `DATABASE_URL`/`SECRET_KEY` read from the dev overlay, and `DASH0_AUTH_TOKEN` as
  `OTEL_EXPORTER_OTLP_HEADERS`. `k8s:deploy:dev` runs it for you; re-run it (it restarts the API) after
  editing `.env`.
- **Prod** is deliberately awkward: `task k8s:secrets:prod ENV_FILE=prod.env KUBE_CONTEXT=<ctx>` once, then
  `task k8s:deploy:prod IMAGE_REPO=ghcr.io/<org>/evently-api IMAGE_TAG=sha-<commit> KUBE_CONTEXT=<ctx>`.
  It refuses the kind context, `latest`, the `OWNER` placeholder, and a missing or still-placeholder Secret,
  shows a `kubectl diff`, and asks for confirmation.
- Every `kubectl`/`helm` call passes `--context` (default `kind-evently`), so the local tasks cannot hit
  another cluster by accident. Destructive tasks ask for confirmation (`task --yes ...` skips it).
- **Scale-out** is applied from a throw-away copy of `deploy/k8s` with the `components:` block uncommented;
  the HPA needs metrics-server, which kind does not include.
- The Job template's worker entrypoint does not exist yet, so `k8s:job:venue-research` creates a pod that
  fails (section 7); `RUN_ID` must be a valid DNS label (no underscores).
- `lint` also runs `ruff format --check`, which CI does not; `ci` is the exact CI equivalent.

## 1. The one rule: one API process

Several subsystems keep state **in the process**, not in Postgres or Redis:

| Subsystem | Where | Why it matters |
|---|---|---|
| WebSocket audio hub + STT tee | `app/live/audio_hub.py`, `stt_pipeline.py` | The speaker's frames are fanned out to the listeners *of the same process*. Speaker and listeners must land on one pod. |
| Live-session runtime state (incl. transcript `seq`) | `app/live/service.py` | Counters and current-slide state are process-local. |
| Presence snapshot | `app/realtime/presence.py` | Joins/leaves are published on the bus, but the snapshot sent in the WebSocket hello only knows local clients. |
| Per-event orchestrator / board locks | `app/agents/orchestrator`, `app/services/board.py` | `asyncio.Lock`s; two pods could race on the same board. |
| Agent runtime semaphore | `app/agents/runtime.py` | `MAX_PARALLEL_AGENTS` is per process. |

So the default everywhere is **one API replica and one uvicorn worker** (`replicas: 1`, no `--workers`).
Scale *up* (CPU/memory) before you scale *out*; when you do need more pods read [§6 Scaling out](#6-scaling-out).

## 2. Container image

`Dockerfile` is a two-stage build on `python:3.13-slim`:

1. **builder** creates `/opt/venv` and runs `pip install ".[postgres,redis,otel,search]"` (the project
   itself is installed, non-editable, so the runtime stage copies only the venv).
2. **runtime** copies the venv, creates user `evently` (uid 10001), runs as that user with a
   read-only root filesystem friendly layout (`/tmp`, `/data` are the only writable paths), exposes
   8000, and starts
   `uvicorn app.main:app_factory --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips *`.
   `--proxy-headers` makes `request.client`/scheme follow `X-Forwarded-*` from the Ingress or
   compose network; tighten `--forwarded-allow-ips` to your ingress CIDR if the pod is reachable
   without a proxy.
3. `HEALTHCHECK` hits `GET /api/v1/health` with the standard library (no curl in the image).

```sh
docker build -t ghcr.io/<org>/evently-api:dev .
docker run --rm -p 8000:8000 --env-file .env ghcr.io/<org>/evently-api:dev
```

Without `DATABASE_URL` the image falls back to SQLite under `/data` (a `VOLUME`), which is enough for
a smoke test. `.dockerignore` keeps tests, docs, deploy manifests, `.env` and local databases out of
the build context.

Image tags pushed by CI (see §5): `sha-<commit>` for every push to `main`, `latest` and `dev` for the
tip of `main`, and `X.Y.Z` for `vX.Y.Z` tags.

## 3. Local stack with Docker Compose

```sh
cp .env.example .env         # fill ANTHROPIC_API_KEY, Google/STT keys as needed
docker compose up --build    # API on http://localhost:8000 (OpenAPI UI at /docs)
```

Services and what the API gets from them:

| Service | Image | API setting (set by compose) |
|---|---|---|
| `api` | built from `Dockerfile` | `ENVIRONMENT=development`, `OTEL_ENABLED=true`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317` |
| `postgres` | `postgres:16-alpine` | `DATABASE_URL=postgresql+asyncpg://evently:evently@postgres:5432/evently` |
| `redis` | `redis:7-alpine` (no persistence) | `REDIS_URL=redis://redis:6379/0` → `RedisEventBus` |
| `elasticsearch` | `elasticsearch:9.1.0`, single node, security off, 1 GB heap | `ELASTICSEARCH_URL=http://elasticsearch:9200` |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.144.0` | receives OTLP on 4317/4318, forwards to Dash0 |
| `kibana` (profile `tools`) | `kibana:9.1.0` | optional, `docker compose --profile tools up kibana` |

Everything else in `.env` (API keys, `AUTH_REQUIRED`, …) is passed through unchanged. The
collector reads three extra `.env` keys, all optional:

```
DASH0_ENDPOINT=ingress.<region>.<cloud>.dash0.com:4317   # Dash0 → Organization settings → Endpoints
DASH0_AUTH_TOKEN=auth_...                                 # Dash0 → Organization settings → Auth tokens
DASH0_DATASET=dev
```

Without a token the collector still receives everything and logs a per-batch summary (`debug`
exporter), so you can verify instrumentation offline; the Dash0 export just fails in the collector
log. Elasticsearch needs ~2 GB of RAM in Docker Desktop; drop the service and the
`ELASTICSEARCH_URL` line if you do not need search.

## 4. Kubernetes (kustomize)

```
deploy/k8s/
├── base/                 Namespace, ConfigMap, Secret (template), API Deployment+Service, Redis, Ingress
├── overlays/dev/         + Postgres StatefulSet, Elasticsearch StatefulSet, dev settings, *.localtest.me host
├── overlays/prod/        managed Postgres/Elastic via Secret, TLS via cert-manager, prod settings
├── components/scale-out/ OPT-IN: HPA, PDB, hash-routed Ingress for /ws/* (see §6)
├── dash0/                Dash0Monitoring (+ example Dash0OperatorConfiguration) — applied separately
└── jobs/                 venue-research Job template ("one card = one pod", §7)
```

### 4.1 Render and apply

```sh
kubectl kustomize deploy/k8s/overlays/dev | less          # always look first
kubectl apply -k deploy/k8s/overlays/dev
kubectl -n evently rollout status deploy/evently-api
kubectl -n evently port-forward svc/evently-api 8000:80    # http://localhost:8000/api/v1/health
```

Prod is the same with `overlays/prod`, after you have (a) replaced `ghcr.io/OWNER/...` with your
registry path in `base/kustomization.yaml` or via `kustomize edit set image`, (b) pinned
`images[0].newTag` to a real tag, (c) replaced the Secret (next section) and (d) set the Ingress host.

### 4.2 Configuration and secrets

Every key in the ConfigMap/Secret is an `app/core/config.py` setting (uppercase env name). The API
pod gets both with `envFrom`, so adding a setting is a one-line change.

- `base/configmap.yaml` — non-secret settings. `REDIS_URL` points at the in-cluster Redis so the
  `RedisEventBus` is used (envelope history survives restarts and it is a prerequisite for §6).
  `OTEL_*` and `DASH0_DATASET` configure telemetry.
- `base/secret.yaml` — **a template with placeholders**. Do not commit real values. Either delete it
  from the kustomization and create the Secret with your tooling (External Secrets Operator, SOPS,
  Sealed Secrets, `kubectl create secret generic evently-api-secrets --from-env-file=prod.env`), or
  keep it and let the secret manager overwrite it. The Secret carries `DATABASE_URL` (managed
  Postgres in prod), the API keys and `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <dash0 token>`.
- The dev overlay patches `DATABASE_URL` to the in-cluster Postgres and moves `ELASTICSEARCH_URL`
  to the ConfigMap. Note the `envFrom` order: Secret after ConfigMap, so a key present in both takes
  the Secret's value (an empty string means "unset" thanks to `env_ignore_empty`).

### 4.3 The API Deployment

- `replicas: 1`, `RollingUpdate` with `maxUnavailable: 0, maxSurge: 1`: the new pod must be Ready
  before the old one is taken out; with one replica this is the difference between a blip and an
  outage. For a few seconds both pods exist — fine, because the old one stops *accepting* new
  WebSockets as soon as it leaves the endpoints.
- Probes all hit `GET /api/v1/health` (which also reports what is configured). The startup probe
  allows 90 s for first boot (table creation, Redis/ES connections).
- `preStop` sleeps 5 s so ingress-nginx drops the pod before uvicorn stops; the app's shutdown
  hooks then close WebSockets with 1001 and clients reconnect with `?since=<seq>`.
- Resources: requests 250m/512Mi, limits 2 CPU/1Gi (dev overlay shrinks, prod overlay grows). The
  API is mostly I/O bound; memory grows with concurrent agents (Claude streaming buffers) and audio
  listeners (0.5 s of PCM each, ~16 KB).
- `OTEL_RESOURCE_ATTRIBUTES` carries `k8s.pod.name`, `k8s.namespace.name`, `k8s.node.name` from the
  downward API so Dash0 can join traces with Kubernetes metadata.
- Hardened: non-root uid 10001, read-only root FS, no capabilities, no service-account token.

### 4.4 Data services

- **Redis** (`base/redis.yaml`) — Deployment + Service, 256 MB `allkeys-lru`, no persistence. It is
  a message bus with a 24 h replay log, not a store; losing it only forces clients to resync.
- **Postgres** (`overlays/dev/postgres.yaml`) — dev only. In prod set `DATABASE_URL` to a managed
  database (`postgresql+asyncpg://…`). Tables are created on startup (no migrations yet, plan §7).
- **Elasticsearch** (`overlays/dev/elasticsearch.yaml`) — dev only: single node, security off, 1 GB
  heap, privileged init container for `vm.max_map_count`. In prod use **Elastic Cloud** (set
  `ELASTICSEARCH_URL` + `ELASTICSEARCH_API_KEY`) or **ECK** (`helm install elastic-operator
  elastic/eck-operator`, an `Elasticsearch` CR with 3 nodes and TLS, then
  `ELASTICSEARCH_URL=https://<name>-es-http.<ns>.svc:9200` and an API key).

### 4.5 Ingress

`base/ingress.yaml` targets **ingress-nginx** with one `Prefix /` rule. WebSocket upgrades work
out of the box; the annotations raise `proxy-read/send-timeout` to 3600 s (idle control sockets,
hour-long audio streams), turn `proxy-buffering` off for SSE, and allow 16 MB bodies. Overlays set
the host (`api.evently.localtest.me` in dev — resolves to 127.0.0.1 — and `api.evently.example` +
cert-manager TLS in prod).

Other ingress controllers: the equivalents are `timeout-read/send` style annotations plus
WebSocket enablement; the path-hash affinity in §6 is ingress-nginx specific.

### 4.6 Labels for Radar

Every resource carries the recommended labels: `app.kubernetes.io/name` (per workload),
`app.kubernetes.io/component` (`api`, `event-bus`, `database`, `search`, `agent`, `ingress`),
`app.kubernetes.io/part-of: evently`, and a plain `app: evently`. Radar's topology view can group
"by namespace" or "by app label"; its docs do not say which label key it reads (unverified), so both
the standard `part-of` and the plain `app` label are present. Grouping by namespace (`evently`) always
works. Install Radar with `brew install kubectl-radar && kubectl radar` (UI on `localhost:9280`) or
in-cluster via `helm install radar skyhook/radar -n radar --create-namespace`.

### 4.7 Dash0 operator

Install once per cluster (verified against Dash0's installation docs):

```sh
helm repo add dash0-operator https://dash0hq.github.io/dash0-operator
helm repo update
kubectl create namespace dash0-system
kubectl -n dash0-system create secret generic dash0-authorization-secret --from-literal=token=auth_...
helm install --wait --namespace dash0-system dash0-operator dash0-operator/dash0-operator \
  --set operator.dash0Export.enabled=true \
  --set operator.dash0Export.endpoint=ingress.<region>.<cloud>.dash0.com:4317 \
  --set operator.dash0Export.apiEndpoint=https://api.<region>.<cloud>.dash0.com \
  --set operator.dash0Export.secretRef.name=dash0-authorization-secret \
  --set operator.dash0Export.secretRef.key=token \
  --set operator.clusterName=evently-dev \
  --set operator.instrumentation.enablePythonAutoInstrumentation=true   # optional, see OBSERVABILITY.md
```

Then enable monitoring for the namespace: `kubectl apply -k deploy/k8s/dash0` (a `Dash0Monitoring`
in `evently`, `instrumentWorkloads.mode: all`, label selector `dash0.com/enable!=false`). Redis,
Postgres and Elasticsearch are labelled `dash0.com/enable: "false"`. If you installed the chart
*without* the `dash0Export` values, uncomment `dash0-operator-configuration.example.yaml` in
`deploy/k8s/dash0/kustomization.yaml` and fill it in — never have both. Endpoint formats
("starts with `ingress.`, ends with `dash0.com:4317`") and the `Dash0Monitoring` `v1beta1` /
`Dash0OperatorConfiguration` `v1alpha1` API versions were checked against the docs; the exact
`spec.filter` OTTL syntax in the monitoring resource was not (unverified; remove the block if the
CRD rejects it).

## 5. CI (GitHub Actions, Sonar, Gitar)

`.github/workflows/ci.yml` runs on PRs, pushes to `main`, `v*` tags:

| Job | What | Notes |
|---|---|---|
| `lint` | `ruff check` | Also writes `ruff-report.json` for Sonar. |
| `test` | `pytest tests --cov=app` → `coverage.xml`, `pytest-report.xml` | Installs `.[dev,otel]` + `pytest-cov` (CI-only, not a runtime dependency). Existing tests only. |
| `sonar` | `SonarSource/sonarqube-scan-action@v8` | Needs `SONAR_TOKEN`; project key/org in `sonar-project.properties`; coverage, xunit and ruff reports imported; `sonar.qualitygate.wait=true` fails the job on a red gate. Skipped for fork PRs. |
| `docker-build` | buildx → GHCR | Builds on every PR, pushes on `main`/tags with `sha-`, `latest`, `dev`, semver tags. |

**SonarQube Cloud setup:** create the project in SonarQube Cloud (GitHub import), turn *off*
"Automatic Analysis" (CI-based analysis and automatic analysis are mutually exclusive), copy the
project key and organization into `sonar-project.properties`, and add `SONAR_TOKEN` as a repository
secret. `v8` is the current major of the scan action (checked 2026-11-09); it needs
`fetch-depth: 0` on checkout.

**Gitar** is a GitHub App, not a workflow step: sign in at `app.gitar.ai`, *Connect GitHub*, grant
the repo, and `gitar-bot` reviews every PR (mention `@gitar` in a comment to ask it things; label a PR
`gitar-skip` to skip it). Repository rules live in **`.gitar/rules/*.md`** — Markdown with YAML
front matter (`title`, `description`, `slug`, `type: check|automation`, `when`, `actions`) followed
by free-form instructions; this layout was checked against docs.gitar.ai/features/rules. Rules
require a Pro or Enterprise plan (Pro: up to five per organisation). Ours encode plan §5:

- `frozen-foundation-files.md` — touching `app/core/**`, `app/models/**`, `app/llm/**`,
  `app/agents/runtime.py`, `app/main.py`, `pyproject.toml`, … fails unless the PR title starts
  with `foundation:`.
- `no-network-in-tests.md` — changed tests must use `ScriptedLLM`, `respx` and in-process fakes.
- `package-ownership.md` — reports which packages a PR touches; fails on imports that bypass the
  package entrypoints or on services writing to sockets instead of `ctx.bus.publish`.

## 6. Scaling out

Do **not** just raise `replicas`. Work through this checklist; each item removes one piece of
per-process state from §1.

1. **Redis event bus on** — `REDIS_URL` set (already the case in `base/configmap.yaml`). Required so
   envelopes published on pod A reach WebSockets on pod B.
2. **Postgres** — never SQLite with more than one pod.
3. **Sticky routing for `/ws/sessions/*` and `/ws/events/*`** — enable the `scale-out` component
   (next paragraph). This pins all sockets of one live session (speaker + listeners + control) and
   of one event board to a single pod, which keeps the in-memory audio hub, STT tee, session state
   and presence snapshot correct *for that session*.
4. **DB-backed transcript `seq`** — the live-session transcript counter must move from process
   memory to a Postgres sequence/row (or Redis `INCR`), otherwise a pod change mid-session restarts
   numbering. (Package 13 work; not done.)
5. **External lock for orchestrator / board** — replace the per-event `asyncio.Lock`s with a Redis
   lock (`SET NX PX`) so two pods cannot run two orchestrators for one event. (Packages 2/7; not done.)
6. **Agent concurrency** — `MAX_PARALLEL_AGENTS` becomes per pod; either accept N× the Claude
   concurrency or move the semaphore to Redis.
7. Then: `components: [../../components/scale-out]` in the overlay, which sets `replicas: 2`, adds an
   HPA (2–6 pods on CPU 70 % / memory 80 %, slow scale-down), a PodDisruptionBudget, and the
   `evently-ws` Ingress.

### How the sticky Ingress works

`components/scale-out/ingress-ws-affinity.yaml` is a second Ingress for the same host with only
`/ws/sessions` and `/ws/events` paths and `nginx.ingress.kubernetes.io/upstream-hash-by: "$uri"`.
ingress-nginx merges rules per host and the longer path wins, so WebSocket paths get consistent
hashing on the request path (without the query string — tokens are in `?token=`), REST stays
round-robin.

- `/ws/sessions/{sid}` → one pod per session for the control socket (presence, slide state).
- `/ws/sessions/{sid}/audio` → one pod per session for speaker **and** all listeners (the hub).
- `/ws/events/{eid}` → one pod per board for collaborators.

Control and audio sockets of the same session hash to *different* keys and may land on different
pods. Today that is fine: the hub publishes `session.audio.state` on the bus and the STT pipeline
publishes transcripts on the bus, so the control pod sees them. If a later feature needs both on
one pod, use a regex path with a named capture and hash on it:
`nginx.ingress.kubernetes.io/use-regex: "true"`, path `/ws/sessions/(?<session_id>[^/]+)`, and
`upstream-hash-by: "$session_id"` (nginx exposes named captures from the `location` regex as
variables; unverified end-to-end with ingress-nginx's Lua balancer).

**Trade-off vs cookie affinity.** `affinity: cookie` pins a *browser*; two attendees of one session
would land on different pods and never share a hub, so it does not solve our problem. Path hashing
pins a *session*. Its costs: a very popular session cannot be spread across pods (use LiveKit for
that, plan §7), and every scale event remaps about 1/N of keys — those clients get disconnected and
reconnect with `?since=<seq>` (the hash is consistent, so the other N-1/N stay put). The HPA's
600 s scale-down window keeps this rare.

## 7. One agent per pod (Job template)

`deploy/k8s/jobs/venue-research-job.yaml` is a `Job` that would run exactly one venue-research
agent, so Radar shows one pod per whiteboard card. It is a **template only** (`envsubst` variables
`RUN_ID`, `EVENT_ID`, `CARD_ID`, `PLACE_ID`, `IMAGE_TAG`, `TRACEPARENT`) and is not applied by any
overlay. The pod reuses the API image and ConfigMap/Secret, gets `OTEL_SERVICE_NAME=evently-agent`,
labels `evently.dev/agent-kind`, `evently.dev/run-id`, `evently.dev/card-id`, and
`ttlSecondsAfterFinished: 600` so finished pods stay visible for a while.

What the runtime needs before this works (design note, not implemented):

1. **A `k8s` backend for `AgentRuntime`.** `spawn()` today creates an asyncio task. A backend
   abstraction would let `spawn(kind="venue_research", …)` instead create the `AgentRun` row and a
   `Job` from this template (Kubernetes API via a small in-cluster client; the API pod needs a
   ServiceAccount with `create jobs` in its namespace — currently the token is not even mounted).
   `cancel()` deletes the Job; `wait()` polls the row.
2. **A worker entrypoint** `python -m app.agents.worker --kind venue_research --run-id …` that builds
   an `AppContext` (same settings, so Postgres + Redis bus + OTel), loads the run row, constructs a
   `RunContext` and calls the same coroutine the in-process runtime calls
   (`app/agents/venues` entrypoint), then exits 0/1. `run.step()`/`_finish()` already write to the
   DB and publish on the bus, so the whiteboard card updates exactly as today **because the bus is
   Redis** — that is why the Redis bus is on by default.
3. **Trace continuity:** the worker reads `TRACEPARENT` and starts its `agent.run` span as a child
   of the orchestrator's span (`app/observability/tracing.current_traceparent()` produces the value).
4. **Flush on exit:** call `app.observability.otel.force_flush()` before the process ends so the
   last spans are not lost.
5. Limits: Job start-up (image pull + Python boot) adds 5–20 s per card compared with an asyncio task,
   so keep the in-process backend as the default and use `k8s` for the demo/"big research" mode.

## 8. Checks you can run without a cluster

- `kubectl kustomize deploy/k8s/overlays/dev` and `.../prod` render (done; 14 and 9 resources).
- `.venv/bin/python -c "from app.main import create_app; create_app()"` boots with and without
  `OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`.
- Things that need Docker or a cluster and were **not** run here: the image build itself (wheel
  availability for every dependency on 3.13-slim is expected but unverified), compose start-up
  ordering, the Elasticsearch init container, ingress-nginx's hashing behaviour, the Dash0 operator's
  reaction to a pod that already exports OTLP (documented behaviour: a Warning event, no override).
