"""consumercomplaints.in complaints about Urban Company (public brand page, no login).

Brand page: https://www.consumercomplaints.in/urban-company-b115885 (found via the site's public
brand sitemap, bnames_*.xml). The list is paginated as /page/N, about 20 complaints per page.
Full text is on each complaint's detail page in [itemprop=reviewBody], with the date in
[itemprop=datePublished].

Access checked on 2026-11-22: list pages 1-2 and detail pages returned HTTP 200 with full text
and no login wall. robots.txt disallows /?search=*, /profile-*, /ajax.php and /statistics/*, so
this collector never uses search, profiles or AJAX endpoints; it only walks the brand page.
PII warning: complaint bodies often contain addresses and phone numbers. pipeline/clean.py
must redact these before anything leaves data/raw/.
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from collect.common import kw_settings, PoliteSession, SourceBlocked, looks_like_wall, match_keywords, source_cfg, write_jsonl

SOURCE = "consumer_complaints"
BASE = "https://www.consumercomplaints.in"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DETAIL_RE = re.compile(r"^/[a-z0-9-]+-c(\d+)$")


def _list_links(html: str):
    s = BeautifulSoup(html, "html.parser")
    out = []
    for a in s.find_all("a", href=True):
        m = DETAIL_RE.match(a["href"])
        if m and a["href"] not in out:
            out.append(a["href"])
    return out


def run(cfg: dict) -> dict:
    sc = source_cfg(cfg, SOURCE)
    brand = sc.get("brand_url", BASE + "/urban-company-b115885").rstrip("/")
    max_items = float("inf") if sc.get("max_items") is None else int(sc["max_items"])  # null = no cap
    kws, thr, fmin, kvar = kw_settings(cfg)
    http = PoliteSession(cfg, user_agent=BROWSER_UA)

    seen, kept, page, stop_note = [], [], 1, None
    while len(seen) < max_items:
        r = http.get(brand if page == 1 else "%s/page/%d" % (brand, page))
        wall = looks_like_wall(r)
        if wall:
            if page == 1:
                raise SourceBlocked("consumercomplaints.in: %s" % wall)
            stop_note = "stopped at list page %d: %s" % (page, wall)
            break
        if r.status_code != 200:
            break
        links = [l for l in _list_links(r.text) if l not in seen]
        if not links:
            break
        for href in links:
            if len(seen) >= max_items:
                break
            seen.append(href)
            d = http.get(urljoin(BASE, href))
            wall = looks_like_wall(d)
            if wall:
                stop_note = "detail page blocked: %s" % wall
                break
            s = BeautifulSoup(d.text, "html.parser")
            body = s.select_one("[itemprop=reviewBody]")
            if not body:
                continue
            title = s.title.get_text(strip=True) if s.title else ""
            text = "%s. %s" % (title, body.get_text(" ", strip=True))
            dt = s.select_one("[itemprop=datePublished]")
            hits = match_keywords(text, kws, thr, fmin, kvar)
            if True:  # save every fetched item; filter.py applies the keyword rule
                kept.append({
                    "source": SOURCE, "id": DETAIL_RE.match(href).group(1), "text": text, "rating": None,
                    "date": dt.get("content") if dt else None, "language": "unknown",
                    "resolved": title.startswith("[Resolved]"),
                    "matched_keywords": hits, "url": urljoin(BASE, href),
                })
        if stop_note:
            break
        page += 1
    out = write_jsonl(SOURCE, kept)
    res = {"source": SOURCE, "fetched": len(seen), "list_pages": page, "saved": len(kept), "matched_keywords": sum(1 for x in kept if x["matched_keywords"]), "output": str(out)}
    if stop_note:
        res["limit"] = stop_note
    return res
