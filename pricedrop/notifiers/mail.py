"""Notifiche via email, con un server SMTP qualsiasi.

Il canale resta disattivato finche server, credenziali e destinatario non
sono tutti presenti: come per Telegram, una configurazione incompleta non
deve far fallire il ciclo di raccolta.

Su Gmail la password dell'account non funziona: serve una password per le app,
che Google rilascia solo con la verifica in due passaggi attiva. Sta in `.env`,
che non e versionato.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..rules import Alert
from .base import format_body, format_title

logger = logging.getLogger(__name__)


class EmailNotifier:
    """Invia una email per ogni segnalazione.

    Una email per segnalazione, come gli altri canali: il dedup sulle impronte
    garantisce che la stessa inserzione non venga rimandata a ogni ciclo,
    quindi la casella non si riempie di doppioni.
    """

    name = "email"

    def __init__(
        self,
        host: str | None,
        port: int,
        user: str | None,
        password: str | None,
        recipient: str | None,
        sender: str | None = None,
        *,
        timeout: float = 20.0,
        use_ssl: bool | None = None,
    ) -> None:
        self._host = (host or "").strip()
        self._port = int(port or 0)
        self._user = (user or "").strip()
        self._password = password or ""
        self._recipient = (recipient or "").strip()
        self._sender = (sender or "").strip() or self._user
        self._timeout = timeout
        # La porta 465 parla SSL dal primo byte; la 587 parte in chiaro e passa
        # a TLS con STARTTLS. Si puo forzare, ma il default sulla porta copre
        # i due casi normali.
        self._use_ssl = (self._port == 465) if use_ssl is None else use_ssl

    def __repr__(self) -> str:
        # Password e destinatario non compaiono mai nel repr.
        return f"<EmailNotifier enabled={self.enabled} host={self._host!r}>"

    @property
    def enabled(self) -> bool:
        return bool(
            self._host
            and self._port
            and self._user
            and self._password
            and self._recipient
        )

    def build_message(self, alert: Alert) -> EmailMessage:
        """Compone l'email: oggetto come titolo, corpo come le altre notifiche."""
        message = EmailMessage()
        message["Subject"] = format_title(alert)
        message["From"] = self._sender
        message["To"] = self._recipient
        message.set_content(f"{format_body(alert)}\n")
        return message

    def _connect(self) -> smtplib.SMTP:
        if self._use_ssl:
            return smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        server.starttls(context=ssl.create_default_context())
        return server

    def send(self, alert: Alert) -> bool:
        if not self.enabled:
            return False

        message = self.build_message(alert)
        try:
            with self._connect() as server:
                server.login(self._user, self._password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError:
            # Il messaggio del server non viene ripetuto nel log: puo contenere
            # frammenti delle credenziali rifiutate.
            logger.warning(
                "Il server SMTP ha rifiutato le credenziali. Su Gmail serve "
                "una password per le app, non quella dell'account."
            )
            return False
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("Invio email non riuscito: %s", exc)
            return False

        return True
