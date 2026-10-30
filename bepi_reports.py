"""BEPI statistic reports -> obs store.

Every BEPI "dataset" page embeds an HTML report (`/stat/web_report1.php?val=...`), not an Excel
file. Two layouts exist:
  paired : half-year tables, months across, a row of years (2024 | 2025 per month), section rows
  wide   : one table, months across, rows of (product, unit, values...) for one year

    python bepi_reports.py --family stocks --since 2024
    python bepi_reports.py --p0 --since 2026        # core families, current year (cheap)
    python bepi_reports.py --all --since 2023
    python bepi_reports.py --selftest
"""
from __future__ import annotations
import argparse, calendar, hashlib, json, re, sys, time
from datetime import date
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

import common, crawl

MONTHS = {m[:3].upper(): i for i, m in enumerate(calendar.month_name) if m}
NUM = re.compile(r"^\(?-?[\d,]+(\.\d+)?\)?$")
UNITS = {"TONNES": "tonnes", "RM MIL": "MYR_mil", "RM MILLION": "MYR_mil", "HECTARES": "ha",
         "HA": "ha", "RM/TONNE": "MYR/t", "RM": "MYR", "%": "%", "PERCENT": "%"}
# how a family's monthly values roll up (stocks are snapshots, flows add, rates average)
AGG = {"stocks": "last", "refinery_stocks": "last", "oer": "avg", "yield": "sum", "prices_weekly": "avg",
       "area": "last", "seeds": "sum"}
P0 = ["export_monthly", "stocks", "cpo_production"]
# families whose reports are not parsed correctly yet (never loaded: bad data is worse than none)
UNSUPPORTED = {"summary_annual": "labels embed previous-period values; layout not parsed"}


UNSUPPORTED_TABLES = {"sales_own_use_and_closing_stock_of_oil_palm_seedlings"}   # leading number lands in the label


class NoData(Exception):
    """The source page itself says it has no data (not a parsing failure)."""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _num(s: str) -> float | None:
    s = s.strip().replace("\xa0", "")
    if not NUM.match(s):
        return None
    neg = s.startswith("(") or s.startswith("-")
    v = float(re.sub(r"[^\d.]", "", s))
    return -v if neg else v


def _month(cell: str) -> int | None:
    c = cell.strip().upper()
    if re.search(r"\d|-|TOTAL|AVERAGE", c):
        return None
    return MONTHS.get(c[:3]) if c[:3] in MONTHS else None


def _unit(text: str) -> str | None:
    m = re.search(r"\(([^)]+)\)", text)
    return UNITS.get(m.group(1).strip().upper()) if m else None


def _grid(t) -> list[list[str]]:
    """Table -> rectangular text grid, honouring colspan and rowspan."""
    grid: list[list[str]] = []
    carry: dict[int, tuple[int, str]] = {}          # column -> (rows left, text)
    for tr in t.find_all("tr"):
        row: list[str] = []
        col = 0
        cells = iter(tr.find_all(["td", "th"]))
        while True:
            if col in carry:
                left, txt = carry[col]
                row.append(txt)
                carry[col] = (left - 1, txt) if left > 1 else None
                if carry[col] is None:
                    del carry[col]
                col += 1
                continue
            c = next(cells, None)
            if c is None:
                break
            txt = c.get_text(" ", strip=True)
            span, rspan = int(c.get("colspan") or 1), int(c.get("rowspan") or 1)
            for k in range(span):
                row.append(txt)
                if rspan > 1:
                    carry[col + k] = (rspan - 1, txt)
            col += span
        grid.append(row)
    return grid


def _parse_rowmonths(t, year: int, unit: str) -> list[dict]:
    """Months run DOWN the first column; header rows (e.g. region, then measure) sit above."""
    g = _grid(t)
    first = next((i for i, r in enumerate(g) if r and _month(r[0]) is not None), None)
    if first is None:
        return []
    hdr = g[:first]
    width = max(len(h) for h in hdr)
    cols = [" ".join(dict.fromkeys(h[j] for h in hdr if j < len(h) and h[j])) for j in range(width)]
    out = []
    for r in g[first:]:
        m = _month(r[0]) if r else None
        if m is None:
            continue
        for j in range(1, min(len(r), width)):
            v = _num(r[j]) if r[j].strip() not in ("-", "") else None
            if v is not None:
                out.append(dict(section="", label=cols[j], unit=unit, year=year, month=m, value=v))
    return out


def parse_report(html: str) -> tuple[str, pd.DataFrame]:
    """Return (title, long df[section,label,unit,year,month,value]). Raises on unknown shape."""
    soup = BeautifulSoup(html, "html.parser")
    title = " ".join(soup.get_text(" ", strip=True).split())[:120]
    if re.search(r"currently, this information is being updated", title, re.I):
        raise NoData("source reports: information is being updated")
    rows_out: list[dict] = []
    tables = soup.find_all("table")
    for t in tables:
        trs = t.find_all("tr")
        cells = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in trs]
        hdr = next((i for i, r in enumerate(cells) if sum(_month(c) is not None for c in r) >= 3), None)
        if hdr is None:
            ym = re.search(r"\b(19|20)\d\d\b", title)
            if ym and sum(_month(r[0]) is not None for r in cells if r) >= 6:      # months down the rows
                heading = t.find_previous(string=re.compile(r"\d{4}"))
                rows_out += _parse_rowmonths(t, int(ym.group(0)), _unit(heading or "") or "count")
            continue
        months = [m for m in (_month(c) for c in cells[hdr]) if m]
        nm = len(months)
        heading = t.find_previous(string=re.compile(r"\d{4}"))
        unit_default = _unit(heading or "") or "tonnes"
        years_row = cells[hdr + 1] if hdr + 1 < len(cells) else []
        years = [int(y) for y in years_row if re.fullmatch(r"(19|20)\d\d", y)]
        paired = len(years) >= 2 * nm
        if not paired:
            ym = re.search(r"\b(19|20)\d\d\b", title)
            if not ym:
                raise ValueError("wide report without a year in the title")
            year = int(ym.group(0))
        section, product = "", ""
        start = hdr + (2 if paired else 1)
        for tr, r in zip(trs[start:], cells[start:]):
            if not r or not any(r):
                continue
            if len(r) == 1:                                   # section header row
                section = r[0].strip()
                continue
            nums = [(_num(c) if c.strip() not in ("-", "") else None) for c in r]
            need = (2 * nm + 2) if paired else (nm + 1)
            if len(r) < need + 0:
                continue
            lead = r[: len(r) - need] if len(r) > need else r[:1]
            vals = nums[len(r) - need:] if len(r) >= need else []
            lead = [c for c in lead if c.strip()]
            if not lead and not paired:
                continue
            if paired:
                label, unit = lead[0], unit_default
            else:                                             # (product, unit) or (unit,) continuation
                if len(lead) >= 2:
                    product, unit = lead[0], UNITS.get(lead[1].upper(), slug(lead[1]))
                    label = product
                else:
                    unit = UNITS.get(lead[0].upper(), slug(lead[0]))
                    label = product
            for i in range(nm):
                if paired:
                    for j in range(2):
                        v = vals[2 * i + j]
                        if v is not None:
                            rows_out.append(dict(section=section, label=label, unit=unit,
                                                 year=years[2 * i + j], month=months[i], value=v))
                else:
                    v = vals[i]
                    if v is not None:
                        rows_out.append(dict(section=section, label=label, unit=unit,
                                             year=year, month=months[i], value=v))
    if not rows_out:
        raise ValueError("no data rows parsed - layout changed")
    df = pd.DataFrame(rows_out).drop_duplicates(["section", "label", "unit", "year", "month"], keep="last")
    return title, df


def to_obs(family: str, df: pd.DataFrame, source: str, table: str = "", annual: bool = False) -> tuple[pd.DataFrame, list[dict]]:
    d = df.copy()
    d["series_key"] = ("bepi:" + family + ":" + table + ":" + d["section"].map(slug) + ":" + d["label"].map(slug) + ":" + d["unit"].map(slug)
                       ).str.replace(r":{2,}", ":", regex=True)
    if annual:                                        # point-in-time annual figures (e.g. planted area at December)
        d["period_start"] = [date(int(y), 1, 1) for y in d["year"]]
        d["period_end"] = [date(int(y), 12, 31) for y in d["year"]]
    else:
        d["period_start"] = [date(int(y), int(m), 1) for y, m in zip(d["year"], d["month"])]
        d["period_end"] = [date(int(y), int(m), calendar.monthrange(int(y), int(m))[1]) for y, m in zip(d["year"], d["month"])]
    obs = d.assign(granularity="y" if annual else "m", status="actual", coverage=1.0, source_url=source)[
        ["series_key", "period_start", "period_end", "granularity", "value", "unit", "status", "coverage", "source_url"]]
    agg = AGG.get(family, "sum")
    series = [{"series_key": k, "dataset": "bepi:" + family, "measure": "stock" if agg == "last" else "volume",
               "product": g.iloc[0]["label"], "geography": g.iloc[0]["section"] or "Malaysia",
               "unit": g.iloc[0]["unit"], "agg_rule": agg, "native_freq": "annual" if annual else "monthly"}
              for k, g in d.groupby("series_key")]
    return obs, series


def report_urls(article_url: str) -> list[str]:
    html = common.http_get(article_url).text
    return [urljoin(article_url, i["src"]) for i in BeautifulSoup(html, "html.parser").find_all("iframe")
            if "web_report" in (i.get("src") or "")]


def sync(families: list[str], since: int, con=None) -> dict:
    own = con is None
    con = con or common.connect()
    families = [f for f in families if f not in UNSUPPORTED]
    rows = [r for r in crawl.harvest() if r["family"] in families and r["year"] and r["year"] >= since and not r["is_file"]]
    stats = {"inserted": 0, "revised": 0, "unchanged": 0, "failed": 0, "reports": 0}
    seen_urls: set[str] = set()
    for r in sorted(rows, key=lambda r: (r["family"], r["year"])):
        table = slug(re.sub(r"\b(19|20)\d\d\b", "", r["title"]))
        if table in UNSUPPORTED_TABLES:
            continue
        try:
            for url in report_urls(r["url"]):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                time.sleep(2)
                html = common.http_get(url).text
                (common.RAW / "bepi").mkdir(exist_ok=True)
                digest = hashlib.sha256(html.encode()).hexdigest()
                raw = common.RAW / "bepi" / f"{r['family']}_{r['year']}_{digest[:10]}.html"
                raw.write_text(html)
                raw.with_suffix(".json").write_text(json.dumps(
                    {"family": r["family"], "year": r["year"], "table": table, "url": url, "title": r["title"]}))
                _, df = parse_report(html)
                obs, series = to_obs(r["family"], df, url, table)
                common.register_series(con, series)
                s = common.upsert_obs(con, obs)
                for k in ("inserted", "revised", "unchanged"):
                    stats[k] += s[k]
                stats["reports"] += 1
                print(f"{r['family']} {r['year']}: {len(df)} values, {s}")
        except NoData as e:
            stats["no_data"] = stats.get("no_data", 0) + 1
            print(f"NO DATA {r['family']} {r['year']}: {e}", file=sys.stderr)
        except Exception as e:                  # quarantine: one bad report must not block the rest
            stats["failed"] += 1
            print(f"FAILED {r['family']} {r['year']} ({r['url']}): {e}", file=sys.stderr)
        time.sleep(2)
    if own:
        common.save_lake(con); con.close()
    return stats


def rebuild(con=None) -> dict:
    """Offline: re-parse every saved raw report (data/raw/bepi/*.html + .json sidecar) with the current parser."""
    own = con is None
    con = con or common.connect()
    stats = {"inserted": 0, "revised": 0, "unchanged": 0, "failed": 0, "reports": 0}
    for meta_f in sorted((common.RAW / "bepi").glob("*.json")):
        m = json.loads(meta_f.read_text())
        try:
            if m.get("kind") == "pdf":
                import pdf_reports
                df, annual = pdf_reports.parse_pdf_text(pdf_reports.pdf_text(meta_f.with_suffix(".pdf").read_bytes()))
            else:
                (_, df), annual = parse_report(meta_f.with_suffix(".html").read_text()), False
            obs, series = to_obs(m["family"], df, m["url"], m["table"], annual)
            common.register_series(con, series)
            for k, v in common.upsert_obs(con, obs).items():
                stats[k] += v
            stats["reports"] += 1
        except Exception as e:
            stats["failed"] += 1
            print(f"FAILED {meta_f.name}: {e}", file=sys.stderr)
    if own:
        common.save_lake(con); con.close()
    return stats


def selftest() -> int:
    wide = ("<h1>MONTHLY EXPORT - 2025</h1><table><tr><td>PRODUCT</td><td>UNIT</td>" +
            "".join(f"<td>{m}</td>" for m in ["JAN", "FEB", "MAR"]) + "<td>JAN - MAR</td></tr>"
            "<tr><td>CPO</td><td>Tonnes</td><td>1,000</td><td>2,000</td><td>-</td><td>3,000</td></tr>"
            "<tr><td>RM Mil</td><td>10.5</td><td>20.5</td><td></td><td>31.0</td></tr></table>")
    _, df = parse_report(wide)
    assert len(df) == 4 and set(df["unit"]) == {"tonnes", "MYR_mil"}, df
    assert df[(df.unit == "MYR_mil") & (df.month == 2)]["value"].iloc[0] == 20.5
    print("selftest passed")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", action="append")
    ap.add_argument("--p0", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--since", type=int, default=date.today().year)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="re-parse saved raw reports offline")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    if a.rebuild:
        print(rebuild()); raise SystemExit
    fams = a.family or (P0 if a.p0 else [f["id"] for f in crawl.CFG["families"]] if a.all else [])
    if not fams:
        ap.print_help(); raise SystemExit
    print(sync(fams, a.since))
