"""Exporters: every format renders from the same assembled page views."""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

FORMAT_EXTENSIONS = {"txt": ".txt", "pdf": ".pdf", "docx": ".docx", "md": ".md", "json": ".json", "html": ".html"}


def output_basename(template: str, name: str) -> str:
    stem = Path(name).stem
    base = (template or "{name}").replace("{name}", stem).replace("{date}", _dt.date.today().isoformat())
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip() or stem
    return base


SIDE_WORDS = {"right": "right margin", "left": "left margin", "top": "top margin", "bottom": "bottom margin"}
