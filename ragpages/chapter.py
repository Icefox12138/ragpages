"""Chapter stage: header / 回 OCR -> `toc` rows, via `classics-index` only.

No runtime import: the pure logic is the published package `classics-index`
(PyPI, >= 0.1), the storage is `ragpages.toc_store`. A downstream RAG runtime
keeps reading the same `toc` table.

Inputs (JSONL, one record per page):
  headers: {"page": 12, "vol": 3}            or {"page": 12, "lines": [{"text": ...}]}
  hui:     {"page": 12, "texts": ["第五十九回", "唐三藏路阻火焰山"]}
"""
import json

from classics_index import core, hui

from . import toc_store


def _vol(rec):
    vol = rec.get("vol")
    if vol is not None:
        return core.cn2int(vol) if isinstance(vol, str) else int(vol)
    lines = rec.get("lines") or ([{"text": rec["text"]}] if rec.get("text") else [])
    return core.volume_of(lines)


def _lines(rec):
    return rec.get("lines") or ([{"text": rec["text"]}] if rec.get("text") else [])


def rows_from_headers(records, min_run=5):
    """卷/篇 rows: contiguous page ranges, 篇 names attached where a page has one."""
    records = [r for r in records if r.get("page")]
    pages = {int(r["page"]): _lines(r) for r in records}
    vols = {int(r["page"]): _vol(r) for r in records}
    chapters = core.build_from_volumes(core.clean_volumes(vols, min_run=min_run))
    chapters = core.enrich(chapters, pages)
    return chapters, {"pages": len(records), "min_run": min_run}


def rows_from_hui(records, names=None):
    """回 rows for one or more novels; `names` maps book number -> title."""
    books = hui.build(records, names=names or {})
    rows = []
    for book_no, part in enumerate(books, 1):
        for r in part:
            rows.append({
                "ord": len(rows), "level": 2,
                "title": r.get("title_text") or r.get("title") or "回%d" % r["num"],
                "page_start": r["page_start"], "page_end": r["page_end"],
                "volume": None, "pages": r["page_end"] - r["page_start"] + 1,
                "book": book_no, "num": r["num"],
            })
    return rows, {"pages": len(records), "books": len(books), "replies": len(rows)}


def rows_from_toc(data):
    """Import an already-built toc JSON ({"chapters": [...]} or a list)."""
    rows = data.get("chapters") if isinstance(data, dict) else data
    rows = list(rows or [])
    for i, row in enumerate(rows):
        row.setdefault("ord", i)
        row.setdefault("level", 1)
        row.setdefault("pages", row["page_end"] - row["page_start"] + 1)
    return rows, {"rows": len(rows)}


def write(db, source, book, rows, meta=None):
    return toc_store.save(source, book, rows, meta=meta, path=db)
