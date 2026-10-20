"""Region recognition with self-checking.

For every text region the recogniser:

1. crops it (masking neighbouring regions so text is never read twice),
2. straightens rotated marginal notes,
3. finds the text lines and cuts tall regions *between* lines into chunks,
4. scales each chunk so lines have a consistent height, pads it with white,
5. reads the chunks with the OCR model (batched),
6. checks every result — line count against the image, runaway loops,
   token-budget overruns — and re-reads failures in smaller pieces,
7. records line boxes so the searchable PDF can place the text exactly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw

from ..models.ocr_model import weak_words
from . import geometry as G
from . import imaging
from .area import AREA_NOTE, clip_to_box
from .document import FOOTER, FOOTNOTE, HEADER, MARGIN, PAGE_NUMBER, TABLE, TEXT_KINDS, Line, Region
from .text import (
    clean_model_output,
    foreign_char_count,
    html_to_text,
    normalize_presentation_forms,
    strip_markdown,
    trailing_echo,
    trailing_restart,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[dict], None]


@dataclass
class Chunk:
    region: Region
    order: int
    bands: list[imaging.LineBand]  # in (rotated) crop coordinates
    box: tuple[int, int, int, int]  # chunk box in crop coordinates
    image: Image.Image | None = None
    scale: float = 1.0
    budget: int = 256
    depth: int = 0
    variant: str = ""
    result: object | None = None
    text: str = ""
    ok: bool = True
    previous: "Chunk | None" = None  # earlier attempt of a re-read single line
    doubt: list[int] = field(default_factory=list)  # lines holding a token the model was unsure of
    patch_of: "tuple[Chunk, int] | None" = None  # (block, line index) this single-line re-read may replace
    off_length: set[int] = field(default_factory=set)  # lines far too short/long for their width
    density: float = 0.0  # the region's typical characters per pixel of line width


@dataclass
class RegionJob:
    region: Region
    crop: Image.Image  # straightened crop
    crop_origin: tuple[int, int]  # top-left of the crop on the page (before rotation)
    pre_size: tuple[int, int]  # crop size before rotation
    angle: float
    line_height: float
    bands: list[imaging.LineBand]
    chunks: list[Chunk] = field(default_factory=list)
    alt: "RegionJob | None" = None  # 180° flipped alternative for rotated notes


class Recognizer:
    def __init__(self, ocr_model, settings, progress: ProgressFn | None = None, cancel: Callable[[], bool] | None = None):
        self.ocr = ocr_model
        self.settings = settings
        self.progress = progress or (lambda e: None)
        self.cancel = cancel or (lambda: False)
        # (region id, variant) -> (typical characters per pixel, widest line in pixels)
        self.density: dict[tuple[str, str], tuple[float, float]] = {}

    # ------------------------------------------------------------------ #
    def recognize_page(self, page_img: Image.Image, regions: list[Region], page_index: int, targets: list[Region] | None = None) -> None:
        """Read the text regions of a page.  ``targets`` limits reading to some of them;
        ``regions`` is still the whole page, so neighbouring blocks are masked out."""
        jobs: list[RegionJob] = []
        for r in regions if targets is None else targets:
            if r.kind not in TEXT_KINDS:
                continue
            job = self._prepare_region(page_img, r, regions)
            if job is None:
                r.text = ""
                continue
            jobs.append(job)
            if job.alt is not None:
                jobs.append(job.alt)
        all_chunks = [c for j in jobs for c in j.chunks]
        self._run_chunks(all_chunks, page_index)
        self._learn_density(jobs)
        for c in all_chunks:
            self._check_length(c)
        # Self-check and retry
        if self.settings.recognition.verify_and_retry:
            for depth in range(4):
                retry: list[Chunk] = []
                for job in jobs:
                    new_chunks: list[Chunk] = []
                    for c in job.chunks:
                        if c.depth > depth or (c.ok and not c.doubt):
                            new_chunks.append(c)
                            continue
                        if c.ok:
                            # verified, but some line holds a letter the model was unsure of
                            extra = self._reread_doubtful(job, c)
                            c.doubt = []
                            if len(c.bands) > 1:
                                new_chunks.append(c)  # the block stays; re-read lines may patch it
                            elif not extra:
                                new_chunks.append(c)
                            retry.extend(extra)
                            new_chunks.extend(extra)
                            continue
                        replacement = self._split_or_rescale(job, c)
                        if replacement:
                            retry.extend(replacement)
                            new_chunks.extend(replacement)
                        else:
                            new_chunks.append(c)
                    job.chunks = new_chunks
                if not retry or self.cancel():
                    break
                self.progress({"type": "status", "page": page_index, "message": f"Re-reading {len(retry)} uncertain piece(s)…"})
                self._run_chunks(retry, page_index)
        # Pick the better orientation for rotated notes
        by_region: dict[str, list[RegionJob]] = {}
        for job in jobs:
            by_region.setdefault(job.region.id, []).append(job)
        for region_id, candidates in by_region.items():
            best = max(candidates, key=_job_score)
            self._finalize(best)

    # ------------------------------------------------------------------ #
    def _prepare_region(self, page_img: Image.Image, r: Region, regions: list[Region]) -> RegionJob | None:
        lay = self.settings.layout
        pad = lay.region_padding
        W, H = page_img.size
        x0, y0, x1, y1 = r.bbox
        cx0, cy0 = int(max(0, math.floor(x0 - pad))), int(max(0, math.floor(y0 - pad)))
        cx1, cy1 = int(min(W, math.ceil(x1 + pad))), int(min(H, math.ceil(y1 + pad)))
        if cx1 - cx0 < 4 or cy1 - cy0 < 4:
            return None
        crop = page_img.crop((cx0, cy0, cx1, cy1)).convert("RGB")
        if AREA_NOTE in r.notes:
            # a box the user drew: exactly what lies inside it, nothing of its neighbours
            crop = clip_to_box(crop, (x0 - cx0, y0 - cy0, x1 - cx0, y1 - cy0))
        # White out other regions that intrude into this crop.
        draw = ImageDraw.Draw(crop)
        for o in regions:
            if o is r or G.intersection_area(o.bbox, (cx0, cy0, cx1, cy1)) <= 0:
                continue
            if G.containment(o.bbox, r.bbox) > 0.6 and o.kind not in (MARGIN, FOOTNOTE, HEADER, FOOTER, PAGE_NUMBER):
                continue  # nested piece of this region (e.g. a title inside a text block)
            if G.containment(r.bbox, o.bbox) > 0.9:
                continue  # we are inside it; don't erase ourselves
            if G.containment(o.bbox, r.bbox) > 0.3:
                pieces = [o.bbox]  # a block sitting inside our box (e.g. a margin note): erase it
            elif o.kind == MARGIN and r.kind != MARGIN and _overlap_depth(o.bbox, r.bbox) <= 0.4 * (r.line_height or 30):
                # a note whose box only grazes ours: the ink in that sliver is the note's, and
                # leaving it would add a stray letter to the end of our lines
                pieces = [o.bbox]
            else:
                pieces = _box_difference(o.bbox, r.bbox)  # only erase what lies outside our own box
            for ox0, oy0, ox1, oy1 in pieces:
                draw.rectangle([ox0 - cx0, oy0 - cy0, ox1 - cx0, oy1 - cy0], fill=(255, 255, 255))
        if lay.use_polygons and r.polygon and len(r.polygon) >= 3:
            mask = Image.new("L", crop.size, 0)
            ImageDraw.Draw(mask).polygon([(px - cx0, py - cy0) for px, py in r.polygon], fill=255)
            # dilate the polygon slightly so descenders survive
            mask = Image.fromarray(np.asarray(mask)).filter(_max_filter(pad))
            white = Image.new("RGB", crop.size, (255, 255, 255))
            crop = Image.composite(crop, white, mask)
        pre_size = crop.size
        angle = 0.0
        gray = imaging.to_gray(crop)
        rotate_candidate = r.kind == MARGIN or r.label == "vertical_text" or (r.height > 2.4 * r.width and r.height > 120)
        if lay.straighten_rotated_text and rotate_candidate:
            angle = imaging.estimate_text_angle(gray)
            if angle:
                crop = imaging.rotate_image(crop, angle, expand=True)
                gray = imaging.to_gray(crop)
        mask_arr = imaging.ink_mask(gray)
        bands = imaging.segment_lines(gray, mask_arr)
        if bands:
            # rules, underlines and separator lines confuse the reader: paint them out
            erased = _erase_rules(crop, mask_arr, imaging.median_line_height(bands) or bands[0].height)
            if erased is not None:
                crop = erased
                gray = imaging.to_gray(crop)
                mask_arr = imaging.ink_mask(gray)
                bands = imaging.segment_lines(gray, mask_arr) or bands
        if not bands:
            ink = imaging.tight_ink_box(mask_arr)
            if ink is None:
                return None
            bands = [imaging.LineBand(ink[1], ink[3], ink[0], ink[2])]
        lh = imaging.median_line_height(bands) or float(bands[0].height)
        job = RegionJob(r, crop, (cx0, cy0), pre_size, angle, lh, bands)
        job.chunks = self._make_chunks(job, bands, depth=0)
        if r.kind == FOOTNOTE:
            first = gray[bands[0].top : bands[0].bottom, :]
            last = gray[bands[-1].top : bands[-1].bottom, :]
            r.notes = [n for n in r.notes if n not in ("starts_with_equals", "ends_with_equals")]
            if imaging.detect_equals_sign(first, "right", lh):
                r.notes.append("starts_with_equals")
            if imaging.detect_equals_sign(last, "left", lh):
                r.notes.append("ends_with_equals")
        if angle and abs(angle) > 10:
            flipped = imaging.rotate_image(crop, 180, expand=True)
            fg = imaging.to_gray(flipped)
            fb = imaging.segment_lines(fg) or bands
            alt = RegionJob(r, flipped, (cx0, cy0), pre_size, angle + 180, imaging.median_line_height(fb) or lh, fb)
            alt.chunks = self._make_chunks(alt, fb, depth=0, variant="flipped")
            job.alt = alt
        return job

    def _make_chunks(self, job: RegionJob, bands: list[imaging.LineBand], depth: int, variant: str = "") -> list[Chunk]:
        """Group lines for reading.  Arabic-GLM-OCR is excellent on single lines and on
        tall paragraphs, but unreliable on strips of 2–5 lines (it skips or reorders
        lines there), so groups are either one line or a real block."""
        rec = self.settings.recognition
        groups: list[list[imaging.LineBand]] = []
        rest = list(bands)
        n = len(rest)
        if job.region.kind == TABLE:
            groups = [rest]  # tables need their whole structure in view
        elif job.region.kind == FOOTNOTE:
            # footnotes: markers like (٢) must stay on their own line, and the first
            # line alone guarantees an opening "=" continuation is never lost
            per = max(1, rec.footnote_lines_per_chunk)
            if per > 1 and n > 1:
                groups.append([rest.pop(0)])
            groups.extend(_blocks(rest, per, min_size=1) if per > 1 else [[b] for b in rest])
        elif rec.granularity == "lines":
            groups = [[b] for b in rest]
        else:
            min_block = max(2, rec.min_block_lines) if rec.granularity == "auto" else 2
            if n < min_block:
                groups = [[b] for b in rest] if n > 1 else [rest]
            else:
                groups = _blocks(rest, max(min_block, rec.max_lines_per_chunk), min_size=min_block)
        return [self._chunk_from_bands(job, g, bands, depth, variant, i) for i, g in enumerate(groups)]

    def _chunk_from_bands(self, job: RegionJob, group, all_bands, depth: int, variant: str, order: int) -> Chunk:
        W, H = job.crop.size
        lh = job.line_height or group[0].height
        first_i = all_bands.index(group[0]) if group[0] in all_bands else 0
        last_i = all_bands.index(group[-1]) if group[-1] in all_bands else len(all_bands) - 1
        top = group[0].top
        bottom = group[-1].bottom
        # extend half-way to neighbouring lines (keeps diacritics) but never into them
        prev_bottom = all_bands[first_i - 1].bottom if first_i > 0 else 0
        next_top = all_bands[last_i + 1].top if last_i + 1 < len(all_bands) else H
        y0 = max(prev_bottom, int(top - lh * 0.35), 0) if first_i > 0 else max(0, int(top - lh * 0.5))
        y1 = min(next_top, int(bottom + lh * 0.35), H) if last_i + 1 < len(all_bands) else min(H, int(bottom + lh * 0.5))
        left = min(b.left for b in group)
        right = max(b.right for b in group)
        x0 = max(0, int(left - lh * 0.6))
        x1 = min(W, int(right + lh * 0.6))
        chunk = Chunk(job.region, order, list(group), (x0, y0, x1, y1), depth=depth, variant=variant)
        self._render_chunk(job, chunk)
        return chunk

    def _render_chunk(self, job: RegionJob, chunk: Chunk, extra_scale: float = 1.0) -> None:
        rec = self.settings.recognition
        sub = job.crop.crop(chunk.box)
        if len(chunk.bands) == 1:
            band = chunk.bands[0]
            sub = _isolate_line(sub, band.top - chunk.box[1], band.bottom - chunk.box[1])
        lh = job.line_height or (chunk.box[3] - chunk.box[1])
        target = int(rec.target_line_height * extra_scale) if rec.target_line_height else 0
        if not target and extra_scale != 1.0:
            target = int(lh * extra_scale)
        img, scale = imaging.prepare_crop(sub, target, lh, rec.max_upscale * extra_scale, rec.max_pixels)
        if len(chunk.bands) == 1 and chunk.bands[0].right - chunk.bands[0].left < 5 * lh:
            # a scrap of one or two words: give it a normal line's proportions
            pad = (img.height - round(sub.height * scale)) // 2
            widest = max((b.right - b.left for b in job.bands), default=0)
            img = _widen_short_line(img, int(max(widest, 12 * lh) * scale) + 2 * pad)
        chunk.image, chunk.scale = img, scale
        n = max(1, len(chunk.bands))
        width_ratio = (chunk.box[2] - chunk.box[0]) / max(8.0, lh)
        budget = int(n * (8.5 * width_ratio + 10) + 32)
        if not rec.adaptive_token_budget:
            budget = rec.max_new_tokens
        chunk.budget = max(48, min(rec.max_new_tokens, budget))

    # ------------------------------------------------------------------ #
    def _run_chunks(self, chunks: list[Chunk], page_index: int) -> None:
        if not chunks:
            return
        bs = self.settings.recognition.batch_size or _auto_batch(self.ocr)
        ordered = sorted(chunks, key=lambda c: c.image.size[0] * c.image.size[1])
        done = 0
        for i in range(0, len(ordered), bs):
            if self.cancel():
                return
            batch = ordered[i : i + bs]
            ids = sorted({c.region.id for c in batch})
            self.progress({"type": "reading", "page": page_index, "regions": ids, "done": done, "total": len(ordered)})
            try:
                results = self.ocr.recognize([c.image for c in batch], [c.budget for c in batch], self.settings, self.cancel)
            except Exception as exc:
                if len(batch) > 1 and _is_oom(exc):
                    log.warning("Out of memory with batch %d; retrying one by one", len(batch))
                    import gc

                    gc.collect()
                    from ..models import hardware

                    hardware.empty_cache()
                    results = []
                    for c in batch:
                        results.extend(self.ocr.recognize([c.image], [c.budget], self.settings, self.cancel))
                else:
                    raise
            for c, res in zip(batch, results):
                c.result = res
                c.text = res.text
                c.ok = self._verify(c)
                c.doubt = self._doubtful_lines(c) if c.ok else []
                self._check_length(c)
            done += len(batch)

    def _verify(self, c: Chunk) -> bool:
        res = c.result
        if res is None:
            return False
        if c.region.kind == TABLE:
            return not res.looped
        raw = res.text or ""
        if "<" in raw:
            raw = html_to_text(raw)
        raw = strip_markdown(raw)
        lines = [l for l in raw.split("\n") if l.strip()]
        expected = len(c.bands)
        if res.looped or res.hit_limit:
            return False
        if expected >= 1 and not lines:
            return False
        if self.settings.text.drop_foreign_scripts and foreign_char_count(raw):
            return False  # a Chinese/Korean/… character means a misread: read it again
        # The model must not drop lines: small chunks get no tolerance at all,
        # large ones a little (line segmentation can over-count diacritic rows).
        tolerance = 0 if expected <= 4 else max(1, int(expected * 0.12))
        if len(lines) < expected - tolerance:
            return False
        if expected >= 2 and len(lines) > expected * 1.6 + 1:
            return False
        if expected >= 2 and _shared_run(lines, 6):
            return False  # the same six words on two lines: the reader mixed lines up
        if expected >= 2 and len(lines) == expected:
            # a line far longer than its width allows = merged lines or an invented tail
            ratios = [len(t.strip()) / max(1, b.right - b.left) for t, b in zip(lines, c.bands)]
            med = sorted(ratios)[len(ratios) // 2] if expected >= 3 else min(ratios)
            if med > 0 and max(ratios) > 2.2 * med and max(len(t) for t in lines) > 40:
                return False
        return True

    def _doubtful_lines(self, c: Chunk) -> list[int]:
        """Indices of output lines whose weakest token is below the doubt threshold."""
        rec = self.settings.recognition
        res = c.result
        if not (rec.verify_and_retry and rec.reread_doubtful) or c.region.kind == TABLE or res is None or not res.line_stats:
            return []
        if c.previous is not None:
            return []  # already the second reading of this line
        weak = [i for i, (low, _) in enumerate(res.line_stats) if low < rec.doubt_threshold]
        if not weak:
            return []
        if len(c.bands) == 1:
            return [0]
        if "<" in (res.text or "") or len(res.line_stats) != len(c.bands):
            return []  # lines cannot be matched to the image reliably
        return weak

    def _learn_density(self, jobs: list[RegionJob]) -> None:
        """Typical characters per pixel of line width, per region, from the first reading.
        In one font a line's length follows its width closely, so a line far off that
        ratio lost words (the model skipped between two identical words) or gained them."""
        for job in jobs:
            rows = [row for c in job.chunks for row in (_line_ratios(c) or [])]
            if len(rows) < 3:
                continue
            widest = max(w for _, w, _ in rows)
            usable = sorted(r for r, w, n in rows if w >= 0.3 * widest and n >= 8)
            if len(usable) >= 3:
                key = (job.region.id, job.chunks[0].variant if job.chunks else "")
                self.density[key] = (usable[len(usable) // 2], widest)

    def _check_length(self, c: Chunk) -> None:
        rec = self.settings.recognition
        if not (rec.verify_and_retry and rec.reread_doubtful) or not c.ok or c.previous is not None:
            return
        med, widest = self.density.get((c.region.id, c.variant), (0.0, 0.0))
        rows = _line_ratios(c)
        if not med or not rows:
            return
        # short lines hold too few letters to judge a shortfall; they can still be too long
        off = {i for i, (r, w, n) in enumerate(rows) if (r > 1.35 * med) or (w >= 0.3 * widest and n >= 8 and r < 0.85 * med)}
        if off and len(c.bands) > 1 and ("<" in (c.result.text or "") or len(c.result.line_stats) != len(c.bands)):
            return  # block lines can't be matched to their re-reads
        if off:
            c.off_length, c.density = off, med
            c.doubt = sorted(set(c.doubt) | ({0} if len(c.bands) == 1 else off))

    def _reread_doubtful(self, job: RegionJob, c: Chunk) -> list[Chunk]:
        if len(c.bands) == 1:
            return self._split_or_rescale(job, c) or []
        out = []
        for i in c.doubt:
            nc = self._chunk_from_bands(job, [c.bands[i]], job.bands, c.depth + 1, c.variant, 0)
            nc.order = c.order + (i + 1) / 1000.0
            nc.patch_of = (c, i)
            nc.density = c.density
            out.append(nc)
        return out

    def _split_or_rescale(self, job: RegionJob, c: Chunk) -> list[Chunk] | None:
        if len(c.bands) >= 2:
            # a block that failed its self-check is re-read line by line
            out = []
            for k, band in enumerate(c.bands):
                nc = self._chunk_from_bands(job, [band], job.bands, c.depth + 1, c.variant, 0)
                nc.order = c.order + (k + 1) / 1000.0
                out.append(nc)
            return out
        if c.previous is None:
            # a single line that failed: read it once more, a little larger
            nc = Chunk(c.region, c.order, c.bands, c.box, depth=c.depth + 1, variant=c.variant, previous=c, patch_of=c.patch_of, density=c.density)
            self._render_chunk(job, nc, extra_scale=1.3)
            if c.result is not None and c.result.hit_limit:
                nc.budget = min(self.settings.recognition.max_new_tokens, int(c.budget * 1.6))  # dense line: room to finish
            return [nc]
        return None

    # ------------------------------------------------------------------ #
    def _finalize(self, job: RegionJob) -> None:
        r = job.region
        # for rescaled single-line retries keep whichever attempt is better
        chunks = [_best_attempt(c) for c in sorted(job.chunks, key=lambda c: c.order)]
        patches: dict[int, dict[int, Chunk]] = {}
        for c in chunks:
            if c.patch_of is not None:
                block, i = c.patch_of
                patches.setdefault(id(block), {})[i] = c
        texts, conf_num, conf_den, flagged, notes, unsure = [], 0.0, 0, False, [], []
        mark_below = self.settings.recognition.mark_uncertain_below
        for c in chunks:
            if c.patch_of is not None:
                continue
            res = c.result
            if res is None:
                flagged = True
                continue
            raw, replaced = res.text, {}
            if id(c) in patches:
                raw, replaced = _apply_patches(c, patches[id(c)])
                if replaced:
                    notes.append("re-read doubtful lines")
            if c.off_length - set(replaced):
                flagged = True  # a line whose length doesn't fit its width: words may be missing
                notes.append("line length doubtful")
            if self.settings.text.collapse_repeats:
                density = self.density.get((c.region.id, c.variant), (0.0, 0.0))[0]
                raw, trimmed = _trim_line_ends(c, raw, density)
                if trimmed:
                    notes.append("removed repeated line ending")
            if mark_below > 0:
                unsure.extend(w for i, w in weak_words(res.pieces, res.probs, mark_below) if i not in replaced)
                for p in replaced.values():
                    unsure.extend(w for _, w in weak_words(p.result.pieces, p.result.probs, mark_below))
            cleaned, cn = clean_model_output(raw, self.settings)
            notes.extend(cn)
            texts.append(cleaned)
            if res.confidence is not None:
                conf_num += res.confidence * max(1, res.tokens)
                conf_den += max(1, res.tokens)
            if not c.ok:
                flagged = True
                notes.append("could not fully verify")
            if res.looped:
                notes.append("loop stopped")
        r.text = "\n".join(t for t in texts if t)
        if self.settings.text.line_mode == "paragraphs":
            from .text import reflow_paragraphs

            r.text = reflow_paragraphs(r.text)
        r.confidence = conf_num / conf_den if conf_den else None
        r.angle = job.angle % 360 if job.angle else 0.0
        if r.angle > 180:
            r.angle -= 360
        threshold = self.settings.recognition.flag_confidence_below
        r.flagged = flagged or (r.confidence is not None and r.confidence < threshold)
        r.uncertain = _review_words(unsure, r.text)
        r.line_height = job.line_height
        keep = [n for n in r.notes if n.startswith("lines:") or n in ("starts_with_equals", "ends_with_equals", "recovered", AREA_NOTE)]
        r.notes = keep + sorted(set(notes))
        r.lines = self._line_boxes(job, r.text)

    def _line_boxes(self, job: RegionJob, text: str) -> list[Line]:
        """Map recognised lines onto the line bands found in the image (page coordinates)."""
        lines = [l for l in text.split("\n") if l.strip()]
        if not lines:
            return []
        bands = job.bands
        # transform from straightened crop coords -> page coords
        cw, ch = job.crop.size
        pw, ph = job.pre_size
        if job.angle:
            # crop was rotated with expand=True around its centre
            back = G.multiply(
                G.multiply(G.translate(-cw / 2, -ch / 2), G.rotate_about(-job.angle, 0, 0)),
                G.translate(pw / 2 + job.crop_origin[0], ph / 2 + job.crop_origin[1]),
            )
        else:
            back = G.translate(job.crop_origin[0], job.crop_origin[1])
        boxes: list[tuple[float, float, float, float]] = []
        if len(bands) == len(lines):
            boxes = [(b.left, b.top, b.right, b.bottom) for b in bands]
        elif bands:
            # distribute lines proportionally over the bands' vertical extent
            top, bottom = bands[0].top, bands[-1].bottom
            left = min(b.left for b in bands)
            right = max(b.right for b in bands)
            step = (bottom - top) / len(lines)
            boxes = [(left, top + i * step, right, top + (i + 1) * step) for i in range(len(lines))]
        out = []
        for text_line, box in zip(lines, boxes):
            pts = G.apply_box(back, box)
            quad = [(round(x, 2), round(y, 2)) for x, y in pts]
            out.append(Line(bbox=G.bounds(pts), text=text_line, angle=-job.angle if job.angle else 0.0, quad=quad))
        return out


def _line_score(stats: list[tuple[float, float]]) -> float:
    """Mean log-probability, pulled down by the single weakest token."""
    if not stats:
        return -1e9
    low = min(p for p, _ in stats)
    mean = sum(m for _, m in stats) / len(stats)
    return mean + 0.1 * math.log(max(low, 1e-4))


def _best_attempt(c: Chunk) -> Chunk:
    prev = c.previous
    if prev is None or prev.result is None or c.result is None:
        return c
    if prev.off_length and c.ok and prev.density:
        # the first reading lost (or invented) words: prefer a re-read whose length fits
        # the line, unless it is clearly less confident
        new_off, old_off = _length_error(c, prev.density), _length_error(prev, prev.density)
        if new_off < old_off - 0.05 and _chunk_score(c) >= _chunk_score(prev) - 0.3:
            return c
        if old_off < new_off - 0.05:
            return prev
    return prev if _chunk_score(prev) > _chunk_score(c) else c


def _line_ratios(c: Chunk) -> list[tuple[float, float, int]] | None:
    """(characters per pixel, line width, characters) for each line of a reading, when
    its lines can be matched one-to-one with the line bands."""
    res = c.result
    if res is None or not c.bands:
        return None
    raw = res.text or ""
    if "<" in raw:
        if len(c.bands) > 1:
            return None
        raw = html_to_text(raw)
    lines = [l.strip() for l in strip_markdown(raw).split("\n") if l.strip()]
    if len(c.bands) == 1 and lines:
        lines = [" ".join(lines)]
    if len(lines) != len(c.bands):
        return None
    out = []
    for text, b in zip(lines, c.bands):
        w = max(1.0, float(b.right - b.left))
        out.append((len(text) / w, w, len(text)))
    return out


def _length_error(c: Chunk, density: float) -> float:
    """How far a single-line reading's length is from what its width predicts (log ratio)."""
    rows = _line_ratios(c)
    if not rows or not density:
        return 9.0
    return abs(math.log(max(rows[0][0], 1e-6) / density))


def _single_line(text: str) -> str:
    if "<" in text:
        text = html_to_text(text)
    return " ".join(strip_markdown(text).split())


def _apply_patches(block: Chunk, patches: dict[int, Chunk]) -> tuple[str, dict[int, Chunk]]:
    """Replace lines of a block reading with their single-line re-reads where those are
    more confident.  Returns the raw text and the re-reads that were used, by line."""
    res = block.result
    raw = res.text or ""
    stats = res.line_stats
    replace: dict[int, str] = {}
    used: dict[int, Chunk] = {}
    lines = [l for l in raw.split("\n") if l.strip()]
    for i, p in patches.items():
        pr = p.result
        if pr is None or not p.ok or i >= len(lines) or i >= len(stats):
            continue
        new = _single_line(pr.text or "")
        old = lines[i].strip()
        if not new:
            continue
        if i in block.off_length and block.density and i < len(block.bands):
            # the block reading of this line lost or invented words: take the re-read if
            # its length fits the line's width better and it is not clearly less sure
            width = max(1.0, float(block.bands[i].right - block.bands[i].left))
            err_new = abs(math.log(max(len(new), 1) / width / block.density))
            err_old = abs(math.log(max(len(old), 1) / width / block.density))
            if err_new < err_old - 0.05 and _line_score(pr.line_stats) >= _line_score([stats[i]]) - 0.3:
                replace[i] = new
                used[i] = p
            continue
        if not 0.5 <= len(new) / max(1, len(old)) <= 2.0:
            continue  # a re-read that lost or invented half the line is no improvement
        if _line_score(pr.line_stats) > _line_score([stats[i]]):
            replace[i] = new
            used[i] = p
    if not replace:
        return raw, {}
    out, k = [], 0
    for line in raw.split("\n"):
        if line.strip():
            line = replace.get(k, line)
            k += 1
        out.append(line)
    return "\n".join(out), used


def _trim_line_ends(c: Chunk, raw: str, density: float) -> tuple[str, bool]:
    """Drop what the model added after a line's real end: its last phrase said twice
    (unless the line's width calls for that many letters) or, when the reading was cut
    off, the start of the line over again."""
    res = c.result
    if "<" in raw:
        raw = html_to_text(raw)
    lines = raw.split("\n")
    filled = [k for k, l in enumerate(lines) if l.strip()]
    if not filled or len(filled) != len(c.bands):
        return raw, False
    changed = False
    for j, k in enumerate(filled):
        line = lines[k].strip()
        n = trailing_echo(line)
        if n and density:
            shorter = " ".join(line.split()[:-n])
            width = max(1.0, float(c.bands[j].right - c.bands[j].left))
            if abs(math.log(len(shorter) / width / density)) < abs(math.log(len(line) / width / density)):
                lines[k], line, changed = shorter, shorter, True
        if j == len(filled) - 1 and res is not None and (res.hit_limit or res.looped):
            n = trailing_restart(line)
            if n:
                lines[k], changed = " ".join(line.split()[:-n]), True
    return "\n".join(lines), changed


_EDGE_PUNCT = " \t.,،؛:;!?؟\"'«»()[]{}<>-–—…*/\\|"


def _review_words(words: list[str], text: str) -> list[str]:
    """Uncertain words as they appear in the final text (deduplicated, punctuation trimmed)."""
    out: list[str] = []
    for w in words:
        w = normalize_presentation_forms(w).strip(_EDGE_PUNCT)
        if len(w) < 1 or w in out or w not in text or not any(ch.isalnum() for ch in w):
            continue
        out.append(w)
    return out[:300]


def _chunk_score(c: Chunk) -> float:
    res = c.result
    if res is None:
        return -1e9
    s = res.confidence or 0.0
    if res.line_stats:
        s += 0.1 * math.log(max(1e-4, min(p for p, _ in res.line_stats)))
    if res.looped:
        s -= 1.0
    if res.hit_limit:
        s -= 0.5
    if not c.ok:
        s -= 0.25
    s -= 0.2 * min(3, foreign_char_count(res.text or ""))
    return s


def _job_score(job: RegionJob) -> float:
    scores = [_chunk_score(_best_attempt(c)) for c in job.chunks if c.patch_of is None]
    return sum(scores) / len(scores) if scores else -1e9


def _erase_rules(img: Image.Image, mask: np.ndarray, line_height: float) -> Image.Image | None:
    """White out long, thin, stand-alone horizontal strokes (rules, underlines).
    Kashida/tatweel strokes are attached to letters, so they are never touched."""
    import cv2

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    lh = max(8.0, float(line_height))
    hits = [i for i in range(1, n) if stats[i, cv2.CC_STAT_WIDTH] >= 2.5 * lh and stats[i, cv2.CC_STAT_HEIGHT] <= max(4.0, 0.14 * lh)]
    if not hits:
        return None
    arr = np.asarray(img.convert("RGB")).copy()
    erase = np.isin(labels, hits)
    # include the anti-aliased fringe of the stroke
    erase = cv2.dilate(erase.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    arr[erase] = 255
    return Image.fromarray(arr)


def _auto_batch(ocr) -> int:
    device = getattr(ocr, "device", "cpu") or "cpu"
    if device == "cpu":
        return 1
    if device == "mps":
        return 2
    from ..models import hardware

    mem = hardware.device_memory_gb(device) or 8
    return 8 if mem >= 20 else 6 if mem >= 12 else 4 if mem >= 8 else 2


def _widen_short_line(img: Image.Image, width: int) -> Image.Image:
    """Put a scrap of text (a paragraph's last word, a two-word note) on a canvas as wide
    as a normal line, right-aligned like Arabic text.  On its own such a tiny image makes
    the reader hallucinate (Chinese, English); at a normal line's proportions it reads it.
    Longer short lines are left alone: widening them costs the model its diacritics."""
    if img.width >= 0.8 * width:
        return img
    canvas = Image.new("RGB", (width, img.height), (255, 255, 255))
    canvas.paste(img, (width - img.width, 0))
    return canvas


def _isolate_line(img: Image.Image, top: int, bottom: int) -> Image.Image:
    """Erase ink that belongs to the neighbouring lines (descenders from above,
    ascenders from below) from a single-line crop."""
    import cv2

    gray = imaging.to_gray(img)
    mask = imaging.ink_mask(gray)
    h = mask.shape[0]
    if h < 8 or mask.sum() == 0:
        return img
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    erase = np.zeros(n, dtype=bool)
    for i in range(1, n):
        y, ch = stats[i, cv2.CC_STAT_TOP], stats[i, cv2.CC_STAT_HEIGHT]
        y1 = y + ch
        overlap = min(y1, bottom) - max(y, top)
        if overlap <= 0:
            erase[i] = True  # entirely outside this line's band
        elif (y <= 1 or y1 >= h - 1) and overlap < 0.25 * ch:
            erase[i] = True  # a neighbour's stroke poking in from the edge
    if not erase.any():
        return img
    arr = np.asarray(img.convert("RGB")).copy()
    arr[erase[labels]] = 255
    return Image.fromarray(arr)


def _blocks(bands: list, max_size: int, min_size: int) -> list[list]:
    """Split ``bands`` into balanced consecutive groups of at most ``max_size``
    (and, when possible, at least ``min_size``) lines."""
    n = len(bands)
    if n == 0:
        return []
    k = max(1, math.ceil(n / max_size))
    size = math.ceil(n / k)
    if size < min_size and n >= min_size:
        k = max(1, n // min_size)
        size = math.ceil(n / k)
    groups = []
    for i in range(0, n, size):
        groups.append(bands[i : i + size])
    # a short tail would fall in the unreliable range: merge it into the previous group
    if len(groups) > 1 and len(groups[-1]) < min_size:
        groups[-2].extend(groups.pop())
    return groups


def _shared_run(lines: list[str], n: int) -> bool:
    """True if any run of ``n`` consecutive words appears on two different lines."""
    seen: dict[tuple[str, ...], int] = {}
    for i, line in enumerate(lines):
        words = line.split()
        for j in range(len(words) - n + 1):
            key = tuple(words[j : j + n])
            if seen.setdefault(key, i) != i:
                return True
    return False


def _overlap_depth(a, b) -> float:
    """How far two overlapping boxes reach into each other (the thinner side of their intersection)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, min(w, h))


def _box_difference(a, b) -> list[tuple[float, float, float, float]]:
    """Parts of box ``a`` that lie outside box ``b`` (up to four rectangles)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    if G.intersection_area(a, b) <= 0:
        return [tuple(a)]
    out = []
    if ay0 < by0:
        out.append((ax0, ay0, ax1, min(ay1, by0)))
    if ay1 > by1:
        out.append((ax0, max(ay0, by1), ax1, ay1))
    my0, my1 = max(ay0, by0), min(ay1, by1)
    if ax0 < bx0:
        out.append((ax0, my0, min(ax1, bx0), my1))
    if ax1 > bx1:
        out.append((max(ax0, bx1), my0, ax1, my1))
    return [r for r in out if r[2] > r[0] and r[3] > r[1]]


def _is_oom(exc: Exception) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "oom" in text or exc.__class__.__name__ == "OutOfMemoryError"


def _max_filter(pad: int):
    from PIL import ImageFilter

    size = max(3, (pad // 2) * 2 + 1)
    return ImageFilter.MaxFilter(min(size, 15))
