"""Add real manuscripts to the benchmark: pages of the RASAM dataset (Maghrebi Arabic
manuscripts of the BULAC library, Paris), with their transcriptions and zones.

    python bench/fetch_rasam.py               # 2 pages from each of the 18 manuscripts
    python bench/fetch_rasam.py --per-manuscript 4

The transcriptions (PAGE XML) come from GitHub, pinned to one commit; the images from
the BULAC library's IIIF server (bina.bulac.fr).  Every transcribed line is cut out
along its outline, so lines can be scored on their own, and the zones the
transcribers marked (main text, marginal notes, catchwords, folio numbers) are kept to
score layout analysis on the whole pages.  Results go to bench/data/rasam/.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "rasam"
REPO = "calfa-co/rasam-dataset"
COMMIT = "c670eb6e6b3292899873d8b98c6575b8defce96f"
RAW = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/"
IIIF = "https://bina.bulac.fr/iiif/2/{id}/full/{w},{h}/0/default.jpg"
NS = {"p": "http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15"}

# zone names in the transcriptions -> what Warraq should call them
ZONES = {
    "text": "main",
    "MainZone": "main",
    "MainZone:title": "main",
    "marginalia": "margin",
    "MarginTextZone": "margin",
    "catchword": "catchword",
    "MarginTextZone:catchword": "catchword",
    "numbering": "folio",
}


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def zone_of(region) -> str:
    custom = region.get("custom", "")
    if "type:" not in custom:
        return "main"
    raw = custom.split("type:", 1)[1].split(";", 1)[0].strip()
    return ZONES.get(raw, "other")


def parse_page(xml: bytes) -> dict:
    root = ET.fromstring(xml)
    page = root.find("p:Page", NS)
    out = {"file": page.get("imageFilename"), "width": int(page.get("imageWidth")), "height": int(page.get("imageHeight")), "regions": []}
    for region in page.iter(f"{{{NS['p']}}}TextRegion"):
        zone = zone_of(region)
        coords = region.find("p:Coords", NS)
        lines = []
        for line in region.findall("p:TextLine", NS):
            unicode = line.find("p:TextEquiv/p:Unicode", NS)
            text = (unicode.text or "").strip() if unicode is not None else ""
            pts = _points(line.find("p:Coords", NS))
            if text and len(pts) >= 3:
                lines.append({"id": line.get("id"), "text": text, "points": pts})
        out["regions"].append({"zone": zone, "points": _points(coords), "lines": lines})
    return out


def _points(coords) -> list[tuple[int, int]]:
    if coords is None:
        return []
    return [tuple(int(float(v)) for v in p.split(",")) for p in coords.get("points", "").split() if "," in p]


def cut_line(image, points, pad: int = 6):
    """The line's outline on white, cropped to its bounding box (plus a little margin)."""
    from PIL import Image, ImageDraw

    xs, ys = [x for x, _ in points], [y for _, y in points]
    box = (max(0, min(xs) - pad), max(0, min(ys) - pad), min(image.width, max(xs) + pad), min(image.height, max(ys) + pad))
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    white = Image.new("RGB", image.size, (255, 255, 255))
    return Image.composite(image.convert("RGB"), white, mask).crop(box)


def choose(rows: list[dict], per_manuscript: int) -> list[dict]:
    """Evenly spread pages of every manuscript (the same ones on every run)."""
    by_ms: dict[str, list[dict]] = {}
    for r in rows:
        by_ms.setdefault(r["Manuscript"], []).append(r)
    picked = []
    for ms, pages in sorted(by_ms.items()):
        pages.sort(key=lambda r: r["FileName"])
        step = max(1, len(pages) // per_manuscript)
        picked += pages[step // 2 :: step][:per_manuscript]
    return picked


def fetch_page(row: dict) -> dict:
    from PIL import Image

    name = row["FileName"]
    folder = "rasam1" if row["Dataset"] == "RASAM1" else "rasam2"
    xml = get(RAW + f"page/{folder}/{name}.xml")
    page = parse_page(xml)
    img_path = OUT / "pages" / f"{name}.jpg"
    if not img_path.exists():
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(get(IIIF.format(id=row["IIIF image ID"], w=page["width"], h=page["height"])))
    image = Image.open(img_path)
    if image.size != (page["width"], page["height"]):
        image = image.resize((page["width"], page["height"]))
    lines = []
    for r_i, region in enumerate(page["regions"]):
        for line in region["lines"]:
            stem = f"{name}_{r_i}_{line['id']}"
            cut_line(image, line["points"]).save(OUT / "lines" / f"{stem}.png")
            (OUT / "lines" / f"{stem}.gt.txt").write_text(line["text"], encoding="utf-8")
            lines.append({"book": f"BULAC {row['Manuscript']}", "zone": region["zone"], "image": f"rasam/lines/{stem}.png", "text": f"rasam/lines/{stem}.gt.txt"})
    zones = [{"zone": r["zone"], "points": r["points"]} for r in page["regions"] if r["points"]]
    return {"lines": lines, "page": {"book": f"BULAC {row['Manuscript']}", "image": f"rasam/pages/{name}.jpg", "zones": zones}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-manuscript", type=int, default=2)
    args = ap.parse_args(argv)
    rows = list(csv.DictReader(io.StringIO(get(RAW + "list-images.tsv").decode("utf-8")), delimiter="\t"))
    picked = choose([r for r in rows if r.get("FileName")], args.per_manuscript)
    (OUT / "lines").mkdir(parents=True, exist_ok=True)
    manifest = {"source": f"https://github.com/{REPO}", "commit": COMMIT, "license": "RASAM dataset (see its LICENSE); images © BULAC", "lines": [], "pages": []}
    failed = 0
    with ThreadPoolExecutor(4) as pool:
        for row, fut in [(r, pool.submit(fetch_page, r)) for r in picked]:
            try:
                got = fut.result()
            except Exception as exc:
                failed += 1
                print(f"  ! {row['FileName']}: {exc}", file=sys.stderr)
                continue
            manifest["lines"] += got["lines"]
            manifest["pages"].append(got["page"])
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(manifest['pages'])} pages, {len(manifest['lines'])} lines in {OUT}" + (f" ({failed} pages failed)" if failed else ""))
    return 1 if failed and not manifest["pages"] else 0


if __name__ == "__main__":
    sys.exit(main())
