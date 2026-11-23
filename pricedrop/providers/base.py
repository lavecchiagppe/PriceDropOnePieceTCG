"""Contratto che ogni fonte di prezzi deve rispettare.

Tenere questa interfaccia stretta e priva di dettagli CardTrader è ciò che
permetterà di affiancare o sostituire la fonte (import CSV, altra API, una
eventuale API Cardmarket) senza toccare collector, regole e dashboard.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..listings import Listing


@runtime_checkable
class PriceProvider(Protocol):
    """Fonte di anagrafiche e inserzioni."""

    name: str

    def games(self) -> list[dict]:
        """Elenco dei giochi disponibili."""
        ...

    def expansions(self) -> list[dict]:
        """Elenco delle espansioni, con id, codice e nome."""
        ...

    def blueprints(self, expansion_id: int) -> list[dict]:
        """Anagrafica delle carte di una espansione."""
        ...

    def listings_by_expansion(
        self, expansion_id: int, *, language: str | None = None
    ) -> dict[int, list[Listing]]:
        """Inserzioni di tutte le carte di una espansione, per blueprint_id."""
        ...

    def listings_by_blueprint(
        self, blueprint_id: int, *, language: str | None = None
    ) -> list[Listing]:
        """Inserzioni di una singola carta."""
        ...
