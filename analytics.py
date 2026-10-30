"""Derived metrics. Pure functions over the lake so they can be tested offline.

Everything here is `derived`: price statistics come from daily prices, the supply/demand
balance comes from the MPOB headline scoreboard plus its own reported % changes.
"""
from __future__ import annotations
import pandas as pd


def price_stats(daily: pd.DataFrame) -> dict:
    """daily: columns date, value (priced days only). Returns latest, changes, volatility, seasonality."""
    d = daily.dropna(subset=["value"]).sort_values("date").copy()
    d["date"] = pd.to_datetime(d["date"])
    if d.empty:
        return {}
    s = d.set_index("date")["value"]
    last_date, last = s.index[-1], float(s.iloc[-1])

    def chg(days: int):
        prior = s[: last_date - pd.Timedelta(days=days)]
        return None if prior.empty else round((last / float(prior.iloc[-1]) - 1) * 100, 2)

    rets = s.pct_change().dropna()
    vol30 = rets[-20:].std() * (252 ** 0.5) * 100 if len(rets) >= 20 else None   # ~20 trading days
    monthly = s.resample("MS").mean()
    by_moy = {}
    for ts, v in monthly.items():
        by_moy.setdefault(int(ts.year), {})[int(ts.month)] = round(float(v), 1)
    last12 = s[last_date - pd.Timedelta(days=365):]
    return {
        "latest": {"date": str(last_date.date()), "value": last},
        "change_pct": {"1w": chg(7), "1m": chg(30), "3m": chg(91), "12m": chg(365)},
        "vol_annualised_pct": None if vol30 is None else round(float(vol30), 1),
        "vs_12m_avg_pct": round((last / float(last12.mean()) - 1) * 100, 2),
        "high_12m": float(last12.max()), "low_12m": float(last12.min()),
        "monthly_by_year": by_moy,
    }


def balance(h: dict[str, dict]) -> dict | None:
    """Supply/demand balance for the latest MPOB month.

    h maps suffix -> {"value", "pct"} for cpo_production, po_stock, po_import, po_export.
    Opening stock is backed out of MPOB's own reported % change, so it is derived, not published.
    """
    need = ("cpo_production", "po_stock", "po_import", "po_export")
    if any(k not in h for k in need) or h["po_stock"].get("pct") is None:
        return None
    close = h["po_stock"]["value"]
    opening = close / (1 + h["po_stock"]["pct"] / 100)
    prod, imp, exp = h["cpo_production"]["value"], h["po_import"]["value"], h["po_export"]["value"]
    disappearance = opening + prod + imp - close          # exports + domestic use
    return {
        "opening_stock": round(opening), "production": prod, "imports": imp, "closing_stock": close,
        "disappearance": round(disappearance), "exports": exp,
        "implied_domestic_use": round(disappearance - exp),
        "stock_usage_annualised_pct": round(close / (disappearance * 12) * 100, 1),
        "stock_cover_months_of_exports": round(close / exp, 2),
        "note": "opening stock derived from MPOB's reported % change; disappearance = opening + production + imports - closing; "
                "production is CPO only, stock is total palm oil",
    }


def production_band(s: pd.DataFrame, years_back: int = 5) -> dict:
    """s: columns period_start (date), value. Latest month vs the same calendar month in prior years.

    The band is the p25-p75 of up to `years_back` prior years; all of it is `derived`.
    """
    d = s.dropna(subset=["value"]).copy()
    d["period_start"] = pd.to_datetime(d["period_start"])
    d = d.sort_values("period_start").drop_duplicates("period_start", keep="last")
    if d.empty:
        return {}
    last = d.iloc[-1]
    ly, lm = last["period_start"].year, last["period_start"].month
    hist = d[d["period_start"].dt.year < ly]
    by_month = {}
    for m in range(1, 13):
        prior = hist[(hist["period_start"].dt.month == m) & (hist["period_start"].dt.year >= ly - years_back)]["value"]
        by_month[m] = None if len(prior) < 3 else {"p25": float(prior.quantile(.25)), "p50": float(prior.median()),
                                                  "p75": float(prior.quantile(.75)), "n": int(len(prior))}
    band = by_month[lm]
    prev = d[d["period_start"] == last["period_start"] - pd.DateOffset(years=1)]["value"]
    out = {"latest": {"period": last["period_start"].strftime("%Y-%m"), "value": float(last["value"])},
           "yoy_pct": None if prev.empty else round((float(last["value"]) / float(prev.iloc[0]) - 1) * 100, 2),
           "band_by_month": by_month,
           "series": [{"period": p.strftime("%Y-%m"), "value": float(v)} for p, v in zip(d["period_start"], d["value"])][-36:]}
    if band:
        out["vs_median_pct"] = round((float(last["value"]) / band["p50"] - 1) * 100, 2)
        out["position"] = "above band" if last["value"] > band["p75"] else "below band" if last["value"] < band["p25"] else "within band"
    return out


def exact_balance(stock: pd.DataFrame, production: float, imports: float, exports: float) -> dict | None:
    """Balance from a real stock history: stock df has period_start, value (>= 2 months)."""
    d = stock.dropna(subset=["value"]).sort_values("period_start")
    if len(d) < 2:
        return None
    close, opening = float(d["value"].iloc[-1]), float(d["value"].iloc[-2])
    disappearance = opening + production + imports - close
    return {"opening_stock": round(opening), "closing_stock": round(close), "disappearance": round(disappearance),
            "implied_domestic_use": round(disappearance - exports),
            "stock_usage_annualised_pct": round(close / (disappearance * 12) * 100, 1),
            "stock_cover_months_of_exports": round(close / exports, 2), "opening_source": "published stock history"}


# ---------------------------------------------------------------- overview (plain-language layer)
import calendar as _cal


def _pct(now: float, before) -> float | None:
    return None if before in (None, 0) or pd.isna(before) else round((now / before - 1) * 100, 1)


def kpi(key: str, label: str, plain: str, unit: str, df: pd.DataFrame, *, annual: bool = False,
        scale: float = 1.0, matters: str = "") -> dict | None:
    """One headline number with its change and a sparkline. df: period_start, value."""
    d = df.dropna(subset=["value"]).copy()
    if d.empty:
        return None
    d["period_start"] = pd.to_datetime(d["period_start"])
    d = d.sort_values("period_start").drop_duplicates("period_start", keep="last")
    last = d.iloc[-1]
    ts, v = last["period_start"], float(last["value"])

    def at(offset):
        m = d[d["period_start"] == ts - offset]["value"]
        return None if m.empty else float(m.iloc[0])

    return {"key": key, "label": label, "plain": plain, "unit": unit, "matters": matters,
            "value": round(v * scale, 4), "period": str(ts.year) if annual else ts.strftime("%Y-%m"),
            "period_label": str(ts.year) if annual else f"{_cal.month_name[ts.month]} {ts.year}",
            "mom_pct": None if annual else _pct(v, at(pd.DateOffset(months=1))),
            "yoy_pct": _pct(v, at(pd.DateOffset(years=1))),
            "spark": [round(float(x) * scale, 4) for x in d["value"].tail(24 if not annual else 12)]}


def _dir(p):
    return "flat" if p is None or abs(p) < 0.5 else ("up" if p > 0 else "down")


def insights(k: dict, production_band: dict | None, balance: dict | None) -> list[dict]:
    """Plain sentences for non-specialists. Facts and direction only - no good/bad judgement,
    because a rise in price or stocks means different things to a planter, a refiner and a buyer."""
    out = []
    p = k.get("production")
    if p:
        pos = (production_band or {}).get("position")
        extra = {"above band": " That is higher than usual for the month.", "below band": " That is lower than usual for the month.",
                 "within band": " That is in line with what is usual for the month."}.get(pos, "")
        out.append({"icon": "palm", "topic": "Supply", "tone": _dir(p["yoy_pct"]),
                    "text": f"Malaysia produced {p['value']:.2f} million tonnes of crude palm oil in {p['period_label']}, "
                            + (f"{abs(p['yoy_pct'])}% {'more' if p['yoy_pct'] > 0 else 'less'} than a year earlier." if p["yoy_pct"] is not None else "")
                            + extra})
    e = k.get("exports")
    if e:
        out.append({"icon": "ship", "topic": "Demand", "tone": _dir(e["yoy_pct"]),
                    "text": f"Palm oil exports were {e['value']:.2f} million tonnes"
                            + (f", {abs(e['yoy_pct'])}% {'above' if e['yoy_pct'] > 0 else 'below'} the same month last year." if e["yoy_pct"] is not None else ".")})
    s = k.get("stocks")
    if s:
        cover = (balance or {}).get("stock_cover_months_of_exports")
        out.append({"icon": "tank", "topic": "Stocks", "tone": _dir(s["mom_pct"]),
                    "text": f"Stocks held in the country are {s['value']:.2f} million tonnes, "
                            + (f"{abs(s['mom_pct'])}% {'more' if s['mom_pct'] > 0 else 'less'} than the month before" if s["mom_pct"] is not None else "")
                            + (f" - roughly {cover} months' worth of exports." if cover else ".")})
    pr = k.get("price")
    if pr:
        out.append({"icon": "coin", "topic": "Price", "tone": _dir(pr["mom_pct"]),
                    "text": f"Crude palm oil costs RM {pr['value']:,.0f} per tonne ({pr['period_label']}), "
                            + (f"{'up' if pr['mom_pct'] > 0 else 'down'} {abs(pr['mom_pct'])}% over the past month" if pr["mom_pct"] is not None else "")
                            + (f" and {'up' if pr['yoy_pct'] > 0 else 'down'} {abs(pr['yoy_pct'])}% over the year." if pr["yoy_pct"] is not None else ".")})
    a = k.get("area")
    if a:
        out.append({"icon": "leaf", "topic": "Land", "tone": _dir(a["yoy_pct"]),
                    "text": f"Oil palm covers {a['value']:.2f} million hectares ({a['period_label']})"
                            + (f", {abs(a['yoy_pct'])}% {'more' if a['yoy_pct'] > 0 else 'less'} than the year before." if a["yoy_pct"] is not None else ".")})
    return out
