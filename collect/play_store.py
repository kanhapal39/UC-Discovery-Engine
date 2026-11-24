"""Google Play Store reviews for Urban Company, via google-play-scraper (public listing, no login).

Package id: com.urbanclap.urbanclap ("Urban Company: Home Services", developer Urban Company,
10M+ installs). Verified on 2026-11-22 from the public Play search results page and
google_play_scraper.app(). Note: google_play_scraper.search() returns appId=None for the
featured top result, so the id was read from the search page's details?id= links.

Fetches up to `max_items` newest reviews (paged with continuation tokens), then keeps only
reviews matching config keywords. Language is written as "unknown"; detection happens in pipeline/.
"""
import time

from google_play_scraper import Sort, reviews
from tqdm import tqdm

from collect.common import kw_settings, match_keywords, source_cfg, write_jsonl

SOURCE = "play_store"
BATCH = 200


def run(cfg: dict) -> dict:
    sc = source_cfg(cfg, SOURCE)
    app_id = sc.get("app_id", "com.urbanclap.urbanclap")
    max_items = int(sc.get("max_items", 1000))
    kws, thr, fmin, kvar = kw_settings(cfg)
    delay = float(cfg.get("http", {}).get("request_delay_seconds", 2.0))  # polite gap between 200-review batches

    fetched, kept, token = 0, [], None
    with tqdm(total=max_items, desc=SOURCE) as bar:
        while fetched < max_items:
            if fetched:
                time.sleep(delay)
            batch, token = reviews(
                app_id, lang=sc.get("lang", "en"), country=sc.get("country", "in"),
                sort=Sort.NEWEST, count=min(BATCH, max_items - fetched), continuation_token=token,
            )
            if not batch:
                break
            fetched += len(batch)
            bar.update(len(batch))
            for r in batch:
                hits = match_keywords(r.get("content") or "", kws, thr, fmin, kvar)
                if True:  # save every fetched item; filter.py applies the keyword rule
                    kept.append({
                        "source": SOURCE,
                        "id": r["reviewId"],
                        "text": r.get("content"),
                        "rating": r.get("score"),
                        "date": r["at"].isoformat() if r.get("at") else None,
                        "language": "unknown",
                        "app_version": r.get("reviewCreatedVersion"),
                        "thumbs_up": r.get("thumbsUpCount"),
                        "matched_keywords": hits,
                        "url": "https://play.google.com/store/apps/details?id=%s&reviewId=%s" % (app_id, r["reviewId"]),
                    })
            if token is None or getattr(token, "token", None) is None:
                break
    out = write_jsonl(SOURCE, kept)
    return {"source": SOURCE, "fetched": fetched, "saved": len(kept), "matched_keywords": sum(1 for x in kept if x["matched_keywords"]), "output": str(out)}
