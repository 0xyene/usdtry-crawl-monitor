# Plan: USD/TRY crawl monitor

Written 2026-09-25, after reading the official EVDS guide (`EVDS_WEB_SERVIS.pdf`,
downloaded from evds3.tcmb.gov.tr/dokumanlar) and the Frankfurter docs, and probing
both APIs.

## What the docs say (verified, not assumed)

**EVDS (primary)**
- The old `evds2.tcmb.gov.tr/service/evds/` URL now returns a **302 redirect to the
  web UI**, so any code still using it gets HTML back.
- The new base is `https://evds3.tcmb.gov.tr/igmevdsms-dis/`. Example request:
  `series=TP.DK.USD.A.YTL&startDate=01-09-2026&endDate=24-09-2026&type=json`
- The API key goes in the **HTTP header `key`**, never in the URL. I probed it:
  - no key → `403 {"message":"Required request header 'key' is not present"}`
  - wrong key → `401 Invalid API Key`
- Dates are `dd-mm-yyyy`. Each request returns **at most 1000 observations**, so the
  client splits long ranges into chunks of ≤365 days.
- `TP.DK.USD.A.YTL` is "(USD) ABD Doları (Döviz **Alış**)". That is TCMB's
  indicative **buying** rate, frequency GÜNLÜK. I confirmed this via the EVDS
  metadata for data group `bie_dkdovytl`.
- Response shape: `{"items":[{"Tarih":"25-09-2026","TP_DK_USD_A_YTL":"48.77",…}]}`.
  The dots in the code become underscores, values arrive as strings, and weekends
  and holidays come back as `null`. Monthly dates look like `2026-1`.
- **CPI series code:** `TP.FG.J0` is now marked **"(Arşiv)"**, meaning archived.
  TÜİK rebased CPI to **2025=100**. The current general index is
  **`TP.TUKFIY2025.GENEL`** (data group `bie_tukfiy2025`, "Genel Endeks", monthly).

**Frankfurter (fallback)**
- v1 (`?symbols=TRY`, ECB-only data) is **deprecated**. v2 uses
  `/v2/rates?base=USD&quotes=TRY&from=…&to=…`.
- v2 lists a **`TCMB` provider**. `providers=TCMB` returns TCMB's own daily fixing
  (the buying/selling midpoint) with no key needed.
- By default v2 blends ~40 providers, and the blended series has **Saturday and
  Sunday values**. The ECB series is fixed at 14:15 CET and runs about 0.4% above
  TCMB.

## Decisions (flag any you disagree with)

1. **The fallback is Frankfurter v2 with `providers=TCMB`**, not v1/ECB. This is the
   same fixing as the primary, so switching source doesn't move the level. The
   remaining gap is **mid vs buying, about 0.09%**. It is configurable in
   `config.yaml` (`null` = blended).
2. **CPI has no fallback.** Without `EVDS_API_KEY` the crawl check shows FX only and
   says why. I am not adding another source unless you say so.
3. **The anchor stays at your 45.70 @ 2026-05-29**, but note:
   - 27–29 May 2026 was Kurban Bayramı, so **TCMB published no fixing on the anchor
     date**. The data jumps from 26 May to 1 Jun.
   - For comparison: the TCMB mid on 26 May was 45.67, and ECB on 29 May was 45.886.
   - "Since anchor" pace uses your 45.70 so that it compares directly with the path
     rates.
4. **The trend check uses rolling 3-month windows re-evaluated monthly.**
   - Windows end at the latest close, one month earlier, two months earlier, and
     so on.
   - Each window fits a log-linear OLS, `ln(rate) ~ days`. Slope → %/mo =
     `exp(b·30.4375) − 1`, reported with R².
   - A window is flagged "trend getting faster" when its slope is more than
     +0.20pp above the window ending one month earlier.
   - Window length, step and threshold are all in config.
5. **The pace formula matches the model compounding:**
   `%/mo = (end/start)^(30.4375/days) − 1`. Rolling N-day windows start from the last
   close on or before `latest − N` days. YTD starts from the last close of the
   previous year.
6. **Crawl check:** compare the month-over-month change of the monthly average of
   daily closes with CPI month-over-month. Months where FX > CPI are flagged. The
   current month is marked partial, and no flag is raised until CPI for that month
   is released.
7. **Updated path:** starts at the latest close (on its date) and compounds at the
   slider rate. The target-date calculator inverts each path in closed form:
   - compounding: `days = 30.4375·ln(T/S)/ln(1+r)`
   - linear: `days = 30.4375·(T−S)/0.556`
8. **Layout:** tabs across the top (Chart · Pace · Crawl vs CPI · Target dates).
   The slider is in the sidebar because it drives both the chart and the targets.
   UI text is English, as in the spec.
9. **Entry point:** `streamlit run src/app.py`. A small path bootstrap makes
   `src.*` imports work both locally and on Community Cloud.

## Layout

```
config.yaml            model params (editable)
src/data.py            EVDS + Frankfurter clients, parsing, fallback, source logging
src/model.py           paths, pace, regression, crawl check, target dates (pure, no Streamlit)
src/app.py             Streamlit UI; st.cache_data(ttl=6h) wraps the data calls
tests/                 model math + API parsing (HTTP mocked with `responses`)
.github/workflows/     pytest on push/PR
.env.example, README.md
```

## Verification

- Hand-checked numbers in tests. Example: 1461 days is exactly 48 months, so
  `anchor·1.0145^48`.
- Mutation check: break the formula, show that the test fails.
- Live run of the app on the Frankfurter fallback (there is no EVDS key on this
  machine), with a screenshot.
- The EVDS parser is tested against the documented response shape. It **has not
  been run against a live EVDS response**; that needs your key.
