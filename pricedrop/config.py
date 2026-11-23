"""Caricamento della configurazione: segreti da .env, parametri da config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Configurazione mancante o non valida."""


@dataclass(frozen=True, slots=True)
class FilterSettings:
    conditions: tuple[str, ...] = ("Near Mint", "Slightly Played")
    require_cardtrader_zero: bool = True
    exclude_graded: bool = True


@dataclass(frozen=True, slots=True)
class OutlierSettings:
    reference_window: int = 5
    min_comparables: int = 4
    threshold: float = 0.35
    min_reference_cents: int = 100
    # Risparmio minimo in valore assoluto perche valga un alert. Sui dati reali
    # la sola soglia percentuale premia le carte da pochi centesimi, dove un
    # -70% vale un euro e mezzo.
    min_saving_cents: int = 300


@dataclass(frozen=True, slots=True)
class TrendSettings:
    """Regola secondaria: calo rispetto al passato recente."""

    window_days: int = 7
    threshold: float = 0.15
    # Punti storici minimi nella finestra perche il confronto abbia senso.
    min_points: int = 5
    min_saving_cents: int = 300


@dataclass(frozen=True, slots=True)
class Settings:
    cardtrader_token: str
    provider_name: str = "cardtrader"
    language: str = "en"
    expansion_codes: tuple[str, ...] = ()
    expansion_ids: tuple[int, ...] = ()
    # Rarita da storicizzare comunque, anche sotto `min_reference_cents`.
    # Senza questo le SR da meno di un euro sparivano insieme ai comuni.
    track_rarities: tuple[str, ...] = ()
    database_path: Path = PROJECT_ROOT / "data" / "pricedrop.db"
    web_port: int = 8000
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # Email: server, credenziali e destinatario stanno tutti in .env, perche
    # sono segreti o dati personali e config.toml si versiona.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_to: str | None = None
    email_from: str | None = None
    desktop_notifications: bool = True
    schedule_enabled: bool = True
    schedule_interval_minutes: int = 60
    filters: FilterSettings = field(default_factory=FilterSettings)
    outlier: OutlierSettings = field(default_factory=OutlierSettings)
    trend: TrendSettings = field(default_factory=TrendSettings)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def email_enabled(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_user
            and self.smtp_password
            and self.email_to
        )


def _as_tuple(value: object, cast=str) -> tuple:
    if not value:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"Atteso un elenco, trovato {type(value).__name__}: {value!r}")
    return tuple(cast(item) for item in value)


def load_settings(
    *,
    env_path: Path | None = None,
    config_path: Path | None = None,
    require_token: bool = True,
) -> Settings:
    """Legge .env e config.toml e restituisce le impostazioni.

    Il token non viene mai registrato nei log né stampato: resta solo
    nell'oggetto Settings in memoria.
    """
    env_path = env_path or PROJECT_ROOT / ".env"
    config_path = config_path or PROJECT_ROOT / "config.toml"

    load_dotenv(env_path, override=False)

    token = (os.getenv("CARDTRADER_API_TOKEN") or "").strip()
    if require_token and not token:
        raise ConfigError(
            f"CARDTRADER_API_TOKEN non impostato. Incolla il token JWT in {env_path} "
            "dopo 'CARDTRADER_API_TOKEN=' (senza 'Bearer' davanti)."
        )

    raw: dict = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)

    provider = raw.get("provider", {})
    monitor = raw.get("monitor", {})
    filters = raw.get("filters", {})
    outlier = raw.get("outlier", {})
    trend = raw.get("trend", {})
    notifications = raw.get("notifications", {})
    schedule = raw.get("schedule", {})

    database_path = Path(os.getenv("DATABASE_PATH") or "data/pricedrop.db")
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    return Settings(
        cardtrader_token=token,
        provider_name=str(provider.get("name", "cardtrader")),
        language=str(provider.get("language", "en")),
        expansion_codes=_as_tuple(monitor.get("expansion_codes"), str),
        expansion_ids=_as_tuple(monitor.get("expansion_ids"), int),
        track_rarities=_as_tuple(monitor.get("track_rarities"), str),
        database_path=database_path,
        web_port=int(os.getenv("WEB_PORT") or 8000),
        telegram_bot_token=(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or None,
        telegram_chat_id=(os.getenv("TELEGRAM_CHAT_ID") or "").strip() or None,
        smtp_host=(os.getenv("SMTP_HOST") or "").strip() or None,
        smtp_port=int(os.getenv("SMTP_PORT") or 587),
        smtp_user=(os.getenv("SMTP_USER") or "").strip() or None,
        # La password non viene ripulita: alcuni server accettano spazi.
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        email_to=(os.getenv("EMAIL_TO") or "").strip() or None,
        email_from=(os.getenv("EMAIL_FROM") or "").strip() or None,
        desktop_notifications=bool(notifications.get("desktop", True)),
        schedule_enabled=bool(schedule.get("enabled", True)),
        schedule_interval_minutes=max(
            int(schedule.get("interval_minutes", 60)), 5
        ),
        filters=FilterSettings(
            conditions=_as_tuple(filters.get("conditions"), str) or FilterSettings().conditions,
            require_cardtrader_zero=bool(filters.get("require_cardtrader_zero", True)),
            exclude_graded=bool(filters.get("exclude_graded", True)),
        ),
        outlier=OutlierSettings(
            reference_window=int(outlier.get("reference_window", 5)),
            min_comparables=int(outlier.get("min_comparables", 4)),
            threshold=float(outlier.get("threshold", 0.35)),
            min_reference_cents=int(outlier.get("min_reference_cents", 100)),
            min_saving_cents=int(outlier.get("min_saving_cents", 300)),
        ),
        trend=TrendSettings(
            window_days=int(trend.get("window_days", 7)),
            threshold=float(trend.get("threshold", 0.15)),
            min_points=int(trend.get("min_points", 5)),
            min_saving_cents=int(trend.get("min_saving_cents", 300)),
        ),
    )
