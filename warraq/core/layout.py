"""Turn raw layout detections into typed regions in Arabic reading order.

PP-DocLayoutV3 gives labelled boxes.  On Arabic books and manuscripts it is
good at body text, headers and marginal notes but often labels footnotes as
ordinary text, so this module adds geometric reasoning:

* boxes are snapped outwards so they never cut a text line,
* ink the detector missed is recovered as extra regions,
* narrow blocks beside the main column become marginal notes,
* blocks under the footnote rule, or set in smaller type at the foot of the
  page, become footnotes,
* body blocks are ordered right-to-left, column by column (XY-cut).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

from . import geometry as G
from . import imaging
from .document import (
    BODY,
    CAPTION,
    FIGURE,
    FOOTER,
    FOOTNOTE,
    FORMULA,
    HEADER,
    MAIN_FLOW_KINDS,
    MARGIN,
    PAGE_NUMBER,
    TABLE,
    TEXT_KINDS,
    TITLE,
    Region,
)

LABEL_TO_KIND: dict[str, str | None] = {
    "text": BODY,
    "content": BODY,
    "abstract": BODY,
    "algorithm": BODY,
    "reference_content": BODY,
    "vertical_text": BODY,
    "doc_title": TITLE,
    "paragraph_title": TITLE,
    "figure_title": CAPTION,
    "chart_title": CAPTION,
    "table_title": CAPTION,
    "vision_footnote": CAPTION,
    "aside_text": MARGIN,
    "footnote": FOOTNOTE,
    "header": HEADER,
    "footer": FOOTER,
    "number": PAGE_NUMBER,
    "table": TABLE,
    "formula": FORMULA,
    "display_formula": FORMULA,
    "image": FIGURE,
    "chart": FIGURE,
    "seal": FIGURE,
    # containers / pieces that would duplicate text
    "reference": None,
    "inline_formula": None,
    "formula_number": None,
    "header_image": None,
    "footer_image": None,
}

FOOTNOTE_START_RE = re.compile(r"^\s*(?:[\(\[（]\s*[0-9٠-٩۰-۹]{1,3}\s*[\)\]）]|[0-9٠-٩۰-۹]{1,3}\s*[-–—.)]\s|\*{1,3}\s)")


@dataclass
class PageContext:
    width: int
    height: int
    mask: np.ndarray  # ink mask (uint8 0/1) of the processed page
    gray: np.ndarray
    body_line_height: float = 0.0
    main_box: tuple[float, float, float, float] | None = None
    rule_y: float | None = None


def build_context(gray: np.ndarray) -> PageContext:
    mask = imaging.ink_mask(gray)
    h, w = gray.shape
    return PageContext(width=w, height=h, mask=mask, gray=gray)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def analyze(detections, ctx: PageContext, settings, page_index: int) -> list[Region]:
    lay = settings.layout
    regions = _from_detections(detections, page_index, lay)
    regions = _dedupe(regions)
    if not regions or not any(r.kind in TEXT_KINDS for r in regions):
        regions = [r for r in regions if r.kind == FIGURE] + _full_page_regions(ctx, page_index)
    _measure_lines(regions, ctx)
    _expand_to_ink(regions, ctx)
    regions = _dedupe(regions)
    if lay.mode == "auto":
        extra = _recover_missed_text(regions, ctx, page_index)
        regions.extend(extra)
        _measure_lines(extra, ctx)
    ctx.main_box = _main_box(regions, ctx)
    ctx.body_line_height = _body_line_height(regions)
    _classify_headers(regions, ctx)
    if lay.detect_margin_notes:
        _classify_margins(regions, ctx, lay.document_type)
    else:
        for r in regions:
            if r.kind == MARGIN:
                r.kind = BODY
    if lay.detect_footnotes:
        _classify_footnotes(regions, ctx)
    else:
        for r in regions:
            if r.kind == FOOTNOTE:
                r.kind = BODY
    _order(regions, lay.reading_order)
    for i, r in enumerate(regions):
        r.id = f"p{page_index + 1}-r{i + 1}"
    return regions


def full_page(ctx: PageContext, settings, page_index: int) -> list[Region]:
    """Layout for 'whole page' mode: one block, but footnotes under a rule are still split off."""
    regions = _full_page_regions(ctx, page_index)
    _measure_lines(regions, ctx)
    if settings.layout.detect_footnotes and regions:
        rules = _footnote_rules(ctx)
        if rules:
            y = rules[0][1]
            body = regions[0]
            x0, y0, x1, y1 = body.bbox
            if y0 < y < y1:
                top = Region(id="", kind=BODY, label="page", bbox=(x0, y0, x1, y - 2))
                bottom = Region(id="", kind=FOOTNOTE, label="page", bbox=(x0, y + 4, x1, y1))
                regions = [top, bottom]
                _measure_lines(regions, ctx)
    for i, r in enumerate(regions):
        r.id = f"p{page_index + 1}-r{i + 1}"
        r.order = i
    return regions


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


def _from_detections(detections, page_index: int, lay) -> list[Region]:
    regions = []
    for i, d in enumerate(detections):
        kind = LABEL_TO_KIND.get(d.label, BODY)
        if kind is None:
            continue
        if kind == FIGURE and lay.read_figures:
            kind = CAPTION
        regions.append(
            Region(
                id=f"p{page_index + 1}-d{i}",
                kind=kind,
                label=d.label,
                bbox=tuple(float(v) for v in d.bbox),
                score=d.score,
                order=d.order,
                polygon=d.polygon if lay.use_polygons else None,
            )
        )
    return regions


def _dedupe(regions: list[Region]) -> list[Region]:
    """Remove duplicates and boxes nested inside a same-family box."""
    keep: list[Region] = []
    for r in sorted(regions, key=lambda r: (-G.area(r.bbox), -r.score)):
        dup = False
        for k in keep:
            iou = G.box_iou(r.bbox, k.bbox)
            inside = G.containment(r.bbox, k.bbox)
            same_family = (r.kind in TEXT_KINDS) == (k.kind in TEXT_KINDS)
            if iou > 0.75 and same_family:
                # same area detected twice with different labels: keep the more specific/confident one
                if _specificity(r) > _specificity(k) and r.score > 0.5 * k.score:
                    k.kind, k.label, k.score = r.kind, r.label, r.score
                dup = True
                break
            if inside > 0.85 and same_family and (r.kind == k.kind or k.kind in (BODY, TITLE)) and r.kind not in (MARGIN, FOOTNOTE, PAGE_NUMBER):
                dup = True
                break
        if not dup:
            keep.append(r)
    return keep


def _specificity(r: Region) -> int:
    return {MARGIN: 4, FOOTNOTE: 4, PAGE_NUMBER: 3, HEADER: 3, FOOTER: 3, TABLE: 3, TITLE: 1, BODY: 2}.get(r.kind, 0)


def _full_page_regions(ctx: PageContext, page_index: int) -> list[Region]:
    box = imaging.tight_ink_box(ctx.mask)
    if box is None:
        return []
    pad = 6
    x0, y0, x1, y1 = box
    bbox = (max(0, x0 - pad), max(0, y0 - pad), min(ctx.width, x1 + pad), min(ctx.height, y1 + pad))
    return [Region(id=f"p{page_index + 1}-full", kind=BODY, label="page", bbox=tuple(float(v) for v in bbox))]


def _measure_lines(regions: Iterable[Region], ctx: PageContext) -> None:
    for r in regions:
        if r.kind not in TEXT_KINDS:
            continue
        x0, y0, x1, y1 = (int(round(v)) for v in r.bbox)
        sub = ctx.mask[max(0, y0) : y1, max(0, x0) : x1]
        if sub.size == 0:
            continue
        bands = imaging.segment_lines(ctx.gray[max(0, y0) : y1, max(0, x0) : x1], sub)
        r.line_height = imaging.median_line_height(bands) if bands else float(max(1, y1 - y0))
        r.notes = [n for n in r.notes if not n.startswith("lines:")] + [f"lines:{len(bands)}"]


def line_count(r: Region) -> int:
    for n in r.notes:
        if n.startswith("lines:"):
            return int(n.split(":")[1])
    return 0


def _expand_to_ink(regions: list[Region], ctx: PageContext) -> None:
    """Grow text boxes so they contain every text line they touch."""
    text_regions = [r for r in regions if r.kind in TEXT_KINDS]
    if not text_regions:
        return
    lh = np.median([r.line_height for r in text_regions if r.line_height > 0] or [40.0])
    kx = max(3, int(lh * 0.9))
    ky = max(1, int(lh * 0.12))
    for r in text_regions:
        others = [o for o in regions if o is not r]
        mask = ctx.mask.copy()
        for o in others:
            ox0, oy0, ox1, oy1 = (int(round(v)) for v in o.bbox)
            # keep ink shared with r itself; only blank what is clearly another block
            if G.containment(o.bbox, r.bbox) > 0.5:
                continue
            mask[max(0, oy0) : oy1, max(0, ox0) : ox1] = 0
        x0, y0, x1, y1 = r.bbox
        margin = lh * 4
        wx0, wy0 = int(max(0, x0 - margin)), int(max(0, y0 - lh))
        wx1, wy1 = int(min(ctx.width, x1 + margin)), int(min(ctx.height, y1 + lh))
        win = mask[wy0:wy1, wx0:wx1]
        if win.size == 0 or win.sum() == 0:
            continue
        blobs = cv2.dilate(win, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky)))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(blobs, connectivity=8)
        nx0, ny0, nx1, ny1 = x0, y0, x1, y1
        for i in range(1, n):
            bx, by, bw, bh, _ = stats[i]
            box = (bx + wx0, by + wy0, bx + bw + wx0, by + bh + wy0)
            inter = G.intersection_area(box, r.bbox)
            if inter <= 0:
                continue
            ink = win[by : by + bh, bx : bx + bw] * (labels[by : by + bh, bx : bx + bw] == i)
            tight = imaging.tight_ink_box(ink)
            if tight is None or (tight[3] - tight[1]) < lh * 0.3:
                continue  # a rule or underline: never stretch a text box along it
            # blob must belong mostly to this region vertically (a line of this block)
            if G.v_overlap(box, r.bbox) < 0.6 * (box[3] - box[1]):
                continue
            # a horizontal text line forms a flat blob; a tall one is rotated or vertical
            # writing (typically a marginal note) and must stay a region of its own
            if (box[3] - box[1]) > 2.2 * lh and not (box[0] >= x0 and box[2] <= x1):
                continue
            nx0, ny0 = min(nx0, box[0] - 2), min(ny0, box[1] - 2)
            nx1, ny1 = max(nx1, box[2] + 2), max(ny1, box[3] + 2)
        # never grow by more than a few line-heights sideways
        nx0, nx1 = max(nx0, x0 - margin), min(nx1, x1 + margin)
        r.bbox = tuple(float(v) for v in G.clamp_box((nx0, ny0, nx1, ny1), ctx.width, ctx.height))


def _recover_missed_text(regions: list[Region], ctx: PageContext, page_index: int) -> list[Region]:
    """Ink that no region covers (faint marginalia, missed footnote lines) becomes new regions."""
    covered = np.zeros_like(ctx.mask)
    for r in regions:
        x0, y0, x1, y1 = (int(round(v)) for v in r.bbox)
        covered[max(0, y0) : y1, max(0, x0) : x1] = 1
    free = ctx.mask * (1 - covered)
    if free.sum() < 50:
        return []
    text_lh = [r.line_height for r in regions if r.kind in TEXT_KINDS and r.line_height > 0]
    lh = float(np.median(text_lh)) if text_lh else max(20.0, ctx.height / 60)
    # rules, frames and underlines are not text
    long_h = cv2.morphologyEx(free, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, int(lh * 2.5)), 1)))
    long_v = cv2.morphologyEx(free, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, int(lh * 2.5)))))
    free = free * (1 - np.maximum(_thin(long_h, lh, axis=0), _thin(long_v, lh, axis=1)))
    free = imaging.remove_specks(free, max(6, int(lh * lh * 0.01)))
    blobs = cv2.dilate(free, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(lh * 1.2)), max(3, int(lh * 0.8)))))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(blobs, connectivity=8)
    found: list[Region] = []
    edge = 0.012
    for i in range(1, n):
        bx, by, bw, bh, area = stats[i]
        if bw < lh * 1.5 or bh < lh * 0.6:
            continue
        if bx <= ctx.width * edge or by <= ctx.height * edge or bx + bw >= ctx.width * (1 - edge) or by + bh >= ctx.height * (1 - edge):
            continue  # touches the page edge: scanner border / bleed
        ink = free[by : by + bh, bx : bx + bw]
        blob = labels[by : by + bh, bx : bx + bw] == i
        # density inside the blob itself, so diagonal notes (sparse bounding boxes) still count
        density = float((ink * blob).sum()) / max(1, int(blob.sum()))
        if density < 0.03 or density > 0.6:
            continue  # empty-ish or a stain / picture
        tight = imaging.tight_ink_box(ink * blob)
        if tight is None or (tight[3] - tight[1]) < lh * 0.3:
            continue  # flat ink: leftovers of a rule or a border
        if bh < lh * 0.6 * 1.0 and bw > ctx.width * 0.5:
            continue  # a rule or frame line
        if bw * bh < (lh * 2) ** 2:
            continue
        # the blob was dilated to join letters; the region itself hugs the ink, so it
        # never reaches into the neighbouring text block
        pad = lh * 0.25
        box = G.clamp_box((bx + tight[0] - pad, by + tight[1] - pad, bx + tight[2] + pad, by + tight[3] + pad), ctx.width, ctx.height)
        found.append(
            Region(
                id=f"p{page_index + 1}-x{i}",
                kind=BODY,
                label="recovered",
                bbox=tuple(float(v) for v in box),
                score=0.5,
                order=10_000 + i,
                notes=["recovered"],
            )
        )
    return found


def _thin(mask: np.ndarray, lh: float, axis: int) -> np.ndarray:
    """Keep only the thin strokes of ``mask`` (rules are a few pixels thick; a dense
    run of letters is not), measured across ``axis`` (0 = height, 1 = width)."""
    if not mask.any():
        return mask
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    limit = max(3, lh * 0.25)
    keep = np.zeros(n, dtype=np.uint8)
    for i in range(1, n):
        thickness = stats[i, cv2.CC_STAT_HEIGHT] if axis == 0 else stats[i, cv2.CC_STAT_WIDTH]
        keep[i] = 1 if thickness <= limit else 0
    return keep[labels]


def _main_box(regions: list[Region], ctx: PageContext):
    body = [r for r in regions if r.kind in (BODY, TITLE, TABLE) and r.label != "recovered"]
    wide = [r for r in body if r.width >= ctx.width * 0.25] or body
    if not wide:
        return None
    x0 = min(r.bbox[0] for r in wide)
    x1 = max(r.bbox[2] for r in wide)
    y0 = min(r.bbox[1] for r in wide)
    y1 = max(r.bbox[3] for r in wide)
    return (x0, y0, x1, y1)


def _body_line_height(regions: list[Region]) -> float:
    body = [r for r in regions if r.kind == BODY and r.line_height > 0]
    if not body:
        return 0.0
    # area-weighted median
    pairs = sorted((r.line_height, r.width * r.height) for r in body)
    total = sum(a for _, a in pairs)
    acc = 0.0
    for lh, a in pairs:
        acc += a
        if acc >= total / 2:
            return lh
    return pairs[-1][0]


def _classify_headers(regions: list[Region], ctx: PageContext) -> None:
    main = ctx.main_box
    for r in regions:
        if r.kind not in (BODY, TITLE) or line_count(r) > 2:
            continue
        top_zone = r.bbox[3] < ctx.height * 0.11
        bottom_zone = r.bbox[1] > ctx.height * 0.9
        narrow = main is None or r.width < 0.75 * (main[2] - main[0])
        if top_zone and narrow and (main is None or r.bbox[3] <= main[1] + 4 or r.label == "recovered"):
            others_above = [o for o in regions if o is not r and o.kind in (BODY, TITLE) and o.bbox[3] < r.bbox[1]]
            if not others_above:
                r.kind = HEADER
        elif bottom_zone and narrow:
            r.kind = FOOTER


def _classify_margins(regions: list[Region], ctx: PageContext, doc_type: str) -> None:
    main = ctx.main_box
    for r in regions:
        if r.kind == MARGIN:
            r.side = _side(r, main, ctx)
            continue
        if r.kind == PAGE_NUMBER and main is not None:
            # page numbers sit above or below the text; a "number" beside it, mid-page,
            # is a short marginal note (collation marks such as بلغ, verse counts…)
            if main[1] < r.cy < main[3] and (r.cx < main[0] or r.cx > main[2]):
                r.kind = MARGIN
                r.side = _side(r, main, ctx)
            continue
        if r.kind not in (BODY, TITLE) or main is None:
            continue
        overlap = G.h_overlap(r.bbox, main) / max(1.0, r.width)
        narrow = r.width < ctx.width * (0.4 if doc_type == "manuscript" else 0.3)
        outside = r.cx < main[0] or r.cx > main[2]
        if r.label == "recovered" and not outside:
            # recovered ink inside the main column is ordinary text
            continue
        if narrow and (overlap < 0.25 or outside):
            r.kind = MARGIN
            r.side = _side(r, main, ctx)


def _side(r: Region, main, ctx: PageContext) -> str:
    if main is None:
        return "right" if r.cx > ctx.width / 2 else "left"
    if r.bbox[3] <= main[1] and G.h_overlap(r.bbox, main) > 0.5 * r.width:
        return "top"
    if r.bbox[1] >= main[3] and G.h_overlap(r.bbox, main) > 0.5 * r.width:
        return "bottom"
    mid = (main[0] + main[2]) / 2
    return "right" if r.cx > mid else "left"


def _footnote_rules(ctx: PageContext) -> list[tuple[int, int, int, int]]:
    rules = imaging.find_horizontal_rules(ctx.gray, min_frac=0.08)
    main = ctx.main_box
    out = []
    for x0, y0, x1, y1 in rules:
        if y0 < ctx.height * 0.35:
            continue  # header underline
        width = x1 - x0
        ref = (main[2] - main[0]) if main else ctx.width * 0.7
        if width > 0.85 * ref:
            continue  # full-width rules are frames or table borders, not footnote separators
        out.append((x0, y0, x1, y1))
    out.sort(key=lambda b: b[1])
    return out


def _classify_footnotes(regions: list[Region], ctx: PageContext) -> None:
    rules = _footnote_rules(ctx)
    if rules:
        ctx.rule_y = float(rules[0][1])
    body_lh = ctx.body_line_height
    candidates = [r for r in regions if r.kind in (BODY, FOOTNOTE)]
    if ctx.rule_y is not None:
        for r in candidates:
            if r.bbox[1] >= ctx.rule_y - 2:
                r.kind = FOOTNOTE
    # smaller type at the foot of the page, below all larger body text
    if body_lh > 0:
        for r in candidates:
            if r.kind != BODY or not r.line_height:
                continue
            if r.line_height < 0.8 * body_lh and r.bbox[1] > ctx.height * 0.45:
                below_body = [o for o in regions if o.kind == BODY and o is not r and o.line_height >= 0.9 * body_lh and o.bbox[1] > r.bbox[1]]
                if not below_body:
                    r.kind = FOOTNOTE


def refine_after_ocr(regions: list[Region], ctx: PageContext | None, page) -> None:
    """Use the recognised text to fix region types (footnotes, page numbers)."""
    body = [r for r in regions if r.kind == BODY and r.text]
    if body:
        lowest = max(body, key=lambda r: r.bbox[1])
        body_lh = ctx.body_line_height if ctx else 0.0
        first = lowest.text.lstrip()
        looks_like_note = bool(FOOTNOTE_START_RE.match(first)) or first[:1] in ("=", "＝")
        smaller = body_lh <= 0 or (lowest.line_height and lowest.line_height <= 0.92 * body_lh)
        not_only = len([r for r in body if r is not lowest]) > 0
        if looks_like_note and smaller and not_only and lowest.bbox[1] > (ctx.height * 0.4 if ctx else 0):
            lowest.kind = FOOTNOTE
    for r in regions:
        if r.kind in (HEADER, FOOTER, PAGE_NUMBER, BODY) and r.text:
            t = r.text.strip()
            if re.fullmatch(r"[\s\-–—_.()\[\]٠-٩0-9۰-۹]+", t) and any(c.isdigit() for c in t) and len(t) <= 12:
                if r.kind == BODY and line_count(r) > 1:
                    continue
                if r.kind == BODY and ctx is not None and ctx.height * 0.12 < r.cy < ctx.height * 0.88:
                    continue
                r.kind = PAGE_NUMBER
                page.printed_number = re.sub(r"[^\d٠-٩۰-۹]", "", t)
    if not page.printed_number:
        for r in regions:
            if r.kind == HEADER and r.text:
                m = re.search(r"(?:^|\s|[-–—])([0-9٠-٩]{1,4})\s*$", r.text.strip()) or re.match(r"^\s*([0-9٠-٩]{1,4})(?:\s|[-–—])", r.text)
                if m:
                    page.printed_number = m.group(1)
                    break


# --------------------------------------------------------------------------- #
# Reading order
# --------------------------------------------------------------------------- #


def _order(regions: list[Region], mode: str) -> None:
    flow = [r for r in regions if r.kind in MAIN_FLOW_KINDS or r.kind == FIGURE]
    rest = [r for r in regions if r not in flow]
    if mode == "model":
        flow.sort(key=lambda r: r.order)
    elif mode == "top_to_bottom":
        flow.sort(key=lambda r: (round(r.bbox[1] / 10), -r.bbox[2]))
    else:
        flow = xy_cut_rtl(flow)
    # secondary regions: top-to-bottom, right-to-left
    rest.sort(key=lambda r: (_kind_rank(r.kind), r.bbox[1], -r.bbox[2]))
    ordered = flow + rest
    regions[:] = ordered
    for i, r in enumerate(regions):
        r.order = i


def _kind_rank(kind: str) -> int:
    return {HEADER: 0, MARGIN: 1, FOOTNOTE: 2, FOOTER: 3, PAGE_NUMBER: 4}.get(kind, 5)


def xy_cut_rtl(regions: list[Region]) -> list[Region]:
    if len(regions) <= 1:
        return list(regions)
    groups = _split(regions, axis=1)
    if len(groups) > 1:
        out: list[Region] = []
        for g in groups:  # top to bottom
            out.extend(xy_cut_rtl(g))
        return out
    groups = _split(regions, axis=0)
    if len(groups) > 1:
        out = []
        for g in reversed(groups):  # right to left
            out.extend(xy_cut_rtl(g))
        return out
    return sorted(regions, key=lambda r: (r.bbox[1], -r.bbox[2]))


def _split(regions: list[Region], axis: int) -> list[list[Region]]:
    """Split into groups separated by empty gaps along x (axis=0) or y (axis=1)."""
    lo, hi = (0, 2) if axis == 0 else (1, 3)
    items = sorted(regions, key=lambda r: r.bbox[lo])
    groups: list[list[Region]] = []
    current: list[Region] = []
    reach = -1e9
    for r in items:
        if current and r.bbox[lo] > reach - 1:
            groups.append(current)
            current = []
        current.append(r)
        reach = max(reach, r.bbox[hi]) if current[:-1] else r.bbox[hi]
    if current:
        groups.append(current)
    return groups
