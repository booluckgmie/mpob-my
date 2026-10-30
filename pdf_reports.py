"""BEPI PDF reports: FFB / oil yield by state (monthly, t/ha) and oil palm planted area (annual, ha).

The yield and area dataset pages link PDFs instead of embedding an HTML report. Text is extracted with
pdfplumber (x_tolerance=2 keeps the spaces in labels like "NEGERI SEMBILAN") and parsed into the same long
format as bepi_reports, so storage, revisions and rebuilds are shared.

    python pdf_reports.py --since 2024
    python pdf_reports.py --selftest
"""
from __future__ import annotations
import argparse, hashlib, io, json, re, sys, time
from datetime import date
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import pandas as pd
from bs4 import BeautifulSoup

import bepi_reports as br
import common, crawl

FAMILIES = ["yield", "area"]
NUM_RE = re.compile(r"^-?[\d,]*\.?\d+$")


# the same state appears under different names across years and datasets; map to one canonical label
# (canonical forms match the HTML reports: "P. MALAYSIA", "SABAH/SARAWAK")
ALIASES = {"JOHORE": "JOHOR", "MALACCA": "MELAKA", "PENANG": "PULAU PINANG", "N. SEMBILAN": "NEGERI SEMBILAN",
           "N.SEMBILAN": "NEGERI SEMBILAN", "PENINSULAR MALAYSIA": "P. MALAYSIA", "SEMENANJUNG MALAYSIA": "P. MALAYSIA", "SEMENANJUNG": "P. MALAYSIA", "PENINSULAR": "P. MALAYSIA",
           "SABAH & SARAWAK": "SABAH/SARAWAK", "SABAH AND SARAWAK": "SABAH/SARAWAK", "W.P. KUALA LUMPUR": "KUALA LUMPUR"}


def norm_label(label: str) -> str:
    u = " ".join(label.upper().split())
    return ALIASES.get(u, u)


def pdf_text(content: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join((p.extract_text(x_tolerance=2) or "") for p in pdf.pages)


def _split_row(line: str) -> tuple[str, list[float]] | None:
    toks = line.split()
    nums: list[float] = []
    while toks and NUM_RE.match(toks[-1]):
        nums.append(float(toks.pop().replace(",", "")))
    label = " ".join(toks).strip()
    return (label, nums[::-1]) if label and nums else None


def parse_yield_text(text: str) -> pd.DataFrame:
    """Half-year tables: a month header line, a year line (2024 2025 per month), then state rows."""
    unit_m = re.search(r"\(\s*TONNES\s*/\s*HECTARE\s*\)", text, re.I)
    unit = "t/ha" if unit_m else "t/ha"
    rows, months, years = [], [], []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        toks = lines[i].split()
        ms = [br._month(t) for t in toks]
        if sum(m is not None for m in ms) >= 3 and "MONTH" not in lines[i].upper():
            months = [m for m in ms if m]
            years = []
            for k in range(1, 4):                       # a "State" caption line can sit between the two rows
                yr = [int(t) for t in lines[i + k].split() if re.fullmatch(r"(19|20)\d\d", t)] if i + k < len(lines) else []
                if len(yr) >= 2 * len(months):
                    years, i = yr[: 2 * len(months)], i + k + 1
                    break
            if not years:
                raise ValueError(f"year row does not cover {len(months)} months after {lines[i][:50]!r}")
            continue
        if months:
            parsed = _split_row(lines[i])
            if parsed and len(parsed[1]) == 2 * len(months) + 2 and not lines[i].upper().startswith("NOTE"):
                label, vals = norm_label(parsed[0]), parsed[1]
                for k in range(len(months)):
                    for j in range(2):
                        rows.append(dict(section="", label=label, unit=unit, year=years[2 * k + j],
                                         month=months[k], value=vals[2 * k + j]))
        i += 1
    if not rows:
        raise ValueError("no yield rows parsed")
    return pd.DataFrame(rows).drop_duplicates(["label", "year", "month"], keep="last")


def _area_values(tokens: list[str]) -> list[float] | None:
    """[matured, immature, total] hectares from a row whose percentage cells may be whole numbers or split
    ("8 7.58" = 87.58). The areas are the integers where matured + immature = total (largest such total wins),
    which both locates them and checks the row."""
    ints = [float(t.replace(",", "")) for t in tokens if re.fullmatch(r"\d[\d,]*", t)]
    best = None
    for i in range(len(ints)):
        for j in range(i + 1, len(ints)):
            for k in range(j + 1, len(ints)):
                if abs(ints[i] + ints[j] - ints[k]) <= 2 and ints[k] >= 100 and (best is None or ints[k] > best[2]):
                    best = [ints[i], ints[j], ints[k]]
    return best


def parse_area_text(text: str) -> pd.DataFrame:
    m = re.search(r"AS AT\s+([A-Z]+)\s+((?:19|20)\d\d)", text.upper())
    if not m:
        raise ValueError("planted-area heading not found")
    year = int(m.group(2))
    rows, seen = [], set()
    for line in text.splitlines():
        toks = line.split()
        k = next((i for i, t in enumerate(toks) if re.fullmatch(r"\d[\d,]*", t)), None)
        if not k:
            continue
        label, v = norm_label(" ".join(toks[:k])), _area_values(toks[k:])
        if v and label not in ("STATE", "REGION") and label not in seen:
            seen.add(label)
            for name, val in (("matured", v[0]), ("immature", v[1]), ("total", v[2])):
                rows.append(dict(section=name, label=label, unit="ha", year=year, month=12, value=val))
    if not rows:
        raise ValueError("no planted-area rows parsed")
    return pd.DataFrame(rows)


def parse_pdf_text(text: str) -> tuple[pd.DataFrame, bool]:
    """Return (long df, is_annual). Dispatch on content, not filename."""
    if re.search(r"PLANTED AREA", text.upper()) and re.search(r"AS AT", text.upper()):  # (area PDFs may also hold a 2nd table)
        return parse_area_text(text), True
    return parse_yield_text(text), False


def pdf_links(article_url: str) -> list[tuple[str, str]]:
    html = common.http_get(article_url).text
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(attrs={"itemprop": "articleBody"})
    out = []
    for a in (body or soup).find_all("a", href=True):
        href = a["href"].lower()
        # never fall back to site-wide navigation links (annual industry overviews etc.)
        if href.endswith(".pdf") and "/overview/" not in href and (body is not None or "/yield/" in href or "/area/" in href):
            u = urlsplit(urljoin(article_url, a["href"]))
            out.append((urlunsplit((u.scheme, u.netloc, quote(u.path, safe="/%()"), u.query, "")), a.get_text(" ", strip=True)))
    return out


def sync(families: list[str], since: int, con=None) -> dict:
    own = con is None
    con = con or common.connect()
    rows = [r for r in crawl.harvest() if r["family"] in families and r["year"] and r["year"] >= since and not r["is_file"]]
    stats = {"inserted": 0, "revised": 0, "unchanged": 0, "failed": 0, "reports": 0}
    (common.RAW / "bepi").mkdir(exist_ok=True)
    for r in sorted(rows, key=lambda r: (r["family"], r["year"])):
        try:
            for url, label in pdf_links(r["url"]):
                time.sleep(2)
                try:
                    content = common.http_get(url, retries=2).content
                except RuntimeError:                      # linked but not uploaded yet (e.g. next year's area summary)
                    stats["no_data"] = stats.get("no_data", 0) + 1
                    print(f"NO DATA {r['family']} {r['year']} {label}: PDF not available at source", file=sys.stderr)
                    continue
                # stable per-dataset name: the page title changes every year, the series must not
                table = ("planted_area" if r["family"] == "area"
                         else "ffb_yield" if "ffb" in (label + url).lower() else "oil_yield")
                try:
                    df, annual = parse_pdf_text(pdf_text(content))
                    obs, series = br.to_obs(r["family"], df, url, table, annual)
                except Exception as e:
                    stats["failed"] += 1
                    print(f"FAILED {r['family']} {r['year']} {label}: {e}", file=sys.stderr)
                    continue
                digest = hashlib.sha256(content).hexdigest()[:10]
                raw = common.RAW / "bepi" / f"{r['family']}_{r['year']}_{digest}.pdf"
                raw.write_bytes(content)
                raw.with_suffix(".json").write_text(json.dumps(
                    {"kind": "pdf", "family": r["family"], "year": r["year"], "table": table, "url": url, "title": label}))
                common.register_series(con, series)
                s = common.upsert_obs(con, obs)
                for k in ("inserted", "revised", "unchanged"):
                    stats[k] += s[k]
                stats["reports"] += 1
                print(f"{r['family']} {r['year']} {label}: {len(df)} values, {s}")
        except Exception as e:
            stats["failed"] += 1
            print(f"FAILED {r['family']} {r['year']} ({r['url']}): {e}", file=sys.stderr)
        time.sleep(2)
    if own:
        common.save_lake(con); con.close()
    return stats


def selftest() -> int:
    y = ("AVERAGE FFB YIELD\nJAN-JUNE 2024 & 2025 (TONNES/HECTARE)\nState Jan Feb Mar Apr May June January-June\n"
         "2024 2025 2024 2025 2024 2025 2024 2025 2024 2025 2024 2025 2024 2025\n"
         "Johor 1.46 1.37 1.27 1.20 1.37 1.29 1.49 1.62 1.70 1.62 1.63 1.67 8.92 8.77\n"
         "Negeri Sembilan 1.32 1.08 1.16 1.12 1.32 1.37 1.44 1.76 1.76 1.76 1.81 1.78 8.81 8.87\nNote: Estates")
    df = parse_yield_text(y)
    assert len(df) == 24 and set(df.label) == {"JOHOR", "NEGERI SEMBILAN"} and df.value.max() < 2.0
    a = "PLANTED AREA\nOIL PALM PLANTED AREA AS AT DECEMBER 2025 (HECTARES)\nSTATE MATURED % IMMATURE % TOTAL %\nJOHOR 615,836 91.2 59,722 8.8 675,558 11.9"
    df, annual = parse_pdf_text(a)
    assert annual and df[df.section == "total"].value.iloc[0] == 675558 and set(df.year) == {2025}
    print("selftest passed")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=date.today().year - 1)
    ap.add_argument("--family", action="append")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    print(sync(a.family or FAMILIES, a.since))
