"""Ciclo di raccolta automatico, nello stesso processo della dashboard."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from .collector import CollectResult, collect
from .config import Settings
from .notifiers import build_notifiers
from .providers import CardTraderProvider

logger = logging.getLogger(__name__)

JOB_ID = "collect"


@dataclass(slots=True)
class RunState:
    """Esito dell'ultimo ciclo, per mostrarlo in pagina."""

    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    snapshots: int = 0
    alerts: int = 0
    error: str | None = None


class CollectionScheduler:
    """Esegue la raccolta a intervalli regolari e su richiesta.

    Un lock impedisce che un ciclo manuale e uno automatico si sovrappongano:
    due raccolte in parallelo sprecherebbero chiamate API e scriverebbero
    snapshot doppi per lo stesso istante.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._lock = threading.Lock()
        self.state = RunState()

    @property
    def interval_minutes(self) -> int:
        return self._settings.schedule_interval_minutes

    @property
    def enabled(self) -> bool:
        return self._settings.schedule_enabled

    @property
    def next_run_at(self) -> str | None:
        job = self._scheduler.get_job(JOB_ID)
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time.isoformat(timespec="seconds")

    def start(self) -> None:
        if not self.enabled:
            logger.info("Raccolta automatica disattivata da configurazione.")
            return
        self._scheduler.add_job(
            self._job,
            trigger="interval",
            minutes=self.interval_minutes,
            id=JOB_ID,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("Raccolta automatica ogni %s minuti.", self.interval_minutes)

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _job(self) -> None:
        try:
            self.run_once()
        except Exception as exc:
            # Un ciclo fallito non deve fermare lo scheduler: il prossimo
            # tentativo potrebbe riuscire, e l'errore resta visibile in pagina.
            logger.warning("Ciclo automatico non riuscito: %s", exc)

    def run_once(self) -> CollectResult | None:
        """Esegue un ciclo. Restituisce None se un altro e gia in corso."""
        if not self._lock.acquire(blocking=False):
            logger.info("Raccolta gia in corso: richiesta ignorata.")
            return None

        self.state = RunState(running=True, started_at=_now())
        try:
            notifiers = build_notifiers(self._settings)
            with CardTraderProvider(self._settings.cardtrader_token) as provider:
                result = collect(self._settings, provider, notifiers)
        except Exception as exc:
            self.state = RunState(
                running=False,
                started_at=self.state.started_at,
                finished_at=_now(),
                error=str(exc)[:300],
            )
            raise
        else:
            self.state = RunState(
                running=False,
                started_at=self.state.started_at,
                finished_at=_now(),
                snapshots=result.snapshots_saved,
                alerts=result.alert_count,
            )
            return result
        finally:
            self._lock.release()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
