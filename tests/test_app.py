"""End-to-end smoke tests: run the real Streamlit script with HTTP mocked."""
import json
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
import responses
from streamlit.testing.v1 import AppTest

from src import data

APP = str(Path(__file__).resolve().parent.parent / "src" / "app.py")
EVDS_RE = re.compile(r"https://evds3\.tcmb\.gov\.tr/igmevdsms-dis/series=.*")
TODAY = pd.Timestamp(date.today())
DAYS = pd.bdate_range("2025-01-01", TODAY)
RATES = 36.0 * 1.015 ** ((DAYS - DAYS[0]).days / 30.4375)
# A completed month (two months back) where CPI is deliberately below the FX crawl.
FLAG_MONTH = (TODAY - pd.DateOffset(months=2)).to_period("M")


def frankfurter_body(request):
    return 200, {}, json.dumps([{"date": f"{d:%Y-%m-%d}", "base": "USD", "quote": "TRY",
                                 "rate": round(r, 4)} for d, r in zip(DAYS, RATES)])


def evds_body(request):
    series = re.search(r"series=([^&]+)", request.url).group(1)
    start, end = (datetime.strptime(v, "%d-%m-%Y") for v in
                  re.search(r"startDate=([\d-]+)&endDate=([\d-]+)", request.url).groups())
    if series == "TP.DK.USD.A.YTL":
        items = [{"Tarih": f"{d:%d-%m-%Y}", "TP_DK_USD_A_YTL": f"{r:.4f}"}
                 for d, r in zip(DAYS, RATES) if start <= d <= end]
    else:  # CPI: +2.0% a month, except +0.5% in FLAG_MONTH; published up to last month
        months = pd.period_range("2025-01", TODAY.to_period("M") - 1, freq="M")
        level, items = 100.0, []
        for m in months:
            level *= 1.005 if m == FLAG_MONTH else 1.02
            items.append({"Tarih": f"{m.year}-{m.month}", "TP_TUKFIY2025_GENEL": f"{level:.2f}"})
    return 200, {}, json.dumps({"items": items})


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)


def run_app():
    return AppTest.from_file(APP, default_timeout=60).run()


@responses.activate
def test_app_runs_on_frankfurter_without_key(monkeypatch):
    monkeypatch.delenv("EVDS_API_KEY", raising=False)
    responses.add_callback(responses.GET, data.FRANKFURTER_URL, callback=frankfurter_body)
    at = run_app()
    assert not at.exception and not at.error
    assert [t.label for t in at.tabs] == ["Chart", "Pace", "Crawl vs CPI", "Target dates"]
    assert at.metric[3].value == "1.50%/mo"  # 3m slope of a series crawling at exactly 1.5%/mo
    assert any("Frankfurter" in i.value for i in at.info)
    assert any("CPI unavailable" in i.value for i in at.info)
    assert not any("evds" in c.request.url for c in responses.calls)


@responses.activate
def test_app_runs_on_evds_and_flags_regime_break(monkeypatch):
    monkeypatch.setenv("EVDS_API_KEY", "test-key")
    responses.add_callback(responses.GET, EVDS_RE, callback=evds_body)
    at = run_app()
    assert not at.exception and not at.error
    assert not at.info  # no fallback banner, CPI present
    assert all(c.request.headers["key"] == "test-key" for c in responses.calls)
    warnings = [w.value for w in at.warning]
    assert any(FLAG_MONTH.strftime("%b %Y") in w for w in warnings), warnings
