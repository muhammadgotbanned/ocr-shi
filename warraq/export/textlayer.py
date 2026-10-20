"""Invisible, selectable Arabic text for searchable PDFs.

The text is drawn with a "glyph-less" CID font whose codes are Unicode code
points (Identity-H + identity ToUnicode), so copy/paste returns exactly the
characters that were recognised — no presentation forms, no ligature
mangling.  Arabic lines are written in *visual* order (as a real typesetter
would place glyphs), which is what PDF viewers reverse back into logical
order when copying.  Spaces are narrow and combining marks have zero width,
mimicking a real Arabic font so viewers do not split words.

Viewers disagree about bracket mirroring when reversing right-to-left text;
``flavour`` selects the variant that suits a given viewer family.
"""

from __future__ import annotations

import math
import unicodedata
import zlib
from functools import lru_cache
from typing import Iterable

from ..paths import ASSETS_DIR

DW = 500  # default glyph width (1/1000 em)
SPACE_W = 250

_MIRROR = str.maketrans("()[]{}«»<>﴾﴿", ")(][}{»«><﴿﴾")


@lru_cache(maxsize=1)
def font_program() -> bytes:
    return (ASSETS_DIR / "glyphless.ttf").read_bytes()


TOUNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


@lru_cache(maxsize=1)
def cid_to_gid_map() -> bytes:
    return zlib.compress(b"\x00\x01" * 65536, 9)


@lru_cache(maxsize=1)
def zero_width_ranges() -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = prev = None
    for cp in range(0x0300, 0x0900):
        if unicodedata.category(chr(cp)) == "Mn":
            if start is None:
                start = prev = cp
            elif cp == prev + 1:
                prev = cp
            else:
                ranges.append((start, prev))
                start = prev = cp
    if start is not None:
        ranges.append((start, prev))
    return ranges


def widths_array() -> str:
    parts = [f"32 [{SPACE_W}]"]
    parts += [f"{a} {b} 0" for a, b in zero_width_ranges()]
    return "[" + " ".join(parts) + "]"


def char_width(ch: str) -> int:
    if ch == " ":
        return SPACE_W
    if unicodedata.category(ch) == "Mn":
        return 0
    return DW


def natural_width(text: str, font_size: float) -> float:
    return sum(char_width(c) for c in text) / 1000.0 * font_size


def is_rtl(text: str) -> bool:
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b in ("R", "AL"):
            return True
        if b == "L":
            return False
    return False


def visual_order(text: str, flavour: str = "standard") -> str:
    """Logical -> visual order for one line (Unicode bidi algorithm)."""
    if not any(unicodedata.bidirectional(c) in ("R", "AL") for c in text):
        return text
    try:
        from bidi import get_display

        vis = get_display(text, base_dir="R" if is_rtl(text) else "L")
    except Exception:  # pragma: no cover - python-bidi missing
        vis = _simple_visual(text)
        if flavour != "chromium":
            vis = vis.translate(_MIRROR)
        return vis
    if flavour == "chromium":
        vis = vis.translate(_MIRROR)  # undo the mirroring python-bidi applied
    return vis


def _simple_visual(text: str) -> str:
    """Fallback bidi for RTL lines: reverse, keeping numbers/Latin runs LTR."""
    runs: list[tuple[bool, str]] = []
    for ch in text:
        b = unicodedata.bidirectional(ch)
        ltr = b in ("L", "EN", "AN") or (b in ("ES", "CS", "ET") and runs and runs[-1][0])
        if runs and runs[-1][0] == ltr:
            runs[-1] = (ltr, runs[-1][1] + ch)
        else:
            runs.append((ltr, ch))
    return "".join(s if ltr else s[::-1] for ltr, s in reversed(runs))


def hex_utf16(text: str) -> str:
    return "<" + text.encode("utf-16-be").hex().upper() + ">"


def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def line_ops(text: str, quad_disp: list[tuple[float, float]], disp_to_user: list[float], flavour: str, font_name: str) -> list[str]:
    """PDF operators for one line.  ``quad_disp`` holds the line corners in display
    points with a top-left origin: top-left, top-right, bottom-right, bottom-left."""
    from ..core import geometry as G

    text = text.strip()
    if not text or not quad_disp or len(quad_disp) != 4:
        return []
    p0, p1, p2, p3 = quad_disp
    bx, by = p2[0] - p3[0], p2[1] - p3[1]
    length = math.hypot(bx, by)
    ux, uy = p0[0] - p3[0], p0[1] - p3[1]
    height = math.hypot(ux, uy)
    if length < 1 or height < 1:
        return []
    u = (bx / length, by / length)
    v = (ux / height, uy / height)
    fs = max(1.0, height * 0.82)
    origin = (p3[0] + v[0] * height * 0.16, p3[1] + v[1] * height * 0.16)
    d = [u[0], u[1], v[0], v[1], origin[0], origin[1]]
    tm = G.multiply(d, disp_to_user)
    ops: list[str] = []
    if flavour == "word" and is_rtl(text):
        ops.extend(_word_ops(text, length, fs, tm, font_name))
        return ops
    vis = visual_order(text, flavour)
    nat = natural_width(vis, fs)
    if nat <= 0:
        return []
    tz = 100.0 * length / nat
    ops.append(f"BT 3 Tr /{font_name} {_fmt(fs)} Tf {_fmt(tz)} Tz {' '.join(_fmt(x) for x in tm)} Tm {hex_utf16(vis)} Tj ET")
    return ops


def _word_ops(text: str, length: float, fs: float, tm: list[float], font_name: str) -> list[str]:
    """OCRmyPDF-style: every word placed separately, words in reading order."""
    words = text.split()
    total = sum(max(1, len(w)) for w in words) + 0.5 * (len(words) - 1)
    unit = length / max(1.0, total)
    x = length
    ops = [f"BT 3 Tr /{font_name} {_fmt(fs)} Tf {' '.join(_fmt(v) for v in tm)} Tm"]
    cursor = 0.0
    for w in words:
        wl = max(1, len(w)) * unit
        start = x - wl
        vis = (w[::-1] if is_rtl(w) else w) + " "
        nat = natural_width(vis, fs)
        tz = 100.0 * (wl + unit * 0.5) / nat if nat > 0 else 100.0
        ops.append(f"{_fmt(start - cursor)} 0 Td {_fmt(tz)} Tz {hex_utf16(vis)} Tj")
        cursor = start
        x = start - unit * 0.5
    ops.append("ET")
    return ops


def box_ops(quads: Iterable[list[tuple[float, float]]], disp_to_user: list[float]) -> list[str]:
    """Outline line boxes (debug view of the text layer)."""
    from ..core import geometry as G

    ops = ["q 0.9 0.2 0.3 RG 0.6 w"]
    for quad in quads:
        pts = [G.apply(disp_to_user, x, y) for x, y in quad]
        ops.append(f"{_fmt(pts[0][0])} {_fmt(pts[0][1])} m " + " ".join(f"{_fmt(x)} {_fmt(y)} l" for x, y in pts[1:]) + " h S")
    ops.append("Q")
    return ops
