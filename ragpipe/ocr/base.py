"""OCR adapter contract.

An engine turns page images into normalized results:

    PageResult = {"page": int, "text": str,
                  "lines": [{"text", "conf", "box"}], "ms": float}

The CLI adds provenance (engine, engine_version, backend, dpi, model_sha256)
when it writes `pages.jsonl`; engines themselves stay provenance-free.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Iterable, Iterator


@dataclass
class PageImage:
    page: int
    image: object                      # BGR ndarray (H, W, 3), engine-agnostic
    width: int = 0
    height: int = 0


@dataclass
class PageResult:
    page: int
    text: str
    lines: list = field(default_factory=list)   # [{"text","conf","box"}]
    ms: float = 0.0

    def as_dict(self):
        return {"page": self.page, "text": self.text,
                "lines": self.lines, "ms": round(self.ms, 1)}


class OcrEngine:
    name = "base"

    def __init__(self, models=None, backend="cpu", params=None):
        self.models = dict(models or {})
        self.backend = backend
        self.params = dict(params or {})
        self.version = ""

    def supports(self, lang):
        return False

    def model_sha256(self):
        out = {}
        for key, path in self.models.items():
            if path and os.path.exists(path):
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        h.update(block)
                out[key] = h.hexdigest()
        return out

    def run(self, pages: Iterable[PageImage]) -> Iterator[PageResult]:
        raise NotImplementedError
