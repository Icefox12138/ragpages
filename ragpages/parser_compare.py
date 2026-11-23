"""Page-level comparison of two PDF parsers on a set of PDFs.

    python -m ragpages.parser_compare --db data/indexes/books.db \
        --pdf-root pdf/books --limit-books 5 --out /tmp/parser-compare.json

Why page-level, not chunk-level: chunk boundaries are a function of the raw
text, so one reordered span shifts every later chunk and makes chunk equality
uninformative. The page is the citation anchor; compare text per page.

Per page it reports:
  * `equal`    — whitespace-normalized texts are identical;
  * `multiset` — same characters, different order (a layout-order difference);
  * `ratio`    — similarity of the normalized texts (first 4000 chars).
Aggregates count pages with content, so blank pages do not dilute the rate.
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
from collections import Counter

from .parser import get_parser

WS = re.compile(r"\s+")


def norm(text):
    return WS.sub("", text or "")


def page_texts(parser, pdf, max_pages=0):
    doc = parser.open(pdf)
    try:
        count = doc.page_count
        if max_pages:
            count = min(count, max_pages)
        return [doc.text(i) or "" for i in range(count)]
    finally:
        doc.close()


def compare_book(pdf, max_pages=0):
    mu, pf = get_parser("pymupdf"), get_parser("pypdfium2")
    a, b = page_texts(mu, pdf, max_pages), page_texts(pf, pdf, max_pages)
    pages = 0
    equal = multiset = 0
    ratios = []
    first = None
    for i, (ta, tb) in enumerate(zip(a, b)):
        na, nb = norm(ta), norm(tb)
        if len(na) < 50 and len(nb) < 50:
            continue                      # blank/cover page; not an anchor
        pages += 1
        if na == nb:
            equal += 1
        if Counter(na) == Counter(nb):
            multiset += 1
        ratios.append(difflib.SequenceMatcher(None, na[:4000], nb[:4000],
                                              autojunk=False).ratio())
        if first is None and na != nb:
            first = {"page": i + 1, "len_pymupdf": len(na), "len_pypdfium2": len(nb),
                     "pymupdf": na[:80], "pypdfium2": nb[:80]}
    return {
        "pages_same_count": len(a) == len(b),
        "content_pages": pages,
        "pages_equal": equal,
        "pages_multiset_equal": multiset,
        "equal_rate": round(equal / max(1, pages), 4),
        "multiset_rate": round(multiset / max(1, pages), 4),
        "mean_ratio": round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
        "min_ratio": round(min(ratios), 4) if ratios else 0.0,
        "first_mismatch": first,
    }


def db_books(db):
    con = sqlite3.connect(db)
    try:
        return [(r[0], r[1]) for r in con.execute(
            "SELECT book, count(*) FROM chunks WHERE visibility='tutor'"
            " GROUP BY book ORDER BY count(*) DESC")]
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="a per-source db (chunks table)")
    ap.add_argument("--pdf-root", required=True, help="PDF directory the db was built from")
    ap.add_argument("--limit-books", type=int, default=0)
    ap.add_argument("--max-pages", type=int, default=0, help="0 = every page")
    ap.add_argument("--book", help="one relative path")
    ap.add_argument("--books-from", help="a <source>-manifest.json: text-layer books only")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    books = [(args.book, 0)] if args.book else db_books(args.db)
    if args.books_from:
        with open(args.books_from, encoding="utf-8") as fh:
            manifest = json.load(fh)
        books = [(b["rel"], 0) for b in manifest.get("books", [])
                 if b.get("text_pages", 0) > 0]
    if args.limit_books:
        books = books[:args.limit_books]
    report = []
    for rel, _ in books:
        pdf = os.path.join(args.pdf_root, rel)
        if not os.path.exists(pdf):
            report.append({"book": rel, "status": "missing"})
            continue
        row = compare_book(pdf, args.max_pages)
        row.update({"book": rel, "status": "ok"})
        report.append(row)
        print("equal=%.3f multiset=%.3f ratio=%.3f pages=%d %s"
              % (row["equal_rate"], row["multiset_rate"], row["mean_ratio"],
                 row["content_pages"], rel[:50]), flush=True)

    ok = [r for r in report if r.get("status") == "ok"]
    summary = {
        "books": len(report), "books_ok": len(ok),
        "content_pages": sum(r["content_pages"] for r in ok),
        "equal_rate": round(sum(r["pages_equal"] for r in ok)
                            / max(1, sum(r["content_pages"] for r in ok)), 4),
        "multiset_rate": round(sum(r["pages_multiset_equal"] for r in ok)
                               / max(1, sum(r["content_pages"] for r in ok)), 4),
        "mean_ratio": round(sum(r["mean_ratio"] for r in ok) / max(1, len(ok)), 4),
        "books_bytes_identical": sum(1 for r in ok if r["equal_rate"] == 1.0),
        "books_multiset_identical": sum(1 for r in ok if r["multiset_rate"] == 1.0),
    }
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "books": report}, fh,
                      ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
