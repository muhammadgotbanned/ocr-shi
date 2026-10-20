"""Page zones from the type itself: footnotes with and without a rule, running headers,
headings at the top of a page, page numbers and catchwords."""

import numpy as np
from PIL import Image, ImageDraw

from warraq.core import imaging, layout as L, typography as T
from warraq.core.document import BODY, FOOTER, FOOTNOTE, HEADER, PAGE_NUMBER, TITLE
from warraq.models.layout_model import Detection
from warraq.settings import Settings

W, H = 1200, 1700
X0, X1 = 150, 1050


def _line(d, top, size, x0=X0, x1=X1, stroke=4, vowels=False):
    """A line of 'type': words made of a baseline stroke and stems, optionally with a row
    of vowel marks above and below (which makes the line's ink taller)."""
    base = top + size
    x = x1
    while x - 70 > x0:
        w = 50 + (x % 37) % 20
        d.rectangle((x - w, base - stroke, x, base), fill=0)
        for sx in range(x - w + 6, x, 14):
            d.rectangle((sx, base - size, sx + stroke, base), fill=0)
        if vowels:
            d.rectangle((x - w + 10, top - 14, x - w + 22, top - 10), fill=0)
            d.rectangle((x - w + 30, base + 9, x - w + 42, base + 13), fill=0)
        x -= w + 22


def _page(rule_y=None, foot=True, header=False, heading=False, folio=False, catchword=False, body_lines=14, foot_size=None, foot_vowels=False):
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    y = 140
    if header:
        _line(d, 70, 22, x0=400, x1=800, stroke=3)
        d.rectangle((X0, 108, X1, 111), fill=0)
    if heading:
        _line(d, y, 30, x0=520, x1=680, stroke=9)
        y += 90
    for _ in range(body_lines):
        _line(d, y, 30, vowels=True)
        y += 72
    last_body = y
    if catchword:
        _line(d, y + 10, 30, x0=X0, x1=X0 + 110)
        y += 80
    if rule_y is not None:
        d.rectangle((X1 - 300, rule_y, X1, rule_y + 3), fill=0)
        y = rule_y + 30
    elif foot:
        y += 40
    if foot:
        size = foot_size or 22
        for k in range(4):
            _line(d, y, size, x0=X0 if k % 2 else 600, stroke=3, vowels=foot_vowels)
            y += size + (42 if foot_vowels else 22)
    if folio:
        _line(d, H - 110, 22, x0=560, x1=640, stroke=4)
    return np.asarray(img), last_body


def test_footnotes_without_a_rule_are_found_by_their_smaller_type():
    gray, last_body = _page()
    t = T.analyze(gray)
    assert t.foot_top is not None and not t.foot_rule
    assert last_body - 72 < t.foot_top < last_body + 60


def test_footnote_rule_is_found_even_close_to_the_notes():
    gray, last_body = _page(rule_y=1250)
    t = T.analyze(gray)
    assert t.foot_rule and 1240 <= t.foot_top <= 1260


def test_rule_over_body_sized_type_starts_no_footnotes():
    # a divider with text set like the body under it (a poem, a table) is not a footnote rule
    gray, _ = _page(rule_y=1250, foot_size=30, foot_vowels=True)
    assert T.analyze(gray).foot_top is None


def test_running_header_under_its_rule():
    gray, _ = _page(header=True)
    t = T.analyze(gray)
    assert t.header_bottom is not None and 95 <= t.header_bottom <= 145


def test_heading_at_the_top_is_a_title_not_a_header():
    gray, _ = _page(heading=True, foot=False)
    t = T.analyze(gray)
    assert t.header_bottom is None
    assert len(t.titles) == 1 and t.titles[0][0] >= 500


def test_page_number_and_catchword():
    gray, _ = _page(folio=True, catchword=True, foot=False)
    t = T.analyze(gray)
    assert len(t.folios) == 1 and t.folios[0][1] > H - 130
    assert t.catchword is not None and t.catchword[2] <= X0 + 130


def _analyze(gray, dets):
    ctx = L.build_context(gray)
    return L.analyze(dets, ctx, Settings(), 0)


def test_detector_block_across_body_and_footnotes_is_cut():
    gray, last_body = _page(folio=True)
    # the detector saw one big text block, footnotes and page number included
    regions = _analyze(gray, [Detection("text", 0.9, (X0 - 5, 120, X1 + 5, H - 80), 1, None)])
    kinds = {r.kind for r in regions}
    assert {BODY, FOOTNOTE, PAGE_NUMBER} <= kinds, [(r.kind, r.bbox) for r in regions]
    body = next(r for r in regions if r.kind == BODY)
    note = next(r for r in regions if r.kind == FOOTNOTE)
    assert body.bbox[3] < last_body + 20 < note.bbox[1] + 40
    assert L.line_count(note) == 4


def test_header_joined_to_the_body_is_cut_and_heading_kept():
    gray, _ = _page(header=True, foot=False)
    regions = _analyze(gray, [Detection("text", 0.9, (X0 - 5, 60, X1 + 5, 1200), 1, None)])
    assert [r.kind for r in sorted(regions, key=lambda r: r.bbox[1])][:2] == [HEADER, BODY]
    gray, _ = _page(heading=True, foot=False)
    regions = _analyze(gray, [Detection("paragraph_title", 0.8, (510, 130, 690, 185), 1, None), Detection("text", 0.9, (X0 - 5, 220, X1 + 5, 1250), 2, None)])
    assert sorted(regions, key=lambda r: r.bbox[1])[0].kind == TITLE


def test_catchword_is_page_furniture():
    gray, _ = _page(catchword=True, foot=False)
    regions = _analyze(gray, [Detection("text", 0.9, (X0 - 5, 120, X1 + 5, 1250), 1, None)])
    catch = [r for r in regions if r.label == "catchword"]
    assert len(catch) == 1 and catch[0].kind == FOOTER


def test_whole_page_mode_splits_zones():
    gray, _ = _page(header=True, folio=True)
    st = Settings()
    st.layout.mode = "full_page"
    regions = L.full_page(L.build_context(gray), st, 0)
    assert sorted(r.kind for r in regions) == sorted([HEADER, BODY, FOOTNOTE, PAGE_NUMBER])


def test_manuscript_has_no_footnotes():
    gray, _ = _page()
    st = Settings()
    st.layout.document_type = "manuscript"
    dets = [Detection("text", 0.9, (X0 - 5, 120, X1 + 5, 1100), 1, None), Detection("footnote", 0.8, (X0 - 5, 1180, X1 + 5, 1400), 2, None)]
    regions = L.analyze(dets, L.build_context(gray), st, 0)
    low = max(regions, key=lambda r: r.bbox[1])
    assert low.kind == "margin" and low.side == "bottom"
