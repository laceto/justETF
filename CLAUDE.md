# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is this project?

A three-script pipeline for scraping ETF data from justetf.com:

```
scrape_etf_list.py  →  etfs.csv  →  batch_scrape.py  →  data/profiles.json
scrape_justetf.py   (single-ISIN profile fetcher, imported by batch_scrape.py)
```

Dependencies: `curl_cffi`, `beautifulsoup4` — activate `venv\Scripts\activate` first.

---

## What is your task?

**Working on `scrape_justetf.py` or `scrape_etf_list.py`** — adding fields, fixing parsing, changing output format
→ READ: `scraping-rules.md`

**Working on `batch_scrape.py`** — concurrency, rate limiting, output schema, resume logic
→ READ: `batch-rules.md`

**Something is broken** — wrong data, empty fields, HTTP errors, page structure changed
→ READ: `debug-scraper-rules.md`

---

## Load Your Rules Now

1. Identify your task above.
2. Load the one corresponding file.
3. Do not load other rule files unless explicitly told to.
