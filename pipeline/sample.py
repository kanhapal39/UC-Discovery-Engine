"""Stratified sample of the filtered set, to tag fewer items.

    python -m pipeline.sample            # -> data/processed/sample.jsonl (size from config sample.size)

Design (config `sample`):
  - sources in keep_all_sources (small ones: consumer_complaints, app_store) are kept WHOLE
  - the rest is a random sample proportional to strata = (source, star rating, keyword group), with
    largest-remainder rounding so the total is exactly `size`; seeded, so it's reproducible
  - deliberately NOT built around the already-tagged items: the first full run went oldest-first, so the
    tagged items are date-biased. Tagged items that fall into the sample are simply reused from the cache.
"""
import json
import random
from collections import Counter, defaultdict

from collect.common import load_config
from pipeline.clean import PROCESSED_DIR

IN = PROCESSED_DIR / "filtered.jsonl"
OUT = PROCESSED_DIR / "sample.jsonl"
LOG = PROCESSED_DIR / "sample_log.json"


def _group(r):
    g = set(r.get("keyword_groups") or [])
    return "loyalty" if "loyalty" in g else "switching" if "switching" in g else "original"


def run():
    cfg = (load_config().get("sample") or {})
    size, seed = int(cfg.get("size", 500)), int(cfg.get("seed", 20261122))
    keep_all = set(cfg.get("keep_all_sources", []))
    rows = [json.loads(l) for l in open(IN, encoding="utf-8")]
    fixed = [r for r in rows if r["source"] in keep_all]
    pool = [r for r in rows if r["source"] not in keep_all]
    n = size - len(fixed)
    if n < 0:
        raise SystemExit("keep_all_sources alone exceed sample size")
    strata = defaultdict(list)
    for r in pool:
        strata[(r["source"], r.get("rating"), _group(r))].append(r)
    quotas = {k: n * len(v) / len(pool) for k, v in strata.items()}
    alloc = {k: int(q) for k, q in quotas.items()}
    for k in sorted(quotas, key=lambda k: quotas[k] - alloc[k], reverse=True)[: n - sum(alloc.values())]:
        alloc[k] += 1
    rng = random.Random(seed)
    picked = list(fixed)
    for k in sorted(strata, key=str):
        picked += rng.sample(strata[k], alloc[k])
    rng.shuffle(picked)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in picked:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    share = lambda rs, key: {str(k): round(v / len(rs), 3) for k, v in sorted(Counter(key(r) for r in rs).items(), key=str)}
    log = {"size": len(picked), "seed": seed, "kept_whole": dict(Counter(r["source"] for r in fixed)),
           "by_source": dict(Counter(r["source"] for r in picked)),
           "rating_share_sample": share([r for r in picked if r["source"] not in keep_all], lambda r: r.get("rating")),
           "rating_share_full": share(pool, lambda r: r.get("rating")),
           "group_share_sample": share(picked, _group), "group_share_full": share(rows, _group)}
    LOG.write_text(json.dumps(log, indent=2))
    return log


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
