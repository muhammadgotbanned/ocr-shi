"""Wrapper around Arabic-GLM-OCR-v2 (GLM-OCR architecture) for region recognition."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image

from . import hardware, hub

log = logging.getLogger(__name__)


@dataclass
class Recognition:
    text: str
    confidence: float | None
    tokens: int
    hit_limit: bool  # stopped because of the token budget
    looped: bool  # stopped by the repetition detector
    seconds: float = 0.0
    # (weakest token probability, mean log-probability) of every non-empty output line
    line_stats: list[tuple[float, float]] = field(default_factory=list)
    pieces: list[str] = field(default_factory=list)  # decoded text of each generated token
    probs: list[float] = field(default_factory=list)  # probability of each generated token


def loop_period(ids: list[int]) -> int:
    """Period of a loop at the tail of ``ids`` (0 if none): a short pattern repeated many
    times, or a long passage (a whole line or two) twice in a row — degenerate output."""
    n = len(ids)
    if n < 16:
        return 0
    tail = ids[-160:]
    m = len(tail)
    for period in range(1, 41):
        reps = 12 if period == 1 else (6 if period <= 3 else 3)
        span = period * reps
        if span > m:
            break
        if period > 3 and span < 24:
            continue
        seg = tail[-span:]
        unit = seg[:period]
        if all(seg[i] == unit[i % period] for i in range(span)):
            return period
    # long periods: the last `p` tokens equal the `p` tokens before them
    for period in range(41, min(400, n // 2) + 1):
        if ids[-period:] == ids[-2 * period : -period]:
            return period
    return 0


def has_loop(ids: list[int]) -> bool:
    return loop_period(ids) > 0


def trim_loop(ids: list[int]) -> list[int]:
    """Cut a detected loop back to a single copy of the repeated unit, so a line the
    model read twice ("X X") is kept once."""
    period = loop_period(ids)
    if not period:
        return ids
    start = len(ids) - 2 * period
    while start > 0 and ids[start - 1] == ids[start - 1 + period]:
        start -= 1
    return ids[: start + period]


def line_stats(pieces: list[str], logprobs: list[float]) -> list[tuple[float, float]]:
    """Per-line confidence from per-token pieces and log-probabilities.  A single
    unsure token (one misread letter) shows up as a low minimum even when the
    line's average looks fine."""
    out: list[tuple[float, float]] = []
    cur: list[float] = []
    has_text = False
    in_tag = False  # inside <table>-style markup: the model's formatting, not page text
    for piece, lp in zip(pieces, logprobs):
        for k, part in enumerate(piece.split("\n")):
            if k > 0:  # the token ended a line
                if has_text:
                    out.append((math.exp(min(cur)), sum(cur) / len(cur)))
                cur, has_text, in_tag = [], False, False
            visible = []
            for ch in part:
                if ch == "<":
                    in_tag = True
                elif ch == ">":
                    in_tag = False
                elif not in_tag:
                    visible.append(ch)
            if visible:
                cur.append(lp)
                has_text = has_text or any(not ch.isspace() for ch in visible)
    if has_text:
        out.append((math.exp(min(cur)), sum(cur) / len(cur)))
    return out


def weak_words(pieces: list[str], probs: list[float], threshold: float) -> list[tuple[int, str]]:
    """(line index, word) for every word holding a token below ``threshold``.
    Lines are counted like ``line_stats`` (non-empty lines only); markup is skipped."""
    out: list[tuple[int, str]] = []
    line, word, low, in_tag, line_has_text = 0, [], 1.0, False, False

    def flush():
        nonlocal word, low
        if word and low < threshold:
            out.append((line, "".join(word)))
        word, low = [], 1.0

    for piece, p in zip(pieces, probs):
        for ch in piece:
            if ch == "\n":
                flush()
                if line_has_text:
                    line += 1
                line_has_text, in_tag = False, False
            elif ch == "<":
                flush()
                in_tag = True
            elif ch == ">":
                in_tag = False
            elif in_tag:
                continue
            elif ch.isspace():
                flush()
            else:
                word.append(ch)
                low = min(low, p)
                line_has_text = True
    flush()
    return out


class OCRModel:
    """Loads the OCR model once and recognises batches of image crops."""

    def __init__(self):
        self.model = None
        self.processor = None
        self.device = "cpu"
        self.dtype = None
        self.model_id = ""
        self.loaded_signature: tuple | None = None
        self.lock = threading.RLock()
        self.last_used = 0.0

    # ------------------------------------------------------------------ #
    def signature(self, settings) -> tuple:
        hw, ms = settings.hardware, settings.models
        return (ms.ocr_model, ms.ocr_revision, hw.device, hw.gpu_index, hw.dtype, hw.attention, hw.quantization, hw.torch_compile)

    @property
    def is_loaded(self) -> bool:
        return self.model is not None

    def load(self, settings, progress: Callable[[dict], None] | None = None) -> None:
        with self.lock:
            sig = self.signature(settings)
            if self.model is not None and sig == self.loaded_signature:
                return
            self.unload()
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor

            hub.configure_environment(settings.models)
            if progress:
                progress({"stage": "model", "message": "Locating OCR model…"})
            path = hub.ensure_model(settings.models.ocr_model, settings.models, settings.models.ocr_revision, progress, "Arabic-GLM-OCR")
            device, dtype = hardware.resolve(settings.hardware)
            if progress:
                progress({"stage": "model", "message": f"Loading OCR model on {hardware.describe(device)}…"})
            kwargs: dict = {"dtype": dtype}
            attn = settings.hardware.attention
            if attn != "auto":
                kwargs["attn_implementation"] = attn
            quant = settings.hardware.quantization
            if quant != "none" and device.startswith("cuda"):
                from transformers import BitsAndBytesConfig

                if quant == "8bit":
                    kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
                else:
                    kwargs["quantization_config"] = BitsAndBytesConfig(
                        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=dtype
                    )
                kwargs["device_map"] = {"": device}
            t0 = time.time()
            processor = AutoProcessor.from_pretrained(path)
            try:
                model = AutoModelForImageTextToText.from_pretrained(path, **kwargs)
            except (ValueError, ImportError) as exc:
                if "attn_implementation" in kwargs:
                    log.warning("Attention '%s' unavailable (%s); using default.", attn, exc)
                    kwargs.pop("attn_implementation")
                    model = AutoModelForImageTextToText.from_pretrained(path, **kwargs)
                else:
                    raise
            if "device_map" not in kwargs:
                model = model.to(device)
            model.eval()
            if settings.hardware.torch_compile:
                try:
                    model.forward = torch.compile(model.forward, mode="reduce-overhead", fullgraph=False)
                except Exception as exc:  # pragma: no cover
                    log.warning("torch.compile failed: %s", exc)
            self.model, self.processor = model, processor
            self.device, self.dtype = device, dtype
            self.model_id = settings.models.ocr_model
            self.loaded_signature = sig
            self.last_used = time.time()
            log.info("OCR model loaded on %s (%s) in %.1fs", device, dtype, time.time() - t0)

    def unload(self) -> None:
        with self.lock:
            if self.model is None:
                return
            self.model = None
            self.processor = None
            self.loaded_signature = None
            import gc

            gc.collect()
            hardware.empty_cache()

    # ------------------------------------------------------------------ #
    def recognize(self, images: list[Image.Image], budgets: list[int], settings, cancel: Callable[[], bool] | None = None) -> list[Recognition]:
        """Recognise a batch of crops. ``budgets`` caps new tokens per crop."""
        if not images:
            return []
        with self.lock:
            if self.model is None:
                raise RuntimeError("OCR model is not loaded")
            import torch
            from transformers import LogitsProcessor, LogitsProcessorList, StoppingCriteria, StoppingCriteriaList

            rec = settings.recognition
            prompt = rec.prompt or "Text Recognition:"
            conversations = [
                [{"role": "user", "content": [{"type": "image", "image": im.convert("RGB")}, {"type": "text", "text": prompt}]}]
                for im in images
            ]
            processor = self.processor
            template_kwargs = dict(tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt")
            if len(conversations) > 1:
                try:
                    inputs = processor.apply_chat_template(conversations, processor_kwargs={"padding": True}, **template_kwargs)
                except TypeError:  # older transformers
                    inputs = processor.apply_chat_template(conversations, padding=True, **template_kwargs)
            else:
                inputs = processor.apply_chat_template(conversations[0], **template_kwargs)
            inputs.pop("token_type_ids", None)
            inputs = inputs.to(self.device)
            if "pixel_values" in inputs and inputs["pixel_values"].dtype != self.dtype and self.dtype is not None:
                inputs["pixel_values"] = inputs["pixel_values"].to(self.dtype)
            prompt_len = inputs["input_ids"].shape[1]
            batch = len(images)
            beams = rec.num_beams if rec.decoding == "beam" else 1

            logprobs: list[list[float]] = [[] for _ in range(batch)]
            stopped_loop = [False] * batch
            budget_hit = [False] * batch

            class Recorder(LogitsProcessor):
                def __call__(self, input_ids, scores):
                    if beams == 1:
                        lp = torch.log_softmax(scores.float(), dim=-1).max(dim=-1).values.tolist()
                        for i, v in enumerate(lp):
                            logprobs[i].append(v)
                    return scores

            class Stopper(StoppingCriteria):
                def __call__(self, input_ids, scores, **kwargs):
                    rows = input_ids.shape[0]
                    done = torch.zeros(rows, dtype=torch.bool, device=input_ids.device)
                    if cancel is not None and cancel():
                        done[:] = True
                        return done
                    gen = input_ids[:, prompt_len:]
                    length = gen.shape[1]
                    for r in range(rows):
                        i = r // beams
                        if length >= budgets[i]:
                            done[r] = True
                            budget_hit[i] = True
                        elif length >= 16 and length % 4 == 0 and has_loop(gen[r].tolist()):
                            done[r] = True
                            stopped_loop[i] = True
                    return done

            gen_kwargs: dict = {
                "max_new_tokens": int(max(budgets)),
                "logits_processor": LogitsProcessorList([Recorder()]),
                "stopping_criteria": StoppingCriteriaList([Stopper()]),
            }
            if rec.decoding == "sampling":
                gen_kwargs.update(do_sample=True, temperature=rec.temperature, top_p=rec.top_p)
            elif rec.decoding == "beam":
                gen_kwargs.update(do_sample=False, num_beams=rec.num_beams, early_stopping=True)
            else:
                gen_kwargs.update(do_sample=False, num_beams=1)
            if rec.repetition_penalty and rec.repetition_penalty != 1.0:
                gen_kwargs["repetition_penalty"] = rec.repetition_penalty
            t0 = time.time()
            with torch.inference_mode():
                out = self.model.generate(**inputs, **gen_kwargs)
            elapsed = time.time() - t0
            self.last_used = time.time()
            sequences = out if isinstance(out, torch.Tensor) else out.sequences
            eos_ids = set(self._eos_ids())
            results: list[Recognition] = []
            for i in range(batch):
                ids = sequences[i][prompt_len:].tolist()
                # trim padding / eos
                trimmed = []
                for t in ids:
                    if t in eos_ids:
                        break
                    trimmed.append(t)
                if stopped_loop[i]:
                    trimmed = trim_loop(trimmed)
                text = processor.decode(trimmed, skip_special_tokens=True)
                lp = logprobs[i][: len(trimmed)] if logprobs[i] else []
                conf = math.exp(sum(lp) / len(lp)) if lp else None
                pieces = self._pieces(trimmed) if lp else []
                stats = line_stats(pieces, lp) if lp else []
                results.append(
                    Recognition(
                        text=text,
                        confidence=conf,
                        tokens=len(trimmed),
                        hit_limit=budget_hit[i] and not stopped_loop[i],
                        looped=stopped_loop[i],
                        seconds=elapsed / batch,
                        line_stats=stats,
                        pieces=pieces,
                        probs=[math.exp(v) for v in lp] if pieces else [],
                    )
                )
            return results

    def _pieces(self, ids: list[int]) -> list[str]:
        tok = getattr(self.processor, "tokenizer", None) or self.processor
        return tok.batch_decode([[t] for t in ids], skip_special_tokens=True)

    def _eos_ids(self) -> list[int]:
        cfg = getattr(self.model, "generation_config", None)
        eos = getattr(cfg, "eos_token_id", None)
        if eos is None:
            return []
        ids = list(eos) if isinstance(eos, (list, tuple)) else [eos]
        pad = getattr(cfg, "pad_token_id", None)
        if pad is not None:
            ids.append(pad)
        return ids
