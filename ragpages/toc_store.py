"""The `toc` table, written by the pipeline itself.

A downstream RAG runtime reads `toc` for chapter navigation (the classic
"toc" sidecar table); the pipeline writes the same table so no runtime code
has to be imported here. It is a dozen lines of SQLite: the pipeline writes
the table, and the runtime reads it back through its own loader.

Schema is byte-compatible with the downstream reader's DDL:
  toc(source, book, ord, level, title, page_start, page_end, volume, pages, meta)
"""
import json
import os
import sqlite3

DDL = (
    "CREATE TABLE IF NOT EXISTS toc ("
    "source TEXT, book TEXT, ord INTEGER, level INTEGER, title TEXT,"
    "page_start INTEGER, page_end INTEGER, volume INTEGER, pages INTEGER,"
    "meta TEXT, PRIMARY KEY(source, book, ord))")


def _conn(path=None):
    if not path:
        raise SystemExit("toc_store needs a database path")
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(DDL)
    con.commit()
    return con


def save(source, book, chapters, meta=None, path=None):
    con = _conn(path)
    try:
        con.execute("DELETE FROM toc WHERE source=? AND book=?", (source, book))
        for ch in chapters:
            con.execute(
                "INSERT INTO toc(source,book,ord,level,title,page_start,page_end,"
                "volume,pages,meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (source, book, ch.get("ord", 0), ch.get("level", 1), ch["title"],
                 ch["page_start"], ch["page_end"], ch.get("volume"),
                 ch.get("pages", ch["page_end"] - ch["page_start"] + 1),
                 json.dumps(meta or {}, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()
    return len(chapters)


def load(source, book, path=None):
    con = _conn(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM toc WHERE source=? AND book=? ORDER BY ord",
            (source, book)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]
