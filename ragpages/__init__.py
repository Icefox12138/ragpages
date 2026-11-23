"""ragpages: PDF (+ OCR) -> data contract -> database for agents.

A lightweight, single-machine producer pipeline. It consumes PDFs (with a text
layer, or via an OCR stage) and emits SQLite artifacts that follow a classic
RAG database contract:

  <data>/indexes/<source>.db   documents + chunks + chunks_fts (per source)
  <data>/indexes/search.db     fused chunks (with `source` column) + chunks_fts
  <data>/indexes/*-manifest.json
  <data>/ocr/<book_id>/pages.jsonl   OCR stage output (optional)

Everything is driven by `pipeline.yaml` profiles; no code path names a corpus
directory. See README.md.
"""

__version__ = "0.1.0"
