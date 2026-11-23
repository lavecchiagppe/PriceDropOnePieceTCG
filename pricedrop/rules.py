"""Regole di segnalazione.

Due regole, con nature diverse:

  - **outlier**: confronta le inserzioni di una carta fra loro, nello stesso
    istante. Funziona dal primo ciclo di raccolta, perche non ha bisogno di
    storico.
  - **trend**: confronta il prezzo di oggi con il passato recente della stessa
    carta. Entra in funzione solo quando lo storico ha abbastanza punti.

Entrambe emettono `Alert` con una impronta (`fingerprint`) che identifica la
segnalazione: e cio che impedisce di notificare due volte la stessa offerta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .config import OutlierSettings, TrendSettings
from .listings import Quote

CARD_URL = "https://www.cardtrader.com/cards/{blueprint_id}"

RULE_OUTLIER = "outlier"
RULE_TREND = "trend"


@dataclass(frozen=True, slots=True)
class Alert:
    """Una segnalazione pronta da salvare e notificare."""

    fingerprint: str
    blueprint_id: int
    rule: str
    card_name: str
    currency: str
    price_cents: float
    reference_price_cents: float
    eligible_count: int = 0
    listing_id: int | None = None
    seller: str | None = None

    @property
    def saving_cents(self) -> float:
        """Quanto si risparmia in valore assoluto."""
        return self.reference_price_cents - self.price_cents

    @property
    def discount_ratio(self) -> float:
        if self.reference_price_cents <= 0:
            return 0.0
        return 1.0 - (self.price_cents / self.reference_price_cents)

    @property
    def url(self) -> str:
        return CARD_URL.format(blueprint_id=self.blueprint_id)


def outlier_alert(quote: Quote, settings: OutlierSettings) -> Alert | None:
    """Segnala l'inserzione che si stacca dal resto del mercato.

    Richiede due condizioni insieme, non una sola:

      - uno scostamento percentuale dal prezzo di riferimento;
      - un risparmio reale in valore assoluto.

    La seconda esiste perche sui dati veri la sola percentuale premia le carte
    da pochi centesimi: un meno settanta per cento su una carta da 1,70 euro
    vale poco piu di un euro, e seppellirebbe le segnalazioni che contano.
    """
    if quote.reference_price_cents < settings.min_reference_cents:
        return None
    if quote.discount_ratio < settings.threshold:
        return None

    saving = quote.reference_price_cents - quote.min_price_cents
    if saving < settings.min_saving_cents:
        return None

    return Alert(
        fingerprint=outlier_fingerprint(
            quote.blueprint_id, quote.best.listing_id, quote.min_price_cents
        ),
        blueprint_id=quote.blueprint_id,
        rule=RULE_OUTLIER,
        card_name=quote.name,
        currency=quote.currency,
        price_cents=quote.min_price_cents,
        reference_price_cents=quote.reference_price_cents,
        eligible_count=quote.eligible_count,
        listing_id=quote.best.listing_id,
        seller=quote.best.seller,
    )


def outlier_fingerprint(
    blueprint_id: int, listing_id: int | None, price_cents: float
) -> str:
    """Identifica una offerta specifica a un prezzo specifico.

    Includere il prezzo e voluto: se il venditore abbassa ancora, quella e una
    notizia nuova e merita una seconda segnalazione.
    """
    return f"{RULE_OUTLIER}:{blueprint_id}:{listing_id}:{round(price_cents)}"


def trend_alert(
    quote: Quote,
    baseline_cents: float,
    points: int,
    settings: TrendSettings,
    *,
    day: str,
) -> Alert | None:
    """Segnala un calo rispetto al passato recente della stessa carta.

    `baseline_cents` e la mediana dei prezzi minimi registrati nella finestra,
    esclusa la rilevazione di oggi: la mediana regge meglio di una media al
    singolo valore anomalo, che e esattamente cio che la regola outlier cerca.

    L'impronta e per carta e per giorno: un calo che persiste viene notificato
    una volta al giorno, non a ogni ciclo di raccolta.
    """
    if points < settings.min_points:
        return None
    if baseline_cents <= 0:
        return None

    discount = 1.0 - (quote.min_price_cents / baseline_cents)
    if discount < settings.threshold:
        return None
    if (baseline_cents - quote.min_price_cents) < settings.min_saving_cents:
        return None

    return Alert(
        fingerprint=f"{RULE_TREND}:{quote.blueprint_id}:{day}",
        blueprint_id=quote.blueprint_id,
        rule=RULE_TREND,
        card_name=quote.name,
        currency=quote.currency,
        price_cents=quote.min_price_cents,
        reference_price_cents=baseline_cents,
        eligible_count=quote.eligible_count,
        listing_id=quote.best.listing_id,
        seller=quote.best.seller,
    )


def evaluate(
    quotes: Iterable[Quote],
    *,
    outlier: OutlierSettings,
    trend: TrendSettings,
    baselines: Mapping[int, tuple[float, int]] | None = None,
    day: str,
) -> list[Alert]:
    """Applica entrambe le regole a un insieme di quotazioni.

    `baselines` associa a ogni blueprint la coppia (mediana storica, numero di
    punti). Se manca, la regola di trend viene semplicemente saltata: e il caso
    normale nelle prime settimane, quando lo storico non esiste ancora.
    """
    baselines = baselines or {}
    alerts: list[Alert] = []

    for quote in quotes:
        found = outlier_alert(quote, outlier)
        if found is not None:
            alerts.append(found)

        baseline = baselines.get(quote.blueprint_id)
        if baseline is not None:
            baseline_cents, points = baseline
            found = trend_alert(quote, baseline_cents, points, trend, day=day)
            if found is not None:
                alerts.append(found)

    alerts.sort(key=lambda alert: alert.saving_cents, reverse=True)
    return alerts
