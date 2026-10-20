"""Turn recognised regions into the final page structure.

* footnote areas are split into individual notes and labelled,
* a footnote area that opens with "=" is the continuation of the previous
  page's last footnote and is merged into it (or kept and marked),
* marginal notes are collected and labelled,
* digits are made consistent across the document,
* the result is a list of :class:`PageView` objects that every exporter uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import text as T
from .document import (
    BODY,
    CAPTION,
    FOOTER,
    FOOTNOTE,
    FORMULA,
    HEADER,
    MARGIN,
    PAGE_NUMBER,
    TABLE,
    TITLE,
    Document,
    Footnote,
    MarginNote,
    Page,
)

DIGITS = "0-9٠-٩۰-۹"
# A footnote marker at the start of a line.
MARKER_RES = [
    re.compile(rf"^\s*(?P<m>[\(\[（﴿]\s*(?P<n>[{DIGITS}]{{1,3}})\s*[\)\]）﴾])\s*[-–—:.]?\s*"),
    re.compile(rf"^\s*(?P<m>(?P<n>[{DIGITS}]{{1,3}})\s*[-–—).:])\s+"),
    re.compile(r"^\s*(?P<m>(?P<n>[¹²³⁴⁵⁶⁷⁸⁹⁰]{1,3}))\s*"),
    re.compile(r"^\s*(?P<m>\*{1,3})\s*"),
]
SUPERSCRIPT = str.maketrans("¹²³⁴⁵⁶⁷⁸⁹⁰", "1234567890")


def _number(s: str | None) -> int | None:
    if not s:
        return None
    s = T.to_western_digits(s).translate(SUPERSCRIPT)
    return int(s) if s.isdigit() else None


def match_marker(line: str):
    for rx in MARKER_RES:
        m = rx.match(line)
        if m:
            return m
    return None


# --------------------------------------------------------------------------- #
# Parsing one page's footnote area
# --------------------------------------------------------------------------- #


@dataclass
class ParsedNote:
    marker: str | None
    number: int | None
    lines: list[str] = field(default_factory=list)
    region_ids: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(l.strip() for l in self.lines if l.strip()).strip()


@dataclass
class ParsedArea:
    continuation: str = ""
    continuation_region: str | None = None
    notes: list[ParsedNote] = field(default_factory=list)
    last_continues: bool = False
    continuation_seen_only: bool = False  # the "=" was found in the image, not in the text


def continuation_markers(settings) -> list[str]:
    raw = settings.footnotes.continuation_markers or "="
    return [m.strip() for m in raw.split(",") if m.strip()] or ["="]


def parse_footnote_area(
    lines_with_region: list[tuple[str, str]],
    settings,
    starts_with_equals: bool = False,
    ends_with_equals: bool = False,
) -> ParsedArea:
    fs = settings.footnotes
    markers = continuation_markers(settings)
    area = ParsedArea()
    lines = [(l, rid) for l, rid in lines_with_region if l.strip()]
    if not lines:
        return area
    i = 0
    first = lines[0][0].lstrip()
    starts_cont = any(first.startswith(m) for m in markers)
    if not starts_cont and starts_with_equals and not match_marker(first):
        starts_cont = True  # the sign is visible on the page even if the model dropped it
        area.continuation_seen_only = True
    if starts_cont:
        cont_lines = []
        while i < len(lines):
            line, rid = lines[i]
            if i > 0 and match_marker(line):
                break
            cont_lines.append(line)
            area.continuation_region = area.continuation_region or rid
            i += 1
        text = " ".join(l.strip() for l in cont_lines)
        if fs.strip_markers:
            text = _strip_leading_marks(text, markers)
            text = _strip_trailing_marks(text, markers)
        area.continuation = text.strip()
    current: ParsedNote | None = None
    expected: int | None = None
    body_lines = [l for l, _ in lines[i:]]
    lengths = sorted(len(l.strip()) for l in body_lines if l.strip())
    typical = lengths[len(lengths) // 2] if lengths else 0
    prev_line = ""
    for line, rid in lines[i:]:
        m = match_marker(line)
        num = _number(m.group("n")) if m and "n" in m.groupdict() and m.group("n") else None
        is_new = False
        if m:
            prev_short = bool(prev_line) and typical > 0 and len(prev_line.strip()) < 0.72 * typical
            if current is None:
                is_new = True
            elif num is None:  # asterisk-style
                is_new = m.group("m").startswith("*")
            elif expected is None or num == 1 or expected <= num <= expected + 2:
                # a small jump is almost always a misread marker, not a number in the text
                is_new = True
            elif prev_short and num < 100:
                is_new = True  # previous note ended early on its line: a new note starts here
        if is_new:
            current = ParsedNote(marker=m.group("m").strip(), number=num, lines=[line[m.end():]], region_ids=[rid])
            area.notes.append(current)
            if num is not None:
                # count notes rather than trusting a possibly misread number
                expected = 2 if num == 1 else ((expected + 1) if expected is not None else num + 1)
        else:
            if current is None:
                current = ParsedNote(marker=None, number=None, lines=[], region_ids=[rid])
                area.notes.append(current)
            current.lines.append(line)
            if rid not in current.region_ids:
                current.region_ids.append(rid)
        prev_line = line
    if fs.split_inline_notes:
        area.notes = _split_inline(area.notes)
    if area.notes:
        last = area.notes[-1]
        joined = last.text
        ends = any(joined.endswith(m) for m in markers)
        if ends or ends_with_equals:
            area.last_continues = True
            if fs.strip_markers and ends:
                last.lines = [_strip_trailing_marks(" ".join(last.lines), markers)]
    elif area.continuation and (ends_with_equals or any(area.continuation.endswith(m) for m in markers)):
        area.last_continues = True
    return area


def _strip_leading_marks(text: str, markers: list[str]) -> str:
    t = text.lstrip()
    changed = True
    while changed:
        changed = False
        for m in markers:
            if t.startswith(m):
                t = t[len(m) :].lstrip()
                changed = True
    return t


def _strip_trailing_marks(text: str, markers: list[str]) -> str:
    t = text.rstrip()
    changed = True
    while changed:
        changed = False
        for m in markers:
            if t.endswith(m):
                t = t[: -len(m)].rstrip()
                changed = True
    return t


def _split_inline(notes: list[ParsedNote]) -> list[ParsedNote]:
    """Split "(١) foo. (٢) bar." written on one line into two notes."""
    out: list[ParsedNote] = []
    for note in notes:
        if note.number is None:
            out.append(note)
            continue
        text = note.text
        nxt = note.number + 1
        pieces = [(note.marker, note.number, text)]
        while True:
            marker, number, body = pieces[-1]
            found = None
            for rx in (rf"(?<=\s)([\(\[（]\s*(?:{nxt}|{T.to_arabic_digits(str(nxt))})\s*[\)\]）])\s", rf"(?<=[.؛:])\s*([\(\[（]\s*(?:{nxt}|{T.to_arabic_digits(str(nxt))})\s*[\)\]）])\s"):
                m = re.search(rx, body)
                if m and m.start() > 3:
                    found = m
                    break
            if not found:
                break
            pieces[-1] = (marker, number, body[: found.start()].rstrip())
            pieces.append((found.group(1), nxt, body[found.end() :].lstrip()))
            nxt += 1
        for marker, number, body in pieces:
            out.append(ParsedNote(marker=marker, number=number, lines=[body], region_ids=list(note.region_ids)))
    return out


# --------------------------------------------------------------------------- #
# Page views
# --------------------------------------------------------------------------- #


@dataclass
class FootnoteView:
    label: str
    text: str
    marker: str | None
    region_ids: list[str]
    continued_from_previous: bool = False
    merged_from_page: int | None = None


@dataclass
class MarginView:
    label: str
    side: str | None
    text: str
    region_id: str


@dataclass
class BlockView:
    kind: str
    text: str
    region_id: str


@dataclass
class PageView:
    page: Page
    number: int
    printed_number: str
    headers: list[str] = field(default_factory=list)
    footers: list[str] = field(default_factory=list)
    blocks: list[BlockView] = field(default_factory=list)
    margins: list[MarginView] = field(default_factory=list)
    footnotes: list[FootnoteView] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text)


def detect_digit_style(document: Document) -> str | None:
    arabic = western = 0
    for page in document.pages:
        for r in page.regions:
            for ch in r.text:
                if "٠" <= ch <= "٩" or "۰" <= ch <= "۹":
                    arabic += 1
                elif "0" <= ch <= "9":
                    western += 1
    if arabic == western == 0:
        return None
    # The model often writes ١٢ as 12 but almost never the reverse, so even a
    # modest share of Arabic-Indic digits means the book uses them.
    return "arabic_indic" if arabic / (arabic + western) >= 0.15 else "western"


def assemble(document: Document, settings) -> list[PageView]:
    fs, ms = settings.footnotes, settings.margins
    style = None
    if settings.text.digits == "auto":
        style = detect_digit_style(document)
        document.meta["digit_style"] = style

    def fix(s: str) -> str:
        if style == "arabic_indic":
            return T.to_arabic_digits(s)
        if style == "western":
            return T.to_western_digits(s)
        return s

    views: list[PageView] = []
    areas: list[ParsedArea] = []
    pages_in_order: list[Page] = []
    for page in document.pages:
        view = PageView(page=page, number=page.index + 1, printed_number=fix(page.printed_number))
        foot_lines: list[tuple[str, str]] = []
        starts_eq = ends_eq = False
        foot_regions = [r for r in page.regions if r.kind == FOOTNOTE]
        for k, r in enumerate(foot_regions):
            if k == 0 and "starts_with_equals" in r.notes:
                starts_eq = True
            if k == len(foot_regions) - 1 and "ends_with_equals" in r.notes:
                ends_eq = True
            for line in fix(r.text).split("\n"):
                foot_lines.append((line, r.id))
        hf = settings.layout.headers_footers
        for r in page.regions:
            text = fix(r.text).strip()
            if not text:
                continue
            if r.kind in (BODY, TITLE, TABLE, FORMULA, CAPTION):
                view.blocks.append(BlockView(r.kind, text, r.id))
            elif r.kind == MARGIN:
                view.margins.append(MarginView("", r.side, text, r.id))
            elif r.kind in (HEADER, FOOTER, PAGE_NUMBER):
                if hf == "drop" or r.label == "line_number":
                    continue  # (an edition's line counter is never worth keeping)
                if hf == "inline" and r.label != "catchword":  # the catchword repeats the next page's first word
                    if r.kind == HEADER:
                        view.blocks.insert(0, BlockView(r.kind, text, r.id))
                    else:
                        view.blocks.append(BlockView(r.kind, text, r.id))
                elif r.kind == HEADER:
                    view.headers.append(text)
                else:
                    view.footers.append(text)
        for n, m in enumerate(view.margins, 1):
            m.label = ms.label_format.replace("{n}", str(n))
        area = parse_footnote_area(foot_lines, settings, starts_eq, ends_eq)
        areas.append(area)
        views.append(view)
        pages_in_order.append(page)
    _resolve_leading_text(areas, pages_in_order)
    for page, area in zip(pages_in_order, areas):
        page.continuation = area.continuation

    # Footnote entries, then continuation merging across pages.
    counter = 0
    for idx, (view, area) in enumerate(zip(views, areas)):
        page = view.page
        page.footnotes = []
        entries: list[FootnoteView] = []
        prev_label = 0
        for k, note in enumerate(area.notes):
            if fs.numbering == "per_page":
                num = k + 1
            elif fs.numbering == "document":
                counter += 1
                num = counter
            else:
                num = note.number if note.number is not None else prev_label + 1
                if k > 0 and num != prev_label + 1:
                    num = prev_label + 1  # misread or skipped marker: keep the sequence intact
            prev_label = num
            label_num = T.to_arabic_digits(str(num)) if fs.label_digits == "arabic_indic" else str(num)
            entries.append(FootnoteView(fs.label_format.replace("{n}", label_num), note.text, note.marker, note.region_ids))
            page.footnotes.append(
                Footnote(label=label_num, marker=note.marker, text=note.text, page_index=page.index, region_ids=note.region_ids)
            )
        view.footnotes = entries
    for idx, (view, area) in enumerate(zip(views, areas)):
        if not area.continuation:
            continue
        prev_view = views[idx - 1] if idx > 0 else None
        consecutive = prev_view is not None and prev_view.page.source_index <= view.page.source_index
        target = prev_view.footnotes[-1] if (prev_view and prev_view.footnotes and consecutive) else None
        if fs.continuation_mode == "merge" and target is not None:
            target.text = (target.text + " " + area.continuation).strip()
            target.merged_from_page = view.number
            pf = prev_view.page.footnotes[-1]
            pf.merged_continuation = area.continuation
            pf.merged_from_page = view.page.index
            pf.continues_on_next = True
        else:
            base = target.label if target is not None else ""
            num = re.sub(r"[^\d٠-٩]", "", base) or "="
            label = fs.continued_label.replace("{n}", num) if target is not None else "[=]"
            view.footnotes.insert(
                0,
                FootnoteView(label, area.continuation, "=", [area.continuation_region] if area.continuation_region else [], continued_from_previous=True),
            )
            view.page.footnotes.insert(
                0,
                Footnote(label=num, marker="=", text=area.continuation, page_index=view.page.index, continued_from_previous=True),
            )
    for view in views:
        view.page.margin_notes = [MarginNote(m.label, m.text, m.side, m.region_id) for m in view.margins]
    if fs.link_body_markers:
        for view in views:
            _link_markers(view, settings)
    return views


_FINAL = (".", "۔", "؟", "?", "!", "»", ")", "]")


def _finished(text: str) -> bool:
    """Does a footnote end like a finished note (a full stop, a closing reference)?"""
    t = text.rstrip()
    return bool(t) and t.endswith(_FINAL) and not t.endswith("=")


def _resolve_leading_text(areas: list[ParsedArea], pages: list[Page]) -> None:
    """Decide what text standing before a page's first numbered footnote is.

    A footnote that runs over continues at the top of the next page's footnotes, after
    "=".  Without that evidence in the text it is easy to get wrong in both directions:
    an "=" seen only in the image of a page whose previous footnote ended properly is
    no continuation, and a note whose number was misread is no continuation either.
    So, with the previous page's last note and this page's numbering as evidence:

    * an "=" found only in the image is believed when the previous note is unfinished;
    * unnumbered text followed by note 1 can only continue the previous page;
    * unnumbered text followed by note n (n > 1) is note n-1, its number misread;
    * unnumbered text alone continues the previous page when that note is unfinished.
    """
    for idx, area in enumerate(areas):
        prev = areas[idx - 1] if idx > 0 and pages[idx - 1].source_index <= pages[idx].source_index else None
        prev_text = (prev.notes[-1].text if prev.notes else prev.continuation) if prev is not None else ""
        prev_open = prev is not None and bool(prev_text) and (prev.last_continues or not _finished(prev_text))
        if area.continuation and area.continuation_seen_only and not prev_open:
            # the mark seen was not a continuation sign: this is the page's own first note
            area.notes.insert(0, ParsedNote(marker=None, number=None, lines=[area.continuation], region_ids=[r for r in [area.continuation_region] if r]))
            area.continuation, area.continuation_region = "", None
        if area.continuation or not area.notes or area.notes[0].marker is not None:
            continue
        lead = area.notes[0]
        following = next((n.number for n in area.notes[1:] if n.number is not None), None)
        if following is not None and following > 1 and len(area.notes) > 1 and area.notes[1].number == following:
            lead.number = following - 1  # its marker was misread
        elif following == 1 or (following is None and prev_open):
            area.continuation = lead.text
            area.continuation_region = lead.region_ids[0] if lead.region_ids else None
            area.notes.pop(0)


def _link_markers(view: PageView, settings) -> None:
    """Rewrite '(١)' in the main text into the footnote label used in the footnote section."""
    mapping: dict[int, str] = {}
    for fn in view.footnotes:
        if fn.continued_from_previous or not fn.marker:
            continue
        n = _number(re.sub(r"[^\d٠-٩۰-۹¹²³⁴⁵⁶⁷⁸⁹⁰]", "", fn.marker))
        if n is not None:
            mapping[n] = fn.label
    if not mapping:
        return

    def repl(m: re.Match) -> str:
        n = _number(m.group(1))
        return mapping.get(n, m.group(0)) if n is not None else m.group(0)

    rx = re.compile(rf"[\(\[（]\s*([{DIGITS}]{{1,3}})\s*[\)\]）]")
    for b in view.blocks:
        if b.kind in (BODY, TITLE, CAPTION):
            b.text = rx.sub(repl, b.text)
