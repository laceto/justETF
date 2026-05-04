# Scraping Rules

## Context
You are extending or fixing a scraper in this justETF pipeline.
Target scripts: `scrape_justetf.py` (profile pages) or `scrape_etf_list.py` (list overview).

## Core Invariants
- Always use `curl_cffi` with `impersonate="chrome"` — plain `requests` gets 403.
- Missing / unparseable values → store as `None`, never skip the field.
- All floats strip `%` and convert European commas to dots before parsing.

## Adding a New Field to ETFProfile
1. Add the typed attribute to the `ETFProfile` dataclass in `scrape_justetf.py`.
2. Write a `_parse_*` helper if the raw format needs cleaning.
3. Call it inside the appropriate `_parse_*()` section function.
4. Update `_print_profile()` only if the field is useful for human output.

## Fixing Broken Table Parsing
Tables are now found **semantically**: `_build_heading_table_map()` walks `<h2>`/`<h3>` tags and maps each to the first `<table>` that follows it. Positional indices (e.g. `tables[6]`) are only a fallback when heading detection fails — a `WARNING` log is emitted when the fallback is used.

For exposure sections (`top_holdings`, `country_exposure`, `sector_exposure`), the positional-fallback result is also validated by `_is_valid_exposure_table()`. A table is rejected (section returned empty) if **any** of these is true:
- The first row's weight is `None` — signals a column-header row from a different table type (e.g. a "Similar ETFs" table whose first cell is "Nome del Fondo"). Real exposure tables have no header rows; data starts immediately.
- More than half the weights are unparseable (e.g. a listings table whose second column holds currency codes like "EUR").
- Any weight is below −1 (e.g. a performance table with multi-year returns like −87.82%).

This prevents commodity/leveraged ETPs (which have no holdings section) from picking up the wrong tables by accident.

**If a section returns empty and you see a WARNING about positional fallback:**
1. Run with `--debug` to dump raw HTML → `debug_response.html`.
2. Search for the section heading (e.g. "Top Holdings") and check its exact text.
3. Add the new text variant to `_HEADING_PATTERNS` in `scrape_justetf.py` for the relevant section.
4. If the heading is genuinely absent on this ETF type (e.g. commodity ETP), empty is correct — no code change needed.
5. If the heading is present but the validator is still rejecting it, check whether the table's weights are unexpectedly negative or unparseable and fix `_is_valid_exposure_table`.

## Adding a New Field to ETFEntry (list scraper)
1. Add the attribute to the `ETFEntry` dataclass in `scrape_etf_list.py`.
2. Map the JS record key in `_parse_list_page()` (look at the raw JS object keys in `debug_list.html`).
3. Add the field to `CSV_FIELDS` if it should appear in `etfs.csv`.

## Key Parser Functions
| Function | Input | Output |
|---|---|---|
| `_parse_pct` | `"7,60%"` | `7.60` |
| `_parse_eur_mln` | `"EUR 112.876 mln"` | `112876.0` |
| `_parse_ter` | `"0,07% p.a."` | `0.07` |
| `_parse_js_array` | JS source text + var name | `list[dict]` |

## When Done
→ Run `python scrape_justetf.py --isin IE00B5BMR087` and verify output looks correct.
→ STOP. Do not modify `batch_scrape.py` unless the task explicitly requires it.
