"""Modello delle inserzioni, normalizzazione, filtri ed estrazione del prezzo.

Questo modulo non parla con la rete: trasforma le inserzioni grezze del
provider in dati confrontabili. È il cuore della logica di rilevamento, e per
questo è interamente coperto da test.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable, Sequence

# Scala delle condizioni CardTrader, dalla migliore alla peggiore.
CONDITION_SCALE: tuple[str, ...] = (
    "Mint",
    "Near Mint",
    "Slightly Played",
    "Moderately Played",
    "Played",
    "Heavily Played",
    "Poor",
)

DEFAULT_CONDITIONS: tuple[str, ...] = ("Near Mint", "Slightly Played")


@dataclass(frozen=True, slots=True)
class Listing:
    """Una singola inserzione in vendita.

    `price_cents` è il prezzo **del lotto** così come lo restituisce l'API: per
    confrontare inserzioni fra loro va sempre usato `unit_price_cents`.
    """

    listing_id: int
    blueprint_id: int
    name: str
    price_cents: int
    currency: str
    bundle_size: int = 1
    quantity: int = 1
    condition: str | None = None
    language: str | None = None
    can_sell_via_hub: bool = False
    on_vacation: bool = False
    graded: bool = False
    seller: str | None = None
    seller_country: str | None = None
    max_sellable_in24h: int | None = None

    @property
    def unit_price_cents(self) -> float:
        """Prezzo della singola copia.

        Un lotto da 4 copie a 10,00 EUR vale 2,50 EUR per carta. Senza questa
        divisione i lotti falserebbero sia il pavimento di prezzo sia gli alert.
        """
        return self.price_cents / max(self.bundle_size, 1)


def _extract_language(properties: dict[str, Any]) -> str | None:
    """Trova la lingua fra le proprietà.

    Il nome della proprietà dipende dal gioco (`mtg_language`,
    `one_piece_language`, ...), quindi la si cerca per suffisso invece di
    assumere una chiave fissa.
    """
    for key, value in properties.items():
        if key == "language" or key.endswith("_language"):
            return value if isinstance(value, str) else None
    return None


def listing_from_api(raw: dict[str, Any]) -> Listing:
    """Costruisce una Listing dalla risposta di GET /marketplace/products."""
    properties = raw.get("properties_hash") or {}
    user = raw.get("user") or {}
    price = raw.get("price") or {}

    return Listing(
        listing_id=int(raw["id"]),
        blueprint_id=int(raw["blueprint_id"]),
        name=raw.get("name_en") or "",
        price_cents=int(price.get("cents") or 0),
        currency=str(price.get("currency") or "EUR"),
        bundle_size=int(raw.get("bundle_size") or 1),
        quantity=int(raw.get("quantity") or 0),
        condition=properties.get("condition"),
        language=_extract_language(properties),
        can_sell_via_hub=bool(user.get("can_sell_via_hub")),
        on_vacation=bool(raw.get("on_vacation")),
        graded=bool(raw.get("graded")),
        seller=user.get("username"),
        seller_country=user.get("country_code"),
        max_sellable_in24h=user.get("max_sellable_in24h_quantity"),
    )


def is_eligible(
    listing: Listing,
    *,
    conditions: Sequence[str] = DEFAULT_CONDITIONS,
    language: str | None = "en",
    require_cardtrader_zero: bool = True,
    exclude_graded: bool = True,
) -> bool:
    """Dice se l'inserzione è confrontabile e realmente acquistabile.

    Sulla lingua: se la proprietà è assente non si scarta l'inserzione, perché
    il filtro per lingua è già stato applicato lato server dall'API. Si scarta
    solo quando la lingua è presente e diversa da quella richiesta.
    """
    if require_cardtrader_zero and not listing.can_sell_via_hub:
        return False
    if listing.on_vacation:
        return False
    if exclude_graded and listing.graded:
        return False
    if listing.quantity <= 0:
        return False
    if listing.price_cents <= 0:
        return False
    if conditions and listing.condition is not None and listing.condition not in conditions:
        return False
    if conditions and listing.condition is None:
        return False
    if language and listing.language and listing.language.lower() != language.lower():
        return False
    return True


@dataclass(frozen=True, slots=True)
class Quote:
    """Fotografia del mercato di una carta in un dato momento."""

    blueprint_id: int
    name: str
    currency: str
    min_price_cents: float
    reference_price_cents: float
    eligible_count: int
    best: Listing

    @property
    def discount_ratio(self) -> float:
        """Quanto la migliore offerta sta sotto il riferimento, da 0 a 1."""
        if self.reference_price_cents <= 0:
            return 0.0
        return 1.0 - (self.min_price_cents / self.reference_price_cents)


def build_quote(
    listings: Iterable[Listing],
    *,
    reference_window: int = 5,
    min_comparables: int = 4,
    conditions: Sequence[str] = DEFAULT_CONDITIONS,
    language: str | None = "en",
    require_cardtrader_zero: bool = True,
    exclude_graded: bool = True,
) -> Quote | None:
    """Calcola prezzo minimo e prezzo di riferimento di una carta.

    Il riferimento è la mediana delle `reference_window` inserzioni che seguono
    la più economica: escludere la prima è ciò che permette di misurare quanto
    l'offerta migliore si stacca dal resto del mercato, invece di confonderla
    con esso.

    Restituisce None se le inserzioni eleggibili sono troppo poche perché il
    confronto abbia senso.
    """
    eligible = sorted(
        (
            listing
            for listing in listings
            if is_eligible(
                listing,
                conditions=conditions,
                language=language,
                require_cardtrader_zero=require_cardtrader_zero,
                exclude_graded=exclude_graded,
            )
        ),
        key=lambda listing: listing.unit_price_cents,
    )

    if len(eligible) < min_comparables:
        return None

    best = eligible[0]
    window = eligible[1 : 1 + reference_window]
    if not window:
        return None

    reference = median(listing.unit_price_cents for listing in window)
    if reference <= 0:
        return None

    return Quote(
        blueprint_id=best.blueprint_id,
        name=best.name,
        currency=best.currency,
        min_price_cents=best.unit_price_cents,
        reference_price_cents=reference,
        eligible_count=len(eligible),
        best=best,
    )
