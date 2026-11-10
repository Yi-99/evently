"""Password hashing and JWTs (user access tokens and live-session participant tokens)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import bcrypt
import jwt

from app.core.config import Settings
from app.core.ids import utcnow

ALGORITHM = "HS256"


class TokenError(Exception):
    """Token is missing, malformed, expired or of the wrong type."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("ascii"))
    except ValueError:
        return False


def _encode(settings: Settings, claims: dict, ttl_minutes: int) -> str:
    now = utcnow()
    payload = {**claims, "iat": now, "exp": now + timedelta(minutes=ttl_minutes)}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def _decode(settings: Settings, token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("typ") != expected_type:
        raise TokenError(f"expected a {expected_type} token")
    return payload


def create_access_token(settings: Settings, user_id: str) -> str:
    return _encode(settings, {"sub": user_id, "typ": "access"}, settings.access_token_ttl_minutes)


def decode_access_token(settings: Settings, token: str) -> str:
    """Return the user id carried by a valid access token."""
    return _decode(settings, token, "access")["sub"]


@dataclass(frozen=True)
class ParticipantClaims:
    participant_id: str
    session_id: str
    role: str  # host | speaker | listener


def create_participant_token(settings: Settings, *, participant_id: str, session_id: str, role: str) -> str:
    claims = {"sub": participant_id, "sid": session_id, "role": role, "typ": "participant"}
    return _encode(settings, claims, settings.participant_token_ttl_minutes)


def decode_participant_token(settings: Settings, token: str) -> ParticipantClaims:
    payload = _decode(settings, token, "participant")
    return ParticipantClaims(participant_id=payload["sub"], session_id=payload["sid"], role=payload["role"])
