"""
JustETF Batch Profile Scraper
===============================
Reads ISINs from etfs.csv (produced by scrape_etf_list.py) and fetches the
full ETF profile for each one via fetch_etf_profile() in scrape_justetf.py.

Output files (all in --out-dir, default: data/)
------------------------------------------------
  profiles.jsonl   streaming output — one JSON record per line, written
                   incrementally so the run is resumable after interruption
  profiles.json    final merged JSON array (written when the run completes)
  errors.jsonl     ISINs that failed with error messages
  summary.csv      flat CSV with one row per ETF (no nested fields)

Resuming
--------
  If profiles.jsonl already exists, --resume skips any ISIN already in it.

Concurrency & rate limiting
---------------------------
  --workers N     parallel HTTP workers (default: 5)
  --delay  F      minimum seconds between consecutive requests per worker
                  (default: 1.0, jitter ±50% is added automatically)

Usage
-----
    python batch_scrape.py
    python batch_scrape.py --resume
    python batch_scrape.py --csv etfs.csv --out-dir data/ --workers 5
    python batch_scrape.py --lang en --delay 2 --workers 3
    python batch_scrape.py --limit 50            # first 50 ISINs only (testing)
    python batch_scrape.py --csv etfs.csv --out-dir data/ --workers 1 --delay 3 --lang en --fail-fast
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

# Re-use the profile fetcher from the sibling module
sys.path.insert(0, str(Path(__file__).parent))
from scrape_justetf import fetch_etf_profile

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _load_done_isins(jsonl_path: Path) -> set[str]:
    """Return the set of ISINs already written to the JSONL file."""
    done: set[str] = set()
    if not jsonl_path.exists():
        return done
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    if "isin" in rec:
                        done.add(rec["isin"])
                except json.JSONDecodeError:
                    pass
    return done


def _append_jsonl(path: Path, record: dict, lock: threading.Lock) -> None:
    with lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Record builder — merges list metadata with profile data
# ---------------------------------------------------------------------------

def _build_record(list_row: dict, profile) -> dict:
    """
    Merge the row from etfs.csv (list-level metadata) with the ETFProfile
    dataclass (deep profile data) into one flat+nested dict.
    """
    p = asdict(profile)

    return {
        # ---- from etfs.csv ------------------------------------------------
        "category":         list_row.get("category"),
        "savings_plan":     list_row.get("savings_plan") == "True",
        "savings_plan_url": list_row.get("savings_plan_url") or None,
        # ---- from profile page --------------------------------------------
        "isin":             p["isin"],
        "name":             p["name"],
        "wkn":              list_row.get("wkn") or None,
        "index":            p.get("index"),
        "investment_focus": p.get("investment_focus"),
        "fund_size_eur_mln": p.get("fund_size_eur_mln"),
        "ter_pct":          p.get("ter_pct"),
        "replication":      p.get("replication"),
        "distribution":     p.get("distribution"),
        "inception_date":   list_row.get("inception_date") or None,
        "ytd_pct":          _safe_float(list_row.get("ytd_pct")),
        "profile_url":      list_row.get("profile_url"),
        # ---- nested sections ----------------------------------------------
        "top_holdings":     p.get("top_holdings", []),
        "country_exposure": p.get("country_exposure", []),
        "sector_exposure":  p.get("sector_exposure", []),
        "performance":      p.get("performance", []),
        "risk_metrics":     p.get("risk_metrics", []),
        "exchange_listings": p.get("exchange_listings", []),
        "similar_etfs":     p.get("similar_etfs", []),
    }


def _safe_float(val) -> float | None:
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Summary CSV (flat — no nested fields)
# ---------------------------------------------------------------------------

SUMMARY_FIELDS = [
    "isin", "name", "wkn", "category", "index", "investment_focus",
    "fund_size_eur_mln", "ter_pct", "ytd_pct", "inception_date",
    "replication", "distribution", "savings_plan", "profile_url",
    "n_holdings", "n_countries", "n_sectors", "n_exchanges",
]


def _to_summary_row(rec: dict) -> dict:
    return {
        "isin":             rec["isin"],
        "name":             rec["name"],
        "wkn":              rec.get("wkn"),
        "category":         rec.get("category"),
        "index":            rec.get("index"),
        "investment_focus": rec.get("investment_focus"),
        "fund_size_eur_mln": rec.get("fund_size_eur_mln"),
        "ter_pct":          rec.get("ter_pct"),
        "ytd_pct":          rec.get("ytd_pct"),
        "inception_date":   rec.get("inception_date"),
        "replication":      rec.get("replication"),
        "distribution":     rec.get("distribution"),
        "savings_plan":     rec.get("savings_plan"),
        "profile_url":      rec.get("profile_url"),
        "n_holdings":       len(rec.get("top_holdings") or []),
        "n_countries":      len(rec.get("country_exposure") or []),
        "n_sectors":        len(rec.get("sector_exposure") or []),
        "n_exchanges":      len(rec.get("exchange_listings") or []),
    }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

MAX_RETRIES    = 5      # attempts per ISIN before giving up
RETRY_BASE_429 = 300.0  # seconds for first 429 back-off; doubles each retry
                        # → 5 min, 10 min, 20 min, 40 min, 80 min

# Global pause event: when any worker detects 429, it sets this event and
# sleeps; all other workers check it before each request and wait too.
_rate_limited   = threading.Event()
_rl_lock        = threading.Lock()
_rl_resume_at   = 0.0          # epoch time when the pause ends

# Global abort event: set on first 403/429 when --fail-fast is active.
_abort = threading.Event()


def _global_wait_if_rate_limited() -> None:
    """Block the calling thread until the global rate-limit pause expires."""
    while _rate_limited.is_set():
        remaining = _rl_resume_at - time.monotonic()
        if remaining <= 0:
            _rate_limited.clear()
            break
        time.sleep(min(remaining, 5))   # wake up every 5s to recheck


def _set_global_rate_limit(backoff: float) -> None:
    with _rl_lock:
        resume = time.monotonic() + backoff
        global _rl_resume_at
        if resume > _rl_resume_at:      # only extend, never shorten
            _rl_resume_at = resume
        _rate_limited.set()
    LOG.warning("Rate-limit pause: %.0f s (all workers waiting)", backoff)


def _fetch_one(
    list_row: dict,
    lang: str,
    delay: float,
    profiles_path: Path,
    errors_path: Path,
    write_lock: threading.Lock,
    counters: dict,
    total: int,
    fail_fast: bool = False,
) -> bool:
    isin = list_row["isin"]

    if _abort.is_set():
        return False

    # Honour any active global pause first
    _global_wait_if_rate_limited()

    # Per-request jitter delay
    time.sleep(delay * (0.5 + random.random()))

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            profile = fetch_etf_profile(isin=isin, lang=lang)
            record = _build_record(list_row, profile)
            _append_jsonl(profiles_path, record, write_lock)

            with write_lock:
                counters["done"] += 1
                done, fail = counters["done"], counters["fail"]

            LOG.info("[%d/%d] OK   %s  %s", done + fail, total, isin,
                     list_row.get("fund_name", "")[:50])
            return True

        except Exception as exc:
            last_exc = exc
            is_429 = "429" in str(exc)
            is_403 = "403" in str(exc)

            if fail_fast and (is_403 or is_429):
                LOG.error("HTTP %s on %s — aborting all workers (--fail-fast)",
                          "429" if is_429 else "403", isin)
                _abort.set()
                break

            if is_429 and attempt < MAX_RETRIES:
                backoff = RETRY_BASE_429 * (2 ** (attempt - 1)) + random.uniform(0, 30)
                _set_global_rate_limit(backoff)
                _global_wait_if_rate_limited()
            else:
                break   # non-429 error, or final attempt exhausted

    # All attempts exhausted — log failure
    error_rec = {
        "isin": isin,
        "fund_name": list_row.get("fund_name"),
        "error": str(last_exc),
    }
    _append_jsonl(errors_path, error_rec, write_lock)

    with write_lock:
        counters["fail"] += 1
        done, fail = counters["done"], counters["fail"]

    LOG.warning("[%d/%d] FAIL %s  %s", done + fail, total, isin, last_exc)
    return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(
    csv_path: Path,
    out_dir: Path,
    lang: str,
    workers: int,
    delay: float,
    resume: bool,
    limit: int | None,
    fail_fast: bool = False,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = out_dir / "profiles.jsonl"
    errors_path   = out_dir / "errors.jsonl"
    final_json    = out_dir / "profiles.json"
    summary_csv   = out_dir / "summary.csv"

    # Load work list
    rows = _load_csv(csv_path)
    LOG.info("Loaded %d rows from %s", len(rows), csv_path)

    # Deduplicate by ISIN (the same ETF may appear under multiple categories)
    seen: set[str] = set()
    unique_rows: list[dict] = []
    for row in rows:
        isin = row.get("isin", "").strip()
        if isin and isin not in seen:
            seen.add(isin)
            unique_rows.append(row)
    LOG.info("%d unique ISINs after deduplication", len(unique_rows))

    # Resume: skip already-successfully-fetched ISINs, retry everything else
    if resume:
        done_isins = _load_done_isins(profiles_path)
        before = len(unique_rows)
        unique_rows = [r for r in unique_rows if r["isin"] not in done_isins]
        LOG.info("Resume: skipping %d already-done ISINs, %d remaining",
                 before - len(unique_rows), len(unique_rows))
        # Clear errors file so we only track failures from this run onward
        errors_path.write_text("", encoding="utf-8")
    else:
        # Fresh run: truncate output files
        profiles_path.write_text("", encoding="utf-8")
        errors_path.write_text("", encoding="utf-8")

    if limit:
        unique_rows = unique_rows[:limit]
        LOG.info("Limiting to first %d ISINs", limit)

    total = len(unique_rows)
    if total == 0:
        LOG.info("Nothing to fetch.")
        return

    LOG.info("Fetching %d profiles with %d workers, %.1fs delay ...", total, workers, delay)

    write_lock = threading.Lock()
    counters   = {"done": 0, "fail": 0}
    _abort.clear()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _fetch_one,
                    row, lang, delay,
                    profiles_path, errors_path,
                    write_lock, counters, total,
                    fail_fast,
                ): row["isin"]
                for row in unique_rows
            }
            for future in as_completed(futures):
                future.result()   # re-raise unexpected exceptions
    finally:
        if _abort.is_set():
            LOG.warning("Run aborted early due to --fail-fast. Use --resume to continue later.")

        done = counters["done"]
        fail = counters["fail"]
        LOG.info("Finished: %d OK, %d failed out of %d", done, fail, total)

        # Build final JSON from JSONL — always written, even on early exit
        LOG.info("Writing %s ...", final_json)
        all_records: list[dict] = []
        with profiles_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    all_records.append(json.loads(line))
        final_json.write_text(
            json.dumps(all_records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        LOG.info("profiles.json: %d records", len(all_records))

        # Write flat summary CSV — always written, even on early exit
        LOG.info("Writing %s ...", summary_csv)
        with summary_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
            writer.writeheader()
            for rec in all_records:
                writer.writerow(_to_summary_row(rec))
        LOG.info("summary.csv: %d rows", len(all_records))

        if fail:
            LOG.warning("%d failures logged in %s", fail, errors_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch-scrape ETF profiles from JustETF",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--csv",     default="etfs.csv",  help="Input CSV from scrape_etf_list.py")
    p.add_argument("--out-dir", default="data/",     help="Output directory")
    p.add_argument("--lang",    default="en",        help="Site language (en/de/it/fr/...)")
    p.add_argument("--workers", type=int, default=2, help="Parallel HTTP workers")
    p.add_argument("--delay",   type=float, default=3.0,
                   help="Min seconds between requests per worker (±50%% jitter added)")
    p.add_argument("--resume",  action="store_true",
                   help="Skip ISINs already in profiles.jsonl")
    p.add_argument("--limit",   type=int, default=None,
                   help="Only fetch the first N ISINs (useful for testing)")
    p.add_argument("--fail-fast", action="store_true",
                   help="Stop all workers immediately on first 403 or 429")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    run(
        csv_path=Path(args.csv),
        out_dir=Path(args.out_dir),
        lang=args.lang,
        workers=args.workers,
        delay=args.delay,
        resume=args.resume,
        limit=args.limit,
        fail_fast=args.fail_fast,
    )


if __name__ == "__main__":
    main()
