"""Kraken adapter for polytonic Greek / Latin scans.

Kraken is the only engine family that handles polytonic Greek at a usable
error rate. It usually lives in its own environment (its own venv), so this
module is imported only when `ocr.engine: kraken`; the import failure is
translated into a readable error by the registry rather than an ImportError
traceback.
"""
from .base import OcrEngine, PageResult


def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [[int(min(xs)), int(min(ys))], [int(max(xs)), int(min(ys))],
            [int(max(xs)), int(max(ys))], [int(min(xs)), int(max(ys))]]


class KrakenEngine(OcrEngine):
    name = "kraken"

    def __init__(self, models=None, backend="cpu", params=None):
        super().__init__(models=models, backend=backend, params=params)
        import numpy as np
        from PIL import Image
        from kraken import blla, rpred
        from kraken.lib import models as kmodels

        self._np, self._Image = np, Image
        self._blla, self._rpred = blla, rpred
        self.model_path = self.models.get("model") or self.params.get("model")
        if not self.model_path:
            raise SystemExit("kraken needs ocr.models.model (path to a .mlmodel file)")
        self.net = kmodels.load_any(str(self.model_path))
        try:
            from importlib.metadata import version
            self.version = version("kraken")
        except Exception:
            self.version = "unknown"

    def supports(self, lang):
        return str(lang or "").lower() in ("grc", "greek", "greek-ancient",
                                           "latin", "lat", "en")

    def run(self, pages):
        for page in pages:
            rgb = page.image[:, :, ::-1]          # BGR -> RGB
            im = self._Image.fromarray(self._np.ascontiguousarray(rgb)).convert("L")
            seg = self._blla.segment(im)
            preds = list(self._rpred.rpred(self.net, im, seg))
            lines = []
            for out, line in zip(preds, getattr(seg, "lines", [])):
                confs = list(getattr(out, "confidences", []) or [])
                conf = round(sum(confs) / len(confs), 4) if confs else 0.0
                boundary = getattr(line, "boundary", None)
                box = _bbox(boundary) if boundary else []
                lines.append({"text": out.prediction, "conf": conf, "box": box})
            text = "\n".join(item["text"] for item in lines)
            yield PageResult(page=page.page, text=text, lines=lines, ms=0.0)
