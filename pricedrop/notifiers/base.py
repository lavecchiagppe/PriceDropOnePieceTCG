"""Protocollo dei notificatori e formattazione del messaggio."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from ..rules import RULE_TREND, Alert

logger = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    """Canale di notifica."""

    name: str

    @property
    def enabled(self) -> bool:
        """Falso se il canale non e configurato: viene saltato in silenzio."""
        ...

    def send(self, alert: Alert) -> bool:
        """Invia una segnalazione. Restituisce True se riuscita."""
        ...


def _money(cents: float, currency: str) -> str:
    return f"{cents / 100:.2f} {currency}"


def format_title(alert: Alert) -> str:
    kind = "Calo" if alert.rule == RULE_TREND else "Occasione"
    return f"{kind}: {alert.card_name}"


def format_body(alert: Alert) -> str:
    """Testo della notifica, in righe brevi.

    Il riferimento ha un nome diverso nelle due regole, perche significa due
    cose diverse: le altre inserzioni di adesso, oppure il passato recente
    della stessa carta.
    """
    reference_label = (
        "media ultimi giorni" if alert.rule == RULE_TREND else "riferimento mercato"
    )

    lines = [
        f"{_money(alert.price_cents, alert.currency)} "
        f"({reference_label} {_money(alert.reference_price_cents, alert.currency)})",
        f"Sotto del {alert.discount_ratio * 100:.0f}%, "
        f"risparmio {_money(alert.saving_cents, alert.currency)}",
    ]

    details = []
    if alert.eligible_count:
        details.append(f"{alert.eligible_count} inserzioni confrontate")
    if alert.seller:
        details.append(f"venditore {alert.seller}")
    if details:
        lines.append(" - ".join(details))

    # Il prezzo e quello nudo: la commissione CardTrader Zero si applica al
    # carrello e non e inclusa qui.
    lines.append("Prezzo senza commissione CardTrader Zero")
    lines.append(alert.url)

    return "\n".join(lines)


def format_alert(alert: Alert) -> str:
    return f"{format_title(alert)}\n{format_body(alert)}"


@dataclass(slots=True)
class NotifyOutcome:
    """Esito dell'invio: quante per canale, e quali sono arrivate davvero."""

    sent: dict[str, int] = field(default_factory=dict)
    delivered: set[str] = field(default_factory=set)


def notify_all(
    notifiers: Sequence[Notifier], alerts: Sequence[Alert]
) -> NotifyOutcome:
    """Invia le segnalazioni su tutti i canali attivi.

    Il fallimento di un canale non impedisce agli altri di ricevere la
    notifica, e non fa fallire il ciclo di raccolta: una segnalazione persa
    e meno grave di una raccolta interrotta.

    `delivered` raccoglie le impronte arrivate su almeno un canale. Serve a
    non segnare come notificata una segnalazione che nessuno ha ricevuto: se
    la marcassimo comunque, il dedup la nasconderebbe per sempre.
    """
    outcome = NotifyOutcome()

    for notifier in notifiers:
        if not notifier.enabled:
            continue
        count = 0
        for alert in alerts:
            try:
                if notifier.send(alert):
                    count += 1
                    outcome.delivered.add(alert.fingerprint)
            except Exception as exc:
                logger.warning("Notifica su %s non riuscita: %s", notifier.name, exc)
        outcome.sent[notifier.name] = count

    return outcome
