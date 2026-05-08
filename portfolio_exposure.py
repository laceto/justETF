"""
Portfolio Exposure Analyzer

Given a list of ISINs (and optional weights), computes cumulative country,
sector, and holding exposure across all ETFs weighted by their portfolio share.

Usage:
    python portfolio_exposure.py
    python portfolio_exposure.py --budget 10000 --top 15

Import:
    from portfolio_exposure import analyze
    analyze(["IE000XJA2OU4", "IE000G0E83X3"], budget=10_000)
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict


# ── Noise filter (labels that bleed in from performance tables) ─────────────
_NOISE = {
    "1 month", "3 months", "6 months", "1 year", "3 years", "5 years",
    "YTD", "2022", "2023", "2024", "2025", "Other",
    "Current dividend yield", "Dividends (last 12 months)",
    "Since inception (MAX)", "Broker",
}

REAL_SECTORS = {
    "Basic Materials", "Consumer Discretionary", "Consumer Staples",
    "Energy", "Financials", "Health Care", "Industrials", "Real Estate",
    "Technology", "Telecommunication", "Utilities",
}


def load_index(path: str = "data/profiles.json") -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {e["isin"]: e for e in data}


def _aggregate(
    isins: list[str],
    weights: list[float],       # portfolio fractions, sum=1
    index: dict,
    top_n: int,
) -> tuple[list, list, list, list]:
    """Return sorted (country, sector, holding, etf_summary) lists."""

    country_acc: dict[str, float] = defaultdict(float)
    sector_acc: dict[str, float] = defaultdict(float)
    holding_acc: dict[str, float] = defaultdict(float)
    etf_rows = []

    for isin, w in zip(isins, weights):
        etf = index.get(isin)
        if etf is None:
            print(f"  [WARN] {isin} not found in profiles, skipping.")
            continue

        etf_rows.append({
            "isin": isin,
            "name": etf.get("name", ""),
            "weight_pct": round(w * 100, 2),
            "ter_pct": etf.get("ter_pct"),
            "fund_size_eur_mln": etf.get("fund_size_eur_mln"),
        })

        for item in etf.get("country_exposure") or []:
            name = item.get("name", "")
            if name and name not in _NOISE:
                country_acc[name] += w * (item.get("weight_pct") or 0)

        for item in etf.get("sector_exposure") or []:
            name = item.get("name", "")
            if name in REAL_SECTORS:
                sector_acc[name] += w * (item.get("weight_pct") or 0)

        for item in etf.get("top_holdings") or []:
            name = item.get("name", "")
            if name:
                holding_acc[name] += w * (item.get("weight_pct") or 0)

    countries = sorted(country_acc.items(), key=lambda x: -x[1])[:top_n]
    sectors = sorted(sector_acc.items(), key=lambda x: -x[1])[:top_n]
    holdings = sorted(holding_acc.items(), key=lambda x: -x[1])[:top_n]

    return countries, sectors, holdings, etf_rows


def _print_table(title: str, rows: list[tuple[str, float]], budget: float, label: str = "Exposure") -> None:
    sep = "-" * 72
    print(f"\n{sep}")
    print(f"  {title}")
    print(sep)
    print(f"  {'Name':<40}  {'Weight':>8}  {'EUR Amount':>12}")
    print(f"  {'-'*40}  {'-'*8}  {'-'*12}")
    for name, pct in rows:
        eur = budget * pct / 100
        print(f"  {name:<40}  {pct:>7.2f}%  {eur:>10,.0f} EUR")
    print()


def _print_etfs(etf_rows: list[dict], budget: float) -> None:
    sep = "-" * 72
    print(f"\n{sep}")
    print("  Portfolio Composition")
    print(sep)
    print(f"  {'ISIN':<14}  {'Name':<46}  {'Alloc':>6}  {'EUR':>8}  {'TER':>5}  {'AUM(MEUR)':>9}")
    print(f"  {'-'*14}  {'-'*46}  {'-'*6}  {'-'*8}  {'-'*5}  {'-'*9}")
    for r in etf_rows:
        name = r["name"][:46]
        ter = f"{r['ter_pct']:.2f}%" if r["ter_pct"] else "  n/a"
        aum = f"{r['fund_size_eur_mln']:,.0f}" if r["fund_size_eur_mln"] else "  n/a"
        eur = budget * r["weight_pct"] / 100
        print(f"  {r['isin']:<14}  {name:<46}  {r['weight_pct']:>5.1f}%  {eur:>7,.0f}EUR  {ter:>5}  {aum:>9}")
    print()


def analyze(
    isins: list[str],
    weights: list[float] | None = None,
    budget: float = 10_000.0,
    top_n: int = 15,
    data_path: str = "data/profiles.json",
) -> None:
    """
    Print cumulative country/sector/holding exposure for a portfolio.

    Args:
        isins:   List of ISINs (duplicates are deduplicated).
        weights: Portfolio weights (must sum to 1). If None, equal weight.
        budget:  Total investment amount in €.
        top_n:   How many rows to show per table.
    """
    # Deduplicate while preserving order
    seen = set()
    isins = [i for i in isins if not (i in seen or seen.add(i))]

    if weights is None:
        n = len(isins)
        weights = [1 / n] * n
    else:
        if len(weights) != len(isins):
            raise ValueError("len(weights) must equal len(isins)")
        total = sum(weights)
        weights = [w / total for w in weights]  # normalise

    index = load_index(data_path)
    countries, sectors, holdings, etf_rows = _aggregate(isins, weights, index, top_n)

    weighted_ter = sum(
        r["weight_pct"] / 100 * (r["ter_pct"] or 0) for r in etf_rows
    )

    print(f"\n{'='*72}")
    print(f"  Portfolio Exposure  |  Budget: {budget:,.0f} EUR  |  ETFs: {len(isins)}")
    print(f"  Weighted avg TER: {weighted_ter:.3f}%")
    print(f"{'='*72}")

    _print_etfs(etf_rows, budget)
    _print_table(f"Country Exposure (top {top_n})", countries, budget)
    _print_table(f"Sector Exposure (top {top_n})", sectors, budget)
    _print_table(f"Top Holdings (top {top_n})", holdings, budget)


# ── CLI ──────────────────────────────────────────────────────────────────────

ISINS = [
    "IE000XJA2OU4",
    "IE000FF2EBQ8",
    "IE000G0E83X3",
    "IE000LGWDNE5",
    "IE00BKTLJC87",
    "IE0000ZL1RD2",
    "IE000XJA2OU4",   # duplicate — will be dropped
    "IE00BP3QZD73",
    "IE000AON7ET1",
    "IE000Y9MG996",
    "IE000NX8S1Z1",
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio exposure analyzer")
    parser.add_argument("--budget", type=float, default=10_000.0)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--data", default="data/profiles.json")
    args = parser.parse_args()

    analyze(ISINS, budget=args.budget, top_n=args.top, data_path=args.data)
