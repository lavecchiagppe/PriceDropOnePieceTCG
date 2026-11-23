"""Test della normalizzazione, dei filtri e del calcolo del prezzo.

Dati sintetici, nessuna chiamata di rete.
"""

from __future__ import annotations

import pytest

from pricedrop.listings import (
    Listing,
    build_quote,
    is_eligible,
    listing_from_api,
)


def make_listing(
    listing_id: int = 1,
    price_cents: int = 500,
    **overrides,
) -> Listing:
    """Inserzione eleggibile di base, da alterare campo per campo nei test."""
    defaults = dict(
        listing_id=listing_id,
        blueprint_id=1000,
        name="Monkey D. Luffy",
        price_cents=price_cents,
        currency="EUR",
        bundle_size=1,
        quantity=1,
        condition="Near Mint",
        language="en",
        can_sell_via_hub=True,
        on_vacation=False,
        graded=False,
        seller="venditore",
    )
    defaults.update(overrides)
    return Listing(**defaults)


# -- normalizzazione del prezzo ---------------------------------------------


def test_unit_price_divides_by_bundle_size():
    """Un lotto da 4 copie a 10 EUR vale 2,50 EUR per carta."""
    listing = make_listing(price_cents=1000, bundle_size=4)
    assert listing.unit_price_cents == 250.0


def test_unit_price_with_single_card():
    assert make_listing(price_cents=500).unit_price_cents == 500.0


def test_unit_price_survives_zero_bundle_size():
    """Un bundle_size a zero o assente non deve far esplodere il calcolo."""
    assert make_listing(price_cents=500, bundle_size=0).unit_price_cents == 500.0


def test_bundle_does_not_masquerade_as_cheap_card():
    """Il caso che la normalizzazione esiste per evitare.

    Un lotto da 4 a 6 EUR (1,50 per carta) e davvero il piu economico; senza
    divisione sembrerebbe piu caro delle singole a 5 EUR.
    """
    bundle = make_listing(listing_id=1, price_cents=600, bundle_size=4)
    singles = [make_listing(listing_id=i, price_cents=500) for i in range(2, 7)]
    quote = build_quote([*singles, bundle])
    assert quote is not None
    assert quote.best.listing_id == 1
    assert quote.min_price_cents == 150.0


# -- filtri di eleggibilita --------------------------------------------------


def test_rejects_listing_without_cardtrader_zero():
    assert not is_eligible(make_listing(can_sell_via_hub=False))


def test_accepts_non_zero_listing_when_filter_disabled():
    listing = make_listing(can_sell_via_hub=False)
    assert is_eligible(listing, require_cardtrader_zero=False)


def test_rejects_seller_on_vacation():
    assert not is_eligible(make_listing(on_vacation=True))


def test_rejects_graded_card():
    assert not is_eligible(make_listing(graded=True))


def test_rejects_out_of_stock():
    assert not is_eligible(make_listing(quantity=0))


def test_rejects_zero_price():
    assert not is_eligible(make_listing(price_cents=0))


@pytest.mark.parametrize("condition", ["Near Mint", "Slightly Played"])
def test_accepts_wanted_conditions(condition):
    assert is_eligible(make_listing(condition=condition))


@pytest.mark.parametrize(
    "condition", ["Mint", "Moderately Played", "Played", "Heavily Played", "Poor"]
)
def test_rejects_other_conditions(condition):
    assert not is_eligible(make_listing(condition=condition))


def test_rejects_unknown_condition():
    """Senza condizione non si puo confrontare: si scarta."""
    assert not is_eligible(make_listing(condition=None))


def test_rejects_other_language():
    assert not is_eligible(make_listing(language="jp"))


def test_accepts_missing_language():
    """La lingua e gia filtrata lato server: se il campo manca non si scarta."""
    assert is_eligible(make_listing(language=None))


def test_language_comparison_is_case_insensitive():
    assert is_eligible(make_listing(language="EN"))


# -- calcolo della quotazione ------------------------------------------------


def test_quote_uses_median_of_window_excluding_cheapest():
    """Il riferimento ignora la piu economica e media le successive."""
    listings = [
        make_listing(listing_id=1, price_cents=250),
        make_listing(listing_id=2, price_cents=500),
        make_listing(listing_id=3, price_cents=510),
        make_listing(listing_id=4, price_cents=520),
        make_listing(listing_id=5, price_cents=530),
    ]
    quote = build_quote(listings)
    assert quote is not None
    assert quote.min_price_cents == 250.0
    assert quote.reference_price_cents == 515.0
    assert quote.eligible_count == 5


def test_scenario_from_requirements():
    """Il caso descritto nei requisiti: pavimento a 5 EUR, uno sbuca a 2,50."""
    listings = [
        make_listing(listing_id=99, price_cents=250),
        *[make_listing(listing_id=i, price_cents=500) for i in range(1, 6)],
    ]
    quote = build_quote(listings)
    assert quote is not None
    assert quote.best.listing_id == 99
    assert quote.discount_ratio == pytest.approx(0.5)
    assert quote.discount_ratio > 0.35


def test_uniform_market_produces_no_discount():
    listings = [make_listing(listing_id=i, price_cents=500) for i in range(1, 7)]
    quote = build_quote(listings)
    assert quote is not None
    assert quote.discount_ratio == pytest.approx(0.0)


def test_returns_none_when_too_few_comparables():
    listings = [make_listing(listing_id=i, price_cents=500) for i in range(1, 4)]
    assert build_quote(listings, min_comparables=4) is None


def test_ineligible_listings_do_not_count_toward_comparables():
    """Un mercato fatto di inserzioni non acquistabili resta un mercato vuoto."""
    listings = [
        make_listing(listing_id=1, price_cents=250),
        *[
            make_listing(listing_id=i, price_cents=500, can_sell_via_hub=False)
            for i in range(2, 8)
        ],
    ]
    assert build_quote(listings) is None


def test_reference_window_limits_how_many_listings_count():
    """Le inserzioni oltre la finestra non spostano il riferimento."""
    listings = [
        make_listing(listing_id=1, price_cents=100),
        make_listing(listing_id=2, price_cents=200),
        make_listing(listing_id=3, price_cents=200),
        make_listing(listing_id=4, price_cents=9000),
        make_listing(listing_id=5, price_cents=9000),
    ]
    quote = build_quote(listings, reference_window=2, min_comparables=4)
    assert quote is not None
    assert quote.reference_price_cents == 200.0


def test_quote_is_none_without_listings():
    assert build_quote([]) is None


# -- lettura della risposta API ---------------------------------------------


API_SAMPLE = {
    "id": 101862104,
    "blueprint_id": 10050,
    "name_en": "Roronoa Zoro",
    "quantity": 2,
    "price": {"cents": 1250, "currency": "EUR"},
    "description": "mint, sleeved",
    "properties_hash": {
        "condition": "Near Mint",
        "one_piece_language": "en",
        "one_piece_foil": False,
    },
    "expansion": {"id": 3200, "code": "op09", "name_en": "Emperors in the New World"},
    "user": {
        "id": 51242,
        "username": "cardshop",
        "can_sell_via_hub": True,
        "country_code": "IT",
        "user_type": "professional",
        "max_sellable_in24h_quantity": 20,
    },
    "graded": False,
    "on_vacation": False,
    "bundle_size": 1,
}


def test_parses_api_listing():
    listing = listing_from_api(API_SAMPLE)
    assert listing.listing_id == 101862104
    assert listing.blueprint_id == 10050
    assert listing.name == "Roronoa Zoro"
    assert listing.price_cents == 1250
    assert listing.currency == "EUR"
    assert listing.quantity == 2
    assert listing.condition == "Near Mint"
    assert listing.can_sell_via_hub is True
    assert listing.seller == "cardshop"
    assert listing.max_sellable_in24h == 20


def test_finds_language_under_game_specific_property_name():
    """La chiave della lingua dipende dal gioco: one_piece_language, non language."""
    assert listing_from_api(API_SAMPLE).language == "en"


def test_parses_listing_with_missing_optional_fields():
    minimal = {"id": 7, "blueprint_id": 8}
    listing = listing_from_api(minimal)
    assert listing.price_cents == 0
    assert listing.bundle_size == 1
    assert listing.condition is None
    assert listing.can_sell_via_hub is False


def test_bundle_size_is_read_from_api():
    raw = {**API_SAMPLE, "bundle_size": 4, "price": {"cents": 1000, "currency": "EUR"}}
    listing = listing_from_api(raw)
    assert listing.bundle_size == 4
    assert listing.unit_price_cents == 250.0
