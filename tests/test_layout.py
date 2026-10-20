import numpy as np
from PIL import Image, ImageDraw

from warraq.core import imaging, layout as L
from warraq.core.document import BODY, FOOTNOTE, HEADER, MARGIN, Region
from warraq.models.layout_model import Detection
from warraq.settings import Settings


def r(id, x0, y0, x1, y1, kind=BODY):
    return Region(id=id, kind=kind, label="text", bbox=(x0, y0, x1, y1))


def test_xy_cut_reads_columns_right_to_left():
    title = r("t", 100, 50, 900, 100)
    right = r("right", 520, 150, 900, 900)
    left = r("left", 100, 150, 480, 900)
    order = [x.id for x in L.xy_cut_rtl([left, title, right])]
    assert order == ["t", "right", "left"]


def _page_with_text_blocks():
    """Synthetic page: main column, a margin note, a footnote rule and smaller footnote lines."""
    w, h = 1000, 1400
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    for i in range(10):  # body lines, tall strokes
        y = 150 + i * 60
        d.rectangle((150, y, 820, y + 30), fill=0)
    for i in range(3):  # margin note lines
        y = 300 + i * 40
        d.rectangle((860, y, 960, y + 18), fill=0)
    d.rectangle((500, 1080, 820, 1082), fill=0)  # footnote rule (right side, short)
    for i in range(3):  # footnote lines, smaller
        y = 1100 + i * 36
        d.rectangle((150, y, 820, y + 16), fill=0)
    return np.asarray(img)


def test_margins_and_footnotes_are_classified():
    gray = _page_with_text_blocks()
    ctx = L.build_context(gray)
    dets = [
        Detection("text", 0.9, (145, 140, 825, 760), 1, None),
        Detection("text", 0.8, (855, 290, 965, 400), 2, None),
        Detection("text", 0.8, (145, 1095, 825, 1190), 3, None),
    ]
    regions = L.analyze(dets, ctx, Settings(), 0)
    kinds = sorted(x.kind for x in regions)
    assert kinds == sorted([BODY, MARGIN, FOOTNOTE]), [(x.kind, x.bbox) for x in regions]
    margin = [x for x in regions if x.kind == MARGIN][0]
    assert margin.side == "right"


def test_line_segmentation_counts_lines():
    gray = _page_with_text_blocks()[140:760, 145:825]
    bands = imaging.segment_lines(gray)
    assert len(bands) == 10


def test_equals_sign_detection():
    img = Image.new("L", (600, 60), 255)
    d = ImageDraw.Draw(img)
    d.rectangle((560, 22, 590, 26), fill=0)  # "=" at the right edge
    d.rectangle((560, 34, 590, 38), fill=0)
    for x in range(40, 500, 60):
        d.rectangle((x, 12, x + 40, 48), fill=0)  # words
    gray = np.asarray(img)
    assert imaging.detect_equals_sign(gray, "right", 50)
    plain = Image.new("L", (600, 60), 255)
    ImageDraw.Draw(plain).rectangle((40, 12, 590, 48), fill=0)
    assert not imaging.detect_equals_sign(np.asarray(plain), "right", 50)


def test_refine_page_number():
    from warraq.core.document import Page

    page = Page(index=0, source_index=0, source_path="", width=1000, height=1400)
    pn = r("n", 480, 1340, 520, 1370, kind=HEADER)
    pn.text = "١٢"
    L.refine_after_ocr([pn], None, page)
    assert pn.kind == "page_number" and page.printed_number == "١٢"


def test_number_beside_text_is_a_margin_note():
    """A short vertical note mid-page that the detector calls "number" is a marginal note."""
    gray = _page_with_text_blocks()
    ctx = L.build_context(gray)
    dets = [
        Detection("text", 0.9, (145, 140, 825, 760), 1, None),
        Detection("number", 0.6, (855, 290, 965, 400), 2, None),
        Detection("number", 0.7, (470, 1300, 530, 1340), 3, None),  # a real folio number at the foot
    ]
    regions = L.analyze(dets, ctx, Settings(), 0)
    by_box = {tuple(round(v) for v in x.bbox[:2]): x for x in regions}
    note = [x for x in regions if x.bbox[0] > 840]
    assert note and note[0].kind == MARGIN and note[0].side == "right", [(x.kind, x.bbox) for x in regions]
    folio = [x for x in regions if x.bbox[1] > 1250]
    assert all(x.kind != MARGIN for x in folio)


def test_recovered_note_hugs_its_ink():
    """Ink nobody detected becomes a region whose box stays close to the ink."""
    w, h = 1000, 1000
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    for i in range(8):
        d.rectangle((300, 150 + i * 60, 900, 180 + i * 60), fill=0)
    for i in range(4):  # an undetected note on the left: short words, like real text
        for x in range(60, 220, 32):
            d.rectangle((x, 300 + i * 40, x + 24, 318 + i * 40), fill=0)
    ctx = L.build_context(np.asarray(img))
    regions = L.analyze([Detection("text", 0.9, (295, 140, 905, 640), 1, None)], ctx, Settings(), 0)
    notes = [x for x in regions if x.label == "recovered"]
    assert len(notes) == 1 and notes[0].kind == MARGIN
    x0, y0, x1, y1 = notes[0].bbox
    assert 40 <= x0 <= 60 and 220 <= x1 <= 245 and y1 <= 300 + 3 * 40 + 18 + 25


def test_line_pitch_ignores_structure_inside_lines():
    """Rows inside a line alternate dense/sparse (baseline, dots, stems): the pitch is the
    line spacing, not that fine texture, even for blocks of only two or three lines."""
    pitch = 98
    profile = np.zeros(7 * pitch + 40)
    for i in range(7):
        top = 20 + i * pitch
        for k in range(42):
            profile[top + k] = 100 if (k // 5) % 2 == 0 else 30
    for lines in (2, 3, 7):
        assert imaging._estimate_line_pitch(profile[: lines * pitch + 20]) == pitch


def test_whiten_paper_keeps_ink_and_colour():
    rng = np.random.default_rng(0)
    page = np.clip(rng.normal(228, 5, (200, 300, 3)), 0, 255).astype(np.uint8)  # grainy paper
    page[50:60, 20:280] = (25, 25, 25)  # black ink
    page[120:130, 20:280] = (200, 40, 40)  # red rubric
    out = imaging.whiten_paper(page)
    paper = np.concatenate([out[:40].reshape(-1, 3), out[150:].reshape(-1, 3)])
    assert (paper == 255).mean() > 0.99
    assert out[50:60, 20:280].max() < 60
    r, g, b = out[120:130, 20:280].reshape(-1, 3).mean(axis=0)
    assert r > 150 and g < 90 and b < 90


def test_speck_is_not_a_line_but_a_short_last_line_is():
    img = Image.new("L", (900, 260), 255)
    d = ImageDraw.Draw(img)
    for i in range(2):
        for x in range(40, 860, 40):
            d.rectangle((x, 30 + i * 80, x + 30, 70 + i * 80), fill=0)
    d.rectangle((600, 200, 606, 206), fill=0)  # a speck below the text
    assert len(imaging.segment_lines(np.asarray(img))) == 2
    d.rectangle((700, 190, 860, 230), fill=0)  # a short last line beside it
    assert len(imaging.segment_lines(np.asarray(img))) == 3


def test_descender_cut_off_at_the_baseline_stays_with_its_line():
    bands = [imaging.LineBand(42, 75, 21, 118), imaging.LineBand(75, 84, 93, 115)]
    mask = np.zeros((130, 140), dtype=np.uint8)
    mask[45:75, 21:118:3] = 1  # the word
    mask[75:84, 93:115:4] = 1  # the tail of غ
    merged = list(bands)
    imaging._merge_into_neighbour(merged, 1)
    assert merged == [imaging.LineBand(42, 84, 21, 118)]
