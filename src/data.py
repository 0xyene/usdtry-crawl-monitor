"""Data access: USD/TRY daily closes (EVDS → Frankfurter fallback) and Turkey CPI (EVDS only).

EVDS, verified 2026-09-25 against the official guide (evds3.tcmb.gov.tr/dokumanlar,
"EVDS Web Servis Kılavuzu") and live probes:
  * base  https://evds3.tcmb.gov.tr/igmevdsms-dis/   (evds2 …/service/evds/ now 302s to the web UI)
  * query series=<code>&startDate=dd-mm-yyyy&endDate=dd-mm-yyyy&type=json   (no "?" — appended to the path)
  * auth  HTTP header `key` (missing → 403 JSON, wrong → 401 "Invalid API Key")
  * limit at most 1000 observations per request
  * body  {"items": [{"Tarih": "25-09-2026", "TP_DK_USD_A_YTL": "48.77", ...}]}; dots in the
          series code become underscores, values are strings, holidays/weekends are null,
          monthly dates look like "2026-1".

Frankfurter v2 (v1 `symbols=` API is deprecated, ECB-only):
  GET https://api.frankfurter.dev/v2/rates?base=USD&quotes=TRY&from=…&to=…&providers=TCMB
  → [{"date": "2026-09-25", "base": "USD", "quote": "TRY", "rate": 48.811}, ...]
  Provider TCMB = the same TCMB fixing as EVDS, published as the buying/selling midpoint.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import requests

log = logging.getLogger("usdtry.data")

EVDS_BASE = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"
FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rates"
TIMEOUT_S = 20
EVDS_CHUNK_DAYS = 365  # well under the 1000-observation cap even if weekends count

_DAILY = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_MONTHLY = re.compile(r"^\d{4}-\d{1,2}$")


class DataError(RuntimeError):
    """A source answered but the answer is unusable (HTTP error, bad JSON, no data)."""


@dataclass(frozen=True)
class Source:
    name: str                          # "EVDS" | "Frankfurter"
    detail: str
    fallback_reason: str | None = None


def get_api_key() -> str | None:
    key = os.environ.get("EVDS_API_KEY", "").strip()
    return key or None


# --- EVDS ---------------------------------------------------------------------------------

def evds_url(series: str, start: date, end: date, frequency: int | None = None) -> str:
    q = f"series={series}&startDate={start:%d-%m-%Y}&endDate={end:%d-%m-%Y}&type=json"
    if frequency is not None:
        q += f"&frequency={frequency}"
    return EVDS_BASE + q


def parse_evds_date(text: str) -> pd.Timestamp:
    if _DAILY.match(text):
        return pd.Timestamp(pd.to_datetime(text, format="%d-%m-%Y"))
    if _MONTHLY.match(text):
        year, month = text.split("-")
        return pd.Timestamp(int(year), int(month), 1)
    raise DataError(f"Unrecognised EVDS date {text!r}")


def parse_evds(payload: dict, series: str) -> pd.Series:
    """EVDS JSON → float Series indexed by date, nulls dropped."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if items is None:
        raise DataError("EVDS response has no 'items'")
    col = series.replace(".", "_")
    if items and not any(col in it or series in it for it in items):
        raise DataError(f"EVDS response has no column {col} (keys: {sorted(items[0])})")
    dates, values = [], []
    for it in items:
        raw = it.get(col, it.get(series))
        if raw is None or raw == "":
            continue
        dates.append(parse_evds_date(str(it["Tarih"])))
        values.append(float(raw))
    s = pd.Series(values, index=pd.DatetimeIndex(dates), name=series, dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _chunks(start: date, end: date, days: int | None):
    if days is None:
        yield start, end
        return
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def fetch_evds(series: str, start: date, end: date, api_key: str, *,
               chunk_days: int | None = EVDS_CHUNK_DAYS,
               session: requests.Session | None = None) -> pd.Series:
    http = session or requests.Session()
    parts = []
    for c_start, c_end in _chunks(start, end, chunk_days):
        resp = http.get(evds_url(series, c_start, c_end), headers={"key": api_key},
                        timeout=TIMEOUT_S)
        if resp.status_code != 200:
            raise DataError(f"EVDS HTTP {resp.status_code}: {resp.text[:120].strip()}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataError(f"EVDS returned non-JSON ({resp.headers.get('content-type')})") from exc
        parts.append(parse_evds(payload, series))
    s = pd.concat(parts) if parts else pd.Series(dtype=float, name=series)
    return s[~s.index.duplicated(keep="last")].sort_index()


# --- Frankfurter ------------------------------------------------------------------------

def parse_frankfurter(payload) -> pd.Series:
    if not isinstance(payload, list):
        raise DataError(f"Frankfurter v2 response is not a list: {str(payload)[:120]}")
    rows = [r for r in payload if r.get("base") == "USD" and r.get("quote") == "TRY"]
    s = pd.Series([float(r["rate"]) for r in rows],
                  index=pd.DatetimeIndex([pd.Timestamp(r["date"]) for r in rows]),
                  name="USDTRY", dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def fetch_frankfurter(start: date, end: date, provider: str | None = "TCMB", *,
                      session: requests.Session | None = None) -> pd.Series:
    http = session or requests.Session()
    params = {"base": "USD", "quotes": "TRY", "from": start.isoformat(), "to": end.isoformat()}
    if provider:
        params["providers"] = provider
    resp = http.get(FRANKFURTER_URL, params=params, timeout=TIMEOUT_S)
    if resp.status_code != 200:
        raise DataError(f"Frankfurter HTTP {resp.status_code}: {resp.text[:120].strip()}")
    try:
        return parse_frankfurter(resp.json())
    except ValueError as exc:
        raise DataError("Frankfurter returned non-JSON") from exc


# --- public loaders ---------------------------------------------------------------------

def load_usdtry(start: date, end: date, api_key: str | None, *, series: str,
                provider: str | None, session: requests.Session | None = None
                ) -> tuple[pd.Series, Source]:
    """Daily USD/TRY closes. EVDS when a key is set and it answers; otherwise Frankfurter."""
    if api_key:
        try:
            s = fetch_evds(series, start, end, api_key, session=session)
            if s.empty:
                raise DataError("EVDS returned no observations")
            log.info("USD/TRY source=EVDS series=%s rows=%d last=%s %.4f",
                     series, len(s), s.index[-1].date(), s.iloc[-1])
            return s, Source("EVDS", f"TCMB EVDS {series} (indicative buying rate)")
        except (DataError, requests.RequestException) as exc:
            reason = f"EVDS failed: {exc}"
            log.warning("USD/TRY: %s; falling back to Frankfurter", reason)
    else:
        reason = "EVDS_API_KEY not set"
        log.warning("USD/TRY: %s; using Frankfurter", reason)

    try:
        s = fetch_frankfurter(start, end, provider, session=session)
    except (DataError, requests.RequestException) as exc:
        raise DataError(f"No USD/TRY data. {reason}. Frankfurter failed: {exc}") from exc
    if s.empty:
        raise DataError(f"No USD/TRY data. {reason}. Frankfurter returned no observations.")
    detail = ("Frankfurter v2, provider TCMB (TCMB mid rate)" if provider == "TCMB"
              else f"Frankfurter v2, provider {provider or 'blend of all'}")
    log.info("USD/TRY source=Frankfurter provider=%s rows=%d last=%s %.4f (fallback: %s)",
             provider or "blend", len(s), s.index[-1].date(), s.iloc[-1], reason)
    return s, Source("Frankfurter", detail, reason)


def load_cpi(start: date, end: date, api_key: str | None, *, series: str,
             session: requests.Session | None = None) -> tuple[pd.Series | None, str]:
    """Monthly CPI index from EVDS. No fallback source by design."""
    if not api_key:
        return None, "EVDS_API_KEY not set, and CPI comes only from EVDS"
    try:
        s = fetch_evds(series, start.replace(day=1), end, api_key, chunk_days=None,
                       session=session)
    except (DataError, requests.RequestException) as exc:
        log.warning("CPI: EVDS failed: %s", exc)
        return None, f"EVDS CPI request failed: {exc}"
    if s.empty:
        return None, f"EVDS returned no observations for {series}"
    log.info("CPI source=EVDS series=%s rows=%d last=%s", series, len(s), s.index[-1].date())
    return s, f"TCMB EVDS {series}"
