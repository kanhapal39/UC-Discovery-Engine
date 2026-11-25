"""Read-only explorer for the discovery engine's results. Five pages as tabs across the top.

    streamlit run dashboard/app.py

Reads data/processed/tagged_v2.jsonl (and tagged.jsonl for the run-to-run comparison), docs/golden_20.csv,
pipeline/decompose.py (funnel rules) and dashboard/content.yaml (wording). Never writes anything.
The Overview's research assistant (dashboard/assistant.py) sends questions + retrieved evidence to Groq; it needs
GROQ_API_KEY in Streamlit secrets, the environment, or the local .env.
"""
import json
import sys
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.decompose import STAGES, run as decompose, signals, stage_of  # noqa: E402
from pipeline.score_gold import score as score_gold  # noqa: E402
from dashboard import assistant  # noqa: E402

PROC = ROOT / "data" / "processed"
TAGGED, TAGGED_V1 = PROC / "tagged_v2.jsonl", PROC / "tagged.jsonl"
EVIDENCE_LOG = ROOT / "docs" / "evidence_log.md"
ACCENT, MUTED = "#1F4E79", "#9AA5B1"

STAGE_LABEL = {
    "stage1_job_not_delivered": "1. Job didn't happen",
    "stage2_poor_execution": "2. Done poorly (professional)",
    "stage3_trust_soured": "3. Done OK, trust soured",
    "stage4_leaked_local_or_competing_platform": "4. Leaked to local provider / other app",
    "outside_framework": "Outside the framework",
}
# Vetted deck quotes (review id prefixes), as reported for Part 2 on 2026-11-24.
DECK_QUOTES = {
    "stage1_job_not_delivered": ["be421486", "f51912e9", "5fd1bdda"],
    "stage2_poor_execution": ["9ba5a3b9", "03d9bb36", "21f21c0b"],
    "stage3_trust_soured": ["46a6afb0", "4d155bcf", "8a7fce72"],
    "stage4_leaked_local_or_competing_platform": ["2622418", "d31c9771", "d0944f86", "10a31920"],
}
TAG_FIELDS = ["category", "decision_trigger", "acquisition_channel", "post_service_expectation", "service_outcome",
              "rebooking_channel", "reason_not_rebook", "unmet_need", "in_app_pain", "workaround", "household_signal",
              "city_signal", "sentiment", "platform"]
SAME_PRO = {"contacted_same_professional_directly", "direct_whatsapp_same_professional"}

st.set_page_config(page_title="UC Discovery Engine", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""
<style>
  [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {display: none;}
  .block-container {padding-top: 4.75rem; max-width: 1200px;}  /* clears Streamlit's fixed ~3.75rem top toolbar */
  .stTabs [data-baseweb="tab-list"] {gap: 1.75rem; border-bottom: 1px solid #E3E6E8;}
  .stTabs [data-baseweb="tab"] {font-size: 1rem; padding: 0.6rem 0;}
  .kicker {color: #5F6B76; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.2rem;}
  .big {font-size: 2.1rem; font-weight: 650; line-height: 1.1; color: #111;}
  .big.accent {color: #1F4E79;}
  .sub {color: #3D4852; font-size: 0.9rem; margin-top: 0.3rem;}
  .quote {color: #3D4852; font-size: 0.85rem; font-style: italic; margin-top: 0.4rem;}
  .card-title {font-weight: 600; margin-bottom: 0.25rem;}
  .ra-box {border: 1px solid #E3E6E8; border-radius: 10px; padding: 1.1rem 1.2rem 0.4rem 1.2rem; margin-bottom: 1.2rem;}
  div[data-testid="stButton"] button[kind="secondary"] {border-radius: 999px; font-size: 0.85rem; padding: 0.2rem 0.8rem;
      border-color: #D5DADF; color: #222; text-align: left; white-space: normal; height: auto; min-height: 2.2rem;}
  .ra-meta {color: #5F6B76; font-size: 0.8rem;}
  blockquote {border-left: 3px solid #1F4E79 !important; color: #222;}
</style>""", unsafe_allow_html=True)


def _fmt(v):
    return ", ".join(v) if isinstance(v, list) else ("" if v is None else str(v))


def _as_list(v):
    return v if isinstance(v, list) else [v]


@st.cache_data
def load():
    rows = [json.loads(l) for l in open(TAGGED, encoding="utf-8")]
    elig = [r for r in rows if r.get("metric_eligible")]
    recs = []
    for r in elig:
        t = r["tags"]
        sig = signals(t)
        rec = {"id": str(r["id"]), "stage": stage_of(sig), "source": r["source"], "rating": r.get("rating"),
               "date": (r.get("date") or "")[:10], "leaked_local": sig["local"], "leaked_other_app": sig["platform"],
               "evidence_quote": t.get("evidence_quote") or "", "text": r["text"]}
        rec.update({f: _fmt(t.get(f)) for f in TAG_FIELDS})
        recs.append(rec)
    df = pd.DataFrame(recs)
    df["stage_label"] = df["stage"].map(STAGE_LABEL)
    multi_vals = {f: [_as_list(r["tags"].get(f)) for r in elig] for f in TAG_FIELDS}
    return df, {str(r["id"]): r for r in elig}, multi_vals, decompose(TAGGED)


@st.cache_data
def run_noise():
    if not TAGGED_V1.exists():
        return None
    a = {str(x["id"]): x for x in map(json.loads, open(TAGGED_V1, encoding="utf-8"))}
    b = {str(x["id"]): x for x in map(json.loads, open(TAGGED, encoding="utf-8"))}
    ids = [i for i in b if i in a]
    norm = lambda v: tuple(sorted(v)) if isinstance(v, list) else v
    out = [{"field": "metric_eligible", "reviews changed": sum(a[i]["metric_eligible"] != b[i]["metric_eligible"] for i in ids)}]
    for f in ["category", "decision_trigger", "acquisition_channel", "rebooking_channel", "workaround", "sentiment"]:
        out.append({"field": f, "reviews changed": sum(norm(a[i]["tags"][f]) != norm(b[i]["tags"][f]) for i in ids)})
    for o in out:
        o["of"] = len(ids)
        o["share %"] = round(100.0 * o["reviews changed"] / len(ids), 1)
    return pd.DataFrame(out)


@st.cache_data
def gold_scores():
    new = score_gold(TAGGED)
    old = score_gold(TAGGED_V1) if TAGGED_V1.exists() else None
    rows = []
    for f, v in new["per_field"].items():
        o = old["per_field"][f] if old else None
        rows.append({"field": f, "old run": "%d/%d (%.0f%%)" % (o["match"], o["n"], o["pct"]) if o else "",
                     "new run (v2)": "%d/%d (%.0f%%)" % (v["match"], v["n"], v["pct"]),
                     "change (pts)": round(v["pct"] - o["pct"]) if o else None})
    rows.append({"field": "whole rows fully correct",
                 "old run": "%d/%d" % (old["rows_all_match"], old["n"]) if old else "",
                 "new run (v2)": "%d/%d" % (new["rows_all_match"], new["n"]), "change (pts)": None})
    return pd.DataFrame(rows), new["n"]


def hbar(counts, title=None, height=None, pct_of=None):
    """Horizontal bar chart in the single accent colour, sorted, with value labels."""
    d = pd.DataFrame({"label": list(counts.keys()), "value": list(counts.values())})
    if pct_of:
        d["text"] = d["value"].map(lambda v: "%d  (%.1f%%)" % (v, 100.0 * v / pct_of))
    else:
        d["text"] = d["value"].astype(str)
    order = list(d.sort_values("value", ascending=False)["label"])
    base = alt.Chart(d).encode(y=alt.Y("label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320)),
                               x=alt.X("value:Q", title=None, axis=alt.Axis(grid=False, labels=False, ticks=False)))
    ch = base.mark_bar(color=ACCENT, cornerRadiusEnd=2) + base.mark_text(align="left", dx=4, color="#111").encode(text="text:N")
    ch = ch.properties(height=height or max(80, 30 * len(d)), title=title or "")
    return ch.configure_view(strokeWidth=0).configure_axis(domain=False, labelColor="#333").configure_title(
        anchor="start", fontSize=14, fontWeight=600, color="#111")


def counter_of(lists):
    c = Counter()
    for vals in lists:
        c.update(vals)
    return dict(c)


def card(title, body):
    with st.container(border=True):
        st.markdown('<div class="card-title">%s</div>%s' % (title, body), unsafe_allow_html=True)


df, by_id, multi_vals, summary = load()
content = yaml.safe_load(open(Path(__file__).parent / "content.yaml", encoding="utf-8"))
n = summary["metric_eligible"]
sc, sp, ov = summary["stage_counts"], summary["stage_pct"], summary["earlier_stage_overlays"]
s23 = sc[STAGES[1]] + sc[STAGES[2]]
reasons = counter_of(multi_vals["reason_not_rebook"])
noise = run_noise()
gold_table, gold_n = gold_scores()
V = {  # live numbers for content.yaml placeholders
    "s1": sc[STAGES[0]], "s1_pct": sp[STAGES[0]], "s23": s23, "s23_pct": round(100.0 * s23 / n, 1), "s4": sc[STAGES[3]],
    "warranty": reasons.get("warranty_redo_denied", 0), "support": reasons.get("support_unresponsive", 0),
    "warranty_s2": ov.get("stage2_poor_execution+warranty_redo_denied", 0),
    "warranty_s1": ov.get("stage1_job_not_delivered+warranty_redo_denied", 0),
    "support_s1": ov.get("stage1_job_not_delivered+support_unresponsive", 0),
    "support_s2": ov.get("stage2_poor_execution+support_unresponsive", 0),
    "leak": summary["leak_signals_all_stages"].get("any_leak_signal", 0),
    "same_pro": int(sum(1 for i in range(n) if df["rebooking_channel"].iat[i] in SAME_PRO
                        or set(multi_vals["workaround"][i]) & SAME_PRO)),
    "neg_pct": round(100.0 * (df["sentiment"] == "negative").mean()),
    "play_pct": round(100.0 * (df["source"] == "play_store").mean()),
    "chan_unclear_pct": round(100.0 * (df["rebooking_channel"] == "unclear").mean()),
    "flips": (int(noise.loc[noise["field"] == "metric_eligible", "reviews changed"].iat[0]) if noise is not None
              else content.get("recorded_metric_eligible_flips", "?")),
    "gold_n": gold_n,
}
fill = lambda s: s.format(**V)

st.markdown('<div class="kicker">Urban Company · 90-day repeat booking</div>', unsafe_allow_html=True)
st.title("Discovery Engine", anchor=False)
st.caption("Read-only · %d metric-eligible reviews from public sources, tagged by Nemotron · %s" % (n, content["status_note"]))

p1, p2, p3, p4, p5 = st.tabs(["Overview", "User Behaviour", "Product Opportunities", "Evidence Explorer",
                              "Data Quality & Validation"])

# ============================================================================ 1. Overview
with p1:
    # ---- research assistant -------------------------------------------------------------------------------
    elig_rows = list(by_id.values())
    stage_fn = lambda r: stage_of(signals(r["tags"]))

    @st.cache_data(show_spinner=False, ttl=3600)
    def cached_answer(question):  # same question within an hour reuses the answer (saves Groq tokens)
        out = assistant.ask(question, elig_rows, summary, stage_fn, assistant.groq_key(st.secrets))
        out["cited"] = [(c, r["id"]) for c, r in out["cited"]]
        out["retrieved"] = [(c, r["id"]) for c, r in out["retrieved"]]
        return out

    def _use_chip(q):
        st.session_state["ra_input"] = q
        st.session_state["ra_go"] = True

    st.markdown('<div class="kicker">Research assistant</div>', unsafe_allow_html=True)
    with st.container(border=True):
        qcol, bcol = st.columns([5, 1.4], vertical_alignment="bottom")
        question = qcol.text_input("Question", key="ra_input", label_visibility="collapsed",
                                   placeholder="Ask anything about UC customer behaviour, expectations, or pain points...")
        if bcol.button("Ask the research assistant", type="primary", use_container_width=True):
            st.session_state["ra_go"] = True
        chip_cols = st.columns(4)
        for i, q in enumerate(assistant.SUGGESTED_QUESTIONS):
            chip_cols[i % 4].button(q, key="chip%d" % i, on_click=_use_chip, args=(q,), use_container_width=True)
        st.markdown('<div class="ra-meta">Answers come only from the %d tagged reviews and their summary numbers, '
                    'via Groq (%s). Quotes below each answer are shown straight from the data.</div>'
                    % (n, assistant.DEFAULT_MODEL), unsafe_allow_html=True)

    if st.session_state.pop("ra_go", False) and (question or "").strip():
        st.session_state["ra_last"] = question.strip()
    asked = st.session_state.get("ra_last")
    if asked:
        if not assistant.groq_key(st.secrets):
            st.warning("The research assistant needs a GROQ_API_KEY (Streamlit secrets, environment, or .env). "
                       "Everything else on the dashboard works without it.")
        else:
            try:
                with st.spinner("Reading the evidence..."):
                    res = cached_answer(asked)
            except assistant.AssistantBusy as e:
                st.warning("The assistant is busy (Groq free-tier rate limit). %s" % (
                    "Try again in about %d seconds." % (e.wait + 1) if e.wait else "Try again in a little while."))
                res = None
            except Exception as e:  # network / API errors: say so, don't crash the page
                st.error("The research assistant couldn't answer (%s). The rest of the dashboard is unaffected."
                         % type(e).__name__)
                res = None
            if res:
                with st.container(border=True):
                    st.markdown('<div class="kicker">Question</div>**%s**' % asked, unsafe_allow_html=True)
                    conf = {"clear": "Clear answer from the data", "partial": "Partial: the data only partly answers this",
                            "insufficient": "The data doesn't clearly answer this"}.get(res["confidence"], res["confidence"])
                    st.markdown('<div class="ra-meta">%s · based on the %d most relevant reviews · fields: %s</div>'
                                % (conf, len(res["retrieved"]), ", ".join(res["fields"])), unsafe_allow_html=True)
                    st.markdown(res["answer"])
                    if res["data_gaps"]:
                        st.info("**What the data can't tell:** " + res["data_gaps"])
                    if res["unverified"]:
                        st.warning("Couldn't match these quoted words exactly in the evidence, so treat them as "
                                   "paraphrase: " + "; ".join('"%s"' % u for u in res["unverified"]))
                st.markdown('<div class="kicker">Supporting quotes</div>', unsafe_allow_html=True)
                shown_ids = res["cited"] or res["retrieved"][:5]
                if not res["cited"]:
                    st.caption("The answer didn't quote specific reviews; these are the most relevant ones retrieved.")
                qcols = st.columns(2)
                for j, (cid, rid) in enumerate(shown_ids):
                    r = by_id[rid]
                    t = r["tags"]
                    with qcols[j % 2], st.container(border=True):
                        st.markdown("> %s" % (t.get("evidence_quote") or " ".join(r["text"].split()[:60]) + "..."))
                        st.markdown('<div class="ra-meta">[%s] · %s · %s%s · %s · reasons: %s</div>' % (
                            cid, STAGE_LABEL[stage_fn(r)], r["source"].replace("_", " "),
                            ", %s★" % r["rating"] if r.get("rating") else "", t["category"],
                            ", ".join(_as_list(t["reason_not_rebook"]))), unsafe_allow_html=True)
                with st.expander("All %d reviews the assistant read" % len(res["retrieved"])):
                    st.dataframe(pd.DataFrame([{"ref": c, "id": rid, "stage": STAGE_LABEL[stage_fn(by_id[rid])],
                                                "quote": by_id[rid]["tags"].get("evidence_quote") or ""}
                                               for c, rid in res["retrieved"]]), hide_index=True, width="stretch")
                st.caption("Tokens used: %s · model: %s" % (res.get("tokens"), res["model"]))
        st.divider()

    c1, c2, c3 = st.columns(3)
    with c1, st.container(border=True):
        st.markdown('<div class="kicker">Reviews tagged</div><div class="big">%d</div>'
                    '<div class="sub">metric-eligible, of 500 sampled (product purchases and house help excluded)</div>' % n,
                    unsafe_allow_html=True)
    with c2, st.container(border=True):
        cq = content["context_quote"]
        st.markdown('<div class="kicker">Why repeat matters</div><div class="big">%s</div><div class="sub">%s</div>'
                    '<div class="quote">"%s"<br>— %s</div>' % (cq["figure"], cq["label"], cq["quote"], cq["source"]),
                    unsafe_allow_html=True)
    with c3, st.container(border=True):
        st.markdown('<div class="kicker">Stage 2 + 3 combined</div><div class="big accent">%.0f%%</div>'
                    '<div class="sub">%d reviews: poor execution or trust soured. Stable across runs; the split '
                    'between the two isn\'t.</div>' % (V["s23_pct"], s23), unsafe_allow_html=True)

    st.subheader("The 4-stage funnel")
    st.caption("Each review sits in the first gate it fails. Shares are of public complaint voice; treat as ±5-8 points.")
    st.altair_chart(hbar({STAGE_LABEL[s]: sc[s] for s in STAGES}, pct_of=n, height=210), use_container_width=True)
    st.caption("Stage 2 vs Stage 3 depends on the service_outcome tagging rule (each 24%-39% across runs).")

    st.subheader("Headline findings")
    finds = content["headline_findings"]
    for row in range(0, len(finds), 3):
        cols = st.columns(3)
        for col, fnd in zip(cols, finds[row:row + 3]):
            with col:
                card(fnd["title"], fill(fnd["body"]))

# ============================================================================ 2. User Behaviour
with p2:
    st.caption("All charts: the %d metric-eligible reviews. Multi-value fields count every value a review carries." % n)
    a, b = st.columns(2)
    with a:
        st.altair_chart(hbar(df["rebooking_channel"].value_counts().to_dict(), "Where they went next (rebooking_channel)",
                             pct_of=n), use_container_width=True)
    with b:
        st.altair_chart(hbar(counter_of(multi_vals["workaround"]), "Workarounds used", pct_of=n), use_container_width=True)
    st.info("'unclear' dominates where-they-went-next: most reviews don't say. No leakage signal is not the same as "
            "staying with UC or abandoning the category.")
    a, b = st.columns(2)
    with a:
        rc = df["rating"].dropna().astype(int).value_counts().sort_index()
        st.altair_chart(hbar({"%d★" % k: int(v) for k, v in rc.items()}, "Star rating (app stores only)",
                             pct_of=int(rc.sum())), use_container_width=True)
    with b:
        st.altair_chart(hbar(df["sentiment"].value_counts().to_dict(), "Sentiment", pct_of=n), use_container_width=True)
    st.altair_chart(hbar(df["category"].value_counts().to_dict(), "Service category", pct_of=n), use_container_width=True)

    st.subheader("Leakage within each stage")
    lk = summary["cross_stage_leakage"]
    st.dataframe(pd.DataFrame([{"stage": STAGE_LABEL[s], "reviews": v["n"],
                                "leaked to local/offline": "%d (%.1f%%)" % (v["local_offline"], v["pct_local_offline"]),
                                "leaked to another app": "%d (%.1f%%)" % (v["competing_platform"], v["pct_competing_platform"]),
                                "any leakage": "%d (%.1f%%)" % (v["any_leak"], v["pct_any_leak"])}
                               for s, v in lk.items()]), hide_index=True, width="stretch")
    st.caption("From workaround / rebooking_channel. The rest of each stage is mostly 'unclear' (not stated), "
               "not proof of abandoning the category.")

# ============================================================================ 3. Product Opportunities
with p3:
    st.subheader("Lead opportunity: recover failures with warranty and support")
    a, b = st.columns(2)
    with a, st.container(border=True):
        st.markdown('<div class="kicker">Warranty / redo refused</div><div class="big accent">%d</div>'
                    '<div class="sub"><b>%d</b> follow a Stage 2 failure (poorly done job), %d a Stage 1 failure.</div>'
                    % (V["warranty"], V["warranty_s2"], V["warranty_s1"]), unsafe_allow_html=True)
        st.markdown("**Opportunity:** " + content["opportunities"][0]["statement"])
    with b, st.container(border=True):
        st.markdown('<div class="kicker">Support unresponsive</div><div class="big accent">%d</div>'
                    '<div class="sub"><b>%d</b> follow a Stage 1 failure (job never happened), %d a Stage 2 failure.</div>'
                    % (V["support"], V["support_s1"], V["support_s2"]), unsafe_allow_html=True)
        st.markdown("**Opportunity:** " + content["opportunities"][1]["statement"])

    st.subheader("Unmet needs")
    un = counter_of(multi_vals["unmet_need"])
    st.altair_chart(hbar(un, pct_of=n), use_container_width=True)
    opp = content["unmet_need_opportunities"]
    for need, cnt in sorted(un.items(), key=lambda kv: -kv[1]):
        if need in ("unclear", "not_mentioned"):
            continue
        card("%s · %d reviews (%.0f%%)" % (need.replace("_", " "), cnt, 100.0 * cnt / n),
             opp.get(need, ""))
    st.caption("Shares are of public complaint voice and directional (±5-8 points). A review can carry several needs.")

# ============================================================================ 4. Evidence Explorer
with p4:
    view = st.radio("View", ["All reviews", "Quotes by stage"], horizontal=True, label_visibility="collapsed")
    if view == "All reviews":
        f1, f2, f3, f4 = st.columns(4)
        stages = f1.multiselect("Stage", [STAGE_LABEL[s] for s in STAGES])
        cats = f2.multiselect("Category", sorted(df["category"].unique()))
        ratings = f3.multiselect("Star rating", sorted(int(x) for x in df["rating"].dropna().unique()))
        sources = f4.multiselect("Source", sorted(df["source"].unique()))
        g1, g2, g3 = st.columns([1, 2, 2])
        tag_field = g1.selectbox("Any tag field", ["(none)"] + TAG_FIELDS)
        tag_vals = []
        if tag_field != "(none)":
            tag_vals = g2.multiselect("has value", sorted(counter_of(multi_vals[tag_field])))
        q = g3.text_input("Search text, quote or tags")
        mask = pd.Series(True, index=df.index)
        if stages:
            mask &= df["stage_label"].isin(stages)
        if cats:
            mask &= df["category"].isin(cats)
        if ratings:
            mask &= df["rating"].isin(ratings)
        if sources:
            mask &= df["source"].isin(sources)
        if tag_vals:
            mask &= pd.Series([bool(set(v) & set(tag_vals)) for v in multi_vals[tag_field]], index=df.index)
        if q:
            blob = df[["text", "evidence_quote"] + TAG_FIELDS].astype(str).agg(" ".join, axis=1)
            mask &= blob.str.contains(q, case=False, regex=False)
        v = df[mask]
        st.caption("%d of %d reviews · click a row to see its full text and tags" % (len(v), n))
        shown = v[["id", "stage_label", "source", "rating", "date", "category", "service_outcome",
                   "reason_not_rebook", "rebooking_channel", "text"]].reset_index(drop=True)
        ev = st.dataframe(shown, hide_index=True, width="stretch", height=380, on_select="rerun",
                          selection_mode="single-row", key="reviews",
                          column_config={"stage_label": "stage", "text": st.column_config.TextColumn("text", width="large")})
        sel = ev.selection.rows if ev is not None and ev.selection else []
        if sel:
            r = by_id[shown.iloc[sel[0]]["id"]]
            t = r["tags"]
            with st.container(border=True):
                st.markdown("**Review %s** · %s · %s%s · %s" % (
                    r["id"], STAGE_LABEL[stage_of(signals(t))], r["source"],
                    ", %s★" % r["rating"] if r.get("rating") is not None else "", (r.get("date") or "")[:10]))
                a, b = st.columns([3, 2])
                a.markdown('<div class="kicker">Original text (PII-scrubbed)</div>', unsafe_allow_html=True)
                a.write(r["text"])
                if t.get("evidence_quote"):
                    a.markdown('<div class="kicker">Evidence quote</div>', unsafe_allow_html=True)
                    a.markdown("> " + t["evidence_quote"])
                b.markdown('<div class="kicker">Complete tag set</div>', unsafe_allow_html=True)
                b.dataframe(pd.DataFrame([{"field": k, "value": _fmt(t.get(k))} for k in TAG_FIELDS] +
                                         [{"field": "metric_eligible", "value": str(r.get("metric_eligible"))},
                                          {"field": "city_canonical", "value": _fmt(r.get("city_canonical"))},
                                          {"field": "tag_model", "value": r.get("tag_model")}]),
                            hide_index=True, width="stretch", height=640)
    else:
        st.caption("Vetted quotes per stage, verbatim from the reviews (PII already removed).")
        for s in STAGES[:4]:
            sub = df[df["stage"] == s]
            st.subheader("%s · %d reviews" % (STAGE_LABEL[s], len(sub)))
            for pre in DECK_QUOTES[s]:
                hit = sub[sub["id"].str.startswith(pre)]
                if len(hit):
                    h = hit.iloc[0]
                    st.markdown("> %s  \n<span style='color:#5F6B76;font-size:0.85rem'>— %s%s</span>" % (
                        h["evidence_quote"], h["source"].replace("_", " "),
                        ", %d★" % h["rating"] if pd.notna(h["rating"]) else ""), unsafe_allow_html=True)
            with st.expander("All quotes in this stage"):
                st.dataframe(sub[sub["evidence_quote"] != ""][["id", "source", "rating", "evidence_quote"]],
                             hide_index=True, width="stretch")

# ============================================================================ 5. Data Quality & Validation
with p5:
    st.subheader("Golden-set agreement")
    st.caption("Nemotron vs the human-corrected golden set (docs/golden_20.csv): %d of the 20 gold reviews are in the "
               "tagged sample. Old run = before the 2026-11-24 rule tightening; new run = tagged_v2 (used for Part 2)." % gold_n)
    st.dataframe(gold_table, hide_index=True, width="stretch")

    st.subheader("Run-to-run noise")
    if noise is not None:
        st.caption("Same model, same 500 reviews, two runs (v2 also added rules for four other fields). "
                   "These fields' wording did not change, yet the tags did.")
        st.dataframe(noise, hide_index=True, width="stretch")
        st.markdown("**%d of 500 reviews flipped metric_eligible** between the two runs. This comes from the model's "
                    "outputs, not a pipeline bug: every row passed validation and every code step is deterministic. "
                    "**Treat every percentage on this dashboard as directional (±5-8 points).**" % V["flips"])
    else:
        st.markdown("The old run (`data/processed/tagged.jsonl`) isn't in this copy, so the per-field comparison isn't "
                    "shown. Measured on 2026-11-24 (see `docs/evidence_log.md`): **%s of 500 reviews flipped "
                    "metric_eligible** between two runs of the same model. **Treat every percentage on this dashboard "
                    "as directional (±5-8 points).**" % V["flips"])

    st.subheader("Limitations")
    for item in content["limitations"]:
        st.markdown("- " + fill(item))

    st.subheader("Audit trail")
    st.markdown("Every source decision, access limit, codebook change and run is logged, dated, in "
                "`docs/evidence_log.md`.")
    with st.expander("Read docs/evidence_log.md"):
        st.markdown(EVIDENCE_LOG.read_text(encoding="utf-8"))
