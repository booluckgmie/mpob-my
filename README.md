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
python crawl.py --list                      # BEPI file inventory by family x year
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

- **Done and tested against the live sites:** daily CPO price (last ~3 years from BEPI, 2023 from the
  repo CSVs), headline ticker, OpenDOSM IPI, BEPI link inventory.
- **Not built:** the Excel parsers for the families listed in `sources.json` (export by destination,
  stocks, OER, yield, ...). `crawl.py --download <family>` fetches and hashes the files; parsing each
  needs the real sheet layouts (header rows shift between editions).
- **Reconciliation** needs MPOB monthly production history. The ticker only exposes the current month, so
  it fills in one month per release until the Excel parsers exist.
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
