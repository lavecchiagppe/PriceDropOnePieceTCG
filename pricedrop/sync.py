"""Allineamento del database locale con la raccolta che gira nel cloud.

La raccolta vera gira su GitHub Actions ogni pochi minuti (vedi CLOUD.md).
Questo modulo porta quei dati sul PC: scarica l'artifact pubblicato dall'ultimo
ciclo riuscito e ne travasa le righe nuove nel database locale.

Il travaso e' additivo, mai distruttivo, e la direzione e' una sola: cloud ->
locale. Il motivo e' che le due copie non contengono le stesse cose.

- Snapshot, segnalazioni e cronologia dei cicli nascono nel cloud: in locale si
  aggiungono quelle che mancano, riconosciute dalla loro chiave naturale.
- Collezione e watchlist nascono invece dalla dashboard locale, e il cloud non
  le tocca mai: restano dove sono, intonse. Sovrascrivere il file locale con
  quello remoto, che era la strada piu' semplice, le avrebbe cancellate.

Cosi' il database locale e' un sovrainsieme di quello remoto, e un eventuale
ciclo lanciato a mano con "Raccogli ora" non viene perso al sync successivo.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

# Nome dell'artifact caricato dal workflow e del file che contiene.
ARTIFACT_NAME = "database"
DATABASE_FILENAME = "pricedrop.db"
WORKFLOW_NAME = "Raccolta prezzi"

# Attesa massima per i comandi gh: la rete puo' essere lenta, ma oltre questo
# conviene arrendersi e lasciare la dashboard navigabile sui dati che ha.
COMMAND_TIMEOUT = 120.0


class SyncError(RuntimeError):
    """Il sync non e' andato a buon fine, con un motivo leggibile in pagina."""


@dataclass(slots=True)
class SyncResult:
    """Cosa ha portato l'ultimo sync."""

    run_id: int | None = None
    run_at: str | None = None
    snapshots: int = 0
    alerts: int = 0
    runs: int = 0
    cards: int = 0

    @property
    def total(self) -> int:
        return self.snapshots + self.alerts + self.runs + self.cards

    @property
    def empty(self) -> bool:
        return self.total == 0


@dataclass(slots=True)
class SyncState:
    """Stato da mostrare in pagina, come per lo scheduler."""

    running: bool = False
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    error: str | None = None
    result: SyncResult = field(default_factory=SyncResult)


def _run_gh(args: list[str]) -> str:
    """Esegue un comando gh e restituisce lo stdout.

    Si appoggia alla CLI gh invece che all'API diretta perche' gh e' gia'
    autenticata sul PC di chi usa il progetto: nessun token da incollare in
    un secondo posto, nessun segreto nel file di configurazione.
    """
    gh = shutil.which("gh")
    if gh is None:
        raise SyncError(
            "Non trovo il comando gh. Installa GitHub CLI (cli.github.com) "
            "e autenticati con: gh auth login"
        )
    try:
        completed = subprocess.run(
            [gh, *args],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            # Senza questo, su Windows, gh aprirebbe una finestra di console.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(
            f"gh non ha risposto entro {COMMAND_TIMEOUT:.0f} secondi."
        ) from exc

    if completed.returncode != 0:
        dettaglio = (completed.stderr or completed.stdout or "").strip()
        prima_riga = dettaglio.splitlines()[0] if dettaglio else "errore sconosciuto"
        minuscolo = dettaglio.lower()
        if "auth" in minuscolo and "login" in minuscolo:
            raise SyncError("gh non e' autenticato: esegui gh auth login.")
        if "no valid artifacts" in minuscolo:
            # Capita con un ciclo lanciato da una versione del workflow che non
            # pubblicava l'artifact, e finche' il primo ciclo nuovo non gira.
            raise SyncError(
                "L'ultimo ciclo nel cloud non ha pubblicato il database. "
                "Serve almeno un ciclo eseguito con la versione corrente del "
                "workflow: lancialo dalla tab Actions o attendi il prossimo."
            )
        raise SyncError(f"gh ha risposto con un errore: {prima_riga}")
    return completed.stdout


def latest_successful_run() -> tuple[int, str]:
    """Id e orario dell'ultimo ciclo di raccolta riuscito nel cloud."""
    raw = _run_gh(
        [
            "run",
            "list",
            "--workflow",
            WORKFLOW_NAME,
            "--status",
            "success",
            "--limit",
            "1",
            "--json",
            "databaseId,createdAt",
        ]
    )
    try:
        righe = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise SyncError("Risposta di gh non leggibile.") from exc
    if not righe:
        raise SyncError(
            "Nessun ciclo di raccolta riuscito nel cloud: controlla la tab "
            "Actions del repository."
        )
    prima = righe[0]
    return int(prima["databaseId"]), str(prima.get("createdAt") or "")


def download_cloud_database(run_id: int, destination: Path) -> Path:
    """Scarica il database pubblicato da un ciclo, in una cartella di lavoro."""
    destination.mkdir(parents=True, exist_ok=True)
    _run_gh(
        [
            "run",
            "download",
            str(run_id),
            "--name",
            ARTIFACT_NAME,
            "--dir",
            str(destination),
        ]
    )
    scaricato = destination / DATABASE_FILENAME
    if not scaricato.exists():
        # L'artifact potrebbe contenere il file con un altro nome: si prende
        # il primo .db trovato invece di fallire per un dettaglio.
        candidati = sorted(destination.glob("*.db"))
        if not candidati:
            raise SyncError("L'artifact scaricato non contiene un database.")
        scaricato = candidati[0]
    return scaricato


# Tabelle che arrivano dal cloud, con la chiave naturale che identifica una
# riga gia' presente. L'id autoincrementale non serve: fra due database gli
# stessi dati hanno id diversi, e confrontarli porterebbe a duplicati.
# L'ordine conta, perche' le chiavi esterne restano attive: prima le espansioni
# e le carte, poi cio' che le referenzia.
_MERGE_PLAN: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("expansions", ("id",)),
    ("cards", ("blueprint_id",)),
    ("snapshots", ("blueprint_id", "taken_at")),
    ("alerts", ("fingerprint",)),
    ("collection_runs", ("started_at",)),
)


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def merge_cloud_into_local(local_path: Path, cloud_path: Path) -> SyncResult:
    """Aggiunge al database locale le righe presenti solo in quello remoto.

    Non cancella e non aggiorna nulla: una riga che esiste in entrambi resta
    come sta in locale. Collezione e watchlist non vengono nemmeno guardate.
    """
    if not cloud_path.exists():
        raise SyncError("Il database scaricato non esiste.")

    risultato = SyncResult()
    conteggi: dict[str, int] = {}
    conn = db.connect(local_path)
    try:
        conn.execute("ATTACH DATABASE ? AS cloud", (str(cloud_path),))
        try:
            for tabella, chiave in _MERGE_PLAN:
                locali = _columns(conn, tabella, "main")
                remote = _columns(conn, tabella, "cloud")
                # Solo le colonne presenti in entrambi: un database remoto piu'
                # vecchio o piu' nuovo di qualche colonna non blocca il sync.
                comuni = [c for c in locali if c in remote and c != "id"]
                if not comuni:
                    continue
                elenco = ", ".join(comuni)
                condizione = " AND ".join(
                    f"esistente.{c} IS nuovo.{c}" for c in chiave
                )
                cursore = conn.execute(
                    f"INSERT INTO main.{tabella} ({elenco}) "
                    f"SELECT {elenco} FROM cloud.{tabella} AS nuovo "
                    f"WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM main.{tabella} AS esistente WHERE {condizione}"
                    f")"
                )
                aggiunte = cursore.rowcount if (cursore.rowcount or 0) > 0 else 0
                conteggi[tabella] = aggiunte
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE cloud")
    finally:
        conn.close()

    risultato.snapshots = conteggi.get("snapshots", 0)
    risultato.alerts = conteggi.get("alerts", 0)
    risultato.runs = conteggi.get("collection_runs", 0)
    risultato.cards = conteggi.get("cards", 0)
    return risultato


class CloudSync:
    """Porta sul PC i dati raccolti nel cloud, su richiesta o visitando il sito.

    Un lock impedisce due sync insieme: il pulsante e l'aggiornamento
    automatico della pagina possono scattare nello stesso momento, e due
    travasi in parallelo sullo stesso database si ostacolerebbero.
    """

    def __init__(
        self, settings: Settings, *, min_interval_seconds: float | None = None
    ) -> None:
        self._settings = settings
        # Navigare fra le pagine non deve scaricare un artifact per clic: sotto
        # questa distanza dall'ultimo tentativo il sync automatico non parte.
        # Di norma coincide col ciclo di raccolta: prima non c'e' nulla di nuovo
        # da prendere.
        if min_interval_seconds is None:
            min_interval_seconds = max(
                60.0, settings.schedule_interval_minutes * 60.0
            )
        self._min_interval = min_interval_seconds
        self._lock = threading.Lock()
        self._last_attempt: float | None = None
        self.state = SyncState()

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    @property
    def enabled(self) -> bool:
        """Falso quando il sync non ha modo di funzionare.

        Serve la CLI gh (per scaricare l'artifact) e un repository git da cui
        gh capisca a quale progetto GitHub rivolgersi. Senza, la dashboard non
        deve mostrare un pulsante che non puo' funzionare.
        """
        if os.environ.get("PRICEDROP_DISABLE_SYNC"):
            return False
        if shutil.which("gh") is None:
            return False
        return (PROJECT_ROOT / ".git").exists()

    def sync_now(self) -> SyncResult:
        """Scarica e travasa subito. Solleva SyncError se non ci riesce."""
        with self._lock:
            self.state.running = True
            self.state.last_attempt_at = db.utcnow()
            self._last_attempt = time.monotonic()
            try:
                run_id, run_at = latest_successful_run()
                with tempfile.TemporaryDirectory(prefix="pricedrop-sync-") as tmp:
                    scaricato = download_cloud_database(run_id, Path(tmp))
                    risultato = merge_cloud_into_local(
                        self._settings.database_path, scaricato
                    )
                risultato.run_id = run_id
                risultato.run_at = run_at
                self.state.result = risultato
                self.state.error = None
                self.state.last_success_at = db.utcnow()
                logger.info(
                    "Sync dal cloud: %d snapshot e %d segnalazioni dal ciclo %s",
                    risultato.snapshots,
                    risultato.alerts,
                    run_id,
                )
                return risultato
            except SyncError as exc:
                self.state.error = str(exc)
                logger.warning("Sync dal cloud non riuscito: %s", exc)
                raise
            except Exception as exc:
                self.state.error = f"Errore inatteso: {exc}"
                logger.exception("Sync dal cloud non riuscito")
                raise SyncError(self.state.error) from exc
            finally:
                self.state.running = False

    @property
    def due(self) -> bool:
        """Vero se e' passato abbastanza tempo per riprovare da se'."""
        if self._last_attempt is None:
            return True
        return (time.monotonic() - self._last_attempt) >= self._min_interval

    def sync_in_background(self) -> bool:
        """Avvia un sync in un thread, se serve. Dice se l'ha avviato.

        La pagina non aspetta: chi apre la dashboard la vede subito, coi dati
        che il PC ha gia', e il giro successivo mostra quelli nuovi. Un sync
        bloccante aggiungerebbe qualche secondo a ogni visita.
        """
        if not self.enabled or self.state.running or not self.due:
            return False

        def lavora() -> None:
            try:
                self.sync_now()
            except SyncError:
                # Lo stato tiene il motivo, la pagina lo mostra.
                pass

        thread = threading.Thread(target=lavora, name="pricedrop-sync", daemon=True)
        thread.start()
        return True
