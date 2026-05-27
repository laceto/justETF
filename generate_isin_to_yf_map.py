"""
generate_isin_to_yf_map.py
===========================
Build a ISIN → Yahoo Finance ticker mapping for all ETFs in
data/profiles.jsonl using the OpenFIGI batch API.

The exchange_listings field in profiles.jsonl is unreliable (scraper
positional-fallback bug), so we resolve tickers via OpenFIGI instead.

OpenFIGI API (free, no key needed)
-----------------------------------
Endpoint : https://api.openfigi.com/v3/mapping
Limit    : 100 ISINs per request, 25 requests/minute (no key)
Auth     : optional — set OPENFIGI_API_KEY env var for higher rate limits

Exchange preference (OpenFIGI exchCode → Yahoo Finance suffix)
--------------------------------------------------------------
GX  (Xetra)              → .DE   EUR   ← preferred
NA  (Euronext Amsterdam) → .AS   EUR
FP  (Euronext Paris)     → .PA   EUR
IM  (Borsa Italiana)     → .MI   EUR
BB  (Euronext Brussels)  → .BR   EUR
SW  (SIX Swiss)          → .SW   CHF
LN  (London)             → .L    GBP
US  / UN / UW (US)       → ""    USD

Output
------
data/ticker/isin_to_yf.json   — full map { isin: {...} }
data/ticker/isin_to_yf.csv    — flat CSV for review / use in myfinance2

Usage
-----
    python generate_isin_to_yf_map.py
    python generate_isin_to_yf_map.py --limit 50   # test on first 50 ISINs
    python generate_isin_to_yf_map.py --no-report
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

LOG = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
BATCH_SIZE   = 100   # max items per request (no-key limit)
RATE_LIMIT   = 25    # requests per minute (no-key limit)
SLEEP_SEC    = 60 / RATE_LIMIT  # ~2.4 s between requests

# ---------------------------------------------------------------------------
# OpenFIGI exchCode → (YF suffix, currency hint, preference rank)
# Lower rank = more preferred
# ---------------------------------------------------------------------------

_EXCH_MAP: dict[str, tuple[str, str, int]] = {
    "GX": (".DE", "EUR", 0),   # Xetra
    "NA": (".AS", "EUR", 1),   # Euronext Amsterdam
    "FP": (".PA", "EUR", 2),   # Euronext Paris
    "IM": (".MI", "EUR", 3),   # Borsa Italiana
    "BB": (".BR", "EUR", 4),   # Euronext Brussels
    "SW": (".SW", "CHF", 5),   # SIX Swiss Exchange
    "LN": (".L",  "GBP", 6),   # London Stock Exchange
    "UN": ("",    "USD", 7),   # NYSE
    "UW": ("",    "USD", 8),   # NASDAQ
    "US": ("",    "USD", 9),   # generic US
}


def _best_figi(figi_list: list[dict]) -> dict | None:
    """Pick the highest-preference exchange result from a FIGI result list."""
    candidates = []
    for item in figi_list:
        exch = item.get("exchCode", "")
        if exch in _EXCH_MAP:
            _, _, rank = _EXCH_MAP[exch]
            candidates.append((rank, item))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _to_yf_ticker(figi_item: dict) -> str:
    """Construct a Yahoo Finance ticker from a FIGI result dict."""
    ticker = (figi_item.get("ticker") or "").strip().split()[0].upper()
    exch   = figi_item.get("exchCode", "")
    suffix, _, _ = _EXCH_MAP.get(exch, ("", "", 99))
    return f"{ticker}{suffix}"


# ---------------------------------------------------------------------------
# OpenFIGI batch caller
# ---------------------------------------------------------------------------

def _openfigi_batch(isins: list[str], api_key: str | None) -> list[list[dict] | None]:
    """
    Send one batch of up to BATCH_SIZE ISINs to OpenFIGI.

    Returns a list of the same length as `isins`. Each element is either
    a list of FIGI dicts or None on error.
    """
    payload = json.dumps(
        [{"idType": "ID_ISIN", "idValue": isin} for isin in isins]
    ).encode()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    req = urllib.request.Request(OPENFIGI_URL, data=payload, headers=headers, method="POST")

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                results = json.loads(resp.read().decode())
                out = []
                for item in results:
                    if "data" in item:
                        out.append(item["data"])
                    else:
                        LOG.debug("OpenFIGI error for item: %s", item.get("error"))
                        out.append(None)
                return out
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = 60 * (attempt + 1)
                LOG.warning("Rate limited — waiting %ds before retry %d", wait, attempt + 1)
                time.sleep(wait)
            else:
                LOG.warning("HTTP %d on attempt %d: %s", exc.code, attempt + 1, exc)
                time.sleep(5)
        except Exception as exc:
            LOG.warning("Request error on attempt %d: %s", attempt + 1, exc)
            time.sleep(5)

    return [None] * len(isins)


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_isin_map(
    profiles_path: Path,
    api_key: str | None = None,
    limit: int | None = None,
) -> dict[str, dict]:
    """
    Load ISINs from profiles.jsonl, resolve via OpenFIGI, return mapping dict.
    """
    profiles: list[dict] = []
    with profiles_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    profiles.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if limit:
        profiles = profiles[:limit]

    LOG.info("Loaded %d profiles from %s", len(profiles), profiles_path)

    profile_by_isin: dict[str, dict] = {}
    for p in profiles:
        isin = (p.get("isin") or "").strip()
        if isin and isin not in profile_by_isin:
            profile_by_isin[isin] = p

    isins = list(profile_by_isin.keys())
    LOG.info("Unique ISINs to resolve: %d", len(isins))

    mapping: dict[str, dict] = {}
    no_result = skipped = 0
    total_batches = (len(isins) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(0, len(isins), BATCH_SIZE):
        batch = isins[batch_idx: batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1
        LOG.info("Batch %d / %d  (%d ISINs) ...", batch_num, total_batches, len(batch))

        results = _openfigi_batch(batch, api_key)

        for isin, figi_list in zip(batch, results):
            profile = profile_by_isin[isin]
            if not figi_list:
                no_result += 1
                LOG.debug("ISIN %s: no FIGI results", isin)
                continue

            best = _best_figi(figi_list)
            if best is None:
                skipped += 1
                LOG.debug(
                    "ISIN %s: no preferred exchange among %s",
                    isin, [f.get("exchCode") for f in figi_list],
                )
                continue

            yf_ticker = _to_yf_ticker(best)
            suffix, currency, _ = _EXCH_MAP[best["exchCode"]]

            all_options = []
            for fi in figi_list:
                if fi.get("exchCode") in _EXCH_MAP:
                    sfx, cur, _ = _EXCH_MAP[fi["exchCode"]]
                    tk = (fi.get("ticker") or "").strip().split()[0].upper()
                    all_options.append({
                        "exch_code":  fi["exchCode"],
                        "currency":   cur,
                        "yf_ticker":  f"{tk}{sfx}",
                        "figi_name":  fi.get("name", ""),
                    })

            mapping[isin] = {
                "isin":              isin,
                "name":              profile.get("name", ""),
                "yf_ticker":         yf_ticker,
                "exch_code":         best["exchCode"],
                "currency":          currency,
                "yf_suffix":         suffix,
                "figi_name":         best.get("name", ""),
                "ter_pct":           profile.get("ter_pct"),
                "fund_size_eur_mln": profile.get("fund_size_eur_mln"),
                "index":             profile.get("index"),
                "distribution":      profile.get("distribution"),
                "replication":       profile.get("replication"),
                "investment_focus":  profile.get("investment_focus"),
                "all_options":       all_options,
            }

        if batch_idx + BATCH_SIZE < len(isins):
            time.sleep(SLEEP_SEC)

    LOG.info(
        "Done — mapped: %d  |  no FIGI result: %d  |  no preferred exchange: %d",
        len(mapping), no_result, skipped,
    )
    return mapping


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "isin", "name", "yf_ticker", "exch_code", "currency", "yf_suffix",
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
    rows = sorted(mapping.values(), key=lambda x: x.get("name") or "")
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    LOG.info("CSV saved  → %s  (%d rows)", path, len(rows))


def print_coverage_report(mapping: dict[str, dict], total_isins: int) -> None:
    mapped = len(mapping)
    pct = mapped / total_isins * 100 if total_isins else 0

    exch_counts: dict[str, int] = {}
    currency_counts: dict[str, int] = {}
    for entry in mapping.values():
        e = entry["exch_code"]
        c = entry["currency"]
        exch_counts[e]     = exch_counts.get(e, 0) + 1
        currency_counts[c] = currency_counts.get(c, 0) + 1

    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  Coverage: {mapped} / {total_isins} ISINs mapped ({pct:.1f}%)")
    print(sep)

    print("\nExchange distribution (OpenFIGI exchCode → YF suffix):")
    for exch, count in sorted(exch_counts.items(), key=lambda x: -x[1]):
        sfx = _EXCH_MAP.get(exch, ("?", "?", 99))[0] or "(no suffix)"
        bar = "#" * (count * 30 // max(exch_counts.values()))
        print(f"  {exch:>4} {sfx:>5}   {count:>5}  {count/mapped*100:5.1f}%  {bar}")

    print("\nCurrency distribution:")
    for currency, count in sorted(currency_counts.items(), key=lambda x: -x[1]):
        bar = "#" * (count * 30 // max(currency_counts.values()))
        print(f"  {currency:>6}   {count:>5}  {count/mapped*100:5.1f}%  {bar}")
    print()


def _count_profiles(profiles_path: Path) -> int:
    return sum(1 for line in profiles_path.open(encoding="utf-8") if line.strip())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build ISIN → Yahoo Finance ticker mapping via OpenFIGI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input",     default="data/profiles.jsonl")
    p.add_argument("--out-dir",   default="data/ticker")
    p.add_argument("--limit",     type=int, default=None,
                   help="Process only the first N ISINs (for testing)")
    p.add_argument("--no-report", action="store_true")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    profiles_path = Path(args.input)
    if not profiles_path.exists():
        LOG.error("Profiles file not found: %s", profiles_path)
        raise SystemExit(1)

    api_key = os.environ.get("OPENFIGI_API_KEY")
    if api_key:
        LOG.info("Using OpenFIGI API key (higher rate limits)")
    else:
        LOG.info("No OPENFIGI_API_KEY set — using free tier (25 req/min, 100/batch)")

    out_dir = Path(args.out_dir)
    mapping = build_isin_map(profiles_path, api_key=api_key, limit=args.limit)

    save_json(mapping, out_dir / "isin_to_yf.json")
    save_csv(mapping,  out_dir / "isin_to_yf.csv")

    if not args.no_report:
        total = _count_profiles(profiles_path)
        if args.limit:
            total = min(total, args.limit)
        print_coverage_report(mapping, total)


if __name__ == "__main__":
    main()
