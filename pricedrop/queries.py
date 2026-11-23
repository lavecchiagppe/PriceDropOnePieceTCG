"""Letture aggregate per la dashboard.

Separate dalle scritture di `db.py`: qui sta solo cio che serve a mostrare i
dati, in forma gia pronta per le pagine.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import utcnow


@dataclass(frozen=True, slots=True)
class Summary:
    """Stato complessivo della raccolta."""

    snapshots: int = 0
    cards: int = 0
    first_at: str | None = None
    last_at: str | None = None
    alerts_total: int = 0
    alerts_today: int = 0
    monitored_expansions: int = 0
    last_run_at: str | None = None
    last_run_status: str | None = None
    watchlist_size: int = 0

    @property
    def has_history(self) -> bool:
        return self.snapshots > 0

    @property
    def history_span_days(self) -> float:
        """Quanti giorni di storico ci sono davvero.

        Serve a dire in pagina quando la regola di trend e le analisi di
        mercato diventeranno significative.
        """
        if not (self.first_at and self.last_at):
            return 0.0
        try:
            start = datetime.fromisoformat(self.first_at)
            end = datetime.fromisoformat(self.last_at)
        except ValueError:
            return 0.0
        return max((end - start).total_seconds() / 86400.0, 0.0)


def summary(conn: sqlite3.Connection) -> Summary:
    snapshots = conn.execute(
        """
        SELECT COUNT(*) AS snapshots,
               COUNT(DISTINCT blueprint_id) AS cards,
               MIN(taken_at) AS first_at,
               MAX(taken_at) AS last_at
          FROM snapshots
        """
    ).fetchone()

    today = datetime.now(timezone.utc).date().isoformat()
    alerts = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN substr(created_at, 1, 10) = ? THEN 1 ELSE 0 END)
                   AS today
          FROM alerts
        """,
        (today,),
    ).fetchone()

    expansions = conn.execute(
        "SELECT COUNT(*) AS n FROM expansions WHERE monitored = 1"
    ).fetchone()

    run = conn.execute(
        """
        SELECT started_at, status FROM collection_runs
         ORDER BY id DESC LIMIT 1
        """
    ).fetchone()

    watchlist_count = conn.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()

    return Summary(
        snapshots=snapshots["snapshots"] or 0,
        cards=snapshots["cards"] or 0,
        first_at=snapshots["first_at"],
        last_at=snapshots["last_at"],
        alerts_total=alerts["total"] or 0,
        alerts_today=alerts["today"] or 0,
        monitored_expansions=expansions["n"] or 0,
        last_run_at=run["started_at"] if run else None,
        last_run_status=run["status"] if run else None,
        watchlist_size=watchlist_count["n"] or 0,
    )


def expansions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Le espansioni monitorate, con quante carte quotate ha ognuna.

    Serve al filtro della dashboard: mostra solo i set che hanno davvero
    qualcosa da vedere, cosi un set appena aggiunto non compare vuoto prima
    del primo ciclo di raccolta.
    """
    return conn.execute(
        """
        SELECT e.code, e.name, COUNT(DISTINCT s.blueprint_id) AS cards
          FROM expansions e
          LEFT JOIN cards c ON c.expansion_id = e.id
          LEFT JOIN snapshots s ON s.blueprint_id = c.blueprint_id
         WHERE e.monitored = 1 AND e.code IS NOT NULL
         GROUP BY e.id
        HAVING cards > 0
         ORDER BY e.code
        """
    ).fetchall()


# L'ultimo snapshot di ogni carta, con lo scostamento gia calcolato.
LATEST_SNAPSHOT_SQL = """
WITH ranked AS (
    SELECT s.*,
           ROW_NUMBER() OVER (
               PARTITION BY s.blueprint_id ORDER BY s.taken_at DESC, s.id DESC
           ) AS rn
      FROM snapshots s
)
SELECT r.blueprint_id, r.taken_at, r.currency, r.min_price_cents,
       r.reference_price_cents, r.eligible_count, r.best_seller,
       c.name AS card_name, c.image_url AS image_url, e.code AS expansion_code,
       c.rarity AS rarity, c.collector_number AS collector_number,
       (w.blueprint_id IS NOT NULL) AS watched,
       (1.0 - r.min_price_cents / r.reference_price_cents) AS discount
  FROM ranked r
  LEFT JOIN cards c USING (blueprint_id)
  LEFT JOIN expansions e ON e.id = c.expansion_id
  LEFT JOIN watchlist w ON w.blueprint_id = r.blueprint_id
 WHERE r.rn = 1 AND r.reference_price_cents > 0
"""


def rarities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Le rarita presenti fra le carte quotate, con quante carte ognuna.

    CardTrader tratta "Alternate Art" come una rarita a se: una Super Rare in
    versione alternativa non risulta "Super Rare". Filtrare su "Super Rare"
    da quindi le SR normali, senza le alternative.
    """
    return conn.execute(
        """
        SELECT c.rarity, COUNT(DISTINCT s.blueprint_id) AS cards
          FROM cards c
          JOIN snapshots s ON s.blueprint_id = c.blueprint_id
         WHERE c.rarity IS NOT NULL AND c.rarity <> ''
         GROUP BY c.rarity
         ORDER BY cards DESC, c.rarity
        """
    ).fetchall()


# Clausola e parametro del filtro per espansione, usati da piu letture.
# Il codice entra come parametro, mai concatenato nella query.
def _expansion_clause(code: str | None, rarity: str | None = None) -> tuple[str, tuple]:
    clause = ""
    params: tuple = ()
    if code:
        clause += " AND e.code = ?"
        params += (code,)
    if rarity:
        clause += " AND c.rarity = ?"
        params += (rarity,)
    return clause, params


def best_opportunities(
    conn: sqlite3.Connection,
    *,
    limit: int = 15,
    min_saving_cents: int = 0,
    expansion_code: str | None = None,
    rarity: str | None = None,
) -> list[sqlite3.Row]:
    """Le carte il cui prezzo minimo e piu staccato dal riferimento, ora.

    Non e lo stesso delle segnalazioni: qui si vede lo stato attuale del
    mercato, comprese le occasioni gia notificate in passato.
    """
    clause, params = _expansion_clause(expansion_code, rarity)
    return conn.execute(
        LATEST_SNAPSHOT_SQL
        + clause
        + """
   AND (r.reference_price_cents - r.min_price_cents) >= ?
 ORDER BY discount DESC, (r.reference_price_cents - r.min_price_cents) DESC
 LIMIT ?
        """,
        params + (min_saving_cents, limit),
    ).fetchall()


def most_valuable(
    conn: sqlite3.Connection,
    *,
    limit: int = 15,
    expansion_code: str | None = None,
    rarity: str | None = None,
) -> list[sqlite3.Row]:
    clause, params = _expansion_clause(expansion_code, rarity)
    return conn.execute(
        LATEST_SNAPSHOT_SQL + clause + " ORDER BY r.min_price_cents DESC LIMIT ?",
        params + (limit,),
    ).fetchall()


def watchlist(
    conn: sqlite3.Connection,
    *,
    expansion_code: str | None = None,
    rarity: str | None = None,
) -> list[sqlite3.Row]:
    clause, params = _expansion_clause(expansion_code, rarity)
    return conn.execute(
        LATEST_SNAPSHOT_SQL
        + clause
        + """
   AND w.blueprint_id IS NOT NULL
 ORDER BY discount DESC
        """,
        params,
    ).fetchall()


def card(conn: sqlite3.Connection, blueprint_id: int) -> sqlite3.Row | None:
    return conn.execute(
        LATEST_SNAPSHOT_SQL + " AND r.blueprint_id = ?", (blueprint_id,)
    ).fetchone()


def card_info(conn: sqlite3.Connection, blueprint_id: int) -> sqlite3.Row | None:
    """Anagrafica, anche per una carta che non ha ancora snapshot."""
    return conn.execute(
        """
        SELECT c.blueprint_id, c.name, c.image_url, c.cardmarket_ids,
               e.code AS expansion_code, e.name AS expansion_name,
               (w.blueprint_id IS NOT NULL) AS watched
          FROM cards c
          LEFT JOIN expansions e ON e.id = c.expansion_id
          LEFT JOIN watchlist w ON w.blueprint_id = c.blueprint_id
         WHERE c.blueprint_id = ?
        """,
        (blueprint_id,),
    ).fetchone()


def history(
    conn: sqlite3.Connection, blueprint_id: int, *, days: int = 90
) -> list[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    return conn.execute(
        """
        SELECT taken_at, min_price_cents, reference_price_cents, eligible_count
          FROM snapshots
         WHERE blueprint_id = ? AND taken_at >= ?
         ORDER BY taken_at
        """,
        (blueprint_id, since),
    ).fetchall()


def card_alerts(
    conn: sqlite3.Connection, blueprint_id: int, *, limit: int = 20
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT rule, price_cents, reference_price_cents, discount_ratio,
               currency, seller, created_at, notified_at
          FROM alerts
         WHERE blueprint_id = ?
         ORDER BY id DESC
         LIMIT ?
        """,
        (blueprint_id, limit),
    ).fetchall()


def search_cards(
    conn: sqlite3.Connection, term: str, *, limit: int = 40
) -> list[sqlite3.Row]:
    """Ricerca per nome. Il termine e passato come parametro, non concatenato."""
    pattern = f"%{term.strip()}%"
    return conn.execute(
        """
        SELECT c.blueprint_id, c.name, c.image_url, e.code AS expansion_code
          FROM cards c
          LEFT JOIN expansions e ON e.id = c.expansion_id
         WHERE c.name LIKE ?
         ORDER BY c.name
         LIMIT ?
        """,
        (pattern, limit),
    ).fetchall()


def toggle_watchlist(conn: sqlite3.Connection, blueprint_id: int) -> bool:
    """Aggiunge o rimuove una carta dalla watchlist. Restituisce lo stato nuovo."""
    existing = conn.execute(
        "SELECT 1 FROM watchlist WHERE blueprint_id = ?", (blueprint_id,)
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM watchlist WHERE blueprint_id = ?", (blueprint_id,))
        return False

    conn.execute(
        "INSERT INTO watchlist (blueprint_id, added_at) VALUES (?, ?)",
        (blueprint_id, utcnow()),
    )
    return True


@dataclass(frozen=True, slots=True)
class Holding:
    """Un acquisto registrato, con la sua valutazione attuale.

    Il valore usa il **prezzo di riferimento**, non il minimo: il minimo e
    spesso una singola offerta anomala, e valutarci una collezione darebbe un
    numero che oscilla a ogni ciclo di raccolta.
    """

    id: int
    blueprint_id: int
    card_name: str
    image_url: str | None
    expansion_code: str | None
    quantity: int
    purchase_price_cents: int | None
    condition: str | None
    acquired_at: str | None
    note: str | None
    unit_value_cents: float | None
    unit_min_cents: float | None
    currency: str
    valued_at: str | None

    @property
    def has_value(self) -> bool:
        return self.unit_value_cents is not None

    @property
    def has_cost(self) -> bool:
        return self.purchase_price_cents is not None

    @property
    def cost_total(self) -> float | None:
        if self.purchase_price_cents is None:
            return None
        return self.purchase_price_cents * self.quantity

    @property
    def value_total(self) -> float | None:
        if self.unit_value_cents is None:
            return None
        return self.unit_value_cents * self.quantity

    @property
    def pnl(self) -> float | None:
        """Guadagno o perdita. None quando manca il costo o la valutazione."""
        if self.cost_total is None or self.value_total is None:
            return None
        return self.value_total - self.cost_total

    @property
    def pnl_ratio(self) -> float | None:
        cost = self.cost_total
        if cost is None or cost <= 0 or self.pnl is None:
            return None
        return self.pnl / cost


HOLDINGS_SQL = """
WITH ranked AS (
    SELECT s.blueprint_id, s.reference_price_cents, s.min_price_cents,
           s.currency, s.taken_at,
           ROW_NUMBER() OVER (
               PARTITION BY s.blueprint_id ORDER BY s.taken_at DESC, s.id DESC
           ) AS rn
      FROM snapshots s
)
SELECT h.id, h.blueprint_id, h.quantity, h.purchase_price_cents, h.condition,
       h.acquired_at, h.note,
       c.name AS card_name, c.image_url AS image_url, e.code AS expansion_code,
       r.reference_price_cents, r.min_price_cents, r.currency, r.taken_at
  FROM collection h
  LEFT JOIN cards c USING (blueprint_id)
  LEFT JOIN expansions e ON e.id = c.expansion_id
  LEFT JOIN ranked r ON r.blueprint_id = h.blueprint_id AND r.rn = 1
 ORDER BY h.id DESC
"""


def holdings(conn: sqlite3.Connection) -> list[Holding]:
    rows = conn.execute(HOLDINGS_SQL).fetchall()
    return [
        Holding(
            id=int(row["id"]),
            blueprint_id=int(row["blueprint_id"]),
            card_name=row["card_name"] or f"Carta {row['blueprint_id']}",
            image_url=row["image_url"],
            expansion_code=row["expansion_code"],
            quantity=int(row["quantity"]),
            purchase_price_cents=row["purchase_price_cents"],
            condition=row["condition"],
            acquired_at=row["acquired_at"],
            note=row["note"],
            unit_value_cents=row["reference_price_cents"],
            unit_min_cents=row["min_price_cents"],
            currency=row["currency"] or "EUR",
            valued_at=row["taken_at"],
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Totali della collezione."""

    rows: int = 0
    copies: int = 0
    cost_total: float = 0.0
    value_total: float = 0.0
    valued_copies: int = 0
    unvalued_rows: int = 0
    rows_without_cost: int = 0

    @property
    def pnl(self) -> float:
        return self.value_total - self.cost_total

    @property
    def pnl_ratio(self) -> float | None:
        if self.cost_total <= 0:
            return None
        return self.pnl / self.cost_total

    @property
    def is_partial(self) -> bool:
        """Vero se qualche riga non e valutata o non ha costo.

        Serve a dire in pagina che i totali non coprono tutto, invece di
        mostrare un numero che sembra completo e non lo e.
        """
        return bool(self.unvalued_rows or self.rows_without_cost)


def portfolio(conn: sqlite3.Connection) -> Portfolio:
    """Totali calcolati solo sulle righe che hanno i dati necessari.

    Il costo somma le righe con prezzo di acquisto; il valore somma quelle
    valutate. Le righe incomplete vengono contate a parte, per poterlo dire.
    """
    items = holdings(conn)

    cost = sum(item.cost_total or 0.0 for item in items)
    value = sum(item.value_total or 0.0 for item in items)

    return Portfolio(
        rows=len(items),
        copies=sum(item.quantity for item in items),
        cost_total=cost,
        value_total=value,
        valued_copies=sum(item.quantity for item in items if item.has_value),
        unvalued_rows=sum(1 for item in items if not item.has_value),
        rows_without_cost=sum(1 for item in items if not item.has_cost),
    )


def portfolio_history(
    conn: sqlite3.Connection, *, days: int = 90
) -> list[tuple[str, float]]:
    """Valore della collezione giorno per giorno.

    Per ogni carta si prende l'ultima rilevazione di ciascun giorno; nei giorni
    senza rilevazione si trascina l'ultimo prezzo noto, altrimenti il totale
    crollerebbe ogni volta che una carta non viene quotata.
    """
    quantities = {
        int(row["blueprint_id"]): int(row["total"])
        for row in conn.execute(
            "SELECT blueprint_id, SUM(quantity) AS total FROM collection "
            "GROUP BY blueprint_id"
        ).fetchall()
    }
    if not quantities:
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    placeholders = ",".join("?" * len(quantities))
    rows = conn.execute(
        f"""
        SELECT blueprint_id, substr(taken_at, 1, 10) AS day,
               reference_price_cents, taken_at
          FROM snapshots
         WHERE blueprint_id IN ({placeholders}) AND taken_at >= ?
         ORDER BY taken_at
        """,
        (*quantities.keys(), since),
    ).fetchall()

    if not rows:
        return []

    # Ultimo prezzo di ogni carta, per ogni giorno in cui e stata rilevata.
    per_day: dict[str, dict[int, float]] = {}
    for row in rows:
        per_day.setdefault(row["day"], {})[int(row["blueprint_id"])] = float(
            row["reference_price_cents"]
        )

    series: list[tuple[str, float]] = []
    carried: dict[int, float] = {}
    for day in sorted(per_day):
        carried.update(per_day[day])
        total = sum(
            price * quantities.get(blueprint_id, 0)
            for blueprint_id, price in carried.items()
        )
        series.append((day, total))

    return series


def runs(conn: sqlite3.Connection, *, limit: int = 12) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT started_at, finished_at, expansions, cards_seen,
               snapshots_saved, status, error
          FROM collection_runs
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
