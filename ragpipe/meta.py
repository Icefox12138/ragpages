"""`book_meta` stage: validate + copy, never generate.

A downstream RAG runtime reads `book_meta.json` (schema 1) to attach
`doc_kind`/`subject` to citations. It is human/rule-annotated data, so the
pipeline only accepts it as input. Existing files may use `doc_kind:
"unknown"` and a few legacy values, so those are warnings (hard errors with
`--strict`) rather than silent drops.
"""
import json
import os

KINDS = ("textbook", "edition", "dictionary", "grammar", "dataset")
LEGACY_KINDS = ("unknown", "notes", "reference", "docs")


def validate(doc, strict=False):
    """Return (errors, warnings); `strict` promotes unknown kinds to errors."""
    errors, warnings = [], []
    if not isinstance(doc, dict):
        return ["book_meta must be a JSON object"], []
    if doc.get("schema") != 1:
        errors.append("schema must be 1 (got %r)" % doc.get("schema"))
    books = doc.get("books")
    if not isinstance(books, dict):
        errors.append("books must be an object keyed by relative path")
        return errors, warnings
    for rel, entry in books.items():
        if not isinstance(rel, str) or not rel.strip():
            errors.append("empty book key")
            continue
        if not isinstance(entry, dict):
            errors.append("%s: entry must be an object" % rel)
            continue
        subject = entry.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            errors.append("%s: subject must be a non-empty string" % rel)
        kind = entry.get("doc_kind")
        if kind is not None and kind not in KINDS:
            if kind in LEGACY_KINDS and not strict:
                warnings.append("%s: legacy doc_kind %r (kept as-is)" % (rel, kind))
            else:
                errors.append("%s: doc_kind %r not in %s" % (rel, kind, list(KINDS)))
        ocr = entry.get("ocr")
        if ocr is not None and not isinstance(ocr, dict):
            errors.append("%s: ocr must be an object" % rel)
    return errors, warnings


def summary(doc):
    books = (doc or {}).get("books") or {}
    kinds = {}
    for entry in books.values():
        if isinstance(entry, dict):
            kind = entry.get("doc_kind") or "(none)"
            kinds[kind] = kinds.get(kind, 0) + 1
    subjects = sorted({e.get("subject") for e in books.values()
                       if isinstance(e, dict) and e.get("subject")})
    return {"books": len(books), "by_doc_kind": dict(sorted(kinds.items())),
            "subjects": subjects,
            "with_ocr": sum(1 for e in books.values()
                            if isinstance(e, dict) and e.get("ocr"))}


def load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def copy(in_path, out_dir, strict=False):
    """Validate `in_path`, then write it to `<out_dir>/book_meta.json` verbatim."""
    doc = load(in_path)
    errors, warnings = validate(doc, strict=strict)
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings,
                "summary": summary(doc)}
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "book_meta.json")
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    return {"ok": True, "out": dst, "warnings": warnings, "summary": summary(doc)}
