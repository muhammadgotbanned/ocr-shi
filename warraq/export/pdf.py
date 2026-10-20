"""Searchable PDF export.

* PDF input  → the original pages are kept untouched and an invisible text
  layer is added on top (two-page spreads become two cropped pages).
* Image input → each page image is embedded (JPEG files pass through without
  re-compression) with the text layer on top.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    FloatObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

from .. import __version__
from ..core import geometry as G
from ..core.loader import PDF_EXTS
from . import textlayer as TL

log = logging.getLogger(__name__)
FONT_NAME = "WarraqGL"


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #


def _stream(writer: PdfWriter, data: bytes, extra: dict | None = None, compress: bool = True):
    s = DecodedStreamObject()
    s.set_data(data)
    if compress:
        s = s.flate_encode()
    for k, v in (extra or {}).items():
        s[NameObject(k)] = v
    return writer._add_object(s)


def add_glyphless_font(writer: PdfWriter):
    ttf = TL.font_program()
    font_file = _stream(writer, ttf, {"/Length1": NumberObject(len(ttf))})
    c2g = EncodedStreamObject()
    c2g._data = TL.cid_to_gid_map()
    c2g[NameObject("/Filter")] = NameObject("/FlateDecode")
    c2g_ref = writer._add_object(c2g)
    to_unicode = _stream(writer, TL.TOUNICODE)
    descriptor = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontName"): NameObject("/GlyphLessFont"),
            NameObject("/Flags"): NumberObject(5),
            NameObject("/FontBBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(TL.DW), NumberObject(1000)]),
            NameObject("/ItalicAngle"): NumberObject(0),
            NameObject("/Ascent"): NumberObject(1000),
            NameObject("/Descent"): NumberObject(-1),
            NameObject("/CapHeight"): NumberObject(1000),
            NameObject("/StemV"): NumberObject(80),
            NameObject("/FontFile2"): font_file,
        }
    )
    descriptor_ref = writer._add_object(descriptor)
    widths = _parse_widths(TL.widths_array())
    cid_font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType2"),
            NameObject("/BaseFont"): NameObject("/GlyphLessFont"),
            NameObject("/CIDSystemInfo"): DictionaryObject(
                {
                    NameObject("/Registry"): TextStringObject("Adobe"),
                    NameObject("/Ordering"): TextStringObject("Identity"),
                    NameObject("/Supplement"): NumberObject(0),
                }
            ),
            NameObject("/FontDescriptor"): descriptor_ref,
            NameObject("/DW"): NumberObject(TL.DW),
            NameObject("/W"): widths,
            NameObject("/CIDToGIDMap"): c2g_ref,
        }
    )
    cid_ref = writer._add_object(cid_font)
    type0 = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/GlyphLessFont"),
            NameObject("/Encoding"): NameObject("/Identity-H"),
            NameObject("/DescendantFonts"): ArrayObject([cid_ref]),
            NameObject("/ToUnicode"): to_unicode,
        }
    )
    return writer._add_object(type0)


def _parse_widths(spec: str) -> ArrayObject:
    tokens = spec.replace("[", " [ ").replace("]", " ] ").split()[1:-1]
    out = ArrayObject()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "[":
            inner = ArrayObject()
            i += 1
            while tokens[i] != "]":
                inner.append(NumberObject(int(tokens[i])))
                i += 1
            out.append(inner)
        else:
            out.append(NumberObject(int(tok)))
        i += 1
    return out


def disp_to_user_matrix(cropbox, rotation: int) -> list[float]:
    """Map display coordinates (points, top-left origin, after /Rotate) to PDF user space."""
    x0, y0, x1, y1 = cropbox
    r = rotation % 360
    if r == 90:
        return [0.0, 1.0, 1.0, 0.0, x0, y0]
    if r == 180:
        return [-1.0, 0.0, 0.0, 1.0, x1, y0]
    if r == 270:
        return [0.0, -1.0, -1.0, 0.0, x1, y1]
    return [1.0, 0.0, 0.0, -1.0, x0, y1]


def _page_quads(page, px_to_disp: list[float]) -> list[tuple[str, list[tuple[float, float]]]]:
    """All text lines of a page as (text, quad in display points), in reading order."""
    out = []
    to_disp = G.multiply(list(page.transform), px_to_disp)
    for r in page.regions:
        for line in r.lines:
            quad = line.quad
            if not quad:
                x0, y0, x1, y1 = line.bbox
                quad = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            out.append((line.text, [G.apply(to_disp, x, y) for x, y in quad]))
    return out


def _fix_digits(text: str, style: str | None) -> str:
    from ..core import text as T

    if style == "arabic_indic":
        return T.to_arabic_digits(text)
    if style == "western":
        return T.to_western_digits(text)
    return text


def _content_for_page(page, px_to_disp, disp_to_user, settings, style) -> bytes:
    flavour = settings.output.pdf_text_order
    ops: list[str] = []
    quads = _page_quads(page, px_to_disp)
    for text, quad in quads:
        ops.extend(TL.line_ops(_fix_digits(text, style), quad, disp_to_user, flavour, FONT_NAME))
    if settings.output.pdf_visible_text:
        ops.extend(TL.box_ops([q for _, q in quads], disp_to_user))
    return ("\n".join(ops) + "\n").encode("ascii")


def _add_font_resource(page, font_ref) -> None:
    res = page.get("/Resources")
    res = res.get_object() if res is not None else None
    if res is None:
        inherited = page.get_inherited("/Resources", None) if hasattr(page, "get_inherited") else None
        res = DictionaryObject(inherited.get_object()) if inherited is not None else DictionaryObject()
        page[NameObject("/Resources")] = res
    fonts = res.get("/Font")
    fonts = fonts.get_object() if fonts is not None else None
    if fonts is None:
        fonts = DictionaryObject()
        res[NameObject("/Font")] = fonts
    fonts[NameObject("/" + FONT_NAME)] = font_ref


def _append_content(writer: PdfWriter, page, data: bytes) -> None:
    existing = page.get("/Contents")
    items: list = []
    if existing is not None:
        obj = existing.get_object()
        if isinstance(obj, ArrayObject):
            items = list(obj)
        else:
            items = [existing]
    q = _stream(writer, b"q\n", compress=False)
    end = _stream(writer, b"\nQ\n", compress=False)
    ours = _stream(writer, data)
    page[NameObject("/Contents")] = ArrayObject([q] + items + [end, ours])


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #


def write(path: Path, doc, views, settings, job_dir: Path) -> Path:
    src = Path(doc.source_path)
    style = doc.meta.get("digit_style") if settings.text.digits == "auto" else None
    writer = PdfWriter()
    font_ref = add_glyphless_font(writer)
    if src.suffix.lower() in PDF_EXTS and settings.output.pdf_keep_original and src.exists():
        _write_over_original(writer, src, doc, settings, font_ref, style)
    else:
        _write_from_images(writer, src, doc, settings, font_ref, job_dir, style)
    writer.add_metadata({"/Title": Path(doc.name).stem, "/Producer": f"Warraq {__version__}", "/Creator": "Warraq OCR"})
    try:
        writer._root_object[NameObject("/Lang")] = TextStringObject("ar")
    except Exception:  # pragma: no cover
        pass
    tmp = Path(path).with_suffix(".part")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    tmp.replace(path)
    return Path(path)


def strip_invisible_text(page, pdf) -> bool:
    """Drop text drawn in the invisible render mode 3 (neither fill nor stroke) from a
    page's content stream — i.e. an old OCR layer.  Returns True if changed."""
    from pypdf.generic import ContentStream

    contents = page.get_contents()
    if contents is None:
        return False
    try:
        raw = contents.get_data()
    except Exception:
        return False
    if b"Tr" not in raw:
        return False
    stream = ContentStream(contents, pdf)
    mode = 0
    stack: list[int] = []
    kept = []
    dropped = 0
    for operands, op in stream.operations:
        if op == b"q":
            stack.append(mode)
        elif op == b"Q":
            mode = stack.pop() if stack else 0
        elif op == b"Tr" and operands:
            try:
                mode = int(operands[0])
            except Exception:
                pass
        elif op in (b"Tj", b"TJ", b"'", b'"') and mode == 3:
            dropped += 1
            continue
        kept.append((operands, op))
    if not dropped:
        return False
    stream.operations = kept
    page.replace_contents(stream)
    return True


def _write_over_original(writer: PdfWriter, src: Path, doc, settings, font_ref, style) -> None:
    reader = PdfReader(str(src))
    for page in doc.pages:
        if page.source_index >= len(reader.pages):
            continue
        writer.add_page(reader.pages[page.source_index])
        out_page = writer.pages[-1]
        if settings.output.pdf_replace_ocr_layer:
            try:
                strip_invisible_text(out_page, writer)
            except Exception as exc:  # pragma: no cover - unusual content streams
                log.debug("could not strip old text layer: %s", exc)
        cb = out_page.cropbox
        box = (float(cb.left), float(cb.bottom), float(cb.right), float(cb.top))
        rotation = int(out_page.get("/Rotate", 0) or 0) % 360
        disp_w = box[2] - box[0]
        disp_h = box[3] - box[1]
        if rotation in (90, 270):
            disp_w, disp_h = disp_h, disp_w
        ow, oh = page.original_size if page.original_size and page.original_size[0] else (page.width, page.height)
        px_to_disp = G.scale(disp_w / ow, disp_h / oh)
        disp_to_user = disp_to_user_matrix(box, rotation)
        if page.split_part is not None:
            # crop the page to this half of the spread
            corners = G.apply_box(G.multiply(G.multiply(list(page.transform), px_to_disp), disp_to_user), (0, 0, page.width, page.height))
            ux0, uy0, ux1, uy1 = G.bounds(corners)
            rect = RectangleObject([FloatObject(ux0), FloatObject(uy0), FloatObject(ux1), FloatObject(uy1)])
            out_page.cropbox = rect
            out_page.trimbox = rect
        data = _content_for_page(page, px_to_disp, disp_to_user, settings, style)
        _add_font_resource(out_page, font_ref)
        _append_content(writer, out_page, data)


def _write_from_images(writer: PdfWriter, src: Path, doc, settings, font_ref, job_dir: Path, style) -> None:
    out = settings.output
    use_original = out.pdf_keep_original and src.exists()
    for page in doc.pages:
        image_bytes, pil, transform = None, None, list(page.transform)
        if use_original and src.suffix.lower() not in PDF_EXTS:
            image_bytes, pil = _source_image(src, page.source_index)
        if pil is None:
            pil = Image.open(Path(job_dir) / "pages" / page.image_path)
            pil.load()
            image_bytes = None
            transform = list(G.IDENTITY)  # processed image == embedded image
        dpi = _dpi(page, pil)
        if out.pdf_max_dpi and dpi > out.pdf_max_dpi:
            factor = out.pdf_max_dpi / dpi
            pil = pil.resize((max(1, int(pil.width * factor)), max(1, int(pil.height * factor))), Image.LANCZOS)
            image_bytes = None
            transform = G.multiply(transform, G.scale(factor))
            dpi = out.pdf_max_dpi
        w_pt = pil.width * 72.0 / dpi
        h_pt = pil.height * 72.0 / dpi
        new_page = writer.add_blank_page(width=w_pt, height=h_pt)
        xobj = _image_xobject(writer, pil, image_bytes, out.pdf_image_quality)
        new_page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): xobj}),
                NameObject("/Font"): DictionaryObject({NameObject("/" + FONT_NAME): font_ref}),
            }
        )
        px_to_disp = G.scale(72.0 / dpi)
        disp_to_user = disp_to_user_matrix((0.0, 0.0, w_pt, h_pt), 0)
        # region coordinates are in processed px; map to the embedded image's px first
        page_for_text = _with_transform(page, transform)
        text = _content_for_page(page_for_text, px_to_disp, disp_to_user, settings, style)
        draw = f"q {TL._fmt(w_pt)} 0 0 {TL._fmt(h_pt)} 0 0 cm /Im0 Do Q\n".encode("ascii")
        new_page[NameObject("/Contents")] = _stream(writer, draw + text)


class _TransformedPage:
    def __init__(self, page, transform):
        self.regions = page.regions
        self.transform = transform


def _with_transform(page, transform):
    return _TransformedPage(page, transform)


def _dpi(page, pil: Image.Image) -> float:
    dpi = pil.info.get("dpi")
    try:
        if dpi and float(dpi[0]) > 20:
            return float(dpi[0])
    except Exception:
        pass
    if page.source_size_pt:
        return pil.width / (page.source_size_pt[0] / 72.0)
    return 300.0


def _source_image(src: Path, index: int):
    """Original image bytes for pass-through (JPEG) plus a PIL image."""
    from PIL import ImageOps, ImageSequence

    try:
        im = Image.open(src)
        if getattr(im, "n_frames", 1) > 1:
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i == index:
                    f = frame.copy()
                    f.info["dpi"] = im.info.get("dpi")
                    return None, f
            return None, None
        exif_rotated = ImageOps.exif_transpose(im)
        if im.format == "JPEG" and exif_rotated.size == im.size and im.mode in ("RGB", "L", "CMYK"):
            return src.read_bytes(), im
        exif_rotated.info["dpi"] = im.info.get("dpi")
        return None, exif_rotated
    except Exception:  # pragma: no cover
        return None, None


def _image_xobject(writer: PdfWriter, pil: Image.Image, jpeg: bytes | None, quality: int):
    if pil.mode == "1":
        data = pil.tobytes()
        xo = EncodedStreamObject()
        import zlib

        xo._data = zlib.compress(data, 9)
        xo[NameObject("/Filter")] = NameObject("/FlateDecode")
        xo[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
        xo[NameObject("/BitsPerComponent")] = NumberObject(1)
        xo[NameObject("/Decode")] = ArrayObject([NumberObject(0), NumberObject(1)])
    else:
        mode = pil.mode
        if jpeg is None:
            if mode not in ("RGB", "L"):
                pil = pil.convert("RGB")
                mode = "RGB"
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=int(quality), optimize=True)
            jpeg = buf.getvalue()
        xo = EncodedStreamObject()
        xo._data = jpeg
        xo[NameObject("/Filter")] = NameObject("/DCTDecode")
        xo[NameObject("/ColorSpace")] = NameObject({"L": "/DeviceGray", "CMYK": "/DeviceCMYK"}.get(mode, "/DeviceRGB"))
        xo[NameObject("/BitsPerComponent")] = NumberObject(8)
        if mode == "CMYK":
            xo[NameObject("/Decode")] = ArrayObject([NumberObject(v) for v in (1, 0, 1, 0, 1, 0, 1, 0)])
    xo[NameObject("/Type")] = NameObject("/XObject")
    xo[NameObject("/Subtype")] = NameObject("/Image")
    xo[NameObject("/Width")] = NumberObject(pil.width)
    xo[NameObject("/Height")] = NumberObject(pil.height)
    return writer._add_object(xo)
