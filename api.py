"""Read API over the lake.   uvicorn api:app --port 8000   (dashboard served at /)"""
from __future__ import annotations
import threading
from datetime import date
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

import analytics
import common

app = FastAPI(title="MPOB-MY lake API", version="1.0")
VIEWS = {"daily": "v_daily", "weekly": "v_weekly", "monthly": "v_monthly", "quarterly": "v_quarterly"}
# days since newest period_end before a series is flagged STALE
STALE_AFTER = {"daily": 7, "monthly": 45}
DUE_AFTER = {"daily": 4, "monthly": 35}
LAG_EXTRA = {"dosm": 35}
STOCK_KEY = "bepi:stocks:monthly_closing_stock_of_oil_palm_products:crude_palm_oil:total_palm_oil:tonnes"    # DOSM publishes ~2 months behind the data month


_LOCK = threading.Lock()
_CON: duckdb.DuckDBPyConnection | None = None


def db() -> duckdb.DuckDBPyConnection:
    """One shared connection (schema applied once); each request gets its own cursor."""
    global _CON
    with _LOCK:
        if _CON is None:
            _CON = common.connect()
        return _CON.cursor()


def _rows(con, sql: str, params=()):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/v1/series/list")
def series_list():
    con = db()
    try:
        return _rows(con, "SELECT * FROM dim_series ORDER BY series_key")
    finally:
        con.close()


@app.get("/v1/series")
def series(key: str, freq: str = Query("monthly", pattern="^(daily|weekly|monthly|quarterly)$"),
           start: str | None = Query(None, alias="from"), to: str | None = None):
    """Rolled-up values. Aggregated rows are status=derived; n_obs/n_months say how much real data is behind each."""
    con = db()
    try:
        if not con.execute("SELECT 1 FROM dim_series WHERE series_key = ?", [key]).fetchone():
            raise HTTPException(404, f"unknown series {key}")
        view = VIEWS[freq]
        if freq == "daily":
            sql = f"SELECT CAST(period AS VARCHAR) AS period, value, status, unit FROM {view} WHERE series_key = ?"
        else:
            sql = f"SELECT * EXCLUDE (series_key) FROM {view} WHERE series_key = ?"
        params = [key]
        for bound, op in ((start, ">="), (to, "<=")):
            if bound:
                sql += f" AND CAST(period AS VARCHAR) {op} ?"
                params.append(bound)
        sql += " ORDER BY period"
        return {"series_key": key, "freq": freq, "data": _rows(con, sql, params)}
    finally:
        con.close()


@app.get("/v1/headline/latest")
def headline_latest():
    con = db()
    try:
        return _rows(con, """
            SELECT s.series_key, s.product, s.measure, o.value, s.unit,
                   strftime(o.period_start, '%Y-%m') AS period
            FROM v_obs_latest o JOIN dim_series s USING (series_key)
            WHERE s.dataset = 'headline' AND s.measure <> 'pct_change'
              AND o.period_start = (SELECT max(period_start) FROM v_obs_latest o2
                                    WHERE o2.series_key = o.series_key)
            ORDER BY s.series_key""")
    finally:
        con.close()


@app.get("/v1/analytics")
def analytics_endpoint(key: str = "mpob:cpo_daily"):
    """Price statistics + latest-month supply/demand balance (all status=derived)."""
    con = db()
    try:
        daily = con.execute("SELECT CAST(period AS TIMESTAMP) AS date, value FROM v_daily "
                            "WHERE series_key = ? AND value IS NOT NULL", [key]).df()
        h = {}
        for r in _rows(con, """
                SELECT replace(o.series_key, 'mpob:headline:', '') AS k, o.value
                FROM v_obs_latest o WHERE o.series_key LIKE 'mpob:headline:%' AND o.granularity = 'm'
                  AND o.period_start = (SELECT max(period_start) FROM v_obs_latest o2
                                        WHERE o2.series_key = o.series_key)"""):
            k = r["k"]
            if k.endswith(":pct_change"):
                h.setdefault(k[:-11], {})["pct"] = r["value"]
            else:
                h.setdefault(k, {})["value"] = r["value"]
        balance = analytics.balance(h)
        prod_key = "bepi:cpo_production:production_of_crude_palm_oil:malaysia:tonnes"
        prod = con.execute("SELECT period_start, value FROM v_obs_latest WHERE series_key = ? AND granularity = 'm'",
                           [prod_key]).df()
        stock = con.execute("SELECT period_start, value FROM v_obs_latest WHERE series_key = ? AND granularity = 'm'",
                            [STOCK_KEY]).df()
        if balance and len(stock) >= 2 and {"po_import", "po_export"} <= h.keys():     # prefer published history
            exact = analytics.exact_balance(stock, h["cpo_production"]["value"], h["po_import"]["value"],
                                            h["po_export"]["value"])
            if exact:
                balance = {**balance, **exact}
        states = _rows(con, """
            WITH latest AS (
              SELECT series_key, period_start, value, row_number() OVER (PARTITION BY series_key ORDER BY period_start DESC) rn
              FROM v_obs_latest WHERE series_key LIKE 'bepi:cpo_production:%:tonnes' AND granularity = 'm')
            SELECT split_part(l.series_key, ':', 4) AS state, strftime(l.period_start, '%Y-%m') AS period, l.value,
                   round((l.value / p.value - 1) * 100, 1) AS yoy_pct
            FROM latest l LEFT JOIN v_obs_latest p ON p.series_key = l.series_key
                 AND p.period_start = l.period_start - INTERVAL 1 YEAR AND p.granularity = 'm'
            WHERE l.rn = 1 ORDER BY l.value DESC""")
        return {"price": analytics.price_stats(daily), "balance": balance, "production": analytics.production_band(prod),
                "states": states, "status": "derived"}
    finally:
        con.close()


SPATIAL = {   # metric -> (series-key prefix, label, unit, diverging?)
    "production": ("bepi:cpo_production:production_of_crude_palm_oil:", "CPO production", "tonnes", False),
    "oer": ("bepi:oer:oil_extraction_rate_of_crude_palm_oil:", "Oil extraction rate (CPO)", "%", True),
    "ffb": ("bepi:sectoral:ffb_received_by_mill:", "FFB received by mills", "tonnes", False),
}
AGGREGATES = {"malaysia", "p_malaysia", "sabah_sarawak"}


@app.get("/v1/spatial")
def spatial(metric: str = Query("production", pattern="^(production|oer|ffb)$"), period: str | None = None):
    """Latest (or given) month by state; YoY, share of national (volumes) or gap to national (rates)."""
    prefix, label, unit, diverging = SPATIAL[metric]
    con = db()
    try:
        df = con.execute("""SELECT split_part(series_key, ':', 4) AS state, strftime(period_start, '%Y-%m') AS period, value
                            FROM v_obs_latest WHERE series_key LIKE ? AND granularity = 'm' AND value IS NOT NULL""",
                         [prefix + "%"]).df()
        if df.empty:
            raise HTTPException(404, f"no data for {metric} yet")
        period = period or df["period"].max()
        cur = df[df["period"] == period].set_index("state")["value"]
        prev_p = f"{int(period[:4]) - 1}{period[4:]}"
        prev = df[df["period"] == prev_p].set_index("state")["value"]
        nat = cur.get("malaysia")
        out = []
        for st, v in cur.items():
            row = {"state": st, "value": float(v),
                   "yoy_pct": None if st not in prev else round((v / prev[st] - 1) * 100, 1),
                   "aggregate": st in AGGREGATES}
            if nat:
                row["share_pct"] = None if diverging else round(v / nat * 100, 1)
                row["vs_national"] = round(float(v - nat), 2) if diverging else None
            out.append(row)
        return {"metric": metric, "label": label, "unit": unit, "diverging": diverging, "period": period,
                "national": None if nat is None else float(nat), "states": out, "status": "actual (as reported by MPOB)"}
    finally:
        con.close()


@app.get("/boundaries/my_states.geojson")
def boundaries():
    return FileResponse(Path(__file__).parent / "dashboard" / "my_states.geojson", media_type="application/geo+json")


def _family(key: str) -> str:
    p = key.split(":")
    return f"{p[0]}:{p[1]}" if p[0] == "bepi" else (f"{p[0]}:{p[1]}" if p[0] in ("mpob", "dosm") else key)


@app.get("/v1/freshness")
def freshness(detail: bool = False):
    """FRESH / DUE / STALE per dataset family (newest period across its series); detail=true lists every series."""
    con = db()
    try:
        today = date.today()
        rows = _rows(con, "SELECT series_key, dataset, native_freq, last_period, last_ingested FROM v_coverage")
        if not detail:
            fam = {}
            for r in rows:
                if r["series_key"].endswith(":pct_change"):
                    continue
                k = _family(r["series_key"])
                cur = fam.get(k)
                if cur is None or r["last_period"] > cur["last_period"]:
                    fam[k] = {**r, "series_key": k, "n_series": 0}
                fam[k]["n_series"] = (cur["n_series"] if cur else 0) + 1
            rows = list(fam.values())
        out = []
        for r in rows:
            age = (today - r["last_period"]).days
            f = r["native_freq"]
            extra = LAG_EXTRA.get(r["dataset"], 0)
            state = ("STALE" if age > STALE_AFTER[f] + extra
                     else "DUE" if age > DUE_AFTER[f] + extra else "FRESH")
            out.append({**r, "days_old": age, "state": state})
        return sorted(out, key=lambda r: r["series_key"])
    finally:
        con.close()


@app.get("/v1/coverage")
def coverage(key: str | None = None):
    con = db()
    try:
        sql, params = "SELECT * FROM v_coverage", []
        if key:
            sql += " WHERE series_key = ?"; params.append(key)
        return _rows(con, sql + " ORDER BY series_key", params)
    finally:
        con.close()


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "dashboard" / "index.html")
