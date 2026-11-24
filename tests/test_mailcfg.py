"""Mail settings come from the environment only; secrets never appear in repr or errors."""

from __future__ import annotations

import os

import pytest

from setuhaul.mailcfg import load_mail_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith(("SETUHAUL_SMTP_", "SETUHAUL_IMAP_", "SETUHAUL_PARTNER_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SETUHAUL_ENV_FILE", "")  # never read a real .env in tests


def test_defaults_are_simulated_with_gmail_hosts() -> None:
    s = load_mail_settings()
    assert s.mode == "simulated"
    assert (s.smtp_host, s.smtp_port) == ("smtp.gmail.com", 465)
    assert (s.imap_host, s.imap_port, s.imap_folder) == ("imap.gmail.com", 993, "INBOX")
    assert s.request_ttl_min == 120


def test_live_mode_requires_user_and_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_PARTNER_EMAIL_MODE", "live")
    monkeypatch.setenv("SETUHAUL_SMTP_USER", "desk@example.com")
    monkeypatch.setenv("SETUHAUL_SMTP_PASSWORD", "s3cret-app-pw")
    with pytest.raises(ValueError) as exc:
        load_mail_settings()
    assert "SETUHAUL_IMAP_USER" in str(exc.value)
    assert "s3cret-app-pw" not in str(exc.value)


def test_smtp_from_defaults_to_smtp_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_PARTNER_EMAIL_MODE", "live")
    monkeypatch.setenv("SETUHAUL_SMTP_USER", "desk@example.com")
    monkeypatch.setenv("SETUHAUL_SMTP_PASSWORD", "pw")
    monkeypatch.setenv("SETUHAUL_IMAP_USER", "desk@example.com")
    monkeypatch.setenv("SETUHAUL_IMAP_PASSWORD", "pw")
    s = load_mail_settings()
    assert s.smtp_from == "desk@example.com"


def test_unknown_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_PARTNER_EMAIL_MODE", "yolo")
    with pytest.raises(ValueError):
        load_mail_settings()


def test_repr_hides_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_SMTP_PASSWORD", "s3cret-app-pw")
    monkeypatch.setenv("SETUHAUL_IMAP_PASSWORD", "s3cret-app-pw")
    assert "s3cret-app-pw" not in repr(load_mail_settings())
