"""
generate_isin_to_yf_map.py
===========================
Build a ISIN → Yahoo Finance ticker mapping from the already-scraped
justETF profiles (data/profiles.jsonl).

Each ETF profile contains an ``exchange_listings`` list with fields:
    exchange, currency, ticker, bloomberg, reuters

We map the exchange name to a Yahoo Finance suffix (e.g. Xetra → .DE)
and apply a preference order so we consistently pick one ticker per ETF.

Preference order
----------------
1. EUR  Xetra (.DE)                — largest single EUR market for ETFs
2. EUR  Euronext Amsterdam (.AS)
3. EUR  others (.MI .PA .BR .LS .VI .MC .HE .ST .CO .OL .F)
4. GBP  London Stock Exchange (.L)
5. CHF  SIX Swiss Exchange (.SW)
6. Any  remaining known exchange
7. Skip — no known exchange found

Output
------
data/ticker/isin_to_yf.json   — full mapping, one entry per ISIN
data/ticker/isin_to_yf.csv    — flat CSV for review / direct use in myfinance2

Usage
-----
    python generate_isin_to_yf_map.py
    python generate_isin_to_yf_map.py --input data/profiles.jsonl --out-dir data/ticker
    python generate_isin_to_yf_map.py --no-report
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exchange name → Yahoo Finance suffix
# ---------------------------------------------------------------------------
# Each entry: (lowercase_substring, yf_suffix)
# Matched in order — more-specific patterns must come first.

_EXCHANGE_PATTERNS: list[tuple[str, str]] = [
    # EUR — Xetra / Frankfurt
    ("xetra",                 ".DE"),
    ("deutsche börse",        ".DE"),
    ("frankfurt",             ".F"),
    # EUR — Euronext
    ("euronext amsterdam",    ".AS"),
    ("amsterdam",             ".AS"),
    ("euronext paris",        ".PA"),
    ("paris",                 ".PA"),
    ("euronext brussels",     ".BR"),
    ("brussels",              ".BR"),
    ("euronext lisbon",       ".LS"),
    ("lisbon",                ".LS"),
    # EUR — other EU
    ("borsa italiana",        ".MI"),
    ("milan",                 ".MI"),
    ("vienna",                ".VI"),
    ("madrid",                ".MC"),
    ("bolsa de madrid",       ".MC"),
    ("warsaw",                ".WA"),
    ("helsinki",              ".HE"),
    ("stockholm",             ".ST"),
    ("copenhagen",            ".CO"),
    ("oslo",                  ".OL"),
    # GBP
    ("london stock exchange", ".L"),
    ("london",                ".L"),
    ("lse",                   ".L"),
    # CHF
    ("six swiss",             ".SW"),
    ("swiss exchange",        ".SW"),
    # USD
    ("new york",              ""),
    ("nyse",                  ""),
    ("nasdaq",                ""),
]

# Preference order: first suffix in this list wins when an ETF is listed on
# multiple exchanges.  EUR markets first, then GBP, then CHF, then USD.
_SUFFIX_PREFERENCE: list[str] = [
    ".DE", ".AS",
    ".MI", ".PA", ".BR", ".LS", ".VI", ".MC", ".HE", ".ST", ".CO", ".OL", ".F",
    ".L",
    ".SW",
    "",   # USD — lowest preference
]


def _exchange_to_suffix(exchange_name: str) -> str | None:
    """Return the YF suffix for an exchange name, or None if unknown."""
    lower = exchange_name.lower()
    for pattern, suffix in _EXCHANGE_PATTERNS:
        if pattern in lower:
            return suffix
    return None


def _build_yf_ticker(raw_ticker: str, suffix: str) -> str:
    """
    Combine exchange ticker symbol and YF suffix.

    justETF tickers are clean short codes (e.g. "IWDA", "CSPX"), but
    Bloomberg-format fields sometimes bleed in (e.g. "IWDA GY") — take
    only the first word to be safe.
    """
    clean = raw_ticker.strip().split()[0].upper()
    return f"{clean}{suffix}"


def _pick_best_listing(listings: list[dict]) -> dict | None:
    """
    From all exchange listings for one ETF, return the one whose exchange
    maps to the highest-preference YF suffix.

    Returns None if no listing maps to a known exchange.
    """
    resolved: list[tuple[int, dict, str]] = []  # (rank, listing, suffix)

    for lst in listings:
        suffix = _exchange_to_suffix(lst.get("exchange", ""))
        if suffix is None:
            continue
        rank = _SUFFIX_PREFERENCE.index(suffix) if suffix in _SUFFIX_PREFERENCE else 999
        resolved.append((rank, lst, suffix))

    if not resolved:
        return None

    resolved.sort(key=lambda x: x[0])
    _, best_listing, best_suffix = resolved[0]
    return {**best_listing, "_yf_suffix": best_suffix}


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_isin_map(profiles_path: Path) -> dict[str, dict]:
    """
    Stream-parse profiles.jsonl and return:
        { isin: { isin, name, yf_ticker, exchange, currency, yf_suffix,
                  ter_pct, fund_size_eur_mln, index, distribution,
                  replication, investment_focus, all_listings } }
    """
    mapping: dict[str, dict] = {}
    total = skipped_no_listings = skipped_no_suffix = 0

    with profiles_path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            total += 1

            try:
                profile = json.loads(raw)
            except json.JSONDecodeError as exc:
                LOG.warning("Line %d: JSON parse error — %s", line_no, exc)
                continue

            isin = (profile.get("isin") or "").strip()
            if not isin:
                continue

            listings = profile.get("exchange_listings") or []
            if not listings:
                skipped_no_listings += 1
                LOG.debug("ISIN %s: no exchange_listings", isin)
                continue

            best = _pick_best_listing(listings)
            if best is None:
                skipped_no_suffix += 1
                LOG.debug(
                    "ISIN %s: could not resolve a known exchange from %s",
                    isin, [l.get("exchange") for l in listings],
                )
                continue

            yf_ticker = _build_yf_ticker(best["ticker"], best["_yf_suffix"])

            # Build a summary of all resolvable listings for reference
            all_listings_resolved = []
            for lst in listings:
                sfx = _exchange_to_suffix(lst.get("exchange", ""))
                all_listings_resolved.append({
                    "exchange":  lst.get("exchange", ""),
                    "currency":  lst.get("currency", ""),
                    "ticker":    lst.get("ticker", ""),
                    "yf_ticker": _build_yf_ticker(lst["ticker"], sfx) if sfx is not None else None,
                    "yf_suffix": sfx,
                })

            mapping[isin] = {
                "isin":              isin,
                "name":              profile.get("name", ""),
                "yf_ticker":         yf_ticker,
                "exchange":          best.get("exchange", ""),
                "currency":          best.get("currency", ""),
                "yf_suffix":         best["_yf_suffix"],
                "ter_pct":           profile.get("ter_pct"),
                "fund_size_eur_mln": profile.get("fund_size_eur_mln"),
                "index":             profile.get("index"),
                "distribution":      profile.get("distribution"),
                "replication":       profile.get("replication"),
                "investment_focus":  profile.get("investment_focus"),
                "all_listings":      all_listings_resolved,
            }

    LOG.info(
        "Mapped %d / %d ISINs  |  skipped: %d no listings, %d no known exchange",
        len(mapping), total, skipped_no_listings, skipped_no_suffix,
    )
    return mapping


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "isin", "name", "yf_ticker", "exchange", "currency", "yf_suffix",
    "ter_pct", "fund_size_eur_mln", "index", "distribution", "replication",
]


def save_json(mapping: dict[str, dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    LOG.info("JSON saved → %s  (%d entries)", path, len(mapping))


def save_csv(mapping: dict[str, dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(mapping.values(), key=lambda x: (x.get("name") or ""))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    LOG.info("CSV saved  → %s  (%d rows)", path, len(rows))


def print_coverage_report(mapping: dict[str, dict], total_profiles: int) -> None:
    mapped = len(mapping)
    pct = mapped / total_profiles * 100 if total_profiles else 0

    suffix_counts: dict[str, int] = {}
    currency_counts: dict[str, int] = {}
    for entry in mapping.values():
        s = entry["yf_suffix"]
        c = entry["currency"]
        suffix_counts[s] = suffix_counts.get(s, 0) + 1
        currency_counts[c] = currency_counts.get(c, 0) + 1

    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  Coverage: {mapped} / {total_profiles} ISINs mapped ({pct:.1f}%)")
    print(sep)

    print("\nYF suffix distribution:")
    for suffix, count in sorted(suffix_counts.items(), key=lambda x: -x[1]):
        bar = "#" * (count * 30 // max(suffix_counts.values()))
        print(f"  {suffix or '(none)':>6}   {count:>5}  {count/mapped*100:5.1f}%  {bar}")

    print("\nCurrency distribution:")
    for currency, count in sorted(currency_counts.items(), key=lambda x: -x[1]):
        bar = "#" * (count * 30 // max(currency_counts.values()))
        print(f"  {currency:>6}   {count:>5}  {count/mapped*100:5.1f}%  {bar}")
    print()


def _count_profiles(profiles_path: Path) -> int:
    """Count non-empty lines (cheap pass, no JSON parsing)."""
    return sum(1 for line in profiles_path.open(encoding="utf-8") if line.strip())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build ISIN → Yahoo Finance ticker mapping from justETF profiles",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--input", default="data/profiles.jsonl",
        help="Path to justETF profiles.jsonl",
    )
    p.add_argument(
        "--out-dir", default="data/ticker",
        help="Directory for output files (isin_to_yf.json and isin_to_yf.csv)",
    )
    p.add_argument(
        "--no-report", action="store_true",
        help="Suppress the coverage / suffix-distribution report",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    profiles_path = Path(args.input)
    if not profiles_path.exists():
        LOG.error("Profiles file not found: %s", profiles_path)
        raise SystemExit(1)

    out_dir = Path(args.out_dir)

    LOG.info("Reading %s ...", profiles_path)
    mapping = build_isin_map(profiles_path)

    save_json(mapping, out_dir / "isin_to_yf.json")
    save_csv(mapping,  out_dir / "isin_to_yf.csv")

    if not args.no_report:
        total = _count_profiles(profiles_path)
        print_coverage_report(mapping, total)


if __name__ == "__main__":
    main()
