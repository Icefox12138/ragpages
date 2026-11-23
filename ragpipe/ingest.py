"""Extract/search stages: PDF directory -> per-source db -> search.db.

This is the self-contained producer core: no runtime imports, no machine
paths. The output follows the classic RAG database contract (the
"documents / chunks / toc" shape, see README.md -> "Output contract"):

  per-source db:
    documents(id, rel, book_id, pages, text_pages, chars, lang, status,
              ocr, ocr_pages, ocr_book_id, probe_cpp)
    chunks(id, book, book_id, lang, page, text, visibility, stream)
    chunks_fts (fts5 trigram, rowid == chunks.id)

  search.db:
    chunks(id, source, book, lang, page, text, visibility, stream)
    chunks_fts (fts5 unicode61, pre-tokenized text, rowid == chunks.id)

  toc (written by `ragpipe.toc_store`) can live in search.db as well.
"""
import csv
import hashlib
import json
import os
import re
import sqlite3
import time

from .parser import get_parser

CJK = re.compile(r"[\u3400-\u9fff]+")
WORD = re.compile(r"[a-z]{2,}")

# Neutral smoke queries; hit-rate is reported, not enforced.
DEFAULT_QUERIES = ["derivative", "limit", "quotient", "微分", "极限"]
CANARY = "CANARY-7F3A answer key: this row must never reach the student view."

DB_LANG = {"ch": "zh", "zh": "zh", "cn": "zh", "en": "en",
           "latin": "latin", "lat": "latin", "grc": "greek", "greek": "greek"}


def sha_sample(path, size):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(2 * 1024 * 1024))
    h.update(str(size).encode())
    return h.hexdigest()


def chunks_of(text, chunk=600, overlap=80):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= chunk:
        yield text
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk)
        cut = max(text.rfind("。", start, end), text.rfind("；", start, end),
                  text.rfind(".", start, end), text.rfind(";", start, end))
        if cut > start + chunk // 2:
            end = cut + 1
        yield text[start:end]
        start = max(start + 1, end - overlap)


def _search(con, q, topk=5):
    cur = con.execute(
        "SELECT c.book, c.lang, c.page, c.text FROM chunks_fts JOIN chunks c "
        "ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ? "
        "AND c.visibility='tutor' ORDER BY bm25(chunks_fts) LIMIT ?",
        ('"' + q.replace('"', '""') + '"', topk))
    return cur.fetchall()


def _db_lang(value, default="zh"):
    """OCR uses engine codes (ch/grc); the database uses zh/greek/latin/en."""
    return DB_LANG.get(str(value or "").lower(), default)


def _ocr_id(rel):
    return hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:16]


def _ocr_pages(ocr_dir, book_id):
    """Pages written by `ragpipe ocr`; empty when the book was never OCR'd."""
    path = os.path.join(str(ocr_dir), book_id, "pages.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _ocr_meta(ocr_dir, book_id):
    path = os.path.join(str(ocr_dir), book_id, "book.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def extract(pdf_root, out_db, probe_csv, manifest_path=None,
            chunk=600, overlap=80, min_len=80, min_cpp=200,
            limit=None, parser="pypdfium2", queries=None, ocr_dir=None):
    """Ingest `pdf_root` (already probed) into `out_db`.

    A book with no text layer falls back to `ocr_dir/<book_id>/pages.jsonl`
    (written by `ragpipe ocr`); its language comes from `book.json` rather than
    a hardcoded default. `limit` is for smoke runs.
    """
    engine = get_parser(parser)
    with open(probe_csv, encoding="utf-8-sig", newline="") as fh:
        rows_csv = list(csv.DictReader(fh))
    books = []
    for r in rows_csv:
        cpp = float(r["cpp"] or 0)
        if cpp >= min_cpp:
            books.append(r)
        elif ocr_dir and _ocr_pages(ocr_dir, _ocr_id(r["rel"])):
            # A scanned book has cpp below the text-layer threshold; it still
            # belongs in the batch when the `ocr` stage already produced pages.
            books.append(r)
    if limit:
        books = books[:int(limit)]

    if os.path.exists(out_db):
        os.remove(out_db)
    os.makedirs(os.path.dirname(str(out_db)) or ".", exist_ok=True)
    con = sqlite3.connect(out_db)
    con.execute("CREATE TABLE documents ("
                "id INTEGER PRIMARY KEY, rel TEXT, book_id TEXT, pages INTEGER,"
                " text_pages INTEGER, chars INTEGER, lang TEXT, status TEXT,"
                " ocr INTEGER NOT NULL DEFAULT 0, ocr_pages INTEGER,"
                " ocr_book_id TEXT, probe_cpp REAL)")
    con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, book TEXT, book_id TEXT,"
                " lang TEXT, page INTEGER, text TEXT, visibility TEXT,"
                " stream TEXT NOT NULL DEFAULT 'text')")
    con.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(text, tokenize='trigram')")

    manifest, all_rows = [], []
    for b in books:
        rel = b["rel"]
        path = os.path.join(str(pdf_root), rel)
        if not os.path.exists(path):
            manifest.append({"rel": rel, "status": "missing"})
            con.execute("INSERT INTO documents (rel, status, probe_cpp)"
                        " VALUES (?,?,?)", (rel, "missing", float(b["cpp"] or 0)))
            continue
        doc = engine.open(path)
        pages = doc.page_count
        text_pages = chars = cjk = 0
        page_texts = []
        for i in range(pages):
            t = doc.text(i) or ""
            if len(t.strip()) < 200:
                continue
            text_pages += 1
            chars += len(t)
            cjk += len(CJK.findall(t))
            page_texts.append((i + 1, t))
        doc.close()
        lang = "zh" if cjk > max(1, chars) * 0.2 else "en"
        bid = sha_sample(path, os.path.getsize(path))[:16]
        # OCR outputs are keyed by the book's relative path.
        ocr_id = _ocr_id(rel)
        used_ocr = False
        if text_pages == 0 and ocr_dir:
            ocr_pages = _ocr_pages(ocr_dir, ocr_id)
            if ocr_pages:
                used_ocr = True
                meta = _ocr_meta(ocr_dir, ocr_id)
                lang = _db_lang(meta.get("lang"), lang)
                page_texts = [(p["page"], p["text"]) for p in ocr_pages
                              if (p.get("text") or "").strip()]
        for pno, t in page_texts:
            for ch in chunks_of(t, chunk, overlap):
                if len(ch) >= min_len:
                    all_rows.append((rel, bid, lang, pno, ch, "tutor"))
        con.execute("INSERT INTO documents (rel, book_id, pages, text_pages,"
                    " chars, lang, status, ocr, ocr_pages, ocr_book_id, probe_cpp)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rel, bid, pages, text_pages, chars, lang, "ingested",
                     int(used_ocr), len(page_texts) if used_ocr else None,
                     ocr_id if used_ocr else None, float(b["cpp"] or 0)))
        manifest.append({"rel": rel, "pages": pages, "text_pages": text_pages,
                         "chars": chars, "book_id": bid, "lang": lang,
                         "status": "ingested", "ocr": used_ocr,
                         **({"ocr_pages": len(page_texts), "ocr_book_id": ocr_id}
                            if used_ocr else {})})

    con.executemany(
        "INSERT INTO chunks (book,book_id,lang,page,text,visibility)"
        " VALUES (?,?,?,?,?,?)", all_rows)
    con.executemany("INSERT INTO chunks_fts (rowid,text) VALUES (?,?)",
                    [(i + 1, r[4]) for i, r in enumerate(all_rows)])
    con.commit()

    qres = []
    for q in (queries or DEFAULT_QUERIES):
        res = _search(con, q)
        qres.append({"q": q, "hits": len(res),
                     "contains": any(q in (r[3] or "") for r in res)})
    summary = {
        "books_csv": len(books),
        "books_ingested": sum(1 for m in manifest if m["status"] == "ingested"),
        "pages": sum(m.get("pages", 0) for m in manifest),
        "text_pages": sum(m.get("text_pages", 0) for m in manifest),
        "chunks": len(all_rows),
        "queries": {"total": len(qres), "hit": sum(1 for x in qres if x["contains"]),
                    "detail": qres},
    }
    con.close()
    if manifest_path:
        os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "books": manifest}, fh,
                      ensure_ascii=False, indent=1)
    return summary


def tokens(s):
    """Pre-tokenize for the unicode61 FTS: CJK bigrams + lowercase words."""
    out = []
    for run in CJK.findall(s):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    out.extend(WORD.findall(s.lower()))
    return out


def _norm_queries(queries):
    if queries is None:
        queries = DEFAULT_QUERIES
    return [q if isinstance(q, str) else q[1] for q in queries]


def build(out_db, sources, canary=True, eval_path=None, queries=None, dbs=None):
    """Write `search.db` from per-source databases.

    `dbs` maps a source id to its database path; missing sources are skipped
    (the fuse keeps the sources that exist). `chunks_fts` stores pre-tokenized
    text, as the downstream contract requires.
    """
    if os.path.exists(out_db):
        os.remove(out_db)
    os.makedirs(os.path.dirname(str(out_db)) or ".", exist_ok=True)
    con = sqlite3.connect(out_db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, source TEXT, book TEXT,"
                " lang TEXT, page INTEGER, text TEXT, visibility TEXT,"
                " stream TEXT NOT NULL DEFAULT 'text')")
    con.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(text, tokenize='unicode61')")

    rows = []
    for source in sources:
        db = (dbs or {}).get(source)
        if not db or not os.path.exists(db):
            continue
        con2 = sqlite3.connect(db)
        try:
            data = con2.execute(
                "SELECT book, lang, page, text, visibility FROM chunks").fetchall()
        finally:
            con2.close()
        rows.extend((source, b, l, p, t, v) for b, l, p, t, v in data)
    if canary:
        rows.append(("canary", "canary", "en", 1, CANARY, "answer_key"))

    con.executemany(
        "INSERT INTO chunks (source,book,lang,page,text,visibility)"
        " VALUES (?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chunks_fts (rowid,text) VALUES (?,?)",
                    [(i + 1, " ".join(tokens(r[4]))) for i, r in enumerate(rows)])
    con.commit()

    def search(q, topk=10):
        cur = con.execute(
            "SELECT c.id, c.text FROM chunks_fts JOIN chunks c"
            " ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ?"
            " AND c.visibility='tutor' ORDER BY bm25(chunks_fts) LIMIT ?",
            ('"' + " ".join(tokens(q)) + '"', topk))
        return cur.fetchall()

    hits, times, detail = 0, [], []
    for q in _norm_queries(queries):
        t0 = time.perf_counter()
        res = search(q)
        times.append((time.perf_counter() - t0) * 1000)
        ok = any(q.lower() in (r[1] or "").lower() for r in res)
        hits += 1 if ok else 0
        detail.append({"q": q, "hit": ok, "n": len(res)})
    times.sort()
    canary_hits = search("CANARY-7F3A", 5)
    out = {
        "chunks": len(rows),
        "queries": len(_norm_queries(queries)),
        "recall@10": round(hits / max(1, len(_norm_queries(queries))), 3),
        "p95_ms": round(times[int(len(times) * 0.95) - 1], 1) if times else 0.0,
        "mean_ms": round(sum(times) / max(1, len(times)), 1),
        "canary_student_hits": sum(1 for r in canary_hits if "CANARY" in r[1]),
        "detail": detail,
    }
    con.close()
    if eval_path:
        os.makedirs(os.path.dirname(eval_path) or ".", exist_ok=True)
        with open(eval_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    return out
