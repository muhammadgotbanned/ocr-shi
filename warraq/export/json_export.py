"""JSON export: the full structure, with coordinates and confidences."""

from __future__ import annotations

import json
from pathlib import Path

from .. import __version__


def build(doc, views, settings) -> dict:
    pages = []
    for v in views:
        p = v.page.to_dict()
        p["assembled"] = {
            "headers": v.headers,
            "footers": v.footers,
            "body": v.body,
            "margin_notes": [{"label": m.label, "side": m.side, "text": m.text, "region": m.region_id} for m in v.margins],
            "footnotes": [
                {
                    "label": f.label,
                    "text": f.text,
                    "marker": f.marker,
                    "regions": f.region_ids,
                    "continued_from_previous_page": f.continued_from_previous,
                    "merged_continuation_from_page": f.merged_from_page,
                }
                for f in v.footnotes
            ],
        }
        pages.append(p)
    return {
        "generator": f"Warraq {__version__}",
        "name": doc.name,
        "source": doc.source_path,
        "meta": doc.meta,
        "coordinate_space": "pixels of the processed page image (see page.transform for the original)",
        "pages": pages,
    }


def write(path: Path, doc, views, settings) -> Path:
    from ..core.engine import _json_default

    Path(path).write_text(json.dumps(build(doc, views, settings), ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    return Path(path)
