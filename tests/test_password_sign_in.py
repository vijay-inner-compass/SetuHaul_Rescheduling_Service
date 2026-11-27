"""Email and password sign-in: one-time links, passwords for every role, and the emails that carry them."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
import shutil
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from redis.exceptions import RedisError

from setuhaul import partner_booking
from setuhaul.auth import audit, links, local_admin, sessions, users
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import MIGRATIONS_DIR, apply_migrations, open_connection
from setuhaul.identity import AuthError
from setuhaul.mail_transport import InMemoryMailbox, MailDeliveryError, OutgoingMail
from tests.auth_helpers import World, build_world

MIGRATION = "020_password_sign_in.sql"
PASSWORD = "correct horse battery"
BASE = "http://localhost"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    # The world with password sign-in switched on and mail kept in memory.
    built = build_world(tmp_path, clock)
    built.deps.password_sender = InMemoryMailbox()
    built.deps.public_url = BASE
    return built


def _set_password(world: World, who: str, password: str = PASSWORD) -> None:
    conn = open_connection(path=world.path)
    try:
        local_admin.set_password(conn, world.users[who].user_id, password, world.clock)
    finally:
        conn.close()


def _login(world: World, email: str = "wha@x.com", password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return world.client().post("/auth/password", json={"email": email, "password": password})


def _query(world: World, sql: str, *args: object) -> list[tuple[object, ...]]:
    conn = open_connection(path=world.path)
    try:
        rows = [tuple(r) for r in conn.execute(sql, args)]
        conn.commit()
        return rows
    finally:
        conn.close()


# ---- migration and session ---------------------------------------------------------------------------------
def test_migration_keeps_existing_sessions(tmp_path: Path) -> None:
    older = tmp_path / "older"
    older.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < MIGRATION:
            shutil.copyfile(path, older / path.name)
    db = tmp_path / "old.sqlite3"
    shutil.copyfile(load_settings().db_path, db)
    conn = open_connection(path=db)
    try:
        apply_migrations(conn, older)
        conn.execute("insert into users(user_id, email, role, created_at) values ('U1','a@x.com','ADMIN','t')")
        conn.execute("insert into auth_sessions(session_id, user_id, created_at, expires_at, auth_method) "
                     "values ('s1','U1','c','e','GOOGLE')")
        conn.commit()
        assert apply_migrations(conn) == [MIGRATION]
        assert [tuple(r) for r in conn.execute("select * from auth_sessions")] == [("s1", "U1", "c", "e", None, "GOOGLE")]
        indexes = {r[0] for r in conn.execute("select name from sqlite_master where type='index'")}
        assert "ix_auth_sessions_user" in indexes
    finally:
        conn.close()


def test_password_session_lasts_8_hours(world: World) -> None:
    conn = open_connection(path=world.path)
    try:
        token = sessions.create(conn, world.users["wh_a"], "PASSWORD", world.clock)
        world.clock.advance(timedelta(hours=7, minutes=59))
        assert sessions.resolve(conn, token, world.clock) is not None
        world.clock.advance(timedelta(minutes=2))
        assert sessions.resolve(conn, token, world.clock) is None
    finally:
        conn.close()


# ---- password sign-in --------------------------------------------------------------------------------------
def test_warehouse_user_signs_in(world: World) -> None:
    _set_password(world, "wh_a")
    client = world.client()
    r = client.post("/auth/password", json={"email": "wha@x.com", "password": PASSWORD})
    assert r.status_code == 200
    assert "Max-Age=28800" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert client.get("/auth/me").json()["role"] == "WAREHOUSE_AUTHORITY"
    assert _query(world, "select auth_method from auth_sessions where auth_method='PASSWORD'") == [("PASSWORD",)]
    conn = open_connection(path=world.path)
    try:
        assert [r["outcome"] for r in audit.query(conn, action="PASSWORD_LOGIN")] == ["PASSWORD_OK"]
    finally:
        conn.close()


def test_same_refusal_for_unknown_disabled_passwordless_and_locked(world: World) -> None:
    _set_password(world, "wh_a")
    _set_password(world, "wh_b")
    conn = open_connection(path=world.path)
    try:
        users.disable(conn, actor=world.users["admin"], user_id=world.users["wh_b"].user_id, clock=world.clock)
    finally:
        conn.close()
    for _ in range(5):
        _login(world, password="wrong password!!")
    replies = [_login(world, "nobody@x.com"), _login(world, "whb@x.com"), _login(world, "boss@x.com"), _login(world)]
    assert [(r.status_code, r.json()["reason_code"]) for r in replies] == [(401, "BAD_CREDENTIALS")] * 4


def test_email_is_case_and_space_insensitive(world: World) -> None:
    _set_password(world, "wh_a")
    assert _login(world, "  WHA@X.com ").status_code == 200


def test_five_failures_lock_15_minutes(world: World) -> None:
    _set_password(world, "wh_a")
    for _ in range(5):
        assert _login(world, password="wrong password!!").status_code == 401
    world.clock.advance(timedelta(minutes=14))
    assert _login(world).status_code == 401
    world.clock.advance(timedelta(minutes=2))
    assert _login(world).status_code == 200


def test_off_is_404(world: World) -> None:
    _set_password(world, "wh_a")
    world.deps.password_sender = None
    assert _login(world).status_code == 404


def test_bad_bodies_are_refused(world: World) -> None:
    client = world.client()
    bodies = [b"not json", b"[]", b"{}", b'{"email": 1, "password": 2}',
              b'{"email": "wha@x.com", "password": "' + b"x" * 1025 + b'"}',
              b'{"email": "wha@x.com", "password": "\\ud800aaaaaaaaaaaa"}',
              b'{"email": "\\ud800@x.com", "password": "aaaaaaaaaaaaaaa"}']
    for body in bodies:
        r = client.post("/auth/password", content=body, headers={"content-type": "application/json"})
        assert (r.status_code, r.json()["reason_code"]) == (401, "BAD_CREDENTIALS"), body


# ---- one-time links ----------------------------------------------------------------------------------------
def _issue(world: World, who: str = "wh_a", purpose: str = "INVITE") -> str:
    conn = open_connection(path=world.path)
    try:
        return links.issue(conn, world.users[who].user_id, purpose, world.clock)  # type: ignore[arg-type]
    finally:
        conn.close()


def _set(world: World, token: str, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return world.client().post("/auth/password/set", json={"token": token, "password": password})


def _code(response) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    return response.status_code, response.json().get("reason_code", "")


def _toggle(world: World, who: str, on: bool) -> None:
    conn = open_connection(path=world.path)
    try:
        change = users.enable if on else users.disable
        change(conn, actor=world.users["admin"], user_id=world.users[who].user_id, clock=world.clock)
    finally:
        conn.close()


def test_link_sets_password_once(world: World) -> None:
    token = _issue(world)
    assert _set(world, token).status_code == 200
    assert _login(world).status_code == 200
    assert _code(_set(world, token, "another long password")) == (422, "LINK_INVALID")


def test_only_the_hash_is_stored(world: World) -> None:
    token = _issue(world)
    tables = [r[0] for r in _query(world, "select name from sqlite_master where type='table'")]
    for table in tables:
        assert token not in str(_query(world, f'select * from "{table}"')), table


@pytest.mark.parametrize(("purpose", "works_after", "fails_after"), [
    ("INVITE", timedelta(hours=47), timedelta(hours=49)),
    ("RESET", timedelta(minutes=59), timedelta(minutes=61)),
])
def test_expiry(world: World, purpose: str, works_after: timedelta, fails_after: timedelta) -> None:
    early = _issue(world, "wh_a", purpose)
    late = _issue(world, "wh_b", purpose)
    world.clock.advance(works_after)
    assert _set(world, early).status_code == 200
    world.clock.advance(fails_after - works_after)
    assert _code(_set(world, late)) == (422, "LINK_INVALID")


def test_new_link_cancels_old(world: World) -> None:
    first, second = _issue(world), _issue(world)
    assert _code(_set(world, first)) == (422, "LINK_INVALID")
    assert _set(world, second).status_code == 200


def test_disable_cancels_links(world: World) -> None:
    token = _issue(world)
    _toggle(world, "wh_a", on=False)
    assert _code(_set(world, token)) == (422, "LINK_INVALID")
    _toggle(world, "wh_a", on=True)
    assert _code(_set(world, token)) == (422, "LINK_INVALID")


def test_short_password_keeps_link_usable(world: World) -> None:
    token = _issue(world)
    assert _code(_set(world, token, "short")) == (422, "PASSWORD_TOO_SHORT")
    assert _code(_set(world, token, "x" * 1025)) == (422, "PASSWORD_TOO_LONG")
    assert _set(world, token).status_code == 200


def test_setting_password_ends_sessions_and_clears_lock(world: World) -> None:
    _set_password(world, "wh_a")
    signed_in = world.client("wh_a")
    for _ in range(5):
        _login(world, password="wrong password!!")
    assert _set(world, _issue(world), "a brand new password").status_code == 200
    assert signed_in.get("/auth/me").status_code == 401
    assert _login(world, password="a brand new password").status_code == 200


def test_two_simultaneous_redeems_one_wins(world: World) -> None:
    token = _issue(world)
    results: list[str] = []
    gate = threading.Barrier(2)

    def attempt(password: str) -> None:
        conn = open_connection(path=world.path)
        try:
            gate.wait()
            links.redeem(conn, token, password, world.clock)
            results.append("OK")
        except AuthError as exc:
            results.append(exc.code)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(f"long password number {n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["LINK_INVALID", "OK"]


def test_get_does_not_consume_and_bad_bodies_are_422(world: World) -> None:
    token = _issue(world)
    client = world.client()
    assert client.get("/auth/password/set").status_code == 405
    for body in (b"not json", b"[]", b"{}", b'{"token": 1, "password": 2}', b'{"token": "\\ud800", "password": "x"}'):
        r = client.post("/auth/password/set", content=body, headers={"content-type": "application/json"})
        assert _code(r) == (422, "INVALID_REQUEST"), body
    assert _set(world, token).status_code == 200


def test_other_signed_in_user_is_untouched(world: World) -> None:
    token = _issue(world)
    admin = world.client("admin")
    r = admin.post("/auth/password/set", json={"token": token, "password": PASSWORD})
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert admin.get("/auth/me").json()["role"] == "ADMIN"


def test_set_is_404_when_off(world: World) -> None:
    token = _issue(world)
    world.deps.password_sender = None
    assert _set(world, token).status_code == 404


def test_no_secret_in_audit(world: World) -> None:
    token = _issue(world)
    _set(world, token)
    _set(world, token)
    rows = str(_query(world, "select * from audit_log"))
    assert "PASSWORD_SET" in rows and token not in rows and PASSWORD not in rows


# ---- sending the links -------------------------------------------------------------------------------------
class _BrokenSender:
    def send(self, mail: OutgoingMail) -> None:
        raise MailDeliveryError("SMTP SMTPAuthenticationError")


def _mails(world: World) -> list[OutgoingMail]:
    return world.deps.password_sender.sent  # type: ignore[union-attr]


def _token_in(mail: OutgoingMail) -> str:
    found = re.search(rf"{BASE}/set-password#token=([A-Za-z0-9_-]+)", mail.body)
    assert found, mail.body
    return found.group(1)


def _invite(world: World, email: str = "new@x.com"):  # type: ignore[no-untyped-def]
    return world.client("admin").post("/v1/admin/users", json={
        "email": email, "role": "WAREHOUSE_AUTHORITY", "display_name": "New", "facilities": ["FAC-JAI-01"]})


def _forgot(world: World, email: str = "wha@x.com"):  # type: ignore[no-untyped-def]
    return world.client().post("/auth/password/forgot", json={"email": email})


def _send_link(world: World, who: str = "wh_a"):  # type: ignore[no-untyped-def]
    return world.client("admin").post(f"/v1/admin/users/{world.users[who].user_id}/password-link", json={})


def _cool_down_over(world: World) -> None:
    # Fakeredis expiry does not follow the fake clock, so the two-minute keys are removed by hand.
    for key in world.deps.redis.keys("pwmail:cool:*"):
        world.deps.redis.delete(key)


def test_invite_sends_one_link_that_works(world: World) -> None:
    r = _invite(world)
    assert r.status_code == 201 and r.json()["invite_email"] == "SENT"
    [mail] = _mails(world)
    assert mail.to_addr == "new@x.com" and mail.subject == "You're invited to SetuHaul"
    assert "48 hours" in mail.body
    assert _set(world, _token_in(mail)).status_code == 200
    assert _login(world, "new@x.com").status_code == 200


def test_invite_survives_send_failure(world: World) -> None:
    world.deps.password_sender = _BrokenSender()
    r = _invite(world)
    assert r.status_code == 201 and r.json()["invite_email"] == "FAILED"
    assert _query(world, "select count(*) from users where email='new@x.com'") == [(1,)]
    assert _query(world, "select outcome from audit_log where action='ACCOUNT_LINK'") == [("SEND_FAILED",)]
    # A failed send does not make the admin wait before trying again.
    world.deps.password_sender = InMemoryMailbox()
    user_id = _query(world, "select user_id from users where email='new@x.com'")[0][0]
    assert world.client("admin").post(f"/v1/admin/users/{user_id}/password-link", json={}).status_code == 200


def test_invite_with_feature_off_sends_nothing(world: World) -> None:
    box = world.deps.password_sender
    world.deps.password_sender = None
    r = _invite(world)
    assert r.status_code == 201 and r.json()["invite_email"] is None
    assert box.sent == []  # type: ignore[union-attr]


def test_forgot_same_reply_known_and_unknown(world: World) -> None:
    known, unknown = _forgot(world), _forgot(world, "nobody@x.com")
    assert known.status_code == unknown.status_code == 200
    replies = [{k: v for k, v in r.json().items() if k != "request_id"} for r in (known, unknown)]
    assert replies[0] == replies[1]
    assert replies[0]["message"] == "If that address has access, we have sent it a link."
    [mail] = _mails(world)
    assert mail.to_addr == "wha@x.com" and mail.subject == "Reset your SetuHaul password" and "1 hour" in mail.body


def test_forgot_normalises_email(world: World) -> None:
    _forgot(world, "  WHA@X.com ")
    assert [m.to_addr for m in _mails(world)] == ["wha@x.com"]


def test_forgot_disabled_user_gets_nothing(world: World) -> None:
    _toggle(world, "wh_a", on=False)
    assert _forgot(world).status_code == 200
    assert _mails(world) == []


def test_forgot_bad_bodies_and_off(world: World) -> None:
    client = world.client()
    for body in (b"not json", b"{}", b'{"email": 5}'):
        r = client.post("/auth/password/forgot", content=body, headers={"content-type": "application/json"})
        assert _code(r) == (422, "INVALID_REQUEST"), body
    assert _forgot(world, "x" * 300 + "@x.com").status_code == 200
    world.deps.password_sender = None
    assert _forgot(world).status_code == 404


def test_limits_per_account(world: World) -> None:
    _forgot(world)
    _forgot(world)
    assert len(_mails(world)) == 1
    for _ in range(5):
        _cool_down_over(world)
        _forgot(world)
    assert len(_mails(world)) == 5
    assert ("THROTTLED",) in _query(world, "select outcome from audit_log where action='ACCOUNT_LINK'")


def test_limit_on_all_forgot_emails(world: World) -> None:
    world.deps.redis.set("pwmail:forgot", 30, ex=3600)
    _forgot(world)
    assert _mails(world) == []
    # An administrator's own send is not part of that allowance.
    assert _send_link(world).status_code == 200


def test_redis_down(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*args: object, **kwargs: object) -> None:
        raise RedisError("down")

    for name in ("ping", "set", "incr"):
        monkeypatch.setattr(world.deps.redis, name, down)
    assert _code(_forgot(world)) == (503, "TRY_LATER")
    assert _mails(world) == []
    assert _send_link(world).status_code == 200
    assert len(_mails(world)) == 1


def test_link_uses_public_url_not_host_header(world: World) -> None:
    world.client().post("/auth/password/forgot", json={"email": "wha@x.com"}, headers={"host": "evil.example"})
    [mail] = _mails(world)
    assert "evil.example" not in mail.body
    _token_in(mail)


def test_admin_send_link_codes(world: World) -> None:
    assert _send_link(world).status_code == 200
    assert _mails(world)[-1].subject == "You're invited to SetuHaul"
    assert _code(_send_link(world)) == (429, "TOO_SOON")
    _set_password(world, "wh_b")
    assert _send_link(world, "wh_b").status_code == 200
    assert _mails(world)[-1].subject == "Reset your SetuHaul password"
    admin = world.client("admin")
    assert _code(admin.post("/v1/admin/users/USR-NOBODY/password-link", json={})) == (404, "UNKNOWN_USER")
    _toggle(world, "wh_a", on=False)
    assert _code(_send_link(world)) == (409, "USER_DISABLED")
    world.deps.password_sender = _BrokenSender()
    _cool_down_over(world)
    assert _code(_send_link(world, "wh_b")) == (502, "EMAIL_FAILED")
    world.deps.password_sender = None
    assert _code(_send_link(world, "wh_b")) == (404, "NOT_FOUND")


def test_no_link_or_token_in_audit_after_sending(world: World) -> None:
    _forgot(world)
    token = _token_in(_mails(world)[0])
    rows = str(_query(world, "select * from audit_log"))
    assert token not in rows and "set-password" not in rows


def test_partner_reply_reader_ignores_account_mail(world: World) -> None:
    # Both may use one mailbox, and the account subjects contain the word the partner reader searches for.
    conn = open_connection(path=world.path)
    try:
        before = conn.execute("select count(*) from escalation_cases").fetchone()[0]
        for number, subject in enumerate(("Re: You're invited to SetuHaul", "Reset your SetuHaul password")):
            outcome = partner_booking.handle_reply(
                conn, world.clock, message_id=f"<m{number}@x>", from_addr="wha@x.com", subject=subject,
                in_reply_to=None, body="APPROVE", authentication_results=None, require_auth=False)
            assert outcome.status == "UNMATCHED"
        assert conn.execute("select count(*) from escalation_cases").fetchone()[0] == before
    finally:
        conn.close()


# ---- screens -----------------------------------------------------------------------------------------------
def test_login_page_on_and_off(world: World) -> None:
    on = world.client().get("/").text
    assert 'data-api="/auth/password"' in on and 'href="/forgot-password"' in on and 'method="post"' in on
    assert "Administrator password" not in on and "For administrators and warehouse teams." in on
    # The password form takes the place of the break-glass one.
    world.deps.local_admin_enabled = True
    assert "/auth/local-admin" not in world.client().get("/").text
    world.deps.password_sender = None
    off = world.client().get("/").text
    assert "/auth/password" not in off and "forgot-password" not in off and "/auth/local-admin" in off


def test_password_pages(world: World) -> None:
    forgot = world.client().get("/forgot-password")
    assert forgot.status_code == 200 and 'data-api="/auth/password/forgot"' in forgot.text
    assert 'data-done="If that address has access, we have sent it a link."' in forgot.text
    chosen = world.client().get("/set-password")
    assert chosen.status_code == 200 and 'data-api="/auth/password/set"' in chosen.text
    assert 'type="hidden" name="token"' in chosen.text and 'data-next="/?signin=password_set"' in chosen.text
    # The repeat box is checked in the browser and never sent.
    assert re.search(r'<input id="repeat"(?![^>]*name=)[^>]*>', chosen.text)
    assert "at least 12 characters" in chosen.text and "/app/assets/password.js" in chosen.text
    for page in (forgot, chosen):
        assert "default-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["referrer-policy"] == "no-referrer" and page.headers["cache-control"] == "no-store"
        assert not re.search(r"<script(?![^>]*\bsrc=)", page.text) and 'method="post"' in page.text
    assert world.client().get("/app/assets/password.js").status_code == 200


def test_password_pages_404_when_off(world: World) -> None:
    world.deps.password_sender = None
    assert world.client().get("/forgot-password").status_code == 404
    assert world.client().get("/set-password").status_code == 404


def test_users_table_columns(world: World) -> None:
    _set_password(world, "wh_a")
    on = world.client("admin").get("/admin").text
    users_panel = on.split('data-panel="users"><h2>Users</h2>')[1].split("</section>")[0]
    assert "<th>Password</th>" in users_panel and "<td>Set</td>" in users_panel and "<td>Not set</td>" in users_panel
    assert f'/v1/admin/users/{world.users["wh_a"].user_id}/password-link' in users_panel
    _toggle(world, "wh_a", on=False)
    after = world.client("admin").get("/admin").text
    assert f'/v1/admin/users/{world.users["wh_a"].user_id}/password-link' not in after
    world.deps.password_sender = None
    off = world.client("admin").get("/admin").text
    assert "password-link" not in off and "<th>Password</th>" not in off


def test_password_set_notice(world: World) -> None:
    page = world.client().get("/?signin=password_set").text
    assert 'class="status ok" role="alert">Password saved. Sign in below.<' in page
    assert 'class="status ok"' not in world.client().get("/?signin=expired").text


def test_invite_result_is_said_on_screen() -> None:
    script = (Path(__file__).resolve().parents[1] / "src" / "setuhaul" / "auth" / "assets" / "common.js").read_text("utf-8")
    assert "invite_email" in script
    assert "Invitation emailed." in script and "Invited, but the email could not be sent." in script
