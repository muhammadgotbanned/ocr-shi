"""End-to-end API test with a fake OCR engine (no models needed)."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from warraq.core.document import BODY, FOOTNOTE, Document, Line, Page, Region


class FakeEngine:
    def __init__(self):
        self.ocr = type("M", (), {"is_loaded": False, "device": "cpu", "last_used": 0})()
        self.layout = type("M", (), {"is_loaded": False})()

    def unload(self):
        pass

    def run(self, job_dir, source, settings, emit, control=None, page_filter=None, name=None):
        from warraq.core.engine import save_document

        job_dir = Path(job_dir)
        (job_dir / "pages").mkdir(parents=True, exist_ok=True)
        img = Image.open(source).convert("RGB")
        img.save(job_dir / "pages" / "0001.jpg")
        img.save(job_dir / "pages" / "0001_thumb.jpg")
        page = Page(index=0, source_index=0, source_path=str(source), width=img.width, height=img.height, image_path="0001.jpg", thumb_path="0001_thumb.jpg", original_size=img.size, status="done")
        page.regions = [
            Region(id="p1-r1", kind=BODY, label="text", bbox=(20, 20, 380, 80), text="بسم الله الرحمن الرحيم (١)", lines=[Line(bbox=(20, 20, 380, 80), text="بسم الله الرحمن الرحيم (١)")]),
            Region(id="p1-r2", kind=FOOTNOTE, label="text", bbox=(20, 400, 380, 440), text="(١) حاشية أولى.", lines=[Line(bbox=(20, 400, 380, 440), text="(١) حاشية أولى.")]),
        ]
        emit({"type": "job", "total": 1})
        emit({"type": "page_done", "page": page.to_dict(), "done": 1, "total": 1})
        emit({"type": "progress", "done": 1, "total": 1})
        doc = Document(name=name or source.name, source_path=str(source), pages=[page])
        save_document(job_dir, doc)
        return doc


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WARRAQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WARRAQ_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    from warraq.server.api import AppState, create_app
    from warraq.server.events import EventBus
    from warraq.server.jobs import JobManager
    from warraq.settings import SettingsStore

    store = SettingsStore()
    store.update({"general": {"output_dir": str(tmp_path / "out")}, "output": {"formats": ["txt", "pdf", "md", "json", "html", "docx"]}})
    bus = EventBus()
    manager = JobManager(store, bus, engine=FakeEngine())
    state = AppState(store=store, manager=manager)
    state.bus = bus
    state.hardware = {"devices": [{"kind": "cpu", "name": "CPU"}], "cpu_threads": 4}
    return TestClient(create_app(state)), tmp_path


def wait_done(c, job_id, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = c.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_full_job_lifecycle(client):
    c, tmp = client
    img = tmp / "page.png"
    Image.new("RGB", (400, 500), "white").save(img)
    assert c.get("/api/health").json()["ok"]
    s = c.get("/api/settings").json()
    assert any(sec["key"] == "footnotes" for sec in s["schema"])
    r = c.patch("/api/settings", json={"footnotes": {"label_format": "[{n}]"}})
    assert r.status_code == 200
    jobs = c.post("/api/jobs/from-paths", json={"paths": [str(img)]}).json()
    job = wait_done(c, jobs[0]["id"])
    assert job["status"] == "done", job
    assert set(job["outputs"]) >= {"txt", "pdf", "md", "json", "html", "docx"}
    txt = Path(job["outputs"]["txt"]).read_text(encoding="utf-8")
    assert "------footnotes-------\n[1] حاشية أولى." in txt
    assert Path(job["outputs"]["txt"]).name == "page.txt"
    # edit a region, preview reflects it
    page = c.patch(f"/api/jobs/{job['id']}/regions/p1-r1", json={"text": "الحمد لله"}).json()
    assert page["regions"][0]["text"] == "الحمد لله"
    assert "الحمد لله" in c.get(f"/api/jobs/{job['id']}/preview").text
    asm = c.get(f"/api/jobs/{job['id']}/assembled").json()
    assert asm["pages"][0]["footnotes"][0]["label"] == "[1]"
    # re-export
    c.post(f"/api/jobs/{job['id']}/export", json={"formats": ["txt"]})
    job = wait_done(c, job["id"])
    assert "الحمد لله" in Path(job["outputs"]["txt"]).read_text(encoding="utf-8")
    # downloads and page images
    assert c.get(f"/api/jobs/{job['id']}/files/txt").status_code == 200
    assert c.get(f"/api/jobs/{job['id']}/pages/0001.jpg").status_code == 200
    assert c.get(f"/api/jobs/{job['id']}/pages/..%2F..%2Fjob.json").status_code == 404
    # published copy in the user's output folder
    assert (tmp / "out" / "page" / "page.txt").exists()
    # delete
    assert c.delete(f"/api/jobs/{job['id']}").json()["ok"]
    assert c.get("/api/jobs").json() == []


def test_upload_rejects_unknown_types(client):
    c, tmp = client
    r = c.post("/api/jobs", files={"files": ("notes.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_index_served(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "Warraq" in r.text
    assert c.get("/static/js/main.js").status_code == 200
