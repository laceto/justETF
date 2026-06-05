"""
stock_etf_lookup.py — Find ETFs that invest in a given stock.

Modes:
  Local (default): searches top_holdings in data/profiles.jsonl.
  Live  (--live):  scrapes justETF constituent-search for complete results.

Usage:
    python stock_etf_lookup.py "NVIDIA"
    python stock_etf_lookup.py "Apple" --top 20 --min-weight 0.5
    python stock_etf_lookup.py "Taiwan Semiconductor" --live
    python stock_etf_lookup.py "Samsung" --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass

# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class ETFMatch:
    isin: str
    name: str
    matched_holding: str
    holding_weight_pct: float | None
    fund_size_eur_mln: float | None
    ter_pct: float | None
    replication: str | None
    distribution: str | None
    index: str | None


# ─── Local search ─────────────────────────────────────────────────────────────

def search_local(
    stock: str,
    data_path: str = "data/profiles.jsonl",
    min_weight: float = 0.0,
) -> list[ETFMatch]:
    pattern = re.compile(re.escape(stock), re.IGNORECASE)
    matches: list[ETFMatch] = []

    with open(data_path) as fh:
        for line in fh:
            try:
                etf = json.loads(line)
            except json.JSONDecodeError:
                continue

            holdings = etf.get("top_holdings") or []
            if not isinstance(holdings, list):
                continue

            for item in holdings:
                if pattern.search(item.get("name", "")):
                    w = item.get("weight_pct")
                    if w is not None and w < min_weight:
                        break
                    matches.append(ETFMatch(
                        isin=etf.get("isin", ""),
                        name=etf.get("name", ""),
                        matched_holding=item["name"],
                        holding_weight_pct=w,
                        fund_size_eur_mln=etf.get("fund_size_eur_mln"),
                        ter_pct=etf.get("ter_pct"),
                        replication=etf.get("replication"),
                        distribution=etf.get("distribution"),
                        index=etf.get("index"),
                    ))
                    break  # one match per ETF

    return sorted(
        matches,
        key=lambda m: (m.holding_weight_pct or 0),
        reverse=True,
    )


# ─── Live scrape ───────────────────────────────────────────────────────────────

def _parse_js_array(source: str, var_name: str) -> list[dict]:
    """Extract a JS array assigned to var_name from inline script source."""
    pattern = re.compile(
        rf"var\s+{re.escape(var_name)}\s*=\s*(\[.*?\]);",
        re.DOTALL,
    )
    m = pattern.search(source)
    if not m:
        return []
    raw = m.group(1)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)   # trailing commas
    raw = re.sub(r"'([^']*)'", r'"\1"', raw)   # single → double quotes
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _parse_pct(text: str | None) -> float | None:
    if not text:
        return None
    text = str(text).replace(",", ".").replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _parse_mln(text: str | None) -> float | None:
    if not text:
        return None
    text = str(text).replace(",", ".").replace("EUR", "").replace("mln", "").strip()
    try:
        v = float(text)
        return v * 1_000_000 / 1_000_000  # keep in mln
    except ValueError:
        return None


def search_live(
    stock: str,
    lang: str = "en",
    min_weight: float = 0.0,
    timeout: int = 30,
) -> list[ETFMatch]:
    """
    Scrape justETF's constituent-filter page to find ETFs holding `stock`.

    JustETF embeds the filtered ETF list as JS arrays in the page source,
    identical to the format used by scrape_etf_list.py.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("[live] curl_cffi not installed. Run: pip install curl_cffi", file=sys.stderr)
        sys.exit(1)

    query = stock.strip()
    url = (
        f"https://www.justetf.com/{lang}/find-etf.html"
        f"?groupField=none&sortField=ter&sortOrder=asc&constituents[]={query}"
    )

    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[live] HTTP error: {exc}", file=sys.stderr)
        sys.exit(1)

    html = resp.text

    # JustETF embeds ETF list data as JS vars named like "idXEtfs"
    var_pattern = re.compile(r"var\s+(id\w+Etfs)\s*=\s*\[")
    var_names = var_pattern.findall(html)

    if not var_names:
        print("[live] No ETF data arrays found in page. The stock name may not match "
              "justETF's constituent labels, or the page structure changed.", file=sys.stderr)
        return []

    matches: list[ETFMatch] = []
    seen: set[str] = set()

    for var in var_names:
        records = _parse_js_array(html, var)
        for r in records:
            isin = r.get("isin") or r.get("ISIN") or ""
            if isin in seen:
                continue
            seen.add(isin)

            name = r.get("name") or r.get("fundName") or ""
            ter = _parse_pct(r.get("ter"))
            size = _parse_mln(r.get("fundSize") or r.get("aum"))

            matches.append(ETFMatch(
                isin=isin,
                name=name,
                matched_holding=query,
                holding_weight_pct=None,  # not available from list page
                fund_size_eur_mln=size,
                ter_pct=ter,
                replication=r.get("replication"),
                distribution=r.get("distributionPolicy"),
                index=r.get("indexName"),
            ))

    return sorted(
        matches,
        key=lambda m: (m.fund_size_eur_mln or 0),
        reverse=True,
    )


# ─── Output ───────────────────────────────────────────────────────────────────

def _fmt(val: float | None, decimals: int = 2, suffix: str = "") -> str:
    if val is None:
        return "—"
    return f"{val:.{decimals}f}{suffix}"


def print_results(matches: list[ETFMatch], stock: str, live: bool) -> None:
    mode = "live (justETF)" if live else "local (top holdings only)"
    print(f"\n  ETFs investing in '{stock}'  [{mode}]")
    print(f"  {len(matches)} result(s) found")
    print("─" * 110)

    header = (
        f"  {'#':>3}  {'ISIN':<14}  {'Weight':>7}  {'Size(mln€)':>10}  "
        f"{'TER':>6}  {'Dist':>12}  {'Name':<50}"
    )
    print(header)
    print("─" * 110)

    for i, m in enumerate(matches, 1):
        dist = (m.distribution or "")[:12]
        name = (m.name or "")[:50]
        weight = _fmt(m.holding_weight_pct, 2, "%") if m.holding_weight_pct is not None else "—"
        size = _fmt(m.fund_size_eur_mln, 0)
        ter = _fmt(m.ter_pct, 2, "%") if m.ter_pct is not None else "—"
        print(
            f"  {i:>3}  {m.isin:<14}  {weight:>7}  {size:>10}  "
            f"{ter:>6}  {dist:>12}  {name}"
        )

    print("─" * 110)
    if not live:
        print(
            "  Note: local mode searches only top holdings (~10-15 per ETF). "
            "Use --live for complete results.\n"
        )


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Find ETFs that invest in a given stock.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("stock", help="Stock name or partial name, e.g. 'NVIDIA', 'Apple'")
    p.add_argument("--live", action="store_true",
                   help="Scrape justETF constituent search (complete, but slower)")
    p.add_argument("--lang", default="en", choices=["en", "it", "de"],
                   help="Language for live mode (default: en)")
    p.add_argument("--data", default="data/profiles.jsonl",
                   help="Path to profiles.jsonl for local mode (default: data/profiles.jsonl)")
    p.add_argument("--top", type=int, default=None,
                   help="Show only the top N results")
    p.add_argument("--min-weight", type=float, default=0.0,
                   help="Minimum holding weight %% to include (local mode only, default: 0)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Output results as JSON array")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    if args.live:
        matches = search_live(args.stock, lang=args.lang, min_weight=args.min_weight)
    else:
        try:
            matches = search_local(args.stock, data_path=args.data, min_weight=args.min_weight)
        except FileNotFoundError:
            print(f"[error] Data file not found: {args.data}", file=sys.stderr)
            print("  Run batch_scrape.py first, or use --live to fetch from justETF.", file=sys.stderr)
            sys.exit(1)

    if args.top:
        matches = matches[: args.top]

    if args.as_json:
        print(json.dumps([asdict(m) for m in matches], indent=2, ensure_ascii=False))
        return

    if not matches:
        print(f"\n  No ETFs found holding '{args.stock}'.")
        if not args.live:
            print("  Try --live for complete results, or check the spelling.")
        return

    print_results(matches, args.stock, live=args.live)


if __name__ == "__main__":
    main()
