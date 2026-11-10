"""Email providers: Resend (HTTP), SMTP (STARTTLS / implicit TLS, optional auth) and Console (dev).

`build_email_provider(settings)` picks one from `Settings.email_provider`; `send_email` in
`app.integrations.outreach` is the only caller. Every provider implements `send` and raises
`OutreachError` with a human-readable message when the message cannot be delivered.

Resend wire details (verified against https://resend.com/docs/api-reference/emails/send-email):
`POST https://api.resend.com/emails`, `Authorization: Bearer <key>`, JSON body with `from`, `to`
(array), `cc` (array), `reply_to`, `subject`, `text`, `html`; optional `Idempotency-Key` header;
200 response `{"id": "<message id>"}`; errors carry a `message` string.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.integrations.errors import OutreachError

log = logging.getLogger(__name__)

RESEND_SEND_URL = "https://api.resend.com/emails"


@dataclass
class OutgoingEmail:
    """Provider-neutral message. `sender` comes from `EMAIL_FROM`; `idempotency_key` is our row id."""

    sender: str
    to: list[str]
    subject: str
    body_text: str
    cc: list[str] = field(default_factory=list)
    reply_to: str | None = None
    body_html: str | None = None
    idempotency_key: str | None = None


class EmailProvider(Protocol):
    name: str

    async def send(self, message: OutgoingEmail) -> str:
        """Deliver the message; returns the provider's message id. Raises `OutreachError` on failure."""
        ...


# --- Resend ----------------------------------------------------------------------


class ResendProvider:
    name = "resend"

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None, timeout: float = 20.0) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def payload(message: OutgoingEmail) -> dict[str, Any]:
        body: dict[str, Any] = {
            "from": message.sender,
            "to": message.to,
            "subject": message.subject,
            "text": message.body_text,
        }
        if message.cc:
            body["cc"] = message.cc
        if message.reply_to:
            body["reply_to"] = message.reply_to
        if message.body_html:
            body["html"] = message.body_html
        return body

    async def send(self, message: OutgoingEmail) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if message.idempotency_key:
            headers["Idempotency-Key"] = message.idempotency_key
        try:
            response = await self._client.post(RESEND_SEND_URL, json=self.payload(message), headers=headers)
        except httpx.HTTPError as exc:
            raise OutreachError(f"Resend request failed: {exc}") from exc
        if response.status_code >= 400:
            raise OutreachError(f"Resend rejected the email ({response.status_code}): {_error_message(response)}")
        message_id = response.json().get("id")
        if not message_id:
            raise OutreachError("Resend returned no message id")
        return str(message_id)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            if isinstance(body.get(key), str):
                return body[key]
    return response.text.strip()


# --- SMTP ------------------------------------------------------------------------


class SmtpSession(Protocol):
    """The part of `smtplib.SMTP` we use; tests inject a fake."""

    def __enter__(self) -> SmtpSession: ...
    def __exit__(self, *exc: object) -> None: ...
    def starttls(self) -> Any: ...
    def login(self, user: str, password: str) -> Any: ...
    def send_message(self, msg: MimeMessage) -> Any: ...


class SmtpProvider:
    """Plain SMTP. Port 465 means implicit TLS; otherwise STARTTLS when `starttls` is set (Gmail: 587, app password)."""

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int = 587,
        *,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        timeout: float = 30.0,
        connect: Callable[[], SmtpSession] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._starttls = starttls and port != 465
        self._connect = connect or self._default_connect
        self._timeout = timeout

    def _default_connect(self) -> SmtpSession:
        cls = smtplib.SMTP_SSL if self._port == 465 else smtplib.SMTP
        return cls(self._host, self._port, timeout=self._timeout)

    @staticmethod
    def mime(message: OutgoingEmail, message_id: str) -> MimeMessage:
        msg = MimeMessage()
        msg["From"] = message.sender
        msg["To"] = ", ".join(message.to)
        if message.cc:
            msg["Cc"] = ", ".join(message.cc)
        if message.reply_to:
            msg["Reply-To"] = message.reply_to
        msg["Subject"] = message.subject
        msg["Message-ID"] = message_id
        msg.set_content(message.body_text)
        if message.body_html:
            msg.add_alternative(message.body_html, subtype="html")
        return msg

    def _send_blocking(self, msg: MimeMessage) -> None:
        with self._connect() as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password or "")
            smtp.send_message(msg)

    async def send(self, message: OutgoingEmail) -> str:
        message_id = make_msgid(domain=_domain_of(message.sender))
        try:
            await asyncio.to_thread(self._send_blocking, self.mime(message, message_id))
        except (smtplib.SMTPException, OSError) as exc:
            raise OutreachError(f"SMTP send via {self._host}:{self._port} failed: {exc}") from exc
        return message_id


def _domain_of(address: str) -> str | None:
    # "Name <user@host>" or "user@host"; None lets make_msgid use the local hostname.
    _, _, rest = address.rpartition("@")
    domain = rest.rstrip(">").strip()
    return domain or None


# --- Console ---------------------------------------------------------------------


class ConsoleProvider:
    """Logs the message instead of sending it. Default in development when nothing is configured."""

    name = "console"

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    async def send(self, message: OutgoingEmail) -> str:
        self.sent.append(message)
        log.info(
            "[console email] from=%s to=%s cc=%s subject=%r\n%s",
            message.sender,
            message.to,
            message.cc,
            message.subject,
            message.body_text,
        )
        return make_msgid(domain="console.evently.local")


# --- selection -------------------------------------------------------------------


def resolve_email_provider_name(settings: Settings) -> str:
    """The provider `build_email_provider` would pick, without building it (cheap; for /health).

    An explicit `EMAIL_PROVIDER` is returned as is. "auto" resolves to "resend" when
    `RESEND_API_KEY` is set, else "smtp" when `SMTP_HOST` is set, else "console" — or
    "none" in production, where Console is never chosen automatically.
    """
    if settings.email_provider != "auto":
        return settings.email_provider
    if settings.resend_api_key:
        return "resend"
    if settings.smtp_host:
        return "smtp"
    return "none" if settings.environment == "production" else "console"


def build_email_provider(settings: Settings) -> EmailProvider:
    """Provider for `settings.email_provider`.

    "auto": Resend when `RESEND_API_KEY` is set, else SMTP when `SMTP_HOST` is set, else Console
    (Console is never chosen automatically in production). A real provider without `EMAIL_FROM`,
    or an explicit choice missing its credentials, raises `OutreachError` naming the variable.
    """
    choice = resolve_email_provider_name(settings)
    if choice == "none":
        raise OutreachError("No email provider configured: set RESEND_API_KEY or SMTP_HOST (and EMAIL_FROM)")

    if choice == "console":
        return ConsoleProvider()
    if not settings.email_from:
        raise OutreachError(f"EMAIL_FROM must be set to send email through {choice}")
    if choice == "resend":
        if not settings.resend_api_key:
            raise OutreachError("EMAIL_PROVIDER=resend but RESEND_API_KEY is not set")
        return ResendProvider(settings.resend_api_key)
    if not settings.smtp_host:
        raise OutreachError("EMAIL_PROVIDER=smtp but SMTP_HOST is not set")
    return SmtpProvider(
        settings.smtp_host,
        settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
    )


def sender_address(settings: Settings, provider: EmailProvider) -> str:
    """`EMAIL_FROM`, or a placeholder for the console provider so drafts can be exercised without config."""
    return settings.email_from or f"Evently <no-reply@{provider.name}.evently.local>"
