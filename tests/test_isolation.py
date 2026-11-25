"""The test run must never pick up the developer's own .env: it holds real keys for outside services."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import os
from pathlib import Path

import pytest

from setuhaul import mailcfg

OUTSIDE = ("SETUHAUL_LANGSMITH", "LANGSMITH_API_KEY", "SETUHAUL_MODEL_API_KEY", "SETUHAUL_SMTP_PASSWORD", "SETUHAUL_TELEGRAM_BOT_TOKEN")


def test_a_developer_env_file_is_not_loaded_during_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stand-in repository root holding a .env full of made-up settings.
    (tmp_path / ".env").write_text("".join(f"{name}=made-up-value\n" for name in OUTSIDE), encoding="utf-8")
    monkeypatch.setattr(mailcfg, "REPO_ROOT", tmp_path)
    for name in OUTSIDE:
        monkeypatch.delenv(name, raising=False)
    mailcfg.load_mail_settings()
    assert [name for name in OUTSIDE if name in os.environ] == []


def test_outside_service_settings_are_absent_and_dotenv_is_off() -> None:
    assert os.environ.get("SETUHAUL_ENV_FILE") == "" and os.environ.get("SETUHAUL_DOTENV") == "off"
    assert "SETUHAUL_LANGSMITH" not in os.environ and "LANGSMITH_API_KEY" not in os.environ
