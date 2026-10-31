"""Shared look for the dashboard: palette, chart theme, CSS and small formatters.

Colours are the reference data-viz palette (light mode): categorical slots in fixed order,
ink and chrome tokens, and reserved status colours that never stand in for a series.
"""

from __future__ import annotations

import altair as alt

# categorical slots, fixed order (first three validate all-pairs)
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
# ink and chrome
INK, INK_2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SURFACE, PAGE = "#fcfcfb", "#f9f9f7"
# status (reserved)
GOOD, CRITICAL = "#0ca30c", "#d03b3b"

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def money(cents: float, dp: int = 0) -> str:
    return f"{cents / 100:,.{dp}f}"


def chart_theme(chart: alt.Chart) -> alt.Chart:
    return (
        chart.configure(font=FONT, background=SURFACE, padding={"left": 4, "right": 12, "top": 8, "bottom": 4})
        .configure_view(stroke=None)
        .configure_axis(
            gridColor=GRID, gridWidth=1, domainColor=AXIS, tickColor=AXIS,
            labelColor=MUTED, titleColor=INK_2, labelFontSize=12, titleFontSize=12, titleFontWeight="normal",
        )
        .configure_legend(labelColor=INK_2, titleColor=INK_2, orient="top", title=None, symbolStrokeWidth=2,
                          labelFontSize=12)
    )


CSS = f"""
<style>
/* page */
.block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px; }}
[data-testid="stHeader"] {{ background: transparent; }}
h1, h2, h3, h4 {{ letter-spacing: -0.01em; }}

/* hero */
.of-hero {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 2rem;
           border-bottom: 1px solid {GRID}; padding-bottom: 1.1rem; margin-bottom: 0.6rem; }}
.of-hero h1 {{ font-size: 2.4rem; margin: 0; padding: 0; line-height: 1.1; }}
.of-hero .of-tag {{ color: {INK_2}; font-size: 1.02rem; margin-top: .35rem; max-width: 46rem; }}
.of-pills {{ display: flex; gap: .5rem; flex-wrap: wrap; justify-content: flex-end; }}
.of-pill {{ border: 1px solid {GRID}; background: {SURFACE}; border-radius: 999px; padding: .28rem .75rem;
           font-size: .82rem; color: {INK_2}; white-space: nowrap; }}

/* KPI cards: Streamlit metrics get a card */
[data-testid="stMetric"] {{ background: {SURFACE}; border: 1px solid rgba(11,11,11,0.08); border-radius: 12px;
                           padding: .85rem 1rem .75rem 1rem; }}
[data-testid="stMetricLabel"] p {{ color: {INK_2}; font-size: .85rem; }}
[data-testid="stMetricValue"] {{ font-size: 1.75rem; }}

/* stepper */
.of-steps {{ display: flex; gap: .4rem; margin: .4rem 0 1rem 0; }}
.of-step {{ flex: 1; border-top: 3px solid {GRID}; padding-top: .45rem; font-size: .8rem; color: {MUTED};
           line-height: 1.25; }}
.of-step b {{ display: block; font-size: .72rem; letter-spacing: .04em; text-transform: uppercase; margin-bottom: .1rem; }}
.of-step.done {{ border-top-color: {BLUE}; color: {INK_2}; }}
.of-step.current {{ border-top-color: {INK}; color: {INK}; font-weight: 600; }}

/* participant state cards */
.of-card {{ background: {SURFACE}; border: 1px solid rgba(11,11,11,0.08); border-radius: 12px; padding: .7rem .9rem;
           height: 100%; }}
.of-card .of-role {{ font-size: .72rem; letter-spacing: .05em; text-transform: uppercase; color: {MUTED}; }}
.of-card .of-name {{ font-weight: 600; color: {INK}; margin-bottom: .35rem; }}
.of-card .of-val {{ font-size: 1.25rem; color: {INK}; }}
.of-card .of-sub {{ font-size: .8rem; color: {INK_2}; }}
.of-card .of-bar {{ height: 6px; background: {GRID}; border-radius: 3px; margin-top: .45rem; overflow: hidden; }}
.of-card .of-bar > span {{ display: block; height: 100%; background: {BLUE}; border-radius: 3px; }}
.of-card .of-delta {{ font-size: .78rem; color: {INK_2}; margin-top: .25rem; }}

/* section labels */
.of-label {{ font-size: .78rem; letter-spacing: .05em; text-transform: uppercase; color: {MUTED};
            margin: 1.1rem 0 .35rem 0; }}

/* sidebar */
[data-testid="stSidebar"] .of-side-title {{ font-weight: 700; font-size: 1.05rem; margin-bottom: .1rem; }}
[data-testid="stSidebar"] .of-side-sub {{ color: {INK_2}; font-size: .82rem; margin-bottom: .8rem; }}
</style>
"""
