# myfinance2 Asset-Class Integration Map

A repeatable blueprint for wiring any instrument universe into the myfinance2
technical-analysis pipeline.

---

## Architecture Overview

```
┌──────────────────────────────────┐       ┌──────────────────────────────────────┐
│   DATA-SOURCE REPO               │       │   myfinance2                         │
│   (e.g. justETF)                 │       │                                      │
│                                  │       │  data/ticker/<universe>/             │
│  profiles.jsonl / summary.csv    │──────▶│    ticker.xlsx  (col: ticker)        │
│  (ISINs + metadata)              │  Step │    isin_to_yf.json  (optional ref)   │
│                                  │  1+2  │                                      │
│  generate_isin_to_yf_map.py      │       │  config_<universe>.json              │
│   → data/ticker/isin_to_yf.json  │       │    benchmark: <YF ticker>            │
│   → data/ticker/isin_to_yf.csv   │       │    (all other params unchanged)      │
└──────────────────────────────────┘       │                                      │
                                           │  get_historical_ohlc_data.py         │
                                           │   → data/ohlc/historical/<universe>/ │
                                           │       ohlc_data.parquet              │
                                           │                                      │
                                           │  pipeline.py                         │
                                           │   → data/results/<universe>/         │
                                           │       analysis_results.parquet       │
                                           │                                      │
                                           │  app.py  (universe switcher sidebar) │
└─────────────────────────────────────────────────────────────────────────────────┘
```

The pipeline (`pipeline.py`, `algoshort` wheel, signal logic) is **100% asset-class
agnostic** — only three things change per universe:

| Thing that changes       | Where it lives                        |
|--------------------------|---------------------------------------|
| Ticker list              | `data/ticker/<universe>/ticker.xlsx`  |
| Benchmark symbol         | `config_<universe>.json`              |
| Data / result paths      | `config_<universe>.json` (or CLI arg) |

---

## The 5-Step Pattern

### Step 1 — Identifier → Yahoo Finance ticker mapping

**Goal:** convert the source repo's native identifiers (ISIN, CUSIP, CIK, …)
to Yahoo Finance ticker symbols.

**Inputs:**
- Source repo's instrument list with native identifiers
- Exchange listing data (exchange name, currency, ticker code)

**Method (in order of preference):**
1. Extract from already-scraped profiles that contain `exchange_listings`
   (what we did for justETF — zero external calls)
2. OpenFIGI batch API (`https://api.openfigi.com/v3/mapping`, free, 250/req)
3. Manual curation for the residual ~5–10% that neither source covers

**Exchange → suffix mapping (canonical table):**

| Exchange substring | YF suffix | Currency |
|--------------------|-----------|----------|
| xetra / deutsche börse | `.DE` | EUR |
| euronext amsterdam / amsterdam | `.AS` | EUR |
| euronext paris / paris | `.PA` | EUR |
| borsa italiana / milan | `.MI` | EUR |
| london stock exchange / lse | `.L` | GBP |
| six swiss | `.SW` | CHF |
| nyse / nasdaq / new york | `` (no suffix) | USD |
| *(see generate_isin_to_yf_map.py for full list)* | | |

**Preference order:** `.DE` → `.AS` → other EUR → `.L` → `.SW` → USD

**Outputs:**
- `data/ticker/isin_to_yf.json` — full map `{ isin: { yf_ticker, exchange, currency, ... } }`
- `data/ticker/isin_to_yf.csv`  — flat CSV for review

**Script:** `generate_isin_to_yf_map.py` (this repo)

---

### Step 2 — Build the filtered ticker list for myfinance2

**Goal:** produce `data/ticker/<universe>/ticker.xlsx` with a `ticker` column
containing the Yahoo Finance symbols that myfinance2 will actually analyse.

**Decisions to make:**
- Which filters to apply (e.g. AUM > 500M EUR, TER < 0.30%, category = "Global")
- Which exchange to prefer (should match Step 1 preference)
- Whether to include the benchmark ticker in the list (myfinance2 excludes it
  automatically via `load_data()`, but it must be in the parquet)

**Script to write:** `generate_etf_ticker_list.py` (myfinance2 repo)
- loads `isin_to_yf.json` from Step 1
- applies filters from `profiles.jsonl`
- writes `ticker.xlsx`

---

### Step 3 — Create `config_<universe>.json`

Copy `config.json` and change exactly two things:

```json
{
  "benchmark": "<YF ticker for this universe>",
  ...
}
```

**Benchmark choices by asset class:**

| Universe          | Suggested benchmark  | Rationale                            |
|-------------------|----------------------|--------------------------------------|
| ETFs (global)     | `IWDA.AS`            | MSCI World, EUR, high liquidity      |
| ETFs (US-focused) | `SPY`                | S&P 500, USD                         |
| Italian equities  | `FTSEMIB.MI`         | *(current default)*                  |
| European equities | `MEUD.PA`            | MSCI Europe, EUR                     |
| Bonds             | `AGGH.L`             | iShares Global Agg Bond              |
| Crypto            | `BTC-USD`            | Bitcoin as market proxy              |

The benchmark must be downloadable by YFinance and will be added to the ticker
download list so it lands in the parquet alongside the universe instruments.

---

### Step 4 — Run the pipeline

Two scripts need a `--universe` flag (or equivalent config switch) added:

**`get_historical_ohlc_data.py`:**
```
input:   data/ticker/<universe>/ticker.xlsx
output:  data/ohlc/historical/<universe>/ohlc_data.parquet
```

**`pipeline.py` (via `batch_trader.py` or `run_ta_agents.py`):**
```
input:   data/ohlc/historical/<universe>/ohlc_data.parquet
         config_<universe>.json
output:  data/results/<universe>/analysis_results.parquet
```

No logic changes to either script — only path/config parameterisation.

---

### Step 5 — Generalise the Streamlit app

Add a **universe selector** to the sidebar that switches three things:
- `DEFAULT_DATA_PATH` → `data/results/<universe>/analysis_results.parquet`
- Page/sidebar title string
- Ticker input placeholder text

All rendering (`_render_bo`, `_render_ma`, AI calls) stays unchanged.

---

## Asset-Class Checklist (copy for each new universe)

```
Universe name:          _______________
Source repo/data:       _______________
Native identifier type: _______________  (ISIN / CUSIP / symbol / ...)
YF mapping method:      _______________  (profiles / OpenFIGI / manual)
Benchmark YF ticker:    _______________
Preferred exchange:     _______________
Filter criteria:        _______________
config file:            config_<universe>.json
ticker file:            data/ticker/<universe>/ticker.xlsx
OHLC path:              data/ohlc/historical/<universe>/
results path:           data/results/<universe>/

Step 1 done:  [ ] isin_to_yf.json produced
Step 2 done:  [ ] ticker.xlsx produced + validated against YF
Step 3 done:  [ ] config_<universe>.json created
Step 4 done:  [ ] parquet populated, pipeline runs clean
Step 5 done:  [ ] app universe selector works
```

---

## Asset Classes Completed / In Progress

| Universe        | Source             | Benchmark   | Status      |
|-----------------|--------------------|-------------|-------------|
| Italian equities | Borsa Italiana    | `FTSEMIB.MI`| ✅ live     |
| ETFs (justETF)  | this repo          | `IWDA.AS`   | 🔄 Step 1 done |
