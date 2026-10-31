"""Altair charts for the "Network outcome" and "Funders" dashboard tabs.

Every function returns a chart already passed through `chart_theme`, sized by height only
(width follows the container). Colours come from `dashboard_style`; a bank keeps its colour
across charts via `funder_colour`.
"""

from __future__ import annotations

from datetime import date, timedelta

import altair as alt
import pandas as pd

from .dashboard_style import AQUA, AXIS, BLUE, INK_2, ORANGE, SURFACE, chart_theme, money

BUYER_SOURCE = "Buyer's own cash"
OTHER_BANKS = "Other banks"
SERIES_OF = "Via OpenFloat"
SERIES_FACTORING = "Own factoring"
# "~s" renders zero as "0k"/"0M"; print a bare 0 instead
_ZERO_LABEL = "datum.value == 0 ? '0' : datum.label"


# --- colour mapping -----------------------------------------------------------


def _source_label(sc, funder_id: str) -> str:
    """Series name for a funder: the first funder by name; with two funders, the second by name too;
    with three or more, everyone after the first is folded into "Other banks"."""
    ids = [f.id for f in sc.funders]
    keep = ids[:2] if len(ids) <= 2 else ids[:1]
    return funder_id if funder_id in keep else OTHER_BANKS


def funder_colour(sc) -> dict[str, str]:
    """Stable series -> colour mapping: buyer BLUE, then funders in `sc.funders` order ORANGE, AQUA.

    With more than two funders, the second slot becomes "Other banks" (AQUA) so no chart
    ever needs a fourth categorical colour.
    """
    out = {BUYER_SOURCE: BLUE}
    ids = [f.id for f in sc.funders]
    if ids:
        out[ids[0]] = ORANGE
    if len(ids) == 2:
        out[ids[1]] = AQUA
    elif len(ids) > 2:
        out[OTHER_BANKS] = AQUA
    return out


def _colour_scale(mapping: dict[str, str], present: list[str]) -> alt.Scale:
    domain = [k for k in mapping if k in present]
    return alt.Scale(domain=domain, range=[mapping[k] for k in domain])


# --- 1. financing cost per invoice ------------------------------------------------


def cost_scatter(sc) -> alt.LayerChart:
    """Cost of each early-paid invoice as % of face, via OpenFloat vs the supplier's own factor."""
    rows = []
    for s in sc.suppliers:
        apr = s.profile.factoring_apr
        for r in s.receivables.values():
            inv = r.invoice
            if not (r.received and r.received_on and r.received_on < inv.due_date):
                continue
            days = (inv.due_date - r.received_on).days
            of_cost = inv.amount - r.received
            fac_cost = inv.amount * apr * days / 365
            common = {"Invoice": inv.invoice_id, "Supplier": s.id, "Days": days, "Face": money(inv.amount)}
            rows.append({**common, "Series": SERIES_OF, "Cost": of_cost / inv.amount, "Amount": money(of_cost)})
            rows.append({**common, "Series": SERIES_FACTORING, "Cost": fac_cost / inv.amount,
                         "Amount": money(fac_cost)})
    df = pd.DataFrame(rows, columns=["Invoice", "Supplier", "Days", "Face", "Series", "Cost", "Amount"])
    color = alt.Color("Series:N", scale=alt.Scale(domain=[SERIES_OF, SERIES_FACTORING], range=[BLUE, ORANGE]),
                      legend=alt.Legend(symbolType="circle", symbolStrokeWidth=0))
    points = alt.Chart(df).mark_circle(size=45, opacity=0.8, stroke=SURFACE, strokeWidth=1).encode(
        x=alt.X("Days:Q", title="Days paid ahead of due date", scale=alt.Scale(zero=True, nice=True),
                axis=alt.Axis(grid=False, tickCount=8)),
        y=alt.Y("Cost:Q", title="Cost, % of face value", axis=alt.Axis(format=".1%", tickCount=5)),
        color=color,
        tooltip=[
            alt.Tooltip("Invoice:N"), alt.Tooltip("Supplier:N"), alt.Tooltip("Series:N", title="Route"),
            alt.Tooltip("Days:Q", title="Days early"), alt.Tooltip("Cost:Q", title="Cost %", format=".2%"),
            alt.Tooltip("Amount:N", title="Cost"), alt.Tooltip("Face:N", title="Face value"),
        ],
    )
    return chart_theme(points.properties(height=280))


# --- 2. early payments funded per week, by source ---------------------------------


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly_sources(sc) -> alt.Chart:
    """Stacked weekly face value paid early to suppliers, split by who put up the cash."""
    agg: dict[tuple[date, str], list[int]] = {}

    def add(day: date, source: str, amount: int) -> None:
        cell = agg.setdefault((_week_start(day), source), [0, 0])
        cell[0] += amount
        cell[1] += 1

    for p in sc.buyer.payables.values():
        if p.early_advance is not None and p.early_pay_date is not None:
            add(p.early_pay_date, BUYER_SOURCE, p.invoice.amount)
    for f in sc.funders:
        for o, u in f.book:
            add(o.pay_date, _source_label(sc, f.id), u.amount)

    colours = funder_colour(sc)
    order = {k: i for i, k in enumerate(colours)}
    rows = [{"Week": w, "Source": src, "Amount": v[0] / 100, "Invoices": v[1], "Order": order[src]}
            for (w, src), v in agg.items() if v[0] > 0]
    df = pd.DataFrame(rows, columns=["Week", "Source", "Amount", "Invoices", "Order"])
    df["Week"] = pd.to_datetime(df["Week"])
    ticks: list[str] = []
    if not df.empty:
        # every week in range, so a week with no early payments shows as a gap rather than vanishing
        ticks = [w.strftime("%d %b") for w in pd.date_range(df["Week"].min(), df["Week"].max(), freq="7D")]
        df = df.sort_values(["Week", "Order"])
        df["WeekLabel"] = df["Week"].dt.strftime("w/c %d %b %Y")
        df["Tick"] = df["Week"].dt.strftime("%d %b")
    else:
        df["WeekLabel"], df["Tick"] = [], []
    df = df.drop(columns="Week")  # x uses pre-formatted ordinal ticks

    present = list(df["Source"].unique())
    bars = alt.Chart(df).mark_bar(
        size=24, stroke=SURFACE, strokeWidth=2,
        cornerRadiusEnd=4,  # Vega-Lite rounds the end of the whole stack, not each segment
    ).encode(
        x=alt.X("Tick:O", title=None, sort=ticks, scale=alt.Scale(domain=ticks) if ticks else alt.Undefined,
                axis=alt.Axis(labelAngle=0, grid=False)),
        y=alt.Y("Amount:Q", title="Face value paid early", stack="zero",
                axis=alt.Axis(format="~s", tickCount=5, labelExpr=_ZERO_LABEL)),
        color=alt.Color("Source:N", scale=_colour_scale(colours, present),
                        legend=alt.Legend(symbolType="square", symbolStrokeWidth=0)),
        order=alt.Order("Order:Q", sort="ascending"),
        tooltip=[
            alt.Tooltip("WeekLabel:N", title="Week"), alt.Tooltip("Source:N"),
            alt.Tooltip("Amount:Q", title="Face value", format=",.0f"),
            alt.Tooltip("Invoices:Q"),
        ],
    )
    return chart_theme(bars.properties(height=280))


# --- 3. bank exposure over time ---------------------------------------------------


def exposure_chart(sc, funder_ids: list[str] | None = None) -> alt.LayerChart:
    """Daily outstanding face value per bank (funded, not yet repaid), against each bank's limit."""
    funders = [f for f in sc.funders if funder_ids is None or f.id in funder_ids]
    colours = funder_colour(sc)
    deals: dict[str, list] = {}
    limits: dict[str, int] = {}
    for f in funders:
        label = _source_label(sc, f.id)
        deals.setdefault(label, []).extend(f.book)
        limits[label] = limits.get(label, 0) + f.policy.limit
    series = [k for k in colours if k in deals]

    start = sc.config.start
    last = max((u.due_date for book in deals.values() for _, u in book), default=start)
    rows = []
    d = start
    while d <= last:
        for name in series:
            out = sum(u.amount for o, u in deals[name] if o.pay_date <= d < u.due_date)
            rows.append({"Day": d, "Bank": name, "Exposure": out / 100})
        d += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["Day", "Bank", "Exposure"])
    df["Day"] = pd.to_datetime(df["Day"])
    scale = _colour_scale(colours, series)
    legend = alt.Legend(symbolType="stroke", symbolStrokeWidth=2) if len(series) > 1 else None
    color = alt.Color("Bank:N", scale=scale, legend=legend)

    hover = alt.selection_point(fields=["Day"], nearest=True, on="pointerover", empty=False, clear="pointerout")
    base = alt.Chart(df).encode(
        x=alt.X("Day:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
        y=alt.Y("Exposure:Q", title="Outstanding face value", scale=alt.Scale(zero=True, nice=True),
                axis=alt.Axis(format="~s", tickCount=5, labelExpr=_ZERO_LABEL)),
    )
    lines = base.mark_line(strokeWidth=2, interpolate="step-after").encode(color=color)
    points = base.mark_point(size=60, filled=True, stroke=SURFACE, strokeWidth=2).encode(
        color=color, opacity=alt.condition(hover, alt.value(1), alt.value(0)))

    wide = df.pivot(index="Day", columns="Bank", values="Exposure").reset_index() if not df.empty \
        else pd.DataFrame({"Day": [pd.Timestamp(start)]})
    tip_cols = [c for c in series if c in wide.columns]
    # pivot columns may contain characters Vega-Lite treats as paths; rename to safe field names
    safe = {c: f"s{i}" for i, c in enumerate(tip_cols)}
    wide = wide.rename(columns=safe)
    rule = alt.Chart(wide).mark_rule(color=AXIS, strokeWidth=1).encode(
        x="Day:T",
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("Day:T", format="%d %b %Y")]
        + [alt.Tooltip(f"{safe[c]}:Q", title=c, format=",.0f") for c in tip_cols],
    ).add_params(hover)

    # limits: one thin rule per bank in its own colour; banks sharing a limit get interleaved dashes
    # so both colours stay visible, and a single shared label
    by_limit: dict[int, list[str]] = {}
    for k in series:
        by_limit.setdefault(limits[k], []).append(k)
    limit_layers = []
    for value, names in by_limit.items():
        n = len(names)
        for idx, k in enumerate(names):
            dash = {} if n == 1 else {"strokeDash": [6, 6 * (n - 1)], "strokeDashOffset": 6 * idx}
            row = pd.DataFrame({"Limit": [value / 100], "Label": [f"{k} limit"], "Amount": [money(value)]})
            limit_layers.append(alt.Chart(row).mark_rule(color=colours[k], strokeWidth=1.5, opacity=0.5, **dash).encode(
                y="Limit:Q", tooltip=[alt.Tooltip("Label:N", title="Limit"), alt.Tooltip("Amount:N")]))
        label = pd.DataFrame({"Day": [pd.Timestamp(last)], "Limit": [value / 100], "Text": [", ".join(names) + " limit"]})
        limit_layers.append(alt.Chart(label).mark_text(
            align="right", baseline="bottom", dy=-4, color=INK_2, fontSize=11).encode(x="Day:T", y="Limit:Q", text="Text:N"))
    return chart_theme(alt.layer(*limit_layers, lines, points, rule).properties(height=280))
