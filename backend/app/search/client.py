"""The shared Elasticsearch client: connection, capability detection and index bootstrap.

One `SearchClient` lives per app in `ctx.service("search.client", ...)`; both the background indexer
and the search service use it. `connect()` is a single attempt (the indexer retries it in the
background with backoff), so a missing or unreachable cluster never blocks application startup.

Semantic search: at connect time we look for an inference endpoint on the cluster. If one is found
(and answers a probe request) the venues index is created with a `semantic_text` field; otherwise the
same field is plain `text` and search runs BM25 only. The decision is sticky for the life of an
index: an existing index keeps the mapping it was created with, and `semantic` reflects that mapping.

Selection of the inference endpoint, in order:

1. `ELASTICSEARCH_INFERENCE_ID` environment variable: an endpoint id to use as-is, or `none` to force
   plain mode (useful on small local clusters where ELSER cannot be allocated).
2. The cluster's preconfigured `.elser-2-elasticsearch` endpoint, if listed by `GET _inference/_all`.
3. Any other `sparse_embedding` endpoint, then any `text_embedding` endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import ApiError, BadRequestError, TransportError
from elasticsearch.helpers import async_streaming_bulk

from app.search.indices import VENUE_SEMANTIC_FIELD, IndexNames, all_mappings, index_settings

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.context import AppContext

log = logging.getLogger(__name__)

PREFERRED_INFERENCE_ID = ".elser-2-elasticsearch"
INFERENCE_ENV_VAR = "ELASTICSEARCH_INFERENCE_ID"
# Time allowed for the probe that proves the inference endpoint can actually embed text. ELSER needs
# an ML node with a few GB free; on first use it also downloads the model, which may exceed this.
INFERENCE_PROBE_TIMEOUT_S = 90

SERVICE_KEY = "search.client"


class SearchUnavailable(RuntimeError):
    """Elasticsearch is not configured or not reachable. Routes translate this into HTTP 503."""


class SearchClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.elasticsearch_url:
            raise SearchUnavailable("Elasticsearch is not configured (set ELASTICSEARCH_URL).")
        self.url = settings.elasticsearch_url
        self.names = IndexNames(settings.elasticsearch_index_prefix)
        # The transport logs every failed attempt with a traceback; the indexer reports connectivity itself.
        logging.getLogger("elastic_transport").setLevel(logging.ERROR)
        self.es = AsyncElasticsearch(
            settings.elasticsearch_url,
            api_key=settings.elasticsearch_api_key or None,
            request_timeout=15,
            retry_on_timeout=True,
            max_retries=2,
        )
        self.connected = False
        self.semantic = False
        self.inference_id: str | None = None
        self.cluster: dict[str, Any] = {}
        self.serverless = False
        self.last_error: str | None = None
        self._ready = asyncio.Event()
        self._closed = False

    # --- lifecycle ---------------------------------------------------------------

    async def connect(self) -> None:
        """Verify the cluster, detect semantic support and create missing indices. One attempt; raises."""
        try:
            info = await self.es.info()
            self.cluster = {
                "name": info.get("cluster_name"),
                "version": (info.get("version") or {}).get("number"),
            }
            self.serverless = (info.get("version") or {}).get("build_flavor") == "serverless"
            self.inference_id = await self._detect_inference()
            await self.ensure_indices()
        except (TransportError, ApiError) as exc:
            self.connected = False
            self.last_error = _describe(exc)
            raise
        self.connected = True
        self.last_error = None
        self._ready.set()
        log.info(
            "search: connected to Elasticsearch %s (%s), semantic=%s, inference_id=%s",
            self.cluster.get("version"),
            self.url,
            self.semantic,
            self.inference_id,
        )

    def mark_disconnected(self, exc: BaseException) -> None:
        self.connected = False
        self.last_error = _describe(exc)
        self._ready.clear()

    async def wait_ready(self, timeout: float | None = None) -> None:
        """Block until `connect()` has succeeded; raise `SearchUnavailable` on timeout."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            raise SearchUnavailable(
                "Elasticsearch is configured but not reachable" + (f": {self.last_error}" if self.last_error else ".")
            ) from None

    def require_connected(self) -> None:
        if not self.connected:
            raise SearchUnavailable(
                "Elasticsearch is configured but not reachable"
                + (f": {self.last_error}" if self.last_error else ". The indexer is retrying in the background.")
            )

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self.es.close()

    # --- capability detection ----------------------------------------------------

    async def _detect_inference(self) -> str | None:
        forced = os.environ.get(INFERENCE_ENV_VAR, "").strip()
        if forced.lower() in {"none", "off", "false", "0"}:
            log.info("search: semantic search disabled via %s", INFERENCE_ENV_VAR)
            return None
        if forced:
            return forced
        try:
            listing = await self.es.inference.get()
        except (TransportError, ApiError) as exc:
            log.info("search: inference API unavailable (%s); venue search runs in plain-text mode", _describe(exc))
            return None
        endpoints = list(listing.get("endpoints") or [])
        by_id = {e.get("inference_id"): e for e in endpoints}
        candidate: str | None = None
        if PREFERRED_INFERENCE_ID in by_id:
            candidate = PREFERRED_INFERENCE_ID
        else:
            for task_type in ("sparse_embedding", "text_embedding"):
                match = next((e for e in endpoints if e.get("task_type") == task_type), None)
                if match:
                    candidate = match.get("inference_id")
                    break
        if candidate is None:
            return None
        if await self._probe_inference(candidate):
            return candidate
        return None

    async def _probe_inference(self, inference_id: str) -> bool:
        """Prove the endpoint can embed text (allocates/downloads the model on first use)."""
        try:
            await self.es.options(request_timeout=INFERENCE_PROBE_TIMEOUT_S).inference.inference(
                inference_id=inference_id, input=["quiet venue with a terrace for 150 people"]
            )
        except (TransportError, ApiError) as exc:
            log.warning(
                "search: inference endpoint %s did not answer (%s); venue search runs in plain-text mode. "
                "Set %s=none to silence this, or give the cluster an ML node.",
                inference_id,
                _describe(exc),
                INFERENCE_ENV_VAR,
            )
            return False
        return True

    # --- indices -----------------------------------------------------------------

    async def ensure_indices(self) -> None:
        """Create any missing index. Existing indices are left untouched (mappings are not migrated)."""
        mappings = all_mappings(self.names, inference_id=self.inference_id)
        for name, mapping in mappings.items():
            if await self.es.indices.exists(index=name):
                if name == self.names.venues:
                    await self._read_semantic_from_mapping()
                continue
            created = await self._create_index(name, mapping)
            if name == self.names.venues:
                self.semantic = created and self.inference_id is not None
                if not created:
                    await self._read_semantic_from_mapping()

    async def _create_index(self, name: str, mapping: dict[str, Any]) -> bool:
        """Create `name`; returns False when it was created concurrently or the semantic field was refused."""
        try:
            settings = index_settings(serverless=self.serverless)
            await self.es.indices.create(index=name, mappings=mapping, settings=settings)
            log.info("search: created index %s", name)
            return True
        except BadRequestError as exc:
            if "resource_already_exists_exception" in str(exc):
                return False
            if name == self.names.venues and self.inference_id is not None:
                log.warning(
                    "search: cluster refused the semantic_text mapping (%s); creating %s in plain-text mode",
                    _describe(exc),
                    name,
                )
                self.inference_id = None
                plain = all_mappings(self.names, inference_id=None)[name]
                settings = index_settings(serverless=self.serverless)
                await self.es.indices.create(index=name, mappings=plain, settings=settings)
                return False
            raise

    async def _read_semantic_from_mapping(self) -> None:
        """Set `semantic` / `inference_id` from the venues index that already exists."""
        response = await self.es.indices.get_mapping(index=self.names.venues)
        mapping = next(iter(response.body.values()), {}).get("mappings", {})
        field = (mapping.get("properties") or {}).get(VENUE_SEMANTIC_FIELD) or {}
        if field.get("type") == "semantic_text":
            self.semantic = True
            self.inference_id = field.get("inference_id") or self.inference_id
        else:
            if self.inference_id is not None:
                log.info(
                    "search: %s exists without a semantic_text field; delete the index and reindex to enable "
                    "semantic search",
                    self.names.venues,
                )
            self.semantic = False
            self.inference_id = None

    # --- writes ------------------------------------------------------------------

    async def bulk(self, actions: list[dict[str, Any]], *, refresh: bool | str = False) -> tuple[int, list[Any]]:
        """Run a bulk request. Transport failures raise; per-document failures come back in the list."""
        if not actions:
            return 0, []
        ok_count, errors = 0, []
        stream = async_streaming_bulk(
            self.es.options(request_timeout=60),
            actions,
            raise_on_error=False,
            raise_on_exception=True,
            max_retries=3,  # 429s are retried by the helper with exponential backoff
            initial_backoff=1,
            max_backoff=8,
            yield_ok=True,
            refresh=refresh,
        )
        async for ok, item in stream:
            op, body = next(iter(item.items()))
            if ok or (op == "delete" and body.get("status") == 404):
                ok_count += 1  # deleting a document that was never indexed is not an error
            else:
                errors.append(item)
        return ok_count, errors

    async def delete_by_query(self, indices: tuple[str, ...] | list[str], query: dict[str, Any], *, refresh=False):
        await self.es.delete_by_query(
            index=list(indices), query=query, conflicts="proceed", refresh=refresh, ignore_unavailable=True
        )

    # --- reads -------------------------------------------------------------------

    async def doc_counts(self) -> dict[str, int | None]:
        counts: dict[str, int | None] = {}
        for label, name in (
            ("venues", self.names.venues),
            ("transcripts", self.names.transcripts),
            ("messages", self.names.messages),
            ("slides", self.names.slides),
        ):
            try:
                response = await self.es.options(request_timeout=5, max_retries=0).count(index=name)
                counts[label] = int(response["count"])
            except (TransportError, ApiError):
                counts[label] = None
        return counts


def _describe(exc: BaseException) -> str:
    """Short, single-line description of a client error (transport errors nest their causes)."""
    text = str(getattr(exc, "message", None) or exc) or exc.__class__.__name__
    text = text.split("Connection error caused by: ")[-1].replace("\n", " ")
    text = f"{exc.__class__.__name__}: {text}"
    return text if len(text) <= 200 else text[:197] + "..."


def get_search_client(ctx: AppContext) -> SearchClient | None:
    """The app's `SearchClient`, or None when `ELASTICSEARCH_URL` is unset."""
    if not ctx.settings.elasticsearch_url:
        return None

    def build() -> SearchClient:
        client = SearchClient(ctx.settings)
        ctx.on_shutdown(client.close)  # idempotent; the indexer also closes it when it stops
        return client

    return ctx.service(SERVICE_KEY, build)


def require_search_client(ctx: AppContext) -> SearchClient:
    """The connected `SearchClient`; raises `SearchUnavailable` with a message fit for a 503 body."""
    client = get_search_client(ctx)
    if client is None:
        raise SearchUnavailable(
            "Search is not configured: set ELASTICSEARCH_URL (and ELASTICSEARCH_API_KEY for Elastic Cloud)."
        )
    client.require_connected()
    return client
