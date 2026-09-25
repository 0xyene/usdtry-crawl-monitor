# USD/TRY crawl monitor

A Streamlit app that tracks USD/TRY against a managed-crawl model. It covers:

- where the rate is now relative to the model's paths and band
- how fast it is crawling
- whether the trend is speeding up
- whether the crawl is still slower than CPI

## Views

1. **Chart.** Daily closes plotted against:
   - the 1.1–2.0%/mo band (shaded)
   - Base 1.2%, Central 1.45% and Linear +0.556 TRY/mo, all anchored at 45.70 on 2026-05-29
   - an Updated path that compounds from the latest close at the sidebar slider rate

   Horizon: Dec-26, Jun-27 or Nov-27. A table under the chart gives each path's value at
   the horizon.
2. **Pace.** Compounded %/month for YTD, since anchor, and the last 30/60/90 days.
   Below that is a rolling 3-month log-linear trend (slope and R²). A window is flagged
   "▲ faster" when its slope beats the window ending one month earlier by more than
   0.20pp.
3. **Crawl vs CPI.** Month-over-month change in the monthly USD/TRY average against CPI
   month-over-month. Completed months where FX > CPI are flagged as a regime-break
   warning.
4. **Target dates.** When each path, and the actual closes, reach a USD/TRY level you
   type in.

The model math is in `config.yaml`. All paths compound monthly:
`rate(t) = start * (1 + r) ** (days / 30.4375)`.

## Data sources

| Series | Primary | Fallback |
|---|---|---|
| USD/TRY daily | TCMB EVDS `TP.DK.USD.A.YTL` (indicative **buying** rate) | Frankfurter v2 `providers=TCMB` (TCMB **mid** rate, no key) |
| CPI monthly | TCMB EVDS `TP.TUKFIY2025.GENEL` (TÜFE 2025=100) | none, by design |

Checked against the official docs and live probes on 2026-09-25:

- **EVDS moved to EVDS3.** The API base is `https://evds3.tcmb.gov.tr/igmevdsms-dis/`,
  and the key goes in the HTTP header `key`. The old
  `evds2.tcmb.gov.tr/service/evds/` URL now redirects to the web UI.
- **The old CPI code `TP.FG.J0` is archived.** TÜİK rebased CPI to 2025=100.
- **Frankfurter v1 (`symbols=`, ECB-only) is deprecated.** The app uses v2 with the TCMB
  provider, so a fallback keeps the same fixing as the primary.

The sidebar shows which source is in use and why. Every fetch is also logged, e.g.
`USD/TRY source=Frankfurter provider=TCMB rows=435 last=2026-09-25 48.8110 (fallback: EVDS_API_KEY not set)`.

## Run locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # paste your EVDS key, or leave empty for the fallback
.venv/bin/streamlit run src/app.py
```

To get an EVDS key, sign in at evds3.tcmb.gov.tr, open **Profilim**, then click
**API Key Kopyala**. Without a key the app still runs: USD/TRY comes from Frankfurter and
the CPI check is skipped with a notice.

Data is cached with `st.cache_data` (TTL 6h, keyed by date). **Refresh data** in the
sidebar clears the cache.

## Tests

```bash
.venv/bin/pytest -q
```

- `tests/test_model.py` covers the path math, with hand-worked values (e.g. Central on
  2026-12-31 = 50.6155), plus pace, the log-linear fit, the acceleration flag, the crawl
  check and target dates.
- `tests/test_data.py` covers EVDS and Frankfurter parsing, the key-in-header check,
  chunking, errors and fallback. HTTP is mocked with `responses`.
- `tests/test_app.py` runs the whole Streamlit script headless (`AppTest`) on both
  source paths.

GitHub Actions runs `pytest` on every push and PR (`.github/workflows/tests.yml`).

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub. Private repos work too.
2. Go to [share.streamlit.io](https://share.streamlit.io), click **Create app**, then pick
   the repo and branch `main`.
3. Set **Main file path** to `src/app.py`.
4. Under **Advanced settings**, choose Python **3.12** and paste into **Secrets**:
   ```toml
   EVDS_API_KEY = "your-key-here"
   ```
   Root-level secrets are exposed as environment variables, which is what the app reads.
   It also falls back to `st.secrets`.
5. Deploy. Dependencies come from `requirements.txt`. To change the key later, go to
   **App settings → Secrets**.

## Known caveats

- **No fixing on the anchor date.** 27–29 May 2026 was Kurban Bayramı and TCMB published
  nothing. The TCMB mid on 26 May was 45.67, and ECB on 29 May was 45.886. The anchor
  stays at the configured 45.70.
- **Buying vs mid.** EVDS gives the buying rate and the Frankfurter fallback gives the
  mid. They differ by about 0.09%, so switching source shifts the level slightly.
- **Weekend carry lands on Tuesday.** In the TCMB series, the Tuesday fixing jumps about
  22bp while other weekdays move about 4bp; the weekend's crawl gets booked then.
  - Measured over Jun–Sep 2026, Monday closes sit about 11bp below the log-linear trend.
    Other weekdays are within ±5bp.
  - A 30-day pace that starts or ends on a Monday is therefore off by roughly
    0.1pp/mo.
  - The 3-month regression averages this out.
- **EVDS parser not yet run live.** It is tested against the documented response shape,
  not a captured live response, because there was no key on the dev machine. The first
  run with a real key is the live check.
