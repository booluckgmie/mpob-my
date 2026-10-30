import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FX = ROOT / "tests" / "fixtures"

import common, crawl, price_daily, reconcile, ticker  # noqa: E402


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "SILVER", tmp_path)       # keep hydrate/save off the real lake
    c = common.connect(tmp_path / "t.duckdb")
    yield c
    c.close()


def test_price_parse_real_page():
    df = price_daily.parse_html((FX / "cpo_2025.html").read_text(), 2025)
    assert len(df) == 365
    assert set(df["status"]) == {"actual", "PH", "NT"}
    assert df.loc[df["status"] != "actual", "price"].isna().all()
    assert price_daily.validate(df, 2025) == []


def test_price_empty_body_is_no_data_not_error():
    assert price_daily.parse_html("", 2020).empty


def test_price_shape_guard_raises():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((FX / "cpo_2025.html").read_text(), "html.parser")
    row = next(r for r in soup.find("tbody").find_all("tr")
               if r.find("td") and r.find("td").get_text(strip=True).isdigit())
    row.append(soup.new_tag("td"))                       # a 14th column appears
    with pytest.raises(ValueError):
        price_daily.parse_html(str(soup), 2025)


def test_ticker_parse_real_page():
    period, rows = ticker.parse((FX / "ticker.html").read_text())
    assert period == "2026-09" and len(rows) == 8
    by = {r["suffix"]: r for r in rows}
    assert by["cpo_production"]["value"] == 2116950
    assert by["po_export"]["pct"] == -12.13          # "(12.13)" is negative


def test_revisions_append_never_overwrite(con):
    row = dict(series_key="k", period_start="2026-01-01", period_end="2026-01-31", granularity="m",
               value=1.0, unit="t", status="actual")
    common.register_series(con, [dict(series_key="k", dataset="d", measure="m", product="p",
                                      geography="g", unit="t", agg_rule="last", native_freq="monthly")])
    assert common.upsert_obs(con, pd.DataFrame([row]))["inserted"] == 1
    assert common.upsert_obs(con, pd.DataFrame([row]))["unchanged"] == 1
    assert common.upsert_obs(con, pd.DataFrame([{**row, "value": 2.0}]))["revised"] == 1
    assert con.execute("SELECT count(*), max(revision) FROM obs").fetchone() == (2, 2)
    assert con.execute("SELECT value FROM v_obs_latest").fetchone()[0] == 2.0


def test_rollups_and_quarterly_from_monthly(con):
    common.register_series(con, [price_daily.SERIES])
    days = pd.date_range("2026-01-01", "2026-03-31")
    df = pd.DataFrame({"date": days, "price": 4000.0, "status": "actual"})
    df.loc[df["date"].dt.weekday >= 5, ["price", "status"]] = [None, "NT"]
    price_daily.write(df, con)
    q = con.execute("SELECT value, n_months, complete FROM v_quarterly").fetchone()
    assert q == (4000.0, 3, True)                    # NT days must not drag the average to 0
    assert con.execute("SELECT count(*) FROM v_weekly").fetchone()[0] >= 12


def test_reconcile_selftest():
    assert reconcile.selftest() == 0


def test_classify_prefers_specific_family():
    assert crawl.classify("Monthly Export of Oil Palm Products 2025") == "export_monthly"
    assert crawl.classify("Export 2026") is None     # navigation noise


def test_api_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    monkeypatch.setattr(common, "SILVER", tmp_path)
    monkeypatch.setattr(common, "DB_PATH", tmp_path / "api.duckdb")
    monkeypatch.setattr(api, "_CON", None)
    c = common.connect()
    price_daily.write(pd.DataFrame({"date": pd.to_datetime(["2026-01-05"]), "price": [4100.0],
                                    "status": ["actual"]}), c)
    c.close()
    t = TestClient(api.app)
    assert t.get("/health").json() == {"ok": True}
    assert t.get("/v1/series?key=nope").status_code == 404
    d = t.get("/v1/series?key=mpob:cpo_daily&freq=monthly").json()["data"]
    assert d[0]["value"] == 4100.0 and d[0]["status"] == "derived"
    assert t.get("/v1/freshness").json()[0]["state"] in {"FRESH", "DUE", "STALE"}


def test_balance_backs_out_opening_stock():
    import analytics
    b = analytics.balance({"po_stock": {"value": 1100.0, "pct": 10.0}, "cpo_production": {"value": 500.0},
                           "po_import": {"value": 50.0}, "po_export": {"value": 300.0}})
    assert b["opening_stock"] == 1000
    assert b["disappearance"] == 450          # 1000 + 500 + 50 - 1100
    assert b["implied_domestic_use"] == 150
    assert analytics.balance({"po_stock": {"value": 1.0}}) is None


def test_price_stats_changes_and_seasonality():
    import analytics
    days = pd.date_range("2025-01-01", "2026-01-31")
    df = pd.DataFrame({"date": days, "value": [4000.0 + i for i in range(len(days))]})
    s = analytics.price_stats(df)
    assert s["latest"]["value"] == df["value"].iloc[-1]
    assert s["change_pct"]["1m"] > 0 and s["monthly_by_year"][2025][1] < s["monthly_by_year"][2026][1]


def _report(name):
    import bepi_reports
    return bepi_reports.parse_report((FX / name).read_text(errors="replace"))[1]


def test_bepi_production_hierarchy_reconciles():
    df = _report("report_production_2025.html")
    p = df[df.year == 2025].pivot_table(index="label", columns="month", values="value")
    assert (p.loc["P. MALAYSIA"] + p.loc["SABAH/SARAWAK"] - p.loc["MALAYSIA"]).abs().max() == 0
    assert (p.loc["SABAH"] + p.loc["SARAWAK"] - p.loc["SABAH/SARAWAK"]).abs().max() == 0
    assert p.loc["MALAYSIA", 12] == 1829549           # Dec 2025, as printed in the report


def test_bepi_stocks_sections_and_both_years():
    df = _report("report_stocks_2025.html")
    assert set(df.year) == {2024, 2025} and df.month.nunique() == 12
    assert "CRUDE PALM OIL" in set(df.section)
    assert df[(df.label == "TOTAL CRUDE PALM OIL") & (df.year == 2025) & (df.month == 1)]["value"].iloc[0] == 789094


def test_bepi_export_units_and_continuation_rows():
    df = _report("report_export_2025.html")
    cpo = df[(df.label == "CPO") & (df.month == 1)].set_index("unit")["value"]
    assert cpo["tonnes"] == 180019 and cpo["MYR_mil"] == 919.67   # RM row inherits the product above it


def test_bepi_rowmonths_layout():
    import bepi_reports
    html = ("<h1>SUPPLY 2025</h1><table><tr><td rowspan=2>Month</td><td colspan=2>Peninsular</td></tr>"
            "<tr><td>Supply</td><td>Demand</td></tr><tr><td>JANUARY</td><td>7,639,912</td><td>2,906,641</td></tr>"
            + "".join(f"<tr><td>{m}</td><td>1</td><td>2</td></tr>" for m in ["FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE"])
            + "</table>")
    df = bepi_reports.parse_report(html)[1]
    assert len(df) == 12 and set(df.label) == {"Peninsular Supply", "Peninsular Demand"}


def test_bepi_unchanged_report_is_idempotent(con):
    import bepi_reports
    df = _report("report_stocks_2025.html")
    obs, series = bepi_reports.to_obs("stocks", df, "test", "closing_stock")
    common.register_series(con, series)
    assert common.upsert_obs(con, obs)["inserted"] == len(obs)
    again = common.upsert_obs(con, obs)
    assert again["inserted"] == 0 and again["revised"] == 0
    assert len({s["series_key"] for s in series}) == len(series)      # no key collisions


def test_production_band_and_exact_balance():
    import analytics
    idx = pd.date_range("2019-01-01", "2024-03-01", freq="MS")
    s = pd.DataFrame({"period_start": idx, "value": [100.0 + (d.year - 2019) for d in idx]})
    s.loc[s.index[-1], "value"] = 500.0
    out = analytics.production_band(s)
    assert out["position"] == "above band" and out["latest"]["period"] == "2024-03"
    stock = pd.DataFrame({"period_start": pd.to_datetime(["2024-02-01", "2024-03-01"]), "value": [1000.0, 1100.0]})
    b = analytics.exact_balance(stock, production=500.0, imports=50.0, exports=300.0)
    assert b["disappearance"] == 450 and b["implied_domestic_use"] == 150
