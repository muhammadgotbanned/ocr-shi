"""Run Warraq's reader on the benchmark and score it.

    python bench/fetch.py                                   # once: download the data
    python bench/run.py                                     # default settings
    python bench/run.py --limit 3                           # quick: 3 lines per book
    python bench/run.py --set recognition.target_line_height=88 --tag taller
    python bench/fetch_rasam.py && python bench/run.py --suite rasam --preset manuscript

Every gold line is read the way the app reads a line of a page (enhancement, size
normalisation, self-checks and re-reads) and scored with bench/metrics.py.  The
pages, which have no gold text, are run through the whole pipeline (layout, zones,
recognition, assembly) and written as .txt files to look over; on the manuscript
pages (RASAM) the zones Warraq finds are also checked against the zones the
transcribers marked: main text, marginal notes, catchwords and folio numbers.
Results go to bench/results/<tag>/: report.md, report.json and the page texts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import metrics as M  # noqa: E402

DATA = HERE / "data"

# what Warraq should call each zone the RASAM transcribers marked
EXPECT = {"main": {"body", "title"}, "margin": {"margin"}, "catchword": {"catchword"}, "folio": {"page_number", "header", "footer"}}


def suites(which: str) -> list[tuple[str, dict]]:
    out = []
    if which in ("openiti", "all"):
        out.append(("openiti", json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))))
    rasam = DATA / "rasam" / "manifest.json"
    if which in ("rasam", "all") and rasam.exists():
        out.append(("rasam", json.loads(rasam.read_text(encoding="utf-8"))))
    return out


def layout_agreement(zones: list[dict], page) -> list[dict]:
    """For every marked zone, the kind of the Warraq region that covers most of it."""
    from warraq.core import geometry as G

    back = G.invert(page.transform)  # original image -> processed page
    rows = []
    for z in zones:
        if z["zone"] not in EXPECT:
            continue
        zb = G.bounds([G.apply(back, x, y) for x, y in z["points"]])
        best = max(page.regions, key=lambda r: G.intersection_area(zb, r.bbox), default=None)
        covered = best is not None and G.intersection_area(zb, best.bbox) >= 0.3 * max(1.0, G.area(zb))
        got = ("catchword" if best.label == "catchword" else best.kind) if covered else "missed"
        rows.append({"zone": z["zone"], "got": got, "ok": got in EXPECT[z["zone"]]})
    return rows


def settings_from(args):
    from warraq.settings import Settings, apply_preset, update_settings

    settings = Settings()  # the defaults, not this computer's saved settings
    if args.preset:
        settings = apply_preset(settings, args.preset)
    patch: dict = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        section, _, name = key.partition(".")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        patch.setdefault(section, {})[name] = parsed
    if args.device:
        patch.setdefault("hardware", {})["device"] = args.device
    return update_settings(settings, patch) if patch else settings


def read_line(recognizer, path: Path, settings, k: int) -> str:
    from PIL import Image

    from warraq.core import imaging
    from warraq.core.document import BODY, Region

    img = imaging.enhance(Image.open(path).convert("RGB"), settings.input.enhance, settings.input.denoise, settings.input.binarize)
    box = imaging.tight_ink_box(imaging.ink_mask(imaging.to_gray(img))) or (0, 0, img.width, img.height)
    region = Region(id=f"l{k}", kind=BODY, label="text", bbox=tuple(float(v) for v in box))
    recognizer.recognize_page(img, [region], 0)
    return region.text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"), help="name of the results folder")
    ap.add_argument("--limit", type=int, default=0, help="lines per book (0 = all)")
    ap.add_argument("--lines-only", action="store_true", help="skip the page runs")
    ap.add_argument("--device", choices=["auto", "cuda", "mps", "cpu"])
    ap.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE", help="override a setting")
    ap.add_argument("--out", type=Path, help="results folder (default bench/results/<tag>)")
    ap.add_argument("--suite", choices=["openiti", "rasam", "all"], default="all", help="printed classical works, manuscripts (fetch_rasam.py), or both")
    ap.add_argument("--preset", choices=["printed_book", "manuscript", "fast", "max_accuracy"])
    args = ap.parse_args(argv)

    chosen_suites = suites(args.suite)
    if not chosen_suites:
        print("No manuscript data: run  python bench/fetch_rasam.py  first.", file=sys.stderr)
        return 1
    for name, manifest in chosen_suites:
        missing = [l["image"] for l in manifest["lines"] if not (DATA / l["image"]).exists()]
        if missing:
            print(f"{len(missing)} {name} files are missing: run  python bench/{'fetch' if name == 'openiti' else 'fetch_rasam'}.py  first.", file=sys.stderr)
            return 1
    settings = settings_from(args)
    out = args.out or HERE / "results" / args.tag
    out.mkdir(parents=True, exist_ok=True)

    from warraq.core.engine import Engine, export_document
    from warraq.core.recognize import Recognizer

    engine = Engine()
    t_load = time.time()
    engine.ocr.load(settings, lambda e: e.get("message") and print(" ", e["message"], flush=True))
    print(f"OCR model on {engine.ocr.device} in {time.time() - t_load:.0f}s", flush=True)
    recognizer = Recognizer(engine.ocr, settings)

    lines = [{**l, "suite": name} for name, manifest in chosen_suites for l in manifest["lines"]]
    if args.limit:
        taken: dict[str, int] = {}
        chosen = []
        for line in lines:
            if taken.get(line["book"], 0) < args.limit:
                taken[line["book"]] = taken.get(line["book"], 0) + 1
                chosen.append(line)
        lines = chosen
    rows = []
    t_lines = time.time()
    for k, line in enumerate(lines):
        gold = (DATA / line["text"]).read_text(encoding="utf-8").strip()
        t0 = time.time()
        pred = read_line(recognizer, DATA / line["image"], settings, k)
        row = {"suite": line["suite"], "book": line["book"], "zone": line.get("zone", "main"), "image": line["image"], "gold": gold, "pred": pred, "seconds": round(time.time() - t0, 2), **M.score(pred, gold)}
        rows.append(row)
        acc = 1 - row["char_errors"] / max(1, row["chars"])
        print(f"[{k + 1:3d}/{len(lines)}] {acc:6.1%}  {row['seconds']:5.1f}s  {line['book'][:40]}", flush=True)
    lines_seconds = time.time() - t_lines

    books = {}
    for row in rows:
        books.setdefault(row["book"], []).append(row)
    report = {
        "tag": args.tag,
        "settings": {k: v for k, v in (settings.model_dump(mode="json")).items() if k in ("input", "layout", "recognition", "text")},
        "device": engine.ocr.device,
        "overall": M.accuracy(rows),
        "suites": {name: M.accuracy([r for r in rows if r["suite"] == name]) for name, _ in chosen_suites},
        "zones": {z: M.accuracy([r for r in rows if r["zone"] == z]) for z in sorted({r["zone"] for r in rows})},
        "books": {b: M.accuracy(r) for b, r in books.items()},
        "seconds_per_line": lines_seconds / max(1, len(rows)),
        "lines": rows,
        "pages": [],
    }

    if not args.lines_only:
        settings_pages = settings.model_copy(deep=True)
        settings_pages.output.formats = ["txt"]
        zone_rows = []
        for suite, manifest in chosen_suites:
            for page in manifest["pages"]:
                src = DATA / page["image"]
                name = Path(page["image"]).stem
                t0 = time.time()
                page_settings = settings_pages
                if page.get("zones"):
                    # the marked zones are in the coordinates of the single page they show
                    page_settings = settings_pages.model_copy(deep=True)
                    page_settings.input.split_spreads = "off"
                doc = engine.run(out / "work" / name, src, page_settings, lambda e: None, name=name)
                results = export_document(doc, out / "work" / name, page_settings, ["txt"], out_dir=out / "pages")
                kinds: dict[str, int] = {}
                for r in doc.pages[0].regions if doc.pages else []:
                    kinds[r.kind] = kinds.get(r.kind, 0) + 1
                entry = {"suite": suite, "book": page["book"], "image": page["image"], "seconds": round(time.time() - t0, 1), "regions": kinds, "txt": results.get("txt")}
                if page.get("zones") and doc.pages:
                    entry["zones"] = layout_agreement(page["zones"], doc.pages[0])
                    zone_rows += entry["zones"]
                report["pages"].append(entry)
                print(f"page {name}: {time.time() - t0:.0f}s  {kinds}", flush=True)
        if zone_rows:
            report["layout"] = {
                z: {"zones": len(rs), "agreement": sum(r["ok"] for r in rs) / len(rs), "found_as": _count(r["got"] for r in rs)}
                for z in EXPECT
                if (rs := [r for r in zone_rows if r["zone"] == z])
            }

    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(render(report), encoding="utf-8")
    o = report["overall"]
    print(f"\nCharacter accuracy {o['char_accuracy']:.1%} · word accuracy {o['word_accuracy']:.1%} · raw {o['raw_char_accuracy']:.1%} over {o['lines']} lines")
    print(f"Report: {out / 'report.md'}")
    return 0


def _count(items) -> dict:
    out: dict = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def render(report: dict) -> str:
    o = report["overall"]
    md = [
        f"# Benchmark {report['tag']}",
        "",
        f"Device: {report['device']} · {report['seconds_per_line']:.1f} s per line",
        "",
        f"**Character accuracy {o['char_accuracy']:.1%}** · word accuracy {o['word_accuracy']:.1%} · raw character accuracy {o['raw_char_accuracy']:.1%} · {o['lines']} lines",
        "",
    ]
    if len(report.get("suites", {})) > 1 or len(report.get("zones", {})) > 1:
        md += ["| Part | Lines | Characters | Words |", "|---|---:|---:|---:|"]
        for name, a in report.get("suites", {}).items():
            md.append(f"| suite: {name} | {a['lines']} | {a['char_accuracy']:.1%} | {a['word_accuracy']:.1%} |")
        for name, a in report.get("zones", {}).items():
            md.append(f"| zone: {name} | {a['lines']} | {a['char_accuracy']:.1%} | {a['word_accuracy']:.1%} |")
        md.append("")
    md += [
        "| Work | Lines | Characters | Words | Raw characters |",
        "|---|---:|---:|---:|---:|",
    ]
    for book, a in report["books"].items():
        md.append(f"| {book} | {a['lines']} | {a['char_accuracy']:.1%} | {a['word_accuracy']:.1%} | {a['raw_char_accuracy']:.1%} |")
    worst = sorted(report["lines"], key=lambda r: r["char_errors"] / max(1, r["chars"]), reverse=True)[:15]
    md += ["", "## Lines with the most errors", ""]
    for r in worst:
        md += [f"- `{r['image']}` ({r['char_errors']}/{r['chars']})", f"  - gold: {r['gold']}", f"  - read: {r['pred']}"]
    if report.get("layout"):
        md += ["", "## Layout on the manuscript pages", "", "| Zone marked by the transcribers | Zones | Found as expected | Found as |", "|---|---:|---:|---|"]
        for z, a in report["layout"].items():
            md.append(f"| {z} | {a['zones']} | {a['agreement']:.0%} | {', '.join(f'{k} {v}' for k, v in a['found_as'].items())} |")
    if report["pages"]:
        md += ["", "## Pages (whole pipeline)", "", "| Page | Seconds | Regions |", "|---|---:|---|"]
        for p in report["pages"]:
            md.append(f"| {p['book']} · `{Path(p['image']).name}` | {p['seconds']} | {', '.join(f'{k} {v}' for k, v in sorted(p['regions'].items()))} |")
    return "\n".join(md) + "\n"


if __name__ == "__main__":
    sys.exit(main())
