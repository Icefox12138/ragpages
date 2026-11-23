"""Parser abstraction: pypdfium2 (BSD/Apache, default) vs PyMuPDF (AGPL).

A parser opens a PDF and yields page text and page renders; chunking and page
anchors stay in the caller, so switching parsers cannot silently move pages.

PyMuPDF is AGPL-licensed: only enable it where your deployment accepts that.
Never mix parsers inside one database (see `ragpages parser_compare`).
"""
import importlib.util


class PageDoc:
    """One open document; `index` is 0-based like both libraries."""

    page_count = 0

    def text(self, index):
        raise NotImplementedError

    def render_bgr(self, index, dpi=150):
        """Return (BGR ndarray, width, height) — the shape OCR engines expect."""
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class Parser:
    name = "base"

    def __init__(self):
        self.version = ""

    def open(self, path):
        raise NotImplementedError


class PymupdfDoc(PageDoc):
    def __init__(self, doc):
        self.doc = doc
        self.page_count = doc.page_count

    def text(self, index):
        return self.doc.load_page(index).get_text("text") or ""

    def render_bgr(self, index, dpi=150):
        import numpy as np
        pix = self.doc.load_page(index).get_pixmap(dpi=dpi)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        return np.ascontiguousarray(img[:, :, ::-1]), pix.width, pix.height

    def close(self):
        self.doc.close()


class PymupdfParser(Parser):
    name = "pymupdf"

    def __init__(self):
        import pymupdf
        super().__init__()
        self._m = pymupdf
        self.version = str(pymupdf.version[0])

    def open(self, path):
        return PymupdfDoc(self._m.open(path))


class PdfiumDoc(PageDoc):
    def __init__(self, doc):
        self.doc = doc
        self.page_count = len(doc)

    def text(self, index):
        return self.doc[index].get_textpage().get_text_range() or ""

    def render_bgr(self, index, dpi=150):
        import numpy as np
        bitmap = self.doc[index].render(scale=dpi / 72.0)
        arr = bitmap.to_numpy()
        if arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[:, :, :3]
        if getattr(bitmap, "mode", "BGR") == "RGB":
            arr = arr[:, :, ::-1]
        return np.ascontiguousarray(arr), bitmap.width, bitmap.height

    def close(self):
        self.doc.close()


class PypdfiumParser(Parser):
    name = "pypdfium2"

    def __init__(self):
        import pypdfium2
        super().__init__()
        self._p = pypdfium2
        try:
            from importlib.metadata import version
            self.version = version("pypdfium2")
        except Exception:
            self.version = "unknown"

    def open(self, path):
        return PdfiumDoc(self._p.PdfDocument(path))


def get_parser(name):
    name = (name or "pypdfium2").lower()
    if name in ("pypdfium2", "pdfium"):
        return PypdfiumParser()
    if name in ("pymupdf", "fitz"):
        return PymupdfParser()
    raise SystemExit("unknown parser %r (wired: pypdfium2, pymupdf)" % name)


def installed():
    return {name: bool(importlib.util.find_spec(mod))
            for name, mod in (("pypdfium2", "pypdfium2"), ("pymupdf", "pymupdf"))}
