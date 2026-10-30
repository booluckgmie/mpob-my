"""BEPI link inventory: harvest [title, href] anchors and classify them by link TEXT.

Link paths on BEPI are unstable; anchor text is not, so sources.json matches on text.

    python crawl.py --list              # family x year coverage grid + unmatched count
    python crawl.py --manifest          # write data/manifest.csv
    python crawl.py --download stocks   # fetch files of one family into data/raw/bepi/
"""
from __future__ import annotations
import argparse, csv, hashlib, json, re, time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import common

CFG = json.loads((common.ROOT / "sources.json").read_text())
NOISE = re.compile(CFG["nav_noise"], re.I)
# Longest regex first so the most specific title wins.
FAMILIES = sorted(CFG["families"], key=lambda f: -len(f["title"]))
FILE_RE = re.compile(r"\.(xlsx?|csv|pdf)(\?|$)", re.I)


def classify(title: str) -> str | None:
    t = " ".join(title.split())
    if not t or NOISE.match(t):
        return None
    for f in FAMILIES:
        if re.search(f["title"], t, re.I):
            return f["id"]
    return None


def harvest() -> list[dict]:
    seen, rows = set(), []
    for seed in CFG["seeds"]:
        url = urljoin(CFG["base"], seed)
        html = common.http_get(url).text
        (common.RAW / "bepi").mkdir(exist_ok=True)
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            title = " ".join(a.get_text(" ", strip=True).split())
            href = urljoin(url, a["href"])
            if not title or (title, href) in seen:
                continue
            seen.add((title, href))
            m = re.search(r"\b(19|20)\d{2}\b", title)
            rows.append({"title": title, "url": href, "family": classify(title),
                         "year": int(m.group(0)) if m else None,
                         "is_file": bool(FILE_RE.search(href)), "seed": seed})
        time.sleep(2)
    return rows


def coverage_grid(rows: list[dict]) -> str:
    fam = {}
    for r in rows:
        if r["family"]:
            fam.setdefault(r["family"], {}).setdefault(r["year"], 0)
            fam[r["family"]][r["year"]] += 1
    years = sorted({y for d in fam.values() for y in d if y})
    lines = ["family".ljust(18) + " ".join(str(y)[2:] for y in years) + "  total"]
    for f in CFG["families"]:
        d = fam.get(f["id"], {})
        cells = " ".join((str(min(d.get(y, 0), 9)) if d.get(y) else ".").rjust(2) for y in years)
        lines.append(f"{f['id']:<18}{cells}  {sum(d.values())}")
    lines.append(f"unmatched links: {sum(1 for r in rows if not r['family'] and r['is_file'])} (files), "
                 f"{sum(1 for r in rows if not r['family'])} (all anchors)")
    return "\n".join(lines)


def download(rows: list[dict], family: str) -> None:
    con = common.connect()
    out = common.RAW / "bepi"; out.mkdir(exist_ok=True)
    for r in (r for r in rows if r["family"] == family and r["is_file"]):
        body = common.http_get(r["url"]).content
        digest = hashlib.sha256(body).hexdigest()
        path = out / f"{digest[:12]}_{r['url'].split('/')[-1].split('?')[0]}"
        path.write_bytes(body)
        con.execute("INSERT INTO ingest_manifest VALUES (?,?,?,?,?,?,now())",
                    [r["url"], family, r["title"], r["year"], digest, len(body)])
        print("saved", path.name, len(body))
        time.sleep(2)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--manifest", action="store_true")
    ap.add_argument("--download", metavar="FAMILY")
    a = ap.parse_args()
    if not (a.list or a.manifest or a.download):
        ap.print_help(); raise SystemExit
    rows = harvest()
    if a.list:
        print(coverage_grid(rows))
    if a.manifest:
        p = common.DATA / "manifest.csv"
        with p.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        print(f"wrote {p} ({len(rows)} rows)")
    if a.download:
        download(rows, a.download)
