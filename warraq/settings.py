"""Application settings.

Every option is declared once here, with the metadata the GUI needs to render
it (title, description, options, ranges, "advanced" flag).  The settings
screen in the web UI is generated from :func:`ui_schema`, so adding a field
here is all it takes to expose a new option.
"""

from __future__ import annotations

import copy
import json
import threading
import typing
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import paths


def opt(default: Any, title: str, description: str = "", **ui: Any) -> Any:
    """Declare a setting with UI metadata."""
    return Field(default, title=title, description=description, json_schema_extra=ui or None)


class Section(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


class GeneralSettings(Section):
    ui_language: Literal["en", "ar"] = opt(
        "en", "Interface language", labels={"en": "English", "ar": "العربية"}
    )
    theme: Literal["system", "dark", "light"] = opt(
        "system", "Theme", labels={"system": "Match system", "dark": "Dark", "light": "Light"}
    )
    accent: Literal["gold", "jade", "lapis", "rose", "ink"] = opt(
        "gold",
        "Accent colour",
        labels={"gold": "Illuminated gold", "jade": "Jade", "lapis": "Lapis", "rose": "Rose", "ink": "Ink"},
    )
    output_dir: str = opt(
        "",
        "Output folder",
        "Where finished files are written. Leave empty to use Documents/Warraq.",
        widget="folder",
    )
    open_folder_when_done: bool = opt(False, "Open the output folder when a job finishes")
    keep_models_loaded: bool = opt(
        True,
        "Keep models in memory between jobs",
        "Faster start for the next job. Turn off to free GPU memory after every job.",
    )
    unload_after_minutes: int = opt(
        30, "Unload idle models after (minutes)", "0 keeps them loaded forever.", min=0, max=1440, step=5, advanced=True
    )
    notify_when_done: bool = opt(True, "Play a soft chime when a job finishes")


class HardwareSettings(Section):
    device: Literal["auto", "cuda", "mps", "cpu"] = opt(
        "auto",
        "Compute device",
        "Auto picks an NVIDIA GPU (CUDA) on Windows, the Apple GPU (Metal) on Macs, otherwise the CPU.",
        labels={"auto": "Automatic", "cuda": "NVIDIA GPU (CUDA)", "mps": "Apple GPU (Metal)", "cpu": "CPU only"},
    )
    gpu_index: int = opt(0, "GPU index", "Which CUDA GPU to use when several are installed.", min=0, max=15, advanced=True)
    dtype: Literal["auto", "bfloat16", "float16", "float32"] = opt(
        "auto",
        "Precision",
        "Auto uses bfloat16 on modern NVIDIA GPUs, float16 on older GPUs and Apple Silicon, float32 on CPU.",
        labels={"auto": "Automatic", "bfloat16": "bfloat16", "float16": "float16", "float32": "float32 (slowest, most exact)"},
    )
    attention: Literal["auto", "sdpa", "eager", "flash_attention_2"] = opt(
        "auto",
        "Attention kernel",
        "flash_attention_2 needs the flash-attn package (NVIDIA only).",
        labels={"auto": "Automatic", "sdpa": "PyTorch SDPA", "eager": "Eager (most compatible)", "flash_attention_2": "FlashAttention 2"},
        advanced=True,
    )
    quantization: Literal["none", "8bit", "4bit"] = opt(
        "none",
        "Quantization",
        "Reduce GPU memory with bitsandbytes (NVIDIA only). Usually unnecessary: the OCR model needs ~2.5 GB.",
        labels={"none": "None (best accuracy)", "8bit": "8-bit", "4bit": "4-bit NF4"},
        advanced=True,
    )
    layout_device: Literal["same", "cpu"] = opt(
        "same",
        "Layout model device",
        "Run the page-layout detector on the CPU to leave all GPU memory for OCR.",
        labels={"same": "Same as OCR", "cpu": "CPU"},
        advanced=True,
    )
    cpu_threads: int = opt(0, "CPU threads", "0 = let PyTorch decide.", min=0, max=128, advanced=True)
    torch_compile: bool = opt(
        False, "torch.compile the OCR model", "Experimental. Can speed up long jobs on Linux/NVIDIA.", advanced=True
    )


class ModelSettings(Section):
    ocr_model: str = opt(
        "sherif1313/Arabic-GLM-OCR-v2",
        "OCR model",
        "Hugging Face model id or a local folder. Default: Arabic-GLM-OCR-v2 (GLM-OCR fine-tuned for Arabic).",
    )
    ocr_revision: str = opt("main", "OCR model revision", advanced=True)
    layout_model: str = opt(
        "PaddlePaddle/PP-DocLayoutV3_safetensors",
        "Layout model",
        "Detects body text, marginal notes, footnotes, headers and page numbers.",
        advanced=True,
    )
    cache_dir: str = opt("", "Model cache folder", "Leave empty for the standard Hugging Face cache.", widget="folder", advanced=True)
    offline: bool = opt(False, "Offline mode", "Never contact Hugging Face; only use models already downloaded.")
    hf_endpoint: str = opt("", "Hugging Face mirror", "e.g. https://hf-mirror.com — leave empty for huggingface.co.", advanced=True)
    hf_token: str = opt("", "Hugging Face token", "Only needed for private or gated models.", widget="secret", advanced=True)


class InputSettings(Section):
    pdf_dpi: int = opt(300, "PDF render resolution (DPI)", "300 is ideal for scans; use 400 for tiny print.", min=100, max=600, step=25)
    use_embedded_images: bool = opt(
        True,
        "Use the original scan inside PDFs",
        "When a PDF page is a single scanned image, read it at its native resolution instead of re-rendering.",
    )
    page_range: str = opt("", "Pages to process", "e.g. 1-20, 25, 30-  (empty = all pages)", placeholder="all")
    rotate: Literal[0, 90, 180, 270] = opt(
        0, "Rotate pages", labels={0: "No rotation", 90: "90° clockwise", 180: "180°", 270: "90° counter-clockwise"}
    )
    split_spreads: Literal["off", "auto", "always"] = opt(
        "auto",
        "Split two-page spreads",
        "Detect scans that contain two facing pages and process them as separate pages.",
        labels={"off": "Off", "auto": "Automatic", "always": "Always split"},
    )
    spread_order: Literal["rtl", "ltr"] = opt(
        "rtl",
        "Reading order of a spread",
        "Arabic books: the right-hand page comes first.",
        labels={"rtl": "Right page first", "ltr": "Left page first"},
    )
    deskew: bool = opt(True, "Straighten skewed scans")
    max_skew_angle: float = opt(8.0, "Maximum skew to correct (degrees)", min=1, max=20, step=0.5, advanced=True)
    crop_borders: bool = opt(True, "Remove dark scanner borders")
    enhance: Literal["none", "auto", "strong"] = opt(
        "auto",
        "Image enhancement",
        "Evens out yellowed paper, shadows and faded ink. 'Strong' helps old manuscripts.",
        labels={"none": "None", "auto": "Gentle (recommended)", "strong": "Strong (aged manuscripts)"},
    )
    denoise: bool = opt(False, "Remove speckle noise", advanced=True)
    binarize: bool = opt(
        False, "Convert to black & white", "Rarely helps this model; keep off unless pages are very noisy.", advanced=True
    )


class LayoutSettings(Section):
    mode: Literal["auto", "full_page"] = opt(
        "auto",
        "Page analysis",
        "Auto finds columns, marginal notes and footnotes. Full page reads everything as one block.",
        labels={"auto": "Automatic layout analysis", "full_page": "Whole page as one block"},
    )
    document_type: Literal["auto", "printed", "manuscript"] = opt(
        "auto",
        "Document type",
        "Tunes margin-note and footnote detection.",
        labels={"auto": "Detect automatically", "printed": "Printed book", "manuscript": "Manuscript"},
    )
    threshold: float = opt(0.3, "Region detection confidence", min=0.05, max=0.9, step=0.05, advanced=True)
    reading_order: Literal["rtl_columns", "model", "top_to_bottom"] = opt(
        "rtl_columns",
        "Reading order",
        "How text blocks are ordered on the page.",
        labels={"rtl_columns": "Right-to-left columns (Arabic)", "model": "Layout model's order", "top_to_bottom": "Top to bottom"},
    )
    detect_margin_notes: bool = opt(True, "Detect marginal notes (ḥawāshī)")
    detect_footnotes: bool = opt(True, "Detect footnotes")
    straighten_rotated_text: bool = opt(
        True, "Straighten rotated margin notes", "Marginalia written diagonally or vertically are rotated level before reading."
    )
    use_polygons: bool = opt(False, "Use precise region outlines", "Masks neighbouring text out of each region crop.", advanced=True)
    region_padding: int = opt(14, "Region padding (px)", min=0, max=80, advanced=True)
    headers_footers: Literal["label", "drop", "inline"] = opt(
        "label",
        "Running headers, footers & page numbers",
        labels={"label": "Keep, labelled separately", "drop": "Remove", "inline": "Keep inside the main text"},
    )
    read_figures: bool = opt(False, "Read text inside images, seals and stamps", advanced=True)


class RecognitionSettings(Section):
    prompt: str = opt(
        "Text Recognition:",
        "Model prompt",
        "The task prompt Arabic-GLM-OCR was trained with. Changing it usually hurts accuracy.",
        advanced=True,
    )
    decoding: Literal["greedy", "beam", "sampling"] = opt(
        "greedy",
        "Decoding",
        "Greedy is the most faithful for OCR. Beam search can help on damaged text but is slower.",
        labels={"greedy": "Greedy (recommended)", "beam": "Beam search", "sampling": "Sampling"},
    )
    num_beams: int = opt(3, "Beams", min=2, max=8, advanced=True, show_if={"recognition.decoding": "beam"})
    temperature: float = opt(0.4, "Temperature", min=0.05, max=1.5, step=0.05, advanced=True, show_if={"recognition.decoding": "sampling"})
    top_p: float = opt(0.9, "Top-p", min=0.1, max=1.0, step=0.05, advanced=True, show_if={"recognition.decoding": "sampling"})
    repetition_penalty: float = opt(
        1.0,
        "Repetition penalty",
        "Keep at 1.0: Arabic legitimately repeats words and diacritics, and a penalty makes the model skip lines.",
        min=1.0,
        max=1.5,
        step=0.01,
        advanced=True,
    )
    max_new_tokens: int = opt(4096, "Maximum tokens per region", min=128, max=16384, step=128, advanced=True)
    adaptive_token_budget: bool = opt(
        True, "Adaptive token budget", "Limit generation length to what the region can physically contain.", advanced=True
    )
    granularity: Literal["auto", "lines", "blocks"] = opt(
        "lines",
        "Reading granularity",
        "Line by line is the most reliable, and on a GPU much faster: lines are read in parallel batches. "
        "Blocks give the reader a paragraph of context but can skip or mix lines; every block is checked and "
        "re-read line by line when in doubt.",
        labels={"lines": "Line by line (recommended)", "auto": "Paragraphs as blocks", "blocks": "Blocks of lines, always"},
    )
    max_lines_per_chunk: int = opt(
        12,
        "Lines per block",
        "Tall regions are cut between lines into blocks of at most this many lines.",
        min=6,
        max=60,
        advanced=True,
    )
    min_block_lines: int = opt(
        3,
        "Smallest block",
        "Regions with fewer lines are read line by line. Blocks give the model context; every block is checked and re-read line by line if it slips.",
        min=2,
        max=30,
        advanced=True,
    )
    footnote_lines_per_chunk: int = opt(
        1,
        "Footnote lines per chunk",
        "Footnotes are read line by line by default so every (١), (٢) marker stays where it belongs.",
        min=1,
        max=30,
        advanced=True,
    )
    target_line_height: int = opt(
        76,
        "Normalise text height (px)",
        "Small print is enlarged so a text line is about this tall. 0 disables. Larger is more accurate but slower.",
        min=0,
        max=160,
        step=4,
    )
    max_upscale: float = opt(2.5, "Maximum enlargement", min=1.0, max=4.0, step=0.1, advanced=True)
    max_pixels: int = opt(
        4_000_000, "Maximum pixels per crop", min=500_000, max=12_000_000, step=250_000, advanced=True
    )
    batch_size: int = opt(
        0,
        "Batch size",
        "Lines/regions recognised together on the GPU. 0 = automatic (1 on CPU, 2–8 on GPUs depending on memory).",
        min=0,
        max=32,
    )
    verify_and_retry: bool = opt(
        True,
        "Self-check every region",
        "Compares the number of lines read with the lines on the page and detects runaway loops; failed regions are re-read in smaller pieces.",
    )
    reread_doubtful: bool = opt(
        True,
        "Re-read doubtful lines",
        "A line containing a letter the model was unsure of is read again on its own, a little larger, and the more confident reading is kept.",
        show_if={"recognition.verify_and_retry": True},
    )
    doubt_threshold: float = opt(
        0.12,
        "Doubt threshold",
        "A line is doubtful when the model's confidence in any single token falls below this. Higher re-reads more lines (slower, more careful).",
        min=0.01,
        max=0.6,
        step=0.01,
        advanced=True,
        show_if={"recognition.verify_and_retry": True, "recognition.reread_doubtful": True},
    )
    mark_uncertain_below: float = opt(
        0.5,
        "Highlight words with confidence below",
        "Words holding a letter the model was less sure of than this are underlined in the reading view for review. "
        "0.7 underlines about half of all reading errors, at the cost of more false alarms.",
        min=0.0,
        max=0.95,
        step=0.05,
        advanced=True,
    )
    flag_confidence_below: float = opt(
        0.55, "Flag regions with confidence below", "Flagged regions are highlighted for review.", min=0.0, max=1.0, step=0.05
    )


class TextSettings(Section):
    keep_diacritics: bool = opt(True, "Keep diacritics (tashkīl)")
    remove_tatweel: bool = opt(False, "Remove tatweel (ـ)")
    normalize_presentation_forms: bool = opt(
        True, "Normalise Arabic presentation forms", "Converts legacy ligature code points to plain letters so text is searchable."
    )
    digits: Literal["auto", "keep", "arabic_indic", "western"] = opt(
        "auto",
        "Digits",
        "The model sometimes writes ١٢٣ as 123. 'Consistent' makes every number follow the style used most in the document.",
        labels={
            "auto": "Consistent with the book (recommended)",
            "keep": "Exactly as the model wrote them",
            "arabic_indic": "Always Arabic-Indic (٠١٢٣)",
            "western": "Always Western (0123)",
        },
    )
    fix_punctuation_spacing: bool = opt(True, "Tidy spacing around punctuation")
    quotes: Literal["keep", "guillemets"] = opt(
        "keep",
        "Quotation marks",
        "The model writes every quotation mark as a straight \". If your book uses « », restore them here.",
        labels={"keep": 'Straight " " (as read)', "guillemets": "Arabic « »"},
    )
    line_mode: Literal["preserve", "paragraphs"] = opt(
        "preserve",
        "Line breaks",
        "Preserve keeps the page's lines (best for manuscripts). Paragraphs reflows lines into running text.",
        labels={"preserve": "Keep page lines", "paragraphs": "Join into paragraphs"},
    )
    strip_markup: bool = opt(True, "Remove stray HTML/Markdown from model output", advanced=True)
    collapse_repeats: bool = opt(True, "Remove runaway repetitions", advanced=True)
    drop_foreign_scripts: bool = opt(
        True,
        "Remove characters of unrelated scripts",
        "A stray Chinese or Korean character in the output is always a misread smudge; such lines are re-read and any left over removed.",
        advanced=True,
    )
    replacements: list[list[str]] = opt(
        [],
        "Find & replace rules",
        "Applied to every region, in order. Each rule is [find, replace]; prefix find with re: for a regular expression.",
        widget="pairs",
        advanced=True,
    )


class FootnoteSettings(Section):
    section_header: str = opt("------footnotes-------", "Footnote section heading")
    label_format: str = opt("[{n}]", "Footnote label", "{n} is replaced by the footnote number.")
    numbering: Literal["original", "per_page", "document"] = opt(
        "original",
        "Numbering",
        "Original keeps the book's numbers; per page restarts at 1 on every page; document numbers continuously.",
        labels={"original": "As printed", "per_page": "Restart on each page", "document": "Continuous through the document"},
    )
    label_digits: Literal["western", "arabic_indic"] = opt(
        "western", "Label digits", labels={"western": "Western [1]", "arabic_indic": "Arabic-Indic [١]"}
    )
    continuation_markers: str = opt(
        "=,＝,﹦", "Continuation markers", "Comma-separated. A footnote area that starts with one of these continues the previous page's last footnote."
    )
    continuation_mode: Literal["merge", "mark"] = opt(
        "merge",
        "Continued footnotes",
        "Merge joins the continuation onto the footnote it belongs to on the previous page.",
        labels={"merge": "Merge into the previous page's footnote", "mark": "Keep on this page, marked as continued"},
    )
    continued_label: str = opt("[{n} continued]", "Label for continued footnotes (mark mode)", advanced=True)
    strip_markers: bool = opt(True, "Remove the '=' continuation signs from the text")
    link_body_markers: bool = opt(
        False,
        "Rewrite footnote markers in the main text",
        "Turns (١) in the body into the same label used in the footnote section, e.g. [1].",
    )
    split_inline_notes: bool = opt(True, "Split several footnotes written on one line", advanced=True)


class MarginSettings(Section):
    section_header: str = opt("------marginal notes-------", "Marginal notes section heading")
    label_format: str = opt("[M{n}]", "Marginal note label")
    include_position: bool = opt(True, "Say which margin each note is in")
    placement: Literal["before_footnotes", "after_footnotes"] = opt(
        "before_footnotes",
        "Placement on the page",
        labels={"before_footnotes": "After the main text, before footnotes", "after_footnotes": "After footnotes"},
    )


class OutputSettings(Section):
    formats: list[Literal["txt", "pdf", "docx", "md", "json", "html"]] = opt(
        ["txt", "pdf"],
        "Output formats",
        widget="multiselect",
        labels={
            "txt": "Text (.txt)",
            "pdf": "Searchable PDF",
            "docx": "Word (.docx)",
            "md": "Markdown",
            "json": "JSON (with coordinates)",
            "html": "Reading view (.html)",
        },
    )
    page_separator: str = opt("========== Page {n} ==========", "Page separator", "{n} = page number, {label} = printed page number if found.")
    include_headers_in_txt: bool = opt(True, "Write running headers in the text file", advanced=True)
    txt_encoding: Literal["utf-8", "utf-8-sig"] = opt(
        "utf-8", "Text encoding", labels={"utf-8": "UTF-8", "utf-8-sig": "UTF-8 with BOM (old Windows apps)"}, advanced=True
    )
    line_endings: Literal["auto", "lf", "crlf"] = opt(
        "auto", "Line endings", labels={"auto": "Match this computer", "lf": "LF (macOS/Linux)", "crlf": "CRLF (Windows)"}, advanced=True
    )
    pdf_text_order: Literal["standard", "chromium", "word"] = opt(
        "standard",
        "PDF text layer flavour",
        "How the invisible Arabic text is laid out. Standard suits Acrobat, Preview and Firefox; Chromium suits Chrome/Edge copy-paste of brackets.",
        labels={"standard": "Standard", "chromium": "Chrome / Edge optimised", "word": "Word-by-word (OCRmyPDF style)"},
        advanced=True,
    )
    pdf_keep_original: bool = opt(
        True, "Keep original PDF pages", "Adds the text layer to the untouched original pages (best quality)."
    )
    pdf_replace_ocr_layer: bool = opt(
        True,
        "Replace an existing OCR layer",
        "Many Arabic PDFs carry a broken invisible text layer from older OCR; remove it so only Warraq's text is searchable.",
    )
    pdf_image_quality: int = opt(88, "Image quality for picture inputs (JPEG)", min=40, max=100, advanced=True)
    pdf_max_dpi: int = opt(0, "Downsample page images above (DPI)", "0 keeps full resolution.", min=0, max=600, step=25, advanced=True)
    pdf_visible_text: bool = opt(False, "Show the text layer (debugging)", advanced=True)
    docx_font: str = opt("Traditional Arabic", "Word document font")
    docx_font_size: int = opt(15, "Word font size", min=8, max=40)
    filename_template: str = opt("{name}", "File name", "{name} = input file name, {date} = today.", advanced=True)


class TranslationSettings(Section):
    enabled: bool = opt(False, "Translate after OCR")
    engine: Literal["local", "openai", "seq2seq"] = opt(
        "local",
        "Translation engine",
        "Local runs a translation model on this computer. 'OpenAI-compatible' talks to Ollama, LM Studio, llama.cpp or vLLM on this machine.",
        labels={"local": "Built-in local model", "openai": "OpenAI-compatible server (Ollama, LM Studio…)", "seq2seq": "NLLB / Marian (classic MT)"},
    )
    model: str = opt(
        "tencent/Hy-MT2-1.8B",
        "Local translation model",
        "Hy-MT2-1.8B is fast; tencent/Hy-MT2-7B is more accurate; any chat model (e.g. Qwen) also works.",
        suggestions=["tencent/Hy-MT2-1.8B", "tencent/Hy-MT2-7B", "Qwen/Qwen3-4B-Instruct-2507"],
        show_if={"translation.engine": "local"},
    )
    seq2seq_model: str = opt(
        "facebook/nllb-200-distilled-600M",
        "Classic MT model",
        suggestions=["facebook/nllb-200-distilled-600M", "facebook/nllb-200-1.3B", "Helsinki-NLP/opus-mt-ar-en"],
        show_if={"translation.engine": "seq2seq"},
    )
    api_base: str = opt("http://localhost:11434/v1", "Server URL", show_if={"translation.engine": "openai"})
    api_model: str = opt("qwen3:8b", "Server model name", show_if={"translation.engine": "openai"})
    api_key: str = opt("", "API key (if the server needs one)", widget="secret", show_if={"translation.engine": "openai"})
    source_language: str = opt("Arabic", "Source language")
    target_language: str = opt(
        "English",
        "Target language",
        suggestions=["English", "French", "German", "Spanish", "Turkish", "Urdu", "Persian", "Malay", "Indonesian", "Russian", "Chinese"],
    )
    style: Literal["faithful", "fluent", "scholarly"] = opt(
        "faithful",
        "Style",
        labels={"faithful": "Faithful (close to the Arabic)", "fluent": "Fluent", "scholarly": "Scholarly (keeps technical terms)"},
    )
    instructions: str = opt("", "Extra instructions", "e.g. 'Transliterate personal names; keep honorifics like ṣallā Allāhu ʿalayhi wa-sallam.'", widget="textarea")
    glossary: list[list[str]] = opt([], "Glossary", "Fixed translations for terms: [Arabic, translation].", widget="pairs")
    translate_footnotes: bool = opt(True, "Translate footnotes")
    translate_margins: bool = opt(True, "Translate marginal notes")
    use_context: bool = opt(True, "Give the translator the preceding passage as context", advanced=True)
    max_chunk_chars: int = opt(1200, "Characters per translation request", min=200, max=6000, step=100, advanced=True)
    temperature: float = opt(0.3, "Temperature", min=0.0, max=1.5, step=0.05, advanced=True)
    bilingual: bool = opt(True, "Also write a bilingual (Arabic + translation) file")


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    general: GeneralSettings = Field(default_factory=GeneralSettings, title="General")
    hardware: HardwareSettings = Field(default_factory=HardwareSettings, title="Hardware & GPU")
    models: ModelSettings = Field(default_factory=ModelSettings, title="Models")
    input: InputSettings = Field(default_factory=InputSettings, title="Pages & images")
    layout: LayoutSettings = Field(default_factory=LayoutSettings, title="Layout analysis")
    recognition: RecognitionSettings = Field(default_factory=RecognitionSettings, title="Recognition")
    text: TextSettings = Field(default_factory=TextSettings, title="Text clean-up")
    footnotes: FootnoteSettings = Field(default_factory=FootnoteSettings, title="Footnotes")
    margins: MarginSettings = Field(default_factory=MarginSettings, title="Marginal notes")
    output: OutputSettings = Field(default_factory=OutputSettings, title="Output")
    translation: TranslationSettings = Field(default_factory=TranslationSettings, title="Translation")


SECTION_ICONS = {
    "general": "sliders",
    "hardware": "chip",
    "models": "cube",
    "input": "image",
    "layout": "layout",
    "recognition": "eye",
    "text": "type",
    "footnotes": "footnote",
    "margins": "margin",
    "output": "download",
    "translation": "globe",
}

# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #

PRESETS: dict[str, dict[str, Any]] = {
    "printed_book": {
        "title": "Printed book",
        "description": "Typeset books with footnotes and running headers.",
        "values": {
            "layout": {"document_type": "printed", "detect_footnotes": True, "detect_margin_notes": True},
            "input": {"enhance": "auto", "pdf_dpi": 300},
            "recognition": {"granularity": "lines", "target_line_height": 76},
            "text": {"line_mode": "preserve"},
        },
    },
    "manuscript": {
        "title": "Manuscript",
        "description": "Handwritten codices with marginal notes, often rotated.",
        "values": {
            "layout": {"document_type": "manuscript", "detect_margin_notes": True, "straighten_rotated_text": True, "use_polygons": True},
            "input": {"enhance": "strong", "pdf_dpi": 350, "split_spreads": "auto"},
            "recognition": {"granularity": "lines", "target_line_height": 88},
            "text": {"line_mode": "preserve"},
        },
    },
    "fast": {
        "title": "Fast draft",
        "description": "Quick results for clean, modern print.",
        "values": {
            "input": {"enhance": "none", "pdf_dpi": 200, "deskew": False},
            "recognition": {"granularity": "lines", "target_line_height": 0, "verify_and_retry": False, "batch_size": 0},
        },
    },
    "max_accuracy": {
        "title": "Maximum accuracy",
        "description": "Slow and thorough: small chunks, large text, self-checks.",
        "values": {
            "input": {"pdf_dpi": 400, "enhance": "auto"},
            "recognition": {
                "granularity": "lines",
                "target_line_height": 96,
                "verify_and_retry": True,
                "reread_doubtful": True,
                "doubt_threshold": 0.2,
                "decoding": "greedy",
            },
        },
    },
}

# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

_lock = threading.RLock()


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_settings(path: Path | None = None) -> Settings:
    path = path or paths.settings_file()
    with _lock:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return _validate_lenient(data)
            except Exception:
                backup = path.with_suffix(".broken.json")
                try:
                    path.replace(backup)
                except OSError:
                    pass
        return Settings()


def _validate_lenient(data: dict) -> Settings:
    """Validate settings, dropping individual invalid values instead of failing."""
    try:
        return Settings.model_validate(data)
    except Exception:
        clean: dict[str, Any] = {}
        defaults = Settings()
        for section_name in Settings.model_fields:
            section_data = data.get(section_name)
            if not isinstance(section_data, dict):
                continue
            section_cls = type(getattr(defaults, section_name))
            kept: dict[str, Any] = {}
            for key, value in section_data.items():
                if key not in section_cls.model_fields:
                    continue
                try:
                    section_cls.model_validate({key: value})
                    kept[key] = value
                except Exception:
                    continue
            clean[section_name] = kept
        return Settings.model_validate(clean)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or paths.settings_file()
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def update_settings(settings: Settings, patch: dict) -> Settings:
    merged = deep_merge(settings.model_dump(mode="json"), patch)
    return Settings.model_validate(merged)


def apply_preset(settings: Settings, preset: str) -> Settings:
    if preset not in PRESETS:
        raise KeyError(preset)
    return update_settings(settings, PRESETS[preset]["values"])


# --------------------------------------------------------------------------- #
# UI schema
# --------------------------------------------------------------------------- #


def _field_type(annotation: Any) -> tuple[str, list[Any] | None]:
    origin = typing.get_origin(annotation)
    if origin is Literal:
        return "select", list(typing.get_args(annotation))
    if origin in (list, typing.List):
        (inner,) = typing.get_args(annotation) or (str,)
        if typing.get_origin(inner) is Literal:
            return "multiselect", list(typing.get_args(inner))
        if typing.get_origin(inner) in (list, typing.List):
            return "pairs", None
        return "list", None
    if annotation is bool:
        return "bool", None
    if annotation is int:
        return "int", None
    if annotation is float:
        return "float", None
    return "text", None


def ui_schema() -> list[dict[str, Any]]:
    """Describe all settings for the GUI's generated settings screen."""
    sections = []
    defaults = Settings()
    for section_name, section_field in Settings.model_fields.items():
        section_model = type(getattr(defaults, section_name))
        fields = []
        for name, field in section_model.model_fields.items():
            kind, options = _field_type(field.annotation)
            extra = dict(field.json_schema_extra or {})
            widget = extra.pop("widget", None) or kind
            labels = extra.pop("labels", None) or {}
            entry: dict[str, Any] = {
                "key": f"{section_name}.{name}",
                "name": name,
                "title": field.title or name,
                "description": field.description or "",
                "type": widget,
                "default": getattr(getattr(defaults, section_name), name),
                "advanced": bool(extra.pop("advanced", False)),
            }
            if options is not None:
                entry["options"] = [{"value": o, "label": labels.get(o, str(o))} for o in options]
            for k in ("min", "max", "step", "placeholder", "suggestions", "show_if"):
                if k in extra:
                    entry[k] = extra.pop(k)
            fields.append(entry)
        sections.append(
            {
                "key": section_name,
                "title": section_field.title or section_name,
                "icon": SECTION_ICONS.get(section_name, "sliders"),
                "fields": fields,
            }
        )
    return sections


class SettingsStore:
    """Thread-safe holder for the live settings object."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._settings = load_settings(path)
        self._lock = threading.RLock()

    @property
    def current(self) -> Settings:
        with self._lock:
            return self._settings

    def snapshot(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def update(self, patch: dict) -> Settings:
        with self._lock:
            self._settings = update_settings(self._settings, patch)
            save_settings(self._settings, self._path)
            return self._settings

    def replace(self, settings: Settings) -> Settings:
        with self._lock:
            self._settings = settings
            save_settings(self._settings, self._path)
            return self._settings

    def reset(self, section: str | None = None) -> Settings:
        with self._lock:
            if section is None:
                self._settings = Settings()
            else:
                data = self._settings.model_dump(mode="json")
                data[section] = Settings().model_dump(mode="json")[section]
                self._settings = Settings.model_validate(data)
            save_settings(self._settings, self._path)
            return self._settings
