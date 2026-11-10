---
title: "Package ownership and entrypoint-only cross-package calls"
description: "A PR should stay inside one work package's files and talk to other packages through their entrypoints (plan §5 rules 1–4)."
slug: package-ownership
type: check
when: "A pull request changes files under backend/app/"
actions: "Comment with the packages touched; fail only on cross-package imports that bypass entrypoints or on direct socket writes from services"
---

# Package ownership

`backend/docs/IMPLEMENTATION_PLAN.md` §5 maps every path to a work package. Use that table as the source of
truth (the summary below may lag behind it):

| Package | Owns |
|---|---|
| 1 Auth + events | `backend/app/api/auth.py`, `backend/app/api/events.py` |
| 2 Board | `backend/app/api/board.py`, `backend/app/services/board.py`, `backend/app/services/layout.py` |
| 3 Realtime gateway | `backend/app/api/ws_events.py`, `backend/app/api/stream.py`, `backend/app/realtime/redis_bus.py`, `backend/app/realtime/presence.py` |
| 4–5 Providers | `backend/app/providers/places.py`, `weather.py`, `traffic.py` |
| 6 Venue agents | `backend/app/agents/venues/**`, `backend/app/api/venues.py` |
| 7 Orchestrator | `backend/app/agents/orchestrator/**`, `backend/app/api/chat.py` |
| 8–9 Presentations | `backend/app/agents/presentations/**`, `backend/app/api/presentations.py`, `backend/app/services/deck_render.py`, `backend/app/services/html_sanitize.py` |
| 10–11 STT | `backend/app/transcription/*` (except `base.py`) |
| 12–14 Live | `backend/app/live/**`, `backend/app/api/live_sessions.py`, `backend/app/api/ws_live.py`, `backend/app/api/ws_audio.py` |
| 15 Outreach | `backend/app/integrations/**` (except `outreach.py` signatures), `backend/app/api/outreach.py` |
| 16 Platform | `backend/Dockerfile`, `backend/docker-compose.yml`, `backend/deploy/**`, `.github/**`, `backend/app/observability/**`, `backend/sonar-project.properties`, `.gitar/**`, `backend/docs/DEPLOYMENT.md`, `backend/docs/OBSERVABILITY.md` |
| Search | `backend/app/search/**`, `backend/app/api/search.py` |

## Steps

1. Group the changed `backend/app/` files by package. Post one short comment naming the packages touched.
   Touching two or more packages is **not** a failure by itself (integration PRs exist), but ask the
   author to confirm the other owners agreed when the PR title does not start with `integration:`
   or `foundation:`.
2. **Fail** when a changed file imports another package's internals instead of its entrypoint
   (§5 rule 3). Entrypoints are: the `__init__.py` of `backend/app/agents/venues`,
   `backend/app/agents/presentations`, `backend/app/agents/orchestrator`; `backend/app/live/service.py`; `ctx.places`,
   `ctx.weather`, `ctx.traffic`; `backend/app/transcription/factory.py`. Examples that fail:
   `from app.agents.venues.research import …` from outside `backend/app/agents/venues/`,
   `from app.transcription.deepgram import …` from outside `backend/app/transcription/`.
3. **Fail** when a service or agent writes to a WebSocket or SSE response directly instead of
   publishing an envelope with `ctx.bus.publish(topic, type, data)` using constants from
   `backend/app/realtime/events.py` (§5 rule 4). Only `backend/app/api/ws_*.py` and `backend/app/api/stream.py` may call
   `websocket.send_*` / stream bytes to clients.
4. Flag (comment, do not fail) new string literals used as event types where an `events.py`
   constant exists.

Keep comments short and point at the entrypoint the author should import instead.
