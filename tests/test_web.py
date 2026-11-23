"""Test delle letture aggregate, dei grafici e delle pagine della dashboard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pricedrop import analysis, db, queries
from pricedrop.config import FilterSettings, OutlierSettings, Settings, TrendSettings
from pricedrop.web.app import _safe_redirect, create_app
from pricedrop.web.charts import parse_points, price_chart_svg, sparkline_svg


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        cardtrader_token="token-finto",
        expansion_codes=("op17",),
        database_path=tmp_path / "web.db",
        filters=FilterSettings(),
        outlier=OutlierSettings(),
        trend=TrendSettings(),
        schedule_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def seed(settings: Settings) -> None:
    """Due carte con storico: una occasione evidente e una tranquilla."""
    with db.session(settings.database_path) as conn:
        conn.execute(
            "INSERT INTO expansions (id, game_id, code, name, monitored, updated_at) "
            "VALUES (4769, 15, 'op17', 'OP-17', 1, '2026-11-01T00:00:00+00:00')"
        )
        conn.executemany(
            "INSERT INTO cards (blueprint_id, name, expansion_id, rarity, "
            "updated_at) VALUES (?, ?, 4769, ?, '2026-11-01T00:00:00+00:00')",
            [
                (406121, "Edward.Newgate", "Alternate Art"),
                (393047, "Mr.3(Galdino)", "Super Rare"),
            ],
        )
        rows = [
            (406121, "2026-11-20T10:00:00+00:00", 125000.0, 125500.0, 8),
            (406121, "2026-11-21T10:00:00+00:00", 124000.0, 125200.0, 8),
            (406121, "2026-11-22T10:00:00+00:00", 80054.0, 125064.0, 7),
            (393047, "2026-11-22T10:00:00+00:00", 1300.0, 1332.0, 40),
        ]
        conn.executemany(
            "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
            "min_price_cents, reference_price_cents, eligible_count) "
            "VALUES (?, ?, 'EUR', ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO alerts (fingerprint, blueprint_id, listing_id, rule, "
            "price_cents, reference_price_cents, discount_ratio, currency, "
            "seller, created_at) VALUES ('outlier:406121:1:80054', 406121, 1, "
            "'outlier', 80054.0, 125064.0, 0.36, 'EUR', 'negozio', "
            "'2026-11-22T10:00:05+00:00')"
        )


@pytest.fixture
def settings(tmp_path) -> Settings:
    config = make_settings(tmp_path)
    seed(config)
    return config


@pytest.fixture
def client(settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# -- letture aggregate -------------------------------------------------------


def test_summary_counts_snapshots_and_cards(settings):
    with db.session(settings.database_path) as conn:
        result = queries.summary(conn)

    assert result.snapshots == 4
    assert result.cards == 2
    assert result.alerts_total == 1
    assert result.monitored_expansions == 1
    assert result.has_history is True


def test_summary_measures_history_span(settings):
    with db.session(settings.database_path) as conn:
        result = queries.summary(conn)
    assert result.history_span_days == pytest.approx(2.0, abs=0.01)


def test_summary_on_an_empty_database(tmp_path):
    config = make_settings(tmp_path, database_path=tmp_path / "vuoto.db")
    with db.session(config.database_path) as conn:
        result = queries.summary(conn)

    assert result.snapshots == 0
    assert result.has_history is False
    assert result.history_span_days == 0.0


def test_latest_snapshot_wins_over_older_ones(settings):
    """La dashboard deve mostrare il presente, non la prima rilevazione."""
    with db.session(settings.database_path) as conn:
        row = queries.card(conn, 406121)

    assert row is not None
    assert row["min_price_cents"] == 80054.0
    assert row["taken_at"] == "2026-11-22T10:00:00+00:00"


def test_best_opportunities_ranks_by_discount(settings):
    with db.session(settings.database_path) as conn:
        rows = queries.best_opportunities(conn, limit=10)

    assert [row["blueprint_id"] for row in rows][0] == 406121


def test_best_opportunities_filters_by_absolute_saving(settings):
    """La carta tranquilla risparmia 32 centesimi: non e una occasione."""
    with db.session(settings.database_path) as conn:
        rows = queries.best_opportunities(conn, min_saving_cents=300)

    assert [row["blueprint_id"] for row in rows] == [406121]


def test_most_valuable_sorts_by_price(settings):
    with db.session(settings.database_path) as conn:
        rows = queries.most_valuable(conn, limit=5)
    assert [row["blueprint_id"] for row in rows] == [406121, 393047]


def test_history_is_ordered_in_time(settings):
    with db.session(settings.database_path) as conn:
        rows = queries.history(conn, 406121, days=3650)
    assert [row["min_price_cents"] for row in rows] == [125000.0, 124000.0, 80054.0]


def test_search_finds_cards_by_partial_name(settings):
    with db.session(settings.database_path) as conn:
        rows = queries.search_cards(conn, "newgate")
    assert [row["blueprint_id"] for row in rows] == [406121]


def test_search_term_is_not_interpolated_into_sql(settings):
    """Il termine arriva come parametro: un apice non deve rompere la query."""
    with db.session(settings.database_path) as conn:
        assert queries.search_cards(conn, "'; DROP TABLE cards; --") == []
        still_there = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()
    assert still_there["n"] == 2


def test_watchlist_toggles_both_ways(settings):
    with db.session(settings.database_path) as conn:
        assert queries.toggle_watchlist(conn, 406121) is True
        assert len(queries.watchlist(conn)) == 1
        assert queries.toggle_watchlist(conn, 406121) is False
        assert queries.watchlist(conn) == []


# -- grafici -----------------------------------------------------------------


def test_chart_draws_both_series(settings):
    with db.session(settings.database_path) as conn:
        points = parse_points(queries.history(conn, 406121, days=3650))

    svg = price_chart_svg(points)
    assert svg.startswith("<svg")
    assert "chart-minimum" in svg
    assert "chart-reference" in svg
    assert 'role="img"' in svg


def test_chart_with_a_single_point_draws_a_marker(settings):
    with db.session(settings.database_path) as conn:
        points = parse_points(queries.history(conn, 393047, days=3650))

    svg = price_chart_svg(points)
    assert "chart-minimum-dot" in svg
    assert "polyline" not in svg


def test_chart_handles_no_data():
    svg = price_chart_svg([])
    assert "Nessuna rilevazione" in svg


def test_chart_survives_a_flat_series(settings):
    """Prezzi identici: la scala non deve collassare e dividere per zero."""
    with db.session(settings.database_path) as conn:
        conn.executemany(
            "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
            "min_price_cents, reference_price_cents, eligible_count) "
            "VALUES (393047, ?, 'EUR', 1000.0, 1000.0, 10)",
            [("2026-11-21T10:00:00+00:00",), ("2026-11-21T12:00:00+00:00",)],
        )
        points = parse_points(queries.history(conn, 393047, days=3650))

    assert price_chart_svg(points).startswith("<svg")


def test_sparkline_needs_at_least_two_values():
    assert sparkline_svg([100.0]) == ""
    assert sparkline_svg([100.0, 120.0]).startswith("<svg")


# -- pagine ------------------------------------------------------------------


def test_dashboard_renders_with_data(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Edward.Newgate" in response.text
    assert "Occasioni sul mercato" in response.text


def test_dashboard_renders_on_an_empty_database(tmp_path):
    config = make_settings(tmp_path, database_path=tmp_path / "vuoto.db")
    with TestClient(create_app(config)) as empty_client:
        response = empty_client.get("/")
    assert response.status_code == 200
    assert "Nessun ciclo registrato" in response.text


def test_dashboard_warns_while_history_is_short(client):
    """Il limite dello storico va detto in pagina, non solo nella spec."""
    response = client.get("/")
    assert "Lo storico sta ancora crescendo" in response.text


def test_card_page_shows_price_and_chart(client):
    response = client.get("/card/406121")
    assert response.status_code == 200
    assert "800.54" in response.text
    assert "chart-minimum" in response.text
    assert "Riferimento mercato" in response.text


def test_card_page_for_an_unknown_card_still_renders(client):
    response = client.get("/card/999999")
    assert response.status_code == 200
    assert "non ha rilevazioni" in response.text


def test_card_page_accepts_a_period(client):
    response = client.get("/card/406121?days=7")
    assert response.status_code == 200
    assert "7 giorni" in response.text


def test_search_page_without_a_term(client):
    response = client.get("/search")
    assert response.status_code == 200
    assert "Cerca una carta" in response.text


def test_search_page_finds_a_card(client):
    response = client.get("/search?q=Galdino")
    assert response.status_code == 200
    assert "Mr.3(Galdino)" in response.text


def test_watchlist_page_starts_empty(client):
    response = client.get("/watchlist")
    assert response.status_code == 200
    assert "Nessuna carta seguita" in response.text


def test_watchlist_toggle_from_the_page(client):
    response = client.post("/watchlist/406121", data={"back": "/watchlist"})
    assert response.status_code == 200
    assert "Edward.Newgate" in response.text


# -- immagini delle carte ----------------------------------------------------


IMAGE = "https://cardtrader.com/uploads/blueprints/image/406121/preview.webp"


def with_image(settings: Settings) -> None:
    with db.session(settings.database_path) as conn:
        conn.execute(
            "UPDATE cards SET image_url = ? WHERE blueprint_id = 406121", (IMAGE,)
        )


def test_dashboard_shows_the_card_thumbnail(client, settings):
    with_image(settings)
    response = client.get("/")
    assert IMAGE in response.text
    assert 'class="thumb"' in response.text


def test_thumbnails_are_lazy_and_do_not_leak_the_local_url(client, settings):
    """La miniatura non deve dire a CardTrader quale pagina la sta mostrando."""
    with_image(settings)
    response = client.get("/")
    assert 'loading="lazy"' in response.text
    assert 'referrerpolicy="no-referrer"' in response.text


def test_thumbnail_is_decorative_next_to_the_name(client, settings):
    """Il nome e accanto: ripeterlo nell'alt sarebbe rumore per chi legge con
    uno screen reader."""
    with_image(settings)
    response = client.get("/")
    assert f'src="{IMAGE}" alt=""' in response.text


def test_missing_image_renders_a_placeholder_not_a_broken_image(client):
    """Senza segnaposto le righe senza immagine salterebbero in altezza."""
    response = client.get("/")
    assert "thumb-empty" in response.text
    assert "<img" not in response.text


def test_card_page_shows_the_art_with_a_real_alt(client, settings):
    with_image(settings)
    response = client.get("/card/406121")
    assert 'class="card-art"' in response.text
    assert "Illustrazione della carta Edward.Newgate" in response.text


def test_card_page_without_an_image_still_renders(client):
    response = client.get("/card/406121")
    assert response.status_code == 200
    assert 'class="card-art"' not in response.text
    assert "Edward.Newgate" in response.text


def test_search_results_show_thumbnails(client, settings):
    with_image(settings)
    response = client.get("/search?q=Newgate")
    assert IMAGE in response.text


def test_watchlist_shows_thumbnails(client, settings):
    with_image(settings)
    client.post("/watchlist/406121", data={"back": "/watchlist"})
    response = client.get("/watchlist")
    assert IMAGE in response.text


def test_redirect_target_must_be_internal():
    """Il campo nascosto non deve poter rimandare fuori dal sito."""
    assert _safe_redirect("/watchlist") == "/watchlist"
    assert _safe_redirect("https://example.com") == "/"
    assert _safe_redirect("//example.com") == "/"


# -- filtro per espansione ---------------------------------------------------


def seed_second_expansion(settings: Settings) -> None:
    """Una seconda espansione con una sua occasione e una sua segnalazione."""
    with db.session(settings.database_path) as conn:
        conn.execute(
            "INSERT INTO expansions (id, game_id, code, name, monitored, updated_at) "
            "VALUES (4637, 15, 'eb05', 'EB-05: Heroines Edition Vol.2', 1, "
            "'2026-11-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO cards (blueprint_id, name, expansion_id, rarity, "
            "updated_at) VALUES (500001, 'Nico Robin', 4637, 'Super Rare', "
            "'2026-11-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
            "min_price_cents, reference_price_cents, eligible_count) "
            "VALUES (500001, '2026-11-22T10:00:00+00:00', 'EUR', 4000.0, 9000.0, 6)"
        )
        conn.execute(
            "INSERT INTO alerts (fingerprint, blueprint_id, listing_id, rule, "
            "price_cents, reference_price_cents, discount_ratio, currency, "
            "seller, created_at) VALUES ('outlier:500001:2:4000', 500001, 2, "
            "'outlier', 4000.0, 9000.0, 0.55, 'EUR', 'negozio', "
            "'2026-11-22T10:00:06+00:00')"
        )


@pytest.fixture
def two_sets(tmp_path) -> Settings:
    config = make_settings(tmp_path, expansion_codes=("op17", "eb05"))
    seed(config)
    seed_second_expansion(config)
    return config


@pytest.fixture
def two_sets_client(two_sets) -> TestClient:
    with TestClient(create_app(two_sets)) as test_client:
        yield test_client


def test_expansions_lists_only_sets_with_data(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = queries.expansions(conn)
    assert [row["code"] for row in rows] == ["eb05", "op17"]
    assert dict((row["code"], row["cards"]) for row in rows)["eb05"] == 1


def test_expansions_skips_a_set_without_snapshots(two_sets):
    """Un set appena aggiunto non compare nel filtro prima della raccolta."""
    with db.session(two_sets.database_path) as conn:
        conn.execute(
            "INSERT INTO expansions (id, game_id, code, name, monitored, "
            "updated_at) VALUES (4867, 15, 'op19', 'OP19', 1, "
            "'2026-11-01T00:00:00+00:00')"
        )
        rows = queries.expansions(conn)
    assert "op19" not in [row["code"] for row in rows]


def test_opportunities_can_be_filtered_by_expansion(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = queries.best_opportunities(conn, expansion_code="eb05")
    assert [row["blueprint_id"] for row in rows] == [500001]


def test_most_valuable_can_be_filtered_by_expansion(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = queries.most_valuable(conn, expansion_code="op17")
    assert {row["expansion_code"] for row in rows} == {"op17"}


def test_alerts_can_be_filtered_by_expansion(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = db.recent_alerts(conn, expansion_code="eb05")
    assert [row["blueprint_id"] for row in rows] == [500001]


def test_alerts_without_filter_still_return_everything(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = db.recent_alerts(conn)
    assert len(rows) == 2


def test_dashboard_shows_a_chip_per_expansion(two_sets_client):
    response = two_sets_client.get("/")
    assert '/?expansion=eb05' in response.text
    assert '/?expansion=op17' in response.text


def test_dashboard_filtered_hides_the_other_expansion(two_sets_client):
    response = two_sets_client.get("/?expansion=eb05")
    assert response.status_code == 200
    assert "Nico Robin" in response.text
    assert "Edward.Newgate" not in response.text


def test_dashboard_ignores_an_unknown_expansion(two_sets_client):
    """Un codice inventato mostra tutto, non una pagina vuota."""
    response = two_sets_client.get("/?expansion=non-esiste")
    assert response.status_code == 200
    assert "Nico Robin" in response.text
    assert "Edward.Newgate" in response.text


def test_collect_button_keeps_the_filter(two_sets_client):
    response = two_sets_client.get("/?expansion=eb05")
    assert 'value="/?expansion=eb05"' in response.text


# -- filtro per rarita -------------------------------------------------------


def test_rarities_lists_what_is_quoted(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = queries.rarities(conn)
    assert dict((row["rarity"], row["cards"]) for row in rows) == {
        "Super Rare": 2,
        "Alternate Art": 1,
    }


def test_super_rare_filter_excludes_the_alternate_art(two_sets):
    """La SR alternativa ha rarita "Alternate Art": non deve comparire."""
    with db.session(two_sets.database_path) as conn:
        rows = queries.most_valuable(conn, rarity="Super Rare")
    assert {row["blueprint_id"] for row in rows} == {500001, 393047}


def test_rarity_and_expansion_filters_combine(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = queries.most_valuable(
            conn, expansion_code="op17", rarity="Super Rare"
        )
    assert [row["blueprint_id"] for row in rows] == [393047]


def test_alerts_filtered_by_rarity(two_sets):
    with db.session(two_sets.database_path) as conn:
        rows = db.recent_alerts(conn, rarity="Super Rare")
    assert [row["blueprint_id"] for row in rows] == [500001]


def test_dashboard_shows_a_chip_per_rarity(two_sets_client):
    response = two_sets_client.get("/")
    assert "rarity=Super+Rare" in response.text or "rarity=Super%20Rare" in response.text


def test_dashboard_filtered_by_rarity_hides_the_alternate_art(two_sets_client):
    response = two_sets_client.get("/?rarity=Super+Rare")
    assert response.status_code == 200
    assert "Nico Robin" in response.text
    assert "Edward.Newgate" not in response.text


def test_dashboard_ignores_an_unknown_rarity(two_sets_client):
    response = two_sets_client.get("/?rarity=Mitica")
    assert response.status_code == 200
    assert "Edward.Newgate" in response.text


def test_chips_keep_the_other_filter(two_sets_client):
    """Cambiare espansione non azzera la rarita scelta, e viceversa."""
    response = two_sets_client.get("/?rarity=Super+Rare")
    assert "expansion=eb05&amp;rarity=Super" in response.text


# -- filtri su Watchlist e Mercato -------------------------------------------


def test_watchlist_is_filtered_by_rarity(two_sets):
    with db.session(two_sets.database_path) as conn:
        queries.toggle_watchlist(conn, 500001)   # Nico Robin, Super Rare
        queries.toggle_watchlist(conn, 406121)   # Edward.Newgate, Alternate Art
        rows = queries.watchlist(conn, rarity="Super Rare")
    assert [row["blueprint_id"] for row in rows] == [500001]


def test_watchlist_page_keeps_the_filter_in_the_remove_button(two_sets_client, two_sets):
    with db.session(two_sets.database_path) as conn:
        queries.toggle_watchlist(conn, 500001)
    response = two_sets_client.get("/watchlist?rarity=Super+Rare")
    assert response.status_code == 200
    assert "Nico Robin" in response.text
    assert 'value="/watchlist?rarity=Super+Rare"' in response.text


def test_watchlist_page_shows_the_chips(two_sets_client):
    response = two_sets_client.get("/watchlist")
    assert '/watchlist?expansion=eb05' in response.text
    assert "rarity=Super+Rare" in response.text


def test_market_page_is_filtered(two_sets_client):
    response = two_sets_client.get("/market?days=30&rarity=Super+Rare")
    assert response.status_code == 200
    assert "Limitato a" in response.text


def test_market_chips_keep_the_period(two_sets_client):
    """Cambiare espansione non riporta il periodo a 30 giorni."""
    response = two_sets_client.get("/market?days=90")
    assert "expansion=eb05&amp;days=90" in response.text


def test_market_period_chips_keep_the_filters(two_sets_client):
    response = two_sets_client.get("/market?days=30&expansion=eb05")
    assert "expansion=eb05&amp;days=90" in response.text


def test_movers_can_be_filtered_by_expansion(two_sets):
    """Il filtro arriva fino alle analisi, non solo alle tabelle."""
    with db.session(two_sets.database_path) as conn:
        risers, fallers = analysis.movers(
            conn, days=30, min_points=2, min_change_cents=1, expansion_code="op17"
        )
    assert all(
        mover.expansion_code == "op17" for mover in risers + fallers
    )
    assert any(mover.blueprint_id == 406121 for mover in fallers)


def test_movers_filtered_by_rarity_exclude_the_alternate_art(two_sets):
    with db.session(two_sets.database_path) as conn:
        risers, fallers = analysis.movers(
            conn, days=30, min_points=2, min_change_cents=1, rarity="Super Rare"
        )
    assert all(mover.blueprint_id != 406121 for mover in risers + fallers)


def test_supply_series_can_be_filtered(two_sets):
    with db.session(two_sets.database_path) as conn:
        everything = analysis.supply_series(conn, days=30)
        only_eb05 = analysis.supply_series(conn, days=30, expansion_code="eb05")
    assert everything and only_eb05
    assert only_eb05 != everything
