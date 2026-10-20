"""Job queue: one worker thread owns the GPU and processes jobs in order."""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from ..core.engine import Cancelled, Engine, JobControl, copy_outputs, export_document, load_document, save_document
from ..settings import Settings, SettingsStore, apply_preset, update_settings
from .events import EventBus

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    name: str
    source: str  # path of the input copy inside the job folder
    original_path: str
    status: str = "queued"  # queued running paused done error cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    total_pages: int = 0
    done_pages: int = 0
    message: str = ""
    error: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    published: dict[str, str] = field(default_factory=dict)  # copies in the user's output folder
    overrides: dict[str, Any] = field(default_factory=dict)
    preset: str | None = None
    translate: bool = False
    translation_status: str = ""
    kind: str = "ocr"  # ocr | reocr | translate | export
    pages_filter: list[int] | None = None

    @property
    def dir(self) -> Path:
        return paths.jobs_dir() / self.id

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed"] = (self.finished or time.time()) - self.started if self.started else 0
        return d


class JobManager:
    def __init__(self, store: SettingsStore, bus: EventBus, engine: Engine | None = None):
        self.store = store
        self.bus = bus
        self.engine = engine or Engine()
        self.jobs: dict[str, Job] = {}
        self.controls: dict[str, JobControl] = {}
        self.queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.lock = threading.RLock()
        self._load_history()
        self.worker = threading.Thread(target=self._work, name="warraq-worker", daemon=True)
        self.worker.start()
        self.janitor = threading.Thread(target=self._idle_unloader, name="warraq-idle", daemon=True)
        self.janitor.start()
        self.model_status: dict[str, Any] = {"stage": "idle"}

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #
    def _load_history(self) -> None:
        for d in sorted(paths.jobs_dir().glob("*/job.json")):
            try:
                data = json.loads(d.read_text(encoding="utf-8"))
                known = {f for f in Job.__dataclass_fields__}
                job = Job(**{k: v for k, v in data.items() if k in known})
                if job.status in ("running", "queued", "paused"):
                    job.status = "interrupted"
                self.jobs[job.id] = job
            except Exception:  # pragma: no cover - corrupt entry
                log.warning("skipping unreadable job %s", d)

    def _save(self, job: Job) -> None:
        job.dir.mkdir(parents=True, exist_ok=True)
        (job.dir / "job.json").write_text(json.dumps(asdict(job), ensure_ascii=False, indent=1), encoding="utf-8")

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
            return [j.to_dict() for j in jobs]

    def get(self, job_id: str) -> Job:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    # ------------------------------------------------------------------ #
    # Creating jobs
    # ------------------------------------------------------------------ #
    def create(self, src: Path, name: str | None = None, *, move: bool = False, overrides: dict | None = None, preset: str | None = None, translate: bool | None = None) -> Job:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        name = name or src.name
        job = Job(id=job_id, name=name, source="", original_path=str(src), overrides=overrides or {}, preset=preset)
        job.dir.mkdir(parents=True, exist_ok=True)
        dest = job.dir / ("source" + src.suffix.lower())
        if move:
            shutil.move(str(src), dest)
        else:
            try:
                os.link(src, dest)
            except Exception:
                shutil.copy2(src, dest)
        job.source = str(dest)
        settings = self.settings_for(job)
        job.translate = bool(translate if translate is not None else settings.translation.enabled)
        with self.lock:
            self.jobs[job.id] = job
        self._save(job)
        self.enqueue(job, "ocr")
        return job

    def settings_for(self, job: Job) -> Settings:
        settings = self.store.snapshot()
        if job.preset:
            try:
                settings = apply_preset(settings, job.preset)
            except KeyError:
                pass
        if job.overrides:
            settings = update_settings(settings, job.overrides)
        return settings

    def enqueue(self, job: Job, kind: str, pages: list[int] | None = None) -> None:
        with self.lock:
            job.kind = kind
            job.pages_filter = pages
            job.status = "queued"
            job.error = ""
            job.message = "Waiting…"
            self.controls[job.id] = JobControl()
            self._save(job)
        self._emit(job, {"type": "job", "status": "queued"})
        self.queue.put((job.id, kind))

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    def cancel(self, job_id: str) -> None:
        ctl = self.controls.get(job_id)
        if ctl:
            ctl.cancel.set()
        job = self.get(job_id)
        if job.status == "queued":
            job.status = "cancelled"
            self._save(job)
            self._emit(job, {"type": "job", "status": "cancelled"})

    def pause(self, job_id: str) -> None:
        ctl = self.controls.get(job_id)
        if ctl:
            ctl.pause.set()
            job = self.get(job_id)
            job.status = "paused"
            self._emit(job, {"type": "job", "status": "paused"})

    def resume(self, job_id: str) -> None:
        job = self.get(job_id)
        ctl = self.controls.get(job_id)
        if job.status in ("interrupted", "error", "cancelled"):
            self.enqueue(job, "ocr")
            return
        if ctl:
            ctl.pause.clear()
            job.status = "running"
            self._emit(job, {"type": "job", "status": "running"})

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)
        self.cancel(job_id)
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(job.dir, ignore_errors=True)
        self.bus.publish({"type": "job_deleted", "job_id": job_id})

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    def _emit(self, job: Job, event: dict) -> None:
        event = {**event, "job_id": job.id}
        t = event.get("type")
        if t == "job":
            event.setdefault("job", job.to_dict())
        if t == "model":
            self.model_status = {k: v for k, v in event.items() if k != "job_id"}
        self.bus.publish(event)

    def _work(self) -> None:
        while True:
            job_id, kind = self.queue.get()
            try:
                job = self.get(job_id)
            except KeyError:
                continue
            ctl = self.controls.setdefault(job_id, JobControl())
            if ctl.cancel.is_set() or job.status == "cancelled":
                continue
            try:
                self._run(job, kind, ctl)
            except Cancelled:
                job.status = "cancelled"
                job.message = "Cancelled"
                job.finished = time.time()
                self._save(job)
                self._emit(job, {"type": "job", "status": "cancelled"})
            except Exception as exc:  # pragma: no cover - surfaced to the UI
                log.exception("job %s failed", job_id)
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished = time.time()
                self._save(job)
                self._emit(job, {"type": "job", "status": "error", "error": job.error})

    def _run(self, job: Job, kind: str, ctl: JobControl) -> None:
        settings = self.settings_for(job)
        job.status = "running"
        job.started = job.started if kind != "ocr" and job.started else time.time()
        job.finished = None
        job.message = "Starting…"
        self._save(job)
        self._emit(job, {"type": "job", "status": "running"})

        def emit(event: dict) -> None:
            t = event.get("type")
            if t == "job" and "total" in event:
                job.total_pages = event["total"]
            if t == "progress":
                job.done_pages = event.get("done", job.done_pages)
                job.total_pages = event.get("total", job.total_pages)
            if t == "model" and event.get("message"):
                job.message = event["message"]
            if t == "page_start":
                job.message = f"Reading page {event.get('source_index', 0) + 1}"
            if t == "status":
                job.message = event.get("message", job.message)
            if t == "translate" and event.get("total"):
                job.message = f"Translating {event.get('done', 0)}/{event['total']}"
                self._emit(job, {"type": "status", "message": job.message})
            if t == "job":
                event = {**event, "status": "running"}
            self._emit(job, event)

        doc = None
        if kind in ("ocr", "reocr"):
            doc = self.engine.run(job.dir, Path(job.source), settings, emit, ctl, page_filter=job.pages_filter, name=job.name)
        else:
            doc = load_document(job.dir)
        if doc is None:
            raise RuntimeError("No OCR results found for this job.")
        translations = None
        if kind == "translate" or (kind == "ocr" and job.translate):
            translations = self._translate(job, doc, settings, emit, ctl)
        elif (job.dir / "translation.json").exists():
            translations = _load_translations(job.dir)
        job.message = "Writing files…"
        self._emit(job, {"type": "job", "status": "running"})
        results = export_document(doc, job.dir, settings, translations=translations, emit=emit)
        job.outputs = results
        dest_root = Path(settings.general.output_dir).expanduser() if settings.general.output_dir else paths.default_output_dir()
        try:
            job.published = copy_outputs(results, dest_root / Path(job.name).stem)
        except Exception as exc:  # pragma: no cover - unwritable folder
            log.warning("could not copy outputs: %s", exc)
            job.published = {}
        errors = [p for p in doc.pages if p.status == "error"]
        job.status = "done"
        job.finished = time.time()
        job.message = f"Done · {len(doc.pages)} page(s)" + (f" · {len(errors)} with errors" if errors else "")
        self._save(job)
        self._emit(job, {"type": "job", "status": "done", "outputs": job.outputs, "published": job.published})
        if settings.general.open_folder_when_done and job.published:
            try:
                paths.open_in_file_manager(Path(next(iter(job.published.values()))))
            except Exception:  # pragma: no cover
                pass
        if not settings.general.keep_models_loaded and self.queue.empty():
            self.engine.unload()

    def _translate(self, job: Job, doc, settings, emit, ctl: JobControl):
        from ..core.assemble import assemble
        from ..translate.engine import translate_views

        job.translation_status = "running"
        views = assemble(doc, settings)
        if settings.translation.engine in ("local", "seq2seq") and self.engine.ocr.is_loaded:
            from ..models import hardware

            mem = hardware.device_memory_gb(self.engine.ocr.device)
            if mem is not None and mem < (12 if self.engine.ocr.device.startswith("cuda") else 24):
                self.engine.unload()  # make room for the translation model
        translations = translate_views(views, settings, emit=emit, cancel=ctl.cancelled)
        _save_translations(job.dir, translations)
        job.translation_status = "done"
        return translations

    def _idle_unloader(self) -> None:
        while True:
            time.sleep(30)
            try:
                minutes = self.store.current.general.unload_after_minutes
                if not minutes or not self.queue.empty():
                    continue
                busy = any(j.status in ("running", "paused") for j in self.jobs.values())
                if busy or not self.engine.ocr.is_loaded:
                    continue
                if time.time() - self.engine.ocr.last_used > minutes * 60:
                    log.info("unloading idle models")
                    self.engine.unload()
                    self.bus.publish({"type": "model", "stage": "unloaded"})
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ #
    # Review edits
    # ------------------------------------------------------------------ #
    def update_region(self, job_id: str, region_id: str, text: str | None = None, kind: str | None = None) -> dict:
        job = self.get(job_id)
        doc = load_document(job.dir)
        if doc is None:
            raise KeyError("document")
        for page in doc.pages:
            for r in page.regions:
                if r.id == region_id:
                    if text is not None:
                        r.text = text
                        r.edited = True
                        # a corrected word is no longer in doubt; the others still await review
                        r.uncertain = [w for w in r.uncertain if w in text]
                        _relayout_lines(r)
                    if kind is not None:
                        r.kind = kind
                    save_document(job.dir, doc)
                    return page.to_dict()
        raise KeyError(region_id)


def _relayout_lines(region) -> None:
    """After a manual edit keep line boxes in sync (re-split the text over existing boxes)."""
    from ..core.document import Line

    lines = [l for l in region.text.split("\n") if l.strip()]
    if not region.lines:
        x0, y0, x1, y1 = region.bbox
        step = (y1 - y0) / max(1, len(lines))
        region.lines = [Line(bbox=(x0, y0 + i * step, x1, y0 + (i + 1) * step), text=t) for i, t in enumerate(lines)]
        return
    boxes = [l for l in region.lines]
    if len(boxes) == len(lines):
        for b, t in zip(boxes, lines):
            b.text = t
        return
    x0 = min(b.bbox[0] for b in boxes)
    x1 = max(b.bbox[2] for b in boxes)
    y0 = min(b.bbox[1] for b in boxes)
    y1 = max(b.bbox[3] for b in boxes)
    step = (y1 - y0) / max(1, len(lines))
    angle = boxes[0].angle
    region.lines = [Line(bbox=(x0, y0 + i * step, x1, y0 + (i + 1) * step), text=t, angle=angle) for i, t in enumerate(lines)]


def _translation_key(k) -> str:
    return json.dumps(list(k)) if isinstance(k, tuple) else k


def _save_translations(job_dir: Path, translations: list[dict]) -> None:
    data = [{_translation_key(k): v for k, v in page.items()} for page in translations]
    (job_dir / "translation.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _load_translations(job_dir: Path) -> list[dict] | None:
    try:
        data = json.loads((job_dir / "translation.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    out = []
    for page in data:
        d = {}
        for k, v in page.items():
            d[tuple(json.loads(k)) if k.startswith("[") else k] = v
        out.append(d)
    return out
