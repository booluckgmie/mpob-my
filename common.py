"""Shared paths, HTTP and the revision-aware observation store.

Single source of truth = data/lake/silver/obs.parquet (+ dim_series.parquet).
data/mpob.duckdb is a disposable cache rebuilt from those files.
"""
from __future__ import annotations
import os, sys, time
from pathlib import Path

import duckdb
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
SILVER = DATA / "lake" / "silver"
GOLD = DATA / "lake" / "gold"
DB_PATH = DATA / "mpob.duckdb"
for _d in (RAW, SILVER, GOLD):
    _d.mkdir(parents=True, exist_ok=True)

CONTACT = os.environ.get("MPOB_CONTACT", "set-MPOB_CONTACT-env-var")
HEADERS = {"User-Agent": f"mpob-my-pipeline/1.0 (research; contact: {CONTACT})"}

OBS_COLS = ["series_key", "period_start", "period_end", "granularity", "value",
            "unit", "status", "coverage", "source_url", "released_at"]
SERIES_COLS = ["series_key", "dataset", "measure", "product", "geography",
               "unit", "agg_rule", "native_freq"]


def http_get(url: str, retries: int = 4, timeout: int = 30) -> requests.Response:
    last: Exception | None = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            wait = 2 ** i
            print(f"retry {url} in {wait}s ({e})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {retries} tries: {url}") from last


def connect(path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Open the cache DB, apply the schema, and hydrate it from the lake if empty."""
    con = duckdb.connect(str(path or DB_PATH))
    con.execute((ROOT / "schema.sql").read_text())
    con.execute((ROOT / "coverage.sql").read_text())
    if con.execute("SELECT count(*) FROM obs").fetchone()[0] == 0:
        obs_f, dim_f = SILVER / "obs.parquet", SILVER / "dim_series.parquet"
        if dim_f.exists():
            con.execute(f"INSERT INTO dim_series SELECT * FROM read_parquet('{dim_f}')")
        if obs_f.exists():
            con.execute(f"INSERT INTO obs SELECT * FROM read_parquet('{obs_f}')")
    return con


def register_series(con, rows: list[dict]) -> None:
    for r in rows:
        con.execute("INSERT OR REPLACE INTO dim_series VALUES (?,?,?,?,?,?,?,?)",
                    [r[c] for c in SERIES_COLS])


def upsert_obs(con, df: pd.DataFrame) -> dict:
    """Append observations without ever overwriting history.

    A row is inserted only when it is new or its value/status differs from the
    latest stored revision; then it gets revision = latest + 1.
    """
    if df.empty:
        return {"inserted": 0, "revised": 0, "unchanged": 0}
    df = df.copy()
    for c in OBS_COLS:
        if c not in df:
            df[c] = None
    df["period_start"] = pd.to_datetime(df["period_start"]).dt.date
    df["period_end"] = pd.to_datetime(df["period_end"]).dt.date
    df = df.drop_duplicates(["series_key", "period_start", "granularity"], keep="last")
    con.register("incoming", df[OBS_COLS])
    con.execute("""
        CREATE OR REPLACE TEMP TABLE latest AS
        SELECT series_key, period_start, granularity, value, status, revision FROM (
            SELECT *, row_number() OVER (PARTITION BY series_key, period_start, granularity
                                         ORDER BY revision DESC) rn FROM obs) WHERE rn = 1""")
    stats = con.execute("""
        SELECT count(*) FILTER (l.revision IS NULL),
               count(*) FILTER (l.revision IS NOT NULL AND (i.value IS DISTINCT FROM l.value
                                                            OR i.status IS DISTINCT FROM l.status)),
               count(*) FILTER (l.revision IS NOT NULL AND NOT (i.value IS DISTINCT FROM l.value
                                                                OR i.status IS DISTINCT FROM l.status))
        FROM incoming i LEFT JOIN latest l USING (series_key, period_start, granularity)""").fetchone()
    con.execute("""
        INSERT INTO obs (series_key, period_start, period_end, granularity, value, unit,
                         status, revision, coverage, source_url, released_at)
        SELECT i.series_key, i.period_start, i.period_end, i.granularity, i.value, i.unit,
               i.status, coalesce(l.revision, 0) + 1, coalesce(i.coverage, 1.0),
               i.source_url, i.released_at
        FROM incoming i LEFT JOIN latest l USING (series_key, period_start, granularity)
        WHERE l.revision IS NULL OR i.value IS DISTINCT FROM l.value
                                 OR i.status IS DISTINCT FROM l.status""")
    con.unregister("incoming")
    return {"inserted": stats[0], "revised": stats[1], "unchanged": stats[2]}


def save_lake(con) -> None:
    """Persist the store back to Parquet (the committed source of truth)."""
    con.execute(f"COPY (SELECT * FROM obs ORDER BY series_key, period_start, granularity, revision) "
                f"TO '{SILVER / 'obs.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT * FROM dim_series ORDER BY series_key) "
                f"TO '{SILVER / 'dim_series.parquet'}' (FORMAT parquet)")
    for view in ("v_weekly", "v_monthly", "v_quarterly"):
        con.execute(f"COPY (SELECT * FROM {view}) TO '{GOLD / (view[2:] + '.parquet')}' (FORMAT parquet)")
