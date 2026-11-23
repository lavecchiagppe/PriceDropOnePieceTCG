"""Notifiche via bot Telegram."""

from __future__ import annotations

import logging

import httpx

from ..rules import Alert
from .base import format_body, format_title

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Invia le segnalazioni a una chat Telegram.

    Il canale resta disattivato finche bot token e chat id non sono entrambi
    presenti: cosi il ciclo di raccolta funziona anche senza notifiche
    configurate, invece di fallire.
    """

    name = "telegram"

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        *,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._bot_token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._timeout = timeout
        self._client = client

    def __repr__(self) -> str:
        # Il token non compare mai nel repr.
        return f"<TelegramNotifier enabled={self.enabled}>"

    @property
    def enabled(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def send(self, alert: Alert) -> bool:
        if not self.enabled:
            return False

        text = f"{format_title(alert)}\n{format_body(alert)}"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }

        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.post(
                API_URL.format(token=self._bot_token), json=payload
            )
        except httpx.RequestError as exc:
            logger.warning("Telegram non raggiungibile: %s", exc)
            return False
        finally:
            if self._client is None:
                client.close()

        if response.status_code != 200:
            # Il corpo della risposta Telegram non contiene il token.
            logger.warning(
                "Telegram ha risposto %s: %s",
                response.status_code,
                response.text[:200],
            )
            return False

        return True
