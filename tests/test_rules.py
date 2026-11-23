"""Test delle regole di segnalazione e delle notifiche."""

from __future__ import annotations

import pytest

from pricedrop.config import OutlierSettings, TrendSettings
from pricedrop.listings import Listing, Quote
from pricedrop.notifiers.base import format_body, format_title, notify_all
from pricedrop.rules import (
    RULE_OUTLIER,
    RULE_TREND,
    Alert,
    evaluate,
    outlier_alert,
    trend_alert,
)

TODAY = "2026-11-22"


def make_quote(
    min_cents: float,
    reference_cents: float,
    *,
    blueprint_id: int = 1000,
    name: str = "Monkey D. Luffy",
    listing_id: int = 7,
    eligible: int = 20,
) -> Quote:
    best = Listing(
        listing_id=listing_id,
        blueprint_id=blueprint_id,
        name=name,
        price_cents=int(min_cents),
        currency="EUR",
        condition="Near Mint",
        language="en",
        can_sell_via_hub=True,
        seller="negozio",
    )
    return Quote(
        blueprint_id=blueprint_id,
        name=name,
        currency="EUR",
        min_price_cents=min_cents,
        reference_price_cents=reference_cents,
        eligible_count=eligible,
        best=best,
    )


OUTLIER = OutlierSettings(
    threshold=0.35, min_reference_cents=100, min_saving_cents=300
)
TREND = TrendSettings(
    window_days=7, threshold=0.15, min_points=5, min_saving_cents=300
)


# -- regola outlier ----------------------------------------------------------


def test_fires_on_real_outlier():
    """Carta a 800,54 contro un riferimento di 1250,64: il caso di valore."""
    alert = outlier_alert(make_quote(80054, 125064), OUTLIER)
    assert alert is not None
    assert alert.rule == RULE_OUTLIER
    assert alert.discount_ratio == pytest.approx(0.36, abs=0.01)
    assert alert.saving_cents == pytest.approx(45010)


def test_does_not_fire_below_percentage_threshold():
    assert outlier_alert(make_quote(900, 1000), OUTLIER) is None


def test_does_not_fire_when_saving_is_trivial():
    """Il caso che ha fatto aggiungere la soglia assoluta.

    Carta a 0,40 contro un riferimento di 1,72: meno settantasette per cento,
    ma un risparmio di 1,32 euro. Non e un affare, e rumore.
    """
    alert = outlier_alert(make_quote(40, 172), OUTLIER)
    assert alert is None


def test_requires_both_conditions_together():
    """Percentuale alta e risparmio alto insieme: solo allora si segnala."""
    assert outlier_alert(make_quote(100, 1000), OUTLIER) is not None  # -90%, 9 EUR
    assert outlier_alert(make_quote(700, 1000), OUTLIER) is None  # -30%, 3 EUR
    assert outlier_alert(make_quote(50, 200), OUTLIER) is None  # -75%, 1,50 EUR


def test_ignores_cards_below_minimum_reference():
    assert outlier_alert(make_quote(10, 99), OUTLIER) is None


def test_fingerprint_is_stable_for_the_same_offer():
    first = outlier_alert(make_quote(80054, 125064), OUTLIER)
    second = outlier_alert(make_quote(80054, 125064), OUTLIER)
    assert first is not None and second is not None
    assert first.fingerprint == second.fingerprint


def test_fingerprint_changes_when_price_drops_further():
    """Un ribasso ulteriore e una notizia nuova e merita una seconda notifica."""
    first = outlier_alert(make_quote(80054, 125064), OUTLIER)
    second = outlier_alert(make_quote(70000, 125064), OUTLIER)
    assert first is not None and second is not None
    assert first.fingerprint != second.fingerprint


def test_fingerprint_differs_between_cards():
    first = outlier_alert(make_quote(80054, 125064, blueprint_id=1), OUTLIER)
    second = outlier_alert(make_quote(80054, 125064, blueprint_id=2), OUTLIER)
    assert first is not None and second is not None
    assert first.fingerprint != second.fingerprint


def test_alert_carries_card_url():
    alert = outlier_alert(make_quote(80054, 125064, blueprint_id=406121), OUTLIER)
    assert alert is not None
    assert alert.url == "https://www.cardtrader.com/cards/406121"


# -- regola di calo nel tempo ------------------------------------------------


def test_trend_fires_on_sustained_drop():
    alert = trend_alert(make_quote(1700, 1700), 2500.0, 10, TREND, day=TODAY)
    assert alert is not None
    assert alert.rule == RULE_TREND
    assert alert.discount_ratio == pytest.approx(0.32)


def test_trend_stays_silent_without_enough_history():
    """Nelle prime settimane la regola non deve inventare segnali."""
    assert trend_alert(make_quote(1000, 1000), 2500.0, 2, TREND, day=TODAY) is None


def test_trend_respects_percentage_threshold():
    assert trend_alert(make_quote(2400, 2400), 2500.0, 10, TREND, day=TODAY) is None


def test_trend_respects_absolute_saving():
    assert trend_alert(make_quote(100, 100), 200.0, 10, TREND, day=TODAY) is None


def test_trend_fingerprint_is_once_per_card_per_day():
    first = trend_alert(make_quote(1700, 1700), 2500.0, 10, TREND, day=TODAY)
    second = trend_alert(make_quote(1650, 1650), 2500.0, 10, TREND, day=TODAY)
    assert first is not None and second is not None
    assert first.fingerprint == second.fingerprint


def test_trend_fingerprint_differs_the_next_day():
    first = trend_alert(make_quote(1700, 1700), 2500.0, 10, TREND, day=TODAY)
    second = trend_alert(make_quote(1700, 1700), 2500.0, 10, TREND, day="2026-11-23")
    assert first is not None and second is not None
    assert first.fingerprint != second.fingerprint


def test_trend_handles_zero_baseline():
    assert trend_alert(make_quote(1000, 1000), 0.0, 10, TREND, day=TODAY) is None


# -- valutazione complessiva -------------------------------------------------


def test_evaluate_applies_both_rules():
    quote = make_quote(80054, 125064, blueprint_id=55)
    alerts = evaluate(
        [quote],
        outlier=OUTLIER,
        trend=TREND,
        baselines={55: (200000.0, 10)},
        day=TODAY,
    )
    assert {alert.rule for alert in alerts} == {RULE_OUTLIER, RULE_TREND}


def test_evaluate_skips_trend_without_baseline():
    alerts = evaluate(
        [make_quote(80054, 125064)], outlier=OUTLIER, trend=TREND, day=TODAY
    )
    assert [alert.rule for alert in alerts] == [RULE_OUTLIER]


def test_evaluate_sorts_by_saving():
    small = make_quote(500, 1000, blueprint_id=1)
    large = make_quote(50000, 100000, blueprint_id=2)
    alerts = evaluate([small, large], outlier=OUTLIER, trend=TREND, day=TODAY)
    assert [alert.blueprint_id for alert in alerts] == [2, 1]


def test_evaluate_returns_nothing_on_a_calm_market():
    quotes = [make_quote(1000, 1020, blueprint_id=i) for i in range(5)]
    assert evaluate(quotes, outlier=OUTLIER, trend=TREND, day=TODAY) == []


# -- notifiche ---------------------------------------------------------------


class FakeNotifier:
    def __init__(self, name="finto", enabled=True, fail=False):
        self.name = name
        self._enabled = enabled
        self._fail = fail
        self.sent: list[Alert] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, alert: Alert) -> bool:
        if self._fail:
            raise RuntimeError("canale rotto")
        self.sent.append(alert)
        return True


def sample_alert() -> Alert:
    found = outlier_alert(make_quote(80054, 125064), OUTLIER)
    assert found is not None
    return found


def test_notify_all_sends_on_active_channels():
    channel = FakeNotifier()
    alert = sample_alert()
    outcome = notify_all([channel], [alert])
    assert outcome.sent == {"finto": 1}
    assert outcome.delivered == {alert.fingerprint}
    assert len(channel.sent) == 1


def test_notify_all_skips_disabled_channels():
    channel = FakeNotifier(enabled=False)
    outcome = notify_all([channel], [sample_alert()])
    assert outcome.sent == {}
    assert outcome.delivered == set()
    assert channel.sent == []


def test_a_broken_channel_does_not_stop_the_others():
    """Una notifica persa e meno grave di una raccolta interrotta."""
    broken = FakeNotifier(name="rotto", fail=True)
    working = FakeNotifier(name="buono")
    alert = sample_alert()
    outcome = notify_all([broken, working], [alert])
    assert outcome.sent == {"rotto": 0, "buono": 1}
    assert outcome.delivered == {alert.fingerprint}
    assert len(working.sent) == 1


def test_nothing_is_delivered_when_every_channel_fails():
    """Nessun canale ha consegnato: niente va segnato come notificato.

    Senza questa distinzione il dedup nasconderebbe per sempre una
    segnalazione che nessuno ha mai ricevuto.
    """
    broken = FakeNotifier(name="rotto", fail=True)
    outcome = notify_all([broken], [sample_alert()])
    assert outcome.sent == {"rotto": 0}
    assert outcome.delivered == set()


# -- formattazione del messaggio --------------------------------------------


def test_message_includes_price_discount_and_link():
    alert = sample_alert()
    body = format_body(alert)
    assert "800.54 EUR" in body
    assert "1250.64 EUR" in body
    assert "36%" in body
    assert alert.url in body


def test_message_warns_that_zero_fee_is_excluded():
    assert "CardTrader Zero" in format_body(sample_alert())


def test_title_distinguishes_the_two_rules():
    outlier = sample_alert()
    trend = trend_alert(make_quote(1700, 1700), 2500.0, 10, TREND, day=TODAY)
    assert trend is not None
    assert format_title(outlier).startswith("Occasione")
    assert format_title(trend).startswith("Calo")


def test_trend_message_names_its_own_reference():
    trend = trend_alert(make_quote(1700, 1700), 2500.0, 10, TREND, day=TODAY)
    assert trend is not None
    assert "media ultimi giorni" in format_body(trend)
