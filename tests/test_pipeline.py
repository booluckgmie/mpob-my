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


def test_spatial_endpoint_shares_yoy_and_boundaries(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api, json
    monkeypatch.setattr(common, "SILVER", tmp_path)
    monkeypatch.setattr(common, "DB_PATH", tmp_path / "sp.duckdb")
    monkeypatch.setattr(api, "_CON", None)
    c = common.connect()
    pre = "bepi:cpo_production:production_of_crude_palm_oil:"
    rows, series = [], []
    for st, now, before in [("johor", 300.0, 250.0), ("sabah", 500.0, 500.0), ("malaysia", 800.0, 750.0)]:
        series.append(dict(series_key=pre + st + ":tonnes", dataset="bepi", measure="volume", product=st,
                           geography="", unit="tonnes", agg_rule="sum", native_freq="monthly"))
        for per, v in (("2025-09-01", before), ("2026-09-01", now)):
            rows.append(dict(series_key=pre + st + ":tonnes", period_start=per, period_end=per, granularity="m",
                             value=v, unit="tonnes", status="actual"))
    common.register_series(c, series)
    common.upsert_obs(c, pd.DataFrame(rows))
    c.close()
    t = TestClient(api.app)
    d = t.get("/v1/spatial?metric=production").json()
    by = {s["state"]: s for s in d["states"]}
    assert d["period"] == "2026-09" and by["johor"]["share_pct"] == 37.5 and by["johor"]["yoy_pct"] == 20.0
    assert by["malaysia"]["aggregate"] and t.get("/v1/spatial?metric=oer").status_code == 404
    g = t.get("/boundaries/my_states.geojson").json()
    assert {f["properties"]["bepi"] for f in g["features"]} >= {"JOHOR", "SABAH", "OTHER STATES"}


def test_pdf_yield_and_area_parse_real_extracts():
    import pdf_reports
    y, annual = pdf_reports.parse_pdf_text((FX / "yield_ffb_2025.txt").read_text())
    assert not annual and set(y.year) == {2024, 2025} and y.month.nunique() == 12
    my = y[(y.label == "MALAYSIA") & (y.year == 2025)].set_index("month")["value"]
    assert my[1] == 1.15 and abs(my.sum() - 17.77) < 0.05           # monthly yields add up to the printed annual figure
    a, annual = pdf_reports.parse_pdf_text((FX / "area_2025.txt").read_text())
    assert annual and set(a.year) == {2025}
    tot = a[a.section == "total"].set_index("label")["value"]
    states = tot.drop(["MALAYSIA", "P. MALAYSIA", "SABAH/SARAWAK"])
    assert tot["MALAYSIA"] == 5700050 and states.sum() == tot["MALAYSIA"]
    assert "NEGERI SEMBILAN" in tot.index                              # spaces survive extraction


def test_annual_obs_use_year_granularity(con):
    import bepi_reports, pdf_reports
    a, annual = pdf_reports.parse_pdf_text((FX / "area_2025.txt").read_text())
    obs, series = bepi_reports.to_obs("area", a, "test", "planted_area", annual)
    assert set(obs.granularity) == {"y"} and str(obs.period_start.iloc[0]) == "2025-01-01"
    assert {s["native_freq"] for s in series} == {"annual"}


def test_pdf_area_old_layout_split_digits_and_aliases():
    import pdf_reports
    text = ("Oil Palm Planted Area By States As At December 2013 (Hectares)\nState Mature % Immature % Total %\n"
            "Johore 639,946 8 7.58 90,748 1 2.42 730,694 1 3.97\nMalacca 49,635 9 4.18 3,069 5 .82 52,704 1 .01\n"
            "Perlis 190 6 8.35 88 3 1.65 278 0 .01\nMalaysia 4,526,089 8 6.55 703,650 1 3.45 5,229,739 1 00.00\n"
            "Bogus 100 1.0 200 2.0 999 3.0")
    df, annual = pdf_reports.parse_pdf_text(text)
    tot = df[df.section == "total"].set_index("label")["value"]
    assert annual and tot["JOHOR"] == 730694 and tot["MELAKA"] == 52704 and tot["PERLIS"] == 278
    assert "BOGUS" not in tot.index                                   # matured + immature must equal total


def test_pdf_yield_year_row_after_state_caption():
    import pdf_reports
    text = ("AVERAGE FFB YIELD\nJAN-JUNE 2020 & 2019 (TONNES/HECTARE)\nJan Feb Mar April May June January-June\nState\n"
            "2020 2019 2020 2019 2020 2019 2020 2019 2020 2019 2020 2019 2020 2019\n"
            "N. Sembilan 0.76 1.56 1.07 1.48 1.23 1.69 1.53 1.80 1.54 1.83 1.77 1.58 7.90 9.94\n"
            "P. Malaysia 0.96 1.62 1.15 1.44 1.30 1.55 1.57 1.59 1.49 1.54 1.84 1.45 8.31 9.19")
    df, _ = pdf_reports.parse_pdf_text(text)
    assert set(df.label) == {"NEGERI SEMBILAN", "P. MALAYSIA"} and set(df.year) == {2019, 2020}


def test_pdf_area_integer_percentages():
    import pdf_reports
    text = ("OIL PALM PLANTED AREA BY STATE AS AT DECEMBER 2016 (HECTARES)\nState Mature % Immature % Total %\n"
            "Johore 678,508 91 67,122 9 745,630 13\nPerlis 603 92.5 49 7.5 652 0\n"
            "MALAYSIA 5,001,438 87.2 736,547 12.8 5,737,985 100")
    df, _ = pdf_reports.parse_pdf_text(text)
    tot = df[df.section == "total"].set_index("label")["value"]
    assert tot["JOHOR"] == 745630 and tot["PERLIS"] == 652 and tot["MALAYSIA"] == 5737985


def test_pdf_yield_unpublished_months_keep_their_columns():
    import pdf_reports
    text = ("AVERAGE FFB YIELD\nJULY-DECEMBER 2025 & 2026 (TONNES/HECTARE)\nState July Aug Sept Oct Nov Dec January-September\n"
            "2025 2026 2025 2026 2025 2026 2025 2026 2025 2026 2025 2026 2025 2026\n"
            "Johor 1.89 1.79 1.94 1.78 1.85 2.15 1.97 1.95 1.84 14.45 14.18")
    df, _ = pdf_reports.parse_pdf_text(text)
    d = df.set_index(["year", "month"])["value"]
    assert d[(2026, 9)] == 2.15 and d[(2025, 10)] == 1.97 and d[(2025, 12)] == 1.84
    assert (2026, 10) not in d.index and len(df) == 9              # October-December 2026 are not published yet


def test_overview_kpi_and_plain_insights():
    import analytics
    idx = pd.date_range("2025-01-01", "2026-09-01", freq="MS")
    df = pd.DataFrame({"period_start": idx, "value": [1_000_000 + 10_000 * i for i in range(len(idx))]})
    k = analytics.kpi("production", "Production", "plain", "million tonnes", df, scale=1e-6)
    assert k["period"] == "2026-09" and k["period_label"] == "September 2026" and len(k["spark"]) == 21
    assert k["yoy_pct"] is not None and k["mom_pct"] is not None
    text = analytics.insights({"production": k}, {"position": "above band"}, None)[0]["text"]
    assert "September 2026" in text and "higher than usual" in text
    assert "good" not in text and "bad" not in text                  # facts and direction only


def test_issues_endpoint_reads_readme_table():
    import api
    rows = api.issues()
    assert len(rows) >= 15 and rows[0]["n"] == 1 and "`" not in rows[0]["issue"] and rows[0]["where"]
    assert all(r["title"] and len(r["title"]) <= 95 and not r["title"].endswith("(P") for r in rows)


def test_destination_reports_are_classified_and_generic_parse_handles_countries():
    import bepi_reports, crawl
    assert crawl.classify("Export of Palm Oil by Destinations 2018") == "export_destination"
    assert crawl.classify("Export of Palm Oil to Major Destinations") == "export_destination"
    assert crawl.classify("Export of Palm Kernel Oil By Destination 2016") == "export_destination_pko"
    assert crawl.classify("Palm Oil Export by Major Ports 2025") == "export_ports"
    html = ("<h1>EXPORT OF PALM OIL TO MAJOR DESTINATIONS - 2018</h1><table><tr><td>COUNTRY</td>"
            + "".join(f"<td>{m}</td>" for m in ["JAN", "FEB", "MAR"]) + "<td>JAN - MAR</td></tr>"
            "<tr><td>CHINA</td><td>1,000</td><td>2,000</td><td>3,000</td><td>6,000</td></tr>"
            "<tr><td>INDIA</td><td>500</td><td>-</td><td>700</td><td>1,200</td></tr></table>")
    df = bepi_reports.parse_report(html)[1]
    assert set(df.label) == {"CHINA", "INDIA"} and df[(df.label == "INDIA") & (df.month == 3)]["value"].iloc[0] == 700


def test_source_page_saying_information_is_being_updated_is_no_data_not_failure():
    import bepi_reports
    import pytest
    with pytest.raises(bepi_reports.NoData):
        bepi_reports.parse_report("<html>Currently, this information is being updated. Please try again later.</html>")


def test_ports_report_labels_and_the_two_others_rows():
    import bepi_reports
    df = _report("report_ports_2014.html")
    assert set(df.unit) == {"tonnes"} and "" not in set(df.label)          # port names are labels, not units
    out, series = bepi_reports.to_obs("export_ports", df, "test", "ports")
    jan = df[df.month == 1].assign(label=lambda d: d.label.replace(bepi_reports.RENAMES["export_ports"])).set_index("label")["value"]
    assert jan["BUTTERWORTH"] + jan["PORT KLANG"] + jan["PASIR GUDANG"] + jan["OTHERS (P. MALAYSIA)"] == jan["P. MALAYSIA"]
    assert abs(jan["LAHAD DATU"] + jan["SANDAKAN"] + jan["OTHERS (SABAH/SARAWAK)"] - jan["SABAH/SARAWAK"]) <= 2
    assert jan["P. MALAYSIA"] + jan["SABAH/SARAWAK"] == jan["MALAYSIA"]
    keys = {s["series_key"] for s in series}
    assert len(keys) == len(series)                                         # nothing collapses into one series
    assert any(k.endswith(":others_p_malaysia:tonnes") for k in keys) and any(k.endswith(":others_sabah_sarawak:tonnes") for k in keys)
    assert any(":p_malaysia:" in k for k in keys) and not any(":pen_malaysia:" in k for k in keys)
