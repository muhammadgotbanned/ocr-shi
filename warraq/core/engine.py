"""The OCR engine: runs a document through loading, layout, recognition,
assembly and export, emitting progress events along the way."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from .. import paths
from ..models.layout_model import LayoutModel
from ..models.ocr_model import OCRModel
from . import layout as L
from . import loader
from .assemble import assemble
from .document import Document, Page
from .preprocess import preprocess
from .recognize import Recognizer

log = logging.getLogger(__name__)

EventFn = Callable[[dict], None]


class Cancelled(Exception):
    pass


@dataclass
class JobControl:
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)

    def check(self) -> None:
        while self.pause.is_set() and not self.cancel.is_set():
            time.sleep(0.2)
        if self.cancel.is_set():
            raise Cancelled()

    def cancelled(self) -> bool:
        return self.cancel.is_set()


class Engine:
    """Owns the models (loaded once, shared by all jobs)."""

    def __init__(self):
        self.ocr = OCRModel()
        self.layout = LayoutModel()
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def ensure_models(self, settings, emit: EventFn) -> None:
        def progress(info: dict) -> None:
            emit({"type": "model", **info})

        if settings.layout.mode == "auto":
            self.layout.load(settings, progress)
        self.ocr.load(settings, progress)
        emit({"type": "model", "stage": "ready", "device": self.ocr.device})

    def unload(self) -> None:
        with self.lock:
            self.ocr.unload()
            self.layout.unload()

    # ------------------------------------------------------------------ #
    def run(self, job_dir: Path, source: Path, settings, emit: EventFn, control: JobControl | None = None, page_filter: list[int] | None = None, name: str | None = None) -> Document:
        """Process ``source`` into ``job_dir``.  Pages already finished (from a previous,
        interrupted run) are kept unless they are listed in ``page_filter``."""
        control = control or JobControl()
        job_dir = Path(job_dir)
        pages_dir = job_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        doc = load_document(job_dir) or Document(name=name or source.name, source_path=str(source))
        if name:
            doc.name = name
        doc.meta.setdefault("created", time.time())
        doc.meta["settings_preset"] = doc.meta.get("settings_preset")
        done_keys = {(p.source_index, p.split_part) for p in doc.pages if p.status == "done"}
        if page_filter is not None:
            redo = set(page_filter)
            doc.pages = [p for p in doc.pages if p.source_index not in redo]
            done_keys = {(p.source_index, p.split_part) for p in doc.pages if p.status == "done"}
        total_source = loader.count_pages(source)
        wanted = loader.parse_page_range(settings.input.page_range, total_source)
        if page_filter is not None:
            wanted = [i for i in wanted if i in set(page_filter)] or sorted(page_filter)
        emit({"type": "job", "status": "running", "total": len(wanted), "name": doc.name})
        with self.lock:
            self.ensure_models(settings, emit)
            recognizer = Recognizer(self.ocr, settings, progress=emit, cancel=control.cancelled)
            processed = 0
            for sp in loader.iter_pages(source, settings.input.pdf_dpi, _range_spec(wanted), settings.input.use_embedded_images):
                control.check()
                idx = sp.source_index
                if (idx, None) in done_keys or ((idx, 0) in done_keys and (idx, 1) in done_keys):
                    processed += 1
                    emit({"type": "progress", "done": processed, "total": len(wanted)})
                    continue
                emit({"type": "page_start", "source_index": sp.source_index, "done": processed, "total": len(wanted)})
                t0 = time.time()
                try:
                    parts = preprocess(sp.image, settings)
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("preprocess failed")
                    parts = []
                    emit({"type": "log", "level": "error", "message": f"Page {sp.source_index + 1}: preprocessing failed ({exc})"})
                for part in parts:
                    control.check()
                    page = self._process_part(doc, sp, part, pages_dir, settings, recognizer, emit, control)
                    page.elapsed = time.time() - t0
                    _insert_page(doc, page)
                    save_document(job_dir, doc)
                    emit({"type": "page_done", "page": page.to_dict(), "done": processed + 1, "total": len(wanted)})
                processed += 1
                emit({"type": "progress", "done": processed, "total": len(wanted)})
            for i, p in enumerate(doc.pages):
                p.index = i
            save_document(job_dir, doc)
        return doc

    def _process_part(self, doc, sp, part, pages_dir: Path, settings, recognizer: Recognizer, emit, control) -> Page:
        idx_tag = f"{sp.source_index + 1:04d}" + ("" if part.split_part is None else "ab"[part.split_part])
        img_path = pages_dir / f"{idx_tag}.jpg"
        thumb_path = pages_dir / f"{idx_tag}_thumb.jpg"
        part.image.save(img_path, quality=92)
        loader.render_thumbnail(part.image).save(thumb_path, quality=85)
        w, h = part.image.size
        page = Page(
            index=len(doc.pages),
            source_index=sp.source_index,
            source_path=doc.source_path,
            width=w,
            height=h,
            image_path=str(img_path.name),
            thumb_path=str(thumb_path.name),
            split_part=part.split_part,
            transform=list(part.transform),
            original_size=sp.image.size,
            source_size_pt=sp.page_size_pt,
            notes=part.notes,
            status="running",
        )
        # make the page visible right away (a GUI that connects now will see it)
        _insert_page(doc, page)
        save_document(pages_dir.parent, doc)
        emit({"type": "page_image", "page": page.to_dict()})
        try:
            from . import imaging

            gray = imaging.to_gray(part.image)
            ctx = L.build_context(gray)
            if settings.layout.mode == "auto":
                dets = self.layout.detect(part.image, settings.layout.threshold)
                regions = L.analyze(dets, ctx, settings, page.index)
            else:
                regions = L.full_page(ctx, settings, page.index)
            page.regions = regions
            save_document(pages_dir.parent, doc)
            emit({"type": "layout", "page": page.to_dict()})
            control.check()
            recognizer.recognize_page(part.image, regions, page.index)
            control.check()
            L.refine_after_ocr(regions, ctx, page)
            page.status = "done"
        except Cancelled:
            raise
        except Exception as exc:
            log.exception("page failed")
            page.status = "error"
            page.error = f"{type(exc).__name__}: {exc}"
            emit({"type": "log", "level": "error", "message": f"Page {sp.source_index + 1}: {page.error}", "trace": traceback.format_exc(limit=6)})
        return page


def _range_spec(indices: list[int]) -> str:
    return ",".join(str(i + 1) for i in indices) if indices else ""


def _insert_page(doc: Document, page: Page) -> None:
    key = (page.source_index, -1 if page.split_part is None else page.split_part)
    doc.pages = [p for p in doc.pages if (p.source_index, -1 if p.split_part is None else p.split_part) != key]
    doc.pages.append(page)
    doc.pages.sort(key=lambda p: (p.source_index, -1 if p.split_part is None else p.split_part))
    for i, p in enumerate(doc.pages):
        p.index = i


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _json_default(o):
    """Serialise numpy scalars/arrays that slip into region data."""
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def save_document(job_dir: Path, doc: Document) -> None:
    path = Path(job_dir) / "document.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc.to_dict(), ensure_ascii=False, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def load_document(job_dir: Path) -> Document | None:
    path = Path(job_dir) / "document.json"
    if not path.exists():
        return None
    try:
        return Document.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # pragma: no cover - corrupt file
        log.exception("could not read %s", path)
        return None


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def export_document(doc: Document, job_dir: Path, settings, formats: list[str] | None = None, translations=None, emit: EventFn | None = None, out_dir: Path | None = None) -> dict[str, str]:
    """Write all requested output formats. Returns {format: path}."""
    from ..export import output_basename

    emit = emit or (lambda e: None)
    formats = formats or list(settings.output.formats)
    out_dir = Path(out_dir) if out_dir else Path(job_dir) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    views = assemble(doc, settings)
    save_document(job_dir, doc)  # assemble() records footnotes/continuations on the pages
    base = output_basename(settings.output.filename_template, doc.name)
    results: dict[str, str] = {}
    for fmt in formats:
        try:
            path = out_dir / f"{base}.{ 'md' if fmt == 'md' else fmt}"
            if fmt == "txt":
                from ..export import txt

                txt.write(path, views, settings)
            elif fmt == "pdf":
                from ..export import pdf

                pdf.write(path, doc, views, settings, Path(job_dir))
            elif fmt == "docx":
                from ..export import docx_export

                docx_export.write(path, views, settings, doc)
            elif fmt == "md":
                from ..export import markdown

                markdown.write(path, views, settings, doc)
            elif fmt == "json":
                from ..export import json_export

                json_export.write(path, doc, views, settings)
            elif fmt == "html":
                from ..export import html_export

                html_export.write(path, views, settings, doc)
            else:
                continue
            results[fmt] = str(path)
            emit({"type": "export", "format": fmt, "path": str(path)})
        except Exception as exc:
            log.exception("export %s failed", fmt)
            emit({"type": "log", "level": "error", "message": f"Could not write {fmt.upper()}: {exc}"})
    if translations:
        from ..translate import exporter as tx

        results.update(tx.write_translations(out_dir, base, views, settings, translations, emit))
    return results


def copy_outputs(results: dict[str, str], dest: Path) -> dict[str, str]:
    dest.mkdir(parents=True, exist_ok=True)
    copied = {}
    for fmt, src in results.items():
        target = dest / Path(src).name
        if Path(src).resolve() != target.resolve():
            shutil.copy2(src, target)
        copied[fmt] = str(target)
    return copied


def default_job_dir(job_id: str) -> Path:
    return paths.jobs_dir() / job_id


def page_image(job_dir: Path, page: Page) -> Image.Image:
    return Image.open(Path(job_dir) / "pages" / page.image_path)
