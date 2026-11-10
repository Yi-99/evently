"""Luma public API client and the `LumaEventDraft` → Luma payload mapping.

Wire details were verified on 2026-11-09 against docs.luma.com (the OpenAPI-backed reference):

* Base URL `https://public-api.luma.com` (the older `api.lu.ma` host is the legacy name);
  the key goes in the `x-luma-api-key` header. Keys are scoped to one calendar and need
  Luma Plus active on that calendar, so events are created on the calendar the key belongs to.
* `POST /v1/events/create`  body: name, start_at, end_at (ISO 8601), timezone (IANA), description_md,
  geo_address_json ({type: "manual", address} | {type: "google", place_id} | {type: "lookup", query}),
  visibility (public | members-only | private), max_capacity, ticket_types[{name, type, require_approval}]
  → 200 `{"id": "evt-…"}`. Coordinates are not accepted on create.
* `POST /v1/events/update`  body: event_id + the same optional fields → 200 `{}`.
* `GET  /v1/events/get?event_id=…` → flat event object incl. `url`, `name`, `start_at`, `timezone`.
* `POST /v1/events/guests/add` and `POST /v1/events/guests/send-invites`
  body: event_id, guests[{email, name?}] (+ send_email / message) → 200 `{"skipped": [{email}]}`.
* Rate limit: 200 req/min per calendar key, `429` with `Retry-After` seconds.

Error bodies are not documented; `_error_message` reads `message` / `error` when present.
Everything above lives in the constants and the `LumaClient` methods so a wire change is a one-place edit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.integrations.errors import OutreachError
from app.schemas.outreach import LumaEventDraft

LUMA_API_BASE = "https://public-api.luma.com"
LUMA_API_KEY_HEADER = "x-luma-api-key"

CREATE_EVENT_PATH = "/v1/events/create"
UPDATE_EVENT_PATH = "/v1/events/update"
GET_EVENT_PATH = "/v1/events/get"
ADD_GUESTS_PATH = "/v1/events/guests/add"
SEND_INVITES_PATH = "/v1/events/guests/send-invites"


def _iso_utc(value: datetime) -> str:
    """Luma's examples use `…Z`; naive datetimes are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def luma_address(draft: LumaEventDraft) -> str | None:
    """One address line for `geo_address_json` (type "manual"): venue name first unless it is already in the address."""
    name, address = (draft.venue_name or "").strip(), (draft.address or "").strip()
    if not address:
        return name or None
    if name and name.lower() not in address.lower():
        return f"{name}, {address}"
    return address


def draft_to_payload(draft: LumaEventDraft, *, for_update: bool = False) -> dict[str, Any]:
    """Map our draft to Luma's create/update body (without `event_id`).

    `require_rsvp_approval` becomes a free "Standard" ticket type that requires approval; ticket types are
    create-only, so it is omitted on update. `lat`/`lng` have no create-time counterpart and are not sent.
    """
    payload: dict[str, Any] = {
        "name": draft.name,
        "start_at": _iso_utc(draft.start_at),
        "timezone": draft.timezone,
        "description_md": draft.description_md,
        "visibility": draft.visibility,
    }
    if draft.end_at is not None:
        payload["end_at"] = _iso_utc(draft.end_at)
    address = luma_address(draft)
    if address:
        payload["geo_address_json"] = {"type": "manual", "address": address}
    if draft.capacity is not None:
        payload["max_capacity"] = draft.capacity
    if draft.require_rsvp_approval and not for_update:
        payload["ticket_types"] = [{"name": "Standard", "type": "free", "require_approval": True}]
    return payload


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return response.text.strip() or response.reason_phrase or f"HTTP {response.status_code}"


class LumaClient:
    """Thin async client; every method raises `OutreachError` on a non-2xx or transport error."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = LUMA_API_BASE,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._headers = {LUMA_API_KEY_HEADER: api_key, "Accept": "application/json"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, self._base_url + path, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise OutreachError(f"Luma request failed: {exc}") from exc
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            hint = f"; retry after {retry_after}s" if retry_after else ""
            raise OutreachError(f"Luma rate limit exceeded ({_error_message(response)}){hint}")
        if response.status_code >= 400:
            raise OutreachError(f"Luma error {response.status_code}: {_error_message(response)}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise OutreachError("Luma returned a non-JSON response") from exc

    async def create_event(self, payload: dict[str, Any]) -> str:
        """Create an event on the key's calendar; returns Luma's event id (`evt-…`)."""
        body = await self._request("POST", CREATE_EVENT_PATH, json=payload)
        event_id = body.get("id") if isinstance(body, dict) else None
        if not event_id:
            raise OutreachError("Luma created the event but returned no id")
        return str(event_id)

    async def update_event(self, event_id: str, payload: dict[str, Any]) -> None:
        await self._request("POST", UPDATE_EVENT_PATH, json={"event_id": event_id, **payload})

    async def get_event(self, event_id: str) -> dict[str, Any]:
        body = await self._request("GET", GET_EVENT_PATH, params={"event_id": event_id})
        return body if isinstance(body, dict) else {}

    async def add_guests(self, event_id: str, emails: list[str], *, send_email: bool = True) -> list[str]:
        """Register guests as going (Luma emails them by default); returns the emails Luma skipped."""
        body = {"event_id": event_id, "guests": [{"email": e} for e in emails], "send_email": send_email}
        return _skipped(await self._request("POST", ADD_GUESTS_PATH, json=body))

    async def send_invites(self, event_id: str, emails: list[str], *, message: str | None = None) -> list[str]:
        """Have Luma email an invitation each guest can accept; returns the emails Luma skipped."""
        body: dict[str, Any] = {"event_id": event_id, "guests": [{"email": e} for e in emails]}
        if message:
            body["message"] = message[:200]
        return _skipped(await self._request("POST", SEND_INVITES_PATH, json=body))


def _skipped(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    return [item["email"] for item in body.get("skipped", []) if isinstance(item, dict) and "email" in item]
