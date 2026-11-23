"""Test del ciclo di raccolta e della persistenza, con un provider finto."""

from __future__ import annotations

from datetime import date

import pytest

from pricedrop import db
from pricedrop.collector import (
    NoExpansionsConfigured,
    collect,
    resolve_expansions,
)
from pricedrop.config import FilterSettings, OutlierSettings, Settings
from pricedrop.listings import Listing

GAMES = [
    {"id": 1, "name": "Magic", "display_name": "Magic: the Gathering"},
    {"id": 15, "name": "One Piece", "display_name": "One Piece Card Game"},
]

EXPANSIONS = [
    {"id": 100, "game_id": 1, "code": "grn", "name": "Guilds of Ravnica"},
    {"id": 3200, "game_id": 15, "code": "op09", "name": "Emperors in the New World"},
    {"id": 3300, "game_id": 15, "code": "op10", "name": "Royal Blood"},
]


def listing(listing_id: int, blueprint_id: int, price_cents: int, **overrides) -> Listing:
    defaults = dict(
        listing_id=listing_id,
        blueprint_id=blueprint_id,
        name=f"Carta {blueprint_id}",
        price_cents=price_cents,
        currency="EUR",
        bundle_size=1,
        quantity=1,
        condition="Near Mint",
        language="en",
        can_sell_via_hub=True,
        seller="negozio",
    )
    defaults.update(overrides)
    return Listing(**defaults)


class FakeProvider:
    """Provider in memoria, per esercitare il collector senza rete."""

    name = "fake"

    def __init__(
        self,
        books: dict[int, dict[int, list[Listing]]] | None = None,
        fresh: dict[int, list[Listing]] | None = None,
        rarities: dict[int, str] | None = None,
    ):
        self.books = books or {}
        # Rarita per blueprint, come la restituisce l'anagrafica vera.
        self.rarities = rarities or {}
        # `fresh` simula l'endpoint per singola carta, che restituisce prezzi
        # piu aggiornati di quello per espansione.
        self.fresh = fresh or {}
        self.expansion_calls: list[int] = []
        self.blueprint_calls: list[int] = []

    def games(self) -> list[dict]:
        return GAMES

    def expansions(self) -> list[dict]:
        return EXPANSIONS

    def blueprints(self, expansion_id: int) -> list[dict]:
        return [
            {
                "id": blueprint_id,
                "name": f"Carta {blueprint_id}",
                "expansion_id": expansion_id,
                "image_url": None,
                "card_market_ids": [999],
                "tcg_player_id": None,
                "version": None,
                "fixed_properties": (
                    {
                        "collector_number": f"OP09-{blueprint_id}",
                        "onepiece_rarity": self.rarities[blueprint_id],
                    }
                    if blueprint_id in self.rarities
                    else {}
                ),
            }
            for blueprint_id in self.books.get(expansion_id, {})
        ]

    def listings_by_expansion(self, expansion_id, *, language=None):
        self.expansion_calls.append(expansion_id)
        return self.books.get(expansion_id, {})

    def listings_by_blueprint(self, blueprint_id, *, language=None):
        self.blueprint_calls.append(blueprint_id)
        if blueprint_id in self.fresh:
            return self.fresh[blueprint_id]
        for book in self.books.values():
            if blueprint_id in book:
                return book[blueprint_id]
        return []

    def find_game_id(self, needle: str = "one piece") -> int | None:
        for game in self.games():
            if needle.lower() in game["display_name"].lower():
                return int(game["id"])
        return None


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        cardtrader_token="token-finto",
        expansion_codes=("op09",),
        database_path=tmp_path / "test.db",
        filters=FilterSettings(),
        outlier=OutlierSettings(),
    )
    defaults.update(overrides)
    return Settings(**defaults)


# -- risoluzione delle espansioni -------------------------------------------


def test_resolves_expansion_by_code(tmp_path):
    settings = make_settings(tmp_path, expansion_codes=("op09",))
    chosen = resolve_expansions(FakeProvider(), settings)
    assert [item["id"] for item in chosen] == [3200]


def test_resolves_expansion_by_id_with_precedence_over_codes(tmp_path):
    settings = make_settings(
        tmp_path, expansion_codes=("op09",), expansion_ids=(3300,)
    )
    chosen = resolve_expansions(FakeProvider(), settings)
    assert [item["id"] for item in chosen] == [3300]


def test_code_matching_ignores_case(tmp_path):
    settings = make_settings(tmp_path, expansion_codes=("OP10",))
    chosen = resolve_expansions(FakeProvider(), settings)
    assert [item["id"] for item in chosen] == [3300]


def test_does_not_match_expansions_of_other_games(tmp_path):
    """Il codice di un set Magic non deve finire nel monitoraggio One Piece."""
    settings = make_settings(tmp_path, expansion_codes=("grn",))
    with pytest.raises(NoExpansionsConfigured):
        resolve_expansions(FakeProvider(), settings)


def test_refuses_to_run_without_configuration(tmp_path):
    """Senza espansioni configurate non si scarica tutto il catalogo: si ferma."""
    settings = make_settings(tmp_path, expansion_codes=(), expansion_ids=())
    with pytest.raises(NoExpansionsConfigured):
        resolve_expansions(FakeProvider(), settings)


# -- ciclo di raccolta -------------------------------------------------------


def books_with_one_good_and_one_thin_card() -> dict:
    liquid = [listing(i, 5001, 500) for i in range(1, 6)]
    liquid.insert(0, listing(99, 5001, 250))
    thin = [listing(200, 5002, 400), listing(201, 5002, 410)]
    return {3200: {5001: liquid, 5002: thin}}


def test_collect_saves_snapshot_only_for_liquid_card(tmp_path):
    settings = make_settings(tmp_path)
    provider = FakeProvider(books_with_one_good_and_one_thin_card())

    result = collect(settings, provider)

    assert result.expansion_count == 1
    assert result.cards_seen == 2
    assert result.snapshots_saved == 1
    assert result.skipped_thin == 1


def test_collect_persists_snapshot_values(tmp_path):
    settings = make_settings(tmp_path)
    provider = FakeProvider(books_with_one_good_and_one_thin_card())

    collect(settings, provider)

    with db.session(settings.database_path) as conn:
        row = conn.execute(
            """
            SELECT blueprint_id, min_price_cents, reference_price_cents,
                   eligible_count, best_listing_id, currency
              FROM snapshots
            """
        ).fetchone()

    assert row["blueprint_id"] == 5001
    assert row["min_price_cents"] == 250.0
    assert row["reference_price_cents"] == 500.0
    assert row["eligible_count"] == 6
    assert row["best_listing_id"] == 99
    assert row["currency"] == "EUR"


def test_collect_records_cards_and_expansions(tmp_path):
    settings = make_settings(tmp_path)
    collect(settings, FakeProvider(books_with_one_good_and_one_thin_card()))

    with db.session(settings.database_path) as conn:
        cards = conn.execute("SELECT blueprint_id FROM cards ORDER BY blueprint_id").fetchall()
        monitored = conn.execute(
            "SELECT id FROM expansions WHERE monitored = 1"
        ).fetchall()
        all_expansions = conn.execute("SELECT COUNT(*) AS n FROM expansions").fetchone()

    assert [row["blueprint_id"] for row in cards] == [5001, 5002]
    assert [row["id"] for row in monitored] == [3200]
    # Tutte le espansioni vengono registrate, solo quelle scelte sono monitorate.
    assert all_expansions["n"] == 3


def test_collect_logs_a_successful_run(tmp_path):
    settings = make_settings(tmp_path)
    collect(settings, FakeProvider(books_with_one_good_and_one_thin_card()))

    with db.session(settings.database_path) as conn:
        run = conn.execute("SELECT * FROM collection_runs").fetchone()

    assert run["status"] == "ok"
    assert run["snapshots_saved"] == 1
    assert run["finished_at"] is not None


def test_collect_records_failure_and_reraises(tmp_path):
    class BrokenProvider(FakeProvider):
        def blueprints(self, expansion_id):
            raise RuntimeError("API non raggiungibile")

    settings = make_settings(tmp_path)

    with pytest.raises(RuntimeError, match="API non raggiungibile"):
        collect(settings, BrokenProvider(books_with_one_good_and_one_thin_card()))

    with db.session(settings.database_path) as conn:
        run = conn.execute("SELECT * FROM collection_runs").fetchone()

    assert run["status"] == "error"
    assert "API non raggiungibile" in run["error"]


def test_repeated_collections_accumulate_history(tmp_path):
    """Due cicli sulla stessa carta producono due punti nella serie storica."""
    settings = make_settings(tmp_path)
    provider = FakeProvider(books_with_one_good_and_one_thin_card())

    collect(settings, provider)
    collect(settings, provider)

    with db.session(settings.database_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE blueprint_id = 5001"
        ).fetchone()

    assert count["n"] == 2


def books_with_an_expensive_outlier() -> dict:
    """Una carta di valore con una inserzione molto sotto il resto."""
    listings = [listing(10, 7001, 80054)]
    listings += [listing(i, 7001, 125064) for i in range(11, 17)]
    return {3200: {7001: listings}}


class RecordingNotifier:
    name = "registratore"

    def __init__(self, delivers: bool = True):
        self.sent = []
        self._delivers = delivers

    @property
    def enabled(self) -> bool:
        return True

    def send(self, alert) -> bool:
        self.sent.append(alert)
        return self._delivers


def test_undelivered_alert_is_not_marked_as_notified(tmp_path):
    """Un canale che non consegna non deve far sparire la segnalazione."""
    settings = make_settings(tmp_path)
    silent = RecordingNotifier(delivers=False)

    collect(settings, FakeProvider(books_with_an_expensive_outlier()), [silent])

    with db.session(settings.database_path) as conn:
        row = conn.execute("SELECT notified_at FROM alerts").fetchone()

    assert row["notified_at"] is None


def test_collect_raises_alert_and_notifies(tmp_path):
    settings = make_settings(tmp_path)
    channel = RecordingNotifier()

    result = collect(settings, FakeProvider(books_with_an_expensive_outlier()), [channel])

    assert result.alert_count == 1
    assert result.notifications_sent == {"registratore": 1}
    assert len(channel.sent) == 1
    assert channel.sent[0].blueprint_id == 7001


def test_alert_is_marked_as_notified(tmp_path):
    settings = make_settings(tmp_path)
    collect(settings, FakeProvider(books_with_an_expensive_outlier()), [RecordingNotifier()])

    with db.session(settings.database_path) as conn:
        row = conn.execute("SELECT rule, notified_at FROM alerts").fetchone()

    assert row["rule"] == "outlier"
    assert row["notified_at"] is not None


def test_same_offer_is_not_notified_twice(tmp_path):
    """Il cuore dell'anti-spam: due cicli, una sola notifica."""
    settings = make_settings(tmp_path)
    provider = FakeProvider(books_with_an_expensive_outlier())
    channel = RecordingNotifier()

    first = collect(settings, provider, [channel])
    second = collect(settings, provider, [channel])

    assert first.alert_count == 1
    assert second.alert_count == 0
    assert len(channel.sent) == 1

    with db.session(settings.database_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
    assert total["n"] == 1


def test_a_further_price_drop_notifies_again(tmp_path):
    settings = make_settings(tmp_path)
    channel = RecordingNotifier()
    books = books_with_an_expensive_outlier()

    collect(settings, FakeProvider(books), [channel])

    cheaper = dict(books)
    cheaper[3200] = {
        7001: [listing(10, 7001, 60000)]
        + [listing(i, 7001, 125064) for i in range(11, 17)]
    }
    collect(settings, FakeProvider(cheaper), [channel])

    assert len(channel.sent) == 2


def test_small_savings_do_not_raise_alerts(tmp_path):
    """La carta economica del test precedente: -50%, ma due euro e mezzo."""
    settings = make_settings(tmp_path)
    result = collect(settings, FakeProvider(books_with_one_good_and_one_thin_card()))

    assert result.snapshots_saved == 1
    assert result.alert_count == 0


def test_collect_works_without_notifiers(tmp_path):
    settings = make_settings(tmp_path)
    result = collect(settings, FakeProvider(books_with_an_expensive_outlier()))

    assert result.alert_count == 1
    assert result.notifications_sent == {}


def test_trend_rule_is_inactive_without_history(tmp_path):
    settings = make_settings(tmp_path)
    result = collect(settings, FakeProvider(books_with_an_expensive_outlier()))
    assert result.trend_rule_active is False


def test_trend_baseline_ignores_snapshots_taken_today(tmp_path):
    """La regola di trend misura oggi contro il passato, non contro se stesso."""
    settings = make_settings(tmp_path)
    collect(settings, FakeProvider(books_with_an_expensive_outlier()))

    with db.session(settings.database_path) as conn:
        baselines = db.trend_baselines(conn, window_days=7)

    assert baselines == {}


def test_trend_baseline_uses_median_of_past_days(tmp_path):
    settings = make_settings(tmp_path)
    with db.session(settings.database_path) as conn:
        conn.execute(
            "INSERT INTO expansions (id, game_id, code, name, updated_at) "
            "VALUES (3200, 15, 'op09', 'test', '2026-11-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO cards (blueprint_id, name, updated_at) "
            "VALUES (7001, 'Carta', '2026-11-01T00:00:00+00:00')"
        )
        for day, price in (("18", 1000.0), ("19", 1200.0), ("20", 1400.0)):
            conn.execute(
                "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
                "min_price_cents, reference_price_cents, eligible_count) "
                f"VALUES (7001, '2026-11-{day}T10:00:00+00:00', 'EUR', ?, ?, 10)",
                (price, price),
            )

        baselines = db.trend_baselines(
            conn, window_days=7, today=date(2026, 11, 22)
        )

    assert baselines[7001] == (1200.0, 3)


def test_trend_baseline_window_excludes_older_snapshots(tmp_path):
    settings = make_settings(tmp_path)
    with db.session(settings.database_path) as conn:
        conn.execute(
            "INSERT INTO cards (blueprint_id, name, updated_at) "
            "VALUES (7001, 'Carta', '2026-11-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
            "min_price_cents, reference_price_cents, eligible_count) "
            "VALUES (7001, '2026-10-01T10:00:00+00:00', 'EUR', 999.0, 999.0, 10)"
        )
        baselines = db.trend_baselines(
            conn, window_days=7, today=date(2026, 11, 22)
        )

    assert baselines == {}


# -- riverifica contro la cache ----------------------------------------------


def test_candidate_is_rechecked_against_the_fresh_endpoint(tmp_path):
    """Il caso misurato dal vivo: stessa inserzione, due prezzi diversi.

    Il libro di espansione dice 64.46, la chiamata sulla singola carta dice
    63.16. Vince la seconda, che e quella non messa in cache.
    """
    stale = [listing(10, 7001, 6446)] + [
        listing(i, 7001, 9080) for i in range(11, 17)
    ]
    fresh = [listing(10, 7001, 6316)] + [
        listing(i, 7001, 9080) for i in range(11, 17)
    ]
    settings = make_settings(tmp_path)
    provider = FakeProvider({3200: {7001: stale}}, fresh={7001: fresh})

    result = collect(settings, provider)

    assert result.verified == 1
    assert provider.blueprint_calls == [7001]
    with db.session(settings.database_path) as conn:
        row = conn.execute("SELECT min_price_cents FROM snapshots").fetchone()
    assert row["min_price_cents"] == 6316.0


def test_verification_reveals_an_alert_the_cache_was_hiding(tmp_path):
    """Il prezzo in cache stava sotto soglia, quello vero la supera.

    Senza riverifica questa segnalazione non sarebbe mai partita.
    """
    stale = [listing(10, 7001, 6446)] + [
        listing(i, 7001, 9080) for i in range(11, 17)
    ]
    fresh = [listing(10, 7001, 5000)] + [
        listing(i, 7001, 9080) for i in range(11, 17)
    ]
    settings = make_settings(tmp_path)

    stale_only = collect(
        make_settings(tmp_path, database_path=tmp_path / "senza.db"),
        FakeProvider({3200: {7001: stale}}, fresh={7001: stale}),
    )
    assert stale_only.alert_count == 0

    result = collect(settings, FakeProvider({3200: {7001: stale}}, fresh={7001: fresh}))

    assert result.alert_count == 1
    assert result.alerts[0].price_cents == 5000.0


def test_quiet_cards_are_not_rechecked(tmp_path):
    """Una chiamata per carta su tutto il catalogo sarebbe uno spreco."""
    calm = [listing(i, 9001, 1000) for i in range(1, 8)]
    settings = make_settings(tmp_path)
    provider = FakeProvider({3200: {9001: calm}})

    result = collect(settings, provider)

    assert provider.blueprint_calls == []
    assert result.verified == 0


def test_a_near_miss_is_rechecked_too(tmp_path):
    """Il caso reale: -29% con soglia a -35%.

    Il prezzo in cache sottostima lo scostamento, quindi una carta appena
    sotto soglia va guardata comunque: potrebbe gia essere un affare.
    """
    stale = [listing(10, 7001, 6446)] + [
        listing(i, 7001, 9080) for i in range(11, 17)
    ]
    settings = make_settings(tmp_path)
    provider = FakeProvider({3200: {7001: stale}}, fresh={7001: stale})

    collect(settings, provider)

    assert provider.blueprint_calls == [7001]


def test_watched_cards_are_rechecked_even_when_quiet(tmp_path):
    settings = make_settings(tmp_path)
    provider = FakeProvider(books_with_one_good_and_one_thin_card())

    with db.session(settings.database_path) as conn:
        conn.execute(
            "INSERT INTO cards (blueprint_id, name, updated_at) "
            "VALUES (5001, 'Seguita', '2026-11-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO watchlist (blueprint_id, added_at) "
            "VALUES (5001, '2026-11-01T00:00:00+00:00')"
        )

    collect(settings, provider)

    assert provider.blueprint_calls == [5001]


def test_recheck_keeps_the_old_quote_when_the_card_goes_quiet(tmp_path):
    """Se la riverifica non produce una quotazione, non si perde la riga."""
    stale = [listing(10, 7001, 6446)] + [
        listing(i, 7001, 9080) for i in range(11, 17)
    ]
    settings = make_settings(tmp_path)
    provider = FakeProvider({3200: {7001: stale}}, fresh={7001: []})

    result = collect(settings, provider)

    assert result.verified == 0
    assert result.snapshots_saved == 1
    with db.session(settings.database_path) as conn:
        row = conn.execute("SELECT min_price_cents FROM snapshots").fetchone()
    assert row["min_price_cents"] == 6446.0


def test_recheck_is_capped_per_cycle(tmp_path):
    """Con soglie larghissime non si fa una chiamata per ogni carta del set."""
    books = {3200: {}}
    for index in range(10):
        blueprint_id = 8000 + index
        books[3200][blueprint_id] = [listing(1, blueprint_id, 1000)] + [
            listing(i, blueprint_id, 9000) for i in range(2, 8)
        ]
    settings = make_settings(tmp_path)
    provider = FakeProvider(books)

    collect_result = None
    from pricedrop import collector as collector_module

    original = collector_module.MAX_VERIFICATIONS
    collector_module.MAX_VERIFICATIONS = 3
    try:
        collect_result = collect(settings, provider)
    finally:
        collector_module.MAX_VERIFICATIONS = original

    assert len(provider.blueprint_calls) == 3
    assert collect_result.verified == 3


def test_cheap_cards_are_skipped(tmp_path):
    """Sulle carte da pochi centesimi ogni scostamento e percentualmente enorme."""
    cheap = [listing(i, 6001, 20) for i in range(1, 7)]
    settings = make_settings(
        tmp_path, outlier=OutlierSettings(min_reference_cents=100)
    )
    result = collect(settings, FakeProvider({3200: {6001: cheap}}))

    assert result.snapshots_saved == 0
    assert result.skipped_thin == 1


# -- rarita da storicizzare sotto soglia -------------------------------------


def books_with_one_cheap_card() -> dict:
    """Una carta liquida ma da pochi centesimi: sotto `min_reference_cents`."""
    cheap = [listing(i, 5003, 80) for i in range(1, 6)]
    cheap.insert(0, listing(98, 5003, 60))
    return {3200: {5003: cheap}}


def test_a_cheap_card_of_a_tracked_rarity_is_saved(tmp_path):
    """Una Super Rare da 80 centesimi va storicizzata lo stesso."""
    settings = make_settings(tmp_path, track_rarities=("Super Rare",))
    provider = FakeProvider(
        books=books_with_one_cheap_card(), rarities={5003: "Super Rare"}
    )
    result = collect(settings, provider)
    assert result.snapshots_saved == 1
    assert result.skipped_thin == 0


def test_a_cheap_common_is_still_skipped(tmp_path):
    settings = make_settings(tmp_path, track_rarities=("Super Rare",))
    provider = FakeProvider(
        books=books_with_one_cheap_card(), rarities={5003: "Common"}
    )
    result = collect(settings, provider)
    assert result.snapshots_saved == 0
    assert result.skipped_thin == 1


def test_without_the_list_even_a_super_rare_is_skipped(tmp_path):
    """Il comportamento storico resta quello di prima quando la lista e vuota."""
    settings = make_settings(tmp_path, track_rarities=())
    provider = FakeProvider(
        books=books_with_one_cheap_card(), rarities={5003: "Super Rare"}
    )
    result = collect(settings, provider)
    assert result.snapshots_saved == 0


def test_rarity_matching_ignores_case(tmp_path):
    settings = make_settings(tmp_path, track_rarities=("super rare",))
    provider = FakeProvider(
        books=books_with_one_cheap_card(), rarities={5003: "Super Rare"}
    )
    assert collect(settings, provider).snapshots_saved == 1


def test_card_registry_keeps_rarity_and_collector_number(tmp_path):
    """Rarita e numero stanno in fixed_properties, non in cima al blueprint."""
    settings = make_settings(tmp_path, track_rarities=("Super Rare",))
    provider = FakeProvider(
        books=books_with_one_cheap_card(), rarities={5003: "Super Rare"}
    )
    collect(settings, provider)
    with db.session(settings.database_path) as conn:
        row = conn.execute(
            "SELECT rarity, collector_number FROM cards WHERE blueprint_id = 5003"
        ).fetchone()
    assert row["rarity"] == "Super Rare"
    assert row["collector_number"] == "OP09-5003"
