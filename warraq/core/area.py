"""Re-reading part of a finished page: a box the user drew, or one region.

A box is resolved against the page's regions:

* regions lying mostly inside the box are read again together and replaced
  by one region of the box (keeping the kind of the largest one),
* a box inside one region re-reads the lines it touches, and the new lines
  replace those lines of the region in place; asking for a different kind
  ("these lines are a footnote") moves them out into a region of their own,
* a box over ink no region covers becomes a new region, typed by its
  position on the page like any other region.

Boxes are snapped to the ink: letters cut by the box's edge are taken whole
when most of them lies inside, and left out (whitened) otherwise.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import geometry as G
from . import imaging
from . import layout as L
from .document import BODY, MARGIN, TEXT_KINDS, Line, Page, Region

AREA_NOTE = "area"  # the region is a box the user drew: read exactly what lies inside it
KINDS = {"body", "title", "margin", "footnote", "header", "footer", "page_number", "caption", "table"}


@dataclass
class AreaPlan:
    region: Region  # what is read
    replaces: list[Region] = field(default_factory=list)  # regions the reading replaces
    host: Region | None = None  # region whose lines the reading replaces in place
    host_lines: tuple[int, int] = (0, 0)  # [start, end) of the host's (non-empty) lines
    split: bool = False  # the host's lines leave it for a region of their own kind


def prime_context(regions: list[Region], ctx: L.PageContext) -> None:
    """Fill in the page measurements layout analysis made when the page was first read."""
    ctx.main_box = L._main_box(regions, ctx)
    ctx.body_line_height = L._body_line_height(regions)
    rules = L._footnote_rules(ctx)
    ctx.rule_y = float(rules[0][1]) if rules else None


def plan_region(regions: list[Region], region_id: str, kind: str = "auto") -> AreaPlan:
    region = next((r for r in regions if r.id == region_id), None)
    if region is None:
        raise KeyError(region_id)
    if kind in KINDS:
        region.kind = kind
    region.edited = False
    return AreaPlan(region=region)


def plan_area(regions: list[Region], ctx: L.PageContext, bbox, kind: str, page_index: int, line_mode: str = "preserve") -> AreaPlan:
    sel = G.clamp_box(tuple(float(v) for v in bbox), ctx.width, ctx.height)
    if sel[2] - sel[0] < 4 or sel[3] - sel[1] < 4:
        raise ValueError("The selected area is too small.")
    explicit = kind if kind in KINDS else None
    text_regions = [r for r in regions if r.kind in TEXT_KINDS]
    new_id = _new_id(regions, page_index)

    covered = [r for r in text_regions if G.containment(r.bbox, sel) >= 0.5]
    if not covered:
        hosts = [r for r in text_regions if G.containment(sel, r.bbox) >= 0.6 and r.lines and r.text.strip()]
        host = max(hosts, key=lambda r: G.intersection_area(sel, r.bbox), default=None)
        if host is not None and not host.angle and line_mode != "paragraphs":
            span = _touched_lines(host, sel)
            if span is not None:
                a, b = span
                box = host.lines[a].bbox
                for ln in host.lines[a + 1 : b]:
                    box = G.union(box, ln.bbox)
                # whole lines: a box over part of a line re-reads all of it
                box = snap_to_ink(ctx, box)
                new_kind = explicit or host.kind
                region = Region(id=new_id, kind=new_kind, label="area", bbox=box, notes=[AREA_NOTE], side=host.side)
                return AreaPlan(region=region, host=host, host_lines=(a, b), split=new_kind != host.kind)
        if host is not None:
            # rotated notes and reflowed paragraphs can't be patched line by line: read it all again
            covered = [host]

    box = snap_to_ink(ctx, sel)
    if covered:
        biggest = max(covered, key=lambda r: G.area(r.bbox))
        region = Region(id=new_id, kind=explicit or biggest.kind, label="area", bbox=box, notes=[AREA_NOTE], side=biggest.side)
        return AreaPlan(region=region, replaces=covered)
    region = Region(id=new_id, kind=BODY, label="area", bbox=box, notes=[AREA_NOTE])
    region.kind, region.side = explicit or classify(region, regions, ctx), None
    return AreaPlan(region=region)


def classify(region: Region, regions: list[Region], ctx: L.PageContext) -> str:
    """Kind of a new region from its place on the page, by the rules layout analysis uses."""
    probe = copy.deepcopy(region)
    probe.kind, probe.label = BODY, "recovered"
    others = [copy.deepcopy(r) for r in regions]
    trial = others + [probe]
    L._measure_lines([probe], ctx)
    L._classify_headers(trial, ctx)
    L._classify_margins(trial, ctx, "auto")
    L._classify_footnotes(trial, ctx)
    region.side = probe.side
    return probe.kind


def apply(page: Page, plan: AreaPlan, reading_order: str) -> None:
    """Merge a finished reading into the page."""
    region = plan.region
    if plan.host is not None:
        host = plan.host
        a, b = plan.host_lines
        new_lines = [l for l in region.text.split("\n") if l.strip()]
        if plan.split:
            host.text = _splice(host.text, a, b, [])
            host.lines = host.lines[:a] + host.lines[b:]
            host.uncertain = [w for w in host.uncertain if w in host.text]
            if host.lines:
                box = host.lines[0].bbox
                for ln in host.lines[1:]:
                    box = G.union(box, ln.bbox)
                host.bbox = box
                _set_line_count(host, len(host.lines))
                page.regions.append(region)
            else:
                page.regions = [r for r in page.regions if r is not host]
                page.regions.append(region)
        else:
            host.text = _splice(host.text, a, b, new_lines)
            host.lines = host.lines[:a] + [Line(bbox=l.bbox, text=l.text, angle=l.angle, quad=l.quad) for l in region.lines] + host.lines[b:]
            host.uncertain = [w for w in dict.fromkeys(host.uncertain + region.uncertain) if w in host.text]
            host.flagged = host.flagged and region.flagged
            if "re-read area" not in host.notes:
                host.notes.append("re-read area")
            _set_line_count(host, len([l for l in host.text.split("\n") if l.strip()]))
            return
    elif plan.replaces:
        gone = {id(r) for r in plan.replaces}
        page.regions = [r for r in page.regions if id(r) not in gone]
        page.regions.append(region)
    elif not any(r is region for r in page.regions):
        page.regions.append(region)
    if region.kind == MARGIN and not region.side:
        region.side = "right" if region.cx > page.width / 2 else "left"
    L._order(page.regions, reading_order)


def snap_to_ink(ctx: L.PageContext, box) -> tuple[float, float, float, float]:
    """Grow or shrink ``box`` to the ink it holds: a letter (or a dot) mostly inside the box,
    or lying just over its edge, is taken whole; ink mostly outside it is left out."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    lh = _line_height(ctx, (x0, y0, x1, y1))
    m = int(max(12, 1.2 * lh))
    wx0, wy0, wx1, wy1 = max(0, x0 - m), max(0, y0 - m), min(ctx.width, x1 + m), min(ctx.height, y1 + m)
    sub = ctx.mask[wy0:wy1, wx0:wx1]
    if sub.size == 0 or not sub.any():
        return tuple(float(v) for v in box)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=8)
    bx0, by0, bx1, by1 = x0 - wx0, y0 - wy0, x1 - wx0, y1 - wy0
    inside = np.bincount(labels[max(0, by0) : by1, max(0, bx0) : bx1].ravel(), minlength=n)
    tol = 0.25 * lh
    keep = None
    for i in range(1, n):
        cx, cy, cw, ch, area = stats[i]
        whole_near = cx >= bx0 - tol and cy >= by0 - tol and cx + cw <= bx1 + tol and cy + ch <= by1 + tol
        if inside[i] >= 0.5 * area or (whole_near and inside[i] > 0) or (whole_near and ch < 0.5 * lh):
            b = (cx, cy, cx + cw, cy + ch)
            keep = b if keep is None else G.union(keep, b)
    if keep is None:
        return tuple(float(v) for v in box)
    pad = 2
    out = (keep[0] + wx0 - pad, keep[1] + wy0 - pad, keep[2] + wx0 + pad, keep[3] + wy0 + pad)
    return G.clamp_box(out, ctx.width, ctx.height)


def clip_to_box(img, box):
    """White out everything of ``img`` outside ``box`` (crop coordinates), and ink that lies
    mostly outside it, so only what the user chose is read."""
    from PIL import Image

    arr = np.asarray(img.convert("RGB")).copy()
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return img
    mask = imaging.ink_mask(imaging.to_gray(arr))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    inside = np.bincount(labels[y0:y1, x0:x1].ravel(), minlength=n)
    erase = np.zeros(n, dtype=bool)
    erase[1:] = inside[1:] < 0.5 * stats[1:, cv2.CC_STAT_AREA]
    arr[erase[labels]] = 255
    outside = np.ones((h, w), dtype=bool)
    outside[y0:y1, x0:x1] = False
    arr[outside] = 255
    return Image.fromarray(arr)


# --------------------------------------------------------------------------- #


def _new_id(regions: list[Region], page_index: int) -> str:
    taken = {r.id for r in regions}
    k = 1
    while f"p{page_index + 1}-a{k}" in taken:
        k += 1
    return f"p{page_index + 1}-a{k}"


def _touched_lines(host: Region, sel) -> tuple[int, int] | None:
    """[start, end) of the host's lines whose middle lies inside the box's height."""
    hits = [
        i
        for i, ln in enumerate(host.lines)
        if sel[1] <= (ln.bbox[1] + ln.bbox[3]) / 2 <= sel[3] and G.h_overlap(ln.bbox, sel) > 0
    ]
    if not hits:
        return None
    return min(hits), max(hits) + 1


def _splice(text: str, a: int, b: int, new: list[str]) -> str:
    """Replace non-empty lines [a, b) of ``text`` with ``new``; blank lines stay where they are."""
    out: list[str] = []
    k = 0
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        if k == a:
            out.extend(new)
        if not a <= k < b:
            out.append(line)
        k += 1
    if k <= a:  # the span starts past the last line
        out.extend(new)
    while out and not out[-1].strip():
        out.pop()
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out)


def _set_line_count(r: Region, n: int) -> None:
    r.notes = [x for x in r.notes if not x.startswith("lines:")] + [f"lines:{n}"]


def _line_height(ctx: L.PageContext, box) -> float:
    x0, y0, x1, y1 = box
    sub = ctx.mask[max(0, y0) : y1, max(0, x0) : x1]
    if sub.size and sub.any():
        bands = imaging.segment_lines(ctx.gray[max(0, y0) : y1, max(0, x0) : x1], sub)
        lh = imaging.median_line_height(bands)
        if lh:
            return float(lh)
    return float(ctx.body_line_height or max(20.0, ctx.height / 60))
