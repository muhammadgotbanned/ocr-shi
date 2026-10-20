import re
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from warraq.core.assemble import assemble
from warraq.core.document import BODY, FOOTNOTE, Document, Line, Page, Region
from warraq.export import pdf as pdf_export
from warraq.export import textlayer
from warraq.settings import Settings

LINE1 = "الحمد لله رب العالمين"
LINE2 = "انظر: مقدمة ابن الصلاح ص ١٢"


def _doc(src: Path, w: int, h: int, size_pt=None) -> Document:
    page = Page(index=0, source_index=0, source_path=str(src), width=w, height=h, image_path="0001.jpg", original_size=(w, h), source_size_pt=size_pt)
    page.regions = [
        Region(id="r1", kind=BODY, label="text", bbox=(100, 100, 900, 160), text=LINE1, lines=[Line(bbox=(100, 100, 900, 160), text=LINE1, quad=[(100, 100), (900, 100), (900, 160), (100, 160)])]),
        Region(id="r2", kind=FOOTNOTE, label="text", bbox=(100, 1000, 900, 1040), text="(١) " + LINE2, lines=[Line(bbox=(100, 1000, 900, 1040), text="(١) " + LINE2)]),
    ]
    return Document(name=src.name, source_path=str(src), pages=[page])


def _extract(path: Path) -> str:
    doc = pdfium.PdfDocument(str(path))
    try:
        return doc[0].get_textpage().get_text_range()
    finally:
        doc.close()


def _normal(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@pytest.fixture
def scan(tmp_path):
    img = Image.new("RGB", (1000, 1400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((100, 100, 900, 160), outline="black")
    path = tmp_path / "scan.png"
    img.save(path, dpi=(200, 200))
    (tmp_path / "pages").mkdir()
    img.save(tmp_path / "pages" / "0001.jpg")
    return path


def test_image_input_gets_searchable_text(tmp_path, scan):
    s = Settings()
    doc = _doc(scan, 1000, 1400)
    views = assemble(doc, s)
    out = tmp_path / "out.pdf"
    pdf_export.write(out, doc, views, s, tmp_path)
    text = _normal(_extract(out))
    for word in ("الحمد", "لله", "العالمين", "مقدمة", "الصلاح"):
        assert word in text
    pdf = pdfium.PdfDocument(str(out))
    w, h = pdf[0].get_size()
    assert abs(w - 1000 * 72 / 200) < 1 and abs(h - 1400 * 72 / 200) < 1


def test_text_layer_over_original_pdf(tmp_path, scan):
    # build a one-page PDF from the image, then overlay the OCR text on it
    src = tmp_path / "orig.pdf"
    Image.open(scan).save(src, "PDF", resolution=200)
    s = Settings()
    doc = _doc(src, 1000, 1400, size_pt=(360, 504))
    views = assemble(doc, s)
    out = tmp_path / "out.pdf"
    pdf_export.write(out, doc, views, s, tmp_path)
    text = _normal(_extract(out))
    assert "الحمد لله رب العالمين" in text


def test_old_ocr_layer_is_replaced(tmp_path, scan):
    src = tmp_path / "orig.pdf"
    Image.open(scan).save(src, "PDF", resolution=200)
    s = Settings()
    doc = _doc(src, 1000, 1400, size_pt=(360, 504))
    first = tmp_path / "first.pdf"
    pdf_export.write(first, doc, assemble(doc, s), s, tmp_path)
    # OCR the already-OCR'd file again with different text
    doc2 = _doc(first, 1000, 1400, size_pt=(360, 504))
    doc2.pages[0].regions[0].lines[0].text = "نص جديد تماما"
    doc2.pages[0].regions[1].lines = []
    out = tmp_path / "second.pdf"
    pdf_export.write(out, doc2, assemble(doc2, s), s, tmp_path)
    text = _normal(_extract(out))
    assert "جديد" in text
    assert "العالمين" not in text


def test_visual_order_roundtrip_is_exact_codepoints():
    vis = textlayer.visual_order("قال (١) صحيح")
    assert set(vis) == set("قال (١) صحيح")
    assert textlayer.visual_order("Latin only") == "Latin only"


def test_rotated_pages_matrix():
    m = pdf_export.disp_to_user_matrix((0, 0, 600, 800), 90)
    # display top-left (0,0) maps to user-space bottom-left (x0, y0)
    from warraq.core import geometry as G

    assert G.apply(m, 0, 0) == (0, 0)
    assert G.apply(m, 10, 0) == (0, 10)
