# syntax=docker/dockerfile:1.7
#
# Evently API image. Two stages: build the virtualenv with the production extras, then copy
# it into a slim runtime image that runs as a non-root user.
#
#   docker build -t ghcr.io/<org>/evently-api:dev .
#   docker run --rm -p 8000:8000 --env-file .env ghcr.io/<org>/evently-api:dev
#
# Run ONE uvicorn worker per container (see docs/DEPLOYMENT.md): the audio hub, presence and the
# agent runtime keep in-process state, so scale with replicas + sticky routing, not with workers.

ARG PYTHON_VERSION=3.13

# --- builder ------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Every dependency (asyncpg, grpcio, bcrypt, …) ships manylinux wheels for 3.13, so no compiler
# is needed. If a future pin forces a source build, add: apt-get install -y --no-install-recommends gcc libpq-dev
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY app ./app
# The project itself is installed into the venv (non-editable), so the runtime stage only needs /opt/venv.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
 && pip install ".[postgres,redis,otel,search]"

# --- runtime ------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="evently-api" \
      org.opencontainers.image.description="Evently backend: FastAPI, Claude agents, live whiteboard and sessions" \
      org.opencontainers.image.source="https://github.com/OWNER/evently"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Defaults that the cluster/compose override; SQLite keeps `docker run` working out of the box.
    ENVIRONMENT=production \
    DATABASE_URL="sqlite+aiosqlite:////data/evently.db"

RUN groupadd --system --gid 10001 evently \
 && useradd --system --uid 10001 --gid evently --home-dir /app --shell /usr/sbin/nologin evently \
 && mkdir -p /app /data \
 && chown -R evently:evently /app /data

COPY --from=builder --chown=evently:evently /opt/venv /opt/venv

WORKDIR /app
USER evently:evently

EXPOSE 8000
VOLUME ["/data"]

# Health: no curl in the image; stdlib only. Kubernetes uses its own probes (see deploy/k8s).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
