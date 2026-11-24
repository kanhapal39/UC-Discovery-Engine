# Evidence log

A dated record of source decisions, access limits and data-quality findings. One entry per fact.
Check here before re-investigating a source.

---

## 2026-11-22 — Trustpilot EXCLUDED from the project
- **Decision:** Trustpilot is out of scope for this project. `config.yaml` has `trustpilot.enabled: false`.
- **Reason:** every anonymous request to `https://www.trustpilot.com/review/urbancompany.com`
  (also `?page=2`, `?page=11`, and `/review/urbanclap.com`) returned **HTTP 403 with an AWS WAF
  "Verifying your connection..." JavaScript bot challenge**. This is Trustpilot's own bot check,
  **not a login wall**.
- **Why we didn't work around it:** the project's hard constraint (CLAUDE.md) says to stop a
  source that blocks automated access and not bypass it (no headless browsers, proxies or challenge solvers).
- **Not tested:** whether Trustpilot also gates pages beyond about 10 behind sign-in. We never got past page 1.
- **Code:** `collect/trustpilot.py` is kept for reference. If run, it makes one request,
  detects the challenge and stops with `status: blocked`.

## 2026-11-22 — App Store: India storefront feed empty
- Apple's public customer-review RSS feed returned 0 entries for the `/in/` storefront in every
  URL variant tried. `/us/` returns 50–150 entries inconsistently; `ae`, `gb` returned 0.
- `app-store-scraper` (in requirements.txt) returns 0 reviews (JSON decode error; Apple endpoint changed).
- Impact: iOS voice is under-represented. The data is mostly US-storefront reviews.

## 2026-11-22 — PII scrub added to pipeline/clean.py
- Every item is scrubbed before anything is written to `data/processed/`: Indian phone numbers,
  email addresses (including masked `[protected]@gmail.com` forms), addresses after an
  `address:` label, and pincodes. A verification pass re-runs the detectors and aborts the whole
  write if anything remains.
- The log (`data/processed/clean_log.json`) records counts only, never the removed text.
- Address spans are bounded (pincode, next `Label:`, sentence end, or 120 chars) instead of
  "everything after address:". consumercomplaints.in flattens the address and body onto one line, so the
  literal rule would delete the complaint.
- consumer_complaints URLs are dropped in processed data: the slugs contain address text.
- **Known gap:** personal names are NOT scrubbed (no reliable regex). Seen in about 1 of 24 complaints
  ("Main <Name> ..." in Hinglish).
- **Known gap:** langdetect mislabels Hinglish (e.g. as `id`, Indonesian).
- First run: 24 complaints → 8 scrubbed (7 addresses, 2 emails). Shareholder letters: 16 items
  scrubbed (UC/auditor office addresses, IR contact numbers).

## 2026-11-22 — Codebook: 9 scoring fields locked; 5 values added from pilot read
- The owner decided the value sets for decision_trigger, acquisition_channel, post_service_expectation,
  service_outcome, rebooking_channel, reason_not_rebook, unmet_need, workaround and city_signal.
  All are `status: fixed`.
- Added after a pilot read of 51 customer texts (keyword-matched Play Store, App Store, complaints):
  - `reason_not_rebook: warranty_redo_denied`: refused/ignored warranty or redo claims were the top theme (~25/51).
  - `reason_not_rebook: booking_cancelled_by_uc`: UC-side cancellations (~10/51), distinct from a professional no-show.
  - `reason_not_rebook: not_applicable`: for happy or still-rebooking customers. `unclear` now means "no reason given" only.
  - `service_outcome: service_not_delivered`: the service never happened (no-show or UC cancellation).
  - `unmet_need: honoured_warranty_after_sales`.
- Tagging rules: a final bill that differs from the quote → `trust_quality_concern` (not `price_too_high`,
  which means "it's just expensive"). Can't reach a human / bot loop → `faster_responsive_support`.

## 2026-11-22 — Product purchases tagged but excluded from the repeat-booking metric
- New category value `product_purchase` (Native RO purifiers, Native smart locks and their
  after-sales, AMC and warranty). It replaces the draft values water_purifier_product / smart_lock_product.
- **Reason:** a one-time appliance purchase isn't a home-service booking. The brief's metric
  definition already excludes InstaHelp quick-service housekeeping from the anchor booking, and
  product purchases are excluded the same way.
- These items ARE tagged fully: they inform post_service_expectation and unmet_need (after-sales support).
- They're EXCLUDED from the Part 2 repeat-booking decomposition: `config.yaml metric.exclude_categories: [product_purchase]`.
- Scale in the pilot: ~12 of 51 customer texts were product complaints.

## 2026-11-22 — city_signal tiers fixed
- Metro: Delhi NCR, Mumbai, Bangalore, Chennai, Kolkata, Hyderabad, Pune, Ahmedabad.
- Tier 2 (owner's list, matching UC's expansion cities rather than a government classification):
  Jaipur, Lucknow, Chandigarh, Kochi, Coimbatore, Indore, Nagpur, Surat, Vadodara, Visakhapatnam,
  Bhopal, Patna, Guwahati, Kanpur, Ludhiana, Mysore, Nashik, Thiruvananthapuram.
- Any other named city → `tier_3_or_smaller`. No city → `unclear`. Spelling aliases (Bengaluru, Gurugram,
  Trivandrum, …) are in codebook.yaml.

## 2026-11-22 — Shareholder letters / earnings-call transcripts: not tagged
- They're management's own statements, not customer voice, so the codebook's customer-experience fields don't apply.
- Excluded from tag.py (`config.yaml pipeline.untagged_sources`). filter.py routes them to
  `data/processed/business_context.jsonl`, a separate qualitative source of direct quotes for the
  deck's business-context framing (e.g. UC's stated repeat-customer revenue share). They're never used as tagged evidence rows.

## 2026-11-22 — house_help excluded from the repeat-booking metric
- `config.yaml metric.exclude_categories: [product_purchase, house_help]`.
- **Reason:** the brief's metric definition explicitly excludes "Quick-service housekeeping (InstaHelp)"
  from the anchor booking used for the 90-day repeat-booking rate. UC's on-demand maid/cook/babysitter
  House Help offering is that same model (recurring on-demand help, not a scheduled visit for a defined job).
- house_help items are still tagged normally; they're only left out of the Part 2 decomposition.

## 2026-11-22 — Near-duplicate check: all-pairs replaced with MinHash LSH
- Reason: the all-pairs rapidfuzz check is O(n²) and would not scale to the Step 3 Play Store pull (60,000 reviews).
- Method: char 5-gram MinHash (126 hashes, 63 bands × 2 rows), buckets shared across sources; rapidfuzz.ratio ≥ 90
  only within candidate buckets; the earliest kept copy wins, as before.
- **Not keyed on a text prefix:** consumercomplaints.in adds "[Resolved]" to titles, so the first real duplicate
  had prefixes "urban co" vs "resolved". A prefix key would have split it.
- **Not bucketed per source:** the old check compared across sources (the same person posting on two sites).
- Bug found and fixed during validation: the first hash family ((a·h+b) mod 2^61−1, 31-bit a, crc32 h) barely
  wrapped, so the hashes were correlated and LSH missed 16% of duplicates. Replaced with blake2b 64-bit hashes ×
  odd 64-bit multipliers.
- Equivalence: real data, 69 items → 1 dropped by both methods (identical). Stress test of 4,546 items with
  1,200 planted near-copies (1–14% char edits, "[Resolved]" prefixes, truncation, cross-source) → 1,110 dropped
  by both, identical pairs. LSH 5.8s vs all-pairs 47s; 22.7k items in 18s.
- `python -m pipeline.filter --check-dedupe` re-runs the all-pairs reference and fails if results differ.

## 2026-11-22 — pipeline/tag.py built (not yet run against Groq)
- No GROQ_API_KEY was set, so the code was tested with a fake LLM client only: validation, the repair
  re-ask, "unclear must appear alone", the evidence-quote verbatim check, city tiering and caching all pass.
- The prompt is generated from codebook.yaml. platform and city_signal are set in code, not by the model.
  Invalid output is never coerced: the item is marked `tag_status: invalid` and kept out of scoring.
- Config model `llama-3.3-70b-versatile` is unverified; tag.py checks Groq's live model list at startup.
- Personal names are still present in some texts (e.g. "I'm <name>…" in a complaint) and will be sent to Groq
  with the public review text. Known gap from the clean step.
- Clean step now strips the consumercomplaints.in footer "Was this information helpful? No (n) Yes (n)".

## 2026-11-22 — Volume run: Play Store 30k, all complaints
- Play Store: 30,000 newest reviews fetched (dated 2025-05-29 → 2026-11-15); 579 matched keywords. The collector now
  waits request_delay_seconds (2s) between 200-review batches (previously it didn't use the delay).
- consumercomplaints.in: the brand listing has only 9 pages; all 168 complaints fetched, 36 matched. The first run hit a
  30s read timeout (slow server, not a block); PoliteSession now retries timeouts/connection errors twice with backoff.
- Pipeline: 1,435 raw → 1,428 after clean (27 scrubbed) → **607 customer-voice items kept** (+811 business-context
  paragraphs). Dropped: 9 too_short, 1 near-duplicate. `--check-dedupe`: identical to all-pairs (608 items).
- **Skew:** `warranty` matches 475 of 607 kept items (78%). The repeat-intent phrases ("didn't book again",
  "stopped using urban company", "switched to a local") matched 0 items. 501 of 569 Play items are 1-star.

## 2026-11-22 — Keyword expansion (switching + loyalty); raw now keeps everything
- **Design fix:** collectors had applied the keyword filter BEFORE saving, so data/raw/ held only matches (579 of 30,000
  Play reviews), and re-matching was impossible. Collectors now save every fetched item, and filter.py applies the keyword
  rule (`filter.require_keyword_sources`). Play Store (30,000) and complaints (168) were re-collected once to recover the data.
- **Second fix:** an empty fetch no longer overwrites an existing raw file. The App Store RSS feed returned 0 for every
  storefront on re-run and had wiped the 3 App Store reviews; they were restored from the processed backup (`restored_from` field).
- Keywords: +17 switching, +9 loyalty (owner's list). Matching is now word-boundary exact, apostrophe-insensitive, with fuzzy
  only for keywords ≥12 chars, on whole-word windows whose first word shares 3 letters with the keyword's first word.
  Variant `overcharged` = overcharge / over charge / over-charge. This fixed false matches like "UNreliable service" →
  "reliable service", "started the service" → "redo the service", and "on time only" → "one time only".
- min_words 5 → 3. Partner-voice patterns narrowed: a bare "partner app" or "my account blocked" also caught customers.
- Result: 30,988 raw → 22,084 after clean → **1,118 kept** (was 607; 597 of those still kept, 10 were false fuzzy matches).
  521 newly kept: 267 switching, 241 loyalty, 8 both, 5 original-only. Dedupe check identical (1,121 items).
- **Loyalty precision is low:** of the 241 new loyalty-only items, 149 are 1-star; only 64 are 4–5 star. "every time i" (148)
  is mostly "every time I book it gets cancelled". "regular customer" (28) is mostly lapsed loyal customers complaining.
- "uninstall" (146): ~106 app uninstalls, ~13 AC *uninstallation* service, ~29 unclear.

## 2026-11-22 — Tagging pilot (20 items, seed 0)
- Model: `llama-3.3-70b-versatile` is no longer offered on Groq (startup check caught it); switched to `openai/gpt-oss-120b`.
- Mechanics: 20/20 valid (4 after one repair), 6/20 evidence quotes rejected (the model stitches or paraphrases
  sentences), 4m39s at 25 req/min.
- Eligibility leaks (2/20): an InstaHelp item with category left blank → counted as metric-eligible; a "Native items" warranty
  item tagged ac_appliance_repair instead of product_purchase → eligible.
- Codebook gaps seen: rebooking_channel has no "went to a local/offline provider" value (3/20 did, e.g. a society plumber);
  reason_not_rebook has no competitor-app value (2/20 name Snabbit); post_service_expectation `nothing_specific` is used as
  "not stated" (12/20); in_app_pain `none` vs `not_mentioned` used interchangeably (7/7).

## 2026-11-22 — Pilot re-check passed; name redaction added; Groq free-tier limits block a same-day full run
- Codebook: rebooking_channel += local_offline_provider; reason_not_rebook += switched_to_competitor_app (+ rule: always add
  switched_to_local_provider when stated); service_outcome += cancelled_over_quote_dispute; post_service_expectation rule
  (unclear = not stated; nothing_specific = explicitly nothing beyond the job); safety/identity → trust_quality_concern.
- Metric leak fix: category.code_overrides force house_help (InstaHelp / house help) and product_purchase (Native + product
  word) in code. Narrowed with data: bare "helper" (technicians' helpers, 11 hits), "maid" (comparisons), RO/purifier
  (other-brand RO repair is a service) and "lock" are left to the model.
- Same-20 re-check: both leaked items now excluded; local-provider, competitor, quote-dispute and safety cases all tagged
  as intended; post_service_expectation 14 unclear / 1 nothing_specific (was 12 nothing_specific).
- Evidence quotes: an exact-copy instruction alone didn't help (5/20 still stitched). Now the longest verbatim span is kept
  (≥40 chars, 6 words).
- Name redaction runs inside the tagging call (no extra request): the model lists names; code replaces the ones that really
  occur, plus Mr./Mrs./Dr. Name, with [NAME]. Brand stoplist. Live test on 19 name-bearing items: 17 redacted, 49 names.
- **Groq free tier (from response headers): 1,000 requests/day and 8,000 tokens/minute, for gpt-oss-120b, gpt-oss-20b and
  qwen3.8-27b alike.** About 2.5k tokens/item → ~3 items/min → the 1,118-item run needs ~6+ hours and more than one day's
  requests. tag.py now follows retry-after and stops cleanly at the daily limit (cache makes it resumable).

## 2026-11-22 — Batched tagging (5 reviews/request) validated on the same 20; full run started
- Comparison on the same 20 items: A = earlier per-item re-check, B = fresh per-item run (final prompt), C = batched.
  Scoring fields (category … sentiment, excl. draft in_app_pain/household_signal and code-derived city):
  **B vs A 89% identical (per-item noise floor); C vs B 86%.** metric_eligible identical 20/20.
- Mixing checks: 0 contamination flags (quotes and names are checked against the other reviews in each batch); all 18
  batched quotes are in their own review's text and in no other; Native → product_purchase and InstaHelp → house_help
  both still excluded under batching.
- Batch-specific shift: household_signal `not_mentioned` → `none` (8/20) — the draft none/not_mentioned overlap.
- Groq returned `400 json_validate_failed` (empty generation) on one batch → max_completion_tokens 8192, and failed batches
  fall back to per-item tagging. 7/20 pilot items fell back, so batches now get one repair round in the same conversation
  before any per-item fallback. `tag_mode` per row records batch / batch_repaired / single_fallback.
- Expected full-run time about 3–3.5 h: the 8,000 tokens/min limit binds, about 2k tokens per item batched vs 2.8k per item.

## 2026-11-22 — Tagging model: gpt-oss-120b for the whole dataset; NVIDIA Nemotron as a later standby
- The owner asked to make NVIDIA Nemotron ("Ultra") the fallback model. Not applied: a different model only for fallback items
  would mix two models in one dataset, and fallback items are the harder reviews, so any model difference would bias them.
- Decision: the full run stays on openai/gpt-oss-120b. Nemotron to be added later as an optional standby provider (needs an
  exact model id and NVIDIA_API_KEY). It would replace gpt-oss for the WHOLE dataset only if its same-20 comparison matches
  the per-item noise floor (~89% on scoring fields).
- First full run (16:07) hung 15 min on a dropped connection (CLOSE_WAIT, no client timeout). Fixed with a 120s timeout +
  retries on timeouts/connection errors; restarted ~16:22.

## 2026-11-22 — CORRECTION: the full-run stalls were Groq's 200k TOKENS-per-day cap, not a dropped connection
- The full error: "Rate limit reached for model openai/gpt-oss-120b … on tokens per day (TPD): Limit 200000, Used 199135".
  The tagger was sleeping through long retry-after waits (606–1339s). The CLOSE_WAIT socket was just an idle pooled
  connection; the earlier "no client timeout" diagnosis was wrong (the 120s timeout stays, but it wasn't the cause).
- The response headers only show the per-minute (8,000) and per-day request (1,000) limits; the TPD cap appears only in the
  429 message. tag.py now treats any per-day limit as a clean stop (cache keeps progress).
- Impact: about 2k tokens per item batched → the 1,118-item run needs ~2.2M tokens, about 11 days at 200k/day on the free
  tier. Today's budget was used by the pilots and comparisons. 25/1,118 are tagged so far.

## 2026-11-22 — Option 2 chosen: reasoning_effort low + 10 reviews per batch (free tier)
- The owner chose to cut tokens per item rather than pay for a higher tier. `tagging.reasoning_effort: low`, `batch_size: 10`.
- The daily token cap behaves like a bucket that refills at about 8.3k tokens/h (a 1.4k-token deficit gave "try again in
  10m"), so tag.py now waits out refills of up to 30 min instead of stopping.
- The cache key now includes reasoning effort and batch size, because the batch prompt text is identical for 5 and 10 and
  old results would otherwise be reused as if they were the new method.
- Same-20 re-check (run D) started; it must match the per-item noise floor (B vs A 89% on scoring fields) before the full run.

## 2026-11-22 — NVIDIA API key verified; standby model identified
- NVIDIA_API_KEY in .env: an authenticated test call returned 200 in 1.6s; valid JSON reply; 78 tokens (41 reasoning).
- The owner's "Nvidia ultron ultra 3" = `nvidia/nemotron-3-ultra-550b-a55b` (listed at integrate.api.nvidia.com/v1).
  Recorded as `tagging.standby` in config.yaml; not wired into tag.py. No rate-limit headers are returned, so NVIDIA's limits
  are unknown until tested under load.
- Run D, first attempt: the first 10-review batch used 5,253 tokens (~525/item vs ~2,000 at default effort, batch 5), so the
  full run would need ~600k tokens, about 3 days. Its repair request then hit the daily cap (retry-after 1954s) and the valid
  reviews of the batch were discarded. Fixed: valid batch results are kept when a repair is blocked. The refill wait limit was
  raised to 60 min. Run D restarted.

## 2026-11-22 — OpenRouter key verified (second standby)
- OPENROUTER_API_KEY: /api/v1/key → 200, free tier, usage 0. Test call to `nvidia/nemotron-3-ultra-550b-a55b:free` → 200 in
  1.2s, valid JSON, cost 0, served upstream by NVIDIA. No rate-limit headers.
- OpenRouter also lists `openai/gpt-oss-120b` (paid). With credits it would let the run continue on the SAME model as the
  Groq pilots, with no method change. Recorded as `tagging.standby_openrouter`; not wired into tag.py.

## 2026-11-22 — Owner: finish tagging via OpenRouter → gpt-oss-120b pinned to the Groq backend
- OpenRouter lists 24 backends for openai/gpt-oss-120b. An auto-routed test went to Google and returned broken JSON
  ('{"final{"' + blank lines). Pinned to Groq (`provider.order: [Groq]`, no fallbacks): clean JSON, $0.000146.
- This keeps the validated model + backend + method (batch 5, default reasoning: 86% vs the 89% noise floor), billed by
  OpenRouter, so our Groq free-tier token cap no longer applies. Estimated cost of the full run ≈ $1 at listed prices.
- The account has $0 credits, so credits must be added before the full run. Runs D (Groq low-effort/batch 10) and N
  (Nemotron) were stopped so only one method tags.
- Same-20 via OpenRouter (run O, batch 5, default reasoning, Groq backend): 20/20 in 43s, no cap waits, 35.2k tokens,
  $0.0094. Scoring-field agreement: **O vs C (same method, two runs) 88.5%** (= the per-item noise floor 89%), so OpenRouter
  reproduces the validated method. O vs B 80% and C vs B 86%: batching costs about 6 points against per-item.
- Rule slips seen: switched_to_local_provider dropped on a local-vendor case → now a code check (workaround
  local_offline_provider ⇒ reason must include switched_to_local_provider, else repair). not_applicable on a cancellation
  complaint: model error, not mechanically checkable.
- HTTP 402 (out of credits) now stops the run cleanly. The validation version is part of the cache key.
- Full run started 17:43 via OpenRouter. Account has $0 credits and is running slightly negative; estimated cost ≈ $0.55.
- 17:48: the full run stopped cleanly on OpenRouter HTTP 402 (out of credits; the $0-credit account allows about $0.10 of
  negative balance). **174/1,118 tagged and cached**, $0.108 used (about $0.00062/item), so the remaining 944 need about $0.59.
  Concurrency (4 batches in flight; same prompts, model, checks) was added and tested with a fake client (37/37 items, no
  duplicates); it will be used when the run resumes after credits are added.

## 2026-11-22 — Tagging set reduced to a 500-item stratified sample (owner: lower load/cost)
- `pipeline/sample.py` → data/processed/sample.jsonl (seed 20261122). All 42 complaints + 3 App Store reviews are kept whole;
  455 Play Store reviews are sampled in proportion to (star rating × keyword group).
- Representativeness: Play ratings in sample 83.7/4.2/2.6/2.0/7.5% vs full 83.9/4.1/2.6/2.0/7.5%; keyword groups in sample
  loyalty 22.8 / original 53.0 / switching 24.2% vs full 23.2 / 52.0 / 24.9%.
- Deliberately not built around the 174 already-tagged items (the first run went oldest-first, so they're date-biased).
  95 of them fall in the sample and are reused from the cache. 405 still to tag, about $0.25 via OpenRouter.
- Run: `python -m pipeline.tag --provider openrouter --batch-size 5 --concurrency 4 --in data/processed/sample.jsonl`

## 2026-11-22 — Finishing the 500 sample without OpenRouter credits: Groq (same model) + Nemotron check in parallel
- NVIDIA doesn't host gpt-oss, so a free route on the SAME model exists only on Groq direct (free tier, 200k tokens/day →
  405 items ≈ 713k tokens ≈ 3.5 days). It's the same model + backend + method as the OpenRouter run, so cache keys are
  identical and the 95 tagged sample items are reused.
- Config restored to the validated method: reasoning_effort null (default), batch_size 5. Run D's low/10 settings were
  abandoned without being validated.
- Started: (1) Groq run on sample.jsonl; (2) Nemotron 3 Ultra same-20 check (batch 5, max_tokens 16384, 2 in flight). If
  Nemotron clears the bar, the WHOLE sample (incl. the 95) moves to Nemotron; otherwise Groq carries on.
- The cache loader now skips garbled lines (two processes append to the same cache).
- **Nemotron same-20 (run N) does NOT clear the bar.** On scoring fields: N vs per-item gpt-oss (B) 74%, N vs batched gpt-oss
  (C) 73%, N vs O 73.5%, against gpt-oss-with-itself 88.5–89%. No mixing (18/18 quotes own-text), Native/InstaHelp excluded,
  metric_eligible 19/20 same as B. 20/20 in ~5 min (2 in flight), 48.9k tokens (~2.4k/item), no cut-offs at max_tokens 16384.
- Pattern: Nemotron tags MORE reasons per review (adds warranty_redo_denied 5×, support_unresponsive 4×, app_friction 2×),
  and several additions look defensible (e.g. "rej3cted warranty" → warranty_redo_denied). So it's a different labelling
  style, not obviously worse, but it would shift the reason distribution, so it can't be mixed with gpt-oss tags.
- Per the agreed rule, the sample stays on gpt-oss (Groq direct, free, ~3.5 days; or OpenRouter ≈ $0.25, ~5 min).

## 2026-11-22 — Candidate golden set of 20 reviews (docs/golden_20.csv, .jsonl)
- The same 20 pilot reviews (seed 0). Per scoring field: the majority value across the 4 gpt-oss runs (A, B, C, O), its vote
  count, and Nemotron's value for reference. `needs_review` lists fields with no clear majority (<3/4 or a tie).
- 24 of 200 cells flagged, in 15 of 20 reviews. Text is the PII-scrubbed, name-redacted version (0 phones/emails left).
- NOT yet gold: majority votes repeat shared model biases (e.g. G01, a help-centre complaint, gets reason not_applicable 3/4).
  It becomes the gold reference after a person confirms/corrects each row.

## 2026-11-22 — Owner decision: tag with NVIDIA Nemotron 3 Ultra only (no OpenRouter, no Groq)
- "Don't use open router use Nvidia only." The whole 500-item sample is tagged with nvidia/nemotron-3-ultra-550b-a55b,
  including the 100 previously tagged by gpt-oss, so the dataset uses ONE model. The Groq run was stopped; OpenRouter is unused.
- This overrides the agreed bar: Nemotron matched per-item gpt-oss on 74% of scoring-field values (gpt-oss with itself 89%).
  Known effect: more reasons per review (warranty_redo_denied, support_unresponsive, app_friction added more often).
  docs/golden_20.csv majorities are gpt-oss-based, so compare Nemotron against it only after human correction.
- Owner chose option 2: KEEP the 100 gpt-oss results and tag only the other 400 with Nemotron (mixed-model sample). Every row
  records its model in `tag_model`; analyses should report or split by it. Implemented as `--reuse-from openai/gpt-oss-120b`.
- 18:39: Nemotron run stalled 9+ min at concurrency 4 (no completions, no errors); restarted at concurrency 2 (owner).

## 2026-11-22 — 500-item sample tagged (finished 19:41): 100 gpt-oss + 400 Nemotron
- 500/500 valid (31 needed one in-batch repair, 0 per-item fallbacks, 0 cross-review contamination flags). 556k Nemotron
  tokens. 0 phones/emails left; 68 names redacted in 31 items (77 [NAME] tokens in text + quotes). Local-provider rule: 0 violations.
- metric_eligible 398; excluded: product_purchase 63, house_help 39.
- **Model and source are confounded:** the 100 gpt-oss rows hold 34 of the 42 complaints (the first OpenRouter run went
  oldest-first, and complaints are older), so gpt-oss = 34% complaints vs Nemotron 2%. Differences between the model groups
  (e.g. ac_appliance_repair 45% vs 23%, did_not_seek_again 36% vs 22%) are partly source mix, not model. A by-model split
  can't separate the two. Re-tagging the 100 with Nemotron (~25 min) would remove this.
- Reasons per review: gpt-oss 1.85, Nemotron 2.04. city_signal: 483 unclear, 16 metro, 1 tier_2.
- 19:43: owner approved re-tagging the 100 gpt-oss sample rows with Nemotron, so the sample is single-model. Mixed version backed up in scratchpad.
- 21:15: two Nemotron requests hung 84 min (19:45→21:09) despite the 360s timeout: requests' timeout limits only the gap
  between bytes, and NVIDIA kept trickling bytes. Fixed with a TOTAL per-request deadline (chunked read, abandon + retry).
  44/100 re-tagged before the restart; the rest resume from cache.
- 21:36: my deadline fix set a 60s read-gap timeout, shorter than Nemotron's silent reasoning (60-130s), so 13 requests timed out and 0 succeeded. Read gap now = the 360s deadline; restarted.
- 21:58: the run stalled again (last answer 21:44): the deadline check ran inside an 8 KB-chunk read, and NVIDIA's tiny
  keep-alive bytes never filled a chunk while still resetting the read timeout. Replaced with a watchdog timer that
  force-closes the connection at the deadline, reading data as it arrives. Tested against a fake server trickling 1 byte/0.5s:
  gave up at exactly the 3s test deadline and retried. Restarted with 6 reviews left.

## 2026-11-22 — 500-item sample complete on ONE model (Nemotron 3 Ultra), finished 21:57
- 500/500 valid, all `nvidia/nemotron-3-ultra-550b-a55b` (468 straight from batch, 32 after one in-batch repair; 0 per-item
  fallbacks, 0 contamination). 0 phones/emails left; 68 names redacted in 31 items. Local-provider rule: 0 violations.
- **metric_eligible 399**; excluded product_purchase 62, house_help 39.
- The 100 re-tagged from gpt-oss: 69% of scoring-field values unchanged; metric_eligible changed for 1 item. The
  model/source confound is removed.
- Headline (metric-eligible, n=399), reason_not_rebook: trust_quality_concern 72%, support_unresponsive 39%,
  warranty_redo_denied 30%, not_applicable 11%, professional_no_show 11%, switched_to_local_provider 11%, app_friction 9%,
  price_too_high 9%, booking_cancelled_by_uc 9%. 2.07 reasons per review.
- rebooking_channel is unclear for 60% of items; city_signal is nearly always unclear: reviews rarely say where the next
  booking went or where the customer lives.
- The mixed-model version is backed up in the scratchpad (tagged_mixed_backup.jsonl).

## 2026-11-24 — Tightened rules for four weak fields after gold-set scoring
- Scored the Nemotron 500-run against the user-corrected `docs/golden_20.csv` (13 of 20 gold reviews are in the sample).
  Weak fields: reason_not_rebook 54%, unmet_need 38%, post_service_expectation 69%, service_outcome 77%; whole-row 1/13.
  Main error: adding extra "commonly associated" values (faster_responsive_support, warranty_redo_denied, support_unresponsive, app_friction).
- Added one rule each in codebook.yaml to reason_not_rebook, unmet_need (no bonus values), post_service_expectation
  (bare warranty mention is not an expectation), service_outcome (professional_issue only when the professional is blamed).
  No-show stays service_not_delivered (existing rule). Fields scoring >=92% untouched.
- Re-tagging all 500 with Nemotron into data/processed/tagged_v2.jsonl; v1 kept as data/processed/tagged.jsonl.

## 2026-11-24 — Gold-set warranty fix, final scores, v2 adopted for Part 2
- User decision: warranty refusals are fixed in the GOLD set, not the rule. In docs/golden_20.csv: added
  warranty_redo_denied to reason_not_rebook for G06, G14, G16 (each explicitly describes a warranty refused/voided);
  G14 post_service_expectation unclear -> warranty_support_if_issue. Pre-edit copy kept in the session scratchpad.
- Final scores vs the corrected gold (13 gold reviews in the sample), v1 -> v2: reason_not_rebook 54% -> 69%,
  unmet_need 38% -> 69%, post_service_expectation 62% -> 77%, service_outcome 77% -> 77%; category 92%, acquisition 100%,
  workaround 100%, sentiment 92%, metric_eligible 100% (both runs); decision_trigger 100% -> 85%, rebooking_channel
  92% -> 85% (run-to-run noise, see next entry). Whole rows fully correct: 1/13 -> 2/13.
- User decision: Part 2 (metric decomposition) uses data/processed/tagged_v2.jsonl (402 metric-eligible reviews).
  v1 stays in data/processed/tagged.jsonl for reference.

## 2026-11-24 — Run-to-run noise in Nemotron tags: Part 2 percentages are directional (±5-8 points)
- Two runs of the same model (nvidia/nemotron-3-ultra-550b-a55b, temperature 0, batch 5) on the same 500 reviews
  (v1 = tagged.jsonl, v2 = tagged_v2.jsonl) disagree substantially, including on fields whose prompt wording did not change:
  - metric_eligible flipped for 11 of 500 reviews (399 -> 402 eligible).
  - category changed on 92/500 (mostly unclear / not_mentioned / app_general swaps); rebooking_channel on 114/500
    (75 are did_not_seek_again <-> unclear); decision_trigger on 83/500 (reactive_breakdown/planned_habitual <-> unclear);
    acquisition_channel 36/500, workaround 20/500, sentiment 7/500.
- Caveat on cause: v2's prompt added four rules (for reason_not_rebook, unmet_need, post_service_expectation,
  service_outcome), so the churn in other fields is Nemotron's non-determinism plus possible knock-on effects of that
  prompt change; only a repeat run with an identical prompt would isolate pure non-determinism. Either way it comes
  from the model's outputs: every row passed validation in both runs, and the code steps (scrub, filter, sample,
  validation, category overrides, city tier) are deterministic. It is not a pipeline bug.
- Consequence: treat every Part 2 share/percentage as directional, roughly ±5-8 points, not precise. Example: between
  the runs support_unresponsive moved 39.3% -> 31.6%, not_resolved_professional_issue 41.6% -> 31.1% (that one partly
  from the new rule). Report rankings and large gaps, not small differences.

## 2026-11-24 — Part 2 funnel (pipeline/decompose.py) and Stage 4 redefinition
- 4-stage sequential funnel on the 402 metric-eligible reviews in tagged_v2.jsonl (user's framework). Mapping:
  Stage 1 = service_not_delivered or booking_cancelled_by_uc/professional_no_show; Stage 2 = not_resolved_professional_issue;
  resolved_but_recurred passes Stage 2 (user decision; Stage 3 explains it); not_resolved_other/unclear go on to Stage 3;
  Stage 3 = trust_quality_concern/warranty_redo_denied/support_unresponsive/app_friction.
- Stage 4 renamed "leaked to a local/offline provider or competing platform"; same-professional leakage dropped
  (only 2/402 reviews mention it vs 55 for local provider / other app). No review changed stage.
- Result: S1 89 (22.1%), S2 117 (29.1%), S3 138 (34.3%), S4 4 (1.0%), outside 54 (13.4%). S2+S3 = 63% is stable across
  runs; the S2/S3 split depends on the service_outcome rule (S2 39%->29%, S3 24%->34% between v1 and v2).
- Cross-stage leakage (workaround/rebooking_channel = local_offline_provider or other app): S1 4.5%, S2 19.7%, S3 17.4%.
