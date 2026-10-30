"""Headline lane: MPOB monthly scoreboard from bepi.mpob.gov.my/ticker/infoticker.php.

    python ticker.py            # fetch + load
    python ticker.py --file tests/fixtures/ticker.html
"""
from __future__ import annotations
import argparse, calendar, re
from datetime import date

import pandas as pd
from bs4 import BeautifulSoup

import common

URL = "https://bepi.mpob.gov.my/ticker/infoticker.php"
# (label regex, key suffix, measure, product, unit, agg_rule)
MEASURES = [
    (r"CPO.*Production", "cpo_production", "volume", "CPO", "tonnes", "sum"),
    (r"CPO.*Stock", "cpo_stock", "stock", "CPO", "tonnes", "last"),
    (r"PPO.*Stock", "ppo_stock", "stock", "PPO", "tonnes", "last"),
    (r"PO.*Palm Oil Stock", "po_stock", "stock", "PO", "tonnes", "last"),
    (r"PKO.*Stock", "pko_stock", "stock", "PKO", "tonnes", "last"),
    (r"Palm Oil Export", "po_export", "volume", "PO", "tonnes", "sum"),
    (r"Palm Oil Import", "po_import", "volume", "PO", "tonnes", "sum"),
    (r"Fresh Fruit Bunches", "ffb_price", "price", "FFB", "MYR/t", "avg"),
]


def _num(s: str) -> float | None:
    s = s.strip()
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(re.sub(r"[^\d.]", "", s))
    except ValueError:
        return None
    return -v if neg else v


def parse(html: str) -> tuple[str, list[dict]]:
    """Return ('YYYY-MM', rows). Raises if the period or no measure rows are found."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Performance\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        raise ValueError("period heading not found - layout changed")
    month = list(calendar.month_name).index(m.group(1).title())
    period = f"{m.group(2)}-{month:02d}"

    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) < 2 or _num(cells[1]) is None:
            continue
        for pat, suffix, measure, product, unit, agg in MEASURES:
            if re.search(pat, cells[0], re.I):
                pct = _num(cells[3]) if len(cells) > 3 else None
                rows.append({"suffix": suffix, "measure": measure, "product": product,
                             "unit": unit, "agg": agg, "value": _num(cells[1]), "pct": pct})
                break
    if len(rows) != len(MEASURES):
        raise ValueError(f"expected {len(MEASURES)} measures, parsed {len(rows)}")
    return period, rows


def to_obs(period: str, rows: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    y, mo = map(int, period.split("-"))
    start, end = date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    obs, series = [], []
    for r in rows:
        key = f"mpob:headline:{r['suffix']}"
        series.append({"series_key": key, "dataset": "headline", "measure": r["measure"],
                       "product": r["product"], "geography": "Malaysia", "unit": r["unit"],
                       "agg_rule": r["agg"], "native_freq": "monthly"})
        obs.append({"series_key": key, "value": r["value"], "unit": r["unit"]})
        if r["pct"] is not None:   # MPOB's own reported MoM % change, kept separate
            series.append({"series_key": key + ":pct_change", "dataset": "headline",
                           "measure": "pct_change", "product": r["product"], "geography": "Malaysia",
                           "unit": "%", "agg_rule": "last", "native_freq": "monthly"})
            obs.append({"series_key": key + ":pct_change", "value": r["pct"], "unit": "%"})
    df = pd.DataFrame(obs).assign(period_start=start, period_end=end, granularity="m",
                                  status="actual", coverage=1.0, source_url=URL)
    return df, series


def run(html: str | None = None) -> dict:
    html = html or common.http_get(URL).text
    period, rows = parse(html)
    df, series = to_obs(period, rows)
    con = common.connect()
    common.register_series(con, series)
    stats = common.upsert_obs(con, df)
    common.save_lake(con)
    con.close()
    print(f"headline {period}: {stats}")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    a = ap.parse_args()
    run(open(a.file).read() if a.file else None)
