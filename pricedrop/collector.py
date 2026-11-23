"""Ciclo di raccolta: dal provider al database.

Una esecuzione scarica il book delle espansioni monitorate, calcola per ogni
carta prezzo minimo e prezzo di riferimento, e salva uno snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db, rules
from .config import Settings
from .listings import Quote, build_quote
from .notifiers.base import Notifier, notify_all
from .providers.base import PriceProvider

logger = logging.getLogger(__name__)

# Tetto alle riverifiche per ciclo: con soglie molto larghe le candidate
# potrebbero diventare centinaia, e una chiamata a carta non avrebbe senso.
MAX_VERIFICATIONS = 60

# Si riverifica anche chi sta sotto la soglia di alert, entro questo margine.
# Il prezzo in cache e risultato piu ALTO di quello vero, quindi sottostima lo
# scostamento: una carta che sembra a -29% con soglia a -35% potrebbe essere
# gia un affare, e senza margine non la guarderemmo mai.
VERIFY_MARGIN = 0.7


@dataclass(slots=True)
class CollectResult:
    """Esito di un ciclo di raccolta."""

    expansions: list[dict] = field(default_factory=list)
    cards_seen: int = 0
    snapshots_saved: int = 0
    skipped_thin: int = 0
    alerts: list[rules.Alert] = field(default_factory=list)
    notifications_sent: dict[str, int] = field(default_factory=dict)
    trend_rule_active: bool = False
    verified: int = 0

    @property
    def expansion_count(self) -> int:
        return len(self.expansions)

    @property
    def alert_count(self) -> int:
        return len(self.alerts)


class NoExpansionsConfigured(RuntimeError):
    """Nessuna espansione da monitorare in config.toml."""


def resolve_expansions(provider: PriceProvider, settings: Settings) -> list[dict]:
    """Traduce la configurazione negli oggetti espansione del provider.

    Gli id configurati hanno precedenza sui codici. Se non e configurato nulla,
    solleva un errore invece di scaricare silenziosamente tutto il catalogo.
    """
    all_expansions = provider.expansions()

    game_id = None
    finder = getattr(provider, "find_game_id", None)
    if callable(finder):
        game_id = finder("one piece")

    candidates = [
        item
        for item in all_expansions
        if game_id is None or int(item.get("game_id") or 0) == game_id
    ]

    if settings.expansion_ids:
        wanted = set(settings.expansion_ids)
        chosen = [item for item in candidates if int(item["id"]) in wanted]
    elif settings.expansion_codes:
        wanted_codes = {code.lower() for code in settings.expansion_codes}
        chosen = [
            item
            for item in candidates
            if str(item.get("code") or "").lower() in wanted_codes
        ]
    else:
        raise NoExpansionsConfigured(
            "Nessuna espansione configurata. Esegui 'python -m pricedrop sets' "
            "per vedere i codici disponibili e scrivili in config.toml, "
            "nella voce monitor.expansion_codes."
        )

    if not chosen:
        raise NoExpansionsConfigured(
            "Le espansioni in config.toml non corrispondono a nessuna "
            "espansione One Piece su CardTrader. Controlla i codici con "
            "'python -m pricedrop sets'."
        )

    return chosen


def quotes_for_expansion(
    provider: PriceProvider,
    settings: Settings,
    expansion_id: int,
    rarity_by_blueprint: dict[int, str | None] | None = None,
) -> tuple[list[Quote], int, int]:
    """Calcola le quotazioni di una espansione.

    Restituisce le quotazioni, il numero di carte viste e quante sono state
    scartate per mercato troppo sottile.

    La soglia `min_reference_cents` esiste per non storicizzare i comuni da
    pochi centesimi, ma colpiva anche le rarita da collezione quando il loro
    prezzo e basso: una Super Rare a 80 centesimi resta una SR, e chi la segue
    vuole vederne la curva. Le rarita elencate in `track_rarities` passano
    quindi la soglia.
    """
    books = provider.listings_by_expansion(expansion_id, language=settings.language)
    rarity_by_blueprint = rarity_by_blueprint or {}
    always = {value.casefold() for value in settings.track_rarities}

    quotes: list[Quote] = []
    skipped = 0
    for blueprint_id, listings in books.items():
        quote = build_quote(
            listings,
            reference_window=settings.outlier.reference_window,
            min_comparables=settings.outlier.min_comparables,
            conditions=settings.filters.conditions,
            language=settings.language,
            require_cardtrader_zero=settings.filters.require_cardtrader_zero,
            exclude_graded=settings.filters.exclude_graded,
        )
        if quote is None:
            skipped += 1
            continue
        if quote.reference_price_cents < settings.outlier.min_reference_cents:
            rarity = (rarity_by_blueprint.get(blueprint_id) or "").casefold()
            if rarity not in always:
                skipped += 1
                continue
        quotes.append(quote)

    return quotes, len(books), skipped


def watched_cards(conn) -> set[int]:
    return {
        int(row["blueprint_id"])
        for row in conn.execute("SELECT blueprint_id FROM watchlist").fetchall()
    }


def refresh_candidates(
    provider: PriceProvider,
    settings: Settings,
    quotes: list[Quote],
    watched: set[int],
    *,
    limit: int | None = None,
) -> tuple[list[Quote], int]:
    """Riverifica le carte interessanti con una chiamata dedicata.

    La risposta per espansione e memorizzata in cache piu a lungo di quella per
    singola carta: su una stessa inserzione i due endpoint possono dare prezzi
    diversi, e quello per espansione e il piu vecchio. Misurato dal vivo:
    la stessa inserzione a 63,16 per blueprint e 64,46 per espansione.

    Per lo storico la differenza e trascurabile, ma per un alert no: la nostra
    regola esiste proprio per cogliere il ribasso appena compare, ed e esatta-
    mente quello che la cache nasconde. Quindi le carte che stanno per generare
    una segnalazione, e quelle che segui, vengono rilette una per una.
    """
    limit = MAX_VERIFICATIONS if limit is None else limit
    bar = settings.outlier.threshold * VERIFY_MARGIN

    candidates = [
        quote
        for quote in quotes
        if quote.discount_ratio >= bar or quote.blueprint_id in watched
    ]
    candidates.sort(key=lambda quote: quote.discount_ratio, reverse=True)

    if len(candidates) > limit:
        logger.warning(
            "%s carte da riverificare, ne controllo %s: allenta le soglie o "
            "riduci la watchlist se succede spesso",
            len(candidates),
            limit,
        )
        candidates = candidates[:limit]

    wanted = {quote.blueprint_id for quote in candidates}
    if not wanted:
        return quotes, 0

    refreshed: dict[int, Quote] = {}
    for blueprint_id in wanted:
        listings = provider.listings_by_blueprint(
            blueprint_id, language=settings.language
        )
        quote = build_quote(
            listings,
            reference_window=settings.outlier.reference_window,
            min_comparables=settings.outlier.min_comparables,
            conditions=settings.filters.conditions,
            language=settings.language,
            require_cardtrader_zero=settings.filters.require_cardtrader_zero,
            exclude_graded=settings.filters.exclude_graded,
        )
        if quote is not None:
            refreshed[blueprint_id] = quote

    return [
        refreshed.get(quote.blueprint_id, quote) for quote in quotes
    ], len(refreshed)


def collect(
    settings: Settings,
    provider: PriceProvider,
    notifiers: list[Notifier] | None = None,
) -> CollectResult:
    """Esegue un ciclo completo di raccolta e lo registra nel database.

    Le notifiche partono dopo la chiusura della transazione: tenere aperta una
    scrittura sul database mentre si attende una risposta da Telegram non serve
    a nulla e la espone a un blocco inutile.
    """
    result = CollectResult()
    today = datetime.now(timezone.utc).date()
    fresh: list[tuple[int, rules.Alert]] = []

    with db.session(settings.database_path) as conn:
        run_id = db.start_run(conn)
        try:
            all_expansions = provider.expansions()
            db.upsert_expansions(conn, all_expansions)

            result.expansions = resolve_expansions(provider, settings)
            db.mark_monitored(conn, [int(item["id"]) for item in result.expansions])

            # Le soglie storiche si leggono prima di scrivere gli snapshot di
            # oggi, e comunque escludono il giorno corrente.
            baselines = db.trend_baselines(
                conn, window_days=settings.trend.window_days, today=today
            )
            result.trend_rule_active = any(
                points >= settings.trend.min_points
                for _, points in baselines.values()
            )

            all_quotes: list[Quote] = []
            watched = watched_cards(conn)

            for expansion in result.expansions:
                expansion_id = int(expansion["id"])
                label = expansion.get("code") or expansion_id

                blueprints = provider.blueprints(expansion_id)
                db.upsert_cards(conn, blueprints)

                # La rarita arriva con l'anagrafica, dentro fixed_properties:
                # serve subito, perche decide quali carte sotto soglia vanno
                # storicizzate lo stesso.
                rarity_by_blueprint = {
                    int(item["id"]): (item.get("fixed_properties") or {}).get(
                        "onepiece_rarity"
                    )
                    for item in blueprints
                    if item.get("id") is not None
                }

                quotes, seen, skipped = quotes_for_expansion(
                    provider, settings, expansion_id, rarity_by_blueprint
                )
                # Prima di salvare e di segnalare, le carte interessanti
                # vengono rilette dall'endpoint non messo in cache.
                quotes, verified = refresh_candidates(
                    provider, settings, quotes, watched
                )
                result.verified += verified

                saved = db.insert_snapshots(conn, quotes)
                all_quotes.extend(quotes)

                result.cards_seen += seen
                result.snapshots_saved += saved
                result.skipped_thin += skipped
                logger.info(
                    "%s: %s carte, %s snapshot, %s scartate",
                    label,
                    seen,
                    saved,
                    skipped,
                )

            candidates = rules.evaluate(
                all_quotes,
                outlier=settings.outlier,
                trend=settings.trend,
                baselines=baselines,
                day=today.isoformat(),
            )
            fresh = db.record_alerts(conn, candidates)
            result.alerts = [alert for _, alert in fresh]
            logger.info(
                "%s segnalazioni, %s nuove", len(candidates), len(fresh)
            )

        except Exception as exc:
            db.finish_run(
                conn,
                run_id,
                expansions=result.expansion_count,
                cards_seen=result.cards_seen,
                snapshots_saved=result.snapshots_saved,
                status="error",
                error=str(exc)[:500],
            )
            # Commit esplicito: senza questo il rollback del context manager
            # cancellerebbe anche la registrazione dell'errore, lasciando un
            # ciclo fallito senza alcuna traccia. Gli snapshot gia raccolti
            # prima dell'errore restano salvati: sono dati di mercato validi.
            conn.commit()
            raise

        db.finish_run(
            conn,
            run_id,
            expansions=result.expansion_count,
            cards_seen=result.cards_seen,
            snapshots_saved=result.snapshots_saved,
            status="ok",
        )

    if fresh and notifiers:
        outcome = notify_all(notifiers, [alert for _, alert in fresh])
        result.notifications_sent = outcome.sent

        delivered_ids = [
            alert_id
            for alert_id, alert in fresh
            if alert.fingerprint in outcome.delivered
        ]
        if delivered_ids:
            with db.session(settings.database_path) as conn:
                db.mark_notified(conn, delivered_ids)

    return result
