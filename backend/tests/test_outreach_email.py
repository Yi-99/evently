"""Outreach: email providers, the draft → sending → sent/failed status machine, board mirroring."""

from __future__ import annotations

import smtplib

import httpx
import pytest

from app.integrations import outreach
from app.integrations.email_providers import (
    RESEND_SEND_URL,
    ConsoleProvider,
    OutgoingEmail,
    ResendProvider,
    SmtpProvider,
    build_email_provider,
)
from app.integrations.outreach import OutreachError, OutreachStateError
from app.realtime import events as ev
from app.schemas.outreach import EmailDraftIn
from app.services import board
from tests.helpers import published


def draft_in(**overrides) -> EmailDraftIn:
    base = {
        "kind": "venue_inquiry",
        "to": ["events@venue.example"],
        "cc": ["boss@acme.example"],
        "reply_to": "planner@acme.example",
        "subject": "Availability on 12 March?",
        "body_text": "Hi, we are planning an offsite for 40 people...",
        "body_html": "<p>Hi, we are planning an offsite for 40 people...</p>",
    }
    return EmailDraftIn(**{**base, **overrides})


def message(**overrides) -> OutgoingEmail:
    base = {
        "sender": "Evently <events@acme.example>",
        "to": ["events@venue.example"],
        "cc": ["boss@acme.example"],
        "reply_to": "planner@acme.example",
        "subject": "Availability?",
        "body_text": "Hello",
        "body_html": "<p>Hello</p>",
        "idempotency_key": "eml_1",
    }
    return OutgoingEmail(**{**base, **overrides})


class FakeSmtp:
    """Stands in for `smtplib.SMTP`; records the calls and optionally fails."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.sent = []
        self.fail = fail

    def __enter__(self):
        self.calls.append(("connect",))
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)


class FailingProvider:
    name = "resend"

    async def send(self, message: OutgoingEmail) -> str:
        raise OutreachError("Resend rejected the email (422): invalid `from` domain")


def install_provider(ctx, provider):
    return ctx.service(outreach.EMAIL_PROVIDER_SERVICE, lambda: provider)


# --- provider selection -------------------------------------------------------------


def test_build_provider_auto_prefers_resend_then_smtp_then_console(settings):
    assert isinstance(build_email_provider(settings), ConsoleProvider)
    smtp = settings.model_copy(update={"smtp_host": "smtp.gmail.com", "email_from": "a@b.example"})
    assert isinstance(build_email_provider(smtp), SmtpProvider)
    both = smtp.model_copy(update={"resend_api_key": "re_123"})
    assert isinstance(build_email_provider(both), ResendProvider)
    forced = both.model_copy(update={"email_provider": "console"})
    assert isinstance(build_email_provider(forced), ConsoleProvider)


def test_build_provider_errors_name_the_missing_variable(settings):
    with pytest.raises(OutreachError, match="EMAIL_FROM"):
        build_email_provider(settings.model_copy(update={"resend_api_key": "re_123"}))
    with pytest.raises(OutreachError, match="RESEND_API_KEY"):
        build_email_provider(settings.model_copy(update={"email_provider": "resend", "email_from": "a@b.example"}))
    with pytest.raises(OutreachError, match="SMTP_HOST"):
        build_email_provider(settings.model_copy(update={"email_provider": "smtp", "email_from": "a@b.example"}))
    with pytest.raises(OutreachError, match="RESEND_API_KEY or SMTP_HOST"):
        build_email_provider(settings.model_copy(update={"environment": "production"}))


# --- Resend -----------------------------------------------------------------------------


@pytest.mark.respx(assert_all_mocked=True)
async def test_resend_request_shape_and_message_id(respx_mock):
    route = respx_mock.post(RESEND_SEND_URL).mock(return_value=httpx.Response(200, json={"id": "49a3999c-0ce1"}))
    provider = ResendProvider("re_test")
    assert await provider.send(message()) == "49a3999c-0ce1"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer re_test"
    assert request.headers["Idempotency-Key"] == "eml_1"
    import json

    assert json.loads(request.content) == {
        "from": "Evently <events@acme.example>",
        "to": ["events@venue.example"],
        "cc": ["boss@acme.example"],
        "reply_to": "planner@acme.example",
        "subject": "Availability?",
        "text": "Hello",
        "html": "<p>Hello</p>",
    }
    await provider.aclose()


@pytest.mark.respx(assert_all_mocked=True)
async def test_resend_error_surfaces_lumas_message(respx_mock):
    body = {"statusCode": 403, "name": "validation_error", "message": "The acme.example domain is not verified"}
    respx_mock.post(RESEND_SEND_URL).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(OutreachError, match="403.*domain is not verified"):
        await ResendProvider("re_test").send(message())


@pytest.mark.respx(assert_all_mocked=True)
async def test_resend_transport_error(respx_mock):
    respx_mock.post(RESEND_SEND_URL).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(OutreachError, match="Resend request failed"):
        await ResendProvider("re_test").send(message())


# --- SMTP --------------------------------------------------------------------------------


async def test_smtp_starttls_login_and_mime():
    fake = FakeSmtp()
    provider = SmtpProvider("smtp.gmail.com", 587, username="me@gmail.com", password="app-pass", connect=lambda: fake)
    message_id = await provider.send(message())
    assert fake.calls == [("connect",), ("starttls",), ("login", "me@gmail.com", "app-pass"), ("quit",)]
    mime = fake.sent[0]
    assert mime["To"] == "events@venue.example" and mime["Cc"] == "boss@acme.example"
    assert mime["Reply-To"] == "planner@acme.example" and mime["Message-ID"] == message_id
    assert message_id.endswith("@acme.example>")
    assert mime.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in mime.iter_parts()] == ["text/plain", "text/html"]


async def test_smtp_relay_without_auth_or_tls_and_failure():
    fake = FakeSmtp()
    provider = SmtpProvider("relay.internal", 25, starttls=False, connect=lambda: fake)
    await provider.send(message(cc=[], reply_to=None, body_html=None))
    assert fake.calls == [("connect",), ("quit",)] and fake.sent[0].get_content_type() == "text/plain"

    failing = SmtpProvider("relay.internal", 25, connect=lambda: FakeSmtp(fail=smtplib.SMTPRecipientsRefused({})))
    with pytest.raises(OutreachError, match="SMTP send via relay.internal:25 failed"):
        await failing.send(message())


def test_smtp_implicit_tls_on_465_skips_starttls():
    provider = SmtpProvider("smtp.example", 465, starttls=True)
    assert provider._starttls is False


# --- Console -------------------------------------------------------------------------------


async def test_console_provider_logs_and_returns_fake_id(caplog):
    provider = ConsoleProvider()
    with caplog.at_level("INFO"):
        message_id = await provider.send(message())
    assert provider.sent == [message()] and message_id.endswith("@console.evently.local>")
    assert "Availability?" in caplog.text


# --- service: drafts -----------------------------------------------------------------------


async def test_draft_email_creates_card_and_publishes(ctx, event):
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in(), created_by="agent")
    assert row.status == "draft" and row.created_by == "agent" and row.card_id

    card = await board.get_card(ctx, row.card_id)
    assert card.kind == "email" and card.title == row.subject and card.created_by == "agent"
    assert card.data["id"] == row.id and card.data["status"] == "draft" and card.data["to"] == list(row.to)

    envelopes = published(ctx, event.id, "email.")
    assert [e.type for e in envelopes] == [ev.EMAIL_UPDATED]
    assert envelopes[0].data["email"]["card_id"] == card.id
    assert [e.type for e in published(ctx, event.id, "board.card")] == [ev.BOARD_CARD_CREATED, ev.BOARD_CARD_UPDATED]


async def test_update_email_only_while_draft(ctx, event):
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in())
    row = await outreach.update_email(ctx, row.id, changes={"subject": "New subject", "cc": []})
    assert row.subject == "New subject" and row.cc == [] and row.body_text.startswith("Hi")
    assert (await board.get_card(ctx, row.card_id)).title == "New subject"

    with pytest.raises(Exception, match="to"):  # pydantic: an empty recipient list is invalid
        await outreach.update_email(ctx, row.id, changes={"to": []})
    with pytest.raises(ValueError, match="unknown email fields"):
        await outreach.update_email(ctx, row.id, changes={"status": "sent"})

    install_provider(ctx, ConsoleProvider())
    await outreach.send_email(ctx, row.id)
    with pytest.raises(OutreachStateError):
        await outreach.update_email(ctx, row.id, changes={"subject": "too late"})
    with pytest.raises(OutreachStateError):
        await outreach.delete_email(ctx, row.id)
    with pytest.raises(LookupError):
        await outreach.update_email(ctx, "eml_missing", changes={"subject": "x"})


async def test_delete_draft_removes_card_and_publishes(ctx, event):
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in())
    await outreach.delete_email(ctx, row.id)
    assert await outreach.get_email(ctx, row.id) is None
    assert await board.get_card(ctx, row.card_id) is None
    assert published(ctx, event.id, "email.")[-1].type == ev.EMAIL_DELETED
    assert published(ctx, event.id, "email.")[-1].data == {"email_id": row.id}


# --- service: sending ---------------------------------------------------------------------


async def test_send_email_status_machine_and_envelopes(ctx, event):
    provider = install_provider(ctx, ConsoleProvider())
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in())
    sent = await outreach.send_email(ctx, row.id)

    assert sent.status == "sent" and sent.provider == "console" and sent.sent_at is not None
    assert sent.provider_message_id and sent.error is None
    out = provider.sent[0]
    assert out.to == ["events@venue.example"] and out.idempotency_key == row.id and out.sender.startswith("Evently")

    statuses = [e.data["email"]["status"] for e in published(ctx, event.id, ev.EMAIL_UPDATED)]
    assert statuses == ["draft", "sending", "sent"]
    card = await board.get_card(ctx, row.card_id)
    assert card.status == "ready" and card.data["status"] == "sent" and card.data["provider_message_id"]
    card_statuses = [e.data["card"]["status"] for e in published(ctx, event.id, ev.BOARD_CARD_UPDATED)]
    assert card_statuses[-2:] == ["running", "ready"]


async def test_send_email_is_idempotent(ctx, event):
    provider = install_provider(ctx, ConsoleProvider())
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in())
    first = await outreach.send_email(ctx, row.id)
    second = await outreach.send_email(ctx, row.id)
    assert len(provider.sent) == 1
    assert second.status == "sent" and second.provider_message_id == first.provider_message_id
    assert len(published(ctx, event.id, ev.EMAIL_UPDATED)) == 3  # no extra envelope for the no-op


async def test_send_email_failure_marks_failed_and_can_retry(ctx, event):
    install_provider(ctx, FailingProvider())
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in())
    failed = await outreach.send_email(ctx, row.id)
    assert failed.status == "failed" and "domain" in failed.error and failed.sent_at is None
    assert (await board.get_card(ctx, row.card_id)).status == "error"
    assert [e.data["email"]["status"] for e in published(ctx, event.id, ev.EMAIL_UPDATED)] == [
        "draft",
        "sending",
        "failed",
    ]

    ctx._services[outreach.EMAIL_PROVIDER_SERVICE] = ConsoleProvider()
    retried = await outreach.send_email(ctx, row.id)
    assert retried.status == "sent" and retried.error is None and retried.provider == "console"


async def test_send_email_with_resend_provider_end_to_end(ctx, event, respx_mock):
    respx_mock.post(RESEND_SEND_URL).mock(return_value=httpx.Response(200, json={"id": "msg_resend"}))
    ctx.settings = ctx.settings.model_copy(update={"resend_api_key": "re_x", "email_from": "Evently <e@acme.example>"})
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in())
    sent = await outreach.send_email(ctx, row.id)
    assert sent.status == "sent" and sent.provider == "resend" and sent.provider_message_id == "msg_resend"


async def test_send_email_without_usable_provider_raises_and_keeps_draft(ctx, event):
    ctx.settings = ctx.settings.model_copy(update={"resend_api_key": "re_x"})  # EMAIL_FROM missing
    row = await outreach.draft_email(ctx, event_id=event.id, draft=draft_in())
    with pytest.raises(OutreachError, match="EMAIL_FROM"):
        await outreach.send_email(ctx, row.id)
    assert (await outreach.get_email(ctx, row.id)).status == "draft"
    with pytest.raises(LookupError):
        await outreach.send_email(ctx, "eml_missing")
