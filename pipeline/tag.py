"""LLM tagging: data/processed/filtered.jsonl -> data/processed/tagged.jsonl

    python -m pipeline.tag --dry-run            # print the prompt for one item, no API call
    python -m pipeline.tag --limit 20           # pilot: tag 20 items (random, seeded)
    python -m pipeline.tag                      # all items

Applies codebook.yaml to each customer-voice item with Groq (config `tagging.model`).
Shareholder letters never reach this step (filter.py routes them to business_context.jsonl).

Per item:
  - the prompt is generated from codebook.yaml (descriptions, allowed values, multi flag, rules), so
    the codebook is the single source of truth
  - the model returns JSON. Every field is validated against its allowed values; on errors the model
    is re-asked once with the error list. If it's still invalid -> tag_status "invalid": the raw
    output is kept for review and the item is excluded from scoring. Values are NEVER silently coerced.
  - platform comes from the item's source, not the model
  - city_signal is computed in code: the model returns `city_name`; that, the clean step's
    `city_mention`, and a word-boundary scan of the text are mapped to metro / tier_2 using the
    codebook lists and aliases. A named city in neither list -> tier_3_or_smaller; none -> unclear
  - evidence_quote must be verbatim: an exact quote is kept, otherwise the longest span it shares with the text
    (>= 40 chars / 6 words), otherwise dropped (counted in the log)
  - personal names: the model lists them (same call, no extra request); code replaces each one that really occurs,
    plus "Mr./Mrs./Dr. Name", with [NAME] in the saved text and evidence_quote (`names_redacted` in the log)
  - metric_eligible = tag_status ok AND category not in config metric.exclude_categories
  - category.code_overrides (codebook) force house_help / product_purchase when the text names InstaHelp/house
    help or a Native product, whatever the model chose; `category_model` keeps the model's guess
  - results are cached by sha256(model + full prompt + text), so any prompt/codebook change re-tags

Outputs: tagged.jsonl, tag_log.json (status counts + value distributions per field).
"""
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import difflib
import hashlib
import json
import os
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from dotenv import load_dotenv

from collect.common import ROOT, load_config
from pipeline.clean import PROCESSED_DIR

IN = PROCESSED_DIR / "filtered.jsonl"
OUT = PROCESSED_DIR / "tagged.jsonl"
LOG = PROCESSED_DIR / "tag_log.json"
CACHE = PROCESSED_DIR / "tag_cache.jsonl"
CODEBOOK = ROOT / "codebook.yaml"
CODE_DERIVED = {"platform", "city_signal"}          # set in code, not by the model
EXCLUSIVE = {"unclear", "not_applicable", "none_mentioned", "not_mentioned", "none", "nothing_specific"}


# ----------------------------------------------------------------------------- codebook
def load_codebook() -> Tuple[dict, Dict[str, dict], str]:
    raw = CODEBOOK.read_text(encoding="utf-8")
    cb = yaml.safe_load(raw)
    common = cb.get("common_values", [])
    specs = {}
    for name, f in cb["fields"].items():
        vals = f["values"] if isinstance(f["values"], list) else []
        if f.get("status") == "draft":
            vals = vals + [v for v in common if v not in vals]
        specs[name] = {"description": f.get("description", ""), "multi": bool(f.get("multi")),
                       "values": vals, "rules": f.get("rules", []) or []}
    return cb, specs, hashlib.sha256(raw.encode()).hexdigest()[:12]


def category_override(text: str, cb: dict) -> Optional[str]:
    """Codebook category.code_overrides: forced category when the text names InstaHelp / house help or a Native product."""
    for cat, pats in ((cb["fields"]["category"].get("code_overrides") or {}).items()):
        if any(re.search(p, text, re.I) for p in pats):
            return cat
    return None


def build_city_index(cb: dict) -> Dict[str, Tuple[str, str]]:
    """lowercased alias -> (canonical city, tier)"""
    f = cb["fields"]["city_signal"]
    idx = {}
    aliases = f.get("aliases", {}) or {}
    for tier, key in (("metro", "metro_cities"), ("tier_2", "tier_2_cities")):
        for city in f.get(key, []):
            for name in [city] + list(aliases.get(city, [])):
                idx[name.lower()] = (city, tier)
    return idx


# A state/UT/country is not a city: never tier it as tier_3_or_smaller. (Delhi is matched as a city alias first.)
NOT_A_CITY = {x.lower() for x in [
    "India", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Orissa", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura",
    "Uttar Pradesh", "UP", "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Dadra and Nagar Haveli and Daman and Diu",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry"]}


def city_tier(item: Dict, model_city: Optional[str], idx: Dict[str, Tuple[str, str]]) -> Tuple[str, Optional[str]]:
    for cand in (model_city, item.get("city_mention")):
        if cand and cand.strip().lower() in idx:
            c, t = idx[cand.strip().lower()]
            return t, c
    low = item["text"].lower()
    for name in sorted(idx, key=len, reverse=True):          # longest first: "navi mumbai" before "mumbai"
        if re.search(r"\b%s\b" % re.escape(name), low):
            c, t = idx[name]
            return t, c
    for cand in (model_city, item.get("city_mention")):
        if cand and cand.strip() and cand.strip().lower() not in NOT_A_CITY:
            return "tier_3_or_smaller", cand.strip()
    return "unclear", None


# ----------------------------------------------------------------------------- prompt
def build_system_prompt(specs: Dict[str, dict], batch: bool = False) -> str:
    lines = [
        "You tag customer reviews and complaints about Urban Company (UC), an Indian home-services app, for a study of",
        "why customers don't book again within 90 days. Texts may be English, Hindi or Hinglish. Some details are redacted",
        "as [ADDRESS], [PHONE], [EMAIL], [PINCODE].",
        "",
        ("You will receive several reviews, labelled R1, R2, ... Tag EACH review independently, judging it ONLY on its own "
         "text: never carry a category, quote, name or any other detail from one review to another. Return ONE JSON object "
         '{"results": [...]} with exactly one entry per review, in the same order, each entry having "review_id" (e.g. "R1") '
         "plus exactly the keys below." if batch else
         "Return ONE JSON object with exactly these keys.") + " Use ONLY the allowed values, spelled exactly.",
        "Single-value fields: a string. Multi-value fields: a JSON array of 1+ strings.",
        "Tag only what the text states or clearly implies; don't guess. Values like unclear / not_applicable /",
        "none_mentioned / not_mentioned / none / nothing_specific must appear ALONE, never with other values.",
        "",
    ]
    for name, s in specs.items():
        if name in CODE_DERIVED:
            continue
        lines.append("%s (%s): %s" % (name, "multi" if s["multi"] else "single", s["description"]))
        lines.append("  allowed: %s" % ", ".join(s["values"]))
        for r in s["rules"]:
            lines.append("  rule: %s" % r.strip())
    lines += [
        "",
        "city_name (string or null): the Indian CITY the customer is in, if stated or clearly implied (not a state,",
        "  locality or country). null if none.",
        "evidence_quote (string or null): ONE contiguous excerpt (max 30 words) from the text that best supports",
        "  reason_not_rebook / service_outcome, copied character-for-character. Do NOT join separate sentences, skip",
        "  words, shorten, translate or reword. If no single unbroken span works, return null.",
        "person_names (array of strings): every PERSONAL name in the text (customer, professional, technician, staff),",
        "  copied exactly as written, e.g. [\"vikas Mungre\", \"Dhiraj Verma\"]. Not company, brand, product or place names.",
        "  [] if none.",
    ]
    return "\n".join(lines)


def user_prompt(item: Dict) -> str:
    meta = "source: %s" % item["source"]
    if item.get("rating") is not None:
        meta += " | star rating: %s/5" % item["rating"]
    return "%s\n\nTEXT:\n%s" % (meta, item["text"])


def batch_user_prompt(items: List[Dict]) -> str:
    parts = ["Tag each of these %d reviews independently." % len(items)]
    for j, it in enumerate(items, 1):
        parts.append("### R%d\n%s" % (j, user_prompt(it)))
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------- validation
def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


QUOTE_MIN_CHARS, QUOTE_MIN_WORDS = 40, 6
VALIDATION_VERSION = "2"  # bump when validate() rules change, so cached results are re-checked under the new rules


def _verbatim_quote(q, text: str) -> Tuple[Optional[str], bool]:
    """Return (quote, dropped). An exact quote is kept. Otherwise keep the longest contiguous span the quote shares
    with the text (taken from the text itself) if it's >= 40 chars and 6 words: the model often joins a correct
    opening to a later sentence. Anything shorter is dropped."""
    if not q:
        return None, False
    q = str(q).strip()
    if _norm_ws(q) in _norm_ws(text):
        return q, False
    a, b = text.lower(), re.sub(r"\s+", " ", q).strip().lower()
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    span = text[m.a:m.a + m.size].strip(" ,;:-")
    if len(span) >= QUOTE_MIN_CHARS and len(span.split()) >= QUOTE_MIN_WORDS:
        return span, False
    return None, True


# brands/companies/products the model might mislabel as people: never redacted
NOT_A_PERSON = {"urban company", "urbanclap", "urban clap", "uc", "snabbit", "pronto", "yes madam", "native", "insta help",
                "instahelp", "daikin", "diakin", "lg", "samsung", "voltas", "blue star", "kent", "aquaguard", "google", "india"}
TITLE_NAME_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?")


def redact_names(text: str, names: List[str]) -> Tuple[str, int]:
    """Replace model-reported personal names (only if they really occur, word-bounded) and Mr./Mrs./Dr. Name with [NAME]."""
    n = 0
    names = [x for x in names if x.lower().strip() not in NOT_A_PERSON and "company" not in x.lower()]
    for name in sorted(set(names), key=len, reverse=True):
        rx = re.compile(r"(?<![\w])%s(?![\w])" % re.escape(name), re.I)
        text, k = rx.subn("[NAME]", text)
        n += k
    text, k = TITLE_NAME_RE.subn("[NAME]", text)
    return text, n + k


def validate(out: dict, specs: Dict[str, dict], text: str) -> Tuple[Dict, List[str], bool]:
    """Returns (tags, errors, evidence_dropped). Never coerces an invalid value."""
    errs, tags = [], {}
    for name, s in specs.items():
        if name in CODE_DERIVED:
            continue
        if name not in out:
            errs.append("%s: missing" % name)
            continue
        v = out[name]
        if s["multi"]:
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not v:
                errs.append("%s: must be a non-empty array" % name)
                continue
            v = [str(x).strip().lower() for x in v]
            bad = [x for x in v if x not in s["values"]]
            if bad:
                errs.append("%s: not allowed %s" % (name, bad))
                continue
            v = list(dict.fromkeys(v))
            if len(v) > 1 and EXCLUSIVE.intersection(v):
                errs.append("%s: %s must appear alone" % (name, sorted(EXCLUSIVE.intersection(v))))
                continue
        else:
            if not isinstance(v, str) or v.strip().lower() not in s["values"]:
                errs.append("%s: not allowed %r" % (name, v))
                continue
            v = v.strip().lower()
        tags[name] = v
    # owner rule (2026-11-22): switched_to_local_provider ALWAYS when the customer went to a local/offline provider
    if not errs and "local_offline_provider" in (tags.get("workaround") or []) \
            and "switched_to_local_provider" not in (tags.get("reason_not_rebook") or []):
        errs.append("reason_not_rebook: must include switched_to_local_provider because workaround is local_offline_provider "
                    "(unless it is only unclear/not_applicable, then replace that)")
    ev, dropped = _verbatim_quote(out.get("evidence_quote"), text)
    tags["evidence_quote"] = ev
    names = out.get("person_names") or []
    tags["_person_names"] = [n.strip() for n in names if isinstance(n, str) and len(n.strip()) >= 2] if isinstance(names, list) else []
    city = out.get("city_name")
    tags["_city_name"] = city.strip() if isinstance(city, str) and city.strip() else None
    return tags, errs, dropped


# ----------------------------------------------------------------------------- LLM
class DailyLimitReached(Exception):
    """Groq's requests-per-day allowance is used up. Items done so far are cached; re-run later to resume.
    `partial` carries a batch's already-valid results when the limit hit during its repair round."""
    partial = None


def _log_req(msg: str) -> None:
    import sys
    print("[%s] groq %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


class GenerationFailed(Exception):
    """Groq rejected the model's output (e.g. 400 json_validate_failed). The caller falls back to per-item tagging."""


class GroqTagger:
    def __init__(self, cfg: dict):
        load_dotenv(ROOT / ".env")
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise SystemExit("GROQ_API_KEY not set in .env. Run with --dry-run to inspect prompts without it.")
        from groq import Groq
        # explicit timeout: a dropped connection (socket left in CLOSE_WAIT) hung the first full run for 15 min
        self.client = Groq(api_key=key, timeout=float(cfg.get("tagging", {}).get("request_timeout_seconds", 120)),
                           max_retries=0)
        tc = cfg.get("tagging", {})
        self.model = tc.get("model")
        self.temperature = float(tc.get("temperature", 0))
        self.min_interval = 60.0 / float(tc.get("requests_per_minute", 25))
        self._last = 0.0
        self.tokens_used = 0
        self.max_tokens = int(tc.get("max_completion_tokens", 8192))
        self.reasoning_effort = tc.get("reasoning_effort")  # None = model default
        self.max_tpd_wait = float(tc.get("max_daily_token_wait_seconds", 1800))
        available = sorted(m.id for m in self.client.models.list().data)
        if self.model not in available:
            raise SystemExit("Configured tagging.model %r isn't available on Groq. Available: %s"
                             % (self.model, ", ".join(available)))

    def __call__(self, messages: List[dict]) -> str:
        # Free tier (checked 2026-11-22): 1,000 requests/day and 8,000 tokens/minute per model. Waits follow
        # Groq's retry-after header (tokens/minute); an exhausted daily allowance stops the run cleanly.
        from groq import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
        server_errors = 0
        for attempt in range(40):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            t0 = time.time()
            try:
                self._last = time.time()
                r = self.client.chat.completions.create(model=self.model, messages=messages,
                                                        temperature=self.temperature,
                                                        max_completion_tokens=self.max_tokens,
                                                        **({"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}),
                                                        response_format={"type": "json_object"})
                self.tokens_used += getattr(r.usage, "total_tokens", 0) or 0
                _log_req("ok %.1fs tokens=%s finish=%s" % (time.time() - t0, getattr(r.usage, "total_tokens", "?"),
                                                           r.choices[0].finish_reason))
                return r.choices[0].message.content
            except RateLimitError as e:
                h = e.response.headers
                _log_req("429 after %.1fs retry-after=%s remaining-req=%s" % (time.time() - t0, h.get("retry-after"),
                                                                             h.get("x-ratelimit-remaining-requests")))
                msg = str(e).lower()
                try:
                    ra = float(h.get("retry-after") or 0)
                except ValueError:
                    ra = 0
                if ("(tpd)" in msg or "tokens per day" in msg) and ra <= self.max_tpd_wait:
                    # the daily token cap refills continuously (~8.3k tokens/h on 200k/day): short waits are the run's pace
                    _log_req("daily-token bucket: waiting %.0fs" % ra)
                    time.sleep(ra + 2)
                    continue
                if h.get("x-ratelimit-remaining-requests") == "0" or "per day" in msg or "(tpd)" in msg or "(rpd)" in msg:
                    # free tier also caps TOKENS per day (200k for gpt-oss-120b): a long retry-after means "stop", not "wait"
                    raise DailyLimitReached("daily limit reached (%s); retry-after %ss" % (
                        "tokens/day" if "tpd" in msg or "tokens per day" in msg else "requests/day", h.get("retry-after")))
                try:
                    ra = float(h.get("retry-after") or 0)
                except ValueError:
                    ra = 0
                time.sleep(max(ra, 2.0) + 1.0)
            except (APITimeoutError, APIConnectionError) as e:
                _log_req("%s after %.1fs" % (type(e).__name__, time.time() - t0))
                server_errors += 1
                if server_errors > 5:
                    raise
                time.sleep(min(60, 5 * 2 ** server_errors))
            except APIStatusError as e:
                _log_req("HTTP %s after %.1fs: %s" % (e.status_code, time.time() - t0, str(e)[:120]))
                if e.status_code == 400 and "json_validate_failed" in str(e):
                    raise GenerationFailed(str(e)[:200])
                if e.status_code >= 500 and server_errors < 5:
                    server_errors += 1
                    time.sleep(min(60, 5 * 2 ** server_errors))
                else:
                    raise
        raise RuntimeError("Groq: still rate-limited after 40 waits")


class NvidiaTagger:
    """Standby provider: NVIDIA API catalog (OpenAI-compatible). Same interface as GroqTagger: call(messages) -> JSON text.
    Config: tagging.standby {base_url, model, api_key_env}. Limits are undocumented in headers, so 429s back off."""

    section = "standby"
    label = "nvidia"

    def __init__(self, cfg: dict):
        import requests
        load_dotenv(ROOT / ".env")
        sb = (cfg.get("tagging", {}) or {}).get(self.section) or {}
        self.extra = {}
        if sb.get("provider_order"):  # OpenRouter: pin backends (auto-routing sent gpt-oss to a backend that broke JSON)
            self.extra["provider"] = {"order": sb["provider_order"], "allow_fallbacks": False}
        if sb.get("json_mode"):
            self.extra["response_format"] = {"type": "json_object"}
        if sb.get("reasoning_effort"):
            self.extra["reasoning"] = {"effort": sb["reasoning_effort"]}
        key = os.getenv(sb.get("api_key_env", "NVIDIA_API_KEY"), "").strip()
        if not key:
            raise SystemExit("%s not set in .env" % sb.get("api_key_env", "NVIDIA_API_KEY"))
        self.url = sb.get("base_url", "https://integrate.api.nvidia.com/v1").rstrip("/")
        self.model = sb["model"]
        tc = cfg.get("tagging", {})
        self.temperature = float(tc.get("temperature", 0))
        self.max_tokens = int(sb.get("max_tokens", tc.get("max_completion_tokens", 8192)))
        self.timeout = float(sb.get("request_timeout_seconds", tc.get("request_timeout_seconds", 120)))
        self._headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        self._local = threading.local()
        self._lock = threading.Lock()
        self.s = self._session()
        self.tokens_used = 0
        ids = {m["id"] for m in self.s.get(self.url + "/models", timeout=30).json().get("data", [])}
        if self.model not in ids:
            raise SystemExit("standby model %r not listed by NVIDIA" % self.model)

    def _session(self):
        import requests
        if getattr(self._local, "s", None) is None:  # one HTTP session per worker thread
            self._local.s = requests.Session()
            self._local.s.headers.update(self._headers)
        return self._local.s

    @staticmethod
    def _json_text(content: str) -> str:
        t = (content or "").strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
        a, b = t.find("{"), t.rfind("}")
        return t[a:b + 1] if a >= 0 and b > a else t

    def __call__(self, messages: List[dict]) -> str:
        import requests
        for attempt in range(20):
            t0 = time.time()
            try:
                r = self._session().post(self.url + "/chat/completions", timeout=(30, self.timeout), stream=True,  # read gap must allow the model's silent reasoning (60-130s)
                                
                                json=dict({"model": self.model, "messages": messages, "temperature": self.temperature,
                                           "max_tokens": self.max_tokens}, **self.extra))
                # TOTAL deadline: requests' timeout only limits silence between bytes, and NVIDIA trickled bytes for
                # 84 min (2026-11-22). Read in chunks and abandon the request once the deadline passes.
                body, deadline = [], t0 + self.timeout
                # watchdog: force-close the connection at the deadline. A trickle of tiny keep-alive bytes never fills a
                # read buffer and keeps resetting the read timeout, so a check inside the read loop can't fire (2026-11-22).
                dog = threading.Timer(max(1.0, deadline - time.time()), r.close)
                dog.daemon = True
                dog.start()
                try:
                    for chunk in r.iter_content(chunk_size=None):  # whatever arrives, as it arrives
                        body.append(chunk)
                        if time.time() > deadline:
                            raise requests.Timeout("total deadline %.0fs exceeded" % self.timeout)
                except (AttributeError, ValueError, OSError) as e:  # read on a connection the watchdog closed
                    raise requests.Timeout("total deadline %.0fs exceeded (%s)" % (self.timeout, type(e).__name__))
                finally:
                    dog.cancel()
                r._content = b"".join(body)
                if time.time() > deadline:
                    raise requests.Timeout("total deadline %.0fs exceeded" % self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                _log_req(self.label + " %s after %.1fs" % (type(e).__name__, time.time() - t0))
                time.sleep(min(60, 5 * 2 ** min(attempt, 4)))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(60, 5 * 2 ** min(attempt, 4))
                _log_req(self.label + " HTTP %d after %.1fs, waiting %.0fs" % (r.status_code, time.time() - t0, wait))
                time.sleep(wait)
                continue
            if r.status_code == 402:
                _log_req(self.label + " HTTP 402 (out of credits): %s" % r.text[:150])
                raise DailyLimitReached("%s: out of credits (HTTP 402); add credits and re-run to resume" % self.label)
            if r.status_code != 200:
                _log_req(self.label + " HTTP %d: %s" % (r.status_code, r.text[:150]))
                raise GenerationFailed(self.label + " HTTP %d" % r.status_code)
            d = r.json()
            u = d.get("usage") or {}
            with self._lock:
                self.tokens_used += u.get("total_tokens", 0) or 0
            ch = d["choices"][0]
            _log_req(self.label + " ok %.1fs tokens=%s finish=%s" % (time.time() - t0, u.get("total_tokens"), ch.get("finish_reason")))
            if ch.get("finish_reason") == "length":
                raise GenerationFailed(self.label + " output cut off (max_tokens)")
            return self._json_text(ch["message"].get("content"))
        raise RuntimeError(self.label + ": still failing after 20 attempts")


class OpenRouterTagger(NvidiaTagger):
    """OpenRouter (OpenAI-compatible). Config: tagging.standby_openrouter {base_url, model, api_key_env, provider_order, ...}."""
    section = "standby_openrouter"
    label = "openrouter"


def tag_item(item: Dict, llm, system: str, specs: Dict[str, dict], max_repair: int) -> Dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt(item)}]
    raw, errs, tags, dropped = None, ["no response"], {}, False
    for attempt in range(1 + max_repair):
        try:
            raw = llm(messages)
        except GenerationFailed as e:
            raw, errs = None, ["generation failed: %s" % e]
            continue
        try:
            out = json.loads(raw)
            if not isinstance(out, dict):
                raise ValueError("not a JSON object")
        except ValueError as e:
            errs = ["invalid JSON: %s" % e]
        else:
            tags, errs, dropped = validate(out, specs, item["text"])
            if not errs:
                break
        messages += [{"role": "assistant", "content": raw or ""},
                     {"role": "user", "content": "Fix these problems and return the full JSON object again:\n- "
                                                 + "\n- ".join(errs)}]
    return {"tags": tags, "errors": errs, "raw": raw, "attempts": attempt + 1, "evidence_dropped": dropped}


def _contamination(tags: Dict, own: str, others: List[str]) -> List[str]:
    """Detect details that belong to ANOTHER review in the same batch: an evidence quote that isn't in this review's
    text but is in another's, or a reported person name that occurs only in another review."""
    errs = []
    q = tags.get("evidence_quote")
    if q and _norm_ws(q) not in _norm_ws(own) and any(_norm_ws(q) in _norm_ws(o) for o in others):
        errs.append("evidence_quote is from another review in the batch")
    for n in tags.get("_person_names", []):
        rx = re.compile(r"(?<![\w])%s(?![\w])" % re.escape(n), re.I)
        if not rx.search(own) and any(rx.search(o) for o in others):
            errs.append("person name %r is from another review in the batch" % n)
    return errs


def _parse_results(raw: str) -> Dict[str, dict]:
    out = json.loads(raw)
    results = out.get("results") if isinstance(out, dict) else None
    if not isinstance(results, list):
        raise ValueError("no results array")
    return {str(r.get("review_id", "")).strip().upper(): r for r in results if isinstance(r, dict)}


def tag_batch(items: List[Dict], llm, system: str, specs: Dict[str, dict], error_log: Optional[Counter] = None,
              max_repair: int = 1) -> List[Optional[Dict]]:
    """One request for several reviews, plus (like per-item tagging) one repair round in the same conversation for the
    reviews whose answers were missing or invalid. Returns one result per item; None = re-tag that item alone;
    {"_contaminated": [...]} = a detail came from another review in the batch (also re-tagged alone, and counted)."""
    error_log = error_log if error_log is not None else Counter()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": batch_user_prompt(items)}]
    final: List[Optional[Dict]] = [None] * len(items)
    pending = list(range(len(items)))
    for attempt in range(1 + max_repair):
        try:
            raw = llm(messages)
        except DailyLimitReached as e:
            if attempt > 0:  # keep the reviews already validated in this batch
                e.partial = final
            raise
        except GenerationFailed:
            error_log["generation_failed"] += 1
            if attempt == 0:
                return [{"_batch_failed": True}] * len(items)
            break
        try:
            by_id = _parse_results(raw)
        except ValueError:
            error_log["bad_json"] += 1
            break
        problems = {}
        for j in list(pending):
            rid = "R%d" % (j + 1)
            o = by_id.get(rid)
            if o is None:
                problems[rid] = ["missing from results"]
                error_log["missing_entry"] += 1
                continue
            tags, errs, dropped = validate(o, specs, items[j]["text"])
            probe = dict(tags, evidence_quote=o.get("evidence_quote"))
            contam = _contamination(probe, items[j]["text"], [x["text"] for k, x in enumerate(items) if k != j])
            if contam:
                final[j] = {"_contaminated": contam}
                pending.remove(j)
                continue
            if errs:
                problems[rid] = errs
                for e in errs:
                    error_log[e.split(":")[0] + ": " + ("not allowed" if "not allowed" in e else "alone" if "alone" in e else e.split(":", 1)[-1].strip()[:30])] += 1
                continue
            final[j] = {"tags": tags, "errors": [], "raw": json.dumps(o, ensure_ascii=False), "attempts": attempt + 1,
                        "evidence_dropped": dropped, "mode": "batch" if attempt == 0 else "batch_repaired"}
            pending.remove(j)
        if not problems:
            break
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "Fix these problems. Return {\"results\": [...]} with entries ONLY for these "
                      "review_ids, each complete with all keys:\n" + "\n".join("- %s: %s" % (k, "; ".join(v)) for k, v in problems.items())}]
    return final


# ----------------------------------------------------------------------------- run
def _load_cache() -> Dict[str, dict]:
    if not CACHE.exists():
        return {}
    out = {}
    for line in open(CACHE, encoding="utf-8"):
        try:  # two runs can append concurrently; a garbled line is skipped (that item is simply re-tagged)
            d = json.loads(line)
            out[d["key"]] = d
        except (ValueError, KeyError):
            continue
    return out


def run(limit: Optional[int] = None, dry_run: bool = False, seed: int = 0, llm=None, match: Optional[str] = None,
        out_path=None, batch_size: Optional[int] = None, provider: str = "groq", concurrency: int = 1,
        in_path=None, reuse_models: Optional[List[str]] = None) -> Dict:
    cfg = load_config()
    cb, specs, cb_hash = load_codebook()
    city_idx = build_city_index(cb)
    bs = int(batch_size or cfg.get("tagging", {}).get("batch_size", 1))
    system_single = build_system_prompt(specs)
    system = build_system_prompt(specs, batch=bs > 1)
    prompt_hash = hashlib.sha256(system.encode()).hexdigest()[:12]  # any prompt, codebook or batch-mode change re-tags
    items = [json.loads(l) for l in open(in_path or IN, encoding="utf-8")]
    if match:
        items = [r for r in items if re.search(match, r["text"])]
    if limit:
        items = random.Random(seed).sample(items, min(limit, len(items)))
    if dry_run:
        print("=== SYSTEM ===\n%s\n\n=== USER (item 1 of %d) ===\n%s" % (system, len(items), user_prompt(items[0])))
        return {"dry_run": True, "items": len(items), "prompt_chars": len(system)}

    llm = llm or {"nvidia": NvidiaTagger, "openrouter": OpenRouterTagger}.get(provider, GroqTagger)(cfg)
    model = getattr(llm, "model", "custom")
    max_repair = int(cfg.get("tagging", {}).get("max_repair_attempts", 1))
    excluded = set((cfg.get("metric") or {}).get("exclude_categories", []))
    cache = _load_cache()
    stats, dist = Counter(), defaultdict(Counter)
    out_rows = []
    effort = (getattr(llm, "reasoning_effort", None) or "default") if provider == "groq" else "default"
    method = "%s|effort=%s|bs=%d|val=%s" % (prompt_hash, effort, bs, VALIDATION_VERSION)  # same prompt text with another effort/batch size is a different method
    keys = [hashlib.sha256(("%s|%s|%s" % (model, method, it["text"])).encode()).hexdigest() for it in items]
    results: Dict[str, Dict] = {}
    row_model: Dict[str, str] = {}
    # --reuse-from MODEL: keep existing results from another model (same prompt/method) instead of re-tagging.
    # Each row records its model in `tag_model` (owner decision 2026-11-22, option 2: keep the 100 gpt-oss results).
    for rm in (reuse_models or []):
        for k, it in zip(keys, items):
            rk = hashlib.sha256(("%s|%s|%s" % (rm, method, it["text"])).encode()).hexdigest()
            if k not in results and rk in cache:
                results[k] = cache[rk]["result"]
                row_model[k] = rm
    stats["reused_other_model"] = len(results)
    for k in keys:
        if k in cache and k not in results:
            results[k] = cache[k]["result"]
    stats["cache_hits"] = len(results) - stats["reused_other_model"]
    todo = [(k, it) for k, it in zip(keys, items) if k not in results]
    contamination: List[str] = []
    batch_errors: Counter = Counter()
    with open(CACHE, "a", encoding="utf-8") as cf:
        def keep(k, res):
            results[k] = res
            if not res["errors"]:  # cache only valid results, so invalid items are retried on re-run
                cf.write(json.dumps({"key": k, "result": res}, ensure_ascii=False) + "\n")
                cf.flush()
        def process(chunk):
            """One batch end to end (batch call, repair, per-item fallbacks). Runs in a worker thread; returns
            (results, local stats, contamination, batch error types, DailyLimitReached or None)."""
            st, contam, berr, out = Counter(), [], Counter(), []
            try:
                if bs > 1:
                    st["batch_calls"] += 1
                    try:
                        batch_res = tag_batch([it for _, it in chunk], llm, system, specs, berr, max_repair)
                    except DailyLimitReached as e:
                        out = [(k, r) for (k, _), r in zip(chunk, e.partial or []) if r is not None and "tags" in r]
                        return out, st, contam, berr, e
                else:
                    batch_res = [None] * len(chunk)
                if batch_res and batch_res[0] is not None and batch_res[0].get("_batch_failed"):
                    st["batch_generation_failed"] += 1
                    batch_res = [None] * len(chunk)
                for (k, it), r in zip(chunk, batch_res):
                    if r is not None and "_contaminated" in r:
                        st["batch_contamination"] += 1
                        contam.extend(r["_contaminated"])
                        r = None
                    if r is None:
                        if bs > 1:
                            st["batch_fallback_single"] += 1
                        r = tag_item(it, llm, system_single, specs, max_repair)
                        r["mode"] = "single_fallback" if bs > 1 else "single"
                    out.append((k, r))
            except DailyLimitReached as e:
                return out, st, contam, berr, e
            return out, st, contam, berr, None

        chunks = [todo[b:b + bs] for b in range(0, len(todo), bs)]
        stop = None
        last_print = len(results)
        # concurrency only overlaps waiting on the API; prompts, model, settings and checks are unchanged
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(process, c) for c in chunks]
            for fut in as_completed(futures):
                out, st, contam, berr, err = fut.result()
                for k, r in out:
                    keep(k, r)
                stats.update(st)
                contamination.extend(contam)
                batch_errors.update(berr)
                if err is not None and stop is None:
                    stop = err
                    for f in futures:
                        f.cancel()
                if len(results) - last_print >= 25 or len(results) == len(items):
                    last_print = len(results)
                    print("  tagged %d/%d" % (len(results), len(items)), flush=True)
        if stop is not None:
            stats["stopped_early"] = str(stop)
            stats["untagged_remaining"] = len(items) - len(results)
            print("  STOPPED: %s. %d items tagged/cached; re-run to resume." % (stop, len(results)))

        for i, (key, item) in enumerate(zip(keys, items), 1):
            if key not in results:
                continue
            res = results[key]
            tags = dict(res["tags"])
            ok = not res["errors"]
            forced = category_override(item["text"], cb) if ok else None
            if forced and tags.get("category") != forced:
                tags["category_model"] = tags.get("category")
                tags["category"] = forced
                stats["category_overridden"] += 1
            tier, city = city_tier(item, tags.pop("_city_name", None), city_idx)
            item = dict(item)
            item["text"], n_red = redact_names(item["text"], tags.pop("_person_names", []))
            if tags.get("evidence_quote"):
                tags["evidence_quote"] = redact_names(tags["evidence_quote"], res["tags"].get("_person_names", []))[0]
            stats["items_names_redacted"] += int(n_red > 0)
            stats["names_redacted"] += n_red
            tags["city_signal"] = tier
            tags["platform"] = item["source"]
            row = dict(item, tags=tags, city_canonical=city, tag_status="ok" if ok else "invalid",
                       tag_errors=res["errors"] or None, tag_attempts=res["attempts"], tag_model=row_model.get(key, model),
                       codebook_version=cb_hash,
                       metric_eligible=bool(ok and tags.get("category") not in excluded))
            row["tag_mode"] = res.get("mode", "single")
            if not ok:
                row["tag_raw_output"] = res["raw"]
            out_rows.append(row)
            stats["ok" if ok else "invalid"] += 1
            stats["repaired"] += int(ok and res["attempts"] > 1)
            stats["evidence_quote_dropped"] += int(res.get("evidence_dropped", False))
            stats["metric_eligible"] += int(row["metric_eligible"])
            if ok:
                for f, v in tags.items():
                    if f == "evidence_quote":
                        continue
                    for x in (v if isinstance(v, list) else [v]):
                        dist[f][x] += 1

    out = Path(out_path) if out_path else OUT
    tmp = out.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    tmp.replace(out)
    stats["tokens_used"] = getattr(llm, "tokens_used", 0)
    if contamination:
        stats["contamination_examples"] = contamination[:10]
    if batch_errors:
        stats["batch_error_types"] = dict(batch_errors.most_common())
    stats["tag_modes"] = dict(Counter(results[k].get("mode", "single") for k in keys if k in results))
    stats["tag_models"] = dict(Counter(row_model.get(k, model) for k in keys if k in results))
    log = {"items": len(items), "batch_size": bs, "reasoning_effort": effort, "model": model, "codebook_version": cb_hash, "prompt_version": prompt_hash, **stats,
           "distributions": {f: dict(c.most_common()) for f, c in dist.items()}}
    (out.with_name(out.stem + "_log.json") if out_path else LOG).write_text(json.dumps(log, indent=2, ensure_ascii=False))
    return log


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="tag a seeded random sample of N items (pilot)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="print the prompt for one item; no API call")
    ap.add_argument("--match", help="only items whose text matches this regex (targeted checks)")
    ap.add_argument("--batch-size", type=int, help="reviews per request (default: config tagging.batch_size; 1 = per item)")
    ap.add_argument("--out", help="write tagged rows here instead of data/processed/tagged.jsonl (comparison runs)")
    ap.add_argument("--in", dest="in_path", help="input file (default data/processed/filtered.jsonl), e.g. data/processed/sample.jsonl")
    ap.add_argument("--reuse-from", action="append", default=[], help="model id whose cached results to keep (repeatable)")
    ap.add_argument("--concurrency", type=int, default=1, help="batches in flight at once (paid providers; 1 = sequential)")
    ap.add_argument("--provider", choices=["groq", "nvidia", "openrouter"], default="groq",
                    help="nvidia = tagging.standby; openrouter = tagging.standby_openrouter")
    a = ap.parse_args()
    print(json.dumps(run(limit=a.limit, dry_run=a.dry_run, seed=a.seed, match=a.match, batch_size=a.batch_size,
                         out_path=a.out, provider=a.provider, concurrency=a.concurrency,
                         in_path=a.in_path, reuse_models=a.reuse_from), indent=2, ensure_ascii=False))
