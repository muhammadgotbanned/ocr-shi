"""Re-reading part of a page: planning against the page's regions and merging the result."""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from warraq.core import area as A
from warraq.core import layout as L
from warraq.core.document import BODY, FOOTNOTE, MARGIN, Document, Line, Page, Region
from warraq.core.engine import Engine, load_document, save_document
from warraq.models.ocr_model import Recognition
from warraq.settings import Settings

W, H = 1000, 1400
BODY_LINES = [(150 + i * 60, 180 + i * 60) for i in range(10)]


def _page_image() -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    for top, bottom in BODY_LINES:
        for x in range(150, 820, 34):  # words, so lines look like text
            d.rectangle((x, top, x + 26, bottom), fill=(0, 0, 0))
    for i in range(3):  # margin note
        d.rectangle((860, 300 + i * 40, 960, 318 + i * 40), fill=(0, 0, 0))
    d.rectangle((500, 1080, 820, 1082), fill=(0, 0, 0))  # footnote rule
    for i in range(2):  # footnote lines, smaller, that no region covers
        for x in range(150, 820, 30):
            d.rectangle((x, 1100 + i * 36, x + 22, 1116 + i * 36), fill=(0, 0, 0))
    return img


def _regions() -> list[Region]:
    body = Region(id="p1-r1", kind=BODY, label="text", bbox=(145, 145, 825, 725))
    body.text = "\n".join(f"سطر {i + 1}" for i in range(10))
    body.lines = [Line(bbox=(150, t, 820, b), text=f"سطر {i + 1}") for i, (t, b) in enumerate(BODY_LINES)]
    margin = Region(id="p1-r2", kind=MARGIN, label="aside_text", bbox=(855, 295, 965, 400), side="right", text="حاشية قديمة")
    margin.lines = [Line(bbox=(860, 300, 960, 398), text="حاشية قديمة")]
    return [body, margin]


class FakeOCR:
    device = "cpu"

    def __init__(self):
        self.calls = 0

    def load(self, settings, progress=None):
        pass

    def recognize(self, images, budgets, settings, cancel=None):
        out = []
        for _ in images:
            self.calls += 1
            out.append(Recognition(text=f"جديد {self.calls}", confidence=0.95, tokens=4, hit_limit=False, looped=False, line_stats=[(0.9, -0.05)]))
        return out


def _job(tmp_path: Path) -> Path:
    (tmp_path / "pages").mkdir()
    _page_image().save(tmp_path / "pages" / "0001.png")
    page = Page(index=0, source_index=0, source_path="x", width=W, height=H, image_path="0001.png", status="done")
    page.regions = _regions()
    save_document(tmp_path, Document(name="t", source_path="x", pages=[page]))
    return tmp_path


def _reread(tmp_path, **area):
    engine = Engine.__new__(Engine)
    import threading

    engine.ocr, engine.lock = FakeOCR(), threading.RLock()
    events = []
    page = engine.reread_area(tmp_path, Settings(), events.append, page=0, **area)
    assert events[-1]["type"] == "page_done"
    return page, load_document(tmp_path).pages[0]


def test_box_over_two_lines_replaces_just_those_lines(tmp_path):
    _job(tmp_path)
    # a box over lines 4 and 5 (cutting through their ends is fine: whole lines are read)
    page, saved = _reread(tmp_path, bbox=(400, 330, 700, 420))
    body = next(r for r in saved.regions if r.id == "p1-r1")
    lines = body.text.split("\n")
    assert lines[:3] == ["سطر 1", "سطر 2", "سطر 3"] and lines[5:] == [f"سطر {i}" for i in range(6, 11)]
    assert all(l.startswith("جديد") for l in lines[3:5]) and len(lines) == 10
    assert len(body.lines) == 10 and len(saved.regions) == 2


def test_box_over_lines_as_footnote_moves_them_out(tmp_path):
    _job(tmp_path)
    page, saved = _reread(tmp_path, bbox=(140, 625, 830, 730), kind="footnote")
    body = next(r for r in saved.regions if r.id == "p1-r1")
    note = next(r for r in saved.regions if r.kind == FOOTNOTE)
    assert body.text.split("\n") == [f"سطر {i}" for i in range(1, 9)]
    assert body.bbox[3] <= 600 and note.text.startswith("جديد")
    assert note.bbox[1] > 600


def test_box_around_a_region_replaces_it(tmp_path):
    _job(tmp_path)
    page, saved = _reread(tmp_path, bbox=(845, 280, 975, 420))
    margins = [r for r in saved.regions if r.kind == MARGIN]
    assert len(margins) == 1 and margins[0].id != "p1-r2" and margins[0].text.startswith("جديد")
    assert margins[0].side == "right"


def test_box_over_unread_ink_below_the_rule_is_a_footnote(tmp_path):
    _job(tmp_path)
    page, saved = _reread(tmp_path, bbox=(140, 1090, 830, 1160))
    new = [r for r in saved.regions if r.id.startswith("p1-a")]
    assert len(new) == 1 and new[0].kind == FOOTNOTE and new[0].text
    assert "area" in new[0].notes


def test_region_reread_in_place(tmp_path):
    _job(tmp_path)
    page, saved = _reread(tmp_path, region_id="p1-r2")
    margin = next(r for r in saved.regions if r.id == "p1-r2")
    assert margin.text.startswith("جديد") and margin.kind == MARGIN


def test_snap_takes_letters_cut_by_the_edge_whole():
    img = _page_image()
    ctx = L.build_context(np.asarray(img.convert("L")))
    # box cuts line 1 (150-180) in the middle; most of that line is outside -> left out,
    # line 2 (210-240) is inside except its top few pixels -> taken whole
    box = A.snap_to_ink(ctx, (140, 170, 830, 245))
    assert 205 <= box[1] <= 212 and 240 <= box[3] <= 245


def test_clip_whitens_ink_mostly_outside():
    img = Image.new("RGB", (200, 100), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((20, 10, 60, 40), fill=(0, 0, 0))  # inside
    d.rectangle((100, 60, 190, 95), fill=(0, 0, 0))  # mostly outside the box
    out = np.asarray(A.clip_to_box(img, (10, 5, 120, 70)).convert("L"))
    assert out[20, 40] == 0 and out[65, 110] == 255 and out[90, 150] == 255


def test_splice_keeps_blank_lines():
    assert A._splice("a\nb\n\nc\nd", 1, 3, ["X"]) == "a\nX\n\nd"
    assert A._splice("a\nb", 1, 2, []) == "a"
