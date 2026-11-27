"""The emails that carry a password link."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

from setuhaul.mail_transport import MailSender, OutgoingMail, SmtpSender
from setuhaul.mailcfg import MailSettings, load_smtp_settings

# Subject and opening line by kind of link; the lifetime matches links.LIFETIME.
_WORDS = {
    "INVITE": ("You're invited to SetuHaul", "You have been invited to SetuHaul. Set your password here:", "48 hours"),
    "RESET": ("Reset your SetuHaul password", "Set a new SetuHaul password here:", "1 hour"),
}


def account_sender_from_env(env: Mapping[str, str], factory: Callable[[MailSettings], MailSender] = SmtpSender
                            ) -> tuple[MailSender | None, tuple[str, ...]]:
    """The sender for password links, or None with the names (never values) of what is missing.

    Separate from the partner-mail mode: turning this on sends no partner requests and reads no inbox.
    """
    if env.get("SETUHAUL_PASSWORD_LOGIN") != "1":
        return None, ()
    needed = ("SETUHAUL_PUBLIC_URL", "SETUHAUL_SMTP_USER", "SETUHAUL_SMTP_PASSWORD")
    missing = tuple(name for name in needed if not env.get(name))
    if missing:
        return None, missing
    try:
        settings = load_smtp_settings(env)
    except ValueError:
        # The only numbers this reads that sending uses are the SMTP ones.
        return None, ("SETUHAUL_SMTP_PORT",)
    return factory(settings), ()


def link_mail(to_addr: str, purpose: str, url: str, google_url: str | None = None) -> OutgoingMail:
    subject, opening, lifetime = _WORDS[purpose]
    # Offered only when the service has Google sign-in set up; it needs no password.
    google = f"If this address is a Google account, you can sign in with Google instead:\n\n{google_url}\n\n" if google_url else ""
    body = (f"{opening}\n\n{url}\n\nThe link works once, for {lifetime}.\n\n{google}"
            "If you did not expect this email, you can ignore it.\n")
    return OutgoingMail(to_addr=to_addr, subject=subject, body=body, message_id=f"<{uuid.uuid4().hex}@setuhaul.local>")
