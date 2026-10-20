"""Local translation of OCR results.

Backends
--------
* ``local``   – a translation/chat model run with transformers on this machine
                (default: Tencent Hy-MT2-1.8B; Hy-MT2-7B or any chat model works).
* ``openai``  – any OpenAI-compatible server on this machine: Ollama, LM Studio,
                llama.cpp, vLLM…
* ``seq2seq`` – classic MT models (NLLB-200, Marian/opus-mt).

The page structure is kept: the main text, each footnote and each marginal
note are translated separately, so labels like [1] or [M1] survive.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Callable

from ..models import hardware, hub

log = logging.getLogger(__name__)

STYLE_TEXT = {
    "faithful": "faithful and precise, staying close to the Arabic wording and sentence structure",
    "fluent": "natural, fluent and idiomatic",
    "scholarly": "scholarly and academic; keep established technical terms (e.g. ḥadīth, isnād, fiqh) in careful transliteration",
}

NLLB_CODES = {
    "english": "eng_Latn", "french": "fra_Latn", "german": "deu_Latn", "spanish": "spa_Latn", "turkish": "tur_Latn",
    "urdu": "urd_Arab", "persian": "pes_Arab", "farsi": "pes_Arab", "malay": "zsm_Latn", "indonesian": "ind_Latn",
    "russian": "rus_Cyrl", "chinese": "zho_Hans", "italian": "ita_Latn", "portuguese": "por_Latn", "dutch": "nld_Latn",
    "bengali": "ben_Beng", "hindi": "hin_Deva", "swahili": "swh_Latn", "hausa": "hau_Latn", "somali": "som_Latn",
    "bosnian": "bos_Latn", "albanian": "als_Latn", "japanese": "jpn_Jpan", "korean": "kor_Hang", "arabic": "arb_Arab",
}


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #


def build_prompt(text: str, settings, context: str = "") -> str:
    tr = settings.translation
    parts: list[str] = []
    if context:
        parts.append(f"[Background Information]\n{context}\n")
    glossary = [g for g in tr.glossary if len(g) >= 2 and g[0] and g[1]]
    if glossary:
        parts.append("Reference the following translations:\n" + "\n".join(f"{a} translates to {b}" for a, b in glossary) + "\n")
    style = STYLE_TEXT.get(tr.style, STYLE_TEXT["faithful"])
    instruction = (
        f"Translate the following {tr.source_language} text into {tr.target_language}. "
        f"The translation style must be {style}. "
        "Keep footnote markers such as (١) or [1] where they are, written with Western digits. "
        "Note that you should only output the translated result without any additional explanation."
    )
    if context:
        instruction += " Use the background information only for context; do not translate it."
    if tr.instructions.strip():
        instruction += " " + tr.instructions.strip()
    parts.append(instruction)
    parts.append(f"\n[Source Text]\n{text}" if context else f"\n{text}")
    return "\n".join(parts)


def clean_translation(out: str) -> str:
    out = out.strip()
    out = re.sub(r"^<think>.*?</think>\s*", "", out, flags=re.S)
    out = re.sub(r"^(?:\[?Translation\]?|Translated text)\s*:\s*", "", out, flags=re.I)
    return out.strip()


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


class LocalTranslator:
    _lock = threading.RLock()
    _cache: dict = {}

    def __init__(self, settings, progress: Callable[[dict], None] | None = None):
        self.settings = settings
        self.progress = progress
        self.model = None
        self.tokenizer = None
        self.device = "cpu"

    def load(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tr = self.settings.translation
        key = (tr.model, self.settings.hardware.device, self.settings.hardware.dtype)
        with self._lock:
            if key in self._cache:
                self.model, self.tokenizer, self.device = self._cache[key]
                return
            self._cache.clear()
            hardware.empty_cache()
            hub.configure_environment(self.settings.models)
            path = hub.ensure_model(tr.model, self.settings.models, "main", self.progress, "Translator")
            device, dtype = hardware.resolve(self.settings.hardware)
            if self.progress:
                self.progress({"stage": "model", "message": f"Loading translator on {hardware.describe(device)}…"})
            tok = AutoTokenizer.from_pretrained(path)
            model = AutoModelForCausalLM.from_pretrained(path, dtype=dtype).to(device).eval()
            self.model, self.tokenizer, self.device = model, tok, device
            self._cache[key] = (model, tok, device)

    def translate(self, text: str, context: str = "") -> str:
        import torch

        tr = self.settings.translation
        prompt = build_prompt(text, self.settings, context if tr.use_context else "")
        messages = [{"role": "user", "content": prompt}]
        try:
            inputs = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
        except TypeError:  # pragma: no cover - older tokenizers
            ids = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
            inputs = {"input_ids": ids, "attention_mask": torch.ones_like(ids)}
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        max_new = int(min(4096, max(256, len(text) * 3)))
        kwargs = dict(max_new_tokens=max_new, repetition_penalty=1.05)
        if tr.temperature > 0:
            kwargs.update(do_sample=True, temperature=tr.temperature, top_p=0.6, top_k=20)
        else:
            kwargs.update(do_sample=False)
        with self._lock, torch.inference_mode():
            out = self.model.generate(**inputs, **kwargs)
        gen = out[0][inputs["input_ids"].shape[1] :]
        return clean_translation(self.tokenizer.decode(gen, skip_special_tokens=True))


class OpenAITranslator:
    def __init__(self, settings, progress=None):
        self.settings = settings

    def load(self):
        pass

    def translate(self, text: str, context: str = "") -> str:
        import httpx

        tr = self.settings.translation
        prompt = build_prompt(text, self.settings, context if tr.use_context else "")
        headers = {"Content-Type": "application/json"}
        if tr.api_key:
            headers["Authorization"] = f"Bearer {tr.api_key}"
        body = {
            "model": tr.api_model,
            "messages": [
                {"role": "system", "content": "You are a meticulous professional translator of classical and modern Arabic."},
                {"role": "user", "content": prompt},
            ],
            "temperature": tr.temperature,
            "stream": False,
        }
        url = tr.api_base.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            r = client.post(url, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        return clean_translation(data["choices"][0]["message"]["content"])


class Seq2SeqTranslator:
    _cache: dict = {}

    def __init__(self, settings, progress=None):
        self.settings = settings
        self.progress = progress

    def load(self):
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        tr = self.settings.translation
        key = tr.seq2seq_model
        if key in self._cache:
            self.model, self.tokenizer, self.device = self._cache[key]
            return
        hub.configure_environment(self.settings.models)
        path = hub.ensure_model(tr.seq2seq_model, self.settings.models, "main", self.progress, "Translator")
        device, dtype = hardware.resolve(self.settings.hardware)
        is_nllb = "nllb" in tr.seq2seq_model.lower()
        tok = AutoTokenizer.from_pretrained(path, src_lang="arb_Arab") if is_nllb else AutoTokenizer.from_pretrained(path)
        model = AutoModelForSeq2SeqLM.from_pretrained(path, dtype=dtype).to(device).eval()
        self.model, self.tokenizer, self.device = model, tok, device
        self._cache.clear()
        self._cache[key] = (model, tok, device)

    def translate(self, text: str, context: str = "") -> str:
        import torch

        tr = self.settings.translation
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=900).to(self.device)
        kwargs = dict(max_new_tokens=min(1024, int(len(text) * 2.5) + 32), num_beams=4)
        if "nllb" in tr.seq2seq_model.lower():
            code = NLLB_CODES.get(tr.target_language.strip().lower(), "eng_Latn")
            kwargs["forced_bos_token_id"] = self.tokenizer.convert_tokens_to_ids(code)
        with torch.inference_mode():
            out = self.model.generate(**inputs, **kwargs)
        return self.tokenizer.decode(out[0], skip_special_tokens=True).strip()


def make_translator(settings, progress=None):
    engine = settings.translation.engine
    if engine == "openai":
        return OpenAITranslator(settings, progress)
    if engine == "seq2seq":
        return Seq2SeqTranslator(settings, progress)
    return LocalTranslator(settings, progress)


# --------------------------------------------------------------------------- #
# Document translation
# --------------------------------------------------------------------------- #


def _paragraphs(text: str) -> str:
    """Join visual lines into running text, keeping blank-line paragraph breaks."""
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "\n\n".join(paras)


def _chunks(text: str, limit: int) -> list[str]:
    """Split running text into chunks at sentence boundaries."""
    out: list[str] = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            out.append(para)
            continue
        sentences = re.split(r"(?<=[.!؟?؛:])\s+", para)
        cur = ""
        for s in sentences:
            if len(cur) + len(s) + 1 > limit and cur:
                out.append(cur)
                cur = s
            else:
                cur = (cur + " " + s).strip()
            while len(cur) > limit:  # one enormous sentence: hard split on spaces
                cut = cur.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                out.append(cur[:cut])
                cur = cur[cut:].strip()
        if cur:
            out.append(cur)
    return [c for c in out if c.strip()]


def translate_views(views, settings, emit: Callable[[dict], None] | None = None, cancel: Callable[[], bool] | None = None) -> list[dict]:
    emit = emit or (lambda e: None)
    cancel = cancel or (lambda: False)
    tr = settings.translation

    def progress(info: dict):
        emit({"type": "model", **info})

    translator = make_translator(settings, progress)
    translator.load()
    emit({"type": "model", "stage": "ready", "device": getattr(translator, "device", "")})
    # work list: (page_idx, key, text)
    work: list[tuple[int, object, str]] = []
    for i, v in enumerate(views):
        body = _paragraphs(v.body)
        if body:
            work.append((i, "body", body))
        if tr.translate_margins:
            for m in v.margins:
                work.append((i, ("margin", m.region_id), " ".join(m.text.split())))
        if tr.translate_footnotes:
            for f in v.footnotes:
                work.append((i, ("footnote", f.label), " ".join(f.text.split())))
    results: list[dict] = [{} for _ in views]
    total = sum(max(1, len(_chunks(t, tr.max_chunk_chars))) for _, _, t in work) or 1
    done = 0
    context = ""
    emit({"type": "translate", "done": 0, "total": total, "message": "Translating…"})
    for page_idx, key, text in work:
        if cancel():
            break
        pieces = _chunks(text, tr.max_chunk_chars) or [text]
        translated = []
        for piece in pieces:
            if cancel():
                break
            try:
                translated.append(translator.translate(piece, context if key == "body" else ""))
            except Exception as exc:
                log.exception("translation failed")
                translated.append(f"[translation failed: {exc}]")
            done += 1
            emit({"type": "translate", "done": done, "total": total, "page": page_idx})
            if key == "body":
                context = piece[-600:]
        results[page_idx][key] = "\n\n".join(translated)
    return results
