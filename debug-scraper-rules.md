# Debug Scraper Rules

## Context
Something is broken: wrong data, empty fields, HTTP errors, or the page structure changed.
This overrides scraping-rules.md.

## Step 1 — Capture the Raw HTML
```bash
python scrape_justetf.py --isin <ISIN> --debug    # saves debug_response.html
python scrape_etf_list.py --debug                 # saves debug_list.html
```
Open the HTML file in a browser or text editor before touching any code.

## Step 2 — Identify the Failure Mode

| Symptom | Likely cause |
|---|---|
| `RuntimeError: HTTP 403` | `curl_cffi` impersonation failed or Cloudflare updated |
| All fields `None` or empty | Table indices shifted — new table added/removed on page |
| One field consistently `None` | Key name changed (language variant), check `_first_val()` keys |
| `RuntimeError: HTTP 429` | Rate limited — wait and use `--delay` / fewer workers |
| JS arrays not found in list page | justetf changed variable naming (`idXXEtfs`) or moved to API |

## Step 3 — Fix Table Index Drift
1. In `debug_response.html`, search for the section name (e.g. "Holdings").
2. Count `<table>` tags from the top of the document — this is the new index (0-based).
3. Update the index in the relevant `_parse_*()` call in `scrape_justetf.py`.

## Step 4 — Fix Missing Keys (language variants)
Keys like "Fund size" appear in Italian, German, or English depending on `--lang`.
The `_first_val()` helper does fuzzy substring matching. To add a new language variant:
```python
profile.fund_size_eur_mln = _parse_eur_mln(
    _first_val(kv, ["Dimensione del fondo", "Fondsvermögen", "Fund size", "NEW KEY HERE"])
)
```

## Step 5 — Verify the Fix
```bash
python scrape_justetf.py --isin IE00B5BMR087        # known-good baseline
python scrape_justetf.py --isin <broken ISIN>       # the failing case
```
Both should produce non-empty JSON with the expected fields populated.

## Don't
- Don't change the global rate-limit constants in `batch_scrape.py` as a workaround for 403s.
- Don't add fallback values (e.g. `0.0`) to fields that return `None` — `None` is intentional.

## When Done
→ Document the ISIN used to validate the fix in a code comment on the changed index.
→ STOP. Do not refactor surrounding code.
