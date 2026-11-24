"""Score a tagged run against the human-corrected golden set (docs/golden_20.csv).

    python -m pipeline.score_gold                                   # data/processed/tagged_v2.jsonl
    python -m pipeline.score_gold --in data/processed/tagged.jsonl  # e.g. the v1 run

Only gold reviews that are in the tagged file are scored. Multi-value fields match only on the exact same set.
"""
import argparse
import csv
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "docs" / "golden_20.csv"
FIELDS = ["category", "decision_trigger", "acquisition_channel", "post_service_expectation", "service_outcome",
          "rebooking_channel", "reason_not_rebook", "unmet_need", "workaround", "sentiment"]


def _multi():
    cb = yaml.safe_load(open(ROOT / "codebook.yaml", encoding="utf-8"))["fields"]
    return {k: bool(v.get("multi")) for k, v in cb.items()}


def score(tagged_path, gold_path=GOLD):
    multi = _multi()
    norm = lambda f, v: (frozenset(x.strip() for x in (v if isinstance(v, list) else str(v).split("|")) if x.strip())
                         if multi[f] else str(v).strip())
    gold = list(csv.DictReader(open(gold_path, encoding="utf-8-sig")))
    tagged = {(r["source"], str(r["id"])): r for r in map(json.loads, open(tagged_path, encoding="utf-8"))}
    pairs = [(g, tagged[(g["source"], g["source_id"])]) for g in gold if (g["source"], g["source_id"]) in tagged]
    per_field, wrong_rows = {}, {}
    for f in FIELDS + ["metric_eligible"]:
        ok = 0
        for g, t in pairs:
            hit = (str(t["metric_eligible"]).upper() == g[f].strip().upper() if f == "metric_eligible"
                   else norm(f, t["tags"][f]) == norm(f, g[f]))
            ok += hit
            if not hit:
                wrong_rows.setdefault(g["gold_id"], []).append(f)
        per_field[f] = {"match": ok, "n": len(pairs), "pct": round(100.0 * ok / len(pairs), 1) if pairs else None}
    return {"n": len(pairs), "per_field": per_field, "rows_all_match": len(pairs) - len(wrong_rows),
            "mismatches": wrong_rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="path", default=str(ROOT / "data" / "processed" / "tagged_v2.jsonl"))
    print(json.dumps(score(ap.parse_args().path), indent=2))
