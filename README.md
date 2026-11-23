# ragpages

**PDF (+ OCR) → data contract → database for agents.** One command on one machine.

ragpages is a lightweight, single-machine producer pipeline. It eats PDFs (with a text
layer, or via a pluggable OCR stage) and emits SQLite artifacts that follow the classic
RAG database contract — `documents` / `chunks` / `chunks_fts` / `toc` — ready to be served
by any RAG runtime. No services, no daemons, no cloud: `ragpages build` and you are done.

## Positioning vs MinerU

| | ragpages | MinerU-style service pipelines |
|---|---|---|
| Shape | One CLI, one process, one command | Heavy service pipeline (layout models, server components, model downloads) |
| Input | PDF directory + a probe CSV (+ optional OCR models) | PDF (or batches through its service) |
| Output | Plain SQLite (`documents`/`chunks`/`chunks_fts`/`toc`) + manifests | Markdown/JSON content plus its own serving stack |
| OCR | Optional stage, engine-swappable (RapidOCR / Kraken) | Built-in (large models, big downloads) |
| Parser | pypdfium2 (BSD/Apache) by default; PyMuPDF optional | Its own stack |
| License story | MIT code; pypdfium2 is permissively licensed | AGPL / model licenses to review |
| Fit | "I have PDFs and want a searchable database for my agent" | "I want full document understanding at scale" |

ragpages deliberately stays at the *retrieval layer*: it turns books into searchable,
page-anchored chunks with provenance. MinerU targets *document understanding* (formulas,
tables, reading order). If you need heavy layout analysis, use MinerU; if you need a
portable, auditable index for an agent, use ragpages.

### Why not a vector-database ingestion toolkit

Three PyPI packages already use the old name (`ragpipe`, `ragpipe-ai`, `ragpipe-lite`);
all of them end at "chunks → embeddings → vector store". ragpages ends somewhere else:
a **citation-grade SQLite database** where every chunk keeps its `page`, locator and
`sha256` provenance, so an answer can always be checked against the page it came from.
If vectors are all you need, those packages are fine; if you need auditable page
citations, that is exactly what ragpages is for.

## Quick start

```bash
# editable install for development
uv venv .venv
uv pip install -e ".[pdfium]"

# or from PyPI (once published)
# pip install "ragpages[pdfium]"

# prove the wiring without any real data
ragpages selftest
```

Real corpus:

```bash
cd my-corpus
# 1. copy the profile template and edit it
copy <path-to-package>/pipeline.yaml .      # Windows
# cp $(python -c "import ragpages,os;print(os.path.dirname(ragpages.__file__))")/../pipeline.yaml .   # POSIX

# 2. a probe CSV decides which books have a text layer:
#    catalog/probe.csv  ->  rel,cpp
#                           books/a.pdf,900        (cpp = chars per page)

# 3. build: extract per profile, then fuse into search.db
ragpages build --profiles books --out data/

# or stage by stage (handy for re-runs and debugging)
ragpages extract  --profile books --out data/ --limit 1
ragpages search   --out data/ --sources books
ragpages validate --out data/
```

## Configuration

`pipeline.yaml` is the only place a corpus directory is named; code always takes a
profile, never a machine path.

| Setting | Meaning |
|---|---|
| `defaults.data` | output root, relative to the config file |
| `defaults.parser` | `pypdfium2` (default, BSD/Apache) or `pymupdf` (AGPL) — never mix parsers in one database |
| `defaults.chunk` | `size` / `overlap` / `min_len` for chunking |
| `defaults.min_cpp` | books below this chars-per-page average are treated as scans |
| `ocr.models` | local ONNX/`.mlmodel` paths — models never enter the repository, only their sha256 enters provenance |
| `profiles.<name>` | `root` (PDF dir), `source` (the id written into `chunks.source`), `layout`, `probe` CSV, per-profile `ocr`/`parser`/`chunk` overrides |

Environment variables:

| Variable | Effect |
|---|---|
| `RAGPAGES_CONFIG` | path of the `pipeline.yaml` to load |
| `RAGPAGES_DATA` | override the output data root |

## Output contract

Everything lands under `<data>/` (config `defaults.data`, or `--out`):

```
data/
├── indexes/<source>.db        per-source: documents + chunks + chunks_fts (trigram)
├── indexes/<source>-manifest.json
├── indexes/search.db          fused chunks (source column) + chunks_fts (unicode61, pre-tokenized)
├── indexes/search-eval.json   smoke-query evaluation + canary isolation check
├── book_meta.json             copied by `ragpages meta` (validated input, never generated)
└── ocr/<book_id>/pages.jsonl  OCR stage output (page text + boxes + confidence + provenance)
```

The tables follow the classic RAG-DB-SCHEMA concept — `documents` (per-book provenance),
`chunks` (page-anchored text with `visibility`), `chunks_fts` (`rowid == chunks.id`),
and `toc` (`source, book, ord, level, title, page_start, page_end, volume, pages, meta`,
written by `ragpages chapter`). A downstream runtime needs no more than these to serve
search, citations and page images.

- `visibility` is enforced at SQL level (`tutor` vs `answer_key`); the fused `search.db`
  carries a canary `answer_key` row and the smoke eval proves it never surfaces to students.
- CJK text is pre-tokenized into bigrams for the `unicode61` FTS, per the downstream contract.

## Stages

| Command | Stage | Idempotency |
|---|---|---|
| `ragpages ocr` | PDF → per-page OCR text + boxes + confidence + provenance | per book, resumable |
| `ragpages extract` | text layer (or OCR fallback) → per-source db + manifest | overwrites the source db |
| `ragpages chapter` | header/回 OCR JSONL → `toc` rows (via the public `classics-index` package) | per source+book |
| `ragpages meta` | validate + copy `book_meta.json` (never generates it) | overwrites the copy |
| `ragpages search` | per-source dbs → `search.db` + FTS + eval | overwrites |
| `ragpages validate` | artifacts vs contract | read-only |
| `ragpages build` | extract + search | overall re-run |
| `ragpages selftest` | synthetic PDF → extract → search → validate | scratch only |

### OCR engines

`ocr.engine` picks an adapter at runtime; a missing package/model is a readable error,
not an ImportError traceback.

| Engine | Extra | Notes |
|---|---|---|
| `rapidocr` | `pip install ragpages[rapid]` | PP-OCRv4 ONNX, cpu/dml; the default |
| `kraken` | `pip install ragpages[kraken]` | polytonic Greek / Latin scans (own venv recommended) |
| `paddle` | `pip install ragpages[paddle]` | comparison use only, not wired |
| `deepseek-api` | — | network OCR, opt-in only |

### Chapter stage

`ragpages chapter` builds the `toc` table from header/回 OCR JSONL. The pure chapter
logic lives in the public [`classics-index`](https://pypi.org/project/classics-index/)
package (>= 0.1): install it next to ragpages (`pip install classics-index`) when you
use this stage.

## License notes

- ragpages code: MIT.
- `pypdfium2`: BSD/Apache — the default parser is freely distributable.
- `pymupdf`: AGPL — opt-in extra; only enable where your deployment accepts that.
- OCR models are local artifacts: they never ship with the package, only their sha256
  enters the provenance records.

## 中文摘要

ragpages 是一条**单机一条命令**的轻量管线：输入 PDF 目录（有文本层直接抽取，无文本层走可插拔
OCR 阶段），输出符合经典 RAG 数据库契约的 SQLite 产物（`documents` / `chunks` /
`chunks_fts` / `toc` 表 + manifest 与评测文件），可直接交给下游 Agent 运行时做检索、
引用与回页。对比 MinerU 这类服务化重型管线，ragpages 只有 CLI、零服务、零云端依赖，
模型不进仓库只记 sha256；默认解析器 pypdfium2（BSD/Apache），PyMuPDF（AGPL）仅作可选。
配置全部走 `pipeline.yaml` profile + `RAGPAGES_CONFIG` / `RAGPAGES_DATA` 环境变量，
代码中不含任何机器/私有路径。
