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

## Fixing Broken Positional Table Parsing
Tables are parsed by positional index (e.g. `tables[6]` = holdings). When justetf.com adds/removes tables the indices shift.
1. Run with `--debug` to dump raw HTML → `debug_response.html`.
2. Open the file, search for the section heading (e.g. "Top 10 Holdings").
3. Count how many `<table>` tags precede it — that is the new index.
4. Update the index in the corresponding `_parse_*()` call.
5. Log a comment with the validated ISIN so the next person knows the baseline.

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
