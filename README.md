# mpob-my

Single-source-of-truth pipeline for Malaysian palm oil data: MPOB (BEPI) headline numbers,
daily CPO prices, and the OpenDOSM palm-oil IPI as a reconciliation canary. Stored as Parquet,
served by a small FastAPI app with a dashboard.

```
pip install -r requirements.txt
export MPOB_CONTACT=you@example.com        # goes into the User-Agent

python price_daily.py --import-csv data/cpo_daily_prices2023.csv   # one-off: 2023 seed
python price_daily.py --backfill 2023       # BEPI only serves ~3 years; empty years are skipped
python ticker.py                            # headline scoreboard (monthly)
python dosm_ingest.py                       # OpenDOSM IPI (total + MSIC 104)
python reconcile.py --run                   # MPOB vs DOSM growth check, exit 1 if attention needed
python crawl.py --list                      # BEPI dataset inventory by family x year
python bepi_reports.py --all --since 2011   # BEPI HTML reports: state production, stocks, exports, OER, ...
python bepi_reports.py --rebuild            # re-parse saved raw reports offline
python -m uvicorn api:app --port 8000       # API + dashboard at http://localhost:8000/
python -m pytest
```

## Design

| Layer | Where | Notes |
|---|---|---|
| Bronze | `data/raw/` (git-ignored) | raw HTML / files, content hashed |
| Silver | `data/lake/silver/obs.parquet`, `dim_series.parquet` | append-only; a changed value gets `revision + 1`, history is never overwritten |
| Gold | `data/lake/gold/*.parquet` | weekly / monthly / quarterly views, reconciliation |

`data/mpob.duckdb` is a disposable cache rebuilt from the silver Parquet. Only one process can
hold it open at a time (stop the API before running an ingest).

Roll-up rules live in `dim_series.agg_rule` (`sum` flows, `avg` prices, `last` stocks). Aggregates
are labelled `derived` and carry `n_obs` / `n_months`; quarterly always rolls up from monthly.
PH (public holiday) and NT (no trade) days are stored as NULL-value rows and excluded from averages.

## API

`/v1/series?key=mpob:cpo_daily&freq=daily|weekly|monthly|quarterly&from=&to=`, `/v1/series/list`,
`/v1/headline/latest`, `/v1/freshness`, `/v1/coverage`, `/health`.

## What is and isn't covered

BEPI has no Excel downloads: each dataset page embeds an HTML report (`/stat/web_report1.php`), which
`bepi_reports.py` parses (paired half-year tables, wide product/unit tables, months-down-rows tables).
Each saved report has a JSON sidecar so `--rebuild` can re-parse offline.

| Family | Coverage |
|---|---|
| CPO production by state | 2013-2026 monthly |
| Monthly exports by product (tonnes and RM) | 2014-2026 |
| Exports by port, FFB received (sectoral), OER (CPO and kernel oil) by state | 2012/2013-2026 |
| Closing stocks, refinery stocks | 2021-2026 (BEPI publishes no earlier) |
| Germinated seed supply/demand | 2013-2026 |
| Daily CPO price | ~3 years from BEPI, 2023 seeded from the CSVs in `data/` |
| Headline scoreboard | monthly, from the ticker |
| OpenDOSM IPI (total + MSIC 104) | monthly, canary only |

Not loaded, deliberately: `summary_annual` and the seedlings table (labels embed other values, layout not
parsed yet); `import_monthly` (BEPI says "information is being updated"); `yield` and `area` (no embedded
report found). Failures are quarantined and logged, never silently dropped.

Checks on the real data: state production sums exactly to the regional and national rows, and the
September 2026 production and stock figures match the headline ticker to the tonne.

Reconciliation compares MPOB CPO production growth with DOSM IPI group 104 (all vegetable/animal oils and
fats). They only loosely agree (MoM correlation ~0.8, typical gap ~5 points), so the flag threshold is
calibrated from the history (robust z-score), and alerts only cover the last 6 comparable months.

Dashboard analytics: price statistics, seasonality, supply/demand balance (opening stock from the
published history), CPO production vs the same month in prior years, production by state, a choropleth of
production / OER / FFB received (boundaries: geoBoundaries, ODbL, simplified; "other states" grouping is
assumed), and an illustrative 2027 simulator whose assumptions you edit. Indonesia is not mapped (no
comparable official data here), and no El Nino stress layer (needs rainfall data).

- `jenis=1Y` in the price URL selects the series; other values likely expose FFB / PKO / export prices. Unprobed.
- `sources.yaml` is the descriptive dataset registry; `sources.json` is what the crawler reads.
- `scrapeCPO.py` / `cpo_daily_prices.csv` are the original prototype, kept for reference.

BEPI lists data purchase rates ("Caj Data"). Internal analysis is one thing; check with MPOB before redistributing.

## Schedule (`.github/workflows/daily.yml`, runs once on the default branch)

| Mode | When (UTC) | What |
|---|---|---|
| daily | 07:30 and 10:30 | prices (this + last year) + headline, then a **second verification pass**: any new or changed value between passes is flagged |
| weekly | Mon 09:00 | re-pull every price year BEPI exposes + headline + DOSM; flags revisions to published figures |
| monthly | 13th 08:00 | weekly + BEPI file inventory + MPOB/DOSM reconcile |

Run locally with `python refresh.py daily|weekly|monthly`. Each pass is logged to
`data/lake/gold/run_log.csv`; exit code 1 means something needs attention (failed source, unstable
data between passes, or a revision of published history). Add a `MPOB_CONTACT` repo secret.
