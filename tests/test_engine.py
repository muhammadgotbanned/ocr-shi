"""The engine end to end with stand-in models: pages are prepared one ahead while the
current one is read, in order, and a job can be cancelled and resumed."""

import threading
import time

import numpy as np
import pytest
from PIL import Image, ImageDraw

from warraq.core import engine as E
from warraq.core.engine import Cancelled, Engine, JobControl, load_document
from warraq.models.layout_model import Detection
from warraq.models.ocr_model import Recognition
from warraq.settings import Settings


def _page_image(seed: int) -> Image.Image:
    img = Image.new("RGB", (900, 1200), "white")
    d = ImageDraw.Draw(img)
    for i in range(6 + seed % 3):
        y = 150 + i * 70
        for x in range(120, 760, 46):
            d.rectangle((x, y, x + 34, y + 30), fill=(0, 0, 0))
    return img


class FakeOCR:
    device = "cpu"

    def __init__(self, delay=0.05):
        self.lock = threading.RLock()
        self.delay = delay
        self.calls: list[float] = []
        self.batch_limit = None

    def load(self, settings, progress=None):
        pass

    def recognize(self, images, budgets, settings, cancel=None):
        with self.lock:
            self.calls.append(time.time())
            time.sleep(self.delay)
            return [Recognition(text="سطر مقروء", confidence=0.95, tokens=4, hit_limit=False, looped=False, line_stats=[(0.9, -0.05)]) for _ in images]


class FakeLayout:
    device = "cuda"  # pretend: runs beside the OCR model

    def __init__(self):
        self.calls: list[float] = []

    def load(self, settings, progress=None):
        pass

    def detect(self, image, threshold):
        self.calls.append(time.time())
        return [Detection("text", 0.9, (100, 130, 780, 700), 1, None)]


def _engine(delay=0.05):
    eng = Engine.__new__(Engine)
    eng.ocr, eng.layout, eng.lock, eng._unified_gb = FakeOCR(delay), FakeLayout(), threading.RLock(), None
    return eng


def _source(tmp_path, n=4):
    path = tmp_path / "book.tif"
    frames = [_page_image(i) for i in range(n)]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    return path


def _settings():
    st = Settings()
    st.input.deskew = False
    st.input.split_spreads = "off"
    return st


def test_pages_are_read_in_order_with_the_next_one_prepared_meanwhile(tmp_path):
    eng = _engine(delay=0.15)
    events = []
    doc = eng.run(tmp_path / "job", _source(tmp_path), _settings(), events.append)
    assert [p.source_index for p in doc.pages] == [0, 1, 2, 3]
    assert all(p.status == "done" and p.regions and p.regions[0].text for p in doc.pages)
    assert [r.id for r in doc.pages[2].regions] == ["p3-r1"]
    kinds = [e["type"] for e in events if e["type"] in ("page_start", "page_image", "layout", "page_done")]
    assert kinds == ["page_start", "page_image", "layout", "page_done"] * 4
    # page 2's layout ran while page 1 was still being read
    first_read_end = eng.ocr.calls[len(eng.ocr.calls) // 4]
    assert eng.layout.calls[1] < first_read_end


def test_cancel_stops_and_resume_skips_finished_pages(tmp_path):
    eng = _engine()
    control = JobControl()
    seen = []

    def emit(e):
        if e["type"] == "page_done":
            seen.append(e["page"]["source_index"])
            if len(seen) == 2:
                control.cancel.set()

    with pytest.raises(Cancelled):
        eng.run(tmp_path / "job", _source(tmp_path), _settings(), emit, control)
    assert [p.source_index for p in load_document(tmp_path / "job").pages if p.status == "done"] == [0, 1]
    assert not [t for t in threading.enumerate() if t.name == "warraq-prefetch"]
    layout_calls = len(eng.layout.calls)
    doc = eng.run(tmp_path / "job", _source(tmp_path), _settings(), lambda e: None)
    assert [p.source_index for p in doc.pages] == [0, 1, 2, 3]
    assert len(eng.layout.calls) - layout_calls == 2  # only the two unfinished pages


def test_broken_page_is_reported_and_the_rest_continue(tmp_path, monkeypatch):
    eng = _engine()
    real = E.L.analyze

    def flaky(dets, ctx, settings, page_index):
        if ctx.width == 900 and flaky.n == 1:
            flaky.n += 1
            raise RuntimeError("bad page")
        flaky.n += 1
        return real(dets, ctx, settings, page_index)

    flaky.n = 0
    monkeypatch.setattr(E.L, "analyze", flaky)
    events = []
    doc = eng.run(tmp_path / "job", _source(tmp_path, 3), _settings(), events.append)
    assert [p.status for p in doc.pages] == ["done", "error", "done"]
    assert any(e["type"] == "log" and "bad page" in e["message"] for e in events)


def test_rotated_note_is_read_one_way_up_when_the_other_is_garbage():
    from warraq.core import recognize as R
    from warraq.core.document import MARGIN, Region
    from warraq.core.imaging import LineBand

    rec = R.Recognizer(None, Settings())
    region = Region(id="m", kind=MARGIN, label="aside_text", bbox=(0, 0, 100, 300))

    def job(text, conf, n=3):
        chunks = []
        for k in range(n):
            c = R.Chunk(region, k, [LineBand(0, 30, 0, 100 + k)], (0, 0, 110, 40))
            c.image = Image.new("RGB", (110, 40), "white")
            chunks.append(c)
        j = R.RegionJob(region, None, (0, 0), (100, 300), -90, 30, [], chunks=chunks)
        j.text, j.conf = text, conf
        return j

    right, wrong, close = job("بلغ مقابلة", 0.95), job("黑白三色", 0.2), job("بلغ", 0.9)
    right.alt = wrong
    read = []

    def fake_run(chunks, page_index):
        for c in chunks:
            j = next(j for j in (right, wrong, close) if any(c is x for x in j.chunks))
            c.result = Recognition(text=j.text, confidence=j.conf, tokens=4, hit_limit=False, looped=False, line_stats=[(j.conf, float(np.log(j.conf)))])
            c.text, c.ok = j.text, True
            read.append(j.text)

    rec._run_chunks = fake_run
    # one line of each orientation is read; the garbage one is not read any further
    assert rec._settle_orientation([right, wrong], 0) == [right]
    assert read == ["بلغ مقابلة", "黑白三色"]
    # a close call keeps both, to be read in full
    for c in right.chunks:
        c.result = None
    right.alt = close
    assert rec._settle_orientation([right, close], 0) == [right, close]
