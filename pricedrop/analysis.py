"""Analisi dei trend di mercato.

Tutto qui dentro dipende dallo storico: senza rilevazioni ripetute nel tempo
non c'e niente da analizzare. Ogni funzione restituisce anche quanti punti ha
usato, cosi la pagina puo dire quanto fidarsi invece di mostrare un numero
nato da due misure.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median, pstdev

# Sotto questa soglia di punti un confronto non racconta un andamento.
MIN_POINTS = 3


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


@dataclass(frozen=True, slots=True)
class Coverage:
    """Quanto storico c'e davvero, e quindi quanto vale l'analisi."""

    days: float = 0.0
    snapshots: int = 0
    distinct_days: int = 0
    cards: int = 0

    def is_enough_for(self, window_days: int) -> bool:
        """Serve almeno mezza finestra, e piu di due giorni distinti."""
        return self.distinct_days >= 3 and self.days >= window_days / 2


def coverage(conn: sqlite3.Connection) -> Coverage:
    row = conn.execute(
        """
        SELECT COUNT(*) AS snapshots,
               COUNT(DISTINCT substr(taken_at, 1, 10)) AS distinct_days,
               COUNT(DISTINCT blueprint_id) AS cards,
               MIN(taken_at) AS first_at,
               MAX(taken_at) AS last_at
          FROM snapshots
        """
    ).fetchone()

    span = 0.0
    if row["first_at"] and row["last_at"]:
        try:
            span = (
                datetime.fromisoformat(row["last_at"])
                - datetime.fromisoformat(row["first_at"])
            ).total_seconds() / 86400.0
        except ValueError:
            span = 0.0

    return Coverage(
        days=max(span, 0.0),
        snapshots=row["snapshots"] or 0,
        distinct_days=row["distinct_days"] or 0,
        cards=row["cards"] or 0,
    )


@dataclass(frozen=True, slots=True)
class Mover:
    """Una carta che si e mossa nel periodo."""

    blueprint_id: int
    card_name: str
    image_url: str | None
    expansion_code: str | None
    first_cents: float
    last_cents: float
    points: int
    currency: str

    @property
    def change_cents(self) -> float:
        return self.last_cents - self.first_cents

    @property
    def change_ratio(self) -> float:
        if self.first_cents <= 0:
            return 0.0
        return self.change_cents / self.first_cents


MOVERS_SQL = """
WITH windowed AS (
    SELECT s.blueprint_id, s.taken_at, s.reference_price_cents, s.currency, s.id,
           ROW_NUMBER() OVER (
               PARTITION BY s.blueprint_id ORDER BY s.taken_at, s.id
           ) AS rn_asc,
           ROW_NUMBER() OVER (
               PARTITION BY s.blueprint_id ORDER BY s.taken_at DESC, s.id DESC
           ) AS rn_desc,
           COUNT(*) OVER (PARTITION BY s.blueprint_id) AS points
      FROM snapshots s
     WHERE s.taken_at >= ?
)
SELECT first_row.blueprint_id,
       c.name AS card_name,
       c.image_url AS image_url,
       e.code AS expansion_code,
       first_row.reference_price_cents AS first_cents,
       last_row.reference_price_cents AS last_cents,
       first_row.points AS points,
       last_row.currency AS currency
  FROM windowed AS first_row
  JOIN windowed AS last_row
    ON last_row.blueprint_id = first_row.blueprint_id AND last_row.rn_desc = 1
  LEFT JOIN cards c ON c.blueprint_id = first_row.blueprint_id
  LEFT JOIN expansions e ON e.id = c.expansion_id
 WHERE first_row.rn_asc = 1
   AND first_row.points >= ?
   AND first_row.reference_price_cents >= ?
"""


def filter_clause(
    expansion_code: str | None, rarity: str | None
) -> tuple[str, tuple]:
    """Clausola comune per restringere una lettura a un set o a una rarita.

    I valori entrano come parametri, mai concatenati nella query.
    """
    clause = ""
    params: tuple = ()
    if expansion_code:
        clause += " AND e.code = ?"
        params += (expansion_code,)
    if rarity:
        clause += " AND c.rarity = ?"
        params += (rarity,)
    return clause, params


def movers(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    limit: int = 10,
    min_points: int = MIN_POINTS,
    min_reference_cents: int = 100,
    min_change_cents: int = 100,
    expansion_code: str | None = None,
    rarity: str | None = None,
) -> tuple[list[Mover], list[Mover]]:
    """Le carte salite e scese di piu nel periodo.

    Il confronto e fra la prima e l'ultima rilevazione della finestra, sul
    prezzo di riferimento. `min_change_cents` esclude i movimenti che in euro
    non significano nulla, per lo stesso motivo per cui la regola di alert ha
    una soglia assoluta.
    """
    clause, extra = filter_clause(expansion_code, rarity)
    rows = conn.execute(
        MOVERS_SQL + clause,
        (_since(days), min_points, min_reference_cents) + extra,
    ).fetchall()

    found = [
        Mover(
            blueprint_id=int(row["blueprint_id"]),
            card_name=row["card_name"] or f"Carta {row['blueprint_id']}",
            image_url=row["image_url"],
            expansion_code=row["expansion_code"],
            first_cents=float(row["first_cents"]),
            last_cents=float(row["last_cents"]),
            points=int(row["points"]),
            currency=row["currency"] or "EUR",
        )
        for row in rows
    ]
    found = [
        mover for mover in found if abs(mover.change_cents) >= min_change_cents
    ]
    found.sort(key=lambda mover: mover.change_ratio, reverse=True)

    risers = [mover for mover in found if mover.change_ratio > 0][:limit]
    fallers = [mover for mover in found if mover.change_ratio < 0]
    fallers.sort(key=lambda mover: mover.change_ratio)

    return risers, fallers[:limit]


def market_index(
    conn: sqlite3.Connection, *, days: int = 90, min_cards: int = 5
) -> dict[str, list[tuple[str, float]]]:
    """Indice di prezzo per espansione, base 100 al primo giorno.

    Ogni carta contribuisce con il rapporto fra il suo prezzo del giorno e il
    suo prezzo iniziale; l'indice e la mediana di quei rapporti. Cosi una
    carta da 800 euro non domina un intero set, e l'indice misura il movimento
    tipico invece del valore totale.

    Solo le carte presenti anche nel primo giorno entrano nell'indice:
    altrimenti una carta che compare a meta periodo sposterebbe la linea senza
    che il suo prezzo si sia mosso.
    """
    rows = conn.execute(
        """
        SELECT substr(s.taken_at, 1, 10) AS day,
               s.blueprint_id,
               s.reference_price_cents,
               e.code AS expansion_code
          FROM snapshots s
          LEFT JOIN cards c ON c.blueprint_id = s.blueprint_id
          LEFT JOIN expansions e ON e.id = c.expansion_id
         WHERE s.taken_at >= ? AND e.code IS NOT NULL
         ORDER BY s.taken_at, s.id
        """,
        (_since(days),),
    ).fetchall()

    # Ultimo prezzo di ogni carta in ogni giorno, raggruppato per espansione.
    per_set: dict[str, dict[str, dict[int, float]]] = {}
    for row in rows:
        code = row["expansion_code"]
        day = row["day"]
        per_set.setdefault(code, {}).setdefault(day, {})[
            int(row["blueprint_id"])
        ] = float(row["reference_price_cents"])

    indexes: dict[str, list[tuple[str, float]]] = {}
    for code, days_map in per_set.items():
        ordered_days = sorted(days_map)
        if len(ordered_days) < 2:
            continue

        base = days_map[ordered_days[0]]
        if len(base) < min_cards:
            continue

        series: list[tuple[str, float]] = []
        for day in ordered_days:
            ratios = [
                price / base[blueprint_id]
                for blueprint_id, price in days_map[day].items()
                if blueprint_id in base and base[blueprint_id] > 0
            ]
            if len(ratios) < min_cards:
                continue
            series.append((day, median(ratios) * 100.0))

        if len(series) >= 2:
            indexes[code] = series

    return indexes


@dataclass(frozen=True, slots=True)
class Volatile:
    """Una carta il cui prezzo oscilla molto rispetto al suo livello."""

    blueprint_id: int
    card_name: str
    image_url: str | None
    expansion_code: str | None
    mean_cents: float
    swing_cents: float
    low_cents: float
    high_cents: float
    points: int

    @property
    def ratio(self) -> float:
        if self.mean_cents <= 0:
            return 0.0
        return self.swing_cents / self.mean_cents


def volatility(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    limit: int = 10,
    min_points: int = 5,
    min_reference_cents: int = 300,
    expansion_code: str | None = None,
    rarity: str | None = None,
) -> list[Volatile]:
    """Le carte con il prezzo piu instabile nel periodo.

    Si misura sulla deviazione standard del prezzo minimo rapportata alla sua
    media: una oscillazione di 5 euro significa poco su una carta da 500 e
    moltissimo su una da 10.
    """
    clause, extra = filter_clause(expansion_code, rarity)
    rows = conn.execute(
        f"""
        SELECT s.blueprint_id, s.min_price_cents,
               c.name AS card_name, c.image_url AS image_url,
               e.code AS expansion_code
          FROM snapshots s
          LEFT JOIN cards c ON c.blueprint_id = s.blueprint_id
          LEFT JOIN expansions e ON e.id = c.expansion_id
         WHERE s.taken_at >= ?{clause}
         ORDER BY s.taken_at
        """,
        (_since(days),) + extra,
    ).fetchall()

    grouped: dict[int, list[float]] = {}
    names: dict[int, tuple[str, str | None, str | None]] = {}
    for row in rows:
        blueprint_id = int(row["blueprint_id"])
        grouped.setdefault(blueprint_id, []).append(float(row["min_price_cents"]))
        names[blueprint_id] = (
            row["card_name"] or f"Carta {blueprint_id}",
            row["expansion_code"],
            row["image_url"],
        )

    found: list[Volatile] = []
    for blueprint_id, prices in grouped.items():
        if len(prices) < min_points:
            continue
        mean = sum(prices) / len(prices)
        if mean < min_reference_cents:
            continue
        name, code, image_url = names[blueprint_id]
        found.append(
            Volatile(
                blueprint_id=blueprint_id,
                card_name=name,
                image_url=image_url,
                expansion_code=code,
                mean_cents=mean,
                swing_cents=pstdev(prices),
                low_cents=min(prices),
                high_cents=max(prices),
                points=len(prices),
            )
        )

    found.sort(key=lambda item: item.ratio, reverse=True)
    return found[:limit]


def supply_series(
    conn: sqlite3.Connection,
    *,
    days: int = 90,
    expansion_code: str | None = None,
    rarity: str | None = None,
) -> list[tuple[str, float]]:
    """Inserzioni eleggibili per carta, giorno per giorno.

    E una misura dell'offerta: quando sale, di solito i prezzi scendono poco
    dopo. Si usa la mediana per non farsi trascinare dalle carte comunissime.
    """
    clause, extra = filter_clause(expansion_code, rarity)
    rows = conn.execute(
        f"""
        SELECT substr(s.taken_at, 1, 10) AS day, s.eligible_count
          FROM snapshots s
          LEFT JOIN cards c ON c.blueprint_id = s.blueprint_id
          LEFT JOIN expansions e ON e.id = c.expansion_id
         WHERE s.taken_at >= ?{clause}
         ORDER BY s.taken_at
        """,
        (_since(days),) + extra,
    ).fetchall()

    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["day"], []).append(float(row["eligible_count"]))

    return [(day, median(values)) for day, values in sorted(grouped.items())]
