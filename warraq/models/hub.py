"""Model download / lookup on the Hugging Face Hub with progress callbacks."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[dict], None]

MODEL_FILE_PATTERNS = [
    "*.json",
    "*.safetensors",
    "*.jinja",
    "*.txt",
    "*.model",
    "tokenizer*",
    "*.tiktoken",
    "*.py",
    "*.yml",
]
# training code, benchmarks, pictures and alternative weight formats are never needed
IGNORE_PATTERNS = [
    "train/*", "training/*", "examples/*", "example/*", "imgs/*", "images/*", "assets/*", "docs/*", "IFMTBench/*",
    "onnx/*", "original/*", "*.gguf", "*.onnx", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4", "*.pdf",
]


def configure_environment(model_settings) -> None:
    """Apply mirror / offline / token settings to the process environment."""
    if model_settings.hf_endpoint:
        os.environ["HF_ENDPOINT"] = model_settings.hf_endpoint.rstrip("/")
    if model_settings.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
    if model_settings.hf_token:
        os.environ["HF_TOKEN"] = model_settings.hf_token
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def _cache_dir(model_settings) -> str | None:
    return model_settings.cache_dir or None


def is_local_path(model_id: str) -> bool:
    return Path(model_id).expanduser().exists()


def local_snapshot(model_id: str, model_settings, revision: str = "main") -> str | None:
    """Return the cached snapshot folder if the model is fully downloaded."""
    if is_local_path(model_id):
        return str(Path(model_id).expanduser())
    from huggingface_hub import snapshot_download

    try:
        return snapshot_download(
            model_id,
            revision=revision,
            cache_dir=_cache_dir(model_settings),
            allow_patterns=MODEL_FILE_PATTERNS,
            ignore_patterns=IGNORE_PATTERNS,
            local_files_only=True,
        )
    except Exception:
        return None


def ensure_model(
    model_id: str,
    model_settings,
    revision: str = "main",
    progress: ProgressFn | None = None,
    label: str = "",
) -> str:
    """Download (if needed) and return the local folder of a model."""
    existing = local_snapshot(model_id, model_settings, revision)
    if existing:
        return existing
    if model_settings.offline:
        raise RuntimeError(f"{model_id} is not downloaded and offline mode is on.")
    from huggingface_hub import snapshot_download

    tqdm_cls = _make_tqdm(progress, label or model_id) if progress else None
    kwargs = dict(
        revision=revision,
        cache_dir=_cache_dir(model_settings),
        allow_patterns=MODEL_FILE_PATTERNS,
        ignore_patterns=IGNORE_PATTERNS,
        token=model_settings.hf_token or None,
    )
    if tqdm_cls is not None:
        kwargs["tqdm_class"] = tqdm_cls
    try:
        path = snapshot_download(model_id, **kwargs)
    except TypeError:  # older huggingface_hub without tqdm_class
        kwargs.pop("tqdm_class", None)
        path = snapshot_download(model_id, **kwargs)
    if progress:
        progress({"stage": "download", "model": label or model_id, "done": True, "fraction": 1.0})
    return path


def _make_tqdm(progress: ProgressFn, label: str):
    """A tqdm subclass that forwards byte progress to ``progress``."""
    from tqdm.auto import tqdm as base_tqdm

    lock = threading.Lock()
    totals: dict[int, list[float]] = {}

    class ProgressTqdm(base_tqdm):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = False
            kwargs.setdefault("file", open(os.devnull, "w"))
            super().__init__(*args, **kwargs)
            self._is_bytes = kwargs.get("unit") == "B" or getattr(self, "unit", "") == "B"

        def update(self, n=1):
            out = super().update(n)
            if self._is_bytes:
                with lock:
                    totals[id(self)] = [float(self.n), float(self.total or 0)]
                    done = sum(v[0] for v in totals.values())
                    total = sum(v[1] for v in totals.values())
                if total > 0:
                    progress(
                        {
                            "stage": "download",
                            "model": label,
                            "downloaded": done,
                            "total": total,
                            "fraction": min(1.0, done / total),
                        }
                    )
            return out

    return ProgressTqdm
