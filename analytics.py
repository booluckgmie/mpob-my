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
