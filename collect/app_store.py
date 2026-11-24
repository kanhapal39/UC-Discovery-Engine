"""Apple App Store reviews for Urban Company (app id 1032480595), from Apple's public customer-review RSS feed.

Real limits found on 2026-11-22 (anonymous requests, no login):
- The feed is capped by Apple at 10 pages x 50 reviews = 500 per storefront.
- The India storefront (`/in/`) returned an EMPTY feed (0 entries) for every URL variant tried
  (paged, unpaged, mostrecent, mosthelpful, json, xml), even though the app lists ~1.25M ratings
  in India. Most of UC's iOS reviews can't be reached through this public feed.
- `/us/` returned 50 entries on the unpaged URL but 0 on page=2..10; ae/gb returned 0. The feed
  is flaky, so every storefront in config is tried and results are deduplicated.
- The `app-store-scraper` package (in requirements.txt) returned 0 reviews with a JSON decode
  error; Apple changed the endpoint it relies on. It isn't used here.
- apps.apple.com product pages embed about 10 reviews in HTML. Not used yet (low yield).
"""
from collect.common import kw_settings, PoliteSession, match_keywords, source_cfg, write_jsonl

SOURCE = "app_store"


def _feed_urls(country: str, app_id: int):
    yield "https://itunes.apple.com/%s/rss/customerreviews/id=%s/json" % (country, app_id)
    for p in range(1, 11):
        yield ("https://itunes.apple.com/%s/rss/customerreviews/page=%d/id=%s/sortby=mostrecent/json"
               % (country, p, app_id))


def run(cfg: dict) -> dict:
    sc = source_cfg(cfg, SOURCE)
    app_id = sc.get("app_id", 1032480595)
    max_items = int(sc.get("max_items", 500))
    kws, thr, fmin, kvar = kw_settings(cfg)
    http = PoliteSession(cfg)

    seen, kept, per_country = set(), [], {}
    for c in sc.get("countries", ["in"]):
        got, empty_pages = 0, 0
        for url in _feed_urls(c, app_id):
            if len(seen) >= max_items:
                break
            r = http.get(url)
            if r.status_code != 200:
                break
            entries = (r.json().get("feed") or {}).get("entry") or []
            if isinstance(entries, dict):
                entries = [entries]
            entries = [e for e in entries if "im:rating" in e]  # the first entry can be app metadata
            if not entries:
                empty_pages += 1
                if empty_pages >= 2 and got == 0 or empty_pages >= 3:
                    break
                continue
            for e in entries:
                rid = e["id"]["label"]
                if rid in seen:
                    continue
                seen.add(rid)
                got += 1
                text = "%s. %s" % (e.get("title", {}).get("label", ""), e.get("content", {}).get("label", ""))
                hits = match_keywords(text, kws, thr, fmin, kvar)
                if True:  # save every fetched item; filter.py applies the keyword rule
                    kept.append({
                        "source": SOURCE, "id": rid, "text": text,
                        "rating": int(e["im:rating"]["label"]),
                        "date": e.get("updated", {}).get("label"),
                        "language": "unknown", "country": c,
                        "app_version": e.get("im:version", {}).get("label"),
                        "matched_keywords": hits,
                        "url": "https://apps.apple.com/%s/app/id%s" % (c, app_id),
                    })
        per_country[c] = got
    out = write_jsonl(SOURCE, kept)
    return {"source": SOURCE, "fetched": len(seen), "fetched_per_country": per_country,
            "saved": len(kept), "matched_keywords": sum(1 for x in kept if x["matched_keywords"]), "output": str(out)}
