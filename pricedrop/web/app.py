"""Dashboard web locale."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import analysis, db, queries
from ..config import Settings, load_settings
from ..scheduler import CollectionScheduler
from ..sync import CloudSync, SyncError
from .charts import (
    lines_chart_svg,
    parse_points,
    portfolio_chart_svg,
    price_chart_svg,
    sparkline_svg,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def filter_url(
    base: str,
    expansion: str | None = None,
    rarity: str | None = None,
    extra: dict | None = None,
) -> str:
    """Indirizzo di una pagina con i filtri scelti.

    Le pastiglie di espansione e rarita si compongono fra loro e non devono
    perdere gli altri parametri della pagina, come il periodo del Mercato.
    """
    params: list[tuple[str, str]] = []
    if expansion:
        params.append(("expansion", expansion))
    if rarity:
        params.append(("rarity", rarity))
    for key, value in (extra or {}).items():
        if value not in (None, ""):
            params.append((key, str(value)))
    return f"{base}?{urlencode(params)}" if params else base


def _euro(cents: float | None) -> str:
    if cents is None:
        return "-"
    return f"{cents / 100:,.2f}".replace(",", " ")


def _percent(ratio: float | None) -> str:
    if ratio is None:
        return "-"
    return f"{ratio * 100:.0f}%"


def parse_money(raw: str | None) -> int | None:
    """Converte un prezzo scritto a mano in centesimi.

    Accetta sia la virgola sia il punto come separatore decimale, e tollera
    il simbolo di valuta e gli spazi: chi compila il form scrive "12,50" o
    "12.50 EUR" senza pensarci.
    """
    if raw is None:
        return None
    cleaned = raw.strip().replace("EUR", "").replace("€", "").replace(" ", "")
    if not cleaned:
        return None
    cleaned = cleaned.replace(",", ".")
    try:
        return round(float(cleaned) * 100)
    except ValueError:
        return None


def _when(value: str | None) -> str:
    """Data e ora in forma leggibile, nel fuso locale di chi guarda."""
    if not value:
        return "-"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return moment.astimezone().strftime("%d/%m/%Y %H:%M")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings(require_token=False)
    scheduler = CollectionScheduler(settings)
    # La raccolta vera gira nel cloud: questo porta qui i dati che produce.
    cloud = CloudSync(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Lo schema viene creato all'avvio: aprire la dashboard su un progetto
        # appena installato non deve dare errore.
        db.connect(settings.database_path).close()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(title="PriceDrop One Piece TCG", lifespan=lifespan)

    @app.middleware("http")
    async def allinea_dal_cloud(request: Request, call_next):
        """Chiede un sync a ogni visita, senza far aspettare la pagina.

        Il lavoro parte in un thread e la risposta esce subito: scaricare
        l'artifact prima di rispondere aggiungerebbe qualche secondo a ogni
        clic. Chi vuole i dati adesso ha il pulsante, che invece aspetta.
        Un intervallo minimo interno evita un download per ogni clic di
        navigazione: prima del ciclo successivo non c'e' nulla di nuovo.
        """
        if request.method == "GET":
            cloud.sync_in_background()
        return await call_next(request)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["euro"] = _euro
    templates.env.filters["percent"] = _percent
    templates.env.filters["when"] = _when
    templates.env.globals["filter_url"] = filter_url

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.database_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def filters(
        conn: sqlite3.Connection, expansion: str, rarity: str
    ) -> dict:
        """Valida i filtri e prepara il contesto delle pastiglie.

        Accetta solo valori presenti nei dati: un parametro inventato
        nell'indirizzo mostra tutto, invece di una pagina vuota che
        sembrerebbe un guasto.
        """
        available = queries.expansions(conn)
        selected = expansion.strip().lower()
        if selected not in {row["code"] for row in available}:
            selected = ""

        available_rarities = queries.rarities(conn)
        selected_rarity = rarity.strip()
        if selected_rarity not in {row["rarity"] for row in available_rarities}:
            selected_rarity = ""

        return {
            "expansions": available,
            "selected": selected,
            "rarities": available_rarities,
            "selected_rarity": selected_rarity,
        }

    def page(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "settings": settings,
                "scheduler": scheduler,
                "cloud": cloud,
                **context,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        expansion: str = "",
        rarity: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        chosen = filters(conn, expansion, rarity)
        selected = chosen["selected"]
        selected_rarity = chosen["selected_rarity"]

        return page(
            request,
            "index.html",
            summary=queries.summary(conn),
            opportunities=queries.best_opportunities(
                conn,
                limit=15,
                min_saving_cents=settings.outlier.min_saving_cents,
                expansion_code=selected or None,
                rarity=selected_rarity or None,
            ),
            alerts=db.recent_alerts(
                conn,
                limit=12,
                expansion_code=selected or None,
                rarity=selected_rarity or None,
            ),
            valuable=queries.most_valuable(
                conn,
                limit=10,
                expansion_code=selected or None,
                rarity=selected_rarity or None,
            ),
            runs=queries.runs(conn, limit=6),
            **chosen,
        )

    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist_page(
        request: Request,
        expansion: str = "",
        rarity: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        chosen = filters(conn, expansion, rarity)
        rows = queries.watchlist(
            conn,
            expansion_code=chosen["selected"] or None,
            rarity=chosen["selected_rarity"] or None,
        )
        sparklines = {}
        for row in rows:
            values = [
                float(point["min_price_cents"])
                for point in queries.history(conn, row["blueprint_id"], days=30)
            ]
            sparklines[row["blueprint_id"]] = sparkline_svg(values)

        return page(
            request, "watchlist.html", rows=rows, sparklines=sparklines, **chosen
        )

    @app.get("/card/{blueprint_id}", response_class=HTMLResponse)
    def card_page(
        request: Request,
        blueprint_id: int,
        days: int = 90,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        info = queries.card_info(conn, blueprint_id)
        rows = queries.history(conn, blueprint_id, days=days)
        points = parse_points(rows)

        return page(
            request,
            "card.html",
            info=info,
            blueprint_id=blueprint_id,
            latest=queries.card(conn, blueprint_id),
            history=rows,
            chart=price_chart_svg(
                points,
                label=f"Andamento del prezzo di {info['name'] if info else blueprint_id}",
            ),
            alerts=queries.card_alerts(conn, blueprint_id),
            days=days,
        )

    @app.get("/search", response_class=HTMLResponse)
    def search_page(
        request: Request,
        q: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        results = queries.search_cards(conn, q) if q.strip() else []
        return page(request, "search.html", term=q, results=results)

    @app.get("/market", response_class=HTMLResponse)
    def market_page(
        request: Request,
        days: int = 30,
        expansion: str = "",
        rarity: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        chosen = filters(conn, expansion, rarity)
        selected = chosen["selected"] or None
        selected_rarity = chosen["selected_rarity"] or None

        risers, fallers = analysis.movers(
            conn,
            days=days,
            min_reference_cents=settings.outlier.min_reference_cents,
            min_change_cents=settings.outlier.min_saving_cents,
            expansion_code=selected,
            rarity=selected_rarity,
        )
        # L'indice e gia per espansione, quindi il filtro per set lo
        # restringerebbe a una sola linea: qui vale solo quello per rarita.
        indexes = analysis.market_index(conn, days=days)
        if selected:
            indexes = {
                code: series for code, series in indexes.items() if code == selected
            }
        supply = analysis.supply_series(
            conn, days=days, expansion_code=selected, rarity=selected_rarity
        )

        return page(
            request,
            "market.html",
            coverage=analysis.coverage(conn),
            days=days,
            risers=risers,
            fallers=fallers,
            index_names=sorted(indexes),
            index_chart=lines_chart_svg(
                indexes,
                baseline=100.0,
                unit="",
                label="Indice di prezzo per espansione, base 100",
            ),
            supply_chart=lines_chart_svg(
                {"offerta": supply} if supply else {},
                label="Inserzioni per carta, mediana giornaliera",
            ),
            volatile=analysis.volatility(
                conn,
                days=days,
                expansion_code=selected,
                rarity=selected_rarity,
            ),
            **chosen,
        )

    @app.get("/collection", response_class=HTMLResponse)
    def collection_page(
        request: Request,
        days: int = 90,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        totals = queries.portfolio(conn)
        series = queries.portfolio_history(conn, days=days)

        return page(
            request,
            "collection.html",
            holdings=queries.holdings(conn),
            totals=totals,
            days=days,
            chart=portfolio_chart_svg(
                series,
                cost_cents=totals.cost_total if totals.cost_total else None,
            ),
            points=len(series),
        )

    @app.post("/collection/add")
    def collection_add(
        blueprint_id: int = Form(),
        quantity: int = Form(default=1),
        purchase_price: str = Form(default=""),
        condition: str = Form(default=""),
        acquired_at: str = Form(default=""),
        note: str = Form(default=""),
        back: str = Form(default="/collection"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> RedirectResponse:
        db.add_holding(
            conn,
            blueprint_id,
            quantity=max(quantity, 1),
            purchase_price_cents=parse_money(purchase_price),
            condition=condition.strip() or None,
            acquired_at=acquired_at.strip() or None,
            note=note.strip() or None,
        )
        return RedirectResponse(_safe_redirect(back), status_code=303)

    @app.post("/collection/{holding_id}/update")
    def collection_update(
        holding_id: int,
        quantity: int = Form(default=1),
        purchase_price: str = Form(default=""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> RedirectResponse:
        db.update_holding(
            conn,
            holding_id,
            quantity=max(quantity, 1),
            purchase_price_cents=parse_money(purchase_price),
        )
        return RedirectResponse("/collection", status_code=303)

    @app.post("/collection/{holding_id}/delete")
    def collection_delete(
        holding_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> RedirectResponse:
        db.delete_holding(conn, holding_id)
        return RedirectResponse("/collection", status_code=303)

    @app.post("/watchlist/{blueprint_id}")
    def toggle_watchlist(
        blueprint_id: int,
        back: str = Form(default="/"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> RedirectResponse:
        queries.toggle_watchlist(conn, blueprint_id)
        return RedirectResponse(_safe_redirect(back), status_code=303)

    @app.post("/collect")
    def collect_now(back: str = Form(default="/")) -> RedirectResponse:
        # Il ciclo e sincrono: su quattro set impiega pochi secondi, e vedere
        # subito il risultato vale piu di una risposta immediata e vuota.
        try:
            scheduler.run_once()
        except Exception:
            # L'errore e gia registrato nello stato dello scheduler e nel
            # database: la pagina lo mostrera.
            pass
        return RedirectResponse(_safe_redirect(back), status_code=303)

    @app.post("/sync")
    def sync_now(back: str = Form(default="/")) -> RedirectResponse:
        # Questo e' bloccante, al contrario del sync automatico: chi preme il
        # pulsante vuole vedere i dati nuovi nella pagina che si ricarica,
        # non al clic successivo.
        try:
            cloud.sync_now()
        except SyncError:
            # Il motivo e' nello stato, il footer lo mostra.
            pass
        return RedirectResponse(_safe_redirect(back), status_code=303)

    return app


def _safe_redirect(target: str) -> str:
    """Accetta solo percorsi interni.

    Senza questo controllo il campo nascosto del form potrebbe rimandare
    l'utente su un sito esterno.
    """
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/"
