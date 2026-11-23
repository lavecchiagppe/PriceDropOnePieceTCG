"""Test della collezione: valutazione, P&L e pagina."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pricedrop import db, queries
from pricedrop.config import FilterSettings, OutlierSettings, Settings, TrendSettings
from pricedrop.web.app import create_app, parse_money


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        cardtrader_token="token-finto",
        database_path=tmp_path / "collezione.db",
        filters=FilterSettings(),
        outlier=OutlierSettings(),
        trend=TrendSettings(),
        schedule_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def seed(settings: Settings) -> None:
    """Due carte quotate e una senza rilevazioni."""
    with db.session(settings.database_path) as conn:
        conn.execute(
            "INSERT INTO expansions (id, game_id, code, name, monitored, updated_at) "
            "VALUES (4769, 15, 'op17', 'OP-17', 1, '2026-11-01T00:00:00+00:00')"
        )
        conn.executemany(
            "INSERT INTO cards (blueprint_id, name, expansion_id, updated_at) "
            "VALUES (?, ?, 4769, '2026-11-01T00:00:00+00:00')",
            [
                (406121, "Edward.Newgate"),
                (393047, "Mr.3(Galdino)"),
                (999001, "Carta senza mercato"),
            ],
        )
        conn.executemany(
            "INSERT INTO snapshots (blueprint_id, taken_at, currency, "
            "min_price_cents, reference_price_cents, eligible_count) "
            "VALUES (?, ?, 'EUR', ?, ?, 10)",
            [
                (406121, "2026-11-20T10:00:00+00:00", 120000.0, 124000.0),
                (406121, "2026-11-21T10:00:00+00:00", 118000.0, 126000.0),
                (406121, "2026-11-22T10:00:00+00:00", 80054.0, 125000.0),
                (393047, "2026-11-22T10:00:00+00:00", 1300.0, 1400.0),
            ],
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


# -- lettura dei prezzi scritti a mano ---------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12,50", 1250),
        ("12.50", 1250),
        ("12", 1200),
        (" 12,50 EUR ", 1250),
        ("", None),
        ("   ", None),
        (None, None),
        ("abc", None),
    ],
)
def test_parse_money_accepts_what_people_actually_type(raw, expected):
    assert parse_money(raw) == expected


# -- valutazione e P&L -------------------------------------------------------


def test_holding_is_valued_at_the_reference_price(settings):
    """Non al minimo: quello e spesso una singola offerta anomala."""
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=2, purchase_price_cents=100000)
        items = queries.holdings(conn)

    assert len(items) == 1
    item = items[0]
    assert item.unit_value_cents == 125000.0
    assert item.unit_min_cents == 80054.0
    assert item.value_total == 250000.0


def test_profit_is_value_minus_cost(settings):
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=2, purchase_price_cents=100000)
        item = queries.holdings(conn)[0]

    assert item.cost_total == 200000.0
    assert item.pnl == 50000.0
    assert item.pnl_ratio == pytest.approx(0.25)


def test_loss_is_reported_as_negative(settings):
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 393047, quantity=1, purchase_price_cents=2000)
        item = queries.holdings(conn)[0]

    assert item.pnl == -600.0
    assert item.pnl_ratio == pytest.approx(-0.3)


def test_holding_without_purchase_price_has_no_profit(settings):
    """Sapere cosa possiedi senza aver registrato quanto hai pagato."""
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=1)
        item = queries.holdings(conn)[0]

    assert item.has_cost is False
    assert item.cost_total is None
    assert item.pnl is None
    assert item.value_total == 125000.0


def test_holding_of_an_unquoted_card_has_no_value(settings):
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 999001, quantity=3, purchase_price_cents=500)
        item = queries.holdings(conn)[0]

    assert item.has_value is False
    assert item.value_total is None
    assert item.pnl is None
    assert item.cost_total == 1500.0


def test_same_card_bought_twice_stays_in_two_rows(settings):
    """Prezzi di acquisto diversi non vanno fusi, o il P&L si perde."""
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=1, purchase_price_cents=90000)
        db.add_holding(conn, 406121, quantity=1, purchase_price_cents=130000)
        items = queries.holdings(conn)

    assert len(items) == 2
    assert sorted(item.pnl for item in items) == [-5000.0, 35000.0]


def test_quantity_must_be_positive(settings):
    with db.session(settings.database_path) as conn:
        with pytest.raises(ValueError):
            db.add_holding(conn, 406121, quantity=0)


# -- totali ------------------------------------------------------------------


def test_portfolio_totals(settings):
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=2, purchase_price_cents=100000)
        db.add_holding(conn, 393047, quantity=4, purchase_price_cents=1000)
        totals = queries.portfolio(conn)

    assert totals.rows == 2
    assert totals.copies == 6
    assert totals.cost_total == 204000.0
    assert totals.value_total == 255600.0
    assert totals.pnl == 51600.0
    assert totals.is_partial is False


def test_portfolio_flags_incomplete_rows(settings):
    """I totali devono dire quando non coprono tutto."""
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=1)
        db.add_holding(conn, 999001, quantity=1, purchase_price_cents=500)
        totals = queries.portfolio(conn)

    assert totals.rows_without_cost == 1
    assert totals.unvalued_rows == 1
    assert totals.is_partial is True


def test_portfolio_of_an_empty_collection(settings):
    with db.session(settings.database_path) as conn:
        totals = queries.portfolio(conn)

    assert totals.rows == 0
    assert totals.pnl == 0.0
    assert totals.pnl_ratio is None


# -- andamento del valore ----------------------------------------------------


def test_portfolio_history_follows_the_market(settings):
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=2, purchase_price_cents=100000)
        series = queries.portfolio_history(conn, days=3650)

    assert [day for day, _ in series] == [
        "2026-11-20",
        "2026-11-21",
        "2026-11-22",
    ]
    assert [value for _, value in series] == [248000.0, 252000.0, 250000.0]


def test_portfolio_history_carries_prices_forward(settings):
    """Una carta non quotata oggi non deve far crollare il totale."""
    with db.session(settings.database_path) as conn:
        db.add_holding(conn, 406121, quantity=1, purchase_price_cents=1)
        db.add_holding(conn, 393047, quantity=1, purchase_price_cents=1)
        series = dict(queries.portfolio_history(conn, days=3650))

    # Il 20 e il 21 solo Newgate ha un prezzo; il 22 si aggiunge Galdino
    # e Newgate resta al suo valore di quel giorno.
    assert series["2026-11-21"] == 126000.0
    assert series["2026-11-22"] == 125000.0 + 1400.0


def test_portfolio_history_is_empty_without_holdings(settings):
    with db.session(settings.database_path) as conn:
        assert queries.portfolio_history(conn) == []


# -- pagina ------------------------------------------------------------------


def test_collection_page_starts_empty(client):
    response = client.get("/collection")
    assert response.status_code == 200
    assert "Nessun acquisto registrato" in response.text


def test_add_from_the_card_page(client):
    response = client.post(
        "/collection/add",
        data={
            "blueprint_id": "406121",
            "quantity": "2",
            "purchase_price": "900,00",
            "condition": "Near Mint",
            "back": "/collection",
        },
    )
    assert response.status_code == 200
    assert "Edward.Newgate" in response.text
    assert "Near Mint" in response.text


def test_page_shows_profit_after_adding(client):
    client.post(
        "/collection/add",
        data={"blueprint_id": "406121", "quantity": "1", "purchase_price": "1000,00"},
    )
    response = client.get("/collection")
    assert "+250.00" in response.text


def test_update_changes_quantity_and_price(client, settings):
    client.post(
        "/collection/add",
        data={"blueprint_id": "406121", "quantity": "1", "purchase_price": "1000,00"},
    )
    with db.session(settings.database_path) as conn:
        holding_id = queries.holdings(conn)[0].id

    client.post(
        f"/collection/{holding_id}/update",
        data={"quantity": "3", "purchase_price": "1100,00"},
    )

    with db.session(settings.database_path) as conn:
        item = queries.holdings(conn)[0]
    assert item.quantity == 3
    assert item.purchase_price_cents == 110000


def test_delete_removes_the_row(client, settings):
    client.post(
        "/collection/add",
        data={"blueprint_id": "406121", "quantity": "1", "purchase_price": "10,00"},
    )
    with db.session(settings.database_path) as conn:
        holding_id = queries.holdings(conn)[0].id

    client.post(f"/collection/{holding_id}/delete")

    with db.session(settings.database_path) as conn:
        assert queries.holdings(conn) == []


def test_page_warns_when_totals_are_partial(client):
    client.post("/collection/add", data={"blueprint_id": "406121", "quantity": "1"})
    response = client.get("/collection")
    assert "I totali non coprono tutto" in response.text


def test_page_explains_the_valuation(client):
    response = client.get("/collection")
    assert "Come viene calcolato il valore" in response.text
