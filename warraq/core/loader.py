"""Load PDFs and images as page images."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageOps, ImageSequence

log = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".jp2", ".heic", ".heif"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS

try:  # optional HEIC support (iPhone photos)
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
except Exception:  # pragma: no cover
    pass


@dataclass
class SourcePage:
    source_index: int
    image: Image.Image
    # size of the page in PDF points (None for image inputs)
    page_size_pt: tuple[float, float] | None
    dpi: float


def parse_page_range(spec: str, total: int) -> list[int]:
    """'1-3, 7, 10-' -> [0, 1, 2, 6, 9, 10, ...] (0-based, sorted, de-duplicated)."""
    spec = (spec or "").strip()
    if not spec or spec.lower() in ("all", "*"):
        return list(range(total))
    spec = spec.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩،", "0123456789,"))
    pages: set[int] = set()
    for part in re.split(r"[,\s;]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*[-–]\s*(\d*)", part)
        if m:
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else total
            pages.update(range(max(1, start), min(total, end) + 1))
        elif part.isdigit():
            n = int(part)
            if 1 <= n <= total:
                pages.add(n)
    return sorted(p - 1 for p in pages)


def count_pages(path: str | Path) -> int:
    path = Path(path)
    if path.suffix.lower() in PDF_EXTS:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            return len(pdf)
        finally:
            pdf.close()
    with Image.open(path) as im:
        return getattr(im, "n_frames", 1)


def _normalise(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def iter_pages(path: str | Path, dpi: int = 300, page_range: str = "", use_embedded: bool = True) -> Iterator[SourcePage]:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        yield from _iter_pdf(path, dpi, page_range, use_embedded)
    elif ext in IMAGE_EXTS:
        with Image.open(path) as im:
            frames = getattr(im, "n_frames", 1)
            wanted = set(parse_page_range(page_range, frames))
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i not in wanted:
                    continue
                info_dpi = frame.info.get("dpi", (dpi, dpi))
                try:
                    fdpi = float(info_dpi[0]) or float(dpi)
                except Exception:
                    fdpi = float(dpi)
                yield SourcePage(i, _normalise(frame.copy()), None, fdpi)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")


def _iter_pdf(path: Path, dpi: int, page_range: str, use_embedded: bool) -> Iterator[SourcePage]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        total = len(pdf)
        for i in parse_page_range(page_range, total):
            page = pdf[i]
            try:
                w_pt, h_pt = page.get_size()
                img = None
                eff_dpi = float(dpi)
                if use_embedded:
                    img, eff_dpi = _embedded_scan(page, w_pt, h_pt, dpi)
                if img is None:
                    bitmap = page.render(scale=dpi / 72.0, may_draw_forms=True)
                    img = bitmap.to_pil()
                    eff_dpi = float(dpi)
                yield SourcePage(i, _normalise(img), (w_pt, h_pt), eff_dpi)
            finally:
                page.close()
    finally:
        pdf.close()


def _embedded_scan(page, w_pt: float, h_pt: float, dpi: int):
    """If the page is a single full-page scan image, return it at native resolution."""
    import pypdfium2 as pdfium

    try:
        if page.get_rotation() not in (0, None):
            return None, dpi
        images = list(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=2))
        texts = list(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_TEXT], max_depth=2))
        if len(images) != 1 or len(texts) > 0:
            return None, dpi
        obj = images[0]
        get_bounds = getattr(obj, "get_bounds", None) or getattr(obj, "get_pos")
        left, bottom, right, top = get_bounds()
        if (right - left) < 0.9 * w_pt or (top - bottom) < 0.9 * h_pt:
            return None, dpi
        matrix = obj.get_matrix()
        a, b, c, d = (matrix.get()[:4] if hasattr(matrix, "get") else (matrix.a, matrix.b, matrix.c, matrix.d))
        if abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
            return None, dpi  # rotated / flipped placement: let PDFium render it
        bitmap = obj.get_bitmap(render=False)
        img = bitmap.to_pil()
        # Native pixels are always better than a re-render (which only resamples them);
        # small text is enlarged later, per region, by the recogniser.
        native_dpi = img.width / (w_pt / 72.0)
        if native_dpi > 460:
            factor = 400 / native_dpi
            img = img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)
            native_dpi = 400
        # Page cropped smaller than the image? fall back to rendering.
        if abs(img.width / img.height - w_pt / h_pt) > 0.03:
            return None, dpi
        return img, native_dpi
    except Exception as exc:  # pragma: no cover - unusual PDFs
        log.debug("embedded image extraction failed: %s", exc)
        return None, dpi


def render_thumbnail(img: Image.Image, max_side: int = 360) -> Image.Image:
    thumb = img.copy()
    thumb.thumbnail((max_side, max_side), Image.LANCZOS)
    return thumb
