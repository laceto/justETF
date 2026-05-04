# Batch Pipeline Rules

## Context
You are working on `batch_scrape.py` — the script that reads `etfs.csv` and
fetches full profiles for every ISIN using `fetch_etf_profile()` from `scrape_justetf.py`.

## Pipeline Flow
```
etfs.csv → deduplicate ISINs → ThreadPoolExecutor → fetch_etf_profile()
         → profiles.jsonl (streaming) → profiles.json + summary.csv (on completion)
```

## Concurrency & Rate Limiting
- Default: 2 workers, 3 s delay ±50% jitter.
- A **global** `threading.Event` (`_rate_limited`) pauses ALL workers when any one hits a 429.
- Back-off: 300 s doubling each retry (5 attempts max) — do not lower this; justetf blocks aggressively.
- Never increase default workers above 5 without testing first.
- `--fail-fast`: stops all workers immediately on the first 403 or 429. Use with `--resume` to continue later from where it stopped.

## Resumability
- `profiles.jsonl` is written incrementally (append, one record per line).
- `--resume` reads the JSONL to find already-done ISINs and skips them.
- A fresh (non-`--resume`) run truncates both `profiles.jsonl` and `errors.jsonl`.
- Do not break the append-only write pattern; it is the resume mechanism.

## Output Files
| File | Written when | Notes |
|---|---|---|
| `data/profiles.jsonl` | During run | One JSON object per line |
| `data/profiles.json` | Run completes **or** `--fail-fast` abort | Merged array from JSONL |
| `data/errors.jsonl` | During run | ISINs that failed all retries |
| `data/summary.csv` | Run completes **or** `--fail-fast` abort | Flat, no nested fields |

## Modifying Output Schema
- Flat fields → update `_build_record()` and `_to_summary_row()` + `SUMMARY_FIELDS`.
- Nested fields (holdings, exposure, etc.) stay in `profiles.json` only; `summary.csv` stores counts (`n_holdings`, etc.).

## When Done
→ Verify with `python batch_scrape.py --limit 3` (fetches only 3 ISINs).
→ Check `data/profiles.jsonl` has 3 lines and `data/errors.jsonl` is empty.
→ STOP.
