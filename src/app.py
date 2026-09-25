"""USD/TRY crawl monitor — Streamlit UI.

Run from the repo root:  streamlit run src/app.py
"""
from __future__ import annotations

import logging
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):  # `streamlit run src/app.py` executes this file as a script
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from src import data, model  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # optional locally; Streamlit Cloud injects secrets as env vars
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CFG = model.load_config()
TTL_S = int(CFG.cache_ttl_hours * 3600)

# Reference categorical palette, slots 1-4 in fixed order (validated for adjacent line pairs,
# light and dark). Actual closes wear the ink colour; the band is a neutral fill.
PALETTE = {
    "light": {"ink": "#0b0b0b", "muted": "#52514e", "band": "rgba(82,81,78,0.13)",
              "updated": "#2a78d6", "central": "#eb6834", "base": "#1baf7a", "linear": "#eda100"},
    "dark": {"ink": "#ffffff", "muted": "#c3c2b7", "band": "rgba(195,194,183,0.16)",
             "updated": "#3987e5", "central": "#d95926", "base": "#199e70", "linear": "#c98500"},
}


@st.cache_data(ttl=TTL_S, show_spinner="Fetching USD/TRY…")
def get_fx(start: date, end: date, api_key: str | None) -> tuple[pd.Series, data.Source]:
    return data.load_usdtry(start, end, api_key, series=CFG.evds_fx_series,
                            provider=CFG.frankfurter_provider)


@st.cache_data(ttl=TTL_S, show_spinner="Fetching CPI…")
def get_cpi(start: date, end: date, api_key: str | None) -> tuple[pd.Series | None, str]:
    return data.load_cpi(start, end, api_key, series=CFG.evds_cpi_series)


def api_key() -> str | None:
    """EVDS key from the environment (.env locally), else Streamlit secrets (Community Cloud)."""
    key = data.get_api_key()
    if key:
        return key
    try:
        return str(st.secrets.get("EVDS_API_KEY", "")).strip() or None
    except Exception:  # no secrets.toml at all — Streamlit raises its own not-found error
        return None


def colors() -> dict[str, str]:
    try:
        kind = st.context.theme.type or "light"
    except AttributeError:
        kind = "light"
    return PALETTE.get(kind, PALETTE["light"])


def pct(x: float, digits: int = 2) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:.{digits}f}%"


# --- chart --------------------------------------------------------------------------------

def path_chart(closes: pd.Series, paths: pd.DataFrame, horizon_end: pd.Timestamp,
               latest_date: pd.Timestamp) -> go.Figure:
    c = colors()
    p = paths.loc[:horizon_end]
    actual = closes.loc[CFG.chart_start:]
    fig = go.Figure()
    band_name = f"Band {CFG.band_low * 100:.2f}–{CFG.band_high * 100:.2f}%/mo"
    fig.add_trace(go.Scatter(x=p.index, y=p["band_high"], name=band_name, legendgroup="band",
                             showlegend=False, line=dict(color=c["muted"], width=1, dash="dot"),
                             hovertemplate="%{y:.3f}"))
    fig.add_trace(go.Scatter(x=p.index, y=p["band_low"], name=band_name, legendgroup="band",
                             fill="tonexty", fillcolor=c["band"],
                             line=dict(color=c["muted"], width=1, dash="dot"),
                             hovertemplate="%{y:.3f}"))
    lines = [
        ("base", f"Base {CFG.base * 100:.2f}%/mo", "dash"),
        ("central", f"Central {CFG.central * 100:.2f}%/mo", "dash"),
        ("linear", f"Linear +{CFG.linear_per_month:.3f} TRY/mo", "dot"),
    ]
    for key, name, dash in lines:
        fig.add_trace(go.Scatter(x=p.index, y=p[key], name=name,
                                 line=dict(color=c[key], width=2, dash=dash),
                                 hovertemplate="%{y:.3f}"))
    upd = p["updated"].dropna()
    fig.add_trace(go.Scatter(x=upd.index, y=upd, name="Updated (from latest)",
                             line=dict(color=c["updated"], width=2), hovertemplate="%{y:.3f}"))
    fig.add_trace(go.Scatter(x=actual.index, y=actual, name="USD/TRY actual",
                             line=dict(color=c["ink"], width=2), hovertemplate="%{y:.4f}"))
    fig.add_trace(go.Scatter(x=[CFG.anchor_date], y=[CFG.anchor_rate], name="Anchor",
                             mode="markers", showlegend=False,
                             marker=dict(size=9, color=c["ink"], line=dict(width=2, color=c["band"])),
                             hovertemplate=f"Anchor {CFG.anchor_rate:.2f}<extra></extra>"))
    fig.add_vline(x=latest_date, line=dict(color=c["muted"], width=1, dash="dot"))
    fig.add_annotation(x=latest_date, y=0, yref="paper", text=f" latest {latest_date:%d %b}",
                       showarrow=False, xanchor="left", yanchor="bottom",
                       font=dict(color=c["muted"], size=11))
    fig.update_layout(
        height=520, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
        yaxis=dict(title="TRY per USD", tickformat=".1f"),
        xaxis=dict(range=[CFG.chart_start, horizon_end], showspikes=True, spikemode="across",
                   spikethickness=1, spikedash="dot", spikecolor=c["muted"]),
    )
    return fig


def crawl_chart(crawl: pd.DataFrame) -> go.Figure:
    c = colors()
    d = crawl.dropna(subset=["fx_mom_pct"]).iloc[::-1].tail(12)
    months = d.index.strftime("%Y-%m")
    fx_opacity = [0.45 if part else 1.0 for part in d["partial"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=months, y=d["fx_mom_pct"], name="USD/TRY monthly avg, m/m",
                         marker=dict(color=c["updated"], opacity=fx_opacity),
                         hovertemplate="%{y:.2f}%"))
    fig.add_trace(go.Bar(x=months, y=d["cpi_mom_pct"], name="CPI, m/m",
                         marker=dict(color=c["central"]), hovertemplate="%{y:.2f}%"))
    for m, row in zip(months, d.itertuples()):
        if row.flag:
            fig.add_annotation(x=m, y=max(row.fx_mom_pct, row.cpi_mom_pct), text="⚠ FX > CPI",
                               showarrow=False, yshift=12, font=dict(size=11, color=c["ink"]))
    fig.update_layout(
        height=360, barmode="group", bargap=0.3, bargroupgap=0.08, barcornerradius=4,
        margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
        yaxis=dict(title="% change vs previous month", ticksuffix="%"),
    )
    return fig


# --- page ---------------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="USD/TRY crawl monitor", page_icon="📈", layout="wide")
    key = api_key()
    today = date.today()

    with st.sidebar:
        st.header("Controls")
        updated_pct = st.slider(
            "Updated path pace (%/month)", min_value=CFG.updated_min * 100,
            max_value=CFG.updated_max * 100, value=CFG.updated_default * 100,
            step=CFG.updated_step * 100, format="%.2f%%",
            help="Compounds monthly from the latest close.")
        if st.button("Refresh data", help=f"Data is cached for {CFG.cache_ttl_hours:g}h"):
            st.cache_data.clear()
            st.rerun()

    try:
        closes, source = get_fx(CFG.history_start.date(), today, key)
    except data.DataError as exc:
        st.error(f"Could not load USD/TRY: {exc}")
        st.stop()
    cpi, cpi_source = get_cpi(CFG.history_start.date(), today, key)

    updated = updated_pct / 100
    latest_date, latest_rate = closes.index[-1], float(closes.iloc[-1])
    horizon_max = max(CFG.horizons.values())
    paths = model.build_paths(CFG, horizon_max, latest_date, latest_rate, updated)
    pace = model.pace_table(closes, CFG).set_index("window")
    trend = model.trend_windows(closes, CFG)
    crawl = model.crawl_check(closes, cpi)

    with st.sidebar:
        st.divider()
        st.caption(f"**USD/TRY:** {source.detail}")
        if source.fallback_reason:
            st.caption(f"Fallback because: {source.fallback_reason}")
        st.caption(f"**CPI:** {cpi_source}")
        st.caption(f"Latest close {latest_date:%Y-%m-%d}. Cache TTL {CFG.cache_ttl_hours:g}h.")

    st.title("USD/TRY crawl monitor")
    if source.fallback_reason:
        st.info(f"Using {source.detail}, because {source.fallback_reason}. "
                "TCMB mid vs EVDS buying rate differ by ~0.09%.", icon="ℹ️")

    central_today = float(paths.loc[latest_date, "central"]) if latest_date in paths.index else math.nan
    newest = trend.iloc[0]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"Latest close ({latest_date:%d %b})", f"{latest_rate:.4f}")
    k2.metric("Pace since anchor", pct(pace.loc["Since anchor", "pct_per_month"]) + "/mo",
              f"{pace.loc['Since anchor', 'vs_central_pp']:+.2f} pp vs central",
              delta_color="inverse")
    k3.metric("vs central path today", pct((latest_rate / central_today - 1) * 100),
              f"central = {central_today:.3f}", delta_color="off")
    k4.metric(f"{CFG.regression_window_months}m trend slope",
              pct(newest["pct_per_month"]) + "/mo",
              None if pd.isna(newest["delta_pp"]) else f"{newest['delta_pp']:+.2f} pp vs prior window",
              delta_color="inverse")

    t_chart, t_pace, t_crawl, t_target = st.tabs(["Chart", "Pace", "Crawl vs CPI", "Target dates"])

    with t_chart:
        horizon_key = st.radio("Horizon", list(CFG.horizons), horizontal=True)
        horizon_end = CFG.horizons[horizon_key]
        st.plotly_chart(path_chart(closes, paths, horizon_end, latest_date), width="stretch")
        specs = model.path_specs(CFG, latest_date, latest_rate, updated)
        at_h = paths.loc[horizon_end]
        st.dataframe(
            pd.DataFrame({"Path": [s.label for s in specs], "Pace": [s.pace_label for s in specs],
                          f"Value on {horizon_end:%Y-%m-%d}": [at_h[s.key] for s in specs]}),
            hide_index=True, width="content",
            column_config={f"Value on {horizon_end:%Y-%m-%d}": st.column_config.NumberColumn(format="%.3f")})

    with t_pace:
        st.subheader("Pace, compounded %/month")
        st.dataframe(
            pace.reset_index(), hide_index=True, width="stretch",
            column_config={
                "window": "Window", "from": st.column_config.DateColumn("From"),
                "to": st.column_config.DateColumn("To"),
                "start": st.column_config.NumberColumn("Start", format="%.4f"),
                "end": st.column_config.NumberColumn("End", format="%.4f"),
                "days": st.column_config.NumberColumn("Days", format="%d"),
                "pct_per_month": st.column_config.NumberColumn("%/month", format="%.3f%%"),
                "vs_central_pp": st.column_config.NumberColumn("vs central (pp)", format="%+.3f"),
            })
        st.caption(f"%/month = (end/start)^(30.4375/days) − 1. Since anchor starts at your "
                   f"{CFG.anchor_rate:.2f} on {CFG.anchor_date:%Y-%m-%d}.")

        st.subheader(f"Rolling {CFG.regression_window_months}-month log-linear trend")
        if bool(newest["faster"]):
            st.warning(f"Trend getting faster: {newest['pct_per_month']:.2f}%/mo, "
                       f"{newest['delta_pp']:+.2f} pp vs the window ending "
                       f"{trend.iloc[1]['window_end']:%Y-%m-%d}.", icon="⚠️")
        tview = trend.assign(flag=trend["faster"].map({True: "▲ faster", False: ""}))
        st.dataframe(
            tview.drop(columns="faster"), hide_index=True, width="stretch",
            column_config={
                "window_start": st.column_config.DateColumn("Window start"),
                "window_end": st.column_config.DateColumn("Window end"),
                "n_obs": st.column_config.NumberColumn("Obs", format="%d"),
                "pct_per_month": st.column_config.NumberColumn("Slope %/month", format="%.3f%%"),
                "r2": st.column_config.NumberColumn("R²", format="%.3f"),
                "delta_pp": st.column_config.NumberColumn("Δ vs prior (pp)", format="%+.3f"),
                "flag": "Flag",
            })
        st.caption(f"OLS of ln(rate) on calendar days; windows step back "
                   f"{CFG.regression_step_months} month(s) from the latest close. Flag when the "
                   f"slope rises more than {CFG.faster_flag_pp:.2f} pp vs the prior window.")

    with t_crawl:
        if cpi is None:
            st.info(f"CPI unavailable: {cpi_source}. Showing USD/TRY only.", icon="ℹ️")
        flagged = crawl[crawl["flag"]]
        if not flagged.empty:
            st.warning("FX rose faster than CPI in: "
                       + ", ".join(flagged.index.strftime("%b %Y")) + ".", icon="⚠️")
        st.plotly_chart(crawl_chart(crawl), width="stretch")
        st.dataframe(
            crawl.drop(columns=["flag"]).rename_axis("month").reset_index(),
            hide_index=True, width="stretch",
            column_config={
                "month": st.column_config.DateColumn("Month", format="YYYY-MM"),
                "fx_avg": st.column_config.NumberColumn("USD/TRY avg", format="%.4f"),
                "fx_days": st.column_config.NumberColumn("Days", format="%d"),
                "fx_mom_pct": st.column_config.NumberColumn("FX m/m", format="%.2f%%"),
                "cpi_index": st.column_config.NumberColumn("CPI index", format="%.2f"),
                "cpi_mom_pct": st.column_config.NumberColumn("CPI m/m", format="%.2f%%"),
                "partial": st.column_config.CheckboxColumn("Partial"),
                "gap_pp": st.column_config.NumberColumn("FX − CPI (pp)", format="%+.2f"),
                "status": "Status",
            })
        st.caption("FX m/m = change in the monthly average of daily closes. A completed month "
                   "with FX m/m > CPI m/m means real depreciation: a regime-break warning.")

    with t_target:
        target = st.number_input("Target USD/TRY", min_value=1.0, step=0.5, format="%.2f",
                                 value=float(math.floor(latest_rate) + 2))
        tt = model.target_dates(CFG, target, closes, updated)
        st.dataframe(
            tt, hide_index=True, width="stretch",
            column_config={
                "path": "Path", "pace": "Pace",
                "reached": st.column_config.DateColumn("Reaches target"),
                "months_from_latest": st.column_config.NumberColumn("Months from latest close",
                                                                    format="%.1f"),
                "note": "Note",
            })
        st.caption("Closed form: compound days = 30.4375·ln(target/start)/ln(1+r); "
                   "linear days = 30.4375·(target−start)/0.556. Band and model paths start "
                   "at the anchor; Updated starts at the latest close.")


if __name__ == "__main__":
    main()
