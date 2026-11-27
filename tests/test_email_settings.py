"""The Email block of the Configuration tab: the mailbox that sends invitation and password links."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from setuhaul.db import open_connection
from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import World, build_world
from tests.helpers import at
from tests.runtime_helpers import BASE

URL = "/v1/admin/settings/email"
KEY = Fernet.generate_key().decode()
MAILBOX = {"host": "mail.example.com", "port": "587", "user": "send@example.com", "password": "s3cret-pass-word",
           "sender": "SetuHaul@example.com"}


def make_world(tmp_path: Path, **env: str) -> World:
    from setuhaul.clock import FakeClock

    built = build_world(tmp_path, FakeClock(at("09:35")))
    built.deps.settings = SettingsSource(BASE | {"SETUHAUL_SETTINGS_KEY": KEY} | env)
    return built


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def in_force(world: World) -> dict[str, str]:
    conn = open_connection(path=world.path)
    assert world.deps.settings is not None
    env = world.deps.settings.effective(conn).env
    conn.close()
    return env


def rows(world: World, sql: str) -> str:
    conn = open_connection(path=world.path)
    found = conn.execute(sql).fetchall()
    conn.close()
    return " ".join(str(v) for r in found for v in r)


def save(client: TestClient, body: dict[str, Any]) -> tuple[int, str]:
    r = client.post(URL, json=body)
    return r.status_code, str(r.json().get("reason_code", ""))


# ---- store and validate ---------------------------------------------------------------------------------------
def test_saved_email_settings_override_env(tmp_path: Path) -> None:
    world = make_world(tmp_path, SETUHAUL_SMTP_HOST="env.example.com", SETUHAUL_SMTP_USER="env@example.com")
    admin = world.client("admin")
    assert save(admin, MAILBOX) == (200, "")
    env = in_force(world)
    assert (env["SETUHAUL_SMTP_HOST"], env["SETUHAUL_SMTP_PORT"], env["SETUHAUL_SMTP_USER"]) == (
        "mail.example.com", "587", "send@example.com")
    assert env["SETUHAUL_SMTP_PASSWORD"] == "s3cret-pass-word"
    assert admin.post(f"{URL}/reset", json={}).status_code == 200
    assert in_force(world)["SETUHAUL_SMTP_HOST"] == "env.example.com"


def test_password_is_encrypted_and_never_returned(world: World) -> None:
    admin = world.client("admin")
    save(admin, MAILBOX)
    assert "s3cret-pass-word" not in rows(world, "select name, value from app_settings")
    read = admin.get("/v1/admin/settings").text + admin.get("/admin").text
    assert "s3cret-pass-word" not in read
    # Like the webhook secret: that one is set, and none of its characters.
    assert admin.get("/v1/admin/settings").json()["settings"]["SETUHAUL_SMTP_PASSWORD"] == {"source": "saved", "set": True}


def test_a_gmail_app_password_pasted_with_spaces_is_joined(world: World) -> None:
    save(world.client("admin"), MAILBOX | {"password": "abcd efgh ijkl mnop"})
    assert in_force(world)["SETUHAUL_SMTP_PASSWORD"] == "abcdefghijklmnop"


@pytest.mark.parametrize("change", [{"host": "evil.example.com"}, {"port": "465"}, {"user": "other@example.com"}])
def test_changing_server_port_or_user_requires_the_password(world: World, change: dict[str, str]) -> None:
    admin = world.client("admin")
    save(admin, MAILBOX)
    before = in_force(world)
    assert save(admin, change) == (422, "SMTP_PASSWORD_REQUIRED")
    assert in_force(world) == before
    assert save(admin, change | {"password": "another-password"}) == (200, "")


def test_a_password_from_env_is_not_sent_to_a_new_server_either(tmp_path: Path) -> None:
    world = make_world(tmp_path, SETUHAUL_SMTP_PASSWORD="env-password", SETUHAUL_SMTP_USER="env@example.com")
    assert save(world.client("admin"), {"host": "evil.example.com"}) == (422, "SMTP_PASSWORD_REQUIRED")


@pytest.mark.parametrize("body,code", [({"port": "0"}, "INVALID_PORT"), ({"port": "65536"}, "INVALID_PORT"),
                                       ({"port": "abc"}, "INVALID_PORT"), ({"user": "not-an-address"}, "INVALID_EMAIL"),
                                       ({"sender": "two words@x"}, "INVALID_EMAIL"), ({"host": "a b"}, "INVALID_REQUEST"),
                                       ({"link_url": "https://x"}, "INVALID_REQUEST")])
def test_port_and_address_rules(world: World, body: dict[str, str], code: str) -> None:
    assert save(world.client("admin"), body) == (422, code)


def test_without_a_settings_key_the_password_cannot_be_saved(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.deps.settings = SettingsSource({k: v for k, v in BASE.items() if k != "SETUHAUL_SETTINGS_KEY"})
    assert save(world.client("admin"), MAILBOX) == (422, "SETTINGS_KEY_UNAVAILABLE")


def test_only_admin_may_save(world: World) -> None:
    assert world.client("wh_a").post(URL, json=MAILBOX).status_code == 403
    assert world.client().post(URL, json=MAILBOX).status_code == 401


def test_audit_shows_server_and_addresses_but_not_the_password(world: World) -> None:
    save(world.client("admin"), MAILBOX)
    trail = rows(world, "select action, target_id, detail_json from audit_log")
    assert "mail.example.com" in trail and "send@example.com" in trail and "s3cret" not in trail


# ---- send with the mailbox in force ---------------------------------------------------------------------------
class Boxes:
    """Stands in for the SMTP sender: remembers which mailbox each message would have been sent from."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[Any, Any]] = []
        self.fail = fail

    def __call__(self, settings: Any) -> Any:
        boxes = self

        class Sender:
            def send(self, mail: Any) -> None:
                if boxes.fail is not None:
                    raise boxes.fail
                boxes.sent.append((settings, mail))

        return Sender()


def mail_world(tmp_path: Path, switch: str = "1", boxes: Boxes | None = None, **env: str) -> tuple[World, Boxes]:
    world = make_world(tmp_path)
    boxes = boxes or Boxes()
    world.deps.settings = SettingsSource(
        BASE | {"SETUHAUL_SETTINGS_KEY": KEY, "SETUHAUL_PASSWORD_LOGIN": switch,
                "SETUHAUL_PUBLIC_URL": "https://haul.example.com"} | env, mail=boxes)
    world.deps.public_url = "https://haul.example.com"
    # The same-origin check compares with the public address.
    return world, boxes


def admin_of(world: World) -> TestClient:
    client = world.client("admin")
    client.headers["origin"] = "https://haul.example.com"
    return client


def invite(client: TestClient, email: str = "new@x.com") -> Any:
    return client.post("/v1/admin/users", json={"email": email, "display_name": "New", "role": "ADMIN", "facilities": []})


def test_saving_the_mailbox_turns_emails_on_without_restart(tmp_path: Path) -> None:
    world, boxes = mail_world(tmp_path)
    admin = admin_of(world)
    assert not world.deps.password_login_on
    assert world.deps.password_mail().missing == ("SETUHAUL_SMTP_USER", "SETUHAUL_SMTP_PASSWORD")
    assert admin.post(URL, json=MAILBOX).status_code == 200
    assert world.deps.password_login_on
    r = invite(admin)
    assert r.json()["invite_email"] == "SENT", r.text
    [(settings, mail)] = boxes.sent
    assert (settings.smtp_host, settings.smtp_user, mail.to_addr) == ("mail.example.com", "send@example.com", "new@x.com")
    assert "https://haul.example.com/set-password#token=" in mail.body
    assert "Forgot password?" in world.client().get("/").text


def test_switch_off_in_env_keeps_emails_off(tmp_path: Path) -> None:
    world, boxes = mail_world(tmp_path, switch="0")
    admin = admin_of(world)
    admin.post(URL, json=MAILBOX)
    assert not world.deps.password_login_on and world.deps.password_mail().missing == ("SETUHAUL_PASSWORD_LOGIN",)
    invite(admin)
    assert boxes.sent == []


def test_unreadable_password_turns_emails_off(tmp_path: Path) -> None:
    world, boxes = mail_world(tmp_path)
    admin_of(world).post(URL, json=MAILBOX)
    world.deps.settings = SettingsSource(
        BASE | {"SETUHAUL_SETTINGS_KEY": Fernet.generate_key().decode(), "SETUHAUL_PASSWORD_LOGIN": "1",
                "SETUHAUL_PUBLIC_URL": "https://haul.example.com"}, mail=boxes)
    assert not world.deps.password_login_on
    assert world.deps.password_mail().missing == ("SETUHAUL_SMTP_PASSWORD",)


def test_sign_in_page_renders_when_the_database_fails(tmp_path: Path) -> None:
    world, _ = mail_world(tmp_path, SETUHAUL_SMTP_USER="env@example.com", SETUHAUL_SMTP_PASSWORD="env-pass")
    world.deps.db_path = tmp_path / "missing" / "no.sqlite3"
    assert world.deps.password_login_on
    page = world.client().get("/")
    assert page.status_code == 200 and "Forgot password?" in page.text


def test_nothing_saved_behaves_as_before(tmp_path: Path) -> None:
    world, boxes = mail_world(tmp_path, SETUHAUL_SMTP_USER="env@example.com", SETUHAUL_SMTP_PASSWORD="env-pass")
    assert world.deps.password_login_on and world.deps.password_mail().missing == ()
    assert invite(admin_of(world)).json()["invite_email"] == "SENT"
    assert boxes.sent[0][0].smtp_host == "smtp.gmail.com"
    world.deps.public_url = ""
    assert not world.deps.password_login_on
