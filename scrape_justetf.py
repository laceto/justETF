"""
JustETF ETF Profile Scraper
============================
Scrapes the profile page for any ETF on justetf.com given an ISIN.

The page is server-side rendered (Apache Wicket), so a plain HTTP request
with browser impersonation (curl_cffi) is sufficient — no headless browser
needed.

Data extracted
--------------
- Fund overview      : name, ISIN, TER, fund size, index, replication style
- Top holdings       : name + weight (%)
- Country exposure   : country + weight (%)
- Sector exposure    : sector + weight (%)
- Performance        : YTD, 1M, 3M, 6M, 1Y, 3Y, 5Y, MAX + annual returns
- Risk metrics       : volatility, return/risk ratio, max drawdown
- Exchange listings  : exchange, currency, ticker
- Similar ETFs       : competing funds with size/TER

Usage
-----
    python scrape_justetf.py                          # default: IE00B5BMR087
    python scrape_justetf.py --isin IE00B4L5Y983      # different ETF
    python scrape_justetf.py --lang de                # language (it/de/en/fr/...)
    python scrape_justetf.py --out result.json        # save to JSON file

Invariants
----------
- curl_cffi impersonates Chrome to avoid 403s from Cloudflare/bot detection.
- All floats are stored as Python float (percentage sign stripped).
- Missing / unparseable cells are stored as None, never skipped.
- The function `fetch_etf_profile()` returns a typed dict — easy to extend.

Debugging
---------
- Pass `--debug` to dump the raw HTML to `debug_response.html`.
- Enable logging with LOG_LEVEL=DEBUG.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup, Tag

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WeightedItem:
    name: str
    weight_pct: Optional[float]  # e.g. 7.60 means 7.60 %


@dataclass
class PerformanceRecord:
    period: str           # e.g. "YTD", "1 anno", "2024"
    return_pct: Optional[float]


@dataclass
class RiskMetric:
    label: str
    value: Optional[float]


@dataclass
class ExchangeListing:
    exchange: str
    currency: str
    ticker: str
    bloomberg: Optional[str]
    reuters: Optional[str]


@dataclass
class SimilarETF:
    name: str
    aum_eur_mln: Optional[float]
    ter_pct: Optional[float]
    distribution: str
    replication: str


@dataclass
class ETFProfile:
    isin: str
    name: str
    # Overview
    index: Optional[str] = None
    investment_focus: Optional[str] = None
    fund_size_eur_mln: Optional[float] = None
    ter_pct: Optional[float] = None
    replication: Optional[str] = None
    distribution: Optional[str] = None
    # Exposure
    top_holdings: list[WeightedItem] = field(default_factory=list)
    country_exposure: list[WeightedItem] = field(default_factory=list)
    sector_exposure: list[WeightedItem] = field(default_factory=list)
    # Performance & risk
    performance: list[PerformanceRecord] = field(default_factory=list)
    risk_metrics: list[RiskMetric] = field(default_factory=list)
    # Market data
    exchange_listings: list[ExchangeListing] = field(default_factory=list)
    similar_etfs: list[SimilarETF] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_pct(raw: str) -> Optional[float]:
    """Convert '7,60%' or '+23,83%' or '-13,30%' to float."""
    cleaned = raw.replace("%", "").replace(",", ".").replace("\xa0", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_eur_mln(raw: str) -> Optional[float]:
    """Convert 'EUR 112.876 mln' or '40.296' to float (millions)."""
    cleaned = re.sub(r"[^\d,\.]", "", raw).replace(",", "").replace(".", "")
    # raw may be '112.876' (European thousand separator) → 112876
    # or plain '40296' → 40296; keep as millions
    # try parsing the dot-separated form directly
    m = re.search(r"[\d\.]+", raw)
    if m:
        try:
            # European format: dots are thousands separators
            return float(m.group().replace(".", ""))
        except ValueError:
            pass
    return None


def _parse_ter(raw: str) -> Optional[float]:
    """Convert '0,07% annuo' or '0,05% p.a.' to float (percentage)."""
    m = re.search(r"[\d,\.]+", raw)
    if m:
        try:
            return float(m.group().replace(",", "."))
        except ValueError:
            pass
    return None


def _rows_to_weighted_items(table: Tag) -> list[WeightedItem]:
    items = []
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            name = cells[0].get_text(strip=True)
            weight = _parse_pct(cells[1].get_text(strip=True))
            if name:
                items.append(WeightedItem(name=name, weight_pct=weight))
    return items


def _rows_to_kv(table: Tag) -> dict[str, str]:
    """Parse a 2-column key/value table."""
    result = {}
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            key = cells[0].get_text(strip=True)
            val = cells[1].get_text(strip=True)
            if key:
                result[key] = val
    return result


# ---------------------------------------------------------------------------
# Semantic table index
# ---------------------------------------------------------------------------

# Heading variants per section, across all supported languages.
# Keys are canonical section names used throughout this module.
# Values are lists of substrings; a heading matches if any substring is found
# (case-insensitive) in the heading text.
_HEADING_PATTERNS: dict[str, list[str]] = {
    "top_holdings":     ["principali posizioni", "top holdings", "top-positionen"],
    "country_exposure": ["paesi", "countries", "länder"],
    "sector_exposure":  ["settori", "sectors", "branchen"],
    "performance":      ["performance"],
    "risk":             ["rischio", "risk", "risiko"],
    "listings":         ["borse", "exchanges", "börsen"],
    "similar_etfs":     ["etf simili", "similar etfs", "ähnliche etfs"],
}


def _build_heading_table_map(soup: BeautifulSoup) -> dict[str, Tag]:
    """
    Walk the document once and return a mapping:
        canonical_section_name -> first <table> that follows its heading

    Strategy
    --------
    Iterate every <h2> and <h3> in document order.  For each heading, scan
    forward through following siblings (and the siblings of ancestor elements
    when the table is not a direct sibling) to find the first <table> tag.
    Match the heading text against _HEADING_PATTERNS to assign a section name.

    The walk is O(n) in the number of elements — no repeated full-document
    searches.
    """
    result: dict[str, Tag] = {}

    for heading in soup.find_all(["h2", "h3"]):
        heading_text = heading.get_text(strip=True).lower()

        # Identify which section this heading belongs to.
        section = _match_section(heading_text)
        if section is None:
            continue

        # Find the first <table> after this heading.
        table = _first_table_after(heading)
        if table is None:
            LOG.debug("Heading '%s' matched section '%s' but no table follows it",
                      heading.get_text(strip=True), section)
            continue

        if section not in result:
            # First match wins (avoids overwriting with a nested/repeated heading).
            result[section] = table
            LOG.debug("Section '%s' mapped to table via heading '%s'",
                      section, heading.get_text(strip=True))

    return result


def _match_section(heading_text: str) -> Optional[str]:
    """Return the canonical section name for a heading, or None if unrecognised."""
    for section, patterns in _HEADING_PATTERNS.items():
        for pat in patterns:
            if pat in heading_text:
                return section
    return None


def _first_table_after(element: Tag) -> Optional[Tag]:
    """
    Return the first <table> that appears after *element* in the document.

    Search order:
    1. Following siblings of the element itself.
    2. Following siblings of the element's parent, grandparent, etc.
       (handles cases where the heading and table are in different container divs
       that share a common ancestor).

    Stops as soon as another h2/h3 is encountered (bare or nested inside a
    sibling container) to avoid crossing section boundaries.
    """
    node: Optional[Tag] = element
    while node is not None:
        for sibling in node.next_siblings:
            if not isinstance(sibling, Tag):
                continue
            # Stop at the next section heading — bare or nested inside a div.
            if sibling.name in ("h2", "h3"):
                return None
            if sibling.find(["h2", "h3"]):
                # The sibling container holds the next heading; do not descend
                # into it looking for a table — that table belongs to the next
                # section.
                return None
            if sibling.name == "table":
                return sibling
            # Table may be nested inside a wrapper div.
            inner = sibling.find("table")
            if inner:
                return inner
        # Step up one level and continue searching from the parent's position.
        node = node.parent

    return None


def _lookup_table(
    section: str,
    heading_map: dict[str, Tag],
    positional_tables: list[Tag],
    positional_idx: int,
) -> Optional[Tag]:
    """
    Return the table for *section*, preferring the semantic heading map.

    Falls back to *positional_idx* in *positional_tables* when the heading map
    has no entry for the section (e.g. the section is absent on this ETF page,
    or the heading text was in an unrecognised language).  A WARNING is emitted
    on fallback so drifting indices are visible in logs.
    """
    table = heading_map.get(section)
    if table is not None:
        return table

    # Fallback: positional index.
    if positional_idx < len(positional_tables):
        LOG.warning(
            "Section '%s': heading not found, falling back to positional index %d",
            section, positional_idx,
        )
        return positional_tables[positional_idx]

    LOG.warning(
        "Section '%s': heading not found and positional index %d out of range "
        "(%d tables total) — section will be empty",
        section, positional_idx, len(positional_tables),
    )
    return None


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

BASE_URL = "https://www.justetf.com"


def fetch_etf_profile(
    isin: str,
    lang: str = "it",
    timeout: int = 30,
) -> ETFProfile:
    """
    Fetch and parse an ETF profile page from JustETF.

    Parameters
    ----------
    isin    : ETF ISIN, e.g. 'IE00B5BMR087'
    lang    : site language prefix ('it', 'de', 'en', 'fr', ...)
    timeout : HTTP timeout in seconds

    Returns
    -------
    ETFProfile dataclass with all available fields populated.

    Failure modes
    -------------
    - HTTP != 200  → raises RuntimeError
    - Missing table → that section is silently empty (logged as WARNING)
    """
    url = f"{BASE_URL}/{lang}/etf-profile.html"
    params = {"isin": isin}

    LOG.debug("GET %s params=%s", url, params)
    response = cf_requests.get(
        url,
        params=params,
        impersonate="chrome",
        timeout=timeout,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"HTTP {response.status_code} fetching {url}?isin={isin}"
        )

    return _parse_profile(isin, response.text)


def _parse_profile(isin: str, html: str) -> ETFProfile:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    LOG.debug("Found %d tables", len(tables))

    # Build the semantic heading → table map once; every _parse_* function
    # queries it via _lookup_table() so positional indices are only a fallback.
    heading_map = _build_heading_table_map(soup)
    LOG.debug("Heading map resolved sections: %s", list(heading_map.keys()))

    # --- ETF name ---
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else isin

    profile = ETFProfile(isin=isin, name=name)

    _parse_fund_overview(profile, tables)
    _parse_exposure(profile, heading_map, tables)
    _parse_performance(profile, heading_map, tables)
    _parse_risk(profile, heading_map, tables)
    _parse_listings(profile, heading_map, tables)
    _parse_similar(profile, heading_map, tables)

    return profile


def _get_table(tables: list[Tag], idx: int, section: str) -> Optional[Tag]:
    """Legacy positional lookup — retained for _parse_fund_overview."""
    if idx >= len(tables):
        LOG.warning("Table index %d not found (section=%s)", idx, section)
        return None
    return tables[idx]


def _parse_fund_overview(profile: ETFProfile, tables: list[Tag]) -> None:
    """Table 1 holds the main fund details.

    Fund overview has no dedicated h2/h3 heading (it is the page's primary
    content block), so positional lookup is kept here intentionally.
    """
    t1 = _get_table(tables, 1, "fund_overview")
    if t1:
        kv = _rows_to_kv(t1)
        # Normalise keys — they may be in Italian/German/English
        profile.index = _first_val(kv, ["Indice", "Index", "Indeks"])
        profile.investment_focus = _first_val(kv, ["Focus di investimento", "Anlageschwerpunkt", "Investment focus"])
        raw_size = _first_val(kv, ["Dimensione del fondo", "Fondsvermögen", "Fund size"])
        if raw_size:
            profile.fund_size_eur_mln = _parse_eur_mln(raw_size)
        raw_ter = _first_val(kv, ["Indicatore sintetico di spesa (TER)", "Gesamtkostenquote (TER)", "Total expense ratio (TER)"])
        if raw_ter:
            profile.ter_pct = _parse_ter(raw_ter)
        profile.replication = _first_val(kv, ["Replicazione", "Replikationsmethode", "Replication"])
        profile.distribution = _first_val(kv, ["Distribuzione", "Ausschüttung", "Distribution"])


def _first_val(kv: dict[str, str], keys: list[str]) -> Optional[str]:
    for k in keys:
        if k in kv:
            return kv[k]
    # fuzzy: substring match
    for k in keys:
        for actual_key, val in kv.items():
            if k.lower() in actual_key.lower():
                return val
    return None


def _parse_exposure(
    profile: ETFProfile,
    heading_map: dict[str, Tag],
    tables: list[Tag],
) -> None:
    """
    Populate top_holdings, country_exposure, and sector_exposure.

    Table selection uses semantic heading detection (_build_heading_table_map).
    Positional indices 6/7/8 are retained as fallbacks and are only used when
    heading detection fails (logged as WARNING).  Positional indices were
    validated against ISIN IE00B5BMR087.
    """
    t_holdings = _lookup_table("top_holdings",     heading_map, tables, 6)
    t_country   = _lookup_table("country_exposure", heading_map, tables, 7)
    t_sector    = _lookup_table("sector_exposure",  heading_map, tables, 8)

    if t_holdings:
        profile.top_holdings = _rows_to_weighted_items(t_holdings)
        LOG.debug("Parsed %d holdings", len(profile.top_holdings))

    if t_country:
        profile.country_exposure = _rows_to_weighted_items(t_country)
        LOG.debug("Parsed %d country entries", len(profile.country_exposure))

    if t_sector:
        profile.sector_exposure = _rows_to_weighted_items(t_sector)
        LOG.debug("Parsed %d sector entries", len(profile.sector_exposure))


def _parse_performance(
    profile: ETFProfile,
    heading_map: dict[str, Tag],
    tables: list[Tag],
) -> None:
    """Returns table (YTD + annual).  Positional fallback index: 10."""
    t = _lookup_table("performance", heading_map, tables, 10)
    if not t:
        return
    for row in t.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            period = cells[0].get_text(strip=True)
            ret = _parse_pct(cells[1].get_text(strip=True))
            if period:
                profile.performance.append(PerformanceRecord(period=period, return_pct=ret))
    LOG.debug("Parsed %d performance records", len(profile.performance))


def _parse_risk(
    profile: ETFProfile,
    heading_map: dict[str, Tag],
    tables: list[Tag],
) -> None:
    """Volatility + drawdown metrics table.  Positional fallback index: 11."""
    t = _lookup_table("risk", heading_map, tables, 11)
    if not t:
        return
    for row in t.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True)
            raw = cells[1].get_text(strip=True)
            # Values are either % or plain numbers
            val = _parse_pct(raw) if "%" in raw else _safe_float(raw)
            if label:
                profile.risk_metrics.append(RiskMetric(label=label, value=val))
    LOG.debug("Parsed %d risk metrics", len(profile.risk_metrics))


def _safe_float(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ".").strip())
    except ValueError:
        return None


def _parse_listings(
    profile: ETFProfile,
    heading_map: dict[str, Tag],
    tables: list[Tag],
) -> None:
    """Exchange listings table.  Positional fallback index: 12."""
    t = _lookup_table("listings", heading_map, tables, 12)
    if not t:
        return
    rows = t.find_all("tr")
    for row in rows[1:]:  # skip header
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) >= 3:
            profile.exchange_listings.append(ExchangeListing(
                exchange=cells[0],
                currency=cells[1],
                ticker=cells[2],
                bloomberg=cells[3] if len(cells) > 3 else None,
                reuters=cells[4] if len(cells) > 4 else None,
            ))
    LOG.debug("Parsed %d exchange listings", len(profile.exchange_listings))


def _parse_similar(
    profile: ETFProfile,
    heading_map: dict[str, Tag],
    tables: list[Tag],
) -> None:
    """Similar/competing ETFs table.  Positional fallback index: 13."""
    t = _lookup_table("similar_etfs", heading_map, tables, 13)
    if not t:
        return
    rows = t.find_all("tr")
    for row in rows[1:]:  # skip header
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) >= 5:
            profile.similar_etfs.append(SimilarETF(
                name=cells[0],
                aum_eur_mln=_parse_eur_mln(cells[1]),
                ter_pct=_parse_ter(cells[2]),
                distribution=cells[3],
                replication=cells[4],
            ))
    LOG.debug("Parsed %d similar ETFs", len(profile.similar_etfs))


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def _print_profile(p: ETFProfile) -> None:
    sep = "-" * 55

    print(f"\n{'=' * 55}")
    print(f"  {p.name}")
    print(f"  ISIN: {p.isin}")
    print(f"{'=' * 55}\n")

    print("FUND OVERVIEW")
    print(sep)
    for label, val in [
        ("Index",           p.index),
        ("Focus",           p.investment_focus),
        ("Fund size",       f"EUR {p.fund_size_eur_mln:,.0f} mln" if p.fund_size_eur_mln else None),
        ("TER",             f"{p.ter_pct}% p.a." if p.ter_pct is not None else None),
        ("Replication",     p.replication),
        ("Distribution",    p.distribution),
    ]:
        if val:
            print(f"  {label:<20} {val}")

    _print_section("TOP HOLDINGS", p.top_holdings)
    _print_section("COUNTRY EXPOSURE", p.country_exposure)
    _print_section("SECTOR EXPOSURE", p.sector_exposure)

    if p.performance:
        print(f"\nPERFORMANCE")
        print(sep)
        for r in p.performance:
            sign = "+" if (r.return_pct or 0) >= 0 else ""
            pct = f"{sign}{r.return_pct:.2f}%" if r.return_pct is not None else "N/A"
            print(f"  {r.period:<25} {pct:>10}")

    if p.risk_metrics:
        print(f"\nRISK METRICS")
        print(sep)
        for m in p.risk_metrics:
            print(f"  {m.label:<35} {m.value}")

    if p.exchange_listings:
        print(f"\nEXCHANGE LISTINGS")
        print(sep)
        for lst in p.exchange_listings:
            print(f"  {lst.exchange:<35} {lst.currency}  {lst.ticker}")

    if p.similar_etfs:
        print(f"\nSIMILAR ETFs")
        print(sep)
        for s in p.similar_etfs:
            ter = f"{s.ter_pct}%" if s.ter_pct is not None else "?"
            aum = f"{s.aum_eur_mln:,.0f}" if s.aum_eur_mln is not None else "?"
            print(f"  {s.name[:45]:<46} AUM={aum}  TER={ter}")

    print()


def _print_section(title: str, items: list[WeightedItem]) -> None:
    if not items:
        return
    print(f"\n{title}")
    print("-" * 55)
    for item in items:
        pct = f"{item.weight_pct:.2f}%" if item.weight_pct is not None else "N/A"
        print(f"  {item.name:<40} {pct:>8}")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape an ETF profile from JustETF",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--isin", default="IE00B5BMR087", help="ETF ISIN")
    p.add_argument("--lang", default="it", help="Site language (it/de/en/fr/...)")
    p.add_argument("--out", default=None, help="Save JSON output to this file")
    p.add_argument("--debug", action="store_true", help="Dump raw HTML to debug_response.html")
    p.add_argument("--quiet", action="store_true", help="Suppress pretty-print output")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(message)s")

    if args.debug:
        # Fetch and dump raw HTML for inspection
        url = f"{BASE_URL}/{args.lang}/etf-profile.html"
        raw = cf_requests.get(url, params={"isin": args.isin}, impersonate="chrome", timeout=30)
        Path("debug_response.html").write_text(raw.text, encoding="utf-8")
        LOG.debug("Raw HTML saved to debug_response.html")

    LOG.info("Fetching %s ...", args.isin)
    profile = fetch_etf_profile(isin=args.isin, lang=args.lang)

    if not args.quiet:
        _print_profile(profile)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(asdict(profile), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        LOG.info("Saved JSON to %s", out_path)
    else:
        # Always print JSON to stdout if no file specified
        print(json.dumps(asdict(profile), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
