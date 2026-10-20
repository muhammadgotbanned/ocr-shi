"""HTTP API + static GUI, served locally on 127.0.0.1."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, paths
from ..core.loader import SUPPORTED_EXTS
from ..models import hardware, hub
from ..settings import PRESETS, SettingsStore, ui_schema
from .events import EventBus, sse_format
from .jobs import JobManager

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, store: SettingsStore | None = None, manager: JobManager | None = None):
        self.store = store or SettingsStore()
        self.bus = EventBus()
        self.manager = manager or JobManager(self.store, self.bus)
        self.hardware: dict | None = None
        self.window = None  # pywebview window when running as a desktop app
        self.downloading = threading.Lock()


def create_app(state: AppState | None = None) -> FastAPI:
    state = state or AppState()
    app = FastAPI(title="Warraq", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.warraq = state

    # ------------------------------------------------------------------ #
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/system")
    def system():
        if state.hardware is None:
            state.hardware = hardware.detect().to_dict()
        s = state.store.current
        eng = state.manager.engine
        models = {
            "ocr": {
                "id": s.models.ocr_model,
                "downloaded": hub.local_snapshot(s.models.ocr_model, s.models, s.models.ocr_revision) is not None,
                "loaded": eng.ocr.is_loaded,
                "device": eng.ocr.device if eng.ocr.is_loaded else None,
                "device_label": hardware.describe(eng.ocr.device) if eng.ocr.is_loaded else None,
            },
            "layout": {
                "id": s.models.layout_model,
                "downloaded": hub.local_snapshot(s.models.layout_model, s.models) is not None,
                "loaded": eng.layout.is_loaded,
            },
            "translation": {
                "id": s.translation.model,
                "downloaded": hub.local_snapshot(s.translation.model, s.models) is not None if s.translation.engine == "local" else None,
            },
        }
        return {"version": __version__, "desktop": state.window is not None, "hardware": state.hardware, "models": models, "model_status": state.manager.model_status, "data_dir": str(paths.data_dir()), "output_dir": str(_output_root(state))}

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    @app.get("/api/settings")
    def get_settings():
        return {
            "values": state.store.current.model_dump(mode="json"),
            "schema": ui_schema(),
            "presets": {k: {"title": v["title"], "description": v["description"]} for k, v in PRESETS.items()},
        }

    @app.patch("/api/settings")
    def patch_settings(patch: dict = Body(...)):
        try:
            s = state.store.update(patch)
        except Exception as exc:
            raise HTTPException(400, f"Invalid setting: {exc}")
        state.bus.publish({"type": "settings", "values": s.model_dump(mode="json")})
        return {"values": s.model_dump(mode="json")}

    @app.post("/api/settings/reset")
    def reset_settings(body: dict = Body(default={})):
        s = state.store.reset(body.get("section"))
        return {"values": s.model_dump(mode="json")}

    @app.post("/api/settings/preset/{name}")
    def preset(name: str):
        from ..settings import apply_preset

        try:
            s = state.store.replace(apply_preset(state.store.current, name))
        except KeyError:
            raise HTTPException(404, "Unknown preset")
        return {"values": s.model_dump(mode="json")}

    # ------------------------------------------------------------------ #
    # Models
    # ------------------------------------------------------------------ #
    @app.post("/api/models/download")
    def download_models(body: dict = Body(default={})):
        which = body.get("which", ["layout", "ocr"])

        def work():
            if not state.downloading.acquire(blocking=False):
                return
            try:
                s = state.store.snapshot()
                hub.configure_environment(s.models)
                targets = []
                if "layout" in which:
                    targets.append(("PP-DocLayoutV3", s.models.layout_model, "main"))
                if "ocr" in which:
                    targets.append(("Arabic-GLM-OCR", s.models.ocr_model, s.models.ocr_revision))
                if "translation" in which and s.translation.engine == "local":
                    targets.append(("Translator", s.translation.model, "main"))
                for label, mid, rev in targets:
                    state.bus.publish({"type": "model", "stage": "download", "model": label, "fraction": 0.0})
                    hub.ensure_model(mid, s.models, rev, lambda e: state.bus.publish({"type": "model", **e}), label)
                state.bus.publish({"type": "model", "stage": "downloaded"})
            except Exception as exc:
                state.bus.publish({"type": "model", "stage": "error", "message": str(exc)})
            finally:
                state.downloading.release()

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    @app.post("/api/models/unload")
    def unload_models():
        state.manager.engine.unload()
        state.bus.publish({"type": "model", "stage": "unloaded"})
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #
    @app.get("/api/jobs")
    def list_jobs():
        return state.manager.list()

    @app.post("/api/jobs")
    async def create_jobs(files: list[UploadFile] = File(...), options: str = Form("{}")):
        import json

        opts = json.loads(options or "{}")
        created = []
        tmpdir = Path(tempfile.mkdtemp(prefix="warraq-upload-"))
        try:
            for up in files:
                name = Path(up.filename or "upload").name
                if Path(name).suffix.lower() not in SUPPORTED_EXTS:
                    raise HTTPException(400, f"Unsupported file type: {name}")
                dest = tmpdir / name
                with open(dest, "wb") as fh:
                    while chunk := await up.read(1 << 20):
                        fh.write(chunk)
                job = state.manager.create(dest, name, move=True, overrides=opts.get("overrides") or {}, preset=opts.get("preset"), translate=opts.get("translate"))
                created.append(job.to_dict())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return created

    @app.post("/api/jobs/from-paths")
    def create_from_paths(body: dict = Body(...)):
        created = []
        for p in body.get("paths", []):
            src = Path(p).expanduser()
            if src.is_dir():
                files = sorted(f for f in src.iterdir() if f.suffix.lower() in SUPPORTED_EXTS)
            else:
                files = [src]
            for f in files:
                if not f.exists() or f.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                job = state.manager.create(f, f.name, overrides=body.get("overrides") or {}, preset=body.get("preset"), translate=body.get("translate"))
                created.append(job.to_dict())
        if not created:
            raise HTTPException(400, "No supported files (PDF or images) were given.")
        return created

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = _job(state, job_id)
        from ..core.engine import load_document

        doc = load_document(job.dir)
        return {"job": job.to_dict(), "document": doc.to_dict() if doc else None}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        _job(state, job_id)
        state.manager.delete(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/{action}")
    def job_action(job_id: str, action: str, body: dict = Body(default={})):
        job = _job(state, job_id)
        m = state.manager
        if action == "cancel":
            m.cancel(job_id)
        elif action == "pause":
            m.pause(job_id)
        elif action == "resume":
            m.resume(job_id)
        elif action == "reocr":
            pages = body.get("pages")
            if body.get("overrides"):
                job.overrides = {**job.overrides, **body["overrides"]}
            m.enqueue(job, "reocr", pages=[int(p) for p in pages] if pages else None)
        elif action == "area":
            # read part of a page again: a box the user drew, or one region
            if job.status in ("running", "queued", "paused") and job.kind != "area":
                raise HTTPException(409, "This document is still being read; re-read part of it when it is done.")
            area = _area_request(job, body)
            m.enqueue(job, "area", area=area)
        elif action == "translate":
            if body.get("overrides"):
                job.overrides = {**job.overrides, **body["overrides"]}
            m.enqueue(job, "translate")
        elif action == "export":
            if body.get("formats"):
                job.overrides = {**job.overrides, "output": {**job.overrides.get("output", {}), "formats": body["formats"]}}
            m.enqueue(job, "export")
        elif action == "open-folder":
            target = next(iter(job.published.values()), None) or next(iter(job.outputs.values()), None)
            paths.open_in_file_manager(Path(target) if target else job.dir)
        else:
            raise HTTPException(404, "Unknown action")
        return job.to_dict()

    @app.patch("/api/jobs/{job_id}/regions/{region_id}")
    def edit_region(job_id: str, region_id: str, body: dict = Body(...)):
        _job(state, job_id)
        try:
            page = state.manager.update_region(job_id, region_id, body.get("text"), body.get("kind"))
        except KeyError:
            raise HTTPException(404, "Region not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return page

    @app.get("/api/jobs/{job_id}/preview")
    def preview(job_id: str, fmt: str = "txt"):
        job = _job(state, job_id)
        from ..core.assemble import assemble
        from ..core.engine import load_document
        from ..export import txt

        doc = load_document(job.dir)
        if doc is None:
            return Response("", media_type="text/plain; charset=utf-8")
        settings = state.manager.settings_for(job)
        views = assemble(doc, settings)
        return Response(txt.render(views, settings), media_type="text/plain; charset=utf-8")

    @app.get("/api/jobs/{job_id}/assembled")
    def assembled(job_id: str):
        """Per-page structure exactly as the exporters see it (labels, merged footnotes)."""
        job = _job(state, job_id)
        from ..core.assemble import assemble
        from ..core.engine import load_document

        doc = load_document(job.dir)
        if doc is None:
            return {"pages": []}
        settings = state.manager.settings_for(job)
        views = assemble(doc, settings)
        pages = []
        for v in views:
            pages.append(
                {
                    "index": v.page.index,
                    "number": v.number,
                    "printed_number": v.printed_number,
                    "headers": v.headers,
                    "footers": v.footers,
                    "margins": [{"label": m.label, "side": m.side, "text": m.text, "region_id": m.region_id} for m in v.margins],
                    "footnotes": [
                        {"label": f.label, "text": f.text, "regions": f.region_ids, "continued_from_previous": f.continued_from_previous, "merged_from_page": f.merged_from_page}
                        for f in v.footnotes
                    ],
                }
            )
        return {
            "pages": pages,
            "footnote_header": settings.footnotes.section_header,
            "margin_header": settings.margins.section_header,
        }

    @app.get("/api/jobs/{job_id}/translation")
    def translation(job_id: str):
        job = _job(state, job_id)
        from .jobs import _load_translations

        tr = _load_translations(job.dir)
        if not tr:
            return {"pages": []}
        return {"pages": [{("|".join(k) if isinstance(k, tuple) else k): v for k, v in p.items()} for p in tr]}

    @app.get("/api/jobs/{job_id}/pages/{name}")
    def page_file(job_id: str, name: str):
        job = _job(state, job_id)
        path = (job.dir / "pages" / Path(name).name).resolve()
        if not str(path).startswith(str((job.dir / "pages").resolve())) or not path.exists():
            raise HTTPException(404, "Not found")
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/jobs/{job_id}/files/{fmt}")
    def download(job_id: str, fmt: str):
        job = _job(state, job_id)
        path = job.outputs.get(fmt)
        if not path or not Path(path).exists():
            raise HTTPException(404, "File not ready")
        return FileResponse(path, filename=Path(path).name)

    # ------------------------------------------------------------------ #
    # Native dialogs (desktop mode)
    # ------------------------------------------------------------------ #
    @app.post("/api/dialog/{kind}")
    def dialog(kind: str):
        window = state.window
        if window is None:
            raise HTTPException(409, "Native dialogs are only available in the desktop app.")
        import webview

        if kind == "open":
            types = ("Documents (*.pdf;*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.webp;*.bmp)", "All files (*.*)")
            result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=types)
        elif kind == "folder":
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            raise HTTPException(404, "Unknown dialog")
        return {"paths": list(result) if result else []}

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    @app.get("/api/events")
    async def events(request: Request):
        token = state.bus.subscribe()
        _, queue = token

        async def stream():
            try:
                yield "retry: 1500\n\n"
                for e in state.bus.recent()[-50:]:
                    yield sse_format({**e, "replay": True})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield sse_format(event)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                state.bus.unsubscribe(token)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ #
    # GUI
    # ------------------------------------------------------------------ #
    web = paths.WEB_DIR

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse((web / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=str(web)), name="static")

    @app.exception_handler(KeyError)
    async def key_error(_, exc):  # pragma: no cover
        return JSONResponse({"detail": f"Not found: {exc}"}, status_code=404)

    return app


def _job(state: AppState, job_id: str):
    try:
        return state.manager.get(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found")


def _area_request(job, body: dict) -> dict:
    from ..core.area import KINDS
    from ..core.engine import load_document

    doc = load_document(job.dir)
    try:
        page = int(body.get("page", -1))
    except (TypeError, ValueError):
        page = -1
    if doc is None or not 0 <= page < len(doc.pages):
        raise HTTPException(400, "Unknown page")
    kind = body.get("kind") or "auto"
    if kind != "auto" and kind not in KINDS:
        raise HTTPException(400, f"Unknown region type: {kind}")
    area: dict[str, Any] = {"page": page, "kind": kind}
    if body.get("region_id"):
        if not any(r.id == body["region_id"] for r in doc.pages[page].regions):
            raise HTTPException(404, "Region not found")
        area["region_id"] = str(body["region_id"])
        return area
    try:
        x0, y0, x1, y1 = (float(v) for v in body.get("bbox") or ())
    except (TypeError, ValueError):
        raise HTTPException(400, "bbox must be [x0, y0, x1, y1]")
    p = doc.pages[page]
    x0, x1 = sorted((max(0.0, min(p.width, x0)), max(0.0, min(p.width, x1))))
    y0, y1 = sorted((max(0.0, min(p.height, y0)), max(0.0, min(p.height, y1))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise HTTPException(400, "The selected area is too small.")
    area["bbox"] = [x0, y0, x1, y1]
    return area


def _output_root(state: AppState) -> Path:
    s = state.store.current
    return Path(s.general.output_dir).expanduser() if s.general.output_dir else paths.default_output_dir()
