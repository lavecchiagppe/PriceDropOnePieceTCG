"""Interfaccia a riga di comando.

Uso: python -m pricedrop <comando>

Comandi:
  check     verifica il token e la connessione all'API
  games     cerca il gioco One Piece nel catalogo CardTrader
  sets      elenca le espansioni One Piece, con i codici per config.toml
  init-db   crea il database e lo schema
  collect   esegue un ciclo di raccolta, valuta le regole e notifica
  alerts    elenca le segnalazioni registrate
  status    mostra gli ultimi cicli e la consistenza dello storico
  trends    indice per espansione e carte in salita o in discesa
  notify    invia una segnalazione di prova su tutti i canali attivi
  serve     avvia la dashboard web e la raccolta automatica
"""

from __future__ import annotations

import argparse
import logging
import re
import sys

from . import db
from .collector import NoExpansionsConfigured, collect
from .config import ConfigError, Settings, load_settings
from .notifiers import build_notifiers, notify_all
from .rules import RULE_OUTLIER, Alert
from .providers import AuthenticationError, CardTraderError, CardTraderProvider


# Codici dei set principali One Piece: "op" piu sole cifre.
MAIN_SET_CODE = re.compile(r"op\d+", re.IGNORECASE)


def _euro(cents: float) -> str:
    return f"{cents / 100:.2f}"


def _build_provider(settings: Settings) -> CardTraderProvider:
    return CardTraderProvider(settings.cardtrader_token)


def cmd_check(settings: Settings) -> int:
    with _build_provider(settings) as provider:
        info = provider.info()
        app_name = info.get("name") or "senza nome"
        print(f"Autenticazione riuscita. App: {app_name} (id {info.get('id')})")

        game_id = provider.find_game_id("one piece")
        if game_id is None:
            print("Attenzione: nessun gioco One Piece trovato nel catalogo.")
            return 1
        print(f"Gioco One Piece trovato: game_id {game_id}")
    return 0


def cmd_games(settings: Settings) -> int:
    with _build_provider(settings) as provider:
        for game in provider.games():
            label = game.get("display_name") or game.get("name") or ""
            if "one piece" in label.lower() or "one piece" in str(game.get("name", "")).lower():
                print(f"{game['id']:>5}  {label}")
    return 0


def cmd_sets(settings: Settings) -> int:
    with _build_provider(settings) as provider:
        game_id = provider.find_game_id("one piece")
        if game_id is None:
            print("Nessun gioco One Piece trovato.", file=sys.stderr)
            return 1

        expansions = [
            item
            for item in provider.expansions()
            if int(item.get("game_id") or 0) == game_id
        ]
        # L'API non espone la data di uscita: l'id crescente e il miglior
        # indizio di recenzita disponibile.
        expansions.sort(key=lambda item: int(item["id"]), reverse=True)

        print(f"{len(expansions)} espansioni One Piece, dalla piu recente:\n")
        print(f"{'ID':>7}  {'CODICE':<10} NOME")
        for item in expansions:
            print(f"{item['id']:>7}  {str(item.get('code') or ''):<10} {item.get('name')}")

        # I set principali hanno codice "op" seguito solo da cifre: le promo
        # (op18p), i mazzi (st37) e gli eventi (t-cup) hanno id alti ma non
        # sono il mercato che interessa monitorare.
        main_sets = [
            item
            for item in expansions
            if MAIN_SET_CODE.fullmatch(str(item.get("code") or ""))
        ]

        print("\nSet principali, dai piu recenti:")
        for item in main_sets[:8]:
            print(f"{item['id']:>7}  {str(item.get('code')):<10} {item.get('name')}")

        recent = [str(item.get("code")) for item in main_sets[:4]]
        print("\nPer monitorare i quattro piu recenti, in config.toml:")
        formatted = ", ".join(f'"{code}"' for code in recent)
        print(f"  expansion_codes = [{formatted}]")
    return 0


def cmd_init_db(settings: Settings) -> int:
    with db.session(settings.database_path) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    print(f"Database pronto: {settings.database_path}")
    print("Tabelle: " + ", ".join(row["name"] for row in tables))
    return 0


def cmd_collect(settings: Settings) -> int:
    notifiers = build_notifiers(settings)
    with _build_provider(settings) as provider:
        result = collect(settings, provider, notifiers)

    print(f"Espansioni raccolte: {result.expansion_count}")
    for item in result.expansions:
        print(f"  {item.get('code')}  {item.get('name')}")
    print(f"Carte viste:          {result.cards_seen}")
    print(f"Snapshot salvati:     {result.snapshots_saved}")
    print(f"Scartate (mercato sottile o prezzo irrilevante): {result.skipped_thin}")
    print(f"Riverificate senza cache: {result.verified}")

    print(f"\nSegnalazioni nuove:   {result.alert_count}")
    for alert in result.alerts[:10]:
        print(
            f"  [{alert.rule}] -{alert.discount_ratio * 100:.0f}%  "
            f"{_euro(alert.price_cents)} vs {_euro(alert.reference_price_cents)} "
            f"{alert.currency}  (risparmio {_euro(alert.saving_cents)})  "
            f"{alert.card_name[:34]}"
        )
    if result.alert_count > 10:
        print(f"  ... e altre {result.alert_count - 10}. Elenco con: alerts")

    if not result.trend_rule_active:
        print(
            "\nLa regola di calo nel tempo e ancora inattiva: lo storico non ha "
            "abbastanza punti. Si accende da se quando li raggiunge."
        )

    if result.notifications_sent:
        print(
            "\nNotifiche inviate: "
            + ", ".join(f"{k}={v}" for k, v in result.notifications_sent.items())
        )
    # Lo stato si rilegge dopo l'invio: un canale puo scoprirsi non disponibile
    # solo al primo tentativo, per esempio se manca la libreria dei toast.
    inactive = [notifier.name for notifier in notifiers if not notifier.enabled]
    if inactive:
        print("Canali non attivi: " + ", ".join(inactive))
    return 0


def cmd_trends(settings: Settings) -> int:
    from . import analysis

    with db.session(settings.database_path) as conn:
        cover = analysis.coverage(conn)
        risers, fallers = analysis.movers(
            conn,
            days=30,
            min_reference_cents=settings.outlier.min_reference_cents,
            min_change_cents=settings.outlier.min_saving_cents,
        )
        indexes = analysis.market_index(conn, days=30)

    print(
        f"{cover.snapshots} rilevazioni su {cover.cards} carte, "
        f"in {cover.distinct_days} giorni "
        f"({cover.days:.1f} giorni di copertura)."
    )
    if not cover.is_enough_for(30):
        print(
            "Storico ancora troppo corto per l'analisi a 30 giorni: i numeri "
            "qui sotto si basano su pochi punti.\n"
        )

    if indexes:
        print("\nIndice per espansione, base 100 a inizio periodo:")
        for code in sorted(indexes):
            series = indexes[code]
            print(
                f"  {code:<8} {series[-1][1]:7.1f}  "
                f"({len(series)} giorni, da {series[0][0]} a {series[-1][0]})"
            )

    for title, movers_list, sign in (
        ("In salita", risers, "+"),
        ("In discesa", fallers, ""),
    ):
        print(f"\n{title}:")
        if not movers_list:
            print("  nessun movimento rilevante.")
            continue
        for item in movers_list:
            print(
                f"  {sign}{item.change_ratio * 100:5.1f}%  "
                f"{_euro(item.first_cents):>9} -> {_euro(item.last_cents):>9}  "
                f"{item.card_name[:34]}"
            )
    return 0


def cmd_notify(settings: Settings) -> int:
    """Manda una segnalazione finta sui canali configurati.

    Serve a vedere subito se email o Telegram sono impostati bene, senza
    aspettare che il mercato produca una vera occasione.
    """
    notifiers = build_notifiers(settings)
    active = [notifier.name for notifier in notifiers if notifier.enabled]
    if not active:
        print(
            "Nessun canale attivo. Compila .env: per l'email servono "
            "SMTP_HOST, SMTP_USER, SMTP_PASSWORD e EMAIL_TO."
        )
        return 1

    print("Canali attivi: " + ", ".join(active))

    alert = Alert(
        fingerprint="prova",
        blueprint_id=406121,
        rule=RULE_OUTLIER,
        card_name="Prova di notifica - Edward.Newgate",
        currency="EUR",
        price_cents=80054.0,
        reference_price_cents=125064.0,
        eligible_count=7,
        listing_id=1,
        seller="prova",
    )

    outcome = notify_all(notifiers, [alert])
    for name, count in outcome.sent.items():
        esito = "inviata" if count else "NON inviata"
        print(f"  {name}: {esito}")

    if not outcome.delivered:
        print(
            "\nNessun canale ha accettato il messaggio. Rilancia con -v per "
            "vedere l'errore del server."
        )
        return 1

    if outcome.sent.get("email"):
        print("\nSe non la vedi in posta, controlla anche la cartella spam.")
    return 0


def cmd_serve(settings: Settings) -> int:
    # Import ritardati: le dipendenze web non servono agli altri comandi.
    import uvicorn

    from .web import create_app

    print(f"Dashboard su http://127.0.0.1:{settings.web_port}")
    if settings.schedule_enabled:
        print(
            f"Raccolta automatica ogni {settings.schedule_interval_minutes} minuti "
            "finche la dashboard resta aperta."
        )
    print("Ctrl+C per fermare.\n")

    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=settings.web_port,
        log_level="warning",
    )
    return 0


def cmd_alerts(settings: Settings) -> int:
    with db.session(settings.database_path) as conn:
        rows = db.recent_alerts(conn, limit=30)

    if not rows:
        print("Nessuna segnalazione registrata.")
        return 0

    print(f"Ultime {len(rows)} segnalazioni:\n")
    for row in rows:
        discount = (row["discount_ratio"] or 0) * 100
        notified = "inviata" if row["notified_at"] else "non inviata"
        print(
            f"{row['created_at']}  [{row['rule']}]  -{discount:.0f}%  "
            f"{_euro(row['price_cents'])} vs "
            f"{_euro(row['reference_price_cents'] or 0)} {row['currency'] or ''}  "
            f"{(row['card_name'] or '?')[:32]}  ({notified})"
        )
        print(f"    https://www.cardtrader.com/cards/{row['blueprint_id']}")
    return 0


def cmd_status(settings: Settings) -> int:
    with db.session(settings.database_path) as conn:
        runs = conn.execute(
            """
            SELECT started_at, finished_at, expansions, cards_seen,
                   snapshots_saved, status, error
              FROM collection_runs
             ORDER BY id DESC
             LIMIT 10
            """
        ).fetchall()

        totals = conn.execute(
            """
            SELECT COUNT(*) AS snapshots,
                   COUNT(DISTINCT blueprint_id) AS cards,
                   MIN(taken_at) AS first_at,
                   MAX(taken_at) AS last_at
              FROM snapshots
            """
        ).fetchone()

    print(f"Snapshot totali: {totals['snapshots']} su {totals['cards'] or 0} carte")
    if totals["snapshots"]:
        print(f"Storico da {totals['first_at']} a {totals['last_at']}")
    else:
        print("Nessuno snapshot ancora: lo storico parte dal primo ciclo di raccolta.")

    if runs:
        print("\nUltimi cicli:")
        for run in runs:
            flag = "ok " if run["status"] == "ok" else "ERR"
            print(
                f"  [{flag}] {run['started_at']}  "
                f"espansioni={run['expansions']}  "
                f"carte={run['cards_seen']}  "
                f"snapshot={run['snapshots_saved']}"
            )
            if run["error"]:
                print(f"         {run['error']}")
    return 0


COMMANDS = {
    "check": cmd_check,
    "games": cmd_games,
    "sets": cmd_sets,
    "init-db": cmd_init_db,
    "collect": cmd_collect,
    "status": cmd_status,
    "alerts": cmd_alerts,
    "trends": cmd_trends,
    "notify": cmd_notify,
    "serve": cmd_serve,
}

# Comandi che funzionano senza token, perche non toccano la rete.
OFFLINE_COMMANDS = {"init-db", "status", "alerts", "trends", "notify"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pricedrop",
        description="Monitoraggio prezzi One Piece TCG su CardTrader.",
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="comando da eseguire")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="mostra i log di dettaglio"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # I nomi delle carte contengono caratteri che la console di Windows non
    # sempre sa rappresentare: senza questo, una stampa puo interrompere
    # un ciclo di raccolta andato a buon fine.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")

    try:
        settings = load_settings(
            require_token=args.command not in OFFLINE_COMMANDS
        )
    except ConfigError as exc:
        print(f"Configurazione: {exc}", file=sys.stderr)
        return 2

    try:
        return COMMANDS[args.command](settings)
    except NoExpansionsConfigured as exc:
        print(f"Configurazione: {exc}", file=sys.stderr)
        return 2
    except AuthenticationError as exc:
        print(f"Autenticazione: {exc}", file=sys.stderr)
        return 3
    except CardTraderError as exc:
        print(f"Errore CardTrader: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

