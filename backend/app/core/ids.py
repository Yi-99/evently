"""Prefixed random ids (`evt_…`, `card_…`) and UTC time helpers."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

_JOIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def new_join_code(length: int = 6) -> str:
    return "".join(secrets.choice(_JOIN_ALPHABET) for _ in range(length))


def utcnow() -> datetime:
    return datetime.now(UTC)
