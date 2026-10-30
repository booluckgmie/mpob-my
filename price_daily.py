"""
Daily CPO price ingest - hardened from the working prototype.

Endpoint (discovered): /admin2/price_local_daily_view_cpo_msia.php
    ?more=Y&jenis=1Y&tahun={year}

    python price_daily.py --selftest        # offline parse check
    python price_daily.py --backfill 2011   # all years to current
    python price_daily.py --sync            # current + previous year
"""
from __future__ import annotations
import argparse, re, sys, time
from datetime import date

import pandas as pd
from bs4 import BeautifulSoup

import common

BASE = ("https://bepi.mpob.gov.my/admin2/price_local_daily_view_cpo_msia.php"
        "?more=Y&jenis=1Y&tahun={year}")
RAW = common.RAW / "prices"; RAW.mkdir(parents=True, exist_ok=True)
SERIES_KEY = "mpob:cpo_daily"
SERIES = {"series_key": SERIES_KEY, "dataset": "price", "measure": "price", "product": "CPO",
          "geography": "Peninsular Malaysia", "unit": "MYR/t", "agg_rule": "avg",
          "native_freq": "daily"}
PLAUSIBLE = (1_500.0, 9_000.0)


def fetch(year: int) -> str | None:
    try:
        r = common.http_get(BASE.format(year=year))
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return None
    (RAW / f"cpo_{year}.html").write_bytes(r.content)
    return r.text


def parse_html(html: str, year: int) -> pd.DataFrame:
    """Strict parse. Raises if the table shape changed - never silently drops."""
    if not html.strip():          # BEPI serves an empty body for years it no longer exposes
        return pd.DataFrame(columns=["date", "price", "status", "raw"])
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    if tbody is None:
        raise ValueError("no <tbody> - layout changed")
    rows = tbody.find_all("tr")

    months: list[str] = []
    for row in rows:
        cells = row.find_all("td")
        if cells and "Date" in row.get_text():
            months = [c.get_text(strip=True) for c in cells[1:]]
            break
    if len(months) != 12:
        raise ValueError(f"expected 12 month columns, got {len(months)}: {months}")

    records = []
    for row in rows:
        cols = row.find_all("td")
        if not cols or not cols[0].get_text(strip=True).isdigit():
            continue
        # +1 because the first cell is the day number
        if len(cols) != len(months) + 1:
            raise ValueError(f"unexpected column count {len(cols)} on day row")
        day = int(cols[0].get_text(strip=True))
        for idx, cell in enumerate(cols[1:]):
            raw = cell.get_text(strip=True)
            if raw in ("", "-"):
                continue
            flag = raw if raw.upper() in {"PH", "NT"} else None
            value = None if flag else _to_float(raw)
            records.append({"year": year, "month": months[idx], "day": day,
                            "value": value, "flag": flag, "raw": raw})

    df = pd.DataFrame(records)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(
        df["day"].astype(str) + " " + df["month"].map(_full_month) + " " + df["year"].astype(str),
        format="%d %B %Y", errors="coerce")
    bad = df["date"].isna().sum()
    if bad:
        print(f"WARNING: {bad} unparseable dates in {year}", file=sys.stderr)
    df = df.dropna(subset=["date"])
    df = df[df["date"] <= pd.Timestamp.today().normalize()]   # table pre-fills future PH/NT cells
    df["price"] = pd.to_numeric(df["value"], errors="coerce")
    df["status"] = df["flag"].where(df["flag"].notna(), "actual")
    return df[["date", "price", "status", "raw"]].sort_values("date").drop_duplicates("date", keep="last")


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _full_month(label: str) -> str:
    """Accept 'Jan' or 'January'; return the full English name."""
    for m in _MONTHS:
        if m.lower().startswith(label.strip()[:3].lower()):
            return m
    return label


def _to_float(s: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except (ValueError, TypeError):
        return None


def validate(df: pd.DataFrame, year: int) -> list[str]:
    warn = []
    lo, hi = PLAUSIBLE
    bad = df[(df["price"].notna()) & ((df["price"] < lo) | (df["price"] > hi))]
    if not bad.empty:
        warn.append(f"{len(bad)} prices outside RM{lo}-{hi}: {bad['date'].dt.date.head(3).tolist()}")
    if df["price"].isna().all():
        warn.append("no numeric prices at all")
    expect = 240 if year < date.today().year else (date.today().timetuple().tm_yday * 5 // 7)
    if len(df) < expect * 0.6:
        warn.append(f"only {len(df)} rows vs ~{expect} expected - possible partial table")
    return warn


def sync(years: list[int]) -> pd.DataFrame:
    frames = []
    for y in years:
        html = fetch(y)
        if html is None:
            print(f"FAILED {y}", file=sys.stderr); continue
        try:
            df = parse_html(html, y)
        except ValueError as e:
            print(f"PARSE FAIL {y}: {e}", file=sys.stderr); continue
        if df.empty:
            print(f"{y}: endpoint returned no data (BEPI exposes ~3 years)", file=sys.stderr); continue
        for w in validate(df, y):
            print(f"[{y}] {w}", file=sys.stderr)
        print(f"{y}: {len(df)} rows, {df['price'].notna().sum()} priced")
        frames.append(df)
        time.sleep(2)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def write(df: pd.DataFrame, con=None) -> dict:
    """Append into the revision-aware obs store (PH/NT days kept as NULL-value rows)."""
    if df.empty:
        return {"inserted": 0, "revised": 0, "unchanged": 0}
    own = con is None
    con = con or common.connect()
    common.register_series(con, [SERIES])
    obs = pd.DataFrame({
        "series_key": SERIES_KEY, "period_start": df["date"], "period_end": df["date"],
        "granularity": "d", "value": df["price"], "unit": "MYR/t", "status": df["status"],
        "coverage": 1.0, "source_url": "bepi.mpob.gov.my/admin2/price_local_daily_view_cpo_msia.php",
    })
    stats = common.upsert_obs(con, obs)
    print(f"{SERIES_KEY}: {stats} range {df['date'].min().date()} -> {df['date'].max().date()}")
    if own:
        common.save_lake(con)
        con.close()
    return stats


def import_csv(path: str) -> pd.DataFrame:
    """Seed history from the legacy CSVs (date,price) where price may be PH/NT."""
    raw = pd.read_csv(path, dtype={"price": str})
    raw["date"] = pd.to_datetime(raw["date"])
    flag = raw["price"].str.upper().isin(["PH", "NT"])
    out = pd.DataFrame({"date": raw["date"], "raw": raw["price"],
                        "price": pd.to_numeric(raw["price"].where(~flag), errors="coerce"),
                        "status": raw["price"].str.upper().where(flag, "actual")})
    return out.dropna(subset=["date"]).drop_duplicates("date", keep="last")


def selftest() -> int:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    header = "<tr><td>Date</td>" + "".join(f"<td>{m}</td>" for m in months) + "</tr>"
    row1 = "<tr><td>1</td>" + "".join("<td>4,050.50</td>" for _ in range(12)) + "</tr>"
    row2 = "<tr><td>2</td><td>4,060.00</td><td>-</td>" + "<td>PH</td>" + "<td>4,070</td>" + "".join("<td></td>" for _ in range(8)) + "</tr>"
    html = f"<table><tbody>{header}{row1}{row2}</tbody></table>"
    df = parse_html(html, 2025)
    assert len(df) == 15, len(df)  # 12 + 3 (blank and "-" cells skipped)
    assert df["price"].iloc[0] == 4050.50
    assert set(df["status"]) == {"actual", "PH"}
    print("selftest passed\n" + df.head(3).to_string(index=False))

    broken = html.replace("</tbody>", "<tr><td>3</td><td>1</td></tr></tbody>")
    try:
        parse_html(broken, 2025); print("FAIL: bad shape accepted")
    except ValueError as e:
        print("shape guard works:", e)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--backfill", type=int, metavar="START_YEAR")
    ap.add_argument("--import-csv", nargs="+", metavar="CSV", help="seed from legacy date,price CSVs")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    y = date.today().year
    if a.sync:
        write(sync([y - 1, y]))
    if a.backfill:
        write(sync(list(range(a.backfill, y + 1))))
    for p in a.import_csv or []:
        write(import_csv(p))
    if not (a.sync or a.backfill or a.import_csv):
        ap.print_help()
