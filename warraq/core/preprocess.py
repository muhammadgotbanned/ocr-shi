"""Page preprocessing: rotation, border removal, spread splitting, deskew, enhancement.

Each step that changes geometry updates an affine transform mapping processed
pixels back to the original page image, so the searchable PDF can place text
on the untouched original.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from . import geometry as G
from . import imaging


@dataclass
class ProcessedPage:
    image: Image.Image
    transform: list[float]  # processed px -> original px
    split_part: int | None = None
    skew: float = 0.0
    notes: list[str] = field(default_factory=list)


def preprocess(img: Image.Image, settings) -> list[ProcessedPage]:
    inp = settings.input
    img = img.convert("RGB")
    ow, oh = img.size
    transform = list(G.IDENTITY)
    notes: list[str] = []

    # 1. manual rotation (PIL rotates CCW; the setting is clockwise degrees)
    rot = int(inp.rotate or 0) % 360
    if rot:
        img = img.rotate(-rot, expand=True)
        transform = _rotation_transform(rot, ow, oh)
        notes.append(f"rotated {rot}°")

    # 2. dark border removal
    if inp.crop_borders:
        gray = imaging.to_gray(img)
        x0, y0, x1, y1 = imaging.find_content_box(gray)
        w, h = img.size
        if (x1 - x0) < w * 0.985 or (y1 - y0) < h * 0.985:
            if (x1 - x0) > w * 0.5 and (y1 - y0) > h * 0.5:
                img = img.crop((x0, y0, x1, y1))
                transform = G.multiply(G.translate(x0, y0), transform)
                notes.append("cropped borders")

    # 3. two-page spread
    parts: list[tuple[Image.Image, list[float], int | None]] = [(img, transform, None)]
    if inp.split_spreads != "off":
        gray = imaging.to_gray(img)
        gx = imaging.find_spread_split(gray, force=inp.split_spreads == "always")
        if gx:
            w, h = img.size
            right = (img.crop((gx, 0, w, h)), G.multiply(G.translate(gx, 0), transform))
            left = (img.crop((0, 0, gx, h)), list(transform))
            ordered = [right, left] if inp.spread_order == "rtl" else [left, right]
            parts = [(im, t, i) for i, (im, t) in enumerate(ordered)]
            notes.append("split two-page spread")

    out: list[ProcessedPage] = []
    for im, t, part in parts:
        skew = 0.0
        if inp.deskew:
            skew = imaging.estimate_skew(imaging.to_gray(im), inp.max_skew_angle)
            if skew:
                w, h = im.size
                im = imaging.rotate_image(im, skew, expand=False)
                # processed -> pre-rotation: rotate back by -skew about the centre
                t = G.multiply(G.rotate_about(-skew, w / 2, h / 2), t)
        im = imaging.enhance(im, inp.enhance, inp.denoise, inp.binarize)
        page_notes = list(notes)
        if skew:
            page_notes.append(f"deskewed {skew:+.1f}°")
        out.append(ProcessedPage(im, t, part, skew, page_notes))
    return out


def _rotation_transform(rot_cw: int, ow: int, oh: int) -> list[float]:
    """Transform from the clockwise-rotated image back to the original."""
    if rot_cw == 90:
        # rotated (x, y) -> original (y, oh - x)... derived for PIL rotate(-90, expand=True)
        return [0.0, -1.0, 1.0, 0.0, 0.0, float(oh)]
    if rot_cw == 180:
        return [-1.0, 0.0, 0.0, -1.0, float(ow), float(oh)]
    if rot_cw == 270:
        return [0.0, 1.0, -1.0, 0.0, float(ow), 0.0]
    return list(G.IDENTITY)
