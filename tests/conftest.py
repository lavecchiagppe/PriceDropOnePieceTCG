"""Impostazioni comuni a tutti i test."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def sync_spento(monkeypatch: pytest.MonkeyPatch) -> None:
    """Impedisce ai test di allinearsi col cloud.

    La dashboard chiede un sync a ogni visita, e `CloudSync.enabled` e' vero
    su qualunque macchina abbia la CLI gh e il progetto in un repository git:
    cioe' il PC di chi sviluppa. Senza questo, la suite dei test della
    dashboard lancerebbe thread che scaricano artifact da GitHub, con esiti
    diversi a seconda della rete e del repository.

    I test che riguardano il sync lo riattivano da se', con dei finti al posto
    di gh.
    """
    monkeypatch.setenv("PRICEDROP_DISABLE_SYNC", "1")
