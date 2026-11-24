"""Gmail SMTP/IMAP transport against fake servers; no test opens a socket."""

from __future__ import annotations

import socket
from email.message import EmailMessage
from typing import ClassVar

import pytest

from setuhaul import mail_transport
from setuhaul.mail_transport import (
    ImapReader,
    InMemoryMailbox,
    MailDeliveryError,
    OutgoingMail,
    SmtpSender,
    build_mail,
    run_check,
)
from setuhaul.mailcfg import MailSettings


def _settings(**kw: object) -> MailSettings:
    base: dict[str, object] = dict(  # noqa: C408
                                   mode="live", smtp_host="smtp.test", smtp_port=465, smtp_user="desk@test", smtp_password="TOPSECRET",
                                   smtp_from="desk@test", imap_host="imap.test", imap_port=993, imap_user="desk@test",
                                   imap_password="TOPSECRET", imap_folder="INBOX", request_ttl_min=120)
    base.update(kw)
    return MailSettings(**base)  # type: ignore[arg-type]


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []
    fail_login = False

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.kind = type(self).__name__
        self.host, self.port, self.timeout, self.starttls_called, self.sent = host, port, timeout, False, []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:  # noqa: PYI034
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail_login:
            raise RuntimeError(f"auth failed for {password}")  # a hostile message that leaks the password

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def _fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances, FakeSMTP.fail_login = [], False
    monkeypatch.setattr(mail_transport.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail_transport.smtplib, "SMTP_SSL", FakeSMTPSSL)


def _mail() -> OutgoingMail:
    return OutgoingMail(to_addr="p@partner", subject="SetuHaul slot request [PR-AAAAAAAA]", body="hi", message_id="<m@setuhaul>")


def test_smtp_ssl_used_for_465_and_starttls_for_587() -> None:
    SmtpSender(_settings(smtp_port=465)).send(_mail())
    SmtpSender(_settings(smtp_port=587)).send(_mail())
    ssl, plain = FakeSMTP.instances
    assert (ssl.kind, ssl.starttls_called, ssl.timeout) == ("FakeSMTPSSL", False, 30)
    assert (plain.kind, plain.starttls_called) == ("FakeSMTP", True)
    msg = ssl.sent[0]
    assert (msg["To"], msg["From"], msg["Message-ID"]) == ("p@partner", "desk@test", "<m@setuhaul>")


def test_smtp_failure_raises_delivery_error_without_leaking_password() -> None:
    FakeSMTP.fail_login = True
    with pytest.raises(MailDeliveryError) as exc:
        SmtpSender(_settings()).send(_mail())
    assert "TOPSECRET" not in str(exc.value) and "TOPSECRET" not in repr(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


RAW = (b"From: Partner Desk <partner.desk@partner.example>\r\nTo: desk@test\r\nSubject: Re: SetuHaul slot request [PR-AAAAAAAA]\r\n"
       b"Message-ID: <reply1@partner>\r\nIn-Reply-To: <m@setuhaul>\r\nAuthentication-Results: mx.google.com; spf=pass; dkim=pass\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\nAPPROVE\r\n")
HTML_ONLY = (b"From: a@b\r\nSubject: s\r\nMessage-ID: <h@x>\r\nContent-Type: text/html\r\n\r\n<p>APPROVE</p>\r\n")


class FakeIMAP:
    def __init__(self, host: str, port: int) -> None:
        self.calls: list[tuple[str, tuple]] = []
        FakeIMAP.last = self

    last: FakeIMAP
    mails: ClassVar[dict[str, bytes]] = {"1": RAW, "2": HTML_ONLY}

    def login(self, user: str, password: str) -> tuple[str, list]:
        self.calls.append(("login", (user,)))
        return "OK", []

    def select(self, folder: str) -> tuple[str, list]:
        self.calls.append(("select", (folder,)))
        return "OK", [b"2"]

    def search(self, charset: None, criterion: str) -> tuple[str, list]:
        self.calls.append(("search", (criterion,)))
        return "OK", [b"1 2"]

    def fetch(self, num: str, spec: str) -> tuple[str, list]:
        self.calls.append(("fetch", (num, spec)))
        return "OK", [(b"hdr", self.mails[num])]

    def store(self, num: str, op: str, flag: str) -> tuple[str, list]:
        self.calls.append(("store", (num, op, flag)))
        return "OK", []

    def logout(self) -> tuple[str, list]:
        self.calls.append(("logout", ()))
        return "OK", []


def test_imap_reader_parses_plain_text_headers_and_html_only_gives_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail_transport.imaplib, "IMAP4_SSL", FakeIMAP)
    reader = ImapReader(_settings())
    first, second = reader.fetch_unseen()
    assert (first.message_id, first.from_addr, first.in_reply_to) == ("<reply1@partner>", "partner.desk@partner.example", "<m@setuhaul>")
    assert first.subject.endswith("[PR-AAAAAAAA]") and first.body.strip() == "APPROVE"
    assert "spf=pass" in (first.authentication_results or "")
    assert second.body == ""


def test_imap_fetch_uses_peek_so_message_stays_unseen_until_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail_transport.imaplib, "IMAP4_SSL", FakeIMAP)
    reader = ImapReader(_settings())
    reader.fetch_unseen()
    calls = FakeIMAP.last.calls
    assert all(spec == "(BODY.PEEK[])" for name, (_, spec) in [c for c in calls if c[0] == "fetch"])
    assert not any(c[0] == "store" for c in calls)
    reader.mark_seen("<reply1@partner>")
    assert ("store", ("1", "+FLAGS", "\\Seen")) in FakeIMAP.last.calls
    reader.close()
    assert FakeIMAP.last.calls[-1][0] == "logout"


def test_in_memory_mailbox_round_trip() -> None:
    box = InMemoryMailbox()
    box.send(_mail())
    assert box.sent[0].subject.startswith("SetuHaul")
    box.inject(mail_transport.IncomingMail(message_id="<r>", from_addr="a@b", subject="s", in_reply_to=None, body="x",
                                           authentication_results=None))
    assert [m.message_id for m in box.fetch_unseen()] == ["<r>"]
    box.mark_seen("<r>")
    assert box.fetch_unseen() == []


def test_simulated_mode_never_opens_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_socket(*a: object, **k: object) -> None:
        raise AssertionError("simulated mode must not open a socket")
    monkeypatch.setattr(socket, "socket", no_socket)
    sender, reader = build_mail(_settings(mode="simulated", smtp_user="", smtp_password="", imap_user="", imap_password=""))
    sender.send(_mail())
    assert reader.fetch_unseen() == []
    assert sender is reader  # one shared in-memory mailbox


def test_run_check_succeeds_when_the_test_mail_comes_back() -> None:
    class Echo(InMemoryMailbox):
        def send(self, mail: OutgoingMail) -> None:
            super().send(mail)
            self.inject(mail_transport.IncomingMail(message_id="<echo>", from_addr="desk@test", subject=mail.subject,
                                                    in_reply_to=None, body=mail.body, authentication_results=None))
    box = Echo()
    ok, detail = run_check(box, box, "desk@test", attempts=2, sleep=lambda s: None)
    assert ok and "received" in detail


def test_run_check_reports_failure_without_secrets() -> None:
    box = InMemoryMailbox()
    ok, detail = run_check(box, box, "desk@test", attempts=2, sleep=lambda s: None)
    assert not ok and "not received" in detail
