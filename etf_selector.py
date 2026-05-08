"""
ETF Selector — find the best ETFs by country, sector, holding, or theme.

Usage (interactive):
    python etf_selector.py

Usage (import):
    from etf_selector import load_data, find_by_country, find_by_sector, find_by_holding, find_by_theme
    df = load_data()
    find_by_country("South Korea", df)
    find_by_sector("Health Care", df)
    find_by_holding("NVIDIA", df)
    find_by_theme("AI", df)
"""

import re
import textwrap
import pandas as pd

# ─── Constants ────────────────────────────────────────────────────────────────

REAL_SECTORS = {
    "Basic Materials", "Consumer Discretionary", "Consumer Staples",
    "Energy", "Financials", "Health Care", "Industrials", "Real Estate",
    "Technology", "Telecommunication", "Utilities",
}

# Labels that leaked into country/sector fields from the performance table
_NOISE = {
    "1 month", "3 months", "6 months", "1 year", "3 years", "5 years",
    "YTD", "2022", "2023", "2024", "2025", "Other",
    "Current dividend yield", "Dividends (last 12 months)",
    "Since inception (MAX)", "Broker",
}

DISPLAY_COLS = ["isin", "name", "ter_pct", "fund_size_eur_mln",
                "replication", "distribution", "ytd_pct", "index"]

# ─── Load ─────────────────────────────────────────────────────────────────────

def load_data(path: str = "data/profiles.jsonl") -> pd.DataFrame:
    """Load profiles and return the raw DataFrame."""
    return pd.read_json(path, lines=True)


# ─── Scoring ──────────────────────────────────────────────────────────────────

def _quality_score(row: pd.Series) -> float:
    """
    Composite quality score (higher = better).
    Rewards large funds and penalises high TER.
    Capped so a giant fund can't drown out a cheap TER.
    """
    score = 0.0
    if pd.notna(row.get("ter_pct")) and row["ter_pct"] > 0:
        score -= row["ter_pct"] * 20          # 0.20% TER → -4 pts
    if pd.notna(row.get("fund_size_eur_mln")) and row["fund_size_eur_mln"] > 0:
        score += min(row["fund_size_eur_mln"] / 500, 10)  # cap at 10 pts (~5B)
    return round(score, 2)


def _pivot_risk(metrics: list) -> dict:
    """Flatten the risk_metrics list into a flat dict."""
    return {m["label"]: m["value"] for m in (metrics or []) if isinstance(m, dict)}


# ─── Shared display helper ────────────────────────────────────────────────────

def _format_results(
    df: pd.DataFrame,
    weight_col: str,
    top_n: int,
    extra_cols=None,
) -> pd.DataFrame:
    df = df.copy()
    df["quality_score"] = df.apply(_quality_score, axis=1)
    df = df.sort_values([weight_col, "quality_score"], ascending=[False, False])

    cols = [weight_col] + DISPLAY_COLS + (extra_cols or []) + ["quality_score"]
    cols = [c for c in cols if c in df.columns]
    return df[cols].head(top_n).reset_index(drop=True)


def _print(df: pd.DataFrame, title: str, weight_col: str) -> None:
    pd.set_option("display.max_colwidth", 55)
    pd.set_option("display.float_format", "{:.2f}".format)
    pd.set_option("display.width", 160)
    sep = "-" * 80
    print(f"\n{sep}")
    print(f"  {title}")
    print(sep)
    print(df.to_string(index=True))
    print()


# ─── Search functions ─────────────────────────────────────────────────────────

def find_by_country(
    country: str,
    df: pd.DataFrame,
    min_weight: float = 5.0,
    top_n: int = 15,
    print_results: bool = True,
) -> pd.DataFrame:
    """
    Rank ETFs by allocation to a specific country.

    Args:
        country:    Country name, e.g. "South Korea", "Japan", "Germany".
        df:         DataFrame from load_data().
        min_weight: Minimum country allocation % to include (default 5%).
        top_n:      Number of results to return.
    """
    rows = []
    for _, row in df.iterrows():
        exposures = row.get("country_exposure") or []
        if not isinstance(exposures, list):
            continue
        for item in exposures:
            label = item.get("name", "")
            if label in _NOISE:
                continue
            if label.lower() == country.lower():
                w = item.get("weight_pct") or 0
                if w >= min_weight:
                    rows.append({**row.to_dict(), "country_weight_pct": w})
                break

    if not rows:
        print(f"[find_by_country] No ETFs with >={min_weight}% exposure to '{country}'.")
        print(f"  Tip: check spelling or lower min_weight.")
        return pd.DataFrame()

    result = _format_results(pd.DataFrame(rows), "country_weight_pct", top_n)
    if print_results:
        _print(result, f"ETFs with exposure to {country} (>={min_weight}%)", "country_weight_pct")
    return result


def find_by_sector(
    sector: str,
    df: pd.DataFrame,
    min_weight: float = 10.0,
    top_n: int = 15,
    print_results: bool = True,
) -> pd.DataFrame:
    """
    Rank ETFs by allocation to a specific sector.

    Args:
        sector:     Sector name. Valid values:
                    Basic Materials, Consumer Discretionary, Consumer Staples,
                    Energy, Financials, Health Care, Industrials, Real Estate,
                    Technology, Telecommunication, Utilities
        df:         DataFrame from load_data().
        min_weight: Minimum sector allocation % to include (default 10%).
        top_n:      Number of results to return.
    """
    # Fuzzy-match sector name
    matched_sector = next(
        (s for s in REAL_SECTORS if s.lower() == sector.lower()), None
    )
    if matched_sector is None:
        close = [s for s in REAL_SECTORS if sector.lower() in s.lower()]
        print(f"[find_by_sector] Unknown sector '{sector}'.")
        print(f"  Valid sectors: {sorted(REAL_SECTORS)}")
        if close:
            print(f"  Did you mean: {close}?")
        return pd.DataFrame()

    rows = []
    for _, row in df.iterrows():
        exposures = row.get("sector_exposure") or []
        if not isinstance(exposures, list):
            continue
        for item in exposures:
            label = item.get("name", "")
            if label not in REAL_SECTORS:
                continue
            if label == matched_sector:
                w = item.get("weight_pct") or 0
                if w >= min_weight:
                    rows.append({**row.to_dict(), "sector_weight_pct": w})
                break

    if not rows:
        print(f"[find_by_sector] No ETFs with >={min_weight}% in '{matched_sector}'.")
        return pd.DataFrame()

    result = _format_results(pd.DataFrame(rows), "sector_weight_pct", top_n)
    if print_results:
        _print(result, f"ETFs with {matched_sector} allocation >={min_weight}%", "sector_weight_pct")
    return result


def find_by_holding(
    company: str,
    df: pd.DataFrame,
    top_n: int = 15,
    print_results: bool = True,
) -> pd.DataFrame:
    """
    Find ETFs that hold a specific company (fuzzy name match on top_holdings).

    Args:
        company:    Company name or partial name, e.g. "NVIDIA", "Samsung", "Apple".
        df:         DataFrame from load_data().
        top_n:      Number of results to return.
    """
    pattern = re.compile(re.escape(company), re.IGNORECASE)
    rows = []
    for _, row in df.iterrows():
        holdings = row.get("top_holdings") or []
        if not isinstance(holdings, list):
            continue
        for item in holdings:
            if pattern.search(item.get("name", "")):
                rows.append({
                    **row.to_dict(),
                    "holding_weight_pct": item.get("weight_pct") or 0,
                    "matched_holding": item["name"],
                })
                break

    if not rows:
        print(f"[find_by_holding] No ETFs found holding '{company}' in top holdings.")
        return pd.DataFrame()

    result = _format_results(
        pd.DataFrame(rows), "holding_weight_pct", top_n,
        extra_cols=["matched_holding"],
    )
    if print_results:
        _print(result, f"ETFs holding '{company}'", "holding_weight_pct")
    return result


def find_by_theme(
    keyword: str,
    df: pd.DataFrame,
    top_n: int = 15,
    print_results: bool = True,
) -> pd.DataFrame:
    """
    Find thematic ETFs by keyword matched against name, index, and category.
    Best for broad themes: 'AI', 'robotics', 'battery', 'clean energy', 'dividend'.

    Args:
        keyword:    Theme keyword(s), e.g. "AI", "artificial intelligence", "semiconductor".
        df:         DataFrame from load_data().
        top_n:      Number of results to return.
    """
    mask = (
        df["name"].str.contains(keyword, case=False, na=False)
        | df["index"].str.contains(keyword, case=False, na=False)
        | df["investment_focus"].str.contains(keyword, case=False, na=False)
        | df["category"].str.contains(keyword, case=False, na=False)
    )
    result = df[mask].copy()
    if result.empty:
        print(f"[find_by_theme] No ETFs matching theme '{keyword}'.")
        return pd.DataFrame()

    result["quality_score"] = result.apply(_quality_score, axis=1)
    result = result.sort_values(
        ["fund_size_eur_mln", "quality_score"], ascending=[False, False]
    )
    cols = DISPLAY_COLS + ["quality_score"]
    cols = [c for c in cols if c in result.columns]
    result = result[cols].head(top_n).reset_index(drop=True)

    if print_results:
        _print(result, f"Thematic ETFs matching '{keyword}'", "quality_score")
    return result


def find_by_combo(
    df: pd.DataFrame,
    country=None,
    sector=None,
    theme=None,
    min_country_weight: float = 5.0,
    min_sector_weight: float = 10.0,
    top_n: int = 15,
    print_results: bool = True,
) -> pd.DataFrame:
    """
    Filter ETFs that satisfy multiple criteria simultaneously.
    All non-None filters must be satisfied (AND logic).

    Example — ETFs with South Korea exposure AND Technology sector:
        find_by_combo(df, country="South Korea", sector="Technology")
    """
    # Build candidate sets per filter
    sets = []

    if country:
        r = find_by_country(country, df, min_country_weight, top_n=9999, print_results=False)
        if r.empty:
            return pd.DataFrame()
        sets.append(set(r["isin"]))

    if sector:
        r = find_by_sector(sector, df, min_sector_weight, top_n=9999, print_results=False)
        if r.empty:
            return pd.DataFrame()
        sets.append(set(r["isin"]))

    if theme:
        r = find_by_theme(theme, df, top_n=9999, print_results=False)
        if r.empty:
            return pd.DataFrame()
        sets.append(set(r["isin"]))

    if not sets:
        print("[find_by_combo] Provide at least one filter.")
        return pd.DataFrame()

    common_isins = sets[0].intersection(*sets[1:])
    result = df[df["isin"].isin(common_isins)].copy()
    if result.empty:
        print("[find_by_combo] No ETFs satisfy all filters simultaneously.")
        return pd.DataFrame()

    result["quality_score"] = result.apply(_quality_score, axis=1)
    result = result.sort_values(
        ["fund_size_eur_mln", "quality_score"], ascending=[False, False]
    )
    cols = DISPLAY_COLS + ["quality_score"]
    cols = [c for c in cols if c in result.columns]
    result = result[cols].head(top_n).reset_index(drop=True)

    label_parts = [p for p in [country, sector, theme] if p]
    label = " + ".join(label_parts)
    if print_results:
        _print(result, f"ETFs matching: {label}", "quality_score")
    return result


def find_by_holdings_multi(
    companies: "list[str | tuple[str, str]]",
    df: pd.DataFrame,
    top_n: int = 8,
    print_results: bool = True,
) -> "dict[str, pd.DataFrame]":
    """
    Search for multiple companies at once and return ETFs holding each.

    Args:
        companies: List of company name strings, or (name, country_label) tuples.
                   Name is matched as a case-insensitive substring against top_holdings.
        df:        DataFrame from load_data().
        top_n:     Max ETFs to show per company (sorted by holding weight desc).

    Returns:
        Dict mapping each company name to a DataFrame with columns:
        isin, name, matched_holding, holding_weight_pct.

    Example:
        find_by_holdings_multi([
            ("Rheinmetall", "DE"),
            ("Thales", "FR"),
            ("BAE Systems", "UK"),
        ], df)
    """
    results = {}
    for entry in companies:
        if isinstance(entry, tuple):
            company, country_label = entry
        else:
            company, country_label = entry, None

        label = f"{company} ({country_label})" if country_label else company
        pattern = re.compile(re.escape(company), re.IGNORECASE)
        rows = []
        for _, row in df.iterrows():
            holdings = row.get("top_holdings") or []
            if not isinstance(holdings, list):
                continue
            for item in holdings:
                if pattern.search(item.get("name", "")):
                    rows.append({
                        "isin": row["isin"],
                        "name": row.get("name", ""),
                        "matched_holding": item["name"],
                        "holding_weight_pct": item.get("weight_pct") or 0,
                    })
                    break

        if not rows:
            if print_results:
                print(f"\n{label}: -- not found in any ETF top holdings")
            results[company] = pd.DataFrame()
            continue

        result = (
            pd.DataFrame(rows)
            .sort_values("holding_weight_pct", ascending=False)
            .head(top_n)
            .reset_index(drop=True)
        )
        results[company] = result

        if print_results:
            sep = "-" * 80
            print(f"\n{sep}")
            print(f"  {label}")
            print(sep)
            for _, r in result.iterrows():
                print(
                    f"  {r['isin']}  "
                    f"{r['name'][:65]:<65}  "
                    f"[{r['matched_holding']}  {r['holding_weight_pct']:.2f}%]"
                )

    return results


def list_countries(df: pd.DataFrame) -> "list[str]":
    """Return all country names present in the dataset."""
    countries = set()
    for row in df["country_exposure"].dropna():
        if isinstance(row, list):
            for item in row:
                name = item.get("name", "")
                if name and name not in _NOISE:
                    countries.add(name)
    return sorted(countries)


def list_sectors() -> "list[str]":
    """Return all valid sector names."""
    return sorted(REAL_SECTORS)


# ─── Non-US comparables ───────────────────────────────────────────────────────

NON_US_COMPARABLES = [
    # Semiconductors
    ("Infineon", "DE"),
    ("STMicro", "CH/FR"),
    ("NXP", "NL"),
    ("Tokyo Electron", "JP"),
    ("ASM International", "NL"),
    ("Advantest", "JP"),
    ("MediaTek", "TW"),
    ("Renesas", "JP"),
    # Defence / Space
    ("Rheinmetall", "DE"),
    ("Thales", "FR"),
    ("Leonardo", "IT"),
    ("BAE Systems", "UK"),
    ("Saab", "SE"),
    ("Hensoldt", "DE"),
    ("QinetiQ", "UK"),
    # Networking / Telecom
    ("Ericsson", "SE"),
    # Energy / Grid infrastructure
    ("Siemens Energy", "DE"),
    ("Siemens", "DE"),
    ("Orsted", "DK"),
    ("Vestas", "DK"),
    ("RWE", "DE"),
    # Industrial machinery
    ("Atlas Copco", "SE"),
    ("Epiroc", "SE"),
    ("CNH Industrial", "IT/UK"),
    ("Kubota", "JP"),
]


# ─── CLI demo ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Loading ETF data...")
    df = load_data()
    print(f"Loaded {len(df):,} ETFs.\n")

    print("=" * 80)
    print("Non-US comparable companies -- ETFs holding each")
    find_by_holdings_multi(NON_US_COMPARABLES, df, top_n=3)
