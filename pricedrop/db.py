"""Database SQLite: schema, connessione e scritture."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Iterable, Iterator, Sequence

from .listings import Quote
from .rules import Alert

SCHEMA = """
CREATE TABLE IF NOT EXISTS expansions (
    id              INTEGER PRIMARY KEY,
    game_id         INTEGER NOT NULL,
    code            TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    monitored       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    blueprint_id     INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    expansion_id     INTEGER,
    collector_number TEXT,
    -- Rarità come la dà CardTrader: "Super Rare", "Secret Rare", "Common"...
    -- Attenzione: "Alternate Art" è a sua volta un valore di rarità, quindi
    -- una SR alternativa non è "Super Rare" ma "Alternate Art". È proprio ciò
    -- che permette di filtrare le SR semplici.
    rarity           TEXT,
    -- Variante della stampa: "Alternate Art", "Special Rare", "Manga Panel".
    version          TEXT,
    image_url        TEXT,
    cardmarket_ids   TEXT,
    tcgplayer_id     TEXT,
    updated_at       TEXT    NOT NULL,
    FOREIGN KEY (expansion_id) REFERENCES expansions (id)
);

-- Serie storica: una riga per carta per ciclo di raccolta.
CREATE TABLE IF NOT EXISTS snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    blueprint_id            INTEGER NOT NULL,
    taken_at                TEXT    NOT NULL,
    currency                TEXT    NOT NULL,
    min_price_cents         REAL    NOT NULL,
    reference_price_cents   REAL    NOT NULL,
    eligible_count          INTEGER NOT NULL,
    best_listing_id         INTEGER,
    best_seller             TEXT,
    FOREIGN KEY (blueprint_id) REFERENCES cards (blueprint_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_card_time
    ON snapshots (blueprint_id, taken_at DESC);

-- Inserzioni segnalate, per non notificare due volte la stessa offerta.
CREATE TABLE IF NOT EXISTS alerts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint             TEXT    NOT NULL UNIQUE,
    blueprint_id            INTEGER NOT NULL,
    listing_id              INTEGER,
    rule                    TEXT    NOT NULL,
    price_cents             REAL    NOT NULL,
    reference_price_cents   REAL,
    discount_ratio          REAL,
    currency                TEXT,
    seller                  TEXT,
    created_at              TEXT    NOT NULL,
    notified_at             TEXT,
    FOREIGN KEY (blueprint_id) REFERENCES cards (blueprint_id)
);

CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);

-- Carte da seguire, con soglia personale opzionale.
CREATE TABLE IF NOT EXISTS watchlist (
    blueprint_id        INTEGER PRIMARY KEY,
    target_price_cents  INTEGER,
    note                TEXT,
    added_at            TEXT NOT NULL,
    FOREIGN KEY (blueprint_id) REFERENCES cards (blueprint_id)
);

-- Carte possedute, per valore della collezione e P&L.
CREATE TABLE IF NOT EXISTS collection (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    blueprint_id            INTEGER NOT NULL,
    quantity                INTEGER NOT NULL CHECK (quantity > 0),
    purchase_price_cents    INTEGER,
    condition               TEXT,
    acquired_at             TEXT,
    note                    TEXT,
    FOREIGN KEY (blueprint_id) REFERENCES cards (blueprint_id)
);

-- Esito di ogni ciclo di raccolta, per diagnosticare i buchi nello storico.
CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    expansions      INTEGER NOT NULL DEFAULT 0,
    cards_seen      INTEGER NOT NULL DEFAULT 0,
    snapshots_saved INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'running',
    error           TEXT
);
"""


def utcnow() -> str:
    """Timestamp ISO 8601 in UTC, con il fuso esplicito."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Colonne aggiunte dopo la prima versione dello schema. `CREATE TABLE IF NOT
# EXISTS` non tocca una tabella che esiste gia, quindi i database nati prima
# vanno completati a mano, una colonna per volta.
LATER_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("cards", "rarity", "TEXT"),
    ("cards", "version", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, kind in LATER_COLUMNS:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def connect(path: Path) -> sqlite3.Connection:
    """Apre il database, creando la cartella e lo schema se mancano."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    """Apre una connessione, con commit automatico e rollback in caso di errore."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_expansions(conn: sqlite3.Connection, expansions: Iterable[dict]) -> int:
    now = utcnow()
    rows = [
        (
            int(item["id"]),
            int(item.get("game_id") or 0),
            str(item.get("code") or ""),
            str(item.get("name") or ""),
            now,
        )
        for item in expansions
    ]
    conn.executemany(
        """
        INSERT INTO expansions (id, game_id, code, name, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            game_id = excluded.game_id,
            code = excluded.code,
            name = excluded.name,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    return len(rows)


def mark_monitored(conn: sqlite3.Connection, expansion_ids: Iterable[int]) -> None:
    """Segna quali espansioni sono sotto monitoraggio, azzerando le precedenti."""
    ids = list(expansion_ids)
    conn.execute("UPDATE expansions SET monitored = 0")
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE expansions SET monitored = 1 WHERE id IN ({placeholders})", ids
        )


def upsert_cards(conn: sqlite3.Connection, blueprints: Iterable[dict]) -> int:
    """Salva l'anagrafica delle carte di un'espansione.

    Numero di collezione e rarita stanno dentro `fixed_properties`, non in cima
    al blueprint: letti dalla radice risultavano sempre vuoti. I prodotti
    sigillati (box, sleeve, case) hanno `fixed_properties` vuoto, quindi
    restano senza rarita ed e cosi che si distinguono dalle carte.
    """
    now = utcnow()
    rows = []
    for item in blueprints:
        cardmarket = item.get("card_market_ids") or []
        fixed = item.get("fixed_properties") or {}
        rows.append(
            (
                int(item["id"]),
                str(item.get("name") or ""),
                item.get("expansion_id"),
                fixed.get("collector_number") or item.get("collector_number"),
                fixed.get("onepiece_rarity") or None,
                (item.get("version") or "").strip() or None,
                item.get("image_url"),
                ",".join(str(value) for value in cardmarket) or None,
                item.get("tcg_player_id"),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO cards (
            blueprint_id, name, expansion_id, collector_number, rarity,
            version, image_url, cardmarket_ids, tcgplayer_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (blueprint_id) DO UPDATE SET
            name = excluded.name,
            expansion_id = excluded.expansion_id,
            collector_number = excluded.collector_number,
            rarity = excluded.rarity,
            version = excluded.version,
            image_url = excluded.image_url,
            cardmarket_ids = excluded.cardmarket_ids,
            tcgplayer_id = excluded.tcgplayer_id,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    return len(rows)


def insert_snapshots(conn: sqlite3.Connection, quotes: Iterable[Quote]) -> int:
    """Salva uno snapshot per ogni quotazione calcolata."""
    now = utcnow()
    rows = [
        (
            quote.blueprint_id,
            now,
            quote.currency,
            quote.min_price_cents,
            quote.reference_price_cents,
            quote.eligible_count,
            quote.best.listing_id,
            quote.best.seller,
        )
        for quote in quotes
    ]
    conn.executemany(
        """
        INSERT INTO snapshots (
            blueprint_id, taken_at, currency, min_price_cents,
            reference_price_cents, eligible_count, best_listing_id, best_seller
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def trend_baselines(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    today: date | None = None,
) -> dict[int, tuple[float, int]]:
    """Mediana dei prezzi minimi per carta nella finestra, escluso oggi.

    Escludere il giorno corrente evita che le rilevazioni di stamattina
    diluiscano il confronto: la regola di trend deve misurare oggi contro il
    passato, non oggi contro se stesso.
    """
    today = today or datetime.now(timezone.utc).date()
    midnight = datetime.combine(today, time.min, tzinfo=timezone.utc)
    window_start = (midnight - timedelta(days=window_days)).isoformat(
        timespec="seconds"
    )
    today_start = midnight.isoformat(timespec="seconds")

    rows = conn.execute(
        """
        SELECT blueprint_id, min_price_cents
          FROM snapshots
         WHERE taken_at >= ? AND taken_at < ?
        """,
        (window_start, today_start),
    ).fetchall()

    grouped: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        grouped[int(row["blueprint_id"])].append(float(row["min_price_cents"]))

    return {
        blueprint_id: (median(prices), len(prices))
        for blueprint_id, prices in grouped.items()
    }


def record_alerts(
    conn: sqlite3.Connection, alerts: Iterable[Alert]
) -> list[tuple[int, Alert]]:
    """Salva le segnalazioni, scartando quelle gia viste.

    Il vincolo di unicita su `fingerprint` e cio che impedisce di notificare
    due volte la stessa offerta: restituisce solo le segnalazioni davvero
    nuove, che sono quelle da notificare.
    """
    now = utcnow()
    fresh: list[tuple[int, Alert]] = []

    for alert in alerts:
        cursor = conn.execute(
            """
            INSERT INTO alerts (
                fingerprint, blueprint_id, listing_id, rule, price_cents,
                reference_price_cents, discount_ratio, currency, seller,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fingerprint) DO NOTHING
            """,
            (
                alert.fingerprint,
                alert.blueprint_id,
                alert.listing_id,
                alert.rule,
                alert.price_cents,
                alert.reference_price_cents,
                alert.discount_ratio,
                alert.currency,
                alert.seller,
                now,
            ),
        )
        if cursor.rowcount:
            fresh.append((int(cursor.lastrowid), alert))

    return fresh


def mark_notified(conn: sqlite3.Connection, alert_ids: Sequence[int]) -> None:
    if not alert_ids:
        return
    now = utcnow()
    conn.executemany(
        "UPDATE alerts SET notified_at = ? WHERE id = ?",
        [(now, alert_id) for alert_id in alert_ids],
    )


def recent_alerts(
    conn: sqlite3.Connection,
    limit: int = 20,
    *,
    expansion_code: str | None = None,
    rarity: str | None = None,
) -> list[sqlite3.Row]:
    """Le segnalazioni piu recenti, filtrabili per espansione e per rarita."""
    clause = ""
    params: tuple = ()
    if expansion_code:
        clause += " AND e.code = ?"
        params += (expansion_code,)
    if rarity:
        clause += " AND c.rarity = ?"
        params += (rarity,)
    params += (limit,)
    return conn.execute(
        f"""
        SELECT a.id, a.rule, a.blueprint_id, a.price_cents,
               a.reference_price_cents, a.discount_ratio, a.currency,
               a.seller, a.created_at, a.notified_at, c.name AS card_name,
               c.image_url AS image_url, e.code AS expansion_code,
               c.rarity AS rarity
          FROM alerts a
          LEFT JOIN cards c USING (blueprint_id)
          LEFT JOIN expansions e ON e.id = c.expansion_id
         WHERE 1 = 1{clause}
         ORDER BY a.id DESC
         LIMIT ?
        """,
        params,
    ).fetchall()


def add_holding(
    conn: sqlite3.Connection,
    blueprint_id: int,
    *,
    quantity: int,
    purchase_price_cents: int | None = None,
    condition: str | None = None,
    acquired_at: str | None = None,
    note: str | None = None,
) -> int:
    """Registra copie possedute di una carta.

    Ogni acquisto e una riga a se: due copie comprate a prezzi diversi non
    vanno fuse, altrimenti il P&L del singolo acquisto si perde.
    """
    if quantity <= 0:
        raise ValueError("La quantita deve essere almeno 1.")

    cursor = conn.execute(
        """
        INSERT INTO collection (
            blueprint_id, quantity, purchase_price_cents, condition,
            acquired_at, note
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            blueprint_id,
            quantity,
            purchase_price_cents,
            condition,
            acquired_at or utcnow()[:10],
            note,
        ),
    )
    return int(cursor.lastrowid)


def update_holding(
    conn: sqlite3.Connection,
    holding_id: int,
    *,
    quantity: int,
    purchase_price_cents: int | None = None,
) -> None:
    if quantity <= 0:
        raise ValueError("La quantita deve essere almeno 1.")
    conn.execute(
        """
        UPDATE collection
           SET quantity = ?, purchase_price_cents = ?
         WHERE id = ?
        """,
        (quantity, purchase_price_cents, holding_id),
    )


def delete_holding(conn: sqlite3.Connection, holding_id: int) -> None:
    conn.execute("DELETE FROM collection WHERE id = ?", (holding_id,))


def start_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO collection_runs (started_at) VALUES (?)", (utcnow(),)
    )
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    expansions: int,
    cards_seen: int,
    snapshots_saved: int,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE collection_runs
           SET finished_at = ?, expansions = ?, cards_seen = ?,
               snapshots_saved = ?, status = ?, error = ?
         WHERE id = ?
        """,
        (utcnow(), expansions, cards_seen, snapshots_saved, status, error, run_id),
    )
