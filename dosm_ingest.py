"""
OpenDOSM IPI -> obs store (canary series; NOT an independent source).

DOSM derives the palm-oil IPI from MPOB monthly production, so it cannot be ground
truth. If DOSM revises and MPOB did not, one of our parsers is broken (see reconcile.py).

    python dosm_ingest.py            # pull bulk Parquet + load
    python dosm_ingest.py --selftest
"""
from __future__ import annotations
import argparse, io

import pandas as pd

import common

BULK = {
    "ipi": "https://storage.dosm.gov.my/ipi/ipi.parquet",
    "ipi_3d": "https://storage.dosm.gov.my/ipi/ipi_3d.parquet",
}
PALM_GROUPS = {"104"}   # MSIC 2008: vegetable & animal oils and fats
CAVEAT = "index 2015=100; most recent year may be revised"


def pull() -> dict[str, pd.DataFrame]:
    out = {}
    for name, url in BULK.items():
        out[name] = pd.read_parquet(io.BytesIO(common.http_get(url).content))
        out[name].to_parquet(common.RAW / f"dosm_{name}.parquet", index=False)
        print(f"{name}: {len(out[name]):,} rows")
    return out


def _to_obs(df: pd.DataFrame, dataset: str, group: str | None = None) -> pd.DataFrame:
    out = df[df["series"] == "abs"].copy()
    out["date"] = pd.to_datetime(out["date"])
    out["value"] = pd.to_numeric(out["index"], errors="coerce")
    out = out.dropna(subset=["value"]).drop_duplicates("date", keep="last")
    return pd.DataFrame({
        "series_key": f"dosm:ipi:{group or 'total'}",
        "period_start": out["date"].dt.date,
        "period_end": (out["date"] + pd.offsets.MonthEnd(0)).dt.date,
        "granularity": "m", "value": out["value"], "unit": "index_2015_100",
        "status": "actual", "coverage": 1.0, "source_url": f"open.dosm.gov.my/data-catalogue/{dataset}",
    })


def build(frames: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, list[dict]]:
    parts = [_to_obs(frames["ipi"], "ipi")]
    g3 = frames["ipi_3d"]
    g3 = g3[g3["group"].astype(str).isin(PALM_GROUPS)]
    if g3.empty:
        raise ValueError(f"no ipi_3d rows for MSIC groups {PALM_GROUPS}")
    for grp, chunk in g3.groupby("group"):
        parts.append(_to_obs(chunk, "ipi_3d", str(grp)))
    obs = pd.concat(parts, ignore_index=True)
    series = [{"series_key": k, "dataset": "dosm", "measure": "index",
               "product": "palm_oil" if k.endswith(":104") else "all_industry",
               "geography": "Malaysia", "unit": "index_2015_100", "agg_rule": "last",
               "native_freq": "monthly"} for k in obs["series_key"].unique()]
    return obs, series


def run() -> dict:
    obs, series = build(pull())
    con = common.connect()
    common.register_series(con, series)
    stats = common.upsert_obs(con, obs)
    common.save_lake(con)
    con.close()
    print(f"dosm: {stats}")
    return stats


def selftest() -> int:
    fx = pd.DataFrame({"series": ["abs", "abs", "abs", "growth_yoy"],
                       "date": ["2026-07-01", "2026-08-01", "2026-08-01", "2026-08-01"],
                       "group": ["104"] * 4, "index": [131.4, 128.9, 128.9, 4.2]})
    obs = _to_obs(fx, "ipi_3d", "104")
    assert len(obs) == 2 and obs["series_key"].iloc[0] == "dosm:ipi:104"
    assert str(obs["period_end"].iloc[1]) == "2026-08-31"
    print("selftest passed")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    raise SystemExit(selftest() if a.selftest else (run() and 0))
