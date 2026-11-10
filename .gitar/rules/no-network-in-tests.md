---
title: "No network in tests"
description: "Tests must not reach real services: use ScriptedLLM, respx for HTTP, in-process fakes for provider WebSockets (plan §5 rule 5)."
slug: no-network-in-tests
type: check
when: "A pull request adds or changes files under backend/tests/"
actions: "Fail the check and comment on the offending lines"
---

# No network in tests

`backend/docs/IMPLEMENTATION_PLAN.md` §5 rule 5: tests run offline. The fixtures in `backend/tests/conftest.py`
already give every test an isolated app with `ScriptedLLM` in `ctx.llm`.

## Fail the check when a changed test file

- constructs a real client: `anthropic.AsyncAnthropic(` / `anthropic.Anthropic(` or
  `AnthropicLLM(` outside a test that injects a fake `client=`;
- calls `httpx.AsyncClient` / `httpx.Client` / `requests.*` / `aiohttp.ClientSession` against a
  non-`respx`-mocked URL, or `websockets.connect(` / `aiohttp ws_connect(` to a `ws://` or `wss://`
  host that is not an in-process fake;
- connects to infrastructure (`redis.from_url(`, `redis.asyncio.Redis(`, `asyncpg.connect(`,
  `AsyncElasticsearch(` with a URL) instead of the in-process fakes used by
  `backend/tests/test_realtime_redis_bus.py` and friends;
- reads an API key from the environment to decide whether to hit a live service
  (`os.environ["ANTHROPIC_API_KEY"]`, `pytest.mark.skipif(not os.getenv("DEEPGRAM_API_KEY")…)`).
  Live-service checks are manual (backend/docs/IMPLEMENTATION_PLAN.md §6), not pytest.

## Pass when

- HTTP calls are wrapped with `respx.mock` / `respx.MockRouter`;
- WebSocket servers are the project's in-process fakes (`backend/tests/test_audio_fakes.py`, STT fakes);
- the only client is Starlette's `TestClient` / `httpx.ASGITransport` against the app under test.

Comment on the exact lines with the fixture or fake the author should use instead.
