"""Arabic text clean-up for raw model output."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Iterable

ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
WESTERN = "0123456789"
ALL_DIGITS = ARABIC_INDIC + PERSIAN_DIGITS + WESTERN

TO_WESTERN = str.maketrans(ARABIC_INDIC + PERSIAN_DIGITS, WESTERN * 2)
TO_ARABIC_INDIC = str.maketrans(WESTERN + PERSIAN_DIGITS, ARABIC_INDIC * 2)

TATWEEL = "ـ"
# Harakat, Quranic annotation marks, superscript alef and extended marks.
DIACRITICS_RE = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e8\u06ea-\u06ed\u08d3-\u08e1\u08e3-\u08ff]")
BIDI_CONTROLS_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c]")
ZERO_WIDTH_RE = re.compile(r"[\u200b\ufeff]")


def to_western_digits(text: str) -> str:
    return text.translate(TO_WESTERN)


def to_arabic_digits(text: str) -> str:
    return text.translate(TO_ARABIC_INDIC)


def is_arabic_char(ch: str) -> bool:
    return unicodedata.bidirectional(ch) in ("AL", "R")


def arabic_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if is_arabic_char(c)) / len(letters)


# --------------------------------------------------------------------------- #
# Markup produced by the model
# --------------------------------------------------------------------------- #

_TABLE_HINT = re.compile(r"<\s*(table|tr|td|th|html|body)\b", re.I)


def _cells_from_row(row_html: str) -> list[str]:
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
    if not cells:
        stripped = re.sub(r"<[^>]+>", " ", row_html)
        return [stripped] if stripped.strip() else []
    return [re.sub(r"<[^>]+>", " ", c) for c in cells]


def html_to_text(raw: str) -> str:
    """Turn the model's occasional HTML (mostly fake one-row tables) into plain lines.

    GLM-OCR sometimes wraps small blocks — margin notes, headers — in
    ``<table><tr><td>`` markup where each cell is really one text line.  Real
    multi-row tables become one line per row with cells joined by `` | ``.
    """
    if not _TABLE_HINT.search(raw):
        text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        text = re.sub(r"</?(p|div|span|b|i|u|strong|em|sup|sub)\b[^>]*>", "", text, flags=re.I)
        return html.unescape(text)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", raw, flags=re.I | re.S)
    if not rows:
        rows = [raw]
    parsed = [[" ".join(c.split()) for c in _cells_from_row(r)] for r in rows]
    parsed = [[c for c in row if c] for row in parsed]
    parsed = [row for row in parsed if row]
    lines: list[str] = []
    if len(parsed) == 1:
        lines = parsed[0]  # one row of cells == lines of a small block
    else:
        for row in parsed:
            lines.append(" | ".join(row) if len(row) > 1 else row[0])
    # anything outside the table rows (rare)
    outside = re.sub(r"<tr[^>]*>.*?</tr>", " ", raw, flags=re.I | re.S)
    outside = re.sub(r"<[^>]+>", " ", outside)
    outside = " ".join(outside.split())
    text = "\n".join(lines)
    if outside and outside not in text:
        text = (text + "\n" + outside).strip()
    return html.unescape(text)


def strip_markdown(text: str) -> str:
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"^```\w*\s*$", "", text, flags=re.M)
    text = re.sub(r"^\$\$\s*$", "", text, flags=re.M)
    return text


# --------------------------------------------------------------------------- #
# Runaway repetition
# --------------------------------------------------------------------------- #

_PUNCT_RUN = re.compile(r"(?:[\s.·…\-–—_،,:;]*[.·…_]){12,}[\s.·…\-–—_،,:;]*$")


def collapse_repetitions(text: str) -> tuple[str, bool]:
    """Remove degenerate loops the model can fall into. Returns (text, changed)."""
    original = text
    # 1. trailing junk made of dots / dashes / spaces (keep one sentence-final dot)
    text = _PUNCT_RUN.sub(lambda m: m.group(0)[:1] if m.group(0)[:1] in ".…" else "", text).rstrip()
    # 2. one character repeated absurdly (e.g. "٢٢٢٢٢٢٢٢٢٢٢٢٢")
    text = re.sub(r"([^\s.\-_ـ=*])\1{11,}", r"\1", text)
    # 3. a phrase repeated back-to-back three or more times
    text = _collapse_phrase_loops(text)
    # 4. identical lines repeated; a line that starts over with a stray first letter
    lines = [_drop_restart_letter(l) for l in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if len(out) >= 2 and line.strip() and line.strip() == out[-1].strip() == out[-2].strip():
            continue
        out.append(line)
    text = "\n".join(out)
    # 5. hyphen-joined number runs ("١٢٢-١٢٤-١٢٥-...") that the model invents
    text = re.sub(r"(?:[0-9٠-٩]{2,4}[-–]){8,}[0-9٠-٩]*\S*", "", text).rstrip()
    return text, text != original


def trailing_echo(line: str) -> int:
    """Number of words to drop when a line ends with its last 2-4 words said twice
    ("وإن جاز استعماله جاز استعماله"), else 0.  Some texts repeat on purpose
    ("الله أكبر الله أكبر"), so callers check the line's length before dropping."""
    words = line.split()
    for k in range(4, 1, -1):
        if len(words) >= 2 * k + 1 and words[-k:] == words[-2 * k : -k]:
            return k
    return 0


def trailing_restart(line: str) -> int:
    """Number of words to drop when a line ends by starting itself over and getting cut
    off ("… خلق عليها، باب الطه"), else 0.  Only meaningful for readings that were
    stopped (loop or token budget): a finished line may well end like it began."""
    words = line.split()
    for k in range(min(4, len(words) // 3), 0, -1):
        tail, head = words[-k:], words[:k]
        if tail[:-1] == head[:-1] and head[-1].startswith(tail[-1]) and len(head[-1]) > len(tail[-1]):
            return k
    return 0


def _drop_restart_letter(line: str) -> str:
    """A lone letter after the end of a line that is the line's own first letter is
    the model starting over ("… خلق عليها، ب"); no Arabic line ends in a lone letter."""
    words = line.split()
    if len(words) >= 4 and len(words[-1]) == 1 and words[-1] == words[0][0] and len(words[0]) > 1 and words[-1].isalpha():
        return line.rstrip()[:-1].rstrip()
    return line


def _collapse_phrase_loops(text: str, min_unit: int = 6, min_repeats: int = 3) -> str:
    # Work on whitespace-separated tokens so we never cut a word in half.
    tokens = text.split(" ")
    n = len(tokens)
    if n < min_repeats * 2:
        return text
    i = 0
    result: list[str] = []
    while i < n:
        collapsed = False
        for size in range(1, min(40, (n - i) // min_repeats) + 1):
            unit = tokens[i : i + size]
            if len(" ".join(unit)) < min_unit:
                continue
            reps = 1
            while tokens[i + reps * size : i + (reps + 1) * size] == unit:
                reps += 1
            if reps >= min_repeats:
                result.extend(unit)
                i += reps * size
                collapsed = True
                break
        if not collapsed:
            result.append(tokens[i])
            i += 1
    return " ".join(result)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def normalize_presentation_forms(text: str) -> str:
    """Map Arabic Presentation Forms-A/B to base letters (keeps ﷺ-style word ligatures)."""

    def fix(ch: str) -> str:
        cp = ord(ch)
        if (0xFB50 <= cp <= 0xFDCF) or (0xFE70 <= cp <= 0xFEFF and cp != 0xFEFF):
            return unicodedata.normalize("NFKC", ch)
        return ch

    if not any(0xFB50 <= ord(c) <= 0xFEFF for c in text):
        return text
    return "".join(fix(c) for c in text)


# Scripts that never occur in Arabic books (CJK, kana, Hangul, Indic, Thai, private use).
# The model was trained from a Chinese base and sometimes emits one of these on a smudge.
FOREIGN_SCRIPT_RE = re.compile(
    r"[\u0900-\u0dff\u0e00-\u0eff\u1100-\u11ff\u2e80-\u2fdf\u3001-\u30ff\u3100-\u318f\u31a0-\u31ff"
    r"\u3400-\u4dbf\u4e00-\u9fff\ua000-\ua4cf\uac00-\ud7af\ue000-\uf8ff\uf900-\ufaff\ufffd]"
)
# full-width punctuation the model borrows from Chinese, mapped to its Arabic/ASCII equivalent
_FULLWIDTH = {0xFF0C: "،", 0xFF1B: "؛", 0xFF1F: "؟", 0x3002: ".", 0x3001: "،", 0x3000: " "}
_FULLWIDTH.update({cp: chr(cp - 0xFEE0) for cp in range(0xFF01, 0xFF5F) if cp not in _FULLWIDTH})


def foreign_char_count(text: str) -> int:
    return len(FOREIGN_SCRIPT_RE.findall(text.translate(_FULLWIDTH)))


def drop_foreign_scripts(text: str) -> tuple[str, bool]:
    """Map full-width punctuation to normal forms and delete characters of unrelated scripts."""
    text = text.translate(_FULLWIDTH)
    cleaned = FOREIGN_SCRIPT_RE.sub("", text)
    if cleaned == text:
        return text, False
    cleaned = "\n".join(re.sub(r"  +", " ", l).strip() for l in cleaned.split("\n"))
    return cleaned, True


def remove_diacritics(text: str) -> str:
    return DIACRITICS_RE.sub("", text)


def drop_stray_quotes(text: str) -> str:
    """Remove a lone quotation mark the model sometimes appends to a line."""
    out = []
    for line in text.split("\n"):
        for q in ('"', "”", "“"):
            if line.count(q) == 1 and line.rstrip().endswith(q):
                line = line.rstrip()[:-1].rstrip()
            elif line.count(q) == 1 and line.lstrip().startswith(q) and len(line.strip()) > 1:
                line = line.lstrip()[1:].lstrip()
        out.append(line)
    return "\n".join(out)


def fix_punctuation_spacing(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line)
        line = re.sub(r" +([،؛؟!.:,;)\]}»])", r"\1", line)
        line = re.sub(r"([(\[{«]) +", r"\1", line)
        # a space after Arabic comma/semicolon when a letter follows directly
        line = re.sub(r"([،؛])(?=[^\s\d٠-٩)\]»،؛.])", r"\1 ", line)
        lines.append(line.strip())
    return "\n".join(lines)


def convert_quotes(text: str, style: str) -> str:
    """Straight double quotes → « », alternating open/close within each paragraph.
    The model writes every quotation mark as a plain ", so this restores the book's style."""
    if style != "guillemets" or '"' not in text:
        return text
    open_q, close_q = "«", "»"
    out = []
    for para in text.split("\n\n"):
        chars, opened = [], False
        for ch in para:
            if ch == '"':
                ch = close_q if opened else open_q
                opened = not opened
            chars.append(ch)
        out.append("".join(chars))
    return "\n\n".join(out)


def apply_replacements(text: str, rules: Iterable[Iterable[str]]) -> str:
    for rule in rules or []:
        rule = list(rule)
        if len(rule) < 2 or not rule[0]:
            continue
        find, repl = rule[0], rule[1]
        if find.startswith("re:"):
            try:
                text = re.sub(find[3:], repl, text)
            except re.error:
                continue
        else:
            text = text.replace(find, repl)
    return text


_SENTENCE_END = re.compile(r"[.!؟?:»\"”)\]]\s*$")


def reflow_paragraphs(text: str) -> str:
    """Join wrapped lines into paragraphs (used when line_mode == 'paragraphs')."""
    lines = [l.strip() for l in text.split("\n")]
    if len(lines) <= 1:
        return text.strip()
    lengths = sorted(len(l) for l in lines if l)
    typical = lengths[len(lengths) // 2] if lengths else 0
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
        short = typical and len(line) < 0.6 * typical
        if _SENTENCE_END.search(line) and short:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def clean_model_output(raw: str, settings) -> tuple[str, list[str]]:
    """Full clean-up pipeline for one region. Returns (text, notes)."""
    notes: list[str] = []
    text = raw or ""
    text = ZERO_WIDTH_RE.sub("", text)
    text = BIDI_CONTROLS_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    t = settings.text
    if t.strip_markup:
        if "<" in text and ">" in text:
            text = html_to_text(text)
        text = strip_markdown(text)
    if t.collapse_repeats:
        text, changed = collapse_repetitions(text)
        if changed:
            notes.append("removed repeated output")
        text = drop_stray_quotes(text)
    if t.drop_foreign_scripts:
        text, changed = drop_foreign_scripts(text)
        if changed:
            notes.append("removed misread characters")
    if t.normalize_presentation_forms:
        text = normalize_presentation_forms(text)
    if not t.keep_diacritics:
        text = remove_diacritics(text)
    if t.remove_tatweel:
        text = text.replace(TATWEEL, "")
    if t.digits == "western":
        text = to_western_digits(text)
    elif t.digits == "arabic_indic":
        text = to_arabic_digits(text)
    if t.fix_punctuation_spacing:
        text = fix_punctuation_spacing(text)
    text = convert_quotes(text, t.quotes)
    text = apply_replacements(text, t.replacements)
    # tidy blank lines
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, notes
