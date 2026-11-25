"""Research assistant for the dashboard: retrieve relevant tagged reviews, then answer with Groq from that evidence only.

Retrieval is simple keyword/tag overlap (402 reviews is small enough that embeddings aren't needed):
  1. the question's words pick the codebook fields it is about (INTENT_WORDS),
  2. each review scores for informative values in those fields, for tag values named in the question, and for
     word overlap between the question and the review text / evidence quote.
The LLM sees only the top reviews (quote + tags) and summary numbers with their sample sizes, and must cite review ids.
Quoted text in the answer is checked against the evidence afterwards; supporting quotes are shown from the data
itself, never from the model's output.
"""
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "openai/gpt-oss-120b"
TOP_K = 18
UNINFORMATIVE = {"unclear", "not_mentioned", "none", "none_mentioned", "not_applicable", "nothing_specific", ""}

SUGGESTED_QUESTIONS = [
    "What do customers expect once a booking is complete?",
    "How do they decide a home service is needed, and when?",
    "Who or what do they turn to for the same or a different service next time?",
    "Why did they or didn't they book again, in their own words?",
    "What do they do outside the app (local providers, neighbours, family, WhatsApp groups)?",
    "What separates customers who return from those who don't?",
    "How do these patterns differ by category, city, household type, and acquisition channel?",
    "What unmet needs come up consistently?",
]

# question words -> the codebook fields that question is about
INTENT_WORDS = {
    "post_service_expectation": ["expect", "expectation", "expectations", "complete", "completed", "after", "once",
                                 "follow", "warranty", "promise"],
    "decision_trigger": ["decide", "decision", "needed", "trigger", "when", "why book", "planned", "breakdown"],
    "rebooking_channel": ["turn", "next", "rebook", "again", "same", "different", "where", "go", "return"],
    "workaround": ["outside", "local", "neighbour", "neighbours", "neighbor", "family", "whatsapp", "offline",
                   "friends", "groups", "providers"],
    "reason_not_rebook": ["why", "didn", "didnt", "book", "again", "reason", "reasons", "words", "return", "leave",
                          "stop", "pain"],
    "unmet_need": ["unmet", "needs", "consistently", "missing", "want", "wish", "pain"],
    "service_outcome": ["outcome", "resolved", "fixed", "quality", "done"],
    "sentiment": ["return", "separates", "happy", "satisfied", "loyal"],
    "category": ["category", "categories", "service", "differ"],
    "city_signal": ["city", "cities", "metro", "tier"],
    "household_signal": ["household", "kids", "elderly", "tenant", "owner"],
    "acquisition_channel": ["acquisition", "channel", "discover", "found", "referral", "ad", "ads"],
}
# Fields known to be thin in this data; their coverage is always given to the model.
THIN_FIELDS = ["acquisition_channel", "city_signal", "household_signal", "decision_trigger", "rebooking_channel"]
STOP = set("""a an the and or of to in on for with by is are was were be been do does did done how what who whom why when
where which this that these those they them their there it its as at from about into than then so if not no yes can
could would should will just any all each per vs""".split())

SYSTEM_PROMPT = """You are a research assistant for a study of why Urban Company (UC, an Indian home-services app)
customers don't book again within 90 days. You answer ONLY from the EVIDENCE given to you: tagged public reviews
(Play Store, App Store, consumercomplaints.in) and summary numbers computed from all metric-eligible reviews.

Rules:
- Use only the evidence provided. No outside knowledge about UC, its competitors or the market.
- Support your points with 2-4 short VERBATIM quotes from the retrieved reviews: copy the exact words inside double
  quotes, followed by the review id in square brackets, e.g. "the technician did not fix it" [R07]. Never
  paraphrase inside quote marks. Only list in cited_ids the reviews you actually quoted.
- Every percentage must carry its sample in the form "X% (k of n reviews)", e.g. "11% (45 of 402 reviews)". Copy
  numbers exactly as written in the evidence; never compute new percentages, and never read a count as a
  percentage. Don't derive percentages from the handful of retrieved reviews.
- If the evidence doesn't clearly answer the question, say so plainly and explain what is missing. Fields marked
  THIN in the coverage table are mostly unclear/not mentioned: don't draw conclusions from them.
- These are public complaint reviews (about 90% negative), not a representative customer survey; don't present
  shares as shares of all customers.
- Be concise: a direct answer first, then 2-5 short supporting points.

Return ONE JSON object:
{"answer": "<markdown answer>",
 "cited_ids": ["R01", ...],
 "confidence": "clear" | "partial" | "insufficient",
 "data_gaps": "<one or two sentences on what the data can't tell, or empty string>"}"""


# ----------------------------------------------------------------------------- key / client
def groq_key(secrets=None):
    """st.secrets["GROQ_API_KEY"] (Streamlit Cloud) -> GROQ_API_KEY env var -> the project's .env (local runs)."""
    key = None
    if secrets is not None:
        try:
            key = secrets["GROQ_API_KEY"]
        except Exception:
            key = None
    key = key or os.environ.get("GROQ_API_KEY")
    if not key:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("GROQ_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key or None


# ----------------------------------------------------------------------------- retrieval
def _words(s):
    return [w for w in re.findall(r"[a-z]+", (s or "").lower().replace("'", "")) if w not in STOP and len(w) > 2]


def _vals(v):
    return v if isinstance(v, list) else [v]


def intent_fields(question):
    q = " " + " ".join(re.findall(r"[a-z]+", question.lower().replace("'", ""))) + " "
    scores = Counter()
    for field, words in INTENT_WORDS.items():
        for w in words:
            if (" %s " % w) in q or (len(w) > 4 and w in q):
                scores[field] += 1
    return [f for f, _ in scores.most_common(4)] or ["reason_not_rebook", "unmet_need"]


def retrieve(question, reviews, k=TOP_K):
    """Top-k reviews for the question. `reviews`: metric-eligible rows from tagged_v2.jsonl."""
    fields = intent_fields(question)
    qwords = set(_words(question))
    comparative = any(w in question.lower() for w in ("separates", "return from", "differ", "vs", "versus", "compared"))
    scored = []
    for r in reviews:
        t = r["tags"]
        s = 0.0
        for i, f in enumerate(fields):
            informative = [v for v in _vals(t.get(f)) if v not in UNINFORMATIVE]
            if informative:
                s += 3.0 - 0.5 * i
            for v in informative:  # tag value named in the question, e.g. "local" -> local_offline_provider
                if qwords & set(v.split("_")):
                    s += 2.0
        text_words = set(_words(r["text"]))
        quote_words = set(_words(t.get("evidence_quote") or ""))
        s += 0.6 * len(qwords & text_words) + 0.8 * len(qwords & quote_words)
        if t.get("evidence_quote"):
            s += 0.5  # prefer reviews with a citable verbatim quote
        scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    picked = [r for _, r in scored[:k]]
    if comparative:  # make sure the "returning / happy" side is represented, not only complaints
        happy = [r for _, r in scored if r["tags"].get("sentiment") in ("positive", "mixed")
                 or "not_applicable" in _vals(r["tags"].get("reason_not_rebook"))]
        extra = [r for r in happy if r not in picked][: max(0, 6 - sum(1 for r in picked if r in happy))]
        picked = picked[: k - len(extra)] + extra
    return picked, fields


# ----------------------------------------------------------------------------- evidence pack
def _dist(reviews, field, top=8):
    c = Counter()
    for r in reviews:
        c.update(_vals(r["tags"].get(field)))
    n = len(reviews)
    return ", ".join("%s %.0f%% (%d of %d reviews)" % (v, 100.0 * k / n, k, n) for v, k in c.most_common(top))


def _coverage(reviews, field):
    n = len(reviews)
    known = sum(1 for r in reviews if any(v not in UNINFORMATIVE for v in _vals(r["tags"].get(field))))
    return known, n


def build_evidence(question, picked, fields, reviews, summary, stage_of_fn):
    n = len(reviews)
    lines = ["SUMMARY NUMBERS (all %d metric-eligible reviews)" % n]
    sc, sp = summary["stage_counts"], summary["stage_pct"]
    label = lambda s: ("outside_framework (mostly happy customers, price-only or no reason; NOT about leaving the app)"
                       if s == "outside_framework" else s)
    lines.append("Funnel (each review in the first stage it fails): " + "; ".join(
        "%s %.1f%% (%d of %d reviews)" % (label(s), sp[s], sc[s], n) for s in sc))
    lines.append("Stage 2 + Stage 3 combined is stable across runs; the split between them depends on a tagging rule.")
    ov = summary.get("earlier_stage_overlays", {})
    lines.append("Cross-stage: warranty_redo_denied follows a stage-2 failure in %d reviews; support_unresponsive follows a "
                 "stage-1 failure in %d reviews." % (ov.get("stage2_poor_execution+warranty_redo_denied", 0),
                                                     ov.get("stage1_job_not_delivered+support_unresponsive", 0)))
    lk = summary.get("cross_stage_leakage", {})
    if lk:
        lines.append("Leakage within stage (went to local/offline provider or another app): " + "; ".join(
            "%s %d of %d" % (s, v["any_leak"], v["n"]) for s, v in lk.items()))
    for f in dict.fromkeys(fields + ["reason_not_rebook"]):
        lines.append("Distribution of %s (n=%d; multi-value fields can sum past 100%%): %s" % (f, n, _dist(reviews, f)))
    if any(f in fields for f in ("category", "city_signal", "household_signal", "acquisition_channel")):
        by_cat = Counter((r["tags"]["category"], stage_of_fn(r)) for r in reviews)
        cats = Counter(r["tags"]["category"] for r in reviews).most_common(5)
        for cat, cn in cats:
            lines.append("Category %s (n=%d) by stage: %s" % (cat, cn, ", ".join(
                "%s %.0f%% (%d of %d reviews)" % (s.split("_")[0], 100.0 * k / cn, k, cn)
                for (c, s), k in sorted(by_cat.items(), key=lambda kv: -kv[1]) if c == cat)))
    lines.append("COVERAGE (reviews where the field has an informative value, not unclear/not mentioned):")
    for f in dict.fromkeys(THIN_FIELDS + fields):
        known, tot = _coverage(reviews, f)
        lines.append("  %s: %.0f%% (%d of %d reviews)%s" % (f, 100.0 * known / tot, known, tot,
                                                   "  <- THIN" if known < 0.25 * tot else ""))
    lines.append("")
    lines.append("RETRIEVED REVIEWS (the %d most relevant to the question; quote only these)" % len(picked))
    ids = {}
    for i, r in enumerate(picked, 1):
        rid = "R%02d" % i
        ids[rid] = r
        t = r["tags"]
        quote = t.get("evidence_quote") or " ".join(r["text"].split()[:60])
        tag_bits = ["stage=" + stage_of_fn(r)] + ["%s=%s" % (f, "|".join(_vals(t.get(f)))) for f in
                    dict.fromkeys(["category", "service_outcome"] + fields + ["reason_not_rebook", "unmet_need"])]
        lines.append('[%s] (%s, %s) "%s"  tags: %s' % (rid, r["source"], "%s star" % r["rating"] if r.get("rating")
                                                         else "no rating", quote, "; ".join(tag_bits)))
    return "\n".join(lines), ids


# ----------------------------------------------------------------------------- answer
class AssistantBusy(Exception):
    """Groq rate limit hit (free tier). `wait` = suggested seconds, or None if unknown (e.g. daily limit)."""

    def __init__(self, wait=None):
        super().__init__("rate limited")
        self.wait = wait


def _retry_after(err):
    try:
        h = err.response.headers.get("retry-after")
        if h:
            return float(h)
    except Exception:
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(err))
    return (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("’", "'").replace("‘", "'")).strip().lower()


def unverified_quotes(answer, ids):
    """Quoted spans (12+ chars) in the answer that don't appear verbatim in the evidence given to the model."""
    pool = [_norm((r["tags"].get("evidence_quote") or "") + " " + r["text"]) for r in ids.values()]
    spans = re.findall(r'["“]([^"”]{12,})["”]\s*\[R\d{2}\]', answer or "")  # only quotes attributed to a review
    bad = []
    for s in spans:  # a quote shortened with ... / … is fine if every piece is verbatim
        pieces = [_norm(x).strip(" .,;:") for x in re.split(r"\.\.\.|…", s)]
        pieces = [x for x in pieces if len(x) >= 4]
        if not pieces or not all(any(x in p for p in pool) for x in pieces):
            bad.append(s)
    return bad


def ask(question, reviews, summary, stage_of_fn, api_key, model=None):
    from groq import Groq, RateLimitError  # imported here so the rest of the dashboard works without the package

    picked, fields = retrieve(question, reviews)
    evidence, ids = build_evidence(question, picked, fields, reviews, summary, stage_of_fn)
    client = Groq(api_key=api_key, timeout=60.0, max_retries=0)
    kwargs = dict(model=model or DEFAULT_MODEL, temperature=0, max_completion_tokens=3000, reasoning_effort="medium",
                  response_format={"type": "json_object"},
                  messages=[{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": "QUESTION: %s\n\nEVIDENCE:\n%s" % (question, evidence)}])
    try:
        resp = client.chat.completions.create(**kwargs)
    except RateLimitError as e:  # free tier: 8,000 tokens/minute. One short wait, then give up cleanly.
        wait = _retry_after(e)
        if wait is None or wait > 20:
            raise AssistantBusy(wait)
        time.sleep(wait + 0.5)
        try:
            resp = client.chat.completions.create(**kwargs)
        except RateLimitError as e2:
            raise AssistantBusy(_retry_after(e2))
    raw = resp.choices[0].message.content or "{}"
    try:
        out = json.loads(raw)
    except ValueError:
        out = {"answer": raw, "cited_ids": [], "confidence": "partial", "data_gaps": ""}
    inline = list(dict.fromkeys(c for c in re.findall(r"\[(R\d{2})\]", out.get("answer") or "") if c in ids))
    cited = inline or [c for c in out.get("cited_ids") or [] if c in ids][:5]  # prefer the reviews actually quoted
    return {
        "answer": out.get("answer") or "",
        "confidence": out.get("confidence") or "partial",
        "data_gaps": out.get("data_gaps") or "",
        "cited": [(c, ids[c]) for c in cited],
        "retrieved": list(ids.items()),
        "fields": fields,
        "unverified": unverified_quotes(out.get("answer"), ids),
        "tokens": getattr(resp.usage, "total_tokens", None),
        "model": model or DEFAULT_MODEL,
    }
