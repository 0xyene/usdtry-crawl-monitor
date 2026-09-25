import json
import logging
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import responses

from src import data
from src.data import DataError

FIXTURES = Path(__file__).parent / "fixtures"
EVDS_RE = re.compile(r"https://evds3\.tcmb\.gov\.tr/igmevdsms-dis/series=.*")
USD = "TP.DK.USD.A.YTL"
CPI = "TP.TUKFIY2025.GENEL"

# EVDS bodies below are hand-built to the documented shape (official guide + maintained
# clients): values as strings, nulls on holidays, dotted code → underscored column.
# There was no EVDS key on the dev machine, so these are NOT captured live responses.
EVDS_DAILY = {
    "totalCount": 5,
    "items": [
        {"Tarih": "22-05-2026", USD.replace(".", "_"): "45.5231", "UNIXTIME": {"$numberLong": "1779397200"}},
        {"Tarih": "23-05-2026", USD.replace(".", "_"): None},
        {"Tarih": "25-05-2026", USD.replace(".", "_"): "45.5487"},
        {"Tarih": "27-05-2026", USD.replace(".", "_"): None},  # Kurban Bayramı
        {"Tarih": "01-06-2026", USD.replace(".", "_"): "45.6267"},
    ],
}
EVDS_MONTHLY = {
    "items": [
        {"Tarih": "2026-1", "TP_TUKFIY2025_GENEL": "104.10"},
        {"Tarih": "2026-2", "TP_TUKFIY2025_GENEL": "106.25"},
        {"Tarih": "2026-12", "TP_TUKFIY2025_GENEL": None},
    ]
}


def frankfurter_fixture():
    return json.loads((FIXTURES / "frankfurter_v2_tcmb_2026-05-20_2026-06-05.json").read_text())


# --- EVDS parsing ------------------------------------------------------------------------

def test_parse_evds_daily_drops_nulls_and_casts_strings():
    s = data.parse_evds(EVDS_DAILY, USD)
    assert s.index.tolist() == pd.to_datetime(["2026-05-22", "2026-05-25", "2026-06-01"]).tolist()
    assert s.tolist() == [45.5231, 45.5487, 45.6267]
    assert s.dtype == float


def test_parse_evds_monthly_dates_are_month_starts():
    s = data.parse_evds(EVDS_MONTHLY, CPI)
    assert s.index.tolist() == [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-02-01")]
    assert s.tolist() == [104.10, 106.25]


def test_parse_evds_wrong_series_raises():
    with pytest.raises(DataError, match="no column TP_DK_EUR_A_YTL"):
        data.parse_evds(EVDS_DAILY, "TP.DK.EUR.A.YTL")


def test_parse_evds_rejects_payload_without_items():
    with pytest.raises(DataError, match="no 'items'"):
        data.parse_evds({"status": "403"}, USD)


def test_parse_evds_date_rejects_unknown_format():
    with pytest.raises(DataError):
        data.parse_evds_date("2026/05/29")


# --- EVDS HTTP ---------------------------------------------------------------------------

@responses.activate
def test_fetch_evds_sends_key_in_header_and_documented_url():
    responses.add(responses.GET, EVDS_RE, json=EVDS_DAILY)
    data.fetch_evds(USD, date(2026, 5, 22), date(2026, 6, 1), "secret-key")
    req = responses.calls[0].request
    assert req.headers["key"] == "secret-key"
    assert "secret-key" not in req.url
    assert req.url == ("https://evds3.tcmb.gov.tr/igmevdsms-dis/series=TP.DK.USD.A.YTL"
                       "&startDate=22-05-2026&endDate=01-06-2026&type=json")


@responses.activate
def test_fetch_evds_chunks_long_ranges_without_gaps_or_overlap():
    responses.add(responses.GET, EVDS_RE, json={"items": []})
    data.fetch_evds(USD, date(2025, 1, 1), date(2026, 9, 25), "k")
    ranges = [re.search(r"startDate=([\d-]+)&endDate=([\d-]+)", c.request.url).groups()
              for c in responses.calls]
    assert ranges == [("01-01-2025", "31-12-2025"), ("01-01-2026", "25-09-2026")]


@responses.activate
def test_fetch_evds_monthly_is_single_request():
    responses.add(responses.GET, EVDS_RE, json=EVDS_MONTHLY)
    s = data.fetch_evds(CPI, date(2024, 1, 1), date(2026, 9, 25), "k", chunk_days=None)
    assert len(responses.calls) == 1 and len(s) == 2


@responses.activate
@pytest.mark.parametrize("status,body", [(401, "Invalid API Key"),
                                         (403, '{"status":"403","message":"Required request header \'key\' is not present"}')])
def test_fetch_evds_http_errors_raise(status, body):
    responses.add(responses.GET, EVDS_RE, body=body, status=status)
    with pytest.raises(DataError, match=f"EVDS HTTP {status}"):
        data.fetch_evds(USD, date(2026, 9, 1), date(2026, 9, 25), "k")


@responses.activate
def test_fetch_evds_html_instead_of_json_raises():
    # What the retired evds2 endpoint effectively returns now: the web UI's HTML.
    responses.add(responses.GET, EVDS_RE, body="<!DOCTYPE html><html>", status=200,
                  content_type="text/html")
    with pytest.raises(DataError, match="non-JSON"):
        data.fetch_evds(USD, date(2026, 9, 1), date(2026, 9, 25), "k")


# --- Frankfurter -------------------------------------------------------------------------

def test_parse_frankfurter_real_tcmb_response():
    s = data.parse_frankfurter(frankfurter_fixture())
    assert s.loc["2026-05-26"] == 45.6723
    assert s.loc["2026-09-25":].empty
    # Kurban Bayramı 27–29 May 2026: TCMB published nothing, the series jumps to 1 June.
    assert pd.Timestamp("2026-05-29") not in s.index
    assert s.index.is_monotonic_increasing


def test_parse_frankfurter_rejects_v1_shape():
    with pytest.raises(DataError, match="not a list"):
        data.parse_frankfurter({"base": "USD", "rates": {"2026-05-29": {"TRY": 45.886}}})


@responses.activate
def test_fetch_frankfurter_query_params():
    responses.add(responses.GET, data.FRANKFURTER_URL, json=frankfurter_fixture())
    data.fetch_frankfurter(date(2026, 5, 20), date(2026, 6, 5), "TCMB")
    q = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert q == {"base": ["USD"], "quotes": ["TRY"], "from": ["2026-05-20"],
                 "to": ["2026-06-05"], "providers": ["TCMB"]}


@responses.activate
def test_fetch_frankfurter_blend_omits_providers():
    responses.add(responses.GET, data.FRANKFURTER_URL, json=frankfurter_fixture())
    data.fetch_frankfurter(date(2026, 5, 20), date(2026, 6, 5), None)
    assert "providers" not in responses.calls[0].request.url


# --- source selection + logging ----------------------------------------------------------

KW = {"series": USD, "provider": "TCMB"}
START, END = date(2026, 5, 20), date(2026, 6, 5)


@responses.activate
def test_load_usdtry_prefers_evds(caplog):
    responses.add(responses.GET, EVDS_RE, json=EVDS_DAILY)
    with caplog.at_level(logging.INFO, logger="usdtry.data"):
        s, src = data.load_usdtry(START, END, "k", **KW)
    assert src.name == "EVDS" and src.fallback_reason is None
    assert s.iloc[-1] == 45.6267
    assert "source=EVDS" in caplog.text
    assert not any("frankfurter" in c.request.url for c in responses.calls)


@responses.activate
def test_load_usdtry_falls_back_when_evds_rejects_key(caplog):
    responses.add(responses.GET, EVDS_RE, body="Invalid API Key", status=401)
    responses.add(responses.GET, data.FRANKFURTER_URL, json=frankfurter_fixture())
    with caplog.at_level(logging.INFO, logger="usdtry.data"):
        s, src = data.load_usdtry(START, END, "bad", **KW)
    assert src.name == "Frankfurter" and "401" in src.fallback_reason
    assert s.loc["2026-05-26"] == 45.6723
    assert "source=Frankfurter provider=TCMB" in caplog.text


@responses.activate
def test_load_usdtry_without_key_never_calls_evds(caplog):
    responses.add(responses.GET, data.FRANKFURTER_URL, json=frankfurter_fixture())
    with caplog.at_level(logging.INFO, logger="usdtry.data"):
        _, src = data.load_usdtry(START, END, None, **KW)
    assert src.fallback_reason == "EVDS_API_KEY not set"
    assert len(responses.calls) == 1
    assert "EVDS_API_KEY not set" in caplog.text


@responses.activate
def test_load_usdtry_both_down_raises_with_both_reasons():
    responses.add(responses.GET, EVDS_RE, body="Invalid API Key", status=401)
    responses.add(responses.GET, data.FRANKFURTER_URL, status=503, body="down")
    with pytest.raises(DataError, match=r"EVDS HTTP 401.*Frankfurter HTTP 503"):
        data.load_usdtry(START, END, "bad", **KW)


@responses.activate
def test_load_cpi_uses_evds_only():
    none, why = data.load_cpi(START, END, None, series=CPI)
    assert none is None and "EVDS_API_KEY" in why
    assert len(responses.calls) == 0

    responses.add(responses.GET, EVDS_RE, json=EVDS_MONTHLY)
    s, label = data.load_cpi(date(2026, 1, 15), END, "k", series=CPI)
    assert "startDate=01-01-2026" in responses.calls[0].request.url  # month start, per the guide
    assert s.tolist() == [104.10, 106.25] and CPI in label


def test_get_api_key_blank_is_none(monkeypatch):
    monkeypatch.setenv("EVDS_API_KEY", "   ")
    assert data.get_api_key() is None
    monkeypatch.setenv("EVDS_API_KEY", " abc ")
    assert data.get_api_key() == "abc"
