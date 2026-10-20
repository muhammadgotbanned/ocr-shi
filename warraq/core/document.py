"""Data model for an OCR'd document.

Coordinates are always in pixels of the *processed* page image (after
rotation / deskew / spread split).  ``Page.transform`` maps those pixels back
to the original page so the searchable PDF can place text over the untouched
original scan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Region kinds produced by layout analysis.
BODY = "body"
TITLE = "title"
MARGIN = "margin"
FOOTNOTE = "footnote"
HEADER = "header"
FOOTER = "footer"
PAGE_NUMBER = "page_number"
TABLE = "table"
FORMULA = "formula"
FIGURE = "figure"
CAPTION = "caption"

TEXT_KINDS = {BODY, TITLE, MARGIN, FOOTNOTE, HEADER, FOOTER, PAGE_NUMBER, TABLE, FORMULA, CAPTION}
MAIN_FLOW_KINDS = {BODY, TITLE, TABLE, FORMULA, CAPTION}


@dataclass
class Line:
    """A text line inside a region: its box (page pixels) and recognised text."""

    bbox: tuple[float, float, float, float]
    text: str = ""
    angle: float = 0.0  # degrees, counter-clockwise (visual)
    # corners in reading orientation: top-left, top-right, bottom-right, bottom-left (page px)
    quad: list[tuple[float, float]] | None = None


@dataclass
class Region:
    id: str
    kind: str
    label: str  # raw layout label (e.g. "aside_text")
    bbox: tuple[float, float, float, float]
    score: float = 1.0
    order: int = 0
    polygon: list[tuple[float, float]] | None = None
    text: str = ""
    lines: list[Line] = field(default_factory=list)
    angle: float = 0.0  # rotation applied before recognition (deg, CCW)
    confidence: float | None = None
    flagged: bool = False
    side: str | None = None  # for margin notes: right / left / top / bottom
    line_height: float = 0.0
    notes: list[str] = field(default_factory=list)  # diagnostics (retries, warnings)
    edited: bool = False
    uncertain: list[str] = field(default_factory=list)  # words the model was unsure of, for review

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = [round(v, 2) for v in self.bbox]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Region":
        d = dict(d)
        d["bbox"] = tuple(d["bbox"])
        d["lines"] = [
            Line(
                bbox=tuple(l["bbox"]),
                text=l.get("text", ""),
                angle=l.get("angle", 0.0),
                quad=[tuple(p) for p in l["quad"]] if l.get("quad") else None,
            )
            for l in d.get("lines", [])
        ]
        if d.get("polygon"):
            d["polygon"] = [tuple(p) for p in d["polygon"]]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Footnote:
    label: str  # number used in the output label, e.g. "1"
    marker: str | None  # marker as printed, e.g. "(١)"
    text: str
    page_index: int
    region_ids: list[str] = field(default_factory=list)
    continued_from_previous: bool = False  # this entry is a continuation that could not be merged
    continues_on_next: bool = False  # text continues on the next page
    merged_continuation: str = ""  # text merged in from the next page
    merged_from_page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarginNote:
    label: str
    text: str
    side: str | None
    region_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Page:
    index: int  # 0-based position in the output document
    source_index: int  # page number inside the source file (0-based)
    source_path: str
    width: int
    height: int
    image_path: str = ""  # processed page image (PNG) inside the job folder
    thumb_path: str = ""
    split_part: int | None = None  # 0/1 when a two-page spread was split
    transform: list[float] = field(default_factory=lambda: [1, 0, 0, 1, 0, 0])  # processed px -> original px (affine)
    original_size: tuple[int, int] = (0, 0)  # size of the unprocessed page image in px
    source_size_pt: tuple[float, float] | None = None  # PDF page size in points (PDF inputs)
    notes: list[str] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    margin_notes: list[MarginNote] = field(default_factory=list)
    continuation: str = ""  # leading "=" text of the footnote area (before merging)
    printed_number: str = ""
    status: str = "pending"  # pending | running | done | error
    error: str = ""
    elapsed: float = 0.0

    def regions_of(self, *kinds: str) -> list[Region]:
        return [r for r in self.regions if r.kind in kinds]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "source_index": self.source_index,
            "source_path": self.source_path,
            "width": self.width,
            "height": self.height,
            "image_path": self.image_path,
            "thumb_path": self.thumb_path,
            "split_part": self.split_part,
            "transform": list(self.transform),
            "original_size": list(self.original_size),
            "source_size_pt": list(self.source_size_pt) if self.source_size_pt else None,
            "notes": list(self.notes),
            "regions": [r.to_dict() for r in self.regions],
            "footnotes": [f.to_dict() for f in self.footnotes],
            "margin_notes": [m.to_dict() for m in self.margin_notes],
            "continuation": self.continuation,
            "printed_number": self.printed_number,
            "status": self.status,
            "error": self.error,
            "elapsed": self.elapsed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Page":
        page = cls(
            index=d["index"],
            source_index=d.get("source_index", d["index"]),
            source_path=d.get("source_path", ""),
            width=d["width"],
            height=d["height"],
            image_path=d.get("image_path", ""),
            thumb_path=d.get("thumb_path", ""),
            split_part=d.get("split_part"),
            transform=list(d.get("transform", [1, 0, 0, 1, 0, 0])),
            original_size=tuple(d.get("original_size", (0, 0))),
            source_size_pt=tuple(d["source_size_pt"]) if d.get("source_size_pt") else None,
            notes=list(d.get("notes", [])),
            continuation=d.get("continuation", ""),
            printed_number=d.get("printed_number", ""),
            status=d.get("status", "pending"),
            error=d.get("error", ""),
            elapsed=d.get("elapsed", 0.0),
        )
        page.regions = [Region.from_dict(r) for r in d.get("regions", [])]
        page.footnotes = [Footnote(**f) for f in d.get("footnotes", [])]
        page.margin_notes = [MarginNote(**m) for m in d.get("margin_notes", [])]
        return page


@dataclass
class Document:
    name: str
    source_path: str
    pages: list[Page] = field(default_factory=list)
    translation: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_path": self.source_path,
            "pages": [p.to_dict() for p in self.pages],
            "translation": self.translation,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Document":
        doc = cls(name=d["name"], source_path=d.get("source_path", ""), translation=d.get("translation", {}), meta=d.get("meta", {}))
        doc.pages = [Page.from_dict(p) for p in d.get("pages", [])]
        return doc
