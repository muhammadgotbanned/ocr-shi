"""Command-line interface.

    warraq                      open the app (same as `warraq gui`)
    warraq ocr book.pdf -o out  OCR files without the GUI
    warraq info                 show GPU / model status
    warraq download             fetch the models ahead of time
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


def _settings(args):
    from .settings import SettingsStore, apply_preset, update_settings

    s = SettingsStore().snapshot()
    if getattr(args, "preset", None):
        s = apply_preset(s, args.preset)
    patch: dict = {}
    if getattr(args, "formats", None):
        patch.setdefault("output", {})["formats"] = [f.strip() for f in args.formats.split(",") if f.strip()]
    if getattr(args, "pages", None):
        patch.setdefault("input", {})["page_range"] = args.pages
    if getattr(args, "dpi", None):
        patch.setdefault("input", {})["pdf_dpi"] = args.dpi
    if getattr(args, "device", None):
        patch.setdefault("hardware", {})["device"] = args.device
    if getattr(args, "translate", None):
        patch.setdefault("translation", {}).update({"enabled": True, "target_language": args.translate})
    for item in getattr(args, "set", None) or []:
        key, _, value = item.partition("=")
        section, _, name = key.partition(".")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        patch.setdefault(section, {})[name] = parsed
    return update_settings(s, patch) if patch else s


def cmd_ocr(args) -> int:
    from .core.engine import Engine, export_document
    from .core.loader import SUPPORTED_EXTS

    settings = _settings(args)
    files: list[Path] = []
    for p in args.inputs:
        path = Path(p).expanduser()
        if path.is_dir():
            files += sorted(f for f in path.iterdir() if f.suffix.lower() in SUPPORTED_EXTS)
        elif path.exists():
            files.append(path)
        else:
            print(f"not found: {p}", file=sys.stderr)
    if not files:
        print("Nothing to do.", file=sys.stderr)
        return 1
    out_root = Path(args.output).expanduser() if args.output else Path.cwd()
    engine = Engine()
    start = time.time()

    def emit(e: dict) -> None:
        t = e.get("type")
        if t == "page_done":
            p = e["page"]
            kinds: dict[str, int] = {}
            for r in p["regions"]:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            print(f"  page {p['source_index'] + 1:>4}  {e.get('done')}/{e.get('total')}  {p['elapsed']:.1f}s  " + " ".join(f"{k}:{v}" for k, v in sorted(kinds.items())), flush=True)
        elif t == "model" and e.get("message"):
            print(f"  {e['message']}", flush=True)
        elif t == "model" and e.get("stage") == "download" and e.get("total"):
            print(f"\r  downloading {e.get('model')}: {e['fraction'] * 100:5.1f}%", end="", flush=True)
        elif t == "log":
            print(f"  ! {e.get('message')}", file=sys.stderr, flush=True)
        elif t == "translate":
            print(f"\r  translating {e.get('done')}/{e.get('total')}", end="", flush=True)

    for f in files:
        print(f"• {f.name}")
        job_dir = out_root / f".warraq-{f.stem}"
        doc = engine.run(job_dir, f, settings, emit)
        translations = None
        if settings.translation.enabled:
            from .core.assemble import assemble
            from .translate.engine import translate_views

            translations = translate_views(assemble(doc, settings), settings, emit)
            print()
        results = export_document(doc, job_dir, settings, translations=translations, emit=emit, out_dir=out_root)
        for fmt, path in results.items():
            print(f"  → {fmt:>15}: {path}")
        if not args.keep_work:
            import shutil

            shutil.rmtree(job_dir, ignore_errors=True)
    print(f"Done in {time.time() - start:.0f}s")
    return 0


def cmd_info(args) -> int:
    from .models import hardware, hub
    from .settings import SettingsStore

    rep = hardware.detect()
    s = SettingsStore().current
    print(f"Warraq · Python {rep.python} · torch {rep.torch} · CUDA {rep.cuda}")
    print(f"OS: {rep.os} · RAM {rep.ram_gb} GB · {rep.cpu_threads} threads")
    for d in rep.devices:
        mem = f" · {d.memory_gb} GB" if d.memory_gb else ""
        print(f"  [{d.kind}] {d.name}{mem}")
    for label, mid in (("OCR model", s.models.ocr_model), ("Layout model", s.models.layout_model), ("Translator", s.translation.model)):
        ok = hub.local_snapshot(mid, s.models) is not None
        print(f"  {label:13s} {mid}  {'✓ downloaded' if ok else '— not downloaded'}")
    for w in rep.warnings:
        print(f"  ! {w}")
    return 0


def cmd_download(args) -> int:
    from .models import hub
    from .settings import SettingsStore

    s = SettingsStore().current
    hub.configure_environment(s.models)
    targets = [("PP-DocLayoutV3", s.models.layout_model), ("Arabic-GLM-OCR", s.models.ocr_model)]
    if args.translator:
        targets.append(("Translator", s.translation.model))

    def progress(e):
        if e.get("total"):
            print(f"\r  {e.get('model')}: {e['fraction'] * 100:5.1f}%  ({e['downloaded'] / 1e6:,.0f} / {e['total'] / 1e6:,.0f} MB)", end="", flush=True)

    for label, mid in targets:
        print(f"• {label} ({mid})")
        path = hub.ensure_model(mid, s.models, "main", progress, label)
        print(f"\n  ✓ {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warraq", description="Warraq — local Arabic OCR & translation")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    g = sub.add_parser("gui", help="open the desktop app (default)")
    g.add_argument("--browser", action="store_true", help="use the web browser instead of a native window")
    g.add_argument("--port", type=int, default=0)
    o = sub.add_parser("ocr", help="OCR files from the command line")
    o.add_argument("inputs", nargs="+", help="PDF/image files or folders")
    o.add_argument("-o", "--output", help="output folder (default: current folder)")
    o.add_argument("-f", "--formats", help="comma separated: txt,pdf,docx,md,json,html")
    o.add_argument("-p", "--pages", help="page range, e.g. 1-20,25")
    o.add_argument("--preset", choices=["printed_book", "manuscript", "fast", "max_accuracy"])
    o.add_argument("--dpi", type=int)
    o.add_argument("--device", choices=["auto", "cuda", "mps", "cpu"])
    o.add_argument("--translate", metavar="LANGUAGE", help="also translate, e.g. English")
    o.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE", help="override any setting, e.g. --set recognition.batch_size=8")
    o.add_argument("--keep-work", action="store_true", help="keep the intermediate job folder")
    sub.add_parser("info", help="show hardware and model status")
    d = sub.add_parser("download", help="download the models now")
    d.add_argument("--translator", action="store_true", help="also download the translation model")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "ocr":
        return cmd_ocr(args)
    if args.cmd == "info":
        return cmd_info(args)
    if args.cmd == "download":
        return cmd_download(args)
    from .app import main as app_main

    return app_main(browser=getattr(args, "browser", False), port=getattr(args, "port", 0))


if __name__ == "__main__":
    sys.exit(main())
