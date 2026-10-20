"""The benchmark runner works end to end (stand-in models: this checks the plumbing, not
the reading)."""

import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

BENCH = Path(__file__).resolve().parents[1] / "bench"
sys.path.insert(0, str(BENCH))


def test_runner_scores_lines_and_runs_pages(tmp_path, monkeypatch):
    import run as R

    from warraq.models import layout_model, ocr_model
    from warraq.models.layout_model import Detection
    from warraq.models.ocr_model import Recognition

    data = tmp_path / "data"
    lines, pages = [], []
    for k, gold in enumerate(["الحمد لله رب العالمين", "وقد تجد الرجل يطلب الآثار"]):
        img = Image.new("L", (900, 70), 255)
        d = ImageDraw.Draw(img)
        for x in range(40, 860, 46):
            d.rectangle((x, 20, x + 34, 50), fill=0)
        (data / "ara").mkdir(parents=True, exist_ok=True)
        img.save(data / f"ara/{k}.png")
        (data / f"ara/{k}.gt.txt").write_text(gold, encoding="utf-8")
        lines.append({"book": f"book {k}", "image": f"ara/{k}.png", "text": f"ara/{k}.gt.txt"})
    page = Image.new("RGB", (900, 1200), "white")
    d = ImageDraw.Draw(page)
    for i in range(8):
        for x in range(120, 760, 46):
            d.rectangle((x, 150 + i * 70, x + 34, 180 + i * 70), fill=(0, 0, 0))
    page.save(data / "ara/page.png")
    pages.append({"book": "a page", "image": "ara/page.png"})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"lines": lines, "pages": pages}), encoding="utf-8")
    monkeypatch.setattr(R, "DATA", data)
    monkeypatch.setattr(R, "HERE", tmp_path)

    def recognize(self, images, budgets, settings, cancel=None):
        return [Recognition(text="الحمد لله رب العالمين", confidence=0.9, tokens=5, hit_limit=False, looped=False, line_stats=[(0.9, -0.1)]) for _ in images]

    monkeypatch.setattr(ocr_model.OCRModel, "load", lambda self, settings, progress=None: None)
    monkeypatch.setattr(ocr_model.OCRModel, "recognize", recognize)
    monkeypatch.setattr(layout_model.LayoutModel, "load", lambda self, settings, progress=None: None)
    monkeypatch.setattr(layout_model.LayoutModel, "detect", lambda self, image, threshold=0.3: [Detection("text", 0.9, (100, 130, 780, 720), 1, None)])
    assert R.main(["--out", str(tmp_path / "out"), "--device", "cpu"]) == 0
    report = json.loads((tmp_path / "out/report.json").read_text(encoding="utf-8"))
    assert report["books"]["book 0"]["char_accuracy"] == pytest.approx(1.0)
    assert report["books"]["book 1"]["char_accuracy"] < 0.6
    assert report["pages"][0]["regions"] == {"body": 1}
    assert (tmp_path / "out/pages/page.txt").read_text(encoding="utf-8").count("الحمد لله") >= 1
    assert "Character accuracy" in (tmp_path / "out/report.md").read_text(encoding="utf-8")


PAGE_XML = """<?xml version="1.0" ?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
  <Page imageFilename="ms.jpg" imageHeight="400" imageWidth="300">
    <TextRegion id="r1" custom="structure {type:MainZone;}">
      <Coords points="20,20 280,20 280,300 20,300"/>
      <TextLine id="l1"><Coords points="20,40 280,40 280,80 20,80"/><TextEquiv><Unicode>بسم الله الرحمن الرحيم</Unicode></TextEquiv></TextLine>
    </TextRegion>
    <TextRegion id="r2" custom="structure {type:MarginTextZone:catchword;}">
      <Coords points="20,330 80,330 80,360 20,360"/>
      <TextLine id="l2"><Coords points="20,330 80,330 80,360 20,360"/><TextEquiv><Unicode>فقال</Unicode></TextEquiv></TextLine>
    </TextRegion>
  </Page>
</PcGts>"""


def test_rasam_pages_are_cut_into_lines_and_zones():
    import fetch_rasam as F
    from PIL import Image

    page = F.parse_page(PAGE_XML.encode())
    assert (page["width"], page["height"]) == (300, 400)
    assert [r["zone"] for r in page["regions"]] == ["main", "catchword"]
    assert page["regions"][1]["lines"][0]["text"] == "فقال"
    img = Image.new("RGB", (300, 400), (0, 0, 0))
    line = F.cut_line(img, [(20, 40), (280, 40), (150, 80)])  # a triangle on a black page
    assert line.size == (272, 52) and line.getpixel((260, 45)) == (255, 255, 255) and line.getpixel((140, 20)) == (0, 0, 0)


def test_layout_agreement_maps_zones_through_the_page_transform():
    import run as R

    from warraq.core.document import BODY, FOOTER, Page, Region

    page = Page(index=0, source_index=0, source_path="", width=280, height=380, transform=[1, 0, 0, 1, 10, 10])  # cropped 10 px
    page.regions = [Region("a", BODY, "text", (5, 5, 275, 295)), Region("b", FOOTER, "catchword", (8, 318, 72, 352))]
    zones = [{"zone": "main", "points": [(20, 20), (280, 20), (280, 300), (20, 300)]}, {"zone": "catchword", "points": [(20, 330), (80, 330), (80, 360), (20, 360)]}, {"zone": "margin", "points": [(285, 50), (299, 50), (299, 90), (285, 90)]}]
    rows = R.layout_agreement(zones, page)
    assert [(r["zone"], r["got"], r["ok"]) for r in rows] == [("main", "body", True), ("catchword", "catchword", True), ("margin", "missed", False)]
