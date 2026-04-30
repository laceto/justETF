# Plan: OpenAI Extraction Path for scrape_justetf.py

**Goal:** Add an OpenAI extraction path to `scrape_justetf.py` that populates `ETFProfile` from raw HTML via an LLM, selectable with `--use-ai`, while keeping the BeautifulSoup path as the default.

---

## Breakdown

```
A1  Prepare the HTML input for the LLM
  A1.1  Write a _clean_html(html) function that strips scripts/styles/nav
        and returns a compact text representation the model can read cheaply

A2  Define the OpenAI extraction contract
  A2.1  Write a JSON schema (or structured output schema) that mirrors ETFProfile
        — the model must return exactly this shape
  A2.2  Write the system + user prompt that instructs the model to fill the schema
        from the cleaned HTML

A3  Implement the OpenAI extraction function
  A3.1  Add openai to imports (openai>=1.0 SDK); read OPENAI_API_KEY from env
  A3.2  Write _extract_with_openai(html, isin) → ETFProfile using
        client.chat.completions.create() with response_format={"type": "json_object"}
  A3.3  Map the model's JSON response back to ETFProfile + nested dataclasses

A4  Wire it into the existing fetch/parse pipeline
  A4.1  Add --use-ai flag to _build_parser()
  A4.2  In fetch_etf_profile(), accept a use_ai: bool param and branch to
        _extract_with_openai() or _parse_profile() accordingly

A5  Verify end-to-end
  A5.1  Run: python scrape_justetf.py --isin IE00B5BMR087 --use-ai
        and confirm ETFProfile fields are populated
```

## Status

- [ ] A1.1 — `_clean_html(html)`
- [ ] A2.1 — JSON schema mirroring `ETFProfile`
- [ ] A2.2 — System + user prompt
- [ ] A3.1 — openai import + API key setup
- [ ] A3.2 — `_extract_with_openai(html, isin)`
- [ ] A3.3 — JSON → ETFProfile mapping
- [ ] A4.1 — `--use-ai` CLI flag
- [ ] A4.2 — branch in `fetch_etf_profile()`
- [ ] A5.1 — end-to-end test run
