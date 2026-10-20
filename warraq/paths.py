"""Filesystem locations used by the app (per-user, cross-platform)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_documents_dir, user_log_dir

from . import APP_NAME

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
ASSETS_DIR = PACKAGE_DIR / "assets"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    path = _env_path("WARRAQ_CONFIG_DIR") or Path(user_config_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    path = _env_path("WARRAQ_DATA_DIR") or Path(user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_dir() -> Path:
    path = data_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = _env_path("WARRAQ_LOG_DIR") or Path(user_log_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_dir() -> Path:
    try:
        docs = Path(user_documents_dir())
    except Exception:  # pragma: no cover - exotic platforms
        docs = Path.home()
    return docs / APP_NAME


def settings_file() -> Path:
    return config_dir() / "settings.json"


def open_in_file_manager(path: Path) -> None:
    """Reveal a folder (or a file's folder) in Explorer / Finder."""
    import subprocess

    path = Path(path)
    target = path if path.is_dir() else path.parent
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
