"""Scheduled refresh orchestrator.

    python refresh.py daily     # prices (this+last year) + headline, then a 2nd verification pass
    python refresh.py weekly    # re-pull all exposed price years + headline + DOSM (catch revisions)
    python refresh.py monthly   # weekly + BEPI inventory + reconcile (run after the ~10th release)

Every run appends to data/lake/gold/run_log.csv. `revised > 0` on the verification pass,
or any revision to a price older than 7 days, is flagged because MPOB reserves the right to
edit published figures without notice.
"""
from __future__ import annotations
import argparse, csv, sys, time
from datetime import date, datetime, timezone

import common
import bepi_reports, dosm_ingest, pdf_reports, price_daily, ticker

LOG = common.GOLD / "run_log.csv"
FIELDS = ["run_at", "mode", "pass", "step", "inserted", "revised", "unchanged", "ok", "note"]


def _log(rows: list[dict]) -> None:
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def _step(name: str, fn) -> dict:
    try:
        s = fn() or {}
        return {"step": name, "inserted": s.get("inserted", 0), "revised": s.get("revised", 0),
                "unchanged": s.get("unchanged", 0), "ok": True, "note": ""}
    except Exception as e:                     # one failing source must not block the others
        print(f"STEP FAILED {name}: {e}", file=sys.stderr)
        return {"step": name, "inserted": 0, "revised": 0, "unchanged": 0, "ok": False, "note": str(e)[:200]}


def _price_years(mode: str) -> list[int]:
    y = date.today().year
    return [y - 1, y] if mode == "daily" else list(range(y - 2, y + 1))   # BEPI exposes ~3 years


def one_pass(mode: str, n: int) -> list[dict]:
    steps = [("price_daily", lambda: price_daily.write(price_daily.sync(_price_years(mode)))),
             ("ticker", ticker.run)]
    y = date.today().year
    if mode == "daily":     # core reports, current year only: ~6 requests, catches the monthly release day
        steps.append(("bepi_p0", lambda: bepi_reports.sync(bepi_reports.P0, y)))
    else:                   # weekly/monthly refinement: every family, wider window, flags revisions
        fams = [f["id"] for f in __import__("crawl").CFG["families"] if f["id"] not in bepi_reports.UNSUPPORTED]
        steps.append(("bepi_all", lambda: bepi_reports.sync(fams, y - (1 if mode == "weekly" else 3))))
        steps.append(("bepi_pdf", lambda: pdf_reports.sync(pdf_reports.FAMILIES, y - (1 if mode == "weekly" else 3))))
        steps.append(("dosm", dosm_ingest.run))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [{"run_at": now, "mode": mode, "pass": n, **_step(name, fn)} for name, fn in steps]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["daily", "weekly", "monthly"])
    ap.add_argument("--recheck-wait", type=int, default=60, help="seconds before the verification pass")
    a = ap.parse_args()

    first = one_pass(a.mode, 1)
    _log(first)
    time.sleep(a.recheck_wait)
    second = one_pass(a.mode, 2)          # 2x recheck: nothing may change between the passes
    _log(second)

    problems = [f"{r['step']}: {r['note']}" for r in first + second if not r["ok"]]
    problems += [f"{r['step']}: {r['revised']} values changed between passes" for r in second if r["revised"]]
    problems += [f"{r['step']}: {r['inserted']} new rows on verification pass" for r in second if r["inserted"]]
    if a.mode != "daily":
        revised = sum(r["revised"] for r in first)
        if revised:
            problems.append(f"refinement: {revised} previously published values were revised by the source")

    if a.mode == "monthly":
        import crawl, reconcile
        try:
            rows = crawl.harvest()
            print(crawl.coverage_grid(rows))
        except Exception as e:
            problems.append(f"crawl: {e}")
        con = common.connect()
        obs = con.execute("SELECT series_key, strftime(period_start,'%Y-%m') period, value "
                          "FROM v_obs_latest WHERE granularity='m'").df()
        con.close()
        out = reconcile.reconcile(obs, obs)
        if reconcile.summarise(out):
            problems.append("reconcile: months need attention")
        out.to_parquet(common.GOLD / "reconciliation.parquet", index=False)

    con = common.connect(); common.save_lake(con); con.close()
    for p in problems:
        print("ATTENTION:", p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
