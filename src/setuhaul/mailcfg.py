"""Email settings for partner-warehouse requests. Environment only; passwords never appear in repr or errors."""

# Lazy annotations.
from __future__ import annotations

# os for the environment.
import os

# Mapping types an environment passed in.
from collections.abc import Mapping

# dataclass for the immutable settings object.
from dataclasses import dataclass, field

# Paths for the optional .env file.
from pathlib import Path

# The repo root, where an optional .env lives.
from setuhaul.config import REPO_ROOT

# The two modes: in-memory demo mailbox, or real Gmail (any SMTP/IMAP provider works).
MODES = ("simulated", "live")


@dataclass(frozen=True)
class MailSettings:
    mode: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    # repr=False keeps the password out of logs and tracebacks.
    smtp_password: str = field(repr=False)
    smtp_from: str
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str = field(repr=False)
    imap_folder: str
    # How long a partner has to answer before the request expires.
    request_ttl_min: int
    # The receiving server's name in Authentication-Results; live replies must be vouched for by it.
    auth_serv_id: str = "mx.google.com"


def _load_env_file() -> None:
    """Set variables from a git-ignored .env without overriding the real environment. No dependency needed."""
    # SETUHAUL_ENV_FILE="" turns the file off (tests); unset means <repo>/.env.
    raw = os.environ.get("SETUHAUL_ENV_FILE")
    path = REPO_ROOT / ".env" if raw is None else Path(raw) if raw else None
    if path is None or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Real environment wins; empty values in the file are ignored.
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_smtp_settings(env: Mapping[str, str], mode: str = "simulated") -> MailSettings:
    """The settings as written, without the checks that live partner mail needs. Raises ValueError on a bad number."""
    smtp_user = env.get("SETUHAUL_SMTP_USER", "")
    return MailSettings(
        mode=mode,
        smtp_host=env.get("SETUHAUL_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(env.get("SETUHAUL_SMTP_PORT", "465")),
        smtp_user=smtp_user,
        smtp_password=env.get("SETUHAUL_SMTP_PASSWORD", ""),
        smtp_from=env.get("SETUHAUL_SMTP_FROM", "") or smtp_user,
        imap_host=env.get("SETUHAUL_IMAP_HOST", "imap.gmail.com"),
        imap_port=int(env.get("SETUHAUL_IMAP_PORT", "993")),
        imap_user=env.get("SETUHAUL_IMAP_USER", ""),
        imap_password=env.get("SETUHAUL_IMAP_PASSWORD", ""),
        imap_folder=env.get("SETUHAUL_IMAP_FOLDER", "INBOX"),
        request_ttl_min=int(env.get("SETUHAUL_PARTNER_REQUEST_TTL_MIN", "120")),
        auth_serv_id=env.get("SETUHAUL_AUTH_SERV_ID", "mx.google.com"),
    )


def load_mail_settings() -> MailSettings:
    _load_env_file()
    env = os.environ
    mode = env.get("SETUHAUL_PARTNER_EMAIL_MODE", "simulated").strip().lower()
    if mode not in MODES:
        raise ValueError(f"SETUHAUL_PARTNER_EMAIL_MODE must be one of {MODES}")
    settings = load_smtp_settings(env, mode)
    if mode == "live":
        # Name the missing variable, never its value.
        for name, value in (("SETUHAUL_SMTP_USER", settings.smtp_user), ("SETUHAUL_SMTP_PASSWORD", settings.smtp_password),
                            ("SETUHAUL_IMAP_USER", settings.imap_user), ("SETUHAUL_IMAP_PASSWORD", settings.imap_password)):
            if not value:
                raise ValueError(f"live partner email needs {name}")
    return settings
