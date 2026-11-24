"""Relevance filter: data/processed/clean.jsonl -> items ready for tag.py.

    python -m pipeline.filter

Reads only clean.jsonl, which is already PII-scrubbed (see pipeline/clean.py).

Routing
- Sources in config `pipeline.untagged_sources` (shareholder letters / call transcripts) are
  management statements, not customer voice. They go to business_context.jsonl, unchanged
  apart from a `context_terms` list to help find quotes. They're never tagged.

Customer-voice items are dropped, with a recorded reason, if they:
  too_short          have fewer than filter.min_words words
  partner_voice      are written by a UC professional about their own work (partner apps are out of scope)
  no_uc_mention      come from a source in require_uc_mention_sources and don't mention UC
  no_keyword_match   come from a source in require_keyword_sources (Play/App Store, complaints: raw holds
                     every fetched review) and match no config keyword
  not_relevant       (other sources) match no keyword AND have fewer than min_experience_signals service terms
  near_duplicate     match an earlier-dated kept item at rapidfuzz.ratio >= near_duplicate_ratio
                     (the same person re-posting); `duplicate_of` records the kept id

Near-duplicate detection is blocked with MinHash LSH so it scales (60k+ reviews):
  - shingles = character 5-grams of the lowercased alnum text (matches char-level ratio; works on short reviews)
  - 126 MinHash values, split into 63 bands x 2 rows; texts sharing any band bucket become candidates
  - only candidates are compared with rapidfuzz.ratio, and the earliest kept match wins (as in the all-pairs version)
  - buckets span all sources, so the same person posting on two sites is still caught
  - not keyed on the text prefix: consumercomplaints.in adds "[Resolved]" to titles, which
    splits real duplicates (the first one found had prefixes "urban co" vs "resolved")
  `--check-dedupe` also runs the old all-pairs method and fails if the results differ.

Outputs (data/processed/):
  filtered.jsonl          kept customer-voice items -> input to tag.py
  filtered_out.jsonl      dropped items + `filter_reason` (audit trail; already PII-scrubbed)
  business_context.jsonl  untagged qualitative sources
  filter_log.json         counts per source and per reason

product_purchase items are NOT dropped here. They're tagged and excluded later, in the metric
decomposition (config metric.exclude_categories).
"""
import argparse
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
from rapidfuzz import fuzz

from collect.common import kw_settings, load_config, match_keywords
from pipeline.clean import PROCESSED_DIR

IN = PROCESSED_DIR / "clean.jsonl"
OUT = PROCESSED_DIR / "filtered.jsonl"
OUT_DROPPED = PROCESSED_DIR / "filtered_out.jsonl"
OUT_CONTEXT = PROCESSED_DIR / "business_context.jsonl"
LOG = PROCESSED_DIR / "filter_log.json"
CONTEXT_TERMS = re.compile(r"(?i)\b(repeat|retention|retain|cohort|rebook\w*|frequency|loyal\w*|"
                           r"subscription|plus member\w*|churn|lifetime|ltv|wallet share|habit\w*)\b")


def _signals(text: str, terms: List[str]) -> List[str]:
    t = text.lower()
    return [s for s in terms if re.search(r"\b%s\b" % re.escape(s.lower()), t)]


SHINGLE = 5
BANDS, ROWS = 63, 2
_rng = np.random.RandomState(20261122)
# 64-bit multiply-add hashing with natural uint64 wrap-around; multipliers must be odd.
# (A first version used (a*h+b) mod 2^61-1 with 31-bit a and 32-bit crc32 h. The product barely wrapped,
#  so all 126 "independent" hashes picked nearly the same minimum, agreement was ~0 or ~1 instead of ~Jaccard,
#  and LSH missed ~16% of duplicates in the stress test.)
_A = (_rng.randint(0, 1 << 62, size=BANDS * ROWS, dtype=np.int64).astype(np.uint64) << np.uint64(1)) | np.uint64(1)
_B = _rng.randint(0, 1 << 62, size=BANDS * ROWS, dtype=np.int64).astype(np.uint64) << np.uint64(2)


def _gram_hash(g: str) -> int:
    return int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")


def _minhash(text: str) -> np.ndarray:
    t = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    grams = {t[i:i + SHINGLE] for i in range(max(1, len(t) - SHINGLE + 1))}
    h = np.fromiter((_gram_hash(g) for g in grams), dtype=np.uint64, count=len(grams))
    return (h[:, None] * _A[None, :] + _B[None, :]).min(axis=0)  # wraps mod 2^64 by design


def _bucket_keys(sig: np.ndarray) -> List[Tuple[int, bytes]]:
    return [(b, sig[b * ROWS:(b + 1) * ROWS].tobytes()) for b in range(BANDS)]


def near_duplicates_lsh(items: List[Dict], thr: float) -> Tuple[List[Dict], List[Tuple[Dict, Dict]], int]:
    """items must be sorted earliest-first. Returns (kept, [(dropped, kept_match)], rapidfuzz_calls)."""
    kept, dups, index, calls = [], [], defaultdict(list), 0
    for r in items:
        low = r["text"].lower()
        keys = _bucket_keys(_minhash(r["text"]))
        cand = sorted({ki for k in keys for ki in index.get(k, ())})
        match = None
        for ki in cand:  # ascending kept order -> earliest kept match, same as all-pairs
            calls += 1
            if fuzz.ratio(low, kept[ki]["text"].lower()) >= thr:
                match = kept[ki]
                break
        if match is not None:
            dups.append((r, match))
            continue
        for k in keys:
            index[k].append(len(kept))
        kept.append(r)
    return kept, dups, calls


def near_duplicates_allpairs(items: List[Dict], thr: float) -> Tuple[List[Dict], List[Tuple[Dict, Dict]], int]:
    """Reference O(n^2) method (the original). Used only by --check-dedupe."""
    kept, dups, calls = [], [], 0
    for r in items:
        low = r["text"].lower()
        match = None
        for k in kept:
            calls += 1
            if fuzz.ratio(low, k["text"].lower()) >= thr:
                match = k
                break
        if match is not None:
            dups.append((r, match))
        else:
            kept.append(r)
    return kept, dups, calls


def _date_key(r: Dict):
    return (r.get("date") is None, str(r.get("date") or ""))


def classify(r: Dict, cfg: dict, fc: dict, partner_rx, uc_rx) -> Optional[str]:
    text = r["text"]
    if len(text.split()) < int(fc.get("min_words", 5)):
        return "too_short"
    if any(rx.search(text) for rx in partner_rx):
        return "partner_voice"
    if r["source"] in fc.get("require_uc_mention_sources", []) and not uc_rx.search(text):
        return "no_uc_mention"
    kws, thr, fmin, kvar = kw_settings(cfg)
    kw = match_keywords(text, kws, thr, fmin, kvar)
    sig = _signals(text, fc.get("experience_signals", []))
    r["matched_keywords"] = kw
    groups = cfg.get("keyword_to_group", {})
    r["keyword_groups"] = sorted({groups.get(k, "other") for k in kw})
    r["experience_signals"] = sig
    if r["source"] in fc.get("require_keyword_sources", []):
        if not kw:
            return "no_keyword_match"
    elif not kw and len(sig) < int(fc.get("min_experience_signals", 2)):
        return "not_relevant"
    return None


def run(check_dedupe: bool = False) -> Dict:
    cfg = load_config()
    fc = cfg.get("filter", {})
    untagged = set((cfg.get("pipeline") or {}).get("untagged_sources", []))
    partner_rx = [re.compile(p, re.I) for p in fc.get("partner_voice_patterns", [])]
    uc_rx = re.compile(fc.get("uc_mention_pattern", r"\burban ?company\b"), re.I)
    dup_thr = float(fc.get("near_duplicate_ratio", 90))

    rows = [json.loads(l) for l in open(IN, encoding="utf-8")]
    stats = defaultdict(Counter)
    kept, dropped, context = [], [], []

    candidates = []
    for r in rows:
        st = stats[r["source"]]
        st["input"] += 1
        if r["source"] in untagged:
            r["context_terms"] = sorted({m.lower() for m in CONTEXT_TERMS.findall(r["text"])})
            context.append(r)
            st["routed_business_context"] += 1
            continue
        reason = classify(r, cfg, fc, partner_rx, uc_rx)
        if reason:
            r["filter_reason"] = reason
            dropped.append(r)
            st["dropped_%s" % reason] += 1
        else:
            candidates.append(r)

    # near-duplicates: earliest-dated copy wins (undated items sort last)
    candidates.sort(key=_date_key)
    t0 = time.time()
    kept, dups, calls = near_duplicates_lsh(candidates, dup_thr)
    dedupe = {"method": "minhash_lsh", "items": len(candidates), "rapidfuzz_calls": calls,
              "seconds": round(time.time() - t0, 3)}
    if check_dedupe:
        t0 = time.time()
        ref_kept, ref_dups, ref_calls = near_duplicates_allpairs(candidates, dup_thr)
        same = ([id(r) for r in kept] == [id(r) for r in ref_kept]
                and [(id(a), id(b)) for a, b in dups] == [(id(a), id(b)) for a, b in ref_dups])
        dedupe["check_allpairs"] = {"identical": same, "rapidfuzz_calls": ref_calls,
                                    "seconds": round(time.time() - t0, 3), "dropped": len(ref_dups)}
        if not same:
            raise RuntimeError("LSH near-duplicate result differs from all-pairs reference: %s" % dedupe)
    for r, match in dups:
        r["filter_reason"] = "near_duplicate"
        r["duplicate_of"] = "%s:%s" % (match["source"], match["id"])
        dropped.append(r)
        stats[r["source"]]["dropped_near_duplicate"] += 1
    for r in kept:
        stats[r["source"]]["kept"] += 1

    for path, data in ((OUT, kept), (OUT_DROPPED, dropped), (OUT_CONTEXT, context)):
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in data:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        tmp.replace(path)

    log = {"kept_for_tagging": len(kept), "dropped": len(dropped), "business_context": len(context),
           "drop_reasons": dict(Counter(r["filter_reason"] for r in dropped)),
           "near_duplicate_check": dedupe,
           "per_source": {k: dict(v) for k, v in stats.items()}}
    LOG.write_text(json.dumps(log, indent=2))
    return log


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-dedupe", action="store_true",
                    help="also run the O(n^2) all-pairs dedupe and fail if results differ")
    print(json.dumps(run(check_dedupe=ap.parse_args().check_dedupe), indent=2))
