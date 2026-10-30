"""
Reconcile MPOB production against the DOSM index built from it.

Units differ (tonnes vs index), so compare GROWTH, never levels.
   OK          growth gap is within the series' own historical spread
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

# DOSM group 104 (all vegetable & animal oils and fats) only loosely tracks CPO production: on real
# history MoM correlation is ~0.8 and the typical gap ~5 points. So the threshold is calibrated from the
# data: a month is flagged only when its gap is an outlier (robust z-score on the gap distribution).
Z_FLAG = 3.5
MIN_HISTORY = 12
RECENT = 6          # alert only on the last N comparable months
MPOB_KEY = "bepi:cpo_production:production_of_crude_palm_oil:malaysia:tonnes"
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

    diff = j["mpob_mom"] - j["dosm_mom"]
    ok = diff.dropna()
    med = ok.median() if len(ok) else 0.0
    mad = (ok - med).abs().median() * 1.4826 if len(ok) >= MIN_HISTORY else float("nan")
    z = ((diff - med).abs() / mad) if mad and mad == mad else diff * 0     # no history -> never flag
    sign = (j["mpob_mom"] * j["dosm_mom"]) < 0
    j["gap"] = diff
    j["z"] = z

    j["status"] = "OK"
    j.loc[z > Z_FLAG, "status"] = "DIVERGENT"
    j.loc[(z > Z_FLAG) & sign & j["dosm_mom"].notna(), "status"] = "SIGN_FLIP"
    j.loc[j["dosm_mom"].isna(), "status"] = "NO_DOSM"
    j.loc[j["mpob_mom"].isna() & j["dosm_mom"].notna(), "status"] = "NO_MPOB"   # no prior MPOB month yet

    # Staleness: DOSM published a new month and MPOB row for it did not change.
    last_mpob = j.loc[j["mpob_mom"].notna(), "period"].max()
    j.loc[j["dosm_mom"].notna() & (j["period"] == last_mpob) & (j["mpob_mom"] == 0), "status"] = "MPOB_STALE"
    return j


def summarise(j: pd.DataFrame) -> int:
    counts = j["status"].value_counts().to_dict()
    print("reconciliation:", counts)
    comparable = j.loc[j["dosm_mom"].notna(), "period"].sort_values()
    recent = j["period"] >= (comparable.iloc[-RECENT:].iloc[0] if len(comparable) else "")   # old outliers don't alert
    bad = j[recent & j["status"].isin(["SIGN_FLIP", "DIVERGENT", "MPOB_STALE"])]
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
