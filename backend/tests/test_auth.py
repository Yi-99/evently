"""Auth API: register, login, current user, and enforcement when auth is required."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.deps import DEV_USER_EMAIL
from app.core.security import decode_access_token
from app.main import create_app

API = "/api/v1"
ADA = {"email": "ada@example.com", "password": "correct-horse", "name": "Ada"}


@pytest.fixture
def strict_client(settings, llm) -> Iterator[TestClient]:
    """A client for an app where every request needs a token."""
    app = create_app(settings.model_copy(update={"auth_required": True}), llm=llm)
    with TestClient(app) as test_client:
        yield test_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_register_returns_token_and_user(client):
    res = client.post(f"{API}/auth/register", json=ADA)
    assert res.status_code == 201
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["user"] == {"id": body["user"]["id"], "email": "ada@example.com", "name": "Ada"}
    assert decode_access_token(client.app.state.ctx.settings, body["access_token"]) == body["user"]["id"]

    me = client.get(f"{API}/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200 and me.json() == body["user"]


def test_register_name_is_optional_and_email_is_lowercased(client):
    res = client.post(f"{API}/auth/register", json={"email": "Grace@Example.COM", "password": "long-enough"})
    assert res.status_code == 201
    assert res.json()["user"]["email"] == "grace@example.com" and res.json()["user"]["name"] == ""


def test_register_rejects_taken_email_case_insensitively(client):
    assert client.post(f"{API}/auth/register", json=ADA).status_code == 201
    res = client.post(f"{API}/auth/register", json={**ADA, "email": "ADA@example.com"})
    assert res.status_code == 409 and res.json()["detail"] == "Email already registered"


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "ada@example.com", "password": "short"},
        {"email": "not-an-email", "password": "correct-horse"},
        {"email": "ada@example.com"},
        # The implicit dev user's address must never become a password account.
        {"email": DEV_USER_EMAIL, "password": "correct-horse"},
    ],
)
def test_register_validates_input(client, payload):
    assert client.post(f"{API}/auth/register", json=payload).status_code == 422


def test_login_returns_token_for_the_registered_user(client):
    registered = client.post(f"{API}/auth/register", json=ADA).json()
    res = client.post(f"{API}/auth/login", json={"email": "Ada@Example.com", "password": ADA["password"]})
    assert res.status_code == 200
    assert res.json()["token_type"] == "bearer" and res.json()["user"] == registered["user"]
    me = client.get(f"{API}/auth/me", headers=bearer(res.json()["access_token"]))
    assert me.json()["email"] == "ada@example.com"


def test_login_failures_share_one_message(client):
    client.post(f"{API}/auth/register", json=ADA)
    wrong_password = client.post(f"{API}/auth/login", json={"email": ADA["email"], "password": "wrong-password"})
    unknown_email = client.post(f"{API}/auth/login", json={"email": "nobody@example.com", "password": "whatever-1"})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": "Invalid email or password"}


def test_me_is_the_dev_user_when_auth_is_optional(client):
    res = client.get(f"{API}/auth/me")
    assert res.status_code == 200 and res.json()["email"] == DEV_USER_EMAIL


def test_auth_required_rejects_missing_and_invalid_tokens(strict_client):
    assert strict_client.get(f"{API}/auth/me").status_code == 401
    assert strict_client.get(f"{API}/auth/me", headers=bearer("not-a-jwt")).status_code == 401
    assert strict_client.get(f"{API}/events").status_code == 401
    assert strict_client.post(f"{API}/events", json={"name": "Offsite"}).status_code == 401


def test_auth_required_accepts_a_registered_users_token(strict_client):
    token = strict_client.post(f"{API}/auth/register", json=ADA).json()["access_token"]
    me = strict_client.get(f"{API}/auth/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["email"] == "ada@example.com"
