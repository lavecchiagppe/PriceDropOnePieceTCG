"""Test dell'analisi di mercato."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pricedrop import analysis, db
from pricedrop.config import FilterSettings, OutlierSettings, Settings, TrendSettings
from pricedrop.web.app import create_app
from pricedrop.web.charts import lines_chart_svg


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        cardtrader_token="token-finto",
        database_path=tmp_path / "analisi.db",
        filters=FilterSettings(),
        outlier=OutlierSettings(),
        trend=TrendSettings(),
        schedule_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def add_card(conn, blueprint_id: int, name: str, expansion_id: int = 4769) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (blueprint_id, name, expansion_id, updated_at) "
        "VALUES (?, ?, ?, '2026-11-01T00:00:00+00:00')",
        (blueprint_id, name, expansion_id),
    )


def add_snapshot(
    conn,
    blueprint_id: int,
    day: str,
    reference: float,
    *,
    minimum: float | None = None,
    listings: int = 20,
    hour: str = "10",
) -> None:
    conn.execute(
        "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
        "min_price_cents, reference_price_cents, eligible_count) "
        "VALUES (?, ?, 'EUR', ?, ?, ?)",
        (
            blueprint_id,
            f"{day}T{hour}:00:00+00:00",
            minimum if minimum is not None else reference,
            reference,
            listings,
        ),
    )


def seed_expansions(conn) -> None:
    conn.executemany(
        "INSERT INTO expansions (id, game_id, code, name, monitored, updated_at) "
        "VALUES (?, 15, ?, ?, 1, '2026-11-01T00:00:00+00:00')",
        [(4769, "op17", "OP-17"), (4817, "op18", "OP-18")],
    )


DAYS = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]


@pytest.fixture
def settings(tmp_path) -> Settings:
    config = make_settings(tmp_path)
    with db.session(config.database_path) as conn:
        seed_expansions(conn)
        # Sei carte in OP-17: cinque stabili, una che raddoppia.
        for index in range(5):
            add_card(conn, 100 + index, f"Stabile {index}")
            for day in DAYS:
                add_snapshot(conn, 100 + index, day, 1000.0)

        add_card(conn, 200, "In salita")
        for step, day in enumerate(DAYS):
            add_snapshot(conn, 200, day, 1000.0 + step * 250.0)

        add_card(conn, 300, "In discesa")
        for step, day in enumerate(DAYS):
            add_snapshot(conn, 300, day, 5000.0 - step * 500.0)
    return config


@pytest.fixture
def client(settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# -- copertura ---------------------------------------------------------------


def test_coverage_counts_days_and_snapshots(settings):
    with db.session(settings.database_path) as conn:
        cover = analysis.coverage(conn)

    assert cover.distinct_days == 5
    assert cover.cards == 7
    assert cover.days == pytest.approx(4.0, abs=0.01)


def test_coverage_knows_when_it_is_not_enough(settings):
    with db.session(settings.database_path) as conn:
        cover = analysis.coverage(conn)

    assert cover.is_enough_for(7) is True
    assert cover.is_enough_for(90) is False


def test_coverage_of_an_empty_database(tmp_path):
    config = make_settings(tmp_path, database_path=tmp_path / "vuoto.db")
    with db.session(config.database_path) as conn:
        cover = analysis.coverage(conn)

    assert cover.snapshots == 0
    assert cover.is_enough_for(7) is False


# -- carte in movimento ------------------------------------------------------


def test_movers_find_the_rise_and_the_fall(settings):
    with db.session(settings.database_path) as conn:
        risers, fallers = analysis.movers(conn, days=30)

    assert [item.blueprint_id for item in risers] == [200]
    assert [item.blueprint_id for item in fallers] == [300]
    assert risers[0].change_ratio == pytest.approx(1.0)
    assert fallers[0].change_ratio == pytest.approx(-0.4)


def test_stable_cards_are_not_movers(settings):
    with db.session(settings.database_path) as conn:
        risers, fallers = analysis.movers(conn, days=30)

    moved = {item.blueprint_id for item in risers + fallers}
    assert moved.isdisjoint({100, 101, 102, 103, 104})


def test_movers_ignore_small_changes_in_euro(settings):
    """Un rialzo percentuale su una carta da pochi euro non e una notizia."""
    with db.session(settings.database_path) as conn:
        add_card(conn, 400, "Carta da poco")
        add_snapshot(conn, 400, DAYS[0], 100.0)
        add_snapshot(conn, 400, DAYS[2], 150.0)
        add_snapshot(conn, 400, DAYS[4], 190.0)
        risers, _ = analysis.movers(conn, days=30, min_change_cents=300)

    assert 400 not in {item.blueprint_id for item in risers}


def test_movers_need_enough_points(settings):
    with db.session(settings.database_path) as conn:
        add_card(conn, 500, "Vista una volta sola")
        add_snapshot(conn, 500, DAYS[4], 9000.0)
        risers, fallers = analysis.movers(conn, days=30, min_points=3)

    assert 500 not in {item.blueprint_id for item in risers + fallers}


def test_movers_window_excludes_older_snapshots(settings):
    with db.session(settings.database_path) as conn:
        risers, fallers = analysis.movers(conn, days=1)

    # Con una finestra di un giorno restano troppo pochi punti per concludere.
    assert risers == [] and fallers == []


# -- indice per espansione ---------------------------------------------------


def test_index_starts_at_one_hundred(settings):
    with db.session(settings.database_path) as conn:
        indexes = analysis.market_index(conn, days=30)

    assert indexes["op17"][0][1] == pytest.approx(100.0)


def test_index_follows_the_typical_card_not_the_expensive_one(settings):
    """Cinque carte stabili e una che raddoppia: la mediana resta a 100."""
    with db.session(settings.database_path) as conn:
        indexes = analysis.market_index(conn, days=30)

    assert indexes["op17"][-1][1] == pytest.approx(100.0)


def test_index_moves_when_most_cards_move(settings):
    with db.session(settings.database_path) as conn:
        for index in range(5):
            add_card(conn, 600 + index, f"OP18 {index}", expansion_id=4817)
            for step, day in enumerate(DAYS):
                add_snapshot(conn, 600 + index, day, 1000.0 - step * 100.0)
        indexes = analysis.market_index(conn, days=30)

    assert indexes["op18"][-1][1] == pytest.approx(60.0)


def test_index_skips_expansions_with_too_few_cards(settings):
    with db.session(settings.database_path) as conn:
        add_card(conn, 700, "Sola", expansion_id=4817)
        for day in DAYS:
            add_snapshot(conn, 700, day, 1000.0)
        indexes = analysis.market_index(conn, days=30)

    assert "op18" not in indexes


def test_index_ignores_cards_absent_on_the_first_day(settings):
    """Una carta che compare dopo non deve muovere l'indice da sola."""
    with db.session(settings.database_path) as conn:
        add_card(conn, 800, "Arrivata dopo")
        add_snapshot(conn, 800, DAYS[-1], 50000.0)
        indexes = analysis.market_index(conn, days=30)

    assert indexes["op17"][-1][1] == pytest.approx(100.0)


# -- volatilita e offerta ----------------------------------------------------


def test_volatility_ranks_the_unstable_card_first(settings):
    with db.session(settings.database_path) as conn:
        add_card(conn, 900, "Ballerina")
        for step, day in enumerate(DAYS):
            add_snapshot(
                conn, 900, day, 2000.0, minimum=2000.0 if step % 2 else 600.0
            )
        unstable = analysis.volatility(conn, days=30, min_points=5)

    assert unstable[0].blueprint_id == 900
    assert unstable[0].low_cents == 600.0
    assert unstable[0].high_cents == 2000.0


def test_volatility_ignores_cheap_cards(settings):
    with db.session(settings.database_path) as conn:
        add_card(conn, 910, "Centesimi")
        for step, day in enumerate(DAYS):
            add_snapshot(conn, 910, day, 100.0, minimum=10.0 if step % 2 else 90.0)
        unstable = analysis.volatility(conn, days=30, min_reference_cents=300)

    assert 910 not in {item.blueprint_id for item in unstable}


def test_supply_series_is_one_point_per_day(settings):
    with db.session(settings.database_path) as conn:
        series = analysis.supply_series(conn, days=30)

    assert [day for day, _ in series] == DAYS
    assert all(value == 20.0 for _, value in series)


# -- grafico multi-serie -----------------------------------------------------


def test_lines_chart_draws_one_line_per_series():
    svg = lines_chart_svg(
        {
            "op17": [("2026-11-20", 100.0), ("2026-11-21", 110.0)],
            "op18": [("2026-11-20", 100.0), ("2026-11-21", 90.0)],
        },
        baseline=100.0,
    )
    assert svg.count("<polyline") == 2
    assert "series-0" in svg and "series-1" in svg
    assert "chart-reference" in svg


def test_lines_chart_skips_series_with_one_point():
    svg = lines_chart_svg({"op17": [("2026-11-20", 100.0)]})
    assert "Storico insufficiente" in svg


def test_lines_chart_aligns_series_that_start_on_different_days():
    """Due set che non partono insieme restano allineati nel tempo."""
    svg = lines_chart_svg(
        {
            "lungo": [("2026-11-18", 100.0), ("2026-11-22", 120.0)],
            "corto": [("2026-11-21", 100.0), ("2026-11-22", 105.0)],
        }
    )
    assert svg.count("<polyline") == 2
    assert "18/11" in svg and "22/11" in svg


# -- pagina ------------------------------------------------------------------


def test_market_page_renders(client):
    response = client.get("/market")
    assert response.status_code == 200
    assert "Indice di prezzo per espansione" in response.text
    assert "In salita" in response.text


def test_market_page_warns_on_short_history(client):
    response = client.get("/market?days=365")
    assert "Storico insufficiente" in response.text


def test_market_page_on_an_empty_database(tmp_path):
    config = make_settings(tmp_path, database_path=tmp_path / "vuoto.db")
    with TestClient(create_app(config)) as empty_client:
        response = empty_client.get("/market")
    assert response.status_code == 200
    assert "Nessun rialzo rilevante" in response.text
