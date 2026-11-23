"""Grafici come SVG generato lato server.

Niente librerie JavaScript: la dashboard e locale e deve funzionare anche
senza rete. I colori arrivano dalle variabili CSS della pagina, cosi il
grafico segue il tema chiaro o scuro.

Tre scelte di lettura valgono per tutti i grafici qui dentro:

* la griglia e una linea sottile piena, mai tratteggiata: il tratteggio
  sembra una soglia e invece e solo una guida;
* l'ultimo valore e scritto accanto al suo punto, cosi il numero che
  conta non si legge dall'asse;
* ogni punto ha un `<title>`, che il browser mostra come suggerimento al
  passaggio del mouse: niente JavaScript, ma i valori si leggono tutti.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape


@dataclass(frozen=True, slots=True)
class Point:
    """Un punto della serie storica."""

    at: datetime
    minimum: float
    reference: float


def parse_points(rows) -> list[Point]:
    """Converte le righe di `queries.history` in punti ordinati nel tempo."""
    points: list[Point] = []
    for row in rows:
        try:
            at = datetime.fromisoformat(row["taken_at"])
        except (TypeError, ValueError):
            continue
        points.append(
            Point(
                at=at,
                minimum=float(row["min_price_cents"]),
                reference=float(row["reference_price_cents"]),
            )
        )
    points.sort(key=lambda point: point.at)
    return points


def _money(cents: float) -> str:
    """Prezzo compatto per le etichette: sopra i 100 euro spariscono i decimali."""
    if cents >= 10000:
        return f"{cents / 100:,.0f}".replace(",", " ")
    return f"{cents / 100:.2f}"


def _grid(
    *,
    low: float,
    span: float,
    y_of,
    pad_left: int,
    right: int,
    unit: str = "",
    money: bool = True,
    lines: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0),
) -> list[str]:
    """Griglia orizzontale con le etichette di valore sul bordo sinistro.

    Per le serie non monetarie i decimali seguono l'ampiezza della scala:
    un indice che si muove fra 99.6 e 100.2 arrotondato all'intero
    mostrerebbe cinque volte "100".
    """
    step = span * (lines[1] - lines[0]) if len(lines) > 1 else span
    decimals = 0 if step >= 1 else (1 if step >= 0.1 else 2)
    parts: list[str] = []
    for fraction in lines:
        value = low + span * fraction
        y = y_of(value)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{right}" '
            f'y2="{y:.1f}" class="chart-grid" />'
        )
        text = _money(value) if money else f"{value:.{decimals}f}{unit}"
        parts.append(
            f'<text x="{pad_left - 10}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'class="chart-label">{escape(text)}</text>'
        )
    return parts


def _x_axis(labels: list[tuple[float, str]], y: float) -> list[str]:
    """Tre date al massimo: inizio, meta, fine. Oltre, si accavallano.

    La data centrale cade dove cade il punto di mezzo, che su un asse
    temporale puo finire attaccato a un estremo: in quel caso si omette.
    """
    if len(labels) == 3:
        first_x, middle_x, last_x = (x for x, _ in labels)
        if middle_x - first_x < 110 or last_x - middle_x < 110:
            labels = [labels[0], labels[2]]
    parts: list[str] = []
    for index, (x, text) in enumerate(labels):
        if index == 0:
            anchor = "start"
        elif index == len(labels) - 1:
            anchor = "end"
        else:
            anchor = "middle"
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
            f'class="chart-label">{escape(text)}</text>'
        )
    return parts


def _empty_chart(width: int, height: int, message: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{escape(message)}" class="chart">'
        f'<line x1="16" y1="{height - 26}" x2="{width - 16}" y2="{height - 26}" '
        f'class="chart-grid" />'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
        f'class="chart-label">{escape(message)}</text>'
        "</svg>"
    )


def price_chart_svg(
    points: list[Point],
    *,
    width: int = 760,
    height: int = 280,
    label: str = "Andamento del prezzo",
) -> str:
    """Grafico a due serie: prezzo minimo e prezzo di riferimento.

    Il minimo e la serie che si guarda, percio e pieno sotto la linea; il
    riferimento resta una traccia tratteggiata di confronto.

    Con un solo punto disegna un indicatore invece di una linea: due
    rilevazioni sono il minimo per parlare di andamento.
    """
    if not points:
        return _empty_chart(width, height, "Nessuna rilevazione nel periodo")

    pad_left, pad_right, pad_top, pad_bottom = 58, 62, 20, 30
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    right = width - pad_right

    values = [point.minimum for point in points] + [
        point.reference for point in points
    ]
    low, high = min(values), max(values)
    if high == low:
        # Serie piatta: si allarga la scala per non schiacciare la linea sul bordo.
        high = low + max(low * 0.1, 1.0)
    span = high - low
    low -= span * 0.12
    high += span * 0.12
    span = high - low

    first_ts = points[0].at.timestamp()
    last_ts = points[-1].at.timestamp()
    time_span = max(last_ts - first_ts, 1.0)

    def x_of(point: Point) -> float:
        if len(points) == 1:
            return pad_left + plot_w / 2
        return pad_left + (point.at.timestamp() - first_ts) / time_span * plot_w

    def y_of(value: float) -> float:
        return pad_top + plot_h - (value - low) / span * plot_h

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{escape(label)}" class="chart">',
        f"<title>{escape(label)}</title>",
    ]
    parts += _grid(low=low, span=span, y_of=y_of, pad_left=pad_left, right=right)

    reference_line = " ".join(
        f"{x_of(point):.1f},{y_of(point.reference):.1f}" for point in points
    )
    minimum_line = " ".join(
        f"{x_of(point):.1f},{y_of(point.minimum):.1f}" for point in points
    )

    if len(points) == 1:
        point = points[0]
        parts.append(
            f'<circle cx="{x_of(point):.1f}" cy="{y_of(point.reference):.1f}" '
            f'r="4" class="chart-reference-dot" />'
        )
        parts.append(
            f'<circle cx="{x_of(point):.1f}" cy="{y_of(point.minimum):.1f}" '
            f'r="5" class="chart-minimum-dot" />'
        )
    else:
        # Area sotto il minimo: da il livello a colpo d'occhio, prima della forma.
        base = pad_top + plot_h
        parts.append(
            f'<polygon class="chart-area" points="{x_of(points[0]):.1f},{base:.1f} '
            f'{minimum_line} {x_of(points[-1]):.1f},{base:.1f}" />'
        )
        parts.append(
            f'<polyline points="{reference_line}" class="chart-reference" />'
        )
        parts.append(
            f'<polyline points="{minimum_line}" class="chart-minimum" pathLength="1" />'
        )
        last = points[-1]
        parts.append(
            f'<circle cx="{x_of(last):.1f}" cy="{y_of(last.minimum):.1f}" '
            f'r="4.5" class="chart-minimum-dot" />'
        )
        # Etichetta diretta dell'ultimo valore: il numero che conta non si
        # legge piu dall'asse.
        label_y = min(max(y_of(last.minimum) + 4, pad_top + 10), base)
        parts.append(
            f'<text x="{x_of(last) + 10:.1f}" y="{label_y:.1f}" '
            f'class="chart-value">{_money(last.minimum)}</text>'
        )

    # Zone sensibili: una fascia per punto, con i valori nel suggerimento.
    step = plot_w / max(len(points) - 1, 1)
    for point in points:
        x = x_of(point)
        parts.append(
            f'<rect class="chart-hit" x="{max(x - step / 2, pad_left):.1f}" '
            f'y="{pad_top}" width="{min(step, plot_w):.1f}" height="{plot_h}">'
            f"<title>{point.at.strftime('%d/%m %H:%M')} - minimo "
            f"{_money(point.minimum)} EUR, riferimento "
            f"{_money(point.reference)} EUR</title></rect>"
        )

    axis = [(float(pad_left), points[0].at.strftime("%d/%m %H:%M"))]
    if len(points) > 2:
        middle = points[len(points) // 2]
        axis.append((x_of(middle), middle.at.strftime("%d/%m")))
    if len(points) > 1:
        axis.append((float(right), points[-1].at.strftime("%d/%m %H:%M")))
    parts += _x_axis(axis, height - 10)

    parts.append("</svg>")
    return "".join(parts)


def portfolio_chart_svg(
    series: list[tuple[str, float]],
    *,
    cost_cents: float | None = None,
    width: int = 760,
    height: int = 260,
) -> str:
    """Valore della collezione nel tempo, con il costo come linea di confronto.

    La linea del costo e ferma: e quanto hai speso in tutto per cio che
    possiedi oggi, non una ricostruzione storica degli acquisti.
    """
    if len(series) < 2:
        return _empty_chart(
            width,
            height,
            "Servono almeno due giorni di rilevazioni"
            if series
            else "Nessuna rilevazione sulle carte possedute",
        )

    pad_left, pad_right, pad_top, pad_bottom = 64, 70, 20, 30
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    right = width - pad_right

    values = [value for _, value in series]
    if cost_cents:
        values = values + [cost_cents]
    low, high = min(values), max(values)
    if high == low:
        high = low + max(low * 0.1, 1.0)
    span = high - low
    low -= span * 0.12
    high += span * 0.12
    span = high - low

    def x_of(index: int) -> float:
        return pad_left + index / (len(series) - 1) * plot_w

    def y_of(value: float) -> float:
        return pad_top + plot_h - (value - low) / span * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="Valore della collezione nel tempo" class="chart">',
        "<title>Valore della collezione nel tempo</title>",
    ]
    parts += _grid(low=low, span=span, y_of=y_of, pad_left=pad_left, right=right)

    line = " ".join(
        f"{x_of(index):.1f},{y_of(value):.1f}"
        for index, (_, value) in enumerate(series)
    )
    base = pad_top + plot_h
    parts.append(
        f'<polygon class="chart-area" points="{pad_left:.1f},{base:.1f} '
        f'{line} {right:.1f},{base:.1f}" />'
    )

    if cost_cents:
        y = y_of(cost_cents)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{right}" '
            f'y2="{y:.1f}" class="chart-reference" />'
        )
        parts.append(
            f'<text x="{right + 8:.1f}" y="{y + 3.5:.1f}" '
            f'class="chart-label">costo</text>'
        )

    parts.append(f'<polyline points="{line}" class="chart-minimum" pathLength="1" />')
    parts.append(
        f'<circle cx="{x_of(len(series) - 1):.1f}" '
        f'cy="{y_of(series[-1][1]):.1f}" r="4.5" class="chart-minimum-dot" />'
    )
    last_y = min(max(y_of(series[-1][1]) - 10, pad_top + 10), base)
    parts.append(
        f'<text x="{right + 8:.1f}" y="{last_y:.1f}" '
        f'class="chart-value">{_money(series[-1][1])}</text>'
    )

    step = plot_w / max(len(series) - 1, 1)
    for index, (day, value) in enumerate(series):
        x = x_of(index)
        parts.append(
            f'<rect class="chart-hit" x="{max(x - step / 2, pad_left):.1f}" '
            f'y="{pad_top}" width="{min(step, plot_w):.1f}" height="{plot_h}">'
            f"<title>{escape(_day_label(day))} - {_money(value)} EUR"
            f"</title></rect>"
        )

    axis = [(float(pad_left), _day_label(series[0][0]))]
    if len(series) > 2:
        middle = len(series) // 2
        axis.append((x_of(middle), _day_label(series[middle][0])))
    axis.append((float(right), _day_label(series[-1][0])))
    parts += _x_axis(axis, height - 10)

    parts.append("</svg>")
    return "".join(parts)


def lines_chart_svg(
    series_by_name: dict[str, list[tuple[str, float]]],
    *,
    baseline: float | None = None,
    width: int = 760,
    height: int = 280,
    unit: str = "",
    label: str = "Confronto fra serie",
) -> str:
    """Piu serie temporali sullo stesso grafico, una per nome.

    Le serie possono coprire giorni diversi: l'asse orizzontale e l'unione di
    tutti i giorni presenti, cosi due set che non partono insieme restano
    allineati nel tempo invece che a inizio linea.

    Ogni linea porta il proprio nome all'estremita: il colore da solo non
    deve bastare a riconoscere una serie.
    """
    usable = {
        name: points for name, points in series_by_name.items() if len(points) >= 2
    }
    if not usable:
        return _empty_chart(width, height, "Storico insufficiente")

    all_days = sorted({day for points in usable.values() for day, _ in points})
    positions = {day: index for index, day in enumerate(all_days)}

    # Lo spazio a destra serve ai nomi delle serie: con una sola serie il
    # titolo del riquadro la nomina gia, e il nome non si disegna.
    named = len(usable) > 1
    pad_left, pad_top, pad_bottom = 58, 20, 30
    pad_right = 78 if named else 56
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    right = width - pad_right

    values = [value for points in usable.values() for _, value in points]
    if baseline is not None:
        values = values + [baseline]
    low, high = min(values), max(values)
    if high == low:
        high = low + max(abs(low) * 0.05, 1.0)
    span = high - low
    low -= span * 0.12
    high += span * 0.12
    span = high - low

    def x_of(day: str) -> float:
        if len(all_days) == 1:
            return pad_left + plot_w / 2
        return pad_left + positions[day] / (len(all_days) - 1) * plot_w

    def y_of(value: float) -> float:
        return pad_top + plot_h - (value - low) / span * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{escape(label)}" class="chart">',
        f"<title>{escape(label)}</title>",
    ]
    parts += _grid(
        low=low,
        span=span,
        y_of=y_of,
        pad_left=pad_left,
        right=right,
        unit=unit,
        money=False,
    )

    if baseline is not None:
        y = y_of(baseline)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{right}" '
            f'y2="{y:.1f}" class="chart-reference" />'
        )

    # Le etichette alle estremita vengono distanziate in verticale: due set
    # fermi allo stesso valore altrimenti scriverebbero il nome uno
    # sull'altro.
    min_gap = 13.0
    ends = sorted(
        (y_of(points[-1][1]), name) for name, points in usable.items()
    )
    label_y: dict[str, float] = {}
    previous = None
    for y, name in ends:
        if previous is not None and y - previous < min_gap:
            y = previous + min_gap
        label_y[name] = y
        previous = y
    # Se la pila sfora il fondo del grafico, la si risale tutta insieme.
    overflow = (previous or 0) - (pad_top + plot_h)
    if overflow > 0:
        label_y = {name: y - overflow for name, y in label_y.items()}

    # Le tinte sono assegnate in ordine alfabetico del nome: un set che
    # esce dal filtro non ricolora gli altri.
    for index, (name, points) in enumerate(sorted(usable.items())):
        slot = index % 4
        line = " ".join(
            f"{x_of(day):.1f},{y_of(value):.1f}" for day, value in points
        )
        parts.append(
            f'<polyline points="{line}" class="chart-series series-{slot}" '
            f'pathLength="1"><title>{escape(name)}</title></polyline>'
        )
        last_day, last_value = points[-1]
        parts.append(
            f'<circle cx="{x_of(last_day):.1f}" cy="{y_of(last_value):.1f}" '
            f'r="3.5" fill="var(--chart-{slot + 1})" />'
        )
        if named:
            parts.append(
                f'<text x="{x_of(last_day) + 9:.1f}" '
                f'y="{label_y[name] + 3.5:.1f}" '
                f'class="chart-series-label label-{slot}">{escape(name.upper())}</text>'
            )

    axis = [(float(pad_left), _day_label(all_days[0]))]
    if len(all_days) > 2:
        middle = all_days[len(all_days) // 2]
        axis.append((x_of(middle), _day_label(middle)))
    axis.append((float(right), _day_label(all_days[-1])))
    parts += _x_axis(axis, height - 10)

    parts.append("</svg>")
    return "".join(parts)


def _day_label(day: str) -> str:
    try:
        return datetime.fromisoformat(day).strftime("%d/%m")
    except ValueError:
        return day


def sparkline_svg(values: list[float], *, width: int = 110, height: int = 30) -> str:
    """Micrografico per le righe di tabella.

    Il colore dice il verso: verde se il prezzo e salito fra il primo e
    l'ultimo punto, rosso se e scceso. E l'unica cosa leggibile in trenta
    pixel di altezza, e la variazione esatta sta nel suggerimento.
    """
    clean = [value for value in values if value is not None]
    if len(clean) < 2:
        return ""

    low, high = min(clean), max(clean)
    if high == low:
        high = low + 1.0
    inset = 3.0
    usable_h = height - inset * 2
    step = (width - 2) / (len(clean) - 1)

    def y_of(value: float) -> float:
        return inset + usable_h - (value - low) / (high - low) * usable_h

    points = " ".join(
        f"{1 + index * step:.1f},{y_of(value):.1f}"
        for index, value in enumerate(clean)
    )
    direction = "up" if clean[-1] >= clean[0] else "down"
    change = (clean[-1] - clean[0]) / clean[0] * 100 if clean[0] else 0.0

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="Andamento recente: {change:+.0f} per cento" '
        f'class="sparkline"><title>{change:+.0f}% nel periodo</title>'
        f'<polyline points="{points}" class="spark-line {direction}" />'
        f'<circle cx="{1 + (len(clean) - 1) * step:.1f}" '
        f'cy="{y_of(clean[-1]):.1f}" r="2.4" class="spark-dot {direction}" />'
        "</svg>"
    )
