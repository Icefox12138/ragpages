"""ragpages CLI: PDF directory (+ OCR) -> RAG database.

    ragpages build    --profiles books --out data/
    ragpages extract  --profile books --out data/ [--limit 1]
    ragpages search   --out data/ --sources books
    ragpages validate --out data/
    ragpages ocr      --profile books-scan --out data/ --books-from books.jsonl
    ragpages chapter  --source books --book a.pdf --toc toc.json
    ragpages meta     --in book_meta.json --out data/
    ragpages selftest

`build` is extract per profile followed by search. Everything takes a profile,
so no code path names a corpus directory.
"""
import argparse
import json
import os
import shutil
import sqlite3
import tempfile

from . import config as pcfg
from . import ingest
from .parser import get_parser


def _writable_tmp():
    """A fresh scratch dir in the system temp; best-effort removal by callers."""
    return tempfile.mkdtemp(prefix="ragpages-")


def _db_path(out_dir, source):
    path = os.path.join(str(out_dir), "indexes", source + ".db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def cmd_extract(args):
    cfg = pcfg.load(args.config)
    prof = pcfg.profile(cfg, args.profile)
    out = pcfg.data_dir(cfg, args.out)
    root = args.pdf_root or prof["root"]
    probe = args.probe or prof["probe"]
    if not probe or not os.path.exists(str(probe)):
        raise SystemExit("profile %r needs a probe csv (--probe); look for %s"
                         % (prof["name"], probe))
    if not os.path.isdir(str(root)):
        raise SystemExit("pdf root does not exist: %s" % root)
    summary = ingest.extract(
        pdf_root=str(root),
        out_db=_db_path(out, prof["source"]),
        probe_csv=str(probe),
        manifest_path=os.path.join(str(out), "indexes",
                                   prof["source"] + "-manifest.json"),
        chunk=prof["chunk"]["size"], overlap=prof["chunk"]["overlap"],
        min_len=prof["chunk"]["min_len"], min_cpp=prof["min_cpp"],
        limit=args.limit, parser=args.parser or prof["parser"],
        ocr_dir=args.ocr_dir or os.path.join(str(out), "ocr"))
    print(json.dumps({"profile": prof["name"], "summary": {
        k: summary[k] for k in ("books_csv", "books_ingested", "text_pages", "chunks")}},
        ensure_ascii=False))
    return 0


def cmd_search(args):
    cfg = pcfg.load(args.config)
    out = pcfg.data_dir(cfg, args.out)
    sources = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    if not sources:
        raise SystemExit("search needs --sources (comma-separated source ids)")
    dbs = {src: os.path.join(str(out), "indexes", src + ".db")
           for src in sources}
    result = ingest.build(
        out_db=os.path.join(str(out), "indexes", "search.db"),
        sources=sources, dbs=dbs,
        eval_path=os.path.join(str(out), "indexes", "search-eval.json"))
    print(json.dumps({k: v for k, v in result.items() if k != "detail"},
                     ensure_ascii=False))
    return 0


def _book_list(books_from, limit):
    """Rows `{rel, pages}` for the OCR batch; a `{rel,pages}` JSONL is required."""
    if not books_from or not os.path.exists(books_from):
        raise SystemExit("book list not found: %s (pass --books-from)" % books_from)
    rows = []
    with open(books_from, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "tier" in rec and rec.get("tier") != 1:
                continue
            if rec.get("vertical"):
                continue
            rel = rec.get("rel") or rec.get("path")
            if rel:
                rows.append({"rel": rel, "pages": int(rec.get("pages") or 0)})
    rows.sort(key=lambda r: r["pages"])
    return rows[:limit] if limit else rows


def _qc(pages, qc_cfg):
    chars = [len(p.get("text") or "") for p in pages]
    blank = sum(1 for c in chars if c < 20)
    dup_pairs = total_pairs = 0
    for p in pages:
        lines = [l.strip() for l in (p.get("text") or "").splitlines() if l.strip()]
        for a, b in zip(lines, lines[1:]):
            total_pairs += 1
            dup_pairs += 1 if a == b else 0
    cjk = sum(1 for ch in "".join(p.get("text") or "" for p in pages)
              if "\u3400" <= ch <= "\u9fff")
    out = {
        "pages_ocr": len(pages),
        "chars_median": sorted(chars)[len(chars) // 2] if chars else 0,
        "blank_rate": round(blank / max(1, len(pages)), 4),
        "dup_line_rate": round(dup_pairs / max(1, total_pairs), 4),
        "cjk_ratio": round(cjk / max(1, sum(chars)), 4),
    }
    if qc_cfg:
        failed = []
        if "blank_rate" in qc_cfg and out["blank_rate"] > float(qc_cfg["blank_rate"]):
            failed.append("blank_rate")
        if "dup_line_rate" in qc_cfg and out["dup_line_rate"] > float(qc_cfg["dup_line_rate"]):
            failed.append("dup_line_rate")
        if "min_cjk" in qc_cfg and out["cjk_ratio"] < float(qc_cfg["min_cjk"]):
            failed.append("cjk_ratio")
        out["qc_failed"] = failed
    return out


def cmd_ocr(args):
    """Render pages and run the configured OCR engine into `<out>/ocr/`."""
    import hashlib
    import time

    from .ocr.base import PageImage
    from .ocr.registry import get_engine

    cfg = pcfg.load(args.config)
    prof = pcfg.profile(cfg, args.profile)
    out = pcfg.data_dir(cfg, args.out)
    ocr_cfg = dict(prof["ocr"])
    if args.engine:
        ocr_cfg["engine"] = args.engine
    if args.dpi:
        ocr_cfg["dpi"] = int(args.dpi)
    engine = get_engine(ocr_cfg["engine"], models=ocr_cfg["models"],
                        backend=ocr_cfg["backend"], params=ocr_cfg["params"])
    model_sha = engine.model_sha256()

    ocr_root = os.path.join(str(out), "ocr")
    os.makedirs(ocr_root, exist_ok=True)
    state = sqlite3.connect(os.path.join(ocr_root, "state.db"))
    state.execute("CREATE TABLE IF NOT EXISTS books("
                  "rel TEXT PRIMARY KEY, book_id TEXT, pages_expected INT,"
                  " pages_done INT, status TEXT, started TEXT, finished TEXT, error TEXT)")
    state.commit()

    books = _book_list(args.books_from, args.books)
    for b in books:
        rel = b["rel"]
        book_id = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
        bdir = os.path.join(ocr_root, book_id)
        os.makedirs(bdir, exist_ok=True)
        pages_file = os.path.join(bdir, "pages.jsonl")
        if os.path.exists(os.path.join(bdir, "DONE")):
            print("skip (done)", rel[:50], flush=True)
            continue
        path = os.path.join(str(prof["root"]), rel)
        if not os.path.exists(path):
            state.execute("INSERT OR REPLACE INTO books VALUES(?,?,?,?,?,?,?,?)",
                          (rel, book_id, b["pages"], 0, "MISSING", None, None, "not found"))
            state.commit()
            print("MISSING", rel[:60], flush=True)
            continue

        started = time.strftime("%Y-%m-%d %H:%M:%S")
        state.execute("INSERT OR REPLACE INTO books VALUES(?,?,?,?,?,?,?,?)",
                      (rel, book_id, b["pages"], 0, "RUNNING", started, None, None))
        state.commit()
        pages = []
        try:
            doc = get_parser(args.parser or prof["parser"]).open(path)
            total = doc.page_count
            limit = min(total, args.max_pages) if args.max_pages else total
            with open(pages_file, "w", encoding="utf-8", newline="\n") as fh:
                for pno in range(1, limit + 1):
                    bgr, width, height = doc.render_bgr(pno - 1, ocr_cfg["dpi"])
                    res = next(iter(engine.run([PageImage(page=pno, image=bgr,
                                                          width=width,
                                                          height=height)])))
                    rec = res.as_dict()
                    rec.update({"engine": engine.name, "engine_version": engine.version,
                                "backend": ocr_cfg["backend"], "dpi": ocr_cfg["dpi"],
                                "model_sha256": model_sha})
                    pages.append(rec)
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
            doc.close()
        except Exception as exc:
            state.execute("UPDATE books SET status=?, error=? WHERE rel=?",
                          ("ERROR", ("%s: %s" % (type(exc).__name__, exc))[:300], rel))
            state.commit()
            print("ERROR", book_id, type(exc).__name__, str(exc)[:160], flush=True)
            continue

        q = _qc(pages, ocr_cfg["qc"])
        q.update({"rel": rel, "book_id": book_id, "dpi": ocr_cfg["dpi"],
                  "lang": ocr_cfg["lang"],
                  "engine": engine.name, "engine_version": engine.version,
                  "backend": ocr_cfg["backend"], "model_sha256": model_sha,
                  "pages_expected": total})
        with open(os.path.join(bdir, "book.json"), "w", encoding="utf-8") as fh:
            json.dump(q, fh, ensure_ascii=False, indent=1)
        open(os.path.join(bdir, "DONE"), "w").close()
        state.execute("UPDATE books SET pages_done=?, status=?, finished=? WHERE rel=?",
                      (len(pages), "QC_FAIL" if q.get("qc_failed") else "DONE",
                       time.strftime("%Y-%m-%d %H:%M:%S"), rel))
        state.commit()
        print("done %s %d/%d blank=%.2f dup=%.2f cjk=%.2f"
              % (book_id, len(pages), total, q["blank_rate"], q["dup_line_rate"],
                 q["cjk_ratio"]), flush=True)
    state.close()
    print(json.dumps({"profile": prof["name"], "books": len(books),
                      "engine": engine.name, "engine_version": engine.version,
                      "backend": ocr_cfg["backend"], "model_sha256": model_sha},
                     ensure_ascii=False))
    return 0


def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def cmd_chapter(args):
    """Header/回 OCR JSONL -> `toc` rows (classics-index, no runtime import)."""
    from . import chapter as ch

    cfg = pcfg.load(args.config)
    out = pcfg.data_dir(cfg, args.out)
    db = args.db or os.path.join(str(out), "indexes", "search.db")
    if args.headers:
        rows, info = ch.rows_from_headers(_read_jsonl(args.headers), args.min_run)
    elif args.hui:
        names = {}
        for part in (args.names or "").split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                names[int(k)] = v.strip()
        rows, info = ch.rows_from_hui(_read_jsonl(args.hui), names)
    elif args.toc:
        with open(args.toc, encoding="utf-8-sig") as fh:
            rows, info = ch.rows_from_toc(json.load(fh))
    else:
        raise SystemExit("give one of --headers / --hui / --toc")
    n = ch.write(db, args.source, args.book, rows, meta=info)
    if args.out_json:
        os.makedirs(os.path.dirname(args.out_json) or ".", exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as fh:
            json.dump({"source": args.source, "book": args.book,
                       "chapters": rows, "info": info}, fh,
                      ensure_ascii=False, indent=1)
    print(json.dumps({"db": db, "source": args.source, "book": args.book,
                      "rows": n, "info": info}, ensure_ascii=False))
    return 0


def cmd_meta(args):
    """Validate `book_meta.json` and copy it into the output (never generate)."""
    from . import meta as pmeta

    cfg = pcfg.load(args.config)
    out = pcfg.data_dir(cfg, args.out)
    result = pmeta.copy(args.in_path, out, strict=args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 2


def cmd_validate(args):
    """Cheap structural check against the output contract."""
    out = pcfg.data_dir(pcfg.load(args.config), args.out)
    idx = os.path.join(str(out), "indexes")
    problems = []
    found = 0
    if os.path.isdir(idx):
        for name in sorted(os.listdir(idx)):
            if not name.endswith(".db"):
                continue
            path = os.path.join(idx, name)
            con = sqlite3.connect(path)
            try:
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                cols = ({r[1] for r in con.execute("PRAGMA table_info(chunks)")}
                        if "chunks" in tables else set())
                n = (con.execute("SELECT count(*) FROM chunks").fetchone()[0]
                     if "chunks" in tables else 0)
                toc = (con.execute("SELECT count(*) FROM toc").fetchone()[0]
                       if "toc" in tables else None)
            finally:
                con.close()
            found += 1
            missing = {"chunks", "chunks_fts"} - tables
            if missing:
                problems.append("%s: missing %s" % (name, sorted(missing)))
            if "chunks" in tables and "visibility" not in cols:
                problems.append("%s: chunks.visibility absent" % name)
            extra = (" toc=%d" % toc) if toc is not None else ""
            print("%-16s chunks=%-7d tables=%s%s"
                  % (name, n, ",".join(sorted(tables)), extra))
    if not found:
        problems.append("no *.db under %s" % idx)
    if problems:
        for p in problems:
            print("FAIL", p)
        return 2
    print("OK")
    return 0


def cmd_build(args):
    names = [s.strip() for s in (args.profiles or "").split(",") if s.strip()]
    if not names:
        raise SystemExit("build needs --profiles (comma-separated profile names)")
    for name in names:
        cmd_extract(argparse.Namespace(
            config=args.config, profile=name, out=args.out, pdf_root=None,
            probe=None, limit=args.limit, parser=args.parser, ocr_dir=None))
    sources = [pcfg.profile(pcfg.load(args.config), n)["source"] for n in names]
    return cmd_search(argparse.Namespace(
        config=args.config, out=args.out, sources=",".join(sources)))


def _synthetic_pdf(path, pages=2):
    """Write a tiny text-layer PDF without depending on any PDF writer."""
    import textwrap

    text = ("The derivative measures how the output changes when the input "
            "changes. Its definition is the limit of a difference quotient. ") * 12
    lines = textwrap.wrap(text, 78)[:26]

    def esc(s):
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    def page_stream():
        parts = ["BT /F1 10 Tf 14 TL 72 720 Td"]
        parts.append("(%s) Tj" % esc(lines[0]))
        parts.extend("T* (%s) Tj" % esc(line) for line in lines[1:])
        parts.append("ET")
        return "\n".join(parts).encode("latin-1")

    content = [page_stream() for _ in range(pages)]
    font_obj = 3 + pages
    objects = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join("%d 0 R" % (i + 3) for i in range(pages))
    objects.append(("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages))
                   .encode("latin-1"))
    for i in range(pages):
        objects.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                        % (font_obj, font_obj + 1 + i)).encode("latin-1"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for c in content:
        objects.append(b"<< /Length %d >>\nstream\n" % len(c) + c + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    n = len(objects) + 1
    out += b"xref\n0 %d\n" % n
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n, xref)
    with open(path, "wb") as fh:
        fh.write(bytes(out))


def cmd_selftest(args):
    """Build a synthetic PDF in a temp dir and run extract/search/validate.

    Proves the CLI wiring end to end without touching a real corpus or
    producing data that could be mistaken for a real build. The scratch dir
    lives in the system temp dir, and its removal is best-effort.
    """
    import csv

    tmp = _writable_tmp()
    books = os.path.join(tmp, "books")
    os.makedirs(books)
    try:
        pdf = os.path.join(books, "tiny.pdf")
        _synthetic_pdf(pdf)
        probe = os.path.join(books, "probe.csv")
        with open(probe, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["rel", "cpp"])
            w.writerow(["tiny.pdf", "900"])

        out = os.path.join(tmp, "out")
        rc = cmd_extract(argparse.Namespace(
            config=args.config, profile=args.profile or "books", out=out,
            pdf_root=books, probe=probe, limit=1,
            parser=args.parser or "pypdfium2", ocr_dir=None))
        if rc:
            return rc
        rc = cmd_search(argparse.Namespace(
            config=args.config, out=out, sources="books"))
        if rc:
            return rc
        rc = cmd_validate(argparse.Namespace(config=args.config, out=out))
        print("selftest OK" if rc == 0 else "selftest FAILED")
        return rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ragpages", description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="pipeline.yaml (default: RAGPAGES_CONFIG env,"
                             " else ./pipeline.yaml, else the packaged template)")
    ap.add_argument("--config", default=None,
                    help="pipeline.yaml (default: RAGPAGES_CONFIG env,"
                         " else ./pipeline.yaml, else the packaged template)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", parents=[common],
                       help="PDF directory -> per-source db")
    e.add_argument("--profile", required=True)
    e.add_argument("--out")
    e.add_argument("--pdf-root")
    e.add_argument("--probe")
    e.add_argument("--ocr-dir", help="OCR output from `ocr` (default: <out>/ocr)")
    e.add_argument("--limit", type=int)
    e.add_argument("--parser")
    e.set_defaults(func=cmd_extract)

    o = sub.add_parser("ocr", parents=[common], help="PDF pages -> OCR text + provenance")
    o.add_argument("--profile", required=True)
    o.add_argument("--out")
    o.add_argument("--books", type=int, help="how many books from the list")
    o.add_argument("--books-from", required=True,
                   help="a {rel,pages} JSONL book list (one record per line)")
    o.add_argument("--max-pages", type=int, default=0)
    o.add_argument("--engine")
    o.add_argument("--dpi", type=int)
    o.add_argument("--parser")
    o.set_defaults(func=cmd_ocr)

    s = sub.add_parser("search", parents=[common], help="per-source dbs -> search.db")
    s.add_argument("--out")
    s.add_argument("--sources")
    s.set_defaults(func=cmd_search)

    v = sub.add_parser("validate", parents=[common], help="structural check of the artifacts")
    v.add_argument("--out")
    v.set_defaults(func=cmd_validate)

    c = sub.add_parser("chapter", parents=[common], help="header/回 OCR -> toc rows")
    c.add_argument("--source", required=True)
    c.add_argument("--book", required=True)
    c.add_argument("--db", help="toc database (default: <out>/indexes/search.db)")
    c.add_argument("--out", help="output root (default: config data dir)")
    c.add_argument("--headers", help="JSONL of {page, vol|lines}")
    c.add_argument("--hui", help="JSONL of {page, texts} for 章回 novels")
    c.add_argument("--toc", help="already-built toc JSON")
    c.add_argument("--names", help="1=Novel One,2=Novel Two for --hui")
    c.add_argument("--min-run", type=int, default=5)
    c.add_argument("--out-json")
    c.set_defaults(func=cmd_chapter)

    m = sub.add_parser("meta", parents=[common], help="validate + copy book_meta.json")
    m.add_argument("--in", dest="in_path", required=True)
    m.add_argument("--out")
    m.add_argument("--strict", action="store_true",
                   help="treat legacy doc_kind values as errors")
    m.set_defaults(func=cmd_meta)

    b = sub.add_parser("build", parents=[common], help="extract + search")
    b.add_argument("--profiles")
    b.add_argument("--out")
    b.add_argument("--limit", type=int)
    b.add_argument("--parser")
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("selftest", parents=[common],
                       help="synthetic PDF -> extract/search/validate")
    t.add_argument("--profile", default="books",
                   help="profile to borrow config from (default: books)")
    t.add_argument("--parser", help="pypdfium2 | pymupdf (default: pypdfium2)")
    t.set_defaults(func=cmd_selftest)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
