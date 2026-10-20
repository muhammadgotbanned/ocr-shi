"""2-D affine helpers.  Matrices use the PDF convention ``[a, b, c, d, e, f]``:

    x' = a*x + c*y + e
    y' = b*x + d*y + f
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Matrix = list[float]
IDENTITY: Matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def multiply(m1: Sequence[float], m2: Sequence[float]) -> Matrix:
    """Return the matrix that applies ``m1`` first, then ``m2``."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    ]


def invert(m: Sequence[float]) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise ValueError("singular matrix")
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return [ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_)]


def translate(tx: float, ty: float) -> Matrix:
    return [1.0, 0.0, 0.0, 1.0, float(tx), float(ty)]


def scale(sx: float, sy: float | None = None) -> Matrix:
    return [float(sx), 0.0, 0.0, float(sy if sy is not None else sx), 0.0, 0.0]


def rotate_about(angle_ccw_deg: float, cx: float, cy: float) -> Matrix:
    """Rotation by ``angle`` degrees counter-clockwise *as seen on screen*
    (image coordinates with y pointing down) around (cx, cy)."""
    t = math.radians(angle_ccw_deg)
    cos, sin = math.cos(t), math.sin(t)
    # In y-down coordinates a visual CCW rotation is x' = cos*x + sin*y, y' = -sin*x + cos*y.
    r = [cos, -sin, sin, cos, 0.0, 0.0]
    return multiply(multiply(translate(-cx, -cy), r), translate(cx, cy))


def apply(m: Sequence[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def apply_box(m: Sequence[float], box: Sequence[float]) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = box
    return [apply(m, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def bounds(points: Iterable[tuple[float, float]]) -> tuple[float, float, float, float]:
    pts = list(points)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def box_iou(a: Sequence[float], b: Sequence[float]) -> float:
    inter = intersection_area(a, b)
    if inter <= 0:
        return 0.0
    return inter / (area(a) + area(b) - inter)


def area(b: Sequence[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def intersection_area(a: Sequence[float], b: Sequence[float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def containment(inner: Sequence[float], outer: Sequence[float]) -> float:
    """Fraction of ``inner``'s area that lies inside ``outer``."""
    a = area(inner)
    return intersection_area(inner, outer) / a if a > 0 else 0.0


def union(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float, float]:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def h_overlap(a: Sequence[float], b: Sequence[float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def v_overlap(a: Sequence[float], b: Sequence[float]) -> float:
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def clamp_box(b: Sequence[float], w: float, h: float) -> tuple[float, float, float, float]:
    return max(0.0, b[0]), max(0.0, b[1]), min(float(w), b[2]), min(float(h), b[3])
