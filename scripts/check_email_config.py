"""Check the live partner-email settings: sends a test mail to the configured mailbox and looks for it in the inbox.

Usage: python scripts/check_email_config.py   (set SETUHAUL_PARTNER_EMAIL_MODE=live and the SMTP/IMAP variables first)
Never prints passwords.
"""

# Lazy annotations.
from __future__ import annotations

# sys for the exit code, time for the real sleep.
import sys
import time

from setuhaul.mail_transport import build_mail, run_check
from setuhaul.mailcfg import load_mail_settings


def main() -> int:
    try:
        settings = load_mail_settings()
    except ValueError as exc:
        print(f"Settings problem: {exc}")
        return 2
    if settings.mode != "live":
        print("Mode is 'simulated': nothing to check. Set SETUHAUL_PARTNER_EMAIL_MODE=live and the Gmail variables (see .env.example).")
        return 0
    sender, reader = build_mail(settings)
    ok, detail = run_check(sender, reader, settings.smtp_user, sleep=time.sleep)
    print(("OK: " if ok else "FAILED: ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
