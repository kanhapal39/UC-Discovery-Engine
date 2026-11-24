"""YouTube comments on public videos about Urban Company, via the official YouTube Data API v3.

Needs YOUTUBE_API_KEY in .env. This is a Google Cloud developer key for a public API, not a
login to UC or YouTube. Only public comments on public videos are read; no OAuth, no cookies.

Quota: search.list costs 100 units per call and commentThreads.list costs 1. The default free
quota is 10,000 units/day, so the 3 default queries use about 300 units plus about 1 per
100-comment page. Videos with comments disabled return 403 commentsDisabled and are skipped.

Status (2026-11-22): written to the API contract but NOT yet run, because no API key is configured.
"""
import os

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from collect.common import kw_settings, ROOT, SourceBlocked, match_keywords, source_cfg, write_jsonl

SOURCE = "youtube"


def run(cfg: dict) -> dict:
    load_dotenv(ROOT / ".env")
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        raise SourceBlocked("YOUTUBE_API_KEY not set in .env (developer API key needed; no login used)")
    sc = source_cfg(cfg, SOURCE)
    max_items = int(sc.get("max_items", 2000))
    kws, thr, fmin, kvar = kw_settings(cfg)
    yt = build("youtube", "v3", developerKey=key, cache_discovery=False)

    videos = {}
    for q in sc.get("search_queries", ["Urban Company review"]):
        res = yt.search().list(q=q, part="id,snippet", type="video", maxResults=int(sc.get("max_videos_per_query", 10)),
                               regionCode=sc.get("region_code", "IN"), relevanceLanguage="en").execute()
        for it in res.get("items", []):
            videos[it["id"]["videoId"]] = it["snippet"]["title"]

    fetched, kept, skipped = 0, [], []
    for vid, vtitle in videos.items():
        token = None
        while fetched < max_items:
            try:
                res = yt.commentThreads().list(videoId=vid, part="snippet", maxResults=100, pageToken=token,
                                               textFormat="plainText", order="relevance").execute()
            except HttpError as e:
                skipped.append({"video": vid, "reason": str(e.resp.status)})
                break
            for it in res.get("items", []):
                fetched += 1
                c = it["snippet"]["topLevelComment"]["snippet"]
                text = c.get("textDisplay", "")
                hits = match_keywords(text, kws, thr, fmin, kvar)
                if hits:
                    kept.append({
                        "source": SOURCE, "id": it["id"], "text": text, "rating": None,
                        "date": c.get("publishedAt"), "language": "unknown", "likes": c.get("likeCount"),
                        "video_id": vid, "video_title": vtitle, "matched_keywords": hits,
                        "url": "https://www.youtube.com/watch?v=%s&lc=%s" % (vid, it["id"]),
                    })
            token = res.get("nextPageToken")
            if not token:
                break
        if fetched >= max_items:
            break
    out = write_jsonl(SOURCE, kept)
    return {"source": SOURCE, "videos": len(videos), "fetched": fetched, "kept": len(kept),
            "videos_skipped": skipped, "output": str(out)}
