"""Registration, login and the current user.

Emails are stored lowercased, so lookups are case-insensitive by construction.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.deps import Ctx, CurrentUser, Session
from app.core.security import create_access_token, hash_password, verify_password
from app.models import User
from app.schemas.common import UserOut

router = APIRouter(tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


def _token_out(settings: Settings, user: User) -> TokenOut:
    return TokenOut(access_token=create_access_token(settings, user.id), user=UserOut.model_validate(user))


@router.post("/auth/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, ctx: Ctx, session: Session):
    """Create an account and sign it in. 409 if the email is already registered."""
    email = body.email.lower()
    taken = HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    if (await session.execute(select(User.id).where(User.email == email))).first() is not None:
        raise taken
    # bcrypt is deliberately slow; keep it off the event loop that also carries realtime traffic.
    password_hash = await asyncio.to_thread(hash_password, body.password)
    user = User(email=email, name=body.name.strip(), password_hash=password_hash)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # registered concurrently by another request
        raise taken from None
    return _token_out(ctx.settings, user)


@router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn, ctx: Ctx, session: Session):
    """Exchange credentials for an access token. Unknown email and wrong password get the same 401."""
    user = (await session.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    password_hash = user.password_hash if user else None
    if not await asyncio.to_thread(verify_password, body.password, password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password", {"WWW-Authenticate": "Bearer"})
    return _token_out(ctx.settings, user)


@router.get("/auth/me", response_model=UserOut)
async def me(user: CurrentUser):
    """The caller: the token's user, or the implicit dev user when auth is optional and no token is sent."""
    return user
