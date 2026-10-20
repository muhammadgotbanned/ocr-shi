"""What the type on a page says about its parts, without any model.

A printed Arabic page is built from a few zones that the typesetter keeps apart
with size, spacing and rules:

* a running header at the top (book or chapter title and the page number), set
  off by a rule or a wide gap, never larger than the body type,
* the body, in the largest regular type, often fully vocalised,
* headings (باب, فصل, a chapter title): short centred lines in bold or larger type,
* the footnotes at the foot, in smaller, tighter, usually unvocalised type,
  under a short rule or simply after a wider gap,
* the page number, a short line at the top or bottom, and in manuscripts and old
  prints the catchword (the next page's first word) at the bottom left.

Layout analysis uses these zones to check and correct what the detector says,
and on its own when the whole page is read as one block.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from . import imaging

Box = tuple[float, float, float, float]


@dataclass
class TypeLine:
    top: int
    bottom: int
    left: int
    right: int
    stroke: float = 0.0  # median stroke thickness (bold type is thicker)

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def box(self) -> Box:
        return (float(self.left), float(self.top), float(self.right), float(self.bottom))


@dataclass
class Typography:
    lines: list[TypeLine] = field(default_factory=list)  # lines of the main column, top to bottom
    column: tuple[int, int] | None = None  # x-range of the main column
    body_height: float = 0.0  # typical body line height (ink, vowel marks included)
    body_gap: float = 0.0  # typical blank space between body lines
    body_stroke: float = 0.0
    rules: list[Box] = field(default_factory=list)  # short horizontal rules
    header_bottom: float | None = None  # the running header ends above this y
    foot_top: float | None = None  # the footnotes start at this y
    foot_rule: bool = False  # ... marked by a rule
    titles: list[Box] = field(default_factory=list)  # headings
    folios: list[Box] = field(default_factory=list)  # page numbers
    catchword: Box | None = None

    def is_title(self, box: Box) -> bool:
        """``box`` is (mostly) heading lines."""
        return _covered(box, self.titles)

    def is_folio(self, box: Box) -> bool:
        return _covered(box, self.folios)


def analyze(gray: np.ndarray, mask: np.ndarray | None = None) -> Typography:
    if mask is None:
        mask = imaging.ink_mask(gray)
    h, w = mask.shape
    typo = Typography()
    if h < 60 or w < 60 or not mask.any():
        return typo
    first = imaging.segment_lines(gray, mask)
    wide = [b for b in first if b.right - b.left >= 0.5 * max((x.right - x.left for x in first), default=0)]
    if len(wide) < 3:
        return typo
    x0 = int(np.median([b.left for b in wide]))
    x1 = int(np.median([b.right for b in wide]))
    if x1 - x0 < 0.25 * w:
        return typo
    typo.column = (x0, x1)
    # read the column alone: marginal notes and line counters beside it would blur its lines
    pad = int(0.015 * w)
    c0, c1 = max(0, x0 - pad), min(w, x1 + pad)
    col_mask = mask[:, c0:c1].copy()
    # rules first: a rule touching a line (a header rule under its text, a footnote rule
    # close above the first note) would otherwise become part of that line
    rules = _take_rules(col_mask, x1 - x0, h)
    typo.rules = [(r[0] + c0, r[1], r[2] + c0, r[3]) for r in rules]
    bands = imaging.segment_lines(gray[:, c0:c1], col_mask)
    lines = []
    for b in bands:
        sub = col_mask[b.top : b.bottom, b.left : b.right]
        if int(sub.sum()) < 30:
            continue
        lines.append(TypeLine(b.top, b.bottom, c0 + b.left, c0 + b.right, _stroke(sub)))
    if lines:
        # what is left of a rule or a scanner edge: a flat smear of hairline ink, not type
        typical = float(np.median([l.stroke for l in lines]))
        lines = [l for l in lines if l.stroke >= 0.4 * typical or l.height >= 0.3 * float(np.median([x.height for x in lines]))]
    typo.lines = lines
    if len(lines) < 3:
        return typo
    col_w = float(x1 - x0)
    full = [l for l in lines if l.width >= 0.6 * col_w] or lines
    mid = full[len(full) // 5 : len(full) - len(full) // 5] or full
    typo.body_height = float(np.median([l.height for l in mid]))
    typo.body_stroke = float(np.median([l.stroke for l in mid]))
    gaps = [b.top - a.bottom for a, b in zip(lines, lines[1:]) if a in mid and b in mid]
    # lines of dense, vocalised type touch (no blank row between them): a gap is only
    # telling when it is a real share of a line
    typo.body_gap = max(float(np.median(gaps)) if gaps else 0.0, 0.12 * typo.body_height, 1.0)
    _find_catchword(typo, h)
    _find_folios(typo, h)
    _find_titles(typo)
    _find_header(typo, h)
    _find_footnotes(typo, h)
    return typo


# --------------------------------------------------------------------------- #


def _take_rules(mask: np.ndarray, col_w: int, page_h: int) -> list[Box]:
    """Find thin horizontal rules in ``mask`` and erase them from it (in place)."""
    length = max(60, int(0.12 * col_w))
    long_runs = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    if not long_runs.any():
        return []
    n, labels, stats, _ = cv2.connectedComponentsWithStats(long_runs, connectivity=8)
    thin = max(6, int(0.006 * page_h))
    rules: list[Box] = []
    for i in range(1, n):
        x, y, cw, ch, _ = stats[i]
        if cw >= length and ch <= thin:
            rules.append((float(x), float(y), float(x + cw), float(y + ch)))
            # the stroke and its anti-aliased fringe
            stroke = cv2.dilate((labels == i).astype(np.uint8), np.ones((7, 3), np.uint8))
            mask[stroke.astype(bool)] = 0
    return sorted(rules, key=lambda r: r[1])


def _stroke(sub: np.ndarray) -> float:
    """Median thickness of the ink's horizontal strokes (vertical run lengths)."""
    cols = sub.T[:: max(1, sub.shape[1] // 160)]
    runs: list[int] = []
    for col in cols:
        d = np.diff(np.concatenate(([0], col.astype(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        runs.extend((ends - starts).tolist())
    return float(np.median(runs)) if runs else 0.0


def _covered(box: Box, parts: list[Box]) -> bool:
    inside = [p for p in parts if _inside(p, box)]
    area = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
    return bool(inside) and sum((p[2] - p[0]) * (p[3] - p[1]) for p in inside) >= 0.35 * area


def _inside(inner: Box, outer: Box, slack: float = 4.0) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] - slack <= cx <= outer[2] + slack and outer[1] - slack <= cy <= outer[3] + slack


def _rule_between(typo: Typography, a: TypeLine, b: TypeLine) -> float | None:
    """y of a rule separating line ``a`` from the line ``b`` under it.  A rule may run
    through either line's band (descenders hanging below a header rule, vowel marks
    rising above a footnote rule), so it is placed between the lines' middles."""
    for r in typo.rules:
        y = (r[1] + r[3]) / 2
        if (a.top + a.bottom) / 2 < y < (b.top + b.bottom) / 2:
            return y
    return None


def _find_folios(typo: Typography, page_h: int) -> None:
    """A short line alone at the very top or bottom of the column (centred at the bottom;
    at the top it may also stand at a corner)."""
    x0, x1 = typo.column
    col_w = x1 - x0
    for idx in (0, len(typo.lines) - 1):
        l = typo.lines[idx]
        if l.box == typo.catchword or (idx > 0 and abs(l.cx - (x0 + x1) / 2) > 0.2 * col_w):
            continue
        nb = typo.lines[1] if idx == 0 else typo.lines[-2]
        gap = (nb.top - l.bottom) if idx == 0 else (l.top - nb.bottom)
        # a heading such as باب is as short, but heavy and of full height; page numbers
        # are digits: no taller than the body's letters without their vowel marks
        heavy = typo.body_stroke and l.stroke >= 1.25 * typo.body_stroke
        small = l.height <= 0.75 * typo.body_height
        if l.width <= 0.14 * col_w and gap >= 0.8 * typo.body_gap and l.height <= 1.3 * typo.body_height and (small or not heavy):
            typo.folios.append(l.box)


def _find_titles(typo: Typography) -> None:
    """Headings: short centred lines in heavier or larger type than the body."""
    x0, x1 = typo.column
    col_w, mid = x1 - x0, (x0 + x1) / 2
    for l in typo.lines:
        if typo.is_folio(l.box):
            continue
        centred = abs(l.cx - mid) <= 0.08 * col_w and l.width <= 0.75 * col_w
        heavy = typo.body_stroke and l.stroke >= 1.3 * typo.body_stroke
        large = l.height >= 1.35 * typo.body_height
        if centred and (heavy or large):
            typo.titles.append(l.box)


def _find_header(typo: Typography, page_h: int) -> None:
    """The running header: the top line(s), cut off from the body by a rule, or by a wide
    gap when they are not a heading.  Never more than two lines, never mid-page."""
    lines = [l for l in typo.lines if not typo.is_folio(l.box)]
    if len(lines) < 4:
        return
    for k in (1, 2):
        above, below = lines[k - 1], lines[k]
        if above.bottom > 0.2 * page_h:
            break
        gap = below.top - above.bottom
        heading = any(typo.is_title(l.box) for l in lines[:k])
        if _rule_between(typo, above, below) is not None or (gap >= 2.2 * typo.body_gap and gap >= 0.35 * typo.body_height and not heading and above.height <= 1.1 * typo.body_height):
            typo.header_bottom = (above.bottom + below.top) / 2
            return


def _find_footnotes(typo: Typography, page_h: int) -> None:
    """Where the footnotes begin: under a short rule in the lower part of the page, or,
    without a rule, where the lines turn clearly smaller and tighter after a wider gap."""
    lines = [l for l in typo.lines if not typo.is_folio(l.box) and l.box != typo.catchword]
    if len(lines) < 4:
        return
    H, G = typo.body_height, typo.body_gap
    for a, b in zip(lines, lines[1:]):
        y = _rule_between(typo, a, b) if b.top >= 0.35 * page_h else None
        if y is None:
            continue
        rest = [l for l in lines if l.top >= b.top]
        if rest and np.median([l.height for l in rest]) <= 0.95 * H:  # footnote type is smaller (0.5–0.92 of the body on real pages)
            typo.foot_top, typo.foot_rule = float(min(b.top, y)) - 2, True
            return
    # no rule: the longest run of small lines at the foot, after a gap
    small = [l.height <= 0.8 * H for l in lines]
    k = len(lines)
    while k > 0:
        # one medium line between small ones (a footnote quoting a vocalised verse) stays in
        medium = 1 < k < len(lines) and lines[k - 1].height <= 0.92 * H and small[k] and small[k - 2]
        if not (small[k - 1] or medium):
            break
        k -= 1
    if k >= len(lines) or k < 2 or lines[k].top < 0.4 * page_h:
        return
    zone = lines[k:]
    size = float(np.median([l.height for l in zone]))
    gap = lines[k].top - lines[k - 1].bottom
    pitch_zone = np.median([b.top - a.top for a, b in zip(zone, zone[1:])]) if len(zone) >= 2 else None
    pitch_body = np.median([b.top - a.top for a, b in zip(lines[:k], lines[1:k])]) if k >= 2 else None
    tighter = pitch_zone is not None and pitch_body is not None and pitch_zone <= 0.85 * pitch_body
    if size > 0.75 * H:
        return
    if gap >= 1.4 * G or (tighter and gap >= 1.0 * G and len(zone) >= 2):
        typo.foot_top = float(lines[k].top) - 2


def _find_catchword(typo: Typography, page_h: int) -> None:
    """The next page's first word, alone under the last line at the left end of the column."""
    if len(typo.lines) < 4:
        return
    x0, x1 = typo.column
    col_w = x1 - x0
    last, prev = typo.lines[-1], typo.lines[-2]
    if last.right > x0 + 0.4 * col_w and len(typo.lines) >= 5:
        last, prev = typo.lines[-2], typo.lines[-3]  # under a page number
    close = last.top - prev.bottom <= max(3 * typo.body_gap, 1.2 * typo.body_height)
    if close and last.width <= 0.2 * col_w and last.right <= x0 + 0.4 * col_w and last.top >= 0.6 * page_h and prev.width >= 0.5 * col_w:
        typo.catchword = last.box
