"""RapidOCR adapter (PP-OCRv4 ONNX, optional DirectML).

Model paths, backend and parameters come from `pipeline.yaml`, never from code
constants. The default backend is cpu; set `ocr.backend: dml` on Windows
machines with DirectML support.
"""
import time

from .base import OcrEngine, PageResult


class RapidOcrEngine(OcrEngine):
    name = "rapidocr"

    def __init__(self, models=None, backend="cpu", params=None):
        super().__init__(models=models, backend=backend, params=params)
        import rapidocr_onnxruntime as rapid

        self.version = getattr(rapid, "__version__", "") or ""
        use_dml = self.backend == "dml"
        kw = {
            "det_model_path": self.models.get("det"),
            "rec_model_path": self.models.get("rec"),
            "cls_model_path": self.models.get("cls"),
            "det_use_dml": use_dml, "rec_use_dml": use_dml, "cls_use_dml": use_dml,
        }
        if self.params.get("use_cls") is False:
            kw["use_cls"] = False
        if self.params.get("det_limit"):
            kw["det_limit_side_len"] = int(self.params["det_limit"])
        if self.params.get("batch"):
            kw["rec_batch_num"] = int(self.params["batch"])
        self.eng = rapid.RapidOCR(**{k: v for k, v in kw.items() if v is not None})
        if not self.version:
            try:
                from importlib.metadata import version
                self.version = version("rapidocr-onnxruntime")
            except Exception:
                self.version = "unknown"

    def supports(self, lang):
        return str(lang or "").lower() in ("ch", "zh", "en", "latin", "grc-latin")

    def run(self, pages):
        for page in pages:
            t0 = time.perf_counter()
            res, _ = self.eng(page.image)
            ms = (time.perf_counter() - t0) * 1000
            lines, texts = [], []
            for item in res or []:
                box, text, score = item[0], item[1], item[2]
                lines.append({"text": text, "conf": round(float(score), 4),
                              "box": [[int(p[0]), int(p[1])] for p in box]})
                texts.append(text)
            yield PageResult(page=page.page, text="\n".join(texts),
                             lines=lines, ms=ms)
