"""Trustpilot reviews for urbancompany.com (public review pages only).

Real limit found on 2026-11-22: every anonymous request to
https://www.trustpilot.com/review/urbancompany.com (and ?page=2, ?page=11, and urbanclap.com)
returned HTTP 403 with an AWS WAF "Verifying your connection..." JavaScript challenge.
Per CLAUDE.md, this collector does NOT try to get past the challenge (no headless browser, no
proxies, no solvers). It makes one request, detects the challenge, and stops with SourceBlocked.

If the public page is reachable later, reviews are parsed from the page's __NEXT_DATA__ JSON.
Trustpilot is also known to require sign-in past about page 10 for anonymous visitors. If any page
redirects to login, the collector stops at that point and reports the page number.
"""
import json

from bs4 import BeautifulSoup

from collect.common import kw_settings, PoliteSession, SourceBlocked, looks_like_wall, match_keywords, source_cfg, write_jsonl

SOURCE = "trustpilot"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _parse(html: str):
    s = BeautifulSoup(html, "html.parser")
    tag = s.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return []
    data = json.loads(tag.string)
    return ((data.get("props") or {}).get("pageProps") or {}).get("reviews") or []


def run(cfg: dict) -> dict:
    sc = source_cfg(cfg, SOURCE)
    domain = sc.get("domain", "urbancompany.com")
    max_items = int(sc.get("max_items", 200))
    kws, thr, fmin, kvar = kw_settings(cfg)
    http = PoliteSession(cfg, user_agent=BROWSER_UA)

    fetched, kept, page, stop_note = 0, [], 1, None
    while fetched < max_items:
        url = "https://www.trustpilot.com/review/%s" % domain + ("?page=%d" % page if page > 1 else "")
        r = http.get(url)
        wall = looks_like_wall(r)
        if wall:
            if page == 1:
                raise SourceBlocked("Trustpilot: %s. Not bypassing (CLAUDE.md hard constraint)." % wall)
            stop_note = "stopped at page %d: %s" % (page, wall)
            break
        revs = _parse(r.text)
        if not revs:
            break
        for rv in revs:
            fetched += 1
            text = "%s. %s" % (rv.get("title") or "", rv.get("text") or "")
            hits = match_keywords(text, kws, thr, fmin, kvar)
            if hits:
                kept.append({
                    "source": SOURCE, "id": rv.get("id"), "text": text, "rating": rv.get("rating"),
                    "date": (rv.get("dates") or {}).get("publishedDate"), "language": rv.get("language") or "unknown",
                    "matched_keywords": hits, "url": "https://www.trustpilot.com/reviews/%s" % rv.get("id"),
                })
        page += 1
    out = write_jsonl(SOURCE, kept)
    res = {"source": SOURCE, "fetched": fetched, "kept": len(kept), "output": str(out)}
    if stop_note:
        res["limit"] = stop_note
    return res
