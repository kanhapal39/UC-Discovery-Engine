"""Urban Company shareholder letters and earnings-call transcripts (public investor-relations PDFs).

Source: https://investorrelations.urbancompany.com/financials (www.urbancompany.com/investor-relations
redirects there; its robots.txt is `Allow: /`). The PDFs are public S3 objects
(uc-investor-reports.s3...amazonaws.com). Checked on 2026-11-22: 4 shareholder letters
(Q3 FY26-Q2 FY27) and 5 call transcripts were linked, with no login needed.

PDFs are cached in data/raw/shareholder_letters_pdfs/ and text is extracted with pypdf, then
split into paragraphs; one JSONL row per paragraph. By default all paragraphs are kept
(apply_keyword_filter: false), because the shared complaint keywords rarely appear in investor documents.
"""
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from collect.common import kw_settings, RAW_DIR, PoliteSession, SourceBlocked, looks_like_wall, match_keywords, source_cfg, write_jsonl

SOURCE = "shareholder_letters"


def _doc_links(html: str, patterns):
    s = BeautifulSoup(html, "html.parser")
    out = []
    for a in s.find_all("a", href=True):
        h = a["href"]
        if h.lower().endswith(".pdf") and any(p in unquote(h).lower() for p in patterns) and h not in out:
            out.append(h)
    return out


def _paragraphs(text: str, chunk_chars: int = 600):
    text = text.replace("\r", "")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    # Earnings-call transcript PDFs extract one word per line: rejoin, then group sentences into ~600-char chunks.
    if lines and sum(len(l) for l in lines) / len(lines) < 20:
        buf = ""
        for sent in re.split(r"(?<=[.?!])\s+", " ".join(lines)):
            buf = (buf + " " + sent).strip()
            if len(buf) >= chunk_chars:
                yield buf
                buf = ""
        if len(buf) >= 80:
            yield buf
        return
    for chunk in re.split(r"\n\s*\n|(?<=[.?!])\n(?=[A-Z])", text):
        p = re.sub(r"\s+", " ", chunk).strip()
        if len(p) >= 80:
            yield p


def run(cfg: dict) -> dict:
    sc = source_cfg(cfg, SOURCE)
    max_items = int(sc.get("max_items", 2000))
    kws, thr, fmin, kvar = kw_settings(cfg)
    use_filter = bool(sc.get("apply_keyword_filter", False))
    http = PoliteSession(cfg)

    r = http.get(sc.get("financials_url", "https://investorrelations.urbancompany.com/financials"))
    wall = looks_like_wall(r)
    if wall:
        raise SourceBlocked("investor relations: %s" % wall)
    links = _doc_links(r.text, [p.lower() for p in sc.get("doc_patterns", ["letter", "transcript"])])

    cache = RAW_DIR / "shareholder_letters_pdfs"
    cache.mkdir(parents=True, exist_ok=True)
    rows, docs = [], []
    for url in links:
        name = Path(unquote(urlparse(url).path)).name.replace(" ", "_").replace("+", "_")
        path = cache / name
        if not path.exists():
            d = http.get(url)
            if d.status_code != 200 or not d.content.startswith(b"%PDF"):
                docs.append({"url": url, "status": d.status_code})
                continue
            path.write_bytes(d.content)
        doc_type = "transcript" if "transcript" in name.lower() else "shareholder_letter"
        n = 0
        for pi, page in enumerate(PdfReader(str(path)).pages, start=1):
            for k, para in enumerate(_paragraphs(page.extract_text() or "")):
                hits = match_keywords(para, kws, thr, fmin, kvar)
                if use_filter and not hits:
                    continue
                if len(rows) >= max_items:
                    break
                rows.append({"source": SOURCE, "id": "%s#p%d.%d" % (name, pi, k), "text": para, "rating": None,
                             "date": None, "language": "en", "doc_type": doc_type, "document": name,
                             "page": pi, "matched_keywords": hits, "url": url})
                n += 1
        docs.append({"document": name, "paragraphs": n})
    out = write_jsonl(SOURCE, rows)
    return {"source": SOURCE, "documents": len(links), "kept": len(rows), "per_document": docs, "output": str(out)}
