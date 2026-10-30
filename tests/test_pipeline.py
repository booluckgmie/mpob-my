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
