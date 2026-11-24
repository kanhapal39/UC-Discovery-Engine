# UC Discovery Engine

An AI-powered discovery engine for **Urban Company's 90-day repeat-booking problem**: why customers who book once
don't book again within 90 days, and where those bookings go instead. It mines public customer voice, tags each
review against a fixed codebook with an LLM, and decomposes the non-rebooking reasons into a 4-stage funnel.

Why it matters: UC says repeat users are **~84% of its NTV** (Q3 FY26 shareholder letter), so every customer who
doesn't come back is expensive.

## Sources: public only, no login

| Source | How | Status |
|---|---|---|
| Google Play reviews (`com.urbanclap.urbanclap`) | `google-play-scraper` | 30,000 collected |
| consumercomplaints.in (brand page `urban-company-b115885`) | polite HTML fetch | 168 complaints (all pages) |
| Apple App Store (id `1032480595`) | Apple's public RSS feed | 3 reviews (the India feed is empty) |
| UC shareholder letters and earnings-call transcripts | public investor-relations PDFs | 811 passages; used for context quotes only, never tagged |
| YouTube comments | official Data API (developer key) | built, not run yet |
| Reddit | via Apify | not built yet |
| Trustpilot | — | **excluded**: its bot check blocks anonymous access |

**Standing rule (see [`CLAUDE.md`](CLAUDE.md)):** only pages that need no login. No logging into UC's app or site, no
saved cookies or credentials, nothing behind a login wall. If a source needs a sign-in or shows a bot check, that
source is stopped and the reason logged, never worked around. API keys for official public APIs live in `.env`,
never in code.

## Pipeline

```
collect  →  clean  →  filter  →  (sample)  →  tag  →  decompose
```

| Step | Module | What it does |
|---|---|---|
| collect | `collect/` | One collector per source. Saves **every** fetched item to `data/raw/` (keyword decisions happen later) |
| clean | `pipeline/clean.py` | Normalise, dedupe, **PII scrub** (phones, emails, addresses, pincodes): 22,084 items. Nothing reaches `data/processed/` without it |
| filter | `pipeline/filter.py` | Relevance and keyword rules, near-duplicate removal (MinHash LSH): 1,118 customer-voice items |
| sample | `pipeline/sample.py` | Stratified 500-review sample (source × star rating × keyword group), seeded |
| tag | `pipeline/tag.py` | LLM tagging (NVIDIA Nemotron 3 Ultra) against [`codebook.yaml`](codebook.yaml): 14 fields, strict validation, verbatim evidence quotes, names redacted |
| decompose | `pipeline/decompose.py` | Assigns each metric-eligible review to the first funnel stage it fails |
| validate | `pipeline/score_gold.py` | Scores the tags against a 20-review human-corrected golden set ([`docs/golden_20.csv`](docs/golden_20.csv)) |
| explore | `dashboard/app.py` | Read-only Streamlit dashboard |

## Finding: the 4-stage funnel

Across **402 metric-eligible reviews** (product purchases and house help excluded):

| Stage | Reviews | Share |
|---|---|---|
| 1. The job didn't happen (no-show, UC cancelled) | 89 | 22% |
| 2. The job was done poorly (blamed on the professional) | 117 | 29% |
| 3. Done OK, but trust soured (warranty, support, app, trust) | 138 | 34% |
| 4. Leaked to a local provider or competing app | 4 | 1% |
| Outside the framework (mostly happy customers) | 54 | 13% |

- **Stages 2 + 3 = 63%** of complaint voice, stable across tagging runs. The split between them depends on a tagging
  rule, so it isn't precise.
- **Warranty refusal** appears in 133 reviews, and 55 of them follow a poorly done job: the warranty was the way back,
  and it was refused. **Unresponsive support** appears in 127 reviews, and 32 of them follow a job that never happened.
- Leakage goes to **local providers**, not the same professional off-app (2 of 402 reviews).

**Caveats:** these are shares of public complaint voice, not of churned customers. Read every percentage as
directional (±5–8 points); two runs of the same model disagree on 11 of 500 metric-eligible flags.

- Full audit trail (every source decision, limit, codebook change and run, dated): [`docs/evidence_log.md`](docs/evidence_log.md)
- Part 2 write-up (the metric decomposition): in progress, to be added under `docs/`

## How to run it

**Setup** (Python 3.9+):
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your own keys (see below)
```

`.env` keys: `NVIDIA_API_KEY` (tagging), `YOUTUBE_API_KEY` (YouTube collector), `APIFY_TOKEN` (Reddit, later).
`GROQ_API_KEY` and `OPENROUTER_API_KEY` are optional alternative providers. `.env` is git-ignored.

**Collect, clean, filter, sample:**
```bash
python -m collect.run_all play_store
python -m collect.run_all consumer_complaints
python -m collect.run_all app_store
python -m collect.run_all shareholder_letters
python -m pipeline.clean
python -m pipeline.filter
python -m pipeline.sample
```

**Tag** (the Part 2 run):
```bash
python -m pipeline.tag --provider nvidia --in data/processed/sample.jsonl \
    --out data/processed/tagged_v2.jsonl --concurrency 2
```
`python -m pipeline.tag --dry-run` prints the generated prompt without calling the API.

**Decompose, validate, explore:**
```bash
python -m pipeline.decompose       # funnel -> data/processed/decomposition.json
python -m pipeline.score_gold      # golden-set agreement for tagged_v2 (--in <file> for another run)
streamlit run dashboard/app.py     # dashboard on http://localhost:8501 (run from the project root)
```

## Where outputs land

Two result files **are included** in the repository, so the dashboard works straight after cloning, with no API keys
and no pipeline run:

| File | What it is |
|---|---|
| `data/processed/tagged_v2.jsonl` | the 500-review sample tagged by Nemotron (PII-scrubbed, names redacted): input to the funnel and dashboard |
| `data/processed/decomposition.json` | the 4-stage funnel output of `python -m pipeline.decompose` |

Everything else stays out of git: `data/raw/` (raw collected reviews and investor PDFs) and the rest of
`data/processed/` (cleaned and filtered sets, the tag cache, logs). Re-create those with the commands above.

To open the dashboard from a fresh clone: run Setup, then `streamlit run dashboard/app.py`. Without the earlier
tagging run (`tagged.jsonl`), the Data Quality page shows only the current run, not the old-vs-new comparison.

## Repository layout

| Path | Contents |
|---|---|
| `collect/` | source collectors and `run_all.py` |
| `pipeline/` | clean, filter, sample, tag, decompose, score_gold |
| `dashboard/` | `app.py` (Streamlit) and `content.yaml` (the dashboard's wording) |
| `codebook.yaml` | the 14 tagging fields, allowed values and rules |
| `config.yaml` | per-source settings, keyword groups, filter, sample and tagging settings |
| `docs/` | `evidence_log.md` (audit trail), `golden_20.csv` (golden set) |
| `.streamlit/config.toml` | dashboard light theme |
| `CLAUDE.md` | project scope and the public-sources-only rule |
