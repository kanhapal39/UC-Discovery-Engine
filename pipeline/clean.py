"""Clean step: data/raw/*.jsonl -> data/processed/clean.jsonl

    python -m pipeline.clean

Per item, in order:
  1. normalise text (Unicode NFKC, collapse whitespace); drop empty/very short items
  2. drop duplicates (same source+id, or same normalised text within a source)
  3. PII scrub (scrub_pii): Indian phone numbers, email addresses, labelled addresses, pincodes
  4. verify: re-run the detectors on the scrubbed text; any remaining hit aborts the run
  5. language detection (langdetect; "unknown" for short text)

Nothing is written to data/processed/ unless every item has passed the scrub and the verify
step. The log (data/processed/clean_log.json and stdout) holds COUNTS ONLY, never the removed text.

Scrub rules
- Phone: Indian mobiles (+91 / 91 / 0 prefix optional, 10 digits starting 6-9, optional space or
  dash separators) and STD landlines (0 + 2-4 digit code + 6-8 digits) -> "[PHONE]".
- Email: any token@domain.tld, including site-masked forms like "[protected]@gmail.com" -> "[EMAIL]".
- Address: from an "address:" label (case-insensitive) up to whichever comes first: the pincode
  (inclusive), the next "Label:" field (Website:, Phone:, ...), a sentence end, or 120 characters
  -> "[ADDRESS]". The cap matters: consumercomplaints.in flattens the address and the complaint body onto
  one line, so a literal "everything after address:" would delete the whole complaint. Before
  redaction, the city (the last comma-separated part before the pincode, e.g. "Delhi", "Pune") is kept in
  `city_mention` for codebook.city_signal. A city name on its own is not treated as PII.
- Pincode: "pin/pincode: NNNNNN", or a bare 6-digit number starting 1-9 that isn't a rupee amount
  -> "[PINCODE]".
- consumer_complaints URLs are dropped (`url: null`) because the slugs contain address text, e.g.
  "...-bs-42-malibu-towne-gurugram-c3545534". The numeric complaint id is kept for traceability.
- NOT scrubbed: personal names. There's no reliable regex for them; this is a known gap.
"""
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Optional, Tuple

from langdetect import DetectorFactory, LangDetectException, detect

from collect.common import RAW_DIR, ROOT

PROCESSED_DIR = ROOT / "data" / "processed"
OUT = PROCESSED_DIR / "clean.jsonl"
LOG = PROCESSED_DIR / "clean_log.json"
MIN_CHARS = 15
DetectorFactory.seed = 0
DROP_URL_SOURCES = {"consumer_complaints"}

PHONE_RE = re.compile(
    r"(?<![\w])(?:(?:\+|00)?91[\s-]?|0[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"   # mobile, 5-5 or contiguous
    r"|(?<![\w])(?:(?:\+|00)?91[\s-]?|0[\s-]?)?[6-9]\d{2}[\s-]\d{3}[\s-]\d{4}(?!\d)"  # mobile, 3-3-4
    r"|(?<![\w])0\d{2,4}[\s-]\d{6,8}(?!\d)"                              # STD landline
    r"|(?:(?:\+|00)?91[\s-]?)?\(0?\d{2,4}\)[\s-]?\d{6,8}(?!\d)"            # landline, bracketed STD code
)
EMAIL_RE = re.compile(r"[^\s@<>()]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PIN_LABEL_RE = re.compile(r"(?i)\b(?:pin\s*code|pincode|pin)\s*[:\-]?\s*[1-9]\d{2}\s?\d{3}\b")
BARE_PIN_RE = re.compile(r"(?<![A-Za-z]-)(?<![\w₹.,])(?<!rs )(?<!rs\. )(?<!inr )[1-9]\d{5}(?!\w)(?![,.]\d)(?!\s*(?:rs|rupees|/-|inr))", re.I)  # (?!\w): skips reg. nos like 101248W
ADDR_LABEL_RE = re.compile(r"(?i)\baddress\s*:")
ADDR_PIN_RE = re.compile(r"(?<!\d)(?<!\d\s)[1-9]\d{2}\s?\d{3}(?!\s?\d)")  # not part of a longer digit run (house numbers)
NEXT_LABEL_RE = re.compile(r"\s[A-Z][A-Za-z]{1,20}:\s")  # single word ("Website:"); multi-word swallowed localities
SENT_END_RE = re.compile(r"[.!?](?:\s|$)")
ADDR_MAX = 120


BOILERPLATE_RE = re.compile(r"\s*Was this information helpful\?\s*No\s*\(\d+\)\s*Yes\s*\(\d+\)\s*$", re.I)  # consumercomplaints.in footer


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return BOILERPLATE_RE.sub("", text)


def _address_span(text: str, start: int) -> Tuple[int, Optional[str]]:
    """Return (end index, city) for an address that starts at `start` (just after the label)."""
    window = text[start:start + ADDR_MAX]
    ends = [len(window)]
    city = None
    pm = ADDR_PIN_RE.search(window)
    if pm:
        ends.append(pm.end())
        before = window[:pm.start()].strip(" ,-")
        part = before.split(",")[-1].strip(" -")
        words = re.findall(r"[A-Za-z]+", part)
        if words:
            city = " ".join(words[-2:]) if len(words) <= 2 else words[-1]
    lm = NEXT_LABEL_RE.search(window)
    if lm:
        ends.append(lm.start())
    sm = SENT_END_RE.search(window)
    if sm:
        ends.append(sm.start())
    end = min(ends)
    if city is None and lm and lm.start() == end:
        # no pincode, but the address ends cleanly at the next label: last comma part = city
        words = re.findall(r"[A-Za-z]+", window[:end].split(",")[-1])
        if 1 <= len(words) <= 2:
            city = " ".join(words)
    return start + end, city


def scrub_pii(text: str) -> Tuple[str, Counter, Optional[str]]:
    """Return (scrubbed text, Counter of redaction types, city_mention or None)."""
    c = Counter()
    city = None
    # addresses first (they contain pincodes and digit runs)
    out, pos = [], 0
    for m in ADDR_LABEL_RE.finditer(text):
        if m.start() < pos:
            continue
        end, cty = _address_span(text, m.end())
        out.append(text[pos:m.start()])
        out.append("[ADDRESS]")
        city = city or cty
        c["address"] += 1
        pos = end
    out.append(text[pos:])
    text = "".join(out)

    for name, rx, repl in (("email", EMAIL_RE, "[EMAIL]"), ("phone", PHONE_RE, "[PHONE]"),
                           ("pincode", PIN_LABEL_RE, "[PINCODE]"), ("pincode", BARE_PIN_RE, "[PINCODE]")):
        text, n = rx.subn(repl, text)
        c[name] += n
    return text, +c, city


def residual_pii(text: str) -> Counter:
    """Detectors used to verify the scrub. Returns counts of anything still matching."""
    c = Counter()
    for name, rx in (("email", EMAIL_RE), ("phone", PHONE_RE), ("pincode", PIN_LABEL_RE),
                     ("pincode", BARE_PIN_RE), ("address", ADDR_LABEL_RE)):
        c[name] += len(rx.findall(text))
    return +c


def detect_lang(text: str) -> str:
    if len(text) < 20:
        return "unknown"
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"


def run() -> Dict:
    stats = defaultdict(Counter)
    rows, seen_ids, seen_text = [], set(), set()
    for path in sorted(RAW_DIR.glob("*.jsonl")):
        src = path.stem
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            st = stats[src]
            st["input"] += 1
            text = normalise(r.get("text"))
            if len(text) < MIN_CHARS:
                st["dropped_short"] += 1
                continue
            key = (src, str(r.get("id")))
            tkey = (src, text.lower())
            if key in seen_ids or tkey in seen_text:
                st["dropped_duplicate"] += 1
                continue
            seen_ids.add(key)
            seen_text.add(tkey)

            text, red, city = scrub_pii(text)
            red = +red  # drop zero counts; a Counter with zero-valued keys is still truthy
            if red:
                st["items_scrubbed"] += 1
                for k, v in red.items():
                    st["redactions_%s" % k] += v
            left = residual_pii(text)
            if left:
                raise RuntimeError("PII scrub verification failed for %s id=%s (types: %s). Nothing written."
                                   % (src, r.get("id"), dict(left)))
            r["text"] = text
            r["city_mention"] = city
            r["pii_scrubbed"] = bool(red)
            if src in DROP_URL_SOURCES:
                r["url"] = None
            if r.get("language") in (None, "unknown"):
                r["language"] = detect_lang(text)
            rows.append(r)
            st["output"] += 1

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    tmp.replace(OUT)
    log = {"output": str(OUT), "total_output": len(rows),
           "total_items_scrubbed": sum(s["items_scrubbed"] for s in stats.values()),
           "per_source": {k: dict(v) for k, v in stats.items()}}
    LOG.write_text(json.dumps(log, indent=2))
    return log


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
