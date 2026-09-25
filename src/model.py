"""Crawl model: projected paths, pace metrics, trend regression, crawl-vs-CPI, target dates.

Pure functions over pandas objects (no Streamlit, no network), so all of it is unit-tested.
Rates are TRY per USD. Paces are fractions per month (0.0145 = 1.45%/mo) unless a name
says *_pct. Paths compound monthly on calendar days:

    rate(t) = start_rate * (1 + r) ** (days / days_per_month)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
MIN_REGRESSION_OBS = 20          # ~1 month of business days
WINDOW_COVERAGE_SLACK_DAYS = 7   # a window whose first close is later than this is incomplete


@dataclass(frozen=True)
class Config:
    anchor_date: pd.Timestamp
    anchor_rate: float
    base: float
    central: float
    band_low: float
    band_high: float
    linear_per_month: float        # TRY per month
    updated_default: float
    updated_min: float
    updated_max: float
    updated_step: float
    days_per_month: float
    horizons: dict[str, pd.Timestamp]
    rolling_days: tuple[int, ...]
    regression_window_months: int
    regression_step_months: int
    regression_windows_shown: int
    faster_flag_pp: float
    history_start: pd.Timestamp
    chart_start: pd.Timestamp
    evds_fx_series: str
    evds_cpi_series: str
    frankfurter_provider: str | None
    cache_ttl_hours: float


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    p, u, pace, d = raw["paths"], raw["updated_path"], raw["pace"], raw["data"]
    return Config(
        anchor_date=pd.Timestamp(raw["anchor"]["date"]),
        anchor_rate=float(raw["anchor"]["rate"]),
        base=p["base_pct"] / 100,
        central=p["central_pct"] / 100,
        band_low=p["band_low_pct"] / 100,
        band_high=p["band_high_pct"] / 100,
        linear_per_month=float(p["linear_try_per_month"]),
        updated_default=u["default_pct"] / 100,
        updated_min=u["min_pct"] / 100,
        updated_max=u["max_pct"] / 100,
        updated_step=u["step_pct"] / 100,
        days_per_month=float(raw["days_per_month"]),
        horizons={k: pd.Timestamp(v) for k, v in raw["horizons"].items()},
        rolling_days=tuple(int(n) for n in pace["rolling_days"]),
        regression_window_months=int(pace["regression_window_months"]),
        regression_step_months=int(pace["regression_step_months"]),
        regression_windows_shown=int(pace["regression_windows_shown"]),
        faster_flag_pp=float(pace["faster_flag_pp"]),
        history_start=pd.Timestamp(d["history_start"]),
        chart_start=pd.Timestamp(d["chart_start"]),
        evds_fx_series=d["evds_fx_series"],
        evds_cpi_series=d["evds_cpi_series"],
        frankfurter_provider=d.get("frankfurter_provider") or None,
        cache_ttl_hours=float(d["cache_ttl_hours"]),
    )


# --- paths -------------------------------------------------------------------------------

def _days_since(start: pd.Timestamp, dates) -> np.ndarray:
    return (pd.DatetimeIndex(dates) - pd.Timestamp(start)).days.to_numpy(dtype=float)


def compound_path(start_rate: float, start_date, monthly: float, dates,
                  days_per_month: float = 30.4375) -> np.ndarray:
    return start_rate * (1.0 + monthly) ** (_days_since(start_date, dates) / days_per_month)


def linear_path(start_rate: float, start_date, per_month: float, dates,
                days_per_month: float = 30.4375) -> np.ndarray:
    return start_rate + per_month * _days_since(start_date, dates) / days_per_month


@dataclass(frozen=True)
class PathSpec:
    key: str
    label: str
    kind: str                  # "compound" | "linear"
    pace: float                # fraction/month for compound, TRY/month for linear
    start_date: pd.Timestamp
    start_rate: float

    @property
    def pace_label(self) -> str:
        if self.kind == "linear":
            return f"+{self.pace:.3f} TRY/mo"
        return f"{self.pace * 100:.2f}%/mo"


def path_specs(cfg: Config, latest_date, latest_rate: float, updated: float) -> list[PathSpec]:
    a_d, a_r = cfg.anchor_date, cfg.anchor_rate
    return [
        PathSpec("band_low", "Band low", "compound", cfg.band_low, a_d, a_r),
        PathSpec("base", "Base", "compound", cfg.base, a_d, a_r),
        PathSpec("central", "Central", "compound", cfg.central, a_d, a_r),
        PathSpec("band_high", "Band high", "compound", cfg.band_high, a_d, a_r),
        PathSpec("linear", "Linear", "linear", cfg.linear_per_month, a_d, a_r),
        PathSpec("updated", "Updated", "compound", updated,
                 pd.Timestamp(latest_date), float(latest_rate)),
    ]


def evaluate(spec: PathSpec, dates, days_per_month: float) -> np.ndarray:
    fn = linear_path if spec.kind == "linear" else compound_path
    return fn(spec.start_rate, spec.start_date, spec.pace, dates, days_per_month)


def build_paths(cfg: Config, end, latest_date, latest_rate: float, updated: float) -> pd.DataFrame:
    """Daily values of every path from the anchor to `end`. Updated is NaN before its start."""
    dates = pd.date_range(cfg.anchor_date, pd.Timestamp(end), freq="D")
    out = pd.DataFrame(index=dates)
    for spec in path_specs(cfg, latest_date, latest_rate, updated):
        values = evaluate(spec, dates, cfg.days_per_month)
        out[spec.key] = np.where(dates >= spec.start_date, values, np.nan)
    return out


# --- pace --------------------------------------------------------------------------------

def monthly_pace(start_rate: float, end_rate: float, days: float,
                 days_per_month: float = 30.4375) -> float:
    """Compound %/month (as a fraction) that takes start_rate to end_rate in `days`."""
    if days <= 0:
        return math.nan
    return (end_rate / start_rate) ** (days_per_month / days) - 1.0


def close_on_or_before(closes: pd.Series, when) -> tuple[pd.Timestamp, float] | None:
    s = closes.loc[: pd.Timestamp(when)]
    if s.empty:
        return None
    return s.index[-1], float(s.iloc[-1])


def pace_table(closes: pd.Series, cfg: Config) -> pd.DataFrame:
    """YTD, since anchor and rolling N-day paces, each ending at the latest close."""
    end_date, end_rate = closes.index[-1], float(closes.iloc[-1])
    starts: list[tuple[str, tuple[pd.Timestamp, float] | None]] = [
        ("YTD", close_on_or_before(closes, pd.Timestamp(end_date.year - 1, 12, 31))),
        ("Since anchor", (cfg.anchor_date, cfg.anchor_rate)),
    ]
    starts += [(f"Last {n}d", close_on_or_before(closes, end_date - pd.Timedelta(days=n)))
               for n in cfg.rolling_days]

    rows = []
    for label, start in starts:
        if start is None:
            rows.append({"window": label})
            continue
        s_date, s_rate = start
        days = (end_date - s_date).days
        pace = monthly_pace(s_rate, end_rate, days, cfg.days_per_month)
        rows.append({
            "window": label, "from": s_date, "start": s_rate, "to": end_date, "end": end_rate,
            "days": days, "pct_per_month": pace * 100,
            "vs_central_pp": (pace - cfg.central) * 100,
        })
    return pd.DataFrame(rows)


def loglinear_fit(closes: pd.Series, days_per_month: float = 30.4375) -> tuple[float, float, int]:
    """OLS of ln(rate) on calendar days. Returns (monthly pace as fraction, R², n)."""
    n = len(closes)
    if n < 3:
        return math.nan, math.nan, n
    x = (closes.index - closes.index[0]).days.to_numpy(dtype=float)
    y = np.log(closes.to_numpy(dtype=float))
    xc, yc = x - x.mean(), y - y.mean()
    sxx = float((xc ** 2).sum())
    if sxx == 0:
        return math.nan, math.nan, n
    slope = float((xc * yc).sum()) / sxx
    sst = float((yc ** 2).sum())
    ssr = float(((yc - slope * xc) ** 2).sum())
    r2 = 1.0 - ssr / sst if sst > 0 else math.nan
    return math.exp(slope * days_per_month) - 1.0, r2, n


def trend_windows(closes: pd.Series, cfg: Config) -> pd.DataFrame:
    """Rolling log-linear slope over `regression_window_months`, re-evaluated every
    `regression_step_months` back from the latest close. Newest window first.

    `faster` is True when a window's pace exceeds the window ending one step earlier
    by more than `faster_flag_pp` percentage points of %/month.
    """
    end = closes.index[-1]
    rows = []
    # One extra (older) window so the oldest shown row still has a comparison.
    for k in range(cfg.regression_windows_shown + 1):
        w_end = end - pd.DateOffset(months=k * cfg.regression_step_months)
        w_start = w_end - pd.DateOffset(months=cfg.regression_window_months)
        seg = closes.loc[(closes.index >= w_start) & (closes.index <= w_end)]
        complete = (len(seg) >= MIN_REGRESSION_OBS
                    and (seg.index[0] - w_start).days <= WINDOW_COVERAGE_SLACK_DAYS)
        pace, r2, n = loglinear_fit(seg, cfg.days_per_month) if complete else (math.nan, math.nan, len(seg))
        rows.append({"window_start": w_start, "window_end": w_end, "n_obs": n,
                     "pct_per_month": pace * 100, "r2": r2})
    df = pd.DataFrame(rows)
    df["delta_pp"] = df["pct_per_month"] - df["pct_per_month"].shift(-1)
    df["faster"] = df["delta_pp"] > cfg.faster_flag_pp
    return df.iloc[: cfg.regression_windows_shown].reset_index(drop=True)


# --- crawl vs CPI ------------------------------------------------------------------------

def _month_start(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return idx.to_period("M").to_timestamp()


def crawl_check(closes: pd.Series, cpi: pd.Series | None) -> pd.DataFrame:
    """Monthly USD/TRY average change vs CPI monthly change. Newest month first.

    `flag` marks completed months where FX rose faster than CPI (real depreciation,
    i.e. the crawl stopped delivering real appreciation → regime-break warning).
    """
    monthly = closes.resample("MS").agg(["mean", "count"])
    df = pd.DataFrame({"fx_avg": monthly["mean"], "fx_days": monthly["count"]})
    df["fx_mom_pct"] = (df["fx_avg"] / df["fx_avg"].shift(1) - 1) * 100

    if cpi is not None and not cpi.empty:
        c = cpi.copy()
        c.index = _month_start(pd.DatetimeIndex(c.index))
        c = c[~c.index.duplicated(keep="last")].sort_index()
        c = c.reindex(pd.date_range(c.index[0], c.index[-1], freq="MS"))
        df["cpi_index"] = c.reindex(df.index)
        df["cpi_mom_pct"] = ((c / c.shift(1) - 1) * 100).reindex(df.index)
    else:
        df["cpi_index"] = np.nan
        df["cpi_mom_pct"] = np.nan

    last = closes.index[-1]
    last_bday = last + pd.offsets.BMonthEnd(0)
    df["partial"] = (df.index == _month_start(pd.DatetimeIndex([last]))[0]) & (last < last_bday)
    df["gap_pp"] = df["fx_mom_pct"] - df["cpi_mom_pct"]
    known = df["fx_mom_pct"].notna() & df["cpi_mom_pct"].notna() & ~df["partial"]
    df["flag"] = known & (df["gap_pp"] > 0)

    def status(row) -> str:
        if pd.isna(row.fx_mom_pct):
            return "—"
        if row.partial:
            return "Month in progress"
        if pd.isna(row.cpi_mom_pct):
            return "CPI not available"
        return "FX > CPI — regime-break warning" if row.flag else "OK (FX ≤ CPI)"

    df["status"] = df.apply(status, axis=1)
    return df[df["fx_days"] > 0].iloc[::-1]


# --- target dates ------------------------------------------------------------------------

def days_to_target(spec: PathSpec, target: float, days_per_month: float = 30.4375) -> float:
    """Calendar days from the path start until the path equals `target` (may be negative)."""
    if spec.kind == "linear":
        if spec.pace == 0:
            return math.nan
        return days_per_month * (target - spec.start_rate) / spec.pace
    if target <= 0 or spec.pace <= -1 or spec.pace == 0:
        return math.nan
    return days_per_month * math.log(target / spec.start_rate) / math.log1p(spec.pace)


def target_dates(cfg: Config, target: float, closes: pd.Series, updated: float) -> pd.DataFrame:
    """Date each path first reaches `target`, plus the first actual close at or above it."""
    latest_date, latest_rate = closes.index[-1], float(closes.iloc[-1])
    rows = []
    hit = closes[closes >= target]
    rows.append({"path": "Actual closes", "pace": "—",
                 "reached": hit.index[0] if not hit.empty else pd.NaT,
                 "note": "already reached" if not hit.empty else "not yet"})
    for spec in path_specs(cfg, latest_date, latest_rate, updated):
        days = days_to_target(spec, target, cfg.days_per_month)
        if math.isnan(days):
            rows.append({"path": spec.label, "pace": spec.pace_label, "reached": pd.NaT,
                         "note": "never"})
            continue
        reached = spec.start_date + pd.Timedelta(days=math.ceil(days))
        note = "before path start" if days < 0 else (
            "on/before latest close" if reached <= latest_date else "")
        rows.append({"path": spec.label, "pace": spec.pace_label, "reached": reached, "note": note})
    df = pd.DataFrame(rows)
    df["months_from_latest"] = (df["reached"] - latest_date).dt.days / cfg.days_per_month
    return df
