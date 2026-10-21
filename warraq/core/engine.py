"""The OCR engine: runs a document through loading, layout, recognition,
assembly and export, emitting progress events along the way."""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from .. import paths
from ..models import hardware
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


@dataclass
class _PreparedPart:
    """One page (or one half of a spread) ready to be read: laid out and cut into lines."""

    part: object  # preprocess.ProcessedPage
    ctx: L.PageContext | None = None
    regions: list = field(default_factory=list)
    jobs: list = field(default_factory=list)  # recognizer.RegionJob
    error: Exception | None = None
    trace: str = ""


@dataclass
class _PreparedPage:
    sp: object  # loader.SourcePage
    skip: bool = False  # finished in an earlier run
    parts: list[_PreparedPart] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


class Engine:
    """Owns the models (loaded once, shared by all jobs)."""

    def __init__(self):
        self.ocr = OCRModel()
        self.layout = LayoutModel()
        self.lock = threading.RLock()
        self._unified_gb: float | None = None

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
            emit({"type": "status", "message": "Preparing the first page…"})
            # the next page is loaded, cleaned up, laid out and cut into lines on a second
            # thread while the model reads this one
            with contextlib.closing(self._prefetch(source, settings, wanted, done_keys, recognizer)) as pages:
                for item in pages:
                    control.check()
                    sp = item.sp
                    if item.skip:
                        processed += 1
                        emit({"type": "progress", "done": processed, "total": len(wanted)})
                        continue
                    emit({"type": "page_start", "source_index": sp.source_index, "done": processed, "total": len(wanted)})
                    if item.error:
                        emit({"type": "log", "level": "error", "message": item.error})
                    t0 = time.time()
                    for prepared in item.parts:
                        control.check()
                        page = self._read_part(doc, sp, prepared, pages_dir, settings, recognizer, emit, control)
                        page.elapsed = item.seconds + (time.time() - t0)
                        _insert_page(doc, page)
                        save_document(job_dir, doc)
                        emit({"type": "page_done", "page": page.to_dict(), "done": processed + 1, "total": len(wanted)})
                        if self._tight_unified_memory():
                            hardware.empty_cache()  # give Metal's cached buffers back to macOS
                    processed += 1
                    emit({"type": "progress", "done": processed, "total": len(wanted)})
            for i, p in enumerate(doc.pages):
                p.index = i
            save_document(job_dir, doc)
        return doc

    def reread_area(self, job_dir: Path, settings, emit: EventFn, control: JobControl | None = None, page: int = 0, bbox=None, region_id: str | None = None, kind: str = "auto") -> Page:
        """Read part of a finished page again: the box ``bbox`` (page pixels) or the region
        ``region_id``, optionally as a given kind of region, and merge it into the page."""
        from . import area as A
        from . import imaging

        control = control or JobControl()
        job_dir = Path(job_dir)
        doc = load_document(job_dir)
        if doc is None or not 0 <= page < len(doc.pages):
            raise ValueError("That page is not part of this document.")
        pg = doc.pages[page]
        if bbox is None and not region_id:
            raise ValueError("Choose an area or a region to read.")
        img = Image.open(job_dir / "pages" / pg.image_path).convert("RGB")
        with self.lock:
            self.ocr.load(settings, lambda info: emit({"type": "model", **info}))
            emit({"type": "model", "stage": "ready", "device": self.ocr.device})
            ctx = L.build_context(imaging.to_gray(img))
            A.prime_context(pg.regions, ctx)
            if region_id:
                plan = A.plan_region(pg.regions, region_id, kind)
            else:
                plan = A.plan_area(pg.regions, ctx, bbox, kind, pg.index, settings.text.line_mode)
            context = [r for r in pg.regions if all(r is not x for x in plan.replaces) and r is not plan.region] + [plan.region]
            emit({"type": "status", "page": pg.index, "message": f"Re-reading part of page {pg.index + 1}…"})
            recognizer = Recognizer(self.ocr, settings, progress=emit, cancel=control.cancelled)
            recognizer.recognize_page(img, context, pg.index, targets=[plan.region])
            control.check()
            A.apply(pg, plan, settings.layout.reading_order)
            save_document(job_dir, doc)
        emit({"type": "page_done", "page": pg.to_dict(), "done": 1, "total": 1})
        return pg

    def _prefetch(self, source: Path, settings, wanted: list[int], done_keys: set, recognizer: Recognizer):
        """Yield the wanted pages ready for reading, one page ahead on a worker thread."""
        ahead: "queue.Queue[object]" = queue.Queue(maxsize=1)
        stop = threading.Event()
        end = object()

        def put(item) -> bool:
            while not stop.is_set():
                try:
                    ahead.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    continue
            return False

        def work() -> None:
            try:
                for sp in loader.iter_pages(source, settings.input.pdf_dpi, _range_spec(wanted), settings.input.use_embedded_images):
                    if stop.is_set():
                        return
                    idx = sp.source_index
                    if (idx, None) in done_keys or ((idx, 0) in done_keys and (idx, 1) in done_keys):
                        item = _PreparedPage(sp, skip=True)
                    else:
                        item = self._prepare_page(sp, settings, recognizer)
                    if not put(item):
                        return
            except BaseException as exc:  # a broken file: the job fails as it did before
                put(exc)
                return
            put(end)

        worker = threading.Thread(target=work, name="warraq-prefetch", daemon=True)
        worker.start()
        try:
            while True:
                item = ahead.get()
                if item is end:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()
            with contextlib.suppress(queue.Empty):
                ahead.get_nowait()  # let a worker blocked on a full queue see the stop
            worker.join(timeout=10)

    def _prepare_page(self, sp, settings, recognizer: Recognizer) -> _PreparedPage:
        """Everything before the OCR model: preprocessing, layout, cutting regions into lines."""
        from . import imaging

        t0 = time.time()
        item = _PreparedPage(sp)
        try:
            parts = preprocess(sp.image, settings)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("preprocess failed")
            parts = []
            item.error = f"Page {sp.source_index + 1}: preprocessing failed ({exc})"
        for part in parts:
            prepared = _PreparedPart(part)
            try:
                ctx = L.build_context(imaging.to_gray(part.image))
                if settings.layout.mode == "auto":
                    regions = L.analyze(self._detect(part.image, settings), ctx, settings, 0)
                else:
                    regions = L.full_page(ctx, settings, 0)
                prepared.ctx, prepared.regions = ctx, regions
                prepared.jobs = recognizer.prepare(part.image, regions)
            except Exception as exc:
                prepared.error, prepared.trace = exc, traceback.format_exc(limit=6)
            item.parts.append(prepared)
        item.seconds = time.time() - t0
        return item

    def _tight_unified_memory(self) -> bool:
        """An Apple GPU on a Mac with 16 GB or less, where the model, macOS and every open
        app share one pool of memory."""
        if str(self.ocr.device) != "mps":
            return False
        if self._unified_gb is None:
            self._unified_gb = hardware.device_memory_gb("mps") or 0.0
        return self._unified_gb <= 16

    def _detect(self, image: Image.Image, settings):
        # The layout model runs beside the batch being read only where that is safe and
        # useful: both on NVIDIA GPUs, or the layout model on the CPU while OCR uses a GPU.
        # Apple's Metal backend aborts when two threads submit work at once ("A command
        # encoder is already encoding to this command buffer"), and two models on the CPU
        # would only fight over its cores, so there the layout model waits its turn.
        ocr_dev, lay_dev = str(self.ocr.device), str(self.layout.device)
        side_by_side = (ocr_dev.startswith("cuda") and lay_dev.startswith("cuda")) or (lay_dev == "cpu" and ocr_dev != "cpu")
        with contextlib.nullcontext() if side_by_side else self.ocr.lock:
            return self.layout.detect(image, settings.layout.threshold)

    def _read_part(self, doc, sp, prepared: _PreparedPart, pages_dir: Path, settings, recognizer: Recognizer, emit, control) -> Page:
        part = prepared.part
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
            if prepared.error is not None:
                raise prepared.error
            regions = prepared.regions
            for i, r in enumerate(regions):  # laid out before its place in the document was known
                r.id = f"p{page.index + 1}-r{i + 1}"
            page.regions = regions
            save_document(pages_dir.parent, doc)
            emit({"type": "layout", "page": page.to_dict()})
            control.check()
            recognizer.read(prepared.jobs, page.index)
            control.check()
            L.refine_after_ocr(regions, prepared.ctx, page)
            page.status = "done"
        except Cancelled:
            raise
        except Exception as exc:
            log.exception("page failed")
            page.status = "error"
            page.error = f"{type(exc).__name__}: {exc}"
            trace = prepared.trace or traceback.format_exc(limit=6)
            emit({"type": "log", "level": "error", "message": f"Page {sp.source_index + 1}: {page.error}", "trace": trace})
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
