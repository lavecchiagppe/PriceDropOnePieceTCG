"""Notifiche come toast di Windows."""

from __future__ import annotations

import logging

from ..rules import Alert
from .base import format_body, format_title

logger = logging.getLogger(__name__)


class WindowsToastNotifier:
    """Mostra un toast di Windows per ogni segnalazione.

    La libreria viene importata solo al primo invio: cosi il progetto resta
    utilizzabile su altri sistemi, e su Windows non paga l'import quando le
    notifiche desktop sono disattivate.
    """

    name = "windows-toast"

    def __init__(self, *, active: bool = True) -> None:
        self._active = active
        self._toast = None
        self._unavailable = False

    @property
    def enabled(self) -> bool:
        return self._active and not self._unavailable

    def _load(self):
        if self._toast is not None:
            return self._toast
        try:
            from win11toast import notify  # type: ignore import-not-found
        except ImportError:
            logger.info(
                "Notifiche desktop non disponibili: manca win11toast "
                "(pip install win11toast)."
            )
            self._unavailable = True
            return None
        self._toast = notify
        return self._toast

    def send(self, alert: Alert) -> bool:
        if not self.enabled:
            return False

        notify = self._load()
        if notify is None:
            return False

        notify(
            title=format_title(alert),
            body=format_body(alert),
            on_click=alert.url,
        )
        return True
