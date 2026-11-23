"""Test del canale email: nessuna connessione vera, solo un server finto."""

from __future__ import annotations

import smtplib

import pytest

from pricedrop.notifiers import EmailNotifier, build_notifiers, notify_all
from pricedrop.rules import RULE_OUTLIER, Alert
from tests.test_collector import make_settings


def alert() -> Alert:
    return Alert(
        fingerprint="outlier:406121:1:80054",
        blueprint_id=406121,
        rule=RULE_OUTLIER,
        card_name="Edward.Newgate",
        currency="EUR",
        price_cents=80054.0,
        reference_price_cents=125064.0,
        eligible_count=7,
        listing_id=1,
        seller="negozio",
    )


class FakeServer:
    """Finto server SMTP: registra login e messaggi, non apre connessioni."""

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.logins: list[tuple[str, str]] = []
        self.messages: list = []
        self.quit_called = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.quit_called = True
        return False

    def login(self, user, password):
        if self.fail is not None:
            raise self.fail
        self.logins.append((user, password))

    def send_message(self, message):
        self.messages.append(message)


def notifier(**overrides) -> EmailNotifier:
    defaults = dict(
        host="smtp.esempio.it",
        port=587,
        user="io@esempio.it",
        password="segreta",
        recipient="io@esempio.it",
    )
    defaults.update(overrides)
    return EmailNotifier(**defaults)


# -- attivazione -------------------------------------------------------------


def test_channel_is_off_without_a_password():
    assert not notifier(password="").enabled


def test_channel_is_off_without_a_recipient():
    assert not notifier(recipient="").enabled


def test_channel_is_on_when_everything_is_there():
    assert notifier().enabled


def test_a_disabled_channel_sends_nothing(monkeypatch):
    """Senza configurazione non si tenta nemmeno la connessione."""
    channel = notifier(host="")

    def explode(*args, **kwargs):
        raise AssertionError("non deve connettersi")

    monkeypatch.setattr(smtplib, "SMTP", explode)
    assert channel.send(alert()) is False


def test_the_password_never_appears_in_the_repr():
    assert "segreta" not in repr(notifier())


# -- messaggio ---------------------------------------------------------------


def test_the_subject_names_the_card():
    message = notifier().build_message(alert())
    assert "Edward.Newgate" in message["Subject"]
    assert message["To"] == "io@esempio.it"


def test_the_body_has_price_discount_and_link():
    body = notifier().build_message(alert()).get_content()
    assert "800.54 EUR" in body
    assert "36%" in body
    assert "cardtrader.com" in body


def test_the_sender_defaults_to_the_account():
    assert notifier(sender=None).build_message(alert())["From"] == "io@esempio.it"


def test_an_explicit_sender_wins():
    message = notifier(sender="avvisi@esempio.it").build_message(alert())
    assert message["From"] == "avvisi@esempio.it"


# -- invio -------------------------------------------------------------------


def test_sending_logs_in_and_delivers(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: server)
    monkeypatch.setattr(FakeServer, "starttls", lambda self, context=None: None, raising=False)

    assert notifier().send(alert()) is True
    assert server.logins == [("io@esempio.it", "segreta")]
    assert len(server.messages) == 1


def test_port_465_uses_ssl_from_the_first_byte(monkeypatch):
    server = FakeServer()
    used = {}

    def fake_ssl(*args, **kwargs):
        used["ssl"] = True
        return server

    monkeypatch.setattr(smtplib, "SMTP_SSL", fake_ssl)
    assert notifier(port=465).send(alert()) is True
    assert used == {"ssl": True}


def test_a_rejected_password_does_not_raise(monkeypatch):
    """Un canale che fallisce non deve interrompere il ciclo di raccolta."""
    server = FakeServer(fail=smtplib.SMTPAuthenticationError(535, b"no"))
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: server)
    monkeypatch.setattr(FakeServer, "starttls", lambda self, context=None: None, raising=False)

    assert notifier().send(alert()) is False


def test_an_unreachable_server_does_not_raise(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("connessione rifiutata")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    assert notifier().send(alert()) is False


def test_notify_all_marks_the_alert_as_delivered(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: server)
    monkeypatch.setattr(FakeServer, "starttls", lambda self, context=None: None, raising=False)

    outcome = notify_all([notifier()], [alert()])
    assert outcome.sent == {"email": 1}
    assert outcome.delivered == {"outlier:406121:1:80054"}


# -- configurazione ----------------------------------------------------------


def test_build_notifiers_includes_email(tmp_path):
    settings = make_settings(
        tmp_path,
        smtp_host="smtp.esempio.it",
        smtp_user="io@esempio.it",
        smtp_password="segreta",
        email_to="io@esempio.it",
    )
    channels = {n.name: n.enabled for n in build_notifiers(settings)}
    assert channels["email"] is True


def test_email_stays_off_when_not_configured(tmp_path):
    settings = make_settings(tmp_path)
    channels = {n.name: n.enabled for n in build_notifiers(settings)}
    assert channels["email"] is False
