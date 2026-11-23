"""Client della CardTrader Full API (v2).

Documentazione: https://www.cardtrader.com/en/docs/api/full/reference

Limiti dichiarati dall'API e rispettati qui:
  - 200 richieste ogni 10 secondi su tutti gli endpoint
  - 10 richieste al secondo su GET /marketplace/products

Il token non viene mai scritto nei log, nelle eccezioni o nel repr.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import httpx

from ..listings import Listing, listing_from_api

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cardtrader.com/api/v2"

# Intervallo minimo fra due richieste: circa 8 al secondo, sotto il tetto di 10.
MIN_REQUEST_INTERVAL = 0.125


class CardTraderError(RuntimeError):
    """Errore generico del provider."""


class AuthenticationError(CardTraderError):
    """Token assente, scaduto o non valido."""


class RateLimitError(CardTraderError):
    """Limite di richieste superato nonostante i tentativi."""


class CardTraderProvider:
    """Accesso in lettura al marketplace CardTrader."""

    name = "cardtrader"

    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        min_interval: float = MIN_REQUEST_INTERVAL,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise AuthenticationError("Token CardTrader mancante.")
        self._base_url = base_url.rstrip("/")
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_request_at = 0.0
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "pricedrop-onepiece/0.1 (personal price monitor)",
            },
        )

    def __repr__(self) -> str:
        return f"<CardTraderProvider base_url={self._base_url!r}>"

    def __enter__(self) -> CardTraderProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- richieste -----------------------------------------------------------

    def _throttle(self) -> None:
        """Distanzia le richieste per non superare il limite dell'API."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Esegue una GET, con attesa progressiva sui 429 e sugli errori server."""
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                response = self._client.get(path, params=clean_params)
            except httpx.RequestError as exc:
                last_error = CardTraderError(f"Errore di rete su {path}: {exc}")
                logger.warning("Richiesta fallita (%s), nuovo tentativo", exc)
            else:
                if response.status_code in (401, 403):
                    raise AuthenticationError(
                        "CardTrader ha rifiutato il token (HTTP "
                        f"{response.status_code}). Verifica CARDTRADER_API_TOKEN "
                        "nel file .env."
                    )
                if response.status_code == 429:
                    last_error = RateLimitError(
                        "Limite di richieste CardTrader superato."
                    )
                    logger.warning("Rate limit su %s, attendo", path)
                elif response.status_code >= 500:
                    last_error = CardTraderError(
                        f"Errore server CardTrader su {path}: HTTP "
                        f"{response.status_code}"
                    )
                elif response.status_code >= 400:
                    raise CardTraderError(
                        f"Richiesta rifiutata su {path}: HTTP "
                        f"{response.status_code} {response.text[:200]}"
                    )
                else:
                    return response.json()

            if attempt < self._max_retries:
                time.sleep(2.0 * (attempt + 1))

        raise last_error or CardTraderError(f"Richiesta non riuscita su {path}")

    def _get_list(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """GET di un endpoint che restituisce un elenco.

        La documentazione mostra un array nudo, ma l'API in produzione lo
        incapsula in un oggetto con la sola chiave "array". Gestiamo le due
        forme, cosi il client continua a funzionare se il formato cambia.
        """
        payload = self._get(path, params)
        if isinstance(payload, dict):
            payload = payload.get("array", [])
        if not isinstance(payload, list):
            raise CardTraderError(
                f"Risposta inattesa da {path}: atteso un elenco, "
                f"ricevuto {type(payload).__name__}"
            )
        return [item for item in payload if isinstance(item, dict)]

    # -- anagrafiche ---------------------------------------------------------

    def info(self) -> dict:
        """Verifica l'autenticazione. Non contiene il token nella risposta."""
        payload = self._get("/info")
        return payload if isinstance(payload, dict) else {}

    def games(self) -> list[dict]:
        return self._get_list("/games")

    def expansions(self) -> list[dict]:
        return self._get_list("/expansions")

    def blueprints(self, expansion_id: int) -> list[dict]:
        return self._get_list("/blueprints/export", {"expansion_id": expansion_id})

    def find_game_id(self, needle: str = "one piece") -> int | None:
        """Cerca un gioco per nome, senza assumere un id fisso.

        Gli id dei giochi non sono documentati e possono differire: meglio
        risolverli a runtime che inchiodarli nel codice.
        """
        target = needle.lower()
        for game in self.games():
            haystack = f"{game.get('name', '')} {game.get('display_name', '')}".lower()
            if target in haystack:
                return int(game["id"])
        return None

    # -- marketplace ---------------------------------------------------------

    def listings_by_expansion(
        self, expansion_id: int, *, language: str | None = None
    ) -> dict[int, list[Listing]]:
        """Scarica il book di tutte le carte di una espansione.

        Una sola chiamata restituisce ogni blueprint del set con le sue 25
        inserzioni piu economiche.
        """
        payload = self._get(
            "/marketplace/products",
            {"expansion_id": expansion_id, "language": language},
        )
        return _parse_marketplace(payload)

    def listings_by_blueprint(
        self, blueprint_id: int, *, language: str | None = None
    ) -> list[Listing]:
        """Scarica il book di una singola carta.

        Serve come ripiego quando, dopo i filtri su condizione ed eleggibilita,
        il book di espansione lascia troppe poche inserzioni comparabili.
        """
        payload = self._get(
            "/marketplace/products",
            {"blueprint_id": blueprint_id, "language": language},
        )
        parsed = _parse_marketplace(payload)
        return parsed.get(int(blueprint_id), [])


def _parse_marketplace(payload: Any) -> dict[int, list[Listing]]:
    """Converte la risposta grezza in inserzioni, per blueprint_id.

    La risposta e un oggetto che ha come chiavi gli id dei blueprint, in forma
    di stringa, e come valori gli array di inserzioni.
    """
    if not isinstance(payload, dict):
        raise CardTraderError(
            f"Risposta inattesa dal marketplace: {type(payload).__name__}"
        )

    result: dict[int, list[Listing]] = {}
    for key, raw_listings in payload.items():
        try:
            blueprint_id = int(key)
        except (TypeError, ValueError):
            logger.debug("Chiave non numerica ignorata: %r", key)
            continue
        result[blueprint_id] = _parse_listings(raw_listings)
    return result


def _parse_listings(raw_listings: Iterable[Any]) -> list[Listing]:
    listings: list[Listing] = []
    if not isinstance(raw_listings, list):
        return listings
    for raw in raw_listings:
        if not isinstance(raw, dict):
            continue
        try:
            listings.append(listing_from_api(raw))
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Inserzione scartata: %s", exc)
    return listings
