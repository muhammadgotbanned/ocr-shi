"""Image analysis and enhancement built on OpenCV/numpy.

Everything here is model-free: binarisation, deskew, border removal,
illumination correction, two-page spread detection, text-line segmentation,
rotation estimation for marginal notes and the visual "=" detector used for
continued footnotes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------- #
# Basic conversions
# --------------------------------------------------------------------------- #


def to_gray(img: Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(img, Image.Image):
        if img.mode != "L":
            img = img.convert("L")
        return np.asarray(img)
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    return img


def ink_mask(gray: np.ndarray, block: int | None = None) -> np.ndarray:
    """Binary mask (uint8 0/1) of dark ink on light paper, robust to uneven light."""
    h, w = gray.shape[:2]
    if block is None:
        block = max(15, (min(h, w) // 24) | 1)
    block |= 1
    # Otsu on a background-normalised image is more stable than raw adaptive thresholding.
    bg = cv2.medianBlur(gray, min(block, 255) if min(block, 255) % 2 else min(block, 255) - 1) if min(h, w) > 30 else gray
    norm = cv2.divide(gray, np.maximum(bg, 1), scale=255)
    _, otsu = cv2.threshold(norm, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Guard against blank crops where Otsu splits noise.
    if otsu.mean() > 0.6:
        otsu = (norm < 160).astype(np.uint8)
    return otsu.astype(np.uint8)


def remove_specks(mask: np.ndarray, min_area: int) -> np.ndarray:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return mask
    keep = np.zeros(n, dtype=np.uint8)
    keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= min_area).astype(np.uint8)
    return keep[labels]


# --------------------------------------------------------------------------- #
# Page-level preprocessing
# --------------------------------------------------------------------------- #


def estimate_skew(gray: np.ndarray, max_angle: float = 8.0) -> float:
    """Skew angle in degrees (positive = text rises to the right), via projection profiles."""
    h, w = gray.shape
    scale = 1200 / max(h, w) if max(h, w) > 1200 else 1.0
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale != 1 else gray
    mask = ink_mask(small)
    if mask.sum() < 200:
        return 0.0
    # Blur letters horizontally into line blobs so the profile is sharp.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, small.shape[1] // 60), 1))
    blobs = cv2.dilate(mask, kernel)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D((blobs.shape[1] / 2, blobs.shape[0] / 2), angle, 1.0)
        rot = cv2.warpAffine(blobs, m, (blobs.shape[1], blobs.shape[0]), flags=cv2.INTER_NEAREST, borderValue=0)
        prof = rot.sum(axis=1).astype(np.float64)
        return float(np.var(np.diff(prof)))

    best, best_s = 0.0, score(0.0)
    step = 0.5
    a = -max_angle
    while a <= max_angle + 1e-9:
        s = score(a)
        if s > best_s:
            best, best_s = a, s
        a += step
    for a in np.arange(best - 0.5, best + 0.55, 0.1):
        s = score(float(a))
        if s > best_s:
            best, best_s = float(a), s
    if abs(best) < 0.15:
        return 0.0
    return round(best, 2)


def rotate_image(img: Image.Image, angle: float, expand: bool = False, fill=(255, 255, 255)) -> Image.Image:
    """Rotate counter-clockwise by ``angle`` degrees with a white background."""
    if abs(angle) < 1e-6:
        return img
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    fill_value = fill if img.mode == "RGB" else 255
    return img.rotate(angle, resample=Image.BICUBIC, expand=expand, fillcolor=fill_value)


def find_content_box(gray: np.ndarray) -> tuple[int, int, int, int]:
    """Bounding box that excludes dark scanner borders / black edges."""
    h, w = gray.shape
    scale = 800 / max(h, w) if max(h, w) > 800 else 1.0
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale != 1 else gray
    sh, sw = small.shape
    dark = (small < 70).astype(np.uint8)

    def trim(profile: np.ndarray, length: int) -> tuple[int, int]:
        start, end = 0, length
        limit = int(length * 0.2)
        while start < limit and profile[start] > 0.55:
            start += 1
        while end > length - limit and profile[end - 1] > 0.55:
            end -= 1
        return start, end

    rows = dark.mean(axis=1)
    cols = dark.mean(axis=0)
    y0, y1 = trim(rows, sh)
    x0, x1 = trim(cols, sw)
    inv = 1.0 / scale
    pad = 2
    return (
        max(0, int(x0 * inv) - pad),
        max(0, int(y0 * inv) - pad),
        min(w, int(math.ceil(x1 * inv)) + pad),
        min(h, int(math.ceil(y1 * inv)) + pad),
    )


def enhance(img: Image.Image, level: str = "auto", denoise: bool = False, binarize: bool = False) -> Image.Image:
    """Even out paper colour and lighting while keeping coloured inks."""
    if level == "none" and not denoise and not binarize:
        return img
    arr = np.asarray(img.convert("RGB"))
    out = arr
    if level in ("auto", "strong"):
        h, w = arr.shape[:2]
        k = max(31, (min(h, w) // 18) | 1)
        small_scale = 0.25
        small = cv2.resize(arr, (max(1, int(w * small_scale)), max(1, int(h * small_scale))), interpolation=cv2.INTER_AREA)
        ks = max(3, int(k * small_scale) | 1)
        bg_small = cv2.morphologyEx(small, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks)))
        bg_small = cv2.GaussianBlur(bg_small, (ks, ks), 0)
        bg = cv2.resize(bg_small, (w, h), interpolation=cv2.INTER_LINEAR)
        norm = cv2.divide(arr, np.maximum(bg, 1), scale=255)
        # Gentle contrast stretch
        lo, hi = np.percentile(norm, (0.5, 99.5))
        if hi - lo > 20:
            norm = np.clip((norm.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        out = whiten_paper(norm)
        if level == "strong":
            lab = cv2.cvtColor(out, cv2.COLOR_RGB2LAB)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            out = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
            blur = cv2.GaussianBlur(out, (0, 0), 1.2)
            out = cv2.addWeighted(out, 1.35, blur, -0.35, 0)
    if denoise:
        out = cv2.fastNlMeansDenoisingColored(out, None, 6, 6, 7, 21)
    if binarize:
        gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
        bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 15)
        out = cv2.cvtColor(bw, cv2.COLOR_GRAY2RGB)
    return Image.fromarray(out)


def whiten_paper(arr: np.ndarray) -> np.ndarray:
    """Turn the paper itself pure white (its grain and scanner noise included) while
    leaving every ink tone exactly as it was, faint strokes and coloured rubrics too.
    The reader then sees a clean page, and white padding around a line forms no edge."""
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if arr.ndim == 3 else arr
    paper = float(np.median(gray))
    above = gray[gray >= paper]
    if above.size < 100 or paper < 140:
        return arr  # mostly ink, or a dark page: leave it alone
    noise = float(np.median(np.abs(above.astype(np.float32) - paper))) * 1.4826 + 1.0  # robust spread of the paper
    cut = paper - 3.0 * noise  # lighter than this is paper
    ramp = max(4.0, 2.0 * noise)  # values just below the paper fade to white, so strokes keep soft edges
    if cut - ramp < 60:
        return arr
    levels = np.arange(256, dtype=np.float32)
    lut = levels.copy()
    fade = (levels > cut - ramp) & (levels < cut)
    lut[fade] = (cut - ramp) + (levels[fade] - (cut - ramp)) * (255.0 - (cut - ramp)) / ramp
    lut[levels >= cut] = 255.0
    lut = np.clip(lut, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        return lut[arr]
    # scale every channel by the same per-pixel factor so coloured ink keeps its hue
    g = np.maximum(gray.astype(np.float32), 1.0)
    factor = lut[gray].astype(np.float32) / g
    out = np.clip(arr.astype(np.float32) * factor[..., None], 0, 255)
    out[lut[gray] >= 255] = 255
    return out.astype(np.uint8)


def find_spread_split(gray: np.ndarray, force: bool = False) -> int | None:
    """Return the x coordinate of the gutter if the image is a two-page spread."""
    h, w = gray.shape
    if not force and w < h * 1.15:
        return None
    scale = 1000 / w if w > 1000 else 1.0
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale != 1 else gray
    mask = ink_mask(small)
    sh, sw = mask.shape
    # Ignore the outer 4% (borders) and use the central band of rows.
    band = mask[int(sh * 0.08) : int(sh * 0.92), :]
    density = band.mean(axis=0).astype(np.float64)
    density = np.convolve(density, np.ones(9) / 9, mode="same")
    lo, hi = int(sw * 0.35), int(sw * 0.65)
    centre = density[lo:hi]
    if centre.size == 0:
        return None
    left = density[int(sw * 0.1) : int(sw * 0.4)].mean()
    right = density[int(sw * 0.6) : int(sw * 0.9)].mean()
    pages_have_text = left > 0.01 and right > 0.01
    # Centre of the widest near-empty run of columns in the middle band.
    floor = centre.min()
    quiet = centre <= max(floor * 1.5, 0.25 * min(left, right) if pages_have_text else floor)
    best_len, best_mid, run_start = 0, int(np.argmin(centre)), None
    for i, q in enumerate(list(quiet) + [False]):
        if q and run_start is None:
            run_start = i
        elif not q and run_start is not None:
            if i - run_start > best_len:
                best_len, best_mid = i - run_start, (run_start + i - 1) // 2
            run_start = None
    gx = best_mid + lo
    if force or (pages_have_text and density[gx] < 0.25 * min(left, right)):
        return int(gx / scale)
    # Dark gutter shadow: look for the darkest column instead.
    col_mean = small[int(sh * 0.08) : int(sh * 0.92), lo:hi].mean(axis=0)
    dx = int(np.argmin(col_mean)) + lo
    if pages_have_text and col_mean.min() < 0.55 * np.median(small):
        return int(dx / scale)
    return None


# --------------------------------------------------------------------------- #
# Lines inside a region
# --------------------------------------------------------------------------- #


@dataclass
class LineBand:
    top: int
    bottom: int
    left: int
    right: int

    @property
    def height(self) -> int:
        return self.bottom - self.top


def segment_lines(gray: np.ndarray, mask: np.ndarray | None = None) -> list[LineBand]:
    """Split a (roughly horizontal) text block into line bands using valleys of the
    horizontal projection profile.  Diacritics and dots are kept with their line."""
    if mask is None:
        mask = ink_mask(gray)
    h, w = mask.shape
    if h < 6 or w < 6 or mask.sum() == 0:
        return []
    profile = mask.sum(axis=1).astype(np.float64)
    # Estimate stroke height to size the smoothing window.
    ink_rows = np.where(profile > 0)[0]
    if ink_rows.size == 0:
        return []
    est = max(3, int(_estimate_line_pitch(profile) / 6))
    smooth = np.convolve(profile, np.ones(est) / est, mode="same")
    peak = smooth.max()
    if peak <= 0:
        return []
    pitch = _estimate_line_pitch(smooth)
    # Cut at valleys: local minima well below the neighbouring peaks.
    cuts = [0]
    min_gap = max(4, int(pitch * 0.45))
    valleys = _find_valleys(smooth, pitch)
    for v in valleys:
        if v - cuts[-1] >= min_gap:
            cuts.append(v)
    if h - cuts[-1] < min_gap and len(cuts) > 1:
        cuts[-1] = h
    else:
        cuts.append(h)
    bands: list[LineBand] = []
    for top, bottom in zip(cuts[:-1], cuts[1:]):
        sub = mask[top:bottom]
        if sub.sum() < max(8, 0.002 * w * (bottom - top)):
            continue
        cols = np.where(sub.any(axis=0))[0]
        rows = np.where(sub.any(axis=1))[0]
        bands.append(LineBand(top + int(rows[0]), top + int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1))
    # Merge bands that are too thin to be lines (stray diacritic rows).
    merged: list[LineBand] = []
    for band in bands:
        if merged and band.height < pitch * 0.33:
            prev = merged[-1]
            merged[-1] = LineBand(prev.top, max(prev.bottom, band.bottom), min(prev.left, band.left), max(prev.right, band.right))
        else:
            merged.append(band)
    if len(merged) >= 2 and merged[0].height < pitch * 0.33:
        first, second = merged[0], merged[1]
        merged[1] = LineBand(first.top, second.bottom, min(first.left, second.left), max(first.right, second.right))
        merged.pop(0)
    # A band with almost no ink that is small both ways (a dot, a speck) is not a line: it
    # joins its nearest neighbour.  A short last line ("ضعف.") is light too, but a word wide.
    while len(merged) >= 2:
        mass = [int(mask[b.top : b.bottom, b.left : b.right].sum()) for b in merged]
        typical = float(np.median([b.height for b in merged]))
        specks = [
            k
            for k, b in enumerate(merged)
            if mass[k] < 0.08 * max(mass) and b.right - b.left < 0.75 * typical and b.height < 0.75 * typical
        ]
        if not specks:
            break
        i = min(specks, key=lambda k: mass[k])
        _merge_into_neighbour(merged, i)
    # A low fragment touching its line (a descender or a row of dots cut off at the
    # baseline) belongs to that line.  A real short line sits across a line gap.
    while len(merged) >= 2:
        mass = [int(mask[b.top : b.bottom, b.left : b.right].sum()) for b in merged]
        tallest = max(b.height for b in merged)
        typical = float(np.median([b.height for b in merged]))
        touch = max(3.0, 0.1 * typical)
        frags = [
            k
            for k, b in enumerate(merged)
            if b.height < 0.45 * tallest
            and mass[k] < 0.2 * max(mass)
            and min(
                b.top - merged[k - 1].bottom if k > 0 else 1e9,
                merged[k + 1].top - b.bottom if k + 1 < len(merged) else 1e9,
            )
            <= touch
        ]
        if not frags:
            break
        _merge_into_neighbour(merged, min(frags, key=lambda k: mass[k]))
    return merged


def _merge_into_neighbour(bands: list[LineBand], i: int) -> None:
    """Fold band ``i`` into the closer of its neighbours."""
    b = bands.pop(i)
    if i == 0:
        j = 0
    elif i == len(bands):
        j = i - 1
    else:
        j = i - 1 if b.top - bands[i - 1].bottom <= bands[i].top - b.bottom else i
    n = bands[j]
    bands[j] = LineBand(min(n.top, b.top), max(n.bottom, b.bottom), min(n.left, b.left), max(n.right, b.right))


def _estimate_line_pitch(profile: np.ndarray) -> float:
    """Dominant period of the projection profile (≈ line spacing), via autocorrelation."""
    p = profile - profile.mean()
    n = len(p)
    if n < 8 or not np.any(p):
        return max(8.0, n / 2)
    ac = np.correlate(p, p, mode="full")[n - 1 :]
    ac /= ac[0] if ac[0] else 1
    # Lines can't be closer together than their own ink is tall, so lags shorter than a
    # typical run of inked rows are structure *inside* a line (baseline vs. dots row),
    # not the line spacing.
    inked = profile > profile.max() * 0.1
    runs, start = [], None
    for y, on in enumerate(inked):
        if on and start is None:
            start = y
        elif not on and start is not None:
            runs.append(y - start)
            start = None
    if start is not None:
        runs.append(n - start)
    lo = max(6, int(0.8 * np.median(runs))) if runs else 6
    hi = min(n - 1, max(lo + 1, int(n * 0.75)))
    peaks = [i for i in range(lo, hi) if ac[i] > ac[i - 1] and ac[i] >= ac[i + 1] and ac[i] > 0.15]
    if peaks:
        best = max(peaks, key=lambda i: ac[i])
        # the fundamental, not a multiple of it, when its peak is nearly as strong
        for i in peaks:
            if i < best and ac[i] >= 0.8 * ac[best] and abs(best / i - round(best / i)) < 0.12:
                return float(i)
        return float(best)
    rows = np.where(profile > profile.max() * 0.1)[0]
    return float(max(8, (rows[-1] - rows[0] + 1) if rows.size else n))


def _find_valleys(smooth: np.ndarray, pitch: float) -> list[int]:
    h = len(smooth)
    radius = max(2, int(pitch * 0.5))
    valleys = []
    for y in range(1, h - 1):
        lo = max(0, y - radius)
        hi = min(h, y + radius + 1)
        window = smooth[lo:hi]
        if smooth[y] != window.min():
            continue
        left_peak = smooth[max(0, y - int(pitch)) : y].max() if y > 0 else 0
        right_peak = smooth[y + 1 : min(h, y + int(pitch) + 1)].max() if y + 1 < h else 0
        floor = min(left_peak, right_peak)
        if floor <= 0:
            continue
        if smooth[y] <= 0.35 * floor:
            if valleys and y - valleys[-1] < pitch * 0.45:
                # keep the deeper of two nearby valleys
                if smooth[y] < smooth[valleys[-1]]:
                    valleys[-1] = y
                continue
            valleys.append(y)
    return valleys


def median_line_height(bands: list[LineBand]) -> float:
    if not bands:
        return 0.0
    pitches = [b2.top - b1.top for b1, b2 in zip(bands, bands[1:]) if b2.top > b1.top]
    heights = sorted(b.height for b in bands)
    if pitches:
        pitches.sort()
        return float(pitches[len(pitches) // 2])
    return float(heights[len(heights) // 2])


# --------------------------------------------------------------------------- #
# Rotation of marginal notes
# --------------------------------------------------------------------------- #


def estimate_text_angle(gray: np.ndarray, coarse_step: float = 5.0) -> float:
    """Dominant text-line angle in degrees (CCW rotation that makes lines horizontal),
    searched over [-90, 90).  Returns 0 for already-horizontal text."""
    mask = ink_mask(gray)
    if mask.sum() < 50:
        return 0.0
    h, w = mask.shape
    side = int(math.hypot(h, w)) + 4
    canvas = np.zeros((side, side), np.uint8)
    oy, ox = (side - h) // 2, (side - w) // 2
    canvas[oy : oy + h, ox : ox + w] = mask
    scale = 400 / side if side > 400 else 1.0
    if scale != 1.0:
        canvas = cv2.resize(canvas, (int(side * scale), int(side * scale)), interpolation=cv2.INTER_AREA)
        canvas = (canvas > 0).astype(np.uint8)
    c = (canvas.shape[1] / 2, canvas.shape[0] / 2)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(c, angle, 1.0)
        rot = cv2.warpAffine(canvas, m, canvas.shape[::-1], flags=cv2.INTER_NEAREST)
        prof = rot.sum(axis=1).astype(np.float64)
        return float(np.var(prof))

    angles = np.arange(-90, 90, coarse_step)
    scores = [score(float(a)) for a in angles]
    best = float(angles[int(np.argmax(scores))])
    fine = np.arange(best - coarse_step, best + coarse_step + 0.01, 1.0)
    fine_scores = [score(float(a)) for a in fine]
    best = float(fine[int(np.argmax(fine_scores))])
    base = score(0.0)
    if abs(best) < 4 or score(best) < base * 1.15:
        return 0.0
    return best


# --------------------------------------------------------------------------- #
# Continuation sign ("=") detection
# --------------------------------------------------------------------------- #


def detect_equals_sign(gray: np.ndarray, side: str = "right", line_height: float | None = None) -> bool:
    """True if the line image starts (``side='right'``, Arabic) or ends with an '=' sign."""
    mask = ink_mask(gray)
    h, w = mask.shape
    if h < 6 or w < 6:
        return False
    lh = line_height or h
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if area < 4:
            continue
        comps.append((x, y, cw, ch, area))
    if len(comps) < 2:
        return False
    # Sort by distance from the line start.
    comps.sort(key=lambda c: -(c[0] + c[2]) if side == "right" else c[0])
    edge = comps[0][0] + comps[0][2] if side == "right" else comps[0][0]
    # Candidates: flat components near the start edge.
    near = [c for c in comps[:8] if (abs((c[0] + c[2]) - edge) if side == "right" else abs(c[0] - edge)) < lh * 1.2]
    flats = [c for c in near if c[2] >= 2.2 * c[3] and lh * 0.12 <= c[2] <= lh * 1.2 and c[3] <= max(3, lh * 0.16)]
    for i in range(len(flats)):
        for j in range(i + 1, len(flats)):
            a, b = flats[i], flats[j]
            top, bot = (a, b) if a[1] < b[1] else (b, a)
            overlap = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
            if overlap < 0.6 * min(a[2], b[2]):
                continue
            if not (0.6 <= a[2] / max(1, b[2]) <= 1.6):
                continue
            gap = bot[1] - (top[1] + top[3])
            if gap <= 0 or gap > lh * 0.35:
                continue
            # The pair must sit in the middle band of the line, not as diacritics.
            mid = (top[1] + bot[1] + bot[3]) / 2
            if not (h * 0.2 <= mid <= h * 0.85):
                continue
            # Nothing else may overlap the pair's horizontal span (it's a standalone sign).
            x0 = min(a[0], b[0])
            x1 = max(a[0] + a[2], b[0] + b[2])
            others = [c for c in near if c not in (a, b) and c[0] < x1 and c[0] + c[2] > x0 and c[4] > 0.3 * (a[4] + b[4])]
            if others:
                continue
            return True
    return False


# --------------------------------------------------------------------------- #
# Separator rules (footnote lines)
# --------------------------------------------------------------------------- #


def find_horizontal_rules(gray: np.ndarray, min_frac: float = 0.12) -> list[tuple[int, int, int, int]]:
    """Find thin horizontal rules (e.g. the line above footnotes): returns boxes x0,y0,x1,y1."""
    h, w = gray.shape
    mask = ink_mask(gray)
    length = max(20, int(w * min_frac))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1))
    lines = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    n, _, stats, _ = cv2.connectedComponentsWithStats(lines, connectivity=8)
    rules = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if cw >= length and ch <= max(4, h * 0.006):
            # must be isolated: little ink just above/below (not underlined text)
            y_above = max(0, y - int(h * 0.006) - 2)
            y_below = min(h, y + ch + int(h * 0.006) + 2)
            around = mask[y_above:y, x : x + cw].mean() if y > y_above else 0
            around += mask[y + ch : y_below, x : x + cw].mean() if y_below > y + ch else 0
            if around < 0.08:
                rules.append((int(x), int(y), int(x + cw), int(y + ch)))
    return rules


# --------------------------------------------------------------------------- #
# Crops for the recogniser
# --------------------------------------------------------------------------- #


def tight_ink_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(mask > 0)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def prepare_crop(
    img: Image.Image,
    target_line_height: int,
    line_height: float,
    max_upscale: float,
    max_pixels: int,
    pad_ratio: float = 0.33,
    min_pad: int = 16,
) -> tuple[Image.Image, float]:
    """Scale a crop so its text lines are ``target_line_height`` tall and add white padding."""
    scale = 1.0
    if target_line_height and line_height > 0:
        scale = target_line_height / line_height
        scale = max(0.5, min(max_upscale, scale))
        if 0.92 < scale < 1.08:
            scale = 1.0
    w, h = img.size
    if w * h * scale * scale > max_pixels:
        scale = math.sqrt(max_pixels / max(1, w * h))
    if scale != 1.0:
        img = img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.LANCZOS)
    effective_lh = line_height * scale if line_height else img.size[1]
    pad = int(max(min_pad, effective_lh * pad_ratio))
    img = ImageOps.expand(img.convert("RGB"), border=pad, fill=(255, 255, 255))
    return img, scale
