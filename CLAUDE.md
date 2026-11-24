# UC Discovery Engine

## What this project is
An AI-powered discovery engine that mines public customer voice about Urban Company (UC) to
explain its **90-day repeat-booking** problem: why customers who book once don't book again
within 90 days. Collected text is cleaned, filtered and tagged against `codebook.yaml`, and the
tags feed a decomposition of the 90-day repeat-booking metric (who doesn't rebook, why, and
where they go instead).

## Scope
In scope (public, no-login sources only):
- Google Play Store reviews (package `com.urbanclap.urbanclap`)
- Apple App Store reviews (app id `1032480595`)
- ~~Trustpilot~~: **excluded** 2026-11-22. Its own bot check blocks anonymous access (not a login wall); see docs/evidence_log.md
- Consumer complaint forums (consumercomplaints.in, brand page `urban-company-b115885`)
- YouTube comments (via the official YouTube Data API, with a developer API key)
- Reddit (built later, via Apify)
- UC's public shareholder letters and earnings-call transcripts (investorrelations.urbancompany.com)

Out of scope: UC's internal data, the UC app or the logged-in website, partner/provider apps,
and paid data providers.

## HARD CONSTRAINT (standing rule, applies to every task in this repo)
- Only use publicly accessible pages that need **no login**.
- **Never** log into Urban Company's app or website. **Never** use saved credentials, browser
  cookies or session tokens for any site.
- **Never** scrape anything behind a login wall.
- If a source needs sign-in to show more (for example a review site gating later pages), **stop
  that source**, report what it needed, and move on. Do not attempt a workaround.
- The same goes for bot challenges (CAPTCHA, WAF "verifying your connection"): stop and report.
  Don't bypass them with headless browsers, proxies or challenge solvers.
- API keys for official public APIs (YouTube Data API, Groq, Apify) are allowed; they are
  developer keys, not user logins. They live in `.env`, never in code.

## Folders
- `collect/` — one collector per source plus `run_all.py`, the CLI that runs them (`python -m collect.run_all <source>`).
- `pipeline/` — post-collection steps: clean.py (PII scrub), filter.py (relevance + routing), tag.py (Groq LLM tagging against codebook.yaml).
- `data/raw/` — collector output, one `<source>.jsonl` per source (git-ignored).
- `data/processed/` — cleaned, filtered and tagged datasets produced by `pipeline/` (git-ignored).
- `docs/` — notes and the metric decomposition write-up; `docs/evidence_log.md` is the dated log of source decisions and limits.
- `config.yaml` — per-source settings (enabled, max_items) and the shared keyword list.
- `codebook.yaml` — the 14 tagging fields and their allowed values.

## Conventions
- Python 3.9 (system python3), virtualenv in `.venv/`.
- Every collector exposes `run(config: dict) -> dict` (a summary) and writes
  `data/raw/<source>.jsonl`, one JSON object per line, with at least
  `source, id, text, date, url`.
- Collectors save EVERY fetched item to `data/raw/` (with `matched_keywords`); the keyword decision is made in
  `pipeline/filter.py`. Never filter at collection time, so keywords can change without re-collecting.
- When a collector hits a real access limit, record it in that module's docstring and in
  the run summary. Don't guess the limit.
- Record every source decision / access limit in `docs/evidence_log.md` (dated) so it isn't rediscovered.
- Nothing reaches `data/processed/` without passing the PII scrub in `pipeline/clean.py`.
- Shareholder letters/transcripts are never tagged (quotes only). category `product_purchase` is tagged but excluded from the repeat-booking decomposition.
- Don't invent codebook values that affect scoring; ask the user.
