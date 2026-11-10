"""LiveKit (WebRTC) access tokens for rooms too large for the built-in WebSocket relay.

A LiveKit access token is a JWT signed with the API secret (HS256) whose `video` grant holds
the room permissions (https://docs.livekit.io/frontends/reference/tokens-grants/). The room is
the live-session id; clients that may control the session may publish their microphone,
everyone may subscribe.
"""

from __future__ import annotations

from datetime import timedelta

import jwt
from pydantic import BaseModel

from app.core.config import Settings
from app.core.ids import utcnow
from app.live.service import SessionClient

TOKEN_TTL = timedelta(hours=1)  # only checked at connect time; LiveKit refreshes tokens of connected clients


class LiveKitTokenOut(BaseModel):
    url: str
    token: str
    room: str
    identity: str


def livekit_configured(settings: Settings) -> bool:
    return bool(settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret)


def livekit_identity(client: SessionClient) -> str:
    """Stable, room-unique identity: the participant id, else the owner's user id."""
    if client.participant_id:
        return client.participant_id
    if client.user_id:
        return f"user_{client.user_id}"
    return "host"


def create_livekit_token(
    settings: Settings,
    *,
    room: str,
    identity: str,
    name: str,
    can_publish: bool,
    ttl: timedelta = TOKEN_TTL,
) -> str:
    """Mint a join token for `room`: subscribe always, publish (microphone only) when `can_publish`."""
    if not livekit_configured(settings):
        raise RuntimeError("LiveKit is not configured")
    now = utcnow()
    grant: dict[str, object] = {
        "room": room,
        "roomJoin": True,
        "canSubscribe": True,
        "canPublish": can_publish,
        "canPublishData": False,
    }
    if can_publish:
        grant["canPublishSources"] = ["microphone"]
    claims = {
        "iss": settings.livekit_api_key,
        "sub": identity,
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "name": name,
        "video": grant,
    }
    return jwt.encode(claims, settings.livekit_api_secret, algorithm="HS256")


def livekit_join(settings: Settings, client: SessionClient) -> LiveKitTokenOut:
    """Connection details for `client` to join its session's LiveKit room. Raises `RuntimeError` when unconfigured."""
    identity = livekit_identity(client)
    token = create_livekit_token(
        settings,
        room=client.session_id,
        identity=identity,
        name=client.display_name,
        can_publish=client.can_control,
    )
    return LiveKitTokenOut(url=settings.livekit_url or "", token=token, room=client.session_id, identity=identity)
