"""Outreach exceptions.

Kept in their own module so the provider modules and the `outreach` entrypoint can
share them without an import cycle. `OutreachError` is re-exported from
`app.integrations.outreach`, which is where other packages import it from.
"""

from __future__ import annotations


class OutreachError(Exception):
    """A provider failed or is not configured; the message says what to fix."""


class OutreachStateError(Exception):
    """The requested action is not allowed in the record's current status (e.g. editing a sent email)."""
