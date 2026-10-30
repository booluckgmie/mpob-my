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
| Exports by port (each region's ports plus its "others" sum to the subtotal) | 2014-2026 monthly |
| Exports by destination country (palm oil, palm kernel oil) | **none**: BEPI lists the reports for 2014-2018 but every one says "information is being updated" |
| Germinated seed supply/demand | 2013-2026 |
| FFB and CPO yield by state (PDF, estates only, t/ha) | 2019-2026 monthly |
| Planted area by state (PDF, matured/immature/total ha) | annual, 2011-2018 and 2023-2025 (2011: by region only) |
| Daily CPO price | ~3 years from BEPI, 2023 seeded from the CSVs in `data/` |
| Headline scoreboard | monthly, from the ticker |
| OpenDOSM IPI (total + MSIC 104) | monthly, canary only |

Not loaded, deliberately: `summary_annual` and the seedlings table (labels embed other values, layout not
parsed yet) and `import_monthly` (BEPI says "information is being updated"). Yield and planted area come
from PDFs (`pdf_reports.py`, pdfplumber), not HTML reports. Failures are quarantined and logged, never
silently dropped.

Checks on the real data: state production sums exactly to the regional and national rows, and the
September 2026 production and stock figures match the headline ticker to the tonne.

Reconciliation compares MPOB CPO production growth with DOSM IPI group 104 (all vegetable/animal oils and
fats). They only loosely agree (MoM correlation ~0.8, typical gap ~5 points), so the flag threshold is
calibrated from the history (robust z-score), and alerts only cover the last 6 comparable months.

Dashboard (`dashboard/index.html`, served at `/`): written for different readers. **Overview** has plain-language
facts and key numbers with sparklines; **Markets** has supply/demand, production vs earlier years, the
supply/demand balance and price statistics; **Regions** is a state map for production, oil extraction rate,
FFB received, fruit yield and planted area; **Outlook** is an illustrative 2027 what-if simulator whose
assumptions you edit; **Data & methods** holds data freshness, the known issues (read from this README),
a glossary and API notes. The "Explain things to me" switch opens a short explanation under every section.
Boundaries: geoBoundaries (ODbL), simplified; "other states" grouping is assumed. Indonesia is not mapped
(no comparable official data here) and there is no El Nino stress layer.

- `jenis=1Y` in the price URL selects the series; other values likely expose FFB / PKO / export prices. Unprobed.
- `sources.yaml` is the descriptive dataset registry; `sources.json` is what the crawler reads.
- `scrapeCPO.py` / `cpo_daily_prices.csv` are the original prototype, kept for reference.

## Data issues and caveats

Read these before using the numbers. "Source" means the issue is in what MPOB / BEPI / DOSM publish;
"Ours" means it comes from how this pipeline handles it.

| # | Issue | Where | Effect / what to do |
|---|---|---|---|
| 1 | **Daily price history is short.** BEPI serves only ~3 years; earlier years return an empty page. 2023 comes from CSVs scraped earlier (`data/cpo_daily_prices2023.csv`) that cannot be re-checked against the source. | Source / ours | Prices before 2023 are absent. Treat 2023 as unverified. |
| 2 | **Stocks and refinery stocks start in 2021.** BEPI publishes no earlier editions. | Source | No stock/usage history before 2021. |
| 3 | **Missing years at the source.** Planted area: no page for 2019-2022 and 2026 not yet uploaded (MPOB says January 2027). Yield: nothing before 2019; the 2015-2018 pages carry no PDF. The 2021 oil-yield series has about half the usual number of values. | Source | Series are discontinuous; do not interpolate across the gaps. |
| 4 | **Planted area 2011 is by region only** (P. Malaysia, Sabah, Sarawak), not by state. | Source | State-level area starts in 2012. |
| 5 | **Not loaded:** `summary_annual`, the seedlings table, `import_monthly` ("information is being updated"). | Ours / source | No import-origin or annual-summary data. Listed in `bepi_reports.UNSUPPORTED*`. |
| 6 | **Reports overlap and are revised.** Each BEPI report shows the previous and current year, so later reports restate earlier values (22 revisions seen, mostly OER). | Source | Storage is append-only; views read the latest revision. Query `obs` (not `v_obs_latest`) to see history. |
| 7 | **Sub-totals sit beside states in one namespace** (`malaysia`, `p_malaysia`, `sabah_sarawak`). | Source layout | Never sum all rows of a family. The API flags them with `aggregate: true`. Labels are normalised across years (Johore to JOHOR, Malacca to MELAKA, Semenanjung to P. MALAYSIA, ...). |
| 8 | **"Other states" is MPOB's own grouping** and its members are not stated in the reports. The map assumes Perlis, Melaka, Pulau Pinang, KL, Putrajaya and Labuan. Area and yield PDFs list those states separately, so they do not line up with the map regions. | Source / assumption | Treat the "Other states" region as approximate. |
| 9 | **Section and label come from the source layout.** For example `TOTAL PALM OIL` sits under the section "crude palm oil" in the stocks report; the seed report spells "Penisular". | Source | Select by label, not by section. Typos are preserved. |
| 10 | **Series names include the report title.** If BEPI renames a report, new data lands in a new series and the history splits. | Ours | Watch the `unmatched links` count in `crawl.py --list`; the PDF dataset names are fixed in code. |
| 11 | **Units are partly inferred.** Seed supply/demand carries unit `count` because the report states none; yield is tonnes per hectare for estates only (not smallholders); area is a December snapshot. | Source / ours | Do not compare yield with national production per hectare. |
| 12 | **Yield figures are rounded.** Monthly yields sum to within about 0.02 of the printed annual total. | Source | Do not expect exact reconciliation. |
| 13 | **Daily prices: PH / NT days.** Public-holiday and no-trade days are stored as NULL-value rows and excluded from averages (`n_obs` shows how many days were used). | Ours | Weekly/monthly averages are over trading days only. Quarterly averages are averages of monthly averages. |
| 14 | **Headline ticker shows one month only**, and its % changes are MPOB's own month-on-month figures. The supply/demand balance uses the published stock history now, but the older derived opening stock is still the fallback. Production in the balance is CPO, stock is total palm oil. | Source / ours | Balance is `derived`, not published. |
| 15 | **DOSM index is a loose canary, not a second source.** IPI group 104 covers all vegetable and animal oils and fats; month-on-month correlation with CPO production is about 0.78 and the typical gap about 5 points. It lags about two months and the latest year can be revised. | Source | Reconciliation flags outliers against the series' own spread (robust z-score) and alerts on the last 6 comparable months only. One historical outlier (March 2016) is expected. |
| 16 | **Cross-checks done once, not continuously.** State production sums to the national row, planted-area states sum to the national total, and September 2026 production and stocks match the headline ticker. Nothing re-asserts this on each refresh yet. | Ours | A silent layout change that still parses could slip through; the weekly revision report is the only guard. |
| 17 | **Analytics are derived or illustrative.** Seasonal statistics use about three years of prices (production uses up to five prior years). The 2027 simulator's loss, usage, B50 demand and price-sensitivity inputs are assumptions, not calibrated to MPOB data. | Ours | Never quote simulator output as a forecast. |
| 18 | **Map boundaries** are geoBoundaries gbOpen (2017 vintage, ODbL), simplified, with KL/Putrajaya/Labuan merged. Indonesia is not mapped, and there is no El Nino stress layer. | Ours | Attribution is carried in the GeoJSON. |
| 19 | **Release timing slips.** MPOB's nominal 10th often becomes the next working day; freshness thresholds are heuristic (45 days monthly, longer for DOSM and annual data). | Source / ours | A STALE flag means "check", not "MPOB stopped publishing". |
| 20 | **Licensing.** BEPI lists data purchase rates ("Caj Data"). | Source | Check with MPOB before redistributing the data. |
| 21 | **Operational.** The scheduled refresh does not run until `main` is the default branch and `MPOB_CONTACT` is set; the Vercel preview serves the committed lake read-only and runs no jobs. The lake is committed to git, so its history will grow. | Ours | See the schedule section above. |
| 22 | **No export-by-destination data.** BEPI links ten destination reports (palm oil and palm kernel oil, 2014-2018); all return "information is being updated", and nothing newer is linked. MPOB's yearbook contents list destination tables (4.17-4.27) but the downloadable editions are only the contents pages. OpenDOSM's catalogue has no country-level trade dataset, and data.gov.my's archive could not be reached from the build environment. | Source | The dashboard cannot show who buys Malaysian palm oil. The two families are registered (`export_destination`, `export_destination_pko`) so a republished report loads automatically; the layout is untested because there is nothing to test on. |
| 23 | **Exports by port have two rows called "OTHERS"** (one per region); the report only distinguishes them by position. | Source | Stored as `OTHERS (P. MALAYSIA)` and `OTHERS (SABAH/SARAWAK)`, checked against each region's subtotal for all 153 months. An earlier load had mislabelled these series and merged the two; it was replaced. |

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
