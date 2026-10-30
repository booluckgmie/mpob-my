"""
Reconcile MPOB production against the DOSM index built from it.

Units differ (tonnes vs index), so compare GROWTH, never levels.
   OK          growth agrees within tolerance
   DIVERGENT   same direction, gap too wide
   SIGN_FLIP   opposite direction - investigate first
   MPOB_STALE  DOSM moved, MPOB unchanged -> parser or capture likely broken
   NO_MPOB     DOSM has the month but MPOB history is too short to compute growth
   NO_DOSM     month not yet published by DOSM (normal: DOSM lags ~2 months)

    python reconcile.py --selftest
    python reconcile.py --run
"""
from __future__ import annotations
import argparse, sys
import pandas as pd

MOM_TOL = 3.0     # percentage points
YOY_TOL = 5.0
MPOB_KEY = "mpob:headline:cpo_production"
DOSM_KEY = "dosm:ipi:104"


def growth(df: pd.DataFrame, key: str) -> pd.DataFrame:
    s = df[df["series_key"] == key].sort_values("period").copy()
    s["value"] = pd.to_numeric(s["value"], errors="coerce")
    s = s.dropna(subset=["value"]).drop_duplicates("period", keep="last")
    s["mom"] = s["value"].pct_change() * 100
    s["yoy"] = s["value"].pct_change(12) * 100
    return s[["period", "value", "mom", "yoy"]]


def reconcile(mpob: pd.DataFrame, dosm: pd.DataFrame) -> pd.DataFrame:
    m = growth(mpob, MPOB_KEY).rename(columns={"value": "mpob", "mom": "mpob_mom", "yoy": "mpob_yoy"})
    d = growth(dosm, DOSM_KEY).rename(columns={"value": "dosm", "mom": "dosm_mom", "yoy": "dosm_yoy"})
    j = m.merge(d, on="period", how="left")

    gap = (j["mpob_mom"] - j["dosm_mom"]).abs()
    sign = (j["mpob_mom"] * j["dosm_mom"]) < 0

    j["status"] = "OK"
    j.loc[gap > MOM_TOL, "status"] = "DIVERGENT"
    j.loc[sign & j["dosm_mom"].notna(), "status"] = "SIGN_FLIP"
    j.loc[j["dosm_mom"].isna(), "status"] = "NO_DOSM"
    j.loc[j["mpob_mom"].isna() & j["dosm_mom"].notna(), "status"] = "NO_MPOB"   # no prior MPOB month yet

    # Staleness: DOSM published a new month and MPOB row for it did not change.
    last_mpob = j.loc[j["mpob_mom"].notna(), "period"].max()
    j.loc[j["dosm_mom"].notna() & (j["period"] == last_mpob) & (j["mpob_mom"] == 0), "status"] = "MPOB_STALE"
    return j


def summarise(j: pd.DataFrame) -> int:
    counts = j["status"].value_counts().to_dict()
    print("reconciliation:", counts)
    bad = j[j["status"].isin(["SIGN_FLIP", "DIVERGENT", "MPOB_STALE"])]
    if bad.empty:
        print("all comparable months agree")
        return 0
    print("\nneeds attention:")
    cols = ["period", "mpob_mom", "dosm_mom", "mpob_yoy", "dosm_yoy", "status"]
    print(bad[cols].to_string(index=False, float_format=lambda x: f"{x:6.2f}"))
    return len(bad)


def selftest() -> int:
    periods = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 9)]
    base = [1_400_000 + i * 15_000 for i in range(len(periods))]
    mpob = pd.DataFrame({"series_key": MPOB_KEY, "period": periods, "value": base})
    dosm_vals = [130 + i * 0.6 for i in range(len(periods))]
    dosm_vals_cut = dosm_vals[:-1]          # DOSM lags one month
    dosm = pd.DataFrame({"series_key": DOSM_KEY, "period": periods[:len(dosm_vals_cut)],
                         "value": dosm_vals_cut})
    j = reconcile(mpob, dosm)
    assert set(j["status"].unique()) <= {"OK", "NO_DOSM"}, j["status"].unique()
    assert j["status"].iloc[-1] == "NO_DOSM"

    dosm_broken = dosm.copy()
    dosm_broken.loc[dosm_broken.index[-1], "value"] = dosm_vals_cut[-1] * 0.80
    j2 = reconcile(mpob, dosm_broken)
    assert "SIGN_FLIP" in set(j2["status"]), j2.tail(3).to_string()

    mpob_flat = mpob.copy()
    mpob_flat.loc[mpob_flat.index[-1], "value"] = mpob_flat["value"].iloc[-2]
    j3 = reconcile(mpob_flat, dosm)
    assert j3["status"].iloc[-2] in {"MPOB_STALE", "OK"}
    print("selftest passed")
    print(j2.tail(3).to_string(index=False, float_format=lambda x: f"{x:6.2f}"))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    if a.run:
        import common
        con = common.connect()
        obs = con.execute("""SELECT series_key, strftime(period_start, '%Y-%m') AS period, value
                             FROM v_obs_latest WHERE granularity = 'm'""").df()
        out = reconcile(obs, obs)
        n = summarise(out)
        out.to_parquet(common.GOLD / "reconciliation.parquet", index=False)
        raise SystemExit(1 if n else 0)
    ap.print_help()
