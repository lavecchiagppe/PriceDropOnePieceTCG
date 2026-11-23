"""Canali di notifica delle segnalazioni."""

from __future__ import annotations

from ..config import Settings
from .base import Notifier, format_alert, format_body, format_title, notify_all
from .mail import EmailNotifier
from .telegram import TelegramNotifier
from .windows import WindowsToastNotifier

__all__ = [
    "EmailNotifier",
    "Notifier",
    "TelegramNotifier",
    "WindowsToastNotifier",
    "build_notifiers",
    "format_alert",
    "format_body",
    "format_title",
    "notify_all",
]


def build_notifiers(settings: Settings) -> list[Notifier]:
    """Costruisce i canali previsti dalla configurazione.

    I canali non configurati restano nell'elenco ma disattivati: `notify_all`
    li salta, e il comando di stato puo mostrare quali sono spenti.
    """
    return [
        TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id),
        EmailNotifier(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_user,
            settings.smtp_password,
            settings.email_to,
            settings.email_from,
        ),
        WindowsToastNotifier(active=settings.desktop_notifications),
    ]
