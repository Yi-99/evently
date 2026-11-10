"""FastAPI dependencies: context, DB session, current user, owned event."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.core.context import AppContext
from app.core.security import TokenError, decode_access_token
from app.models import Event, User

DEV_USER_EMAIL = "dev@evently.local"
_bearer = HTTPBearer(auto_error=False)


def get_ctx(conn: HTTPConnection) -> AppContext:
    """Works for both HTTP requests and WebSockets."""
    return conn.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def get_session(ctx: Ctx) -> AsyncIterator[AsyncSession]:
    async with ctx.db() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def get_dev_user(ctx: AppContext) -> User:
    """The implicit local user used when auth is optional and no token is sent."""
    async with ctx.db() as session:
        user = (await session.execute(select(User).where(User.email == DEV_USER_EMAIL))).scalar_one_or_none()
        if user is None:
            user = User(email=DEV_USER_EMAIL, name="Dev User")
            session.add(user)
            try:
                await session.commit()
            except IntegrityError:  # created concurrently by another request
                await session.rollback()
                user = (await session.execute(select(User).where(User.email == DEV_USER_EMAIL))).scalar_one()
        return user


async def resolve_user(ctx: AppContext, token: str | None) -> User | None:
    """User for an access token; the dev user when auth is optional; else None."""
    if token:
        try:
            user_id = decode_access_token(ctx.settings, token)
        except TokenError:
            return None
        async with ctx.db() as session:
            return await session.get(User, user_id)
    if ctx.settings.is_auth_required:
        return None
    return await get_dev_user(ctx)


async def get_current_user(
    ctx: Ctx, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> User:
    user = await resolve_user(ctx, credentials.credentials if credentials else None)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", {"WWW-Authenticate": "Bearer"})
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def load_owned_event(ctx: AppContext, event_id: str, user: User) -> Event | None:
    async with ctx.db() as session:
        event = await session.get(Event, event_id)
    if event is None or event.owner_id != user.id:
        return None
    return event


async def get_owned_event(event_id: str, ctx: Ctx, user: CurrentUser) -> Event:
    """Path dependency: the event must exist and belong to the caller (404 otherwise)."""
    event = await load_owned_event(ctx, event_id, user)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


OwnedEvent = Annotated[Event, Depends(get_owned_event)]


async def ws_user(websocket: WebSocket) -> User | None:
    """Authenticate a WebSocket from `?token=` (or an Authorization header). None = reject."""
    token = websocket.query_params.get("token")
    if not token:
        header = websocket.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
    return await resolve_user(get_ctx(websocket), token)
