"""
JustETF ETF List Overview Scraper
===================================
Scrapes https://www.justetf.com/en/etf-list-overview.html and extracts all
ETF entries grouped by geographic/thematic category.

The page is server-side rendered and embeds ETF data as inline JavaScript
arrays (``var id10Etfs = [{...}, ...];``), so no headless browser is needed.

Data extracted per ETF
----------------------
- category        : e.g. "Europe", "Global/World", "Emerging Markets"
- fund_name       : full ETF name
- isin            : ISIN identifier
- wkn             : German security identifier (WKN)
- ter_pct         : Total Expense Ratio as float (e.g. 0.07)
- fund_size_mln   : AUM in EUR millions as float
- ytd_pct         : Year-to-date return as float
- inception_date  : fund launch date string (DD.MM.YY)
- replication     : replication method (HTML tags stripped)
- distribution    : "Accumulating" or "Distributing"
- savings_plan    : True/False - whether a savings plan is available
- profile_url     : direct link to the ETF profile page on justETF
- savings_plan_url: link to savings plan comparison page (or None)

Usage
-----
    python scrape_etf_list.py                    # print table + save CSV
    python scrape_etf_list.py --out etfs.csv     # custom CSV output path
    python scrape_etf_list.py --json etfs.json   # also save JSON
    python scrape_etf_list.py --lang de          # German version of the page
    python scrape_etf_list.py --no-print         # suppress terminal output
    python scrape_etf_list.py --debug            # dump raw HTML for inspection
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup

LOG = logging.getLogger(__name__)

BASE_URL = "https://www.justetf.com"

def _extract_category_map(soup: BeautifulSoup) -> dict[str, str]:
    """
    Build a mapping of JS variable name → category label by reading the HTML.

    Each category section has a <table data-id="idXXEtfs"> and an <h3> heading
    (e.g. "Equity Europe") inside the same container div. We pair them here so
    the mapping is always in sync with the live page, with no hardcoded table.
    """
    mapping: dict[str, str] = {}
    for table in soup.find_all("table", attrs={"data-id": True}):
        var_name: str = table["data-id"]          # e.g. "id14Etfs"
        h = table.find_previous(["h2", "h3", "h4", "h5"])
        if h:
            mapping[var_name] = h.get_text(strip=True)
        else:
            mapping[var_name] = var_name          # fallback
    return mapping


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ETFEntry:
    category: str
    fund_name: str
    isin: str
    wkn: Optional[str]
    ter_pct: Optional[float]
    fund_size_mln: Optional[float]
    ytd_pct: Optional[float]
    inception_date: Optional[str]
    replication: Optional[str]
    distribution: Optional[str]
    savings_plan: bool
    profile_url: str
    savings_plan_url: Optional[str]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    """Remove HTML tags and decode entities."""
    return BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)


def _parse_pct(raw: Optional[str]) -> Optional[float]:
    """'0.65%' or '1.42%' → 0.65"""
    if not raw:
        return None
    cleaned = raw.replace("%", "").replace(",", ".").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_float(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    cleaned = raw.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _has_savings_plan(raw: Optional[str]) -> bool:
    """The savings_plan field contains an HTML check-icon string when available."""
    if not raw:
        return False
    return "check" in raw.lower() or "savings" in raw.lower()


def _build_profile_url(isin: str, lang: str) -> str:
    return f"{BASE_URL}/{lang}/etf-profile.html?isin={isin}"


def _parse_js_array(js_text: str, var_name: str) -> list[dict]:
    """
    Extract a JS array assigned to var_name from js_text and parse as JSON.

    Handles:
    - Trailing commas inside objects/arrays (common in hand-written JS)
    - Single-quoted strings by converting to double-quotes
    """
    # Match: var <name> = [<content>];
    pattern = rf'var\s+{re.escape(var_name)}\s*=\s*(\[.*?\]);'
    m = re.search(pattern, js_text, re.DOTALL)
    if not m:
        return []

    raw = m.group(1)

    # Fix trailing commas before } or ] — JSON does not allow them
    raw = re.sub(r',\s*([}\]])', r'\1', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        LOG.warning("JSON parse error for %s: %s", var_name, exc)
        return []


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

def fetch_etf_list(lang: str = "en", timeout: int = 30) -> list[ETFEntry]:
    """
    Fetch the JustETF list-overview page and return all ETF entries.

    Parameters
    ----------
    lang    : site language prefix ('en', 'de', 'it', ...)
    timeout : HTTP timeout in seconds
    """
    url = f"{BASE_URL}/{lang}/etf-list-overview.html"
    LOG.info("Fetching %s", url)

    response = cf_requests.get(url, impersonate="chrome", timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} from {url}")

    return _parse_list_page(response.text, lang)


def _parse_list_page(html: str, lang: str) -> list[ETFEntry]:
    soup = BeautifulSoup(html, "html.parser")
    category_map = _extract_category_map(soup)

    # Find all JS array variable names, e.g. id10Etfs, id13Etfs, ...
    var_names = re.findall(r'var\s+(id(\d+)Etfs)\s*=\s*\[', html)
    if not var_names:
        LOG.error("No ETF arrays found in page HTML. The page structure may have changed.")
        return []

    LOG.info("Found %d categories", len(var_names))

    entries: list[ETFEntry] = []
    for var_name, cat_id in var_names:
        category = category_map.get(var_name, f"Category {cat_id}")
        records = _parse_js_array(html, var_name)
        LOG.debug("Category %s (%s): %d ETFs", category, var_name, len(records))

        for rec in records:
            isin = rec.get("isin", "")
            sp_url_raw = rec.get("savingsPlanUrl", "") or ""
            sp_url = (BASE_URL + sp_url_raw) if sp_url_raw else None

            entry = ETFEntry(
                category=category,
                fund_name=rec.get("fundName", ""),
                isin=isin,
                wkn=rec.get("wkn") or None,
                ter_pct=_parse_pct(rec.get("ter")),
                fund_size_mln=_parse_float(rec.get("fundSize")),
                ytd_pct=_parse_pct(rec.get("ytd")),
                inception_date=rec.get("inceptionDate") or None,
                replication=_strip_html(rec.get("replicationMethod", "")) or None,
                distribution=rec.get("distributionPolicy") or None,
                savings_plan=_has_savings_plan(rec.get("savingsPlan")),
                profile_url=_build_profile_url(isin, lang),
                savings_plan_url=sp_url,
            )
            entries.append(entry)

    LOG.info("Total ETFs collected: %d", len(entries))
    return entries


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "category", "fund_name", "isin", "wkn",
    "ter_pct", "fund_size_mln", "ytd_pct", "inception_date",
    "replication", "distribution", "savings_plan",
    "profile_url", "savings_plan_url",
]


def save_csv(entries: list[ETFEntry], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for e in entries:
            writer.writerow(asdict(e))
    LOG.info("CSV saved: %s (%d rows)", path, len(entries))


def save_json(entries: list[ETFEntry], path: Path) -> None:
    path.write_text(
        json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    LOG.info("JSON saved: %s", path)


def print_table(entries: list[ETFEntry]) -> None:
    """Print a compact summary table to stdout."""
    col_widths = {"category": 24, "fund_name": 52, "isin": 14,
                  "ter_pct": 7, "fund_size_mln": 12, "ytd_pct": 8}

    header = (
        f"{'Category':<24}  {'Fund Name':<52}  {'ISIN':<14}"
        f"  {'TER%':>7}  {'AUM(mln)':>12}  {'YTD%':>8}"
    )
    sep = "-" * len(header)

    current_cat = None
    print(header)
    print(sep)
    for e in entries:
        if e.category != current_cat:
            current_cat = e.category
            print(f"\n  [{e.category.upper()}]")
        ter = f"{e.ter_pct:.2f}" if e.ter_pct is not None else "N/A"
        aum = f"{e.fund_size_mln:,.0f}" if e.fund_size_mln is not None else "N/A"
        ytd = f"{e.ytd_pct:+.2f}" if e.ytd_pct is not None else "N/A"
        name = (e.fund_name[:50] + "..") if len(e.fund_name) > 52 else e.fund_name
        print(
            f"  {e.category:<22}  {name:<52}  {e.isin:<14}"
            f"  {ter:>7}  {aum:>12}  {ytd:>8}"
        )
    print()
    print(f"Total: {len(entries)} ETFs across {len({e.category for e in entries})} categories")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape the JustETF list overview and export ETF data",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--lang", default="en", help="Site language prefix (en/de/it/fr/...)")
    p.add_argument("--out", default="etfs.csv", help="CSV output file path")
    p.add_argument("--json", dest="json_out", default=None, help="Also save a JSON file at this path")
    p.add_argument("--no-print", action="store_true", help="Suppress terminal table output")
    p.add_argument("--debug", action="store_true", help="Dump raw HTML to debug_list.html and enable DEBUG logging")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s  %(message)s")

    if args.debug:
        url = f"{BASE_URL}/{args.lang}/etf-list-overview.html"
        raw = cf_requests.get(url, impersonate="chrome", timeout=30)
        Path("debug_list.html").write_text(raw.text, encoding="utf-8")
        LOG.debug("Raw HTML saved to debug_list.html")

    entries = fetch_etf_list(lang=args.lang)
    if not entries:
        LOG.error("No ETF entries found — aborting.")
        sys.exit(1)

    if not args.no_print:
        print_table(entries)

    save_csv(entries, Path(args.out))

    if args.json_out:
        save_json(entries, Path(args.json_out))


if __name__ == "__main__":
    main()
