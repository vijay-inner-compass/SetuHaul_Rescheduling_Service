"""The emails that carry a password link."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import uuid

from setuhaul.mail_transport import OutgoingMail

# Subject and opening line by kind of link; the lifetime matches links.LIFETIME.
_WORDS = {
    "INVITE": ("You're invited to SetuHaul", "You have been invited to SetuHaul. Set your password here:", "48 hours"),
    "RESET": ("Reset your SetuHaul password", "Set a new SetuHaul password here:", "1 hour"),
}


def link_mail(to_addr: str, purpose: str, url: str) -> OutgoingMail:
    subject, opening, lifetime = _WORDS[purpose]
    body = (f"{opening}\n\n{url}\n\nThe link works once, for {lifetime}.\n"
            "If you did not expect this email, you can ignore it.\n")
    return OutgoingMail(to_addr=to_addr, subject=subject, body=body, message_id=f"<{uuid.uuid4().hex}@setuhaul.local>")
