"""Read API over the lake.   uvicorn api:app --port 8000   (dashboard served at /)"""
from __future__ import annotations
import threading
from datetime import date
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

import common

app = FastAPI(title="MPOB-MY lake API", version="1.0")
VIEWS = {"daily": "v_daily", "weekly": "v_weekly", "monthly": "v_monthly", "quarterly": "v_quarterly"}
# days since newest period_end before a series is flagged STALE
STALE_AFTER = {"daily": 7, "monthly": 45}
DUE_AFTER = {"daily": 4, "monthly": 35}
LAG_EXTRA = {"dosm": 35}    # DOSM publishes ~2 months behind the data month


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


@app.get("/v1/freshness")
def freshness():
    """FRESH / DUE / STALE per series by days since its newest period_end."""
    con = db()
    try:
        today = date.today()
        out = []
        for r in _rows(con, "SELECT series_key, dataset, native_freq, last_period, last_ingested FROM v_coverage"):
            age = (today - r["last_period"]).days
            f = r["native_freq"]
            extra = LAG_EXTRA.get(r["dataset"], 0)
            state = ("STALE" if age > STALE_AFTER[f] + extra
                     else "DUE" if age > DUE_AFTER[f] + extra else "FRESH")
            out.append({**r, "days_old": age, "state": state})
        return out
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
