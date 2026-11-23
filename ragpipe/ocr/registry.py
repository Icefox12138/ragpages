"""Engine registry: `pipeline.yaml` names an engine, never imports one."""
from .base import OcrEngine

# Engines evaluated but not wired as production; naming them here gives a
# readable error instead of an ImportError.
PLANNED = {"paddle": "comparison use only, not wired as a production engine",
           "deepseek-api": "network OCR, opt-in only"}


def get_engine(name, models=None, backend="cpu", params=None) -> OcrEngine:
    name = (name or "rapidocr").lower()
    if name == "rapidocr":
        from .rapidocr_engine import RapidOcrEngine
        return RapidOcrEngine(models=models, backend=backend, params=params)
    if name == "kraken":
        try:
            from .kraken_engine import KrakenEngine
            return KrakenEngine(models=models, backend=backend, params=params)
        except ImportError as exc:
            raise SystemExit(
                "OCR engine 'kraken' needs the kraken package; "
                "this interpreter cannot import it: %s" % exc)
    if name in PLANNED:
        raise SystemExit("OCR engine %r is not wired yet: %s" % (name, PLANNED[name]))
    raise SystemExit("unknown OCR engine %r (wired: rapidocr, kraken)" % name)
