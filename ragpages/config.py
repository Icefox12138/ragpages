"""Pipeline configuration: a profile maps a physical layout to a source.

The config file is the only place a corpus directory is named; code takes a
profile, never a machine path. Base/data resolution lives here:

  * `RAGPAGES_CONFIG` overrides the pipeline.yaml location;
  * `RAGPAGES_DATA` overrides the output data root;
  * everything else resolves relative to the config file's directory.

Model path resolution is limited to `ocr.models` (plus the parser selection in
`defaults.parser`); nothing else in the package resolves models.
"""
import os
from pathlib import Path

import yaml


def default_config_path():
    """Explicit env > ./pipeline.yaml > the template shipped in the package."""
    env = os.environ.get("RAGPAGES_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    cwd = Path("pipeline.yaml")
    if cwd.exists():
        return cwd.resolve()
    packaged = Path(__file__).resolve().parent / "pipeline.yaml"
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[1] / "pipeline.yaml"


def load(path=None):
    path = Path(path or default_config_path())
    if not path.exists():
        raise SystemExit("pipeline config not found: %s" % path)
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("version", 1)
    cfg.setdefault("defaults", {})
    cfg.setdefault("profiles", {})
    cfg["_path"] = str(path)
    return cfg


def _resolve(value, base):
    if value is None:
        return None
    p = Path(os.path.expanduser(str(value)))
    return p if p.is_absolute() else (base / p).resolve()


def profile(cfg, name):
    """Resolve one profile: absolute root, source id, chunking, probe csv."""
    profiles = cfg.get("profiles") or {}
    if name not in profiles:
        raise SystemExit("unknown profile %r (have: %s)"
                         % (name, ", ".join(sorted(profiles))))
    base = Path(cfg["_path"]).parent
    defaults = cfg.get("defaults") or {}
    raw = profiles[name] or {}
    chunk = dict(defaults.get("chunk") or {})
    chunk.update(raw.get("chunk") or {})
    ocr = dict(defaults.get("ocr") or {})
    ocr.update(cfg.get("ocr") or {})      # top-level `ocr:` block
    ocr.update(raw.get("ocr") or {})      # profile override wins
    models = {k: _resolve(v, base) for k, v in (ocr.get("models") or {}).items()}
    probe = raw.get("probe")
    return {
        "name": name,
        "root": _resolve(raw.get("root"), base),
        "source": raw.get("source") or name,
        "layout": raw.get("layout") or "flat",
        "file": raw.get("file"),
        "probe": _resolve(probe, base) if probe else None,
        "parser": raw.get("parser") or defaults.get("parser") or "pypdfium2",
        "min_cpp": raw.get("min_cpp", defaults.get("min_cpp", 200)),
        "chunk": {
            "size": int(chunk.get("size", 600)),
            "overlap": int(chunk.get("overlap", 80)),
            "min_len": int(chunk.get("min_len", 80)),
        },
        "ocr": {
            "engine": ocr.get("engine") or "rapidocr",
            "backend": ocr.get("backend") or "cpu",
            "lang": ocr.get("lang") or "ch",
            "dpi": int(ocr.get("dpi", 300)),
            "models": {k: str(v) for k, v in models.items()},
            "params": dict(ocr.get("params") or {}),
            "qc": dict(ocr.get("qc") or {}),
        },
    }


def data_dir(cfg, override=None):
    """Output root: `--out` > `RAGPAGES_DATA` > config `defaults.data`."""
    if override:
        return Path(override).resolve()
    env = os.environ.get("RAGPAGES_DATA")
    if env:
        return Path(os.path.expanduser(env)).resolve()
    return _resolve((cfg.get("defaults") or {}).get("data") or "data",
                    Path(cfg["_path"]).parent)
