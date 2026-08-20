"""Reads alert emails from the dedicated Gmail account over IMAP.

App-password IMAP rather than OAuth: GitHub Actions is headless, so an OAuth
flow would mean minting and refreshing a token out of band. One secret, no
refresh, and it behaves identically on a laptop and in CI.

Processed messages are flagged \\Seen rather than deleted, so a run that dies
after fetching but before committing state loses nothing — but a message is
only flagged once the caller confirms it was handled.
"""

from __future__ import annotations

import email
import imaplib
import logging
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


@dataclass
class AlertEmail:
    uid: str
    sender: str
    subject: str
    html: str
    text: str


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_bodies(message: Message) -> tuple[str, str]:
    """Return (html, text) — either may be empty."""
    html_parts: list[str] = []
    text_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            content_type = part.get_content_type()
            if content_type == "text/html":
                html_parts.append(_decode_part(part))
            elif content_type == "text/plain":
                text_parts.append(_decode_part(part))
    else:
        body = _decode_part(message)
        if message.get_content_type() == "text/html":
            html_parts.append(body)
        else:
            text_parts.append(body)

    return "\n".join(html_parts), "\n".join(text_parts)


class GmailClient:
    """Context manager wrapping one IMAP session."""

    def __init__(self, address: str, app_password: str, mailbox: str = "INBOX"):
        self.address = address
        self.app_password = app_password
        self.mailbox = mailbox
        self._imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "GmailClient":
        self._imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        self._imap.login(self.address, self.app_password)
        self._imap.select(self.mailbox)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._imap is None:
            return
        try:
            self._imap.close()
        except Exception:
            pass
        try:
            self._imap.logout()
        except Exception:
            pass
        self._imap = None

    def fetch_unseen(self, limit: int | None = None) -> list[AlertEmail]:
        """Fetch unread messages without marking them read.

        BODY.PEEK keeps the \\Seen flag off so an interrupted run can be retried
        against the same messages; call `mark_processed` once they're stored.
        """
        assert self._imap is not None, "use GmailClient as a context manager"

        status, data = self._imap.search(None, "UNSEEN")
        if status != "OK":
            log.warning("IMAP search failed: %s", status)
            return []

        uids = data[0].split()
        if limit is not None:
            uids = uids[:limit]

        emails: list[AlertEmail] = []
        for uid in uids:
            status, payload = self._imap.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("could not fetch message %s", uid)
                continue

            message = email.message_from_bytes(payload[0][1])
            html, text = _extract_bodies(message)
            emails.append(
                AlertEmail(
                    uid=uid.decode(),
                    sender=_decode_header_value(message.get("From")),
                    subject=_decode_header_value(message.get("Subject")),
                    html=html,
                    text=text,
                )
            )

        log.info("fetched %d unread message(s)", len(emails))
        return emails

    def mark_processed(self, uids: list[str]) -> None:
        """Flag messages as read. Never deletes — the mailbox stays auditable."""
        assert self._imap is not None, "use GmailClient as a context manager"
        if not uids:
            return
        self._imap.store(",".join(uids), "+FLAGS", "\\Seen")
        log.info("marked %d message(s) as read", len(uids))


if __name__ == "__main__":  # setup check: python -m scout.gmail_client
    import sys

    from . import configure_stdio
    from .config import load_settings
    from .parsers import site_for_sender

    configure_stdio()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()

    if not settings.gmail_enabled:
        print("GMAIL_ADDRESS or GMAIL_APP_PASSWORD missing — check .env")
        sys.exit(2)

    print(f"connecting to {IMAP_HOST} as {settings.gmail_address}")
    try:
        with GmailClient(settings.gmail_address, settings.gmail_app_password) as gmail:
            # PEEK only: this leaves every message unread for the real run.
            unread = gmail.fetch_unseen()
    except (imaplib.IMAP4.error, OSError) as exc:
        print(f"login or mailbox failed: {exc}")
        print(
            "usual causes: spaces left in the app password, an address without "
            "@gmail.com, or 2-Step Verification turned off"
        )
        sys.exit(1)

    for message in unread:
        print(
            f"  [{site_for_sender(message.sender)}] "
            f"{message.subject[:70] or '(no subject)'}"
        )
    print(f"{len(unread)} unread message(s), still unread — nothing was marked")
