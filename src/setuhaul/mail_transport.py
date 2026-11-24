"""Email transport for partner requests: Gmail (or any) SMTP to send, IMAP to read replies, and an in-memory twin for demos/tests."""

# Lazy annotations.
from __future__ import annotations

# hashlib for a stable id when a mail has no Message-ID.
import hashlib

# imaplib/smtplib are the stdlib clients (patched in tests; the in-memory twin never touches them).
import imaplib
import smtplib

# uuid for the config-check token.
import uuid

# Callable types the injected sleep.
from collections.abc import Callable

# Message building and parsing.
from email import message_from_bytes, policy

# EmailMessage builds the outgoing mail.
from email.message import EmailMessage, Message

# parseaddr gives the bare sender address.
from email.utils import parseaddr

# Protocol makes sender/reader structural types.
from typing import Protocol

# pydantic value objects.
from pydantic import BaseModel, ConfigDict

# The settings object.
from setuhaul.mailcfg import MailSettings

# Seconds before a mail server call gives up.
_TIMEOUT_S = 30


class MailDeliveryError(Exception):
    """Sending or reading mail failed. The text never contains credentials."""


class OutgoingMail(BaseModel):
    model_config = ConfigDict(frozen=True)

    to_addr: str
    subject: str
    body: str
    message_id: str


class IncomingMail(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    from_addr: str
    subject: str
    in_reply_to: str | None
    body: str
    authentication_results: str | None


class MailSender(Protocol):
    def send(self, mail: OutgoingMail) -> None: ...


class MailReader(Protocol):
    def fetch_unseen(self) -> list[IncomingMail]: ...
    def mark_seen(self, message_id: str) -> None: ...
    def close(self) -> None: ...


class SmtpSender:
    """Sends over TLS: port 465 is implicit SSL, anything else (587) upgrades with STARTTLS."""

    def __init__(self, settings: MailSettings) -> None:
        self._s = settings

    def send(self, mail: OutgoingMail) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = self._s.smtp_from, mail.to_addr, mail.subject, mail.message_id
        msg.set_content(mail.body)
        try:
            if self._s.smtp_port == 465:
                client: smtplib.SMTP = smtplib.SMTP_SSL(self._s.smtp_host, self._s.smtp_port, timeout=_TIMEOUT_S)
            else:
                client = smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=_TIMEOUT_S)
            with client:
                if self._s.smtp_port != 465:
                    client.starttls()
                client.login(self._s.smtp_user, self._s.smtp_password)
                client.send_message(msg)
        except Exception as exc:  # noqa: BLE001 - library text can echo credentials, so only the type is kept
            raise MailDeliveryError(f"SMTP {type(exc).__name__}") from None


def _header(msg: Message, name: str) -> str | None:
    value = msg[name]
    return str(value).strip() or None if value is not None else None


def _text_body(msg: Message) -> str:
    # Only text/plain counts: an HTML-only mail gives an empty body, which reads as an unclear reply.
    if not isinstance(msg, EmailMessage):
        return ""
    part = msg.get_body(preferencelist=("plain",))
    return str(part.get_content()) if part is not None else ""


class ImapReader:
    """Reads unseen mail with BODY.PEEK so nothing is marked seen until the caller says it was handled."""

    def __init__(self, settings: MailSettings) -> None:
        self._s = settings
        self._conn: imaplib.IMAP4_SSL | None = None
        self._nums: dict[str, str] = {}

    def fetch_unseen(self) -> list[IncomingMail]:
        try:
            conn = imaplib.IMAP4_SSL(self._s.imap_host, self._s.imap_port)
            self._conn = conn
            conn.login(self._s.imap_user, self._s.imap_password)
            conn.select(self._s.imap_folder)
            _, found = conn.search(None, "UNSEEN")
            out: list[IncomingMail] = []
            for num_b in (found[0] or b"").split():
                num = num_b.decode()
                _, data = conn.fetch(num, "(BODY.PEEK[])")
                part = data[0]
                raw = part[1] if isinstance(part, tuple) else b""
                msg = message_from_bytes(raw, policy=policy.default)
                mid = _header(msg, "Message-ID") or "<sha-" + hashlib.sha256(raw).hexdigest()[:24] + "@imap>"
                self._nums[mid] = num
                out.append(IncomingMail(message_id=mid, from_addr=parseaddr(_header(msg, "From") or "")[1],
                                        subject=_header(msg, "Subject") or "", in_reply_to=_header(msg, "In-Reply-To"),
                                        body=_text_body(msg), authentication_results=_header(msg, "Authentication-Results")))
            return out
        except Exception as exc:  # noqa: BLE001
            raise MailDeliveryError(f"IMAP {type(exc).__name__}") from None

    def mark_seen(self, message_id: str) -> None:
        if self._conn is None or message_id not in self._nums:
            return
        self._conn.store(self._nums[message_id], "+FLAGS", "\\Seen")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001, S110 - closing is best effort
                pass
            self._conn = None


class InMemoryMailbox:
    """Simulated mode and tests: the same sender+reader shape with no network."""

    def __init__(self) -> None:
        self.sent: list[OutgoingMail] = []
        self._inbox: list[IncomingMail] = []
        self._seen: set[str] = set()

    def send(self, mail: OutgoingMail) -> None:
        self.sent.append(mail)

    def inject(self, mail: IncomingMail) -> None:
        self._inbox.append(mail)

    def fetch_unseen(self) -> list[IncomingMail]:
        return [m for m in self._inbox if m.message_id not in self._seen]

    def mark_seen(self, message_id: str) -> None:
        self._seen.add(message_id)

    def unsee(self, message_id: str) -> None:
        self._seen.discard(message_id)

    def close(self) -> None:
        return None


def build_mail(settings: MailSettings) -> tuple[MailSender, MailReader]:
    """Simulated: one shared in-memory mailbox. Live: real SMTP and IMAP."""
    if settings.mode == "live":
        return SmtpSender(settings), ImapReader(settings)
    box = InMemoryMailbox()
    return box, box


def run_check(sender: MailSender, reader: MailReader, address: str, *, attempts: int = 6,
              sleep: Callable[[float], None]) -> tuple[bool, str]:
    """Send a test mail to ourselves and look for it in the inbox. Detail text never includes credentials."""
    token = uuid.uuid4().hex[:10]
    subject = f"SetuHaul email check {token}"
    try:
        sender.send(OutgoingMail(to_addr=address, subject=subject, body="Configuration check. You can delete this.",
                                 message_id=f"<check-{token}@setuhaul.local>"))
        for _ in range(attempts):
            for mail in reader.fetch_unseen():
                if token in mail.subject:
                    reader.mark_seen(mail.message_id)
                    return True, "test mail sent and received"
            sleep(5)
    except MailDeliveryError as exc:
        return False, str(exc)
    finally:
        reader.close()
    return False, "test mail sent but not received in time (check IMAP is enabled, the folder name, and spam)"
