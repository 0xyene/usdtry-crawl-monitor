import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from src import model
from src.model import PathSpec

DPM = 30.4375


@pytest.fixture(scope="module")
def cfg():
    return model.load_config()


def business_closes(start, end, monthly, start_rate):
    """Weekday closes growing at `monthly` compound per month (calendar-day based)."""
    idx = pd.bdate_range(start, end)
    return pd.Series(model.compound_path(start_rate, idx[0], monthly, idx, DPM), index=idx)


# --- config ------------------------------------------------------------------------------

def test_config_matches_spec(cfg):
    assert cfg.anchor_date == pd.Timestamp("2026-05-29")
    assert cfg.anchor_rate == 45.70
    assert (cfg.base, cfg.central, cfg.band_low, cfg.band_high) == pytest.approx(
        (0.012, 0.0145, 0.011, 0.020), abs=1e-15)
    assert cfg.linear_per_month == 0.556
    assert cfg.updated_default == pytest.approx(0.015, abs=1e-15)
    assert cfg.days_per_month == 30.4375
    assert list(cfg.horizons) == ["Dec-26", "Jun-27", "Nov-27"]


# --- paths -------------------------------------------------------------------------------

def test_compound_path_is_anchor_at_start_and_exact_after_48_months():
    # 1461 days = 4 years = exactly 48 * 30.4375
    dates = pd.to_datetime(["2026-05-29", "2030-05-29"])
    v = model.compound_path(45.70, "2026-05-29", 0.0145, dates, DPM)
    assert v[0] == 45.70
    assert v[1] == pytest.approx(45.70 * 1.0145 ** 48, rel=1e-12)


def test_central_path_hand_worked_dec_2026():
    # 29 May → 31 Dec 2026 = 2 + 30 + 31 + 31 + 30 + 31 + 30 + 31 = 216 days
    # 45.70 * 1.0145 ** (216 / 30.4375) = 50.6155 (worked separately)
    v = model.compound_path(45.70, "2026-05-29", 0.0145, pd.to_datetime(["2026-12-31"]), DPM)
    assert v[0] == pytest.approx(50.6155, abs=1e-4)


def test_linear_path_adds_0556_per_month():
    dates = pd.to_datetime(["2026-05-29", "2030-05-29"])  # 0 and 48 months
    v = model.linear_path(45.70, "2026-05-29", 0.556, dates, DPM)
    assert v[0] == 45.70
    assert v[1] == pytest.approx(45.70 + 0.556 * 48, abs=1e-9)


def test_build_paths_band_ordering_and_updated_start(cfg):
    latest = pd.Timestamp("2026-09-25")
    paths = model.build_paths(cfg, "2027-11-30", latest, 48.811, 0.015)
    assert paths.index[0] == cfg.anchor_date and paths.index[-1] == pd.Timestamp("2027-11-30")
    after = paths.loc["2026-06-01":]
    assert (after["band_low"] < after["base"]).all()
    assert (after["base"] < after["central"]).all()
    assert (after["central"] < after["band_high"]).all()
    assert paths.loc[: latest - pd.Timedelta(days=1), "updated"].isna().all()
    assert paths.loc[latest, "updated"] == pytest.approx(48.811)


# --- pace --------------------------------------------------------------------------------

def test_monthly_pace_inverts_compound_path():
    for r in (0.011, 0.0145, 0.02):
        for days in (30, 91, 216):
            end = model.compound_path(45.70, "2026-05-29", r,
                                      [pd.Timestamp("2026-05-29") + pd.Timedelta(days=days)], DPM)[0]
            assert model.monthly_pace(45.70, end, days, DPM) == pytest.approx(r, abs=1e-12)


def test_pace_table_hand_worked(cfg):
    idx = pd.to_datetime(["2025-12-30", "2026-08-26", "2026-09-25"])  # 31 Dec deliberately missing
    closes = pd.Series([43.00, 48.20, 48.811], index=idx)
    t = model.pace_table(closes, cfg).set_index("window")

    ytd = t.loc["YTD"]
    assert ytd["from"] == pd.Timestamp("2025-12-30")  # last close on/before 31 Dec
    assert ytd["days"] == 269
    assert ytd["pct_per_month"] == pytest.approx(((48.811 / 43.0) ** (DPM / 269) - 1) * 100)

    anchor = t.loc["Since anchor"]
    assert anchor["start"] == 45.70 and anchor["days"] == 119
    assert anchor["pct_per_month"] == pytest.approx(1.698750, abs=1e-5)  # worked separately
    assert anchor["vs_central_pp"] == pytest.approx(1.698750 - 1.45, abs=1e-5)

    last30 = t.loc["Last 30d"]  # 25 Sep − 30d = 26 Aug, which has a close
    assert last30["from"] == pd.Timestamp("2026-08-26") and last30["days"] == 30
    assert not pd.isna(t.loc["Last 60d"]["pct_per_month"])


def test_loglinear_fit_recovers_exact_pace_with_weekend_gaps():
    closes = business_closes("2026-06-25", "2026-09-25", 0.0145, 45.70)
    pace, r2, n = model.loglinear_fit(closes, DPM)
    assert pace == pytest.approx(0.0145, abs=1e-12)
    assert r2 == pytest.approx(1.0, abs=1e-12)
    assert n == len(closes)


def test_loglinear_fit_r2_drops_with_noise():
    closes = business_closes("2026-06-25", "2026-09-25", 0.0145, 45.70)
    noise = np.random.default_rng(0).normal(0, 0.004, len(closes))
    _, r2, _ = model.loglinear_fit(closes * np.exp(noise), DPM)
    assert 0 < r2 < 0.999


def test_trend_windows_flags_acceleration(cfg):
    slow = business_closes("2025-10-01", "2026-08-25", 0.010, 40.0)
    fast = business_closes("2026-08-25", "2026-09-25", 0.025, slow.iloc[-1])
    closes = pd.concat([slow, fast.iloc[1:]])
    w = model.trend_windows(closes, cfg)
    newest, prior = w.iloc[0], w.iloc[1]
    assert newest["window_end"] == pd.Timestamp("2026-09-25")
    assert prior["window_end"] == pd.Timestamp("2026-08-25")
    assert prior["pct_per_month"] == pytest.approx(1.0, abs=1e-9)
    assert newest["delta_pp"] > 0.20 and bool(newest["faster"])
    assert not w["faster"].iloc[1:].any()


def test_trend_windows_constant_pace_never_flags(cfg):
    closes = business_closes("2025-10-01", "2026-09-25", 0.0145, 40.0)
    w = model.trend_windows(closes, cfg)
    assert len(w) == cfg.regression_windows_shown
    assert w["pct_per_month"].dropna().to_numpy() == pytest.approx(1.45, abs=1e-9)
    assert not w["faster"].any()


def test_trend_windows_threshold_is_strict_and_configurable(cfg):
    slow = business_closes("2025-10-01", "2026-08-25", 0.010, 40.0)
    fast = business_closes("2026-08-25", "2026-09-25", 0.025, slow.iloc[-1])
    closes = pd.concat([slow, fast.iloc[1:]])
    delta = model.trend_windows(closes, cfg).iloc[0]["delta_pp"]
    at_threshold = model.trend_windows(closes, replace(cfg, faster_flag_pp=delta))
    assert not at_threshold.iloc[0]["faster"]


def test_trend_windows_marks_incomplete_history_as_nan(cfg):
    closes = business_closes("2026-06-01", "2026-09-25", 0.0145, 45.0)  # < 3 months for old windows
    w = model.trend_windows(closes, cfg)
    assert not math.isnan(w.iloc[0]["pct_per_month"])
    assert math.isnan(w.iloc[-1]["pct_per_month"])


# --- crawl check -------------------------------------------------------------------------

def test_crawl_check_flags_fx_above_cpi_and_skips_partial_month():
    idx = pd.bdate_range("2026-06-01", "2026-09-15")
    level = {6: 46.0, 7: 46.46, 8: 47.40, 9: 48.00}  # Jul +1.00%, Aug +2.02%, Sep partial
    closes = pd.Series([level[d.month] for d in idx], index=idx)
    cpi = pd.Series([100.0, 101.5, 103.0], index=pd.to_datetime(["2026-06-01", "2026-07-01",
                                                                    "2026-08-01"]))
    df = model.crawl_check(closes, cpi)
    df.index = df.index.strftime("%Y-%m")

    assert df.index.tolist() == ["2026-09", "2026-08", "2026-07", "2026-06"]  # newest first
    jul, aug, sep = df.loc["2026-07"], df.loc["2026-08"], df.loc["2026-09"]
    assert jul["fx_mom_pct"] == pytest.approx(1.0, abs=1e-9)
    assert jul["cpi_mom_pct"] == pytest.approx(1.5, abs=1e-9)
    assert not jul["flag"] and jul["status"] == "OK (FX ≤ CPI)"
    assert aug["cpi_mom_pct"] == pytest.approx((103 / 101.5 - 1) * 100)
    assert aug["flag"] and aug["status"].startswith("FX > CPI")
    assert sep["partial"] and not sep["flag"] and sep["status"] == "Month in progress"


def test_crawl_check_does_not_flag_partial_month_even_with_its_cpi():
    # FX feed stalled mid-September but September CPI is already out: a half-month
    # average must not raise a regime-break flag.
    idx = pd.bdate_range("2026-07-01", "2026-09-15")
    level = {7: 46.0, 8: 46.46, 9: 47.50}  # Sep +2.2% on a partial average
    closes = pd.Series([level[d.month] for d in idx], index=idx)
    cpi = pd.Series([100.0, 101.5, 102.0],  # Sep CPI +0.49%
                    index=pd.to_datetime(["2026-07-01", "2026-08-01", "2026-09-01"]))
    sep = model.crawl_check(closes, cpi).iloc[0]
    assert sep["partial"] and sep["gap_pp"] > 0
    assert not sep["flag"] and sep["status"] == "Month in progress"


def test_crawl_check_without_cpi_never_flags():
    closes = business_closes("2026-06-01", "2026-08-31", 0.03, 46.0)
    df = model.crawl_check(closes, None)
    assert not df["flag"].any()
    assert (df["status"].iloc[:-1] == "CPI not available").all()


def test_crawl_check_last_business_day_is_not_partial():
    closes = business_closes("2026-06-01", "2026-07-31", 0.01, 46.0)  # 31 Jul 2026 is a Friday
    assert not model.crawl_check(closes, None)["partial"].any()


# --- target dates ------------------------------------------------------------------------

def test_days_to_target_roundtrip_compound():
    spec = PathSpec("central", "Central", "compound", 0.0145, pd.Timestamp("2026-05-29"), 45.70)
    days = model.days_to_target(spec, 55.0, DPM)
    assert 45.70 * 1.0145 ** (days / DPM) == pytest.approx(55.0, rel=1e-12)


def test_days_to_target_linear_hand_worked():
    spec = PathSpec("linear", "Linear", "linear", 0.556, pd.Timestamp("2026-05-29"), 45.70)
    # (50 − 45.70) / 0.556 months * 30.4375 = 235.398 days
    assert model.days_to_target(spec, 50.0, DPM) == pytest.approx(235.398, abs=1e-3)


def test_target_dates_table(cfg):
    closes = pd.Series([45.9, 47.0, 48.811],
                       index=pd.to_datetime(["2026-06-01", "2026-08-03", "2026-09-25"]))
    t = model.target_dates(cfg, 50.0, closes, 0.015).set_index("path")
    assert pd.isna(t.loc["Actual closes", "reached"])
    assert t.loc["Linear", "reached"] == pd.Timestamp("2026-05-29") + pd.Timedelta(days=236)
    # Faster paths reach 50 sooner.
    assert t.loc["Band high", "reached"] < t.loc["Central", "reached"] < t.loc["Base", "reached"]
    assert t.loc["Updated", "reached"] > pd.Timestamp("2026-09-25")

    hit = model.target_dates(cfg, 47.0, closes, 0.015).set_index("path")
    assert hit.loc["Actual closes", "reached"] == pd.Timestamp("2026-08-03")
    assert hit.loc["Updated", "note"] == "before path start"
