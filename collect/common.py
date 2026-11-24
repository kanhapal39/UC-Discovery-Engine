"""Shared helpers for all collectors: config loading, keyword matching, polite HTTP, JSONL output."""
import json
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
import yaml
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
CONFIG_PATH = ROOT / "config.yaml"


class SourceBlocked(Exception):
    """Raised when a source needs login or shows a bot challenge. Per CLAUDE.md, stop and report it."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    groups = cfg.get("keyword_groups") or {}
    if groups:
        flat = []
        for kws in groups.values():
            flat += [k for k in kws if k not in flat]
        cfg["keywords"] = flat
        cfg["keyword_to_group"] = {k: g for g, kws in groups.items() for k in kws}
    return cfg


def _normalise(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("'", "")  # "won't" == "wont", "didn't" == "didnt"
    return re.sub(r"\s+", " ", text)


def _word_window_match(k: str, t: str, threshold: int) -> bool:
    """Fuzzy match against whole-word windows of the text (n-1..n+1 words for an n-word keyword), so it's
    boundary-correct. The window's first word must share its first 3 letters with the keyword's first word:
    keeps "professional did not show up" / "cancelled a booking"; rejects "UNreliable service",
    "STARTED the service" (vs "redo the service") and "was ON time only" (vs "one time only")."""
    kw_words = re.findall(r"[a-z0-9]+", k)
    words = re.findall(r"[a-z0-9]+", t)
    head = kw_words[0][:3]
    n = len(kw_words)
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(0, len(words) - size) + 1):
            if not words[i].startswith(head):
                continue
            if fuzz.ratio(k, " ".join(words[i:i + size])) >= threshold:
                return True
    return False


def match_keywords(text: str, keywords: List[str], threshold: int = 90, fuzzy_min_chars: int = 12,
                   variants: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Keywords matching `text`: exact phrase (or a configured spelling variant) starting at a word boundary,
    or (for keywords of at least fuzzy_min_chars) fuzzy partial_ratio >= threshold."""
    t = _normalise(text)
    hits = []
    for kw in keywords:
        k = _normalise(kw)
        forms = [k] + [_normalise(v) for v in (variants or {}).get(kw, [])]
        if any(re.search(r"(?<![a-z0-9])" + re.escape(f), t) for f in forms):
            hits.append(kw)
            continue
        # partial_ratio aligns the SHORTER string inside the longer one, so a 2-char review like "ok"
        # would score 100 against "didn't book again": only fuzzy-match when the text can contain the keyword.
        if threshold < 100 and len(k) >= fuzzy_min_chars and len(t) >= len(k) and fuzz.partial_ratio(k, t) >= threshold:
            if _word_window_match(k, t, threshold):
                hits.append(kw)
    return hits


def kw_settings(cfg: dict):
    km = cfg.get("keyword_match", {}) or {}
    return cfg.get("keywords", []), int(km.get("fuzzy_threshold", 90)), int(km.get("fuzzy_min_chars", 12)), km.get("variants") or {}


class PoliteSession:
    """requests wrapper: fixed User-Agent, delay between requests, no cookies persisted or loaded."""

    def __init__(self, cfg: dict, user_agent: Optional[str] = None):
        http = cfg.get("http", {})
        self.delay = float(http.get("request_delay_seconds", 2.0))
        self.timeout = float(http.get("timeout_seconds", 30))
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent or http.get("user_agent", "UC-Discovery-Engine/0.1")
        self._last = 0.0

    def get(self, url: str, **kw) -> requests.Response:
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):  # retry transient network errors only (timeouts, dropped connections)
            self.s.cookies.clear()  # never carry cookies between requests
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
                self._last = time.time()
                return r
            except (requests.Timeout, requests.ConnectionError):
                self._last = time.time()
                if attempt == 2:
                    raise
                time.sleep(self.delay * 5 * (attempt + 1))


def looks_like_wall(resp: requests.Response) -> Optional[str]:
    """Detect login walls / bot challenges. Returns a reason string, or None if the page looks public."""
    if resp.status_code in (401, 407):
        return "HTTP %d (authentication required)" % resp.status_code
    final = resp.url.lower()
    if any(p in final for p in ("/login", "/signin", "/sign-in", "/users/connect", "/auth")):
        return "redirected to login page: %s" % resp.url
    head = resp.text[:4000].lower() if resp.text else ""
    if "verifying your connection" in head or "awswaf" in head or "cf-challenge" in head or "captcha" in head:
        return "bot challenge page (HTTP %d) at %s" % (resp.status_code, resp.url)
    if resp.status_code == 403:
        return "HTTP 403 forbidden at %s" % resp.url
    return None


def write_jsonl(source: str, rows: Iterable[Dict]) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / ("%s.jsonl" % source)
    rows = list(rows)
    if not rows and out.exists() and out.stat().st_size > 0:
        # A flaky source (e.g. Apple's RSS feed) can return nothing; never replace existing data with an empty fetch.
        print("[%s] fetched 0 items; keeping existing %s" % (source, out.name))
        return out
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for row in rows:
            row.setdefault("source", source)
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    return out


def source_cfg(cfg: dict, name: str) -> dict:
    return (cfg.get("sources") or {}).get(name) or {}
