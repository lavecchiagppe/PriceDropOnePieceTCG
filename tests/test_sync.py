"""Test dell'allineamento col database raccolto nel cloud.

Il punto delicato non e' il download, che e' una chiamata a gh, ma il travaso:
deve aggiungere cio' che manca senza duplicare le righe che ci sono gia' e,
soprattutto, senza toccare collezione e watchlist, che esistono solo in locale.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pricedrop import db
from pricedrop.config import FilterSettings, OutlierSettings, Settings, TrendSettings
from pricedrop.sync import CloudSync, SyncError, merge_cloud_into_local


def make_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        cardtrader_token="token-finto",
        expansion_codes=("op17",),
        database_path=tmp_path / "locale.db",
        filters=FilterSettings(),
        outlier=OutlierSettings(),
        trend=TrendSettings(),
        schedule_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def base(path: Path) -> None:
    """Espansione e carta, le righe a cui tutto il resto fa riferimento."""
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO expansions (id, game_id, code, name, monitored, "
            "updated_at) VALUES (4769, 15, 'op17', 'OP-17', 1, "
            "'2026-11-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO cards (blueprint_id, name, expansion_id, rarity, "
            "updated_at) VALUES (406121, 'Edward.Newgate', 4769, "
            "'Alternate Art', '2026-11-01T00:00:00+00:00')"
        )


def snapshot(path: Path, taken_at: str, prezzo: float = 1000.0) -> None:
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
            "min_price_cents, reference_price_cents, eligible_count) "
            "VALUES (406121, ?, 'EUR', ?, 1500.0, 6)",
            (taken_at, prezzo),
        )


def alert(path: Path, fingerprint: str) -> None:
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO alerts (fingerprint, blueprint_id, listing_id, rule, "
            "price_cents, reference_price_cents, discount_ratio, currency, "
            "seller, created_at) VALUES (?, 406121, 1, 'outlier', 800.0, "
            "1500.0, 0.46, 'EUR', 'negozio', '2026-11-22T10:00:05+00:00')",
            (fingerprint,),
        )


def conta(path: Path, tabella: str) -> int:
    with db.session(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabella}").fetchone()[0]


def test_porta_gli_snapshot_che_mancano(tmp_path: Path) -> None:
    locale, cloud = tmp_path / "locale.db", tmp_path / "cloud.db"
    for percorso in (locale, cloud):
        base(percorso)
    snapshot(locale, "2026-11-22T10:00:00+00:00")
    snapshot(cloud, "2026-11-22T10:00:00+00:00")
    snapshot(cloud, "2026-11-22T10:05:00+00:00")
    snapshot(cloud, "2026-11-22T10:10:00+00:00")

    risultato = merge_cloud_into_local(locale, cloud)

    assert risultato.snapshots == 2
    assert conta(locale, "snapshots") == 3


def test_non_duplica_quando_e_gia_allineato(tmp_path: Path) -> None:
    """Un sync ripetuto non deve gonfiare lo storico.

    E' il caso normale: la dashboard chiede un sync a ogni visita, e fra due
    cicli di raccolta non c'e' niente di nuovo da prendere.
    """
    locale, cloud = tmp_path / "locale.db", tmp_path / "cloud.db"
    for percorso in (locale, cloud):
        base(percorso)
    snapshot(cloud, "2026-11-22T10:00:00+00:00")

    primo = merge_cloud_into_local(locale, cloud)
    secondo = merge_cloud_into_local(locale, cloud)

    assert primo.snapshots == 1
    assert secondo.snapshots == 0
    assert secondo.empty
    assert conta(locale, "snapshots") == 1


def test_le_segnalazioni_si_riconoscono_dal_fingerprint(tmp_path: Path) -> None:
    locale, cloud = tmp_path / "locale.db", tmp_path / "cloud.db"
    for percorso in (locale, cloud):
        base(percorso)
    alert(locale, "outlier:406121:1:800")
    alert(cloud, "outlier:406121:1:800")
    alert(cloud, "trend:406121:2:700")

    risultato = merge_cloud_into_local(locale, cloud)

    assert risultato.alerts == 1
    assert conta(locale, "alerts") == 2


def test_collezione_e_watchlist_locali_sopravvivono(tmp_path: Path) -> None:
    """Il motivo per cui il sync travasa invece di sovrascrivere il file.

    Collezione e watchlist si compilano dalla dashboard e il cloud non le
    scrive mai: prendere il file remoto come verita' le cancellerebbe.
    """
    locale, cloud = tmp_path / "locale.db", tmp_path / "cloud.db"
    for percorso in (locale, cloud):
        base(percorso)
    with db.session(locale) as conn:
        conn.execute(
            "INSERT INTO collection (blueprint_id, quantity, "
            "purchase_price_cents) VALUES (406121, 2, 90000)"
        )
        conn.execute(
            "INSERT INTO watchlist (blueprint_id, added_at) "
            "VALUES (406121, '2026-11-20T09:00:00+00:00')"
        )
    snapshot(cloud, "2026-11-22T10:00:00+00:00")

    merge_cloud_into_local(locale, cloud)

    assert conta(locale, "collection") == 1
    assert conta(locale, "watchlist") == 1
    with db.session(locale) as conn:
        riga = conn.execute("SELECT quantity FROM collection").fetchone()
    assert riga["quantity"] == 2


def test_porta_le_carte_nuove_prima_dei_loro_snapshot(tmp_path: Path) -> None:
    """Le chiavi esterne restano attive: l'ordine del travaso conta.

    Una carta vista per la prima volta nel cloud arriva insieme ai suoi
    snapshot, e senza l'ordine giusto l'inserimento violerebbe il vincolo.
    """
    locale, cloud = tmp_path / "locale.db", tmp_path / "cloud.db"
    base(locale)
    base(cloud)
    with db.session(cloud) as conn:
        conn.execute(
            "INSERT INTO cards (blueprint_id, name, expansion_id, rarity, "
            "updated_at) VALUES (999001, 'Carta nuova', 4769, 'Secret Rare', "
            "'2026-11-23T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
            "min_price_cents, reference_price_cents, eligible_count) "
            "VALUES (999001, '2026-11-23T10:00:00+00:00', 'EUR', 500.0, "
            "800.0, 5)"
        )

    risultato = merge_cloud_into_local(locale, cloud)

    assert risultato.cards == 1
    assert risultato.snapshots == 1
    with db.session(locale) as conn:
        nome = conn.execute(
            "SELECT name FROM cards WHERE blueprint_id = 999001"
        ).fetchone()["name"]
    assert nome == "Carta nuova"


def test_tollera_una_colonna_in_piu_nel_cloud(tmp_path: Path) -> None:
    """Un database remoto con lo schema piu' nuovo non deve bloccare il sync.

    Succede fra il momento in cui il workflow gira col codice aggiornato e
    quello in cui il PC lo aggiorna.
    """
    locale, cloud = tmp_path / "locale.db", tmp_path / "cloud.db"
    for percorso in (locale, cloud):
        base(percorso)
    with sqlite3.connect(cloud) as conn:
        conn.execute("ALTER TABLE snapshots ADD COLUMN sconosciuta TEXT")
    snapshot(cloud, "2026-11-22T10:00:00+00:00")

    risultato = merge_cloud_into_local(locale, cloud)

    assert risultato.snapshots == 1


def test_errore_leggibile_se_il_file_non_ce(tmp_path: Path) -> None:
    locale = tmp_path / "locale.db"
    base(locale)
    with pytest.raises(SyncError):
        merge_cloud_into_local(locale, tmp_path / "mai-scaricato.db")


def test_intervallo_minimo_blocca_il_sync_automatico(tmp_path: Path) -> None:
    """Navigare fra le pagine non deve scaricare un artifact per clic."""
    sync = CloudSync(make_settings(tmp_path), min_interval_seconds=3600)
    assert sync.due is True
    sync._last_attempt = __import__("time").monotonic()
    assert sync.due is False


def test_disattivato_dalla_variabile_dambiente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRICEDROP_DISABLE_SYNC", "1")
    sync = CloudSync(make_settings(tmp_path))
    assert sync.enabled is False
    # E se e' spento, il sync automatico non parte nemmeno.
    assert sync.sync_in_background() is False


def test_il_pulsante_appare_solo_se_il_sync_e_disponibile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un pulsante che non puo' funzionare non va mostrato.

    Su un PC senza gh, o fuori da un repository git, il sync non ha sorgente.
    """
    from fastapi.testclient import TestClient

    from pricedrop.web.app import create_app

    settings = make_settings(tmp_path)
    base(settings.database_path)

    with TestClient(create_app(settings)) as client:
        assert "Aggiorna dal cloud" not in client.get("/").text

    monkeypatch.setattr(CloudSync, "enabled", property(lambda self: True))
    # Il sync automatico non deve partire davvero durante il test.
    monkeypatch.setattr(CloudSync, "sync_in_background", lambda self: False)
    with TestClient(create_app(settings)) as client:
        assert "Aggiorna dal cloud" in client.get("/").text


def test_il_pulsante_non_rompe_la_pagina_se_il_sync_fallisce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rete giu' o gh non autenticata: si torna alla pagina, col motivo nel footer."""
    from fastapi.testclient import TestClient

    from pricedrop.web.app import create_app

    settings = make_settings(tmp_path)
    base(settings.database_path)

    def fallisce(self) -> None:
        self.state.error = "gh non e' autenticato: esegui gh auth login."
        raise SyncError(self.state.error)

    monkeypatch.setattr(CloudSync, "enabled", property(lambda self: True))
    monkeypatch.setattr(CloudSync, "sync_in_background", lambda self: False)
    monkeypatch.setattr(CloudSync, "sync_now", fallisce)

    with TestClient(create_app(settings)) as client:
        risposta = client.post("/sync", data={"back": "/"}, follow_redirects=True)

    assert risposta.status_code == 200
    assert "Allineamento non riuscito" in risposta.text


def test_sceglie_l_artifact_piu_recente_non_il_primo() -> None:
    """L'API di GitHub non ordina gli artifact per data.

    Caso reale: l'artifact delle 21:13 arrivava prima di quello delle 21:15,
    e aveva pure l'id piu' alto. Prendere il primo dell'elenco allineava la
    dashboard a una rilevazione piu' vecchia di quella disponibile.
    """
    from pricedrop.sync import pick_newest

    elenco = [
        {"id": 14863601936, "created_at": "2026-11-23T21:13:26Z"},
        {"id": 14862473644, "created_at": "2026-11-23T21:15:02Z"},
        {"id": 14841000302, "created_at": "2026-11-23T16:47:04Z"},
    ]

    assert pick_newest(elenco)["id"] == 14862473644


def test_ignora_gli_artifact_scaduti() -> None:
    """Dopo la scadenza il file non c'e' piu', solo la riga nell'elenco."""
    from pricedrop.sync import pick_newest

    elenco = [
        {"id": 2, "created_at": "2026-11-23T21:15:02Z", "expired": True},
        {"id": 1, "created_at": "2026-11-20T10:00:00Z", "expired": False},
    ]

    assert pick_newest(elenco)["id"] == 1

    with pytest.raises(SyncError):
        pick_newest([{"id": 3, "created_at": "x", "expired": True}])


def test_errore_leggibile_senza_artifact() -> None:
    from pricedrop.sync import pick_newest

    with pytest.raises(SyncError, match="Nessun database pubblicato"):
        pick_newest([])
