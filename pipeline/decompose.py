"""Part 2: decompose the 90-day repeat-booking problem into a 4-stage funnel, from the tagged reviews.

    python -m pipeline.decompose                                   # data/processed/tagged_v2.jsonl
    python -m pipeline.decompose --in data/processed/tagged.jsonl  # e.g. the v1 run, to check stability

Each metric-eligible review is assigned to the FIRST gate it fails (sequential funnel):
  Stage 1  did the job happen at all        -> operational reliability (staffing/ops)
  Stage 2  if it happened, was it done well -> quality of execution (vetting/QA, supply side)
  Stage 3  did something else sour trust    -> customer-side lever (warranty, support, app friction)
  Stage 4  leaked to a local/offline provider or competing platform (same-professional leakage dropped 2026-11-24:
           only 2/402 reviews support it, vs 56 for local provider / other app)
  outside  none of the above (price only, happy/not_applicable, no reason given)
It also reports a cross-stage leakage view (for Stages 1-3: share that ALSO went to a local/offline provider or
a competing platform) and, for reviews stopped at Stage 1/2, how many ALSO carry a Stage-3 / Stage-4 signal (e.g. a
professional failure followed by a denied warranty redo), since those levers can recover earlier-stage failures.

Caveats (see docs/evidence_log.md): these are shares of PUBLIC COMPLAINT VOICE, not of churned customers;
tags are directional (±5-8 points, Nemotron run-to-run noise); gold-set agreement is 69-85% on the fields used.
"""
import argparse
import json
from collections import Counter

from pathlib import Path

# Defined here rather than imported from pipeline.clean: the dashboard imports this module, and pipeline.clean pulls
# in the collection/cleaning dependencies (langdetect, requests, rapidfuzz), which the dashboard doesn't install.
ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = ROOT / "data" / "processed"

# --- stage rules (codebook values) -------------------------------------------------------------------------
S1_OUTCOMES = {"service_not_delivered"}
S1_REASONS = {"booking_cancelled_by_uc", "professional_no_show"}
S2_OUTCOMES = {"not_resolved_professional_issue"}
# Passed Stage 2 per the framework: resolved_as_expected, resolved_but_recurred. Not attributed to the professional,
# so they also go on to Stage 3: not_resolved_other, partially_resolved, cancelled_over_quote_dispute, unclear.
S3_REASONS = {"trust_quality_concern", "warranty_redo_denied", "support_unresponsive", "app_friction"}
LOCAL_CHANNELS, PLATFORM_CHANNELS = {"local_offline_provider"}, {"used_different_platform"}
S4_CHANNELS = LOCAL_CHANNELS | PLATFORM_CHANNELS
S4_REASONS = {"switched_to_local_provider", "switched_to_competitor_app"}
LOCAL_WORKAROUNDS, PLATFORM_WORKAROUNDS = {"local_offline_provider"}, {"different_app_platform"}
S4_WORKAROUNDS = LOCAL_WORKAROUNDS | PLATFORM_WORKAROUNDS
LOCAL_REASONS, PLATFORM_REASONS = {"switched_to_local_provider"}, {"switched_to_competitor_app"}

STAGES = ["stage1_job_not_delivered", "stage2_poor_execution", "stage3_trust_soured", "stage4_leaked_local_or_competing_platform",
          "outside_framework"]


def _set(v):
    return set(v) if isinstance(v, list) else {v}


def signals(t):
    reasons, work = _set(t["reason_not_rebook"]), _set(t["workaround"])
    return {
        "s1": t["service_outcome"] in S1_OUTCOMES or bool(reasons & S1_REASONS),
        "s2": t["service_outcome"] in S2_OUTCOMES,
        "s3": bool(reasons & S3_REASONS),
        "s4": t["rebooking_channel"] in S4_CHANNELS or bool(reasons & S4_REASONS) or bool(work & S4_WORKAROUNDS),
        "local": t["rebooking_channel"] in LOCAL_CHANNELS or bool(reasons & LOCAL_REASONS) or bool(work & LOCAL_WORKAROUNDS),
        "platform": t["rebooking_channel"] in PLATFORM_CHANNELS or bool(reasons & PLATFORM_REASONS) or bool(work & PLATFORM_WORKAROUNDS),
    }


def stage_of(sig):
    for k, name in zip(("s1", "s2", "s3", "s4"), STAGES):
        if sig[k]:
            return name
    return STAGES[-1]


def _rel(path):
    """Path relative to the project root, so outputs don't record a machine-specific absolute path."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return p.name


def run(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    elig = [r for r in rows if r.get("metric_eligible")]
    n = len(elig)
    pct = lambda k: round(100.0 * k / n, 1) if n else 0.0
    leak_by_stage = {s: Counter() for s in STAGES[:3]}
    stage, overlay, s3_reasons, s4_where, outside_why, s2_also = Counter(), Counter(), {}, Counter(), Counter(), Counter()
    for r in elig:
        t = r["tags"]
        sig = signals(t)
        st = stage_of(sig)
        stage[st] += 1
        reasons = _set(t["reason_not_rebook"])
        if st in leak_by_stage:
            lb = leak_by_stage[st]
            lb["n"] += 1
            lb["any_leak"] += sig["s4"]
            lb["local_offline"] += sig["local"]
            lb["competing_platform"] += sig["platform"]
            lb["both"] += sig["local"] and sig["platform"]
        if st in STAGES[:2]:
            if sig["s3"]:
                overlay[st + "+trust_soured"] += 1
                for x in reasons & S3_REASONS:
                    overlay["%s+%s" % (st, x)] += 1
            if sig["s4"]:
                overlay[st + "+leaked"] += 1
        if st == "stage3_trust_soured":
            s3_reasons.setdefault("primary_reason_mentions", Counter()).update(reasons & S3_REASONS)
            s3_reasons.setdefault("outcome", Counter())[t["service_outcome"]] += 1
        if sig["s4"]:
            s4_where.update({"channel:" + t["rebooking_channel"]} if t["rebooking_channel"] in S4_CHANNELS else set())
            s4_where.update("reason:" + x for x in reasons & S4_REASONS)
            s4_where.update("workaround:" + x for x in _set(t["workaround"]) & S4_WORKAROUNDS)
            s4_where["any_leak_signal"] += 1
        if st == "outside_framework":
            outside_why.update(reasons)
        if t["service_outcome"] == "resolved_but_recurred":
            s2_also["resolved_but_recurred(passed_stage2_by_rule)"] += 1
    out = {
        "input": _rel(path), "metric_eligible": n,
        "stage_counts": {s: stage[s] for s in STAGES},
        "stage_pct": {s: pct(stage[s]) for s in STAGES},
        "cross_stage_leakage": {s: dict(v, **{"pct_any_leak": round(100.0 * v["any_leak"] / v["n"], 1) if v["n"] else 0.0,
                                             "pct_local_offline": round(100.0 * v["local_offline"] / v["n"], 1) if v["n"] else 0.0,
                                             "pct_competing_platform": round(100.0 * v["competing_platform"] / v["n"], 1) if v["n"] else 0.0})
                                for s, v in leak_by_stage.items()},
        "earlier_stage_overlays": dict(sorted(overlay.items())),
        "stage3_detail": {k: dict(v.most_common()) for k, v in s3_reasons.items()},
        "leak_signals_all_stages": dict(s4_where.most_common()),
        "outside_framework_reasons": dict(outside_why.most_common()),
        "notes": dict(s2_also),
    }
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="path", default=str(PROCESSED_DIR / "tagged_v2.jsonl"))
    ap.add_argument("--out", default=str(PROCESSED_DIR / "decomposition.json"))
    a = ap.parse_args()
    res = run(a.path)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    print(json.dumps(res, indent=2))
