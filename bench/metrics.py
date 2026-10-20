"""Scoring OCR output against gold text: character and word accuracy.

The gold standard follows its own conventions, so both sides are normalised before
they are compared, and only differences in the text itself count:

* Arabic-Indic and Persian digits become Western digits,
* presentation forms and compatibility characters are unfolded (NFKC),
* vowel marks (tashkīl) and tatweel are removed (the gold text has none, the
  model writes what it sees),
* quotation marks are unified, and spacing around punctuation is ignored.

``raw`` scores compare the texts as they are (whitespace collapsed only).
"""

from __future__ import annotations

import re
import unicodedata

DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ࣓-ࣿ]")
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})
PUNCT = "،؛:.!؟?,;()[]{}\"'«»-–—=*/"


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).translate(DIGITS).translate(QUOTES)
    t = DIACRITICS.sub("", t).replace("ـ", "")
    t = re.sub(r"\s*([%s])\s*" % re.escape(PUNCT), r"\1", t)
    return " ".join(t.split())


def raw(text: str) -> str:
    return " ".join(text.split())


def edit_distance(a: str | list, b: str | list) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def score(pred: str, gold: str) -> dict:
    """Errors and lengths of one line, normalised and raw."""
    p, g = normalize(pred), normalize(gold)
    pw, gw = p.split(), g.split()
    return {
        "char_errors": edit_distance(p, g),
        "chars": len(g),
        "word_errors": edit_distance(pw, gw),
        "words": len(gw),
        "raw_char_errors": edit_distance(raw(pred), raw(gold)),
        "raw_chars": len(raw(gold)),
    }


def accuracy(rows: list[dict]) -> dict:
    """Corpus accuracy (1 - error rate), errors summed over all lines."""
    def rate(err: str, n: str) -> float:
        total = sum(r[n] for r in rows)
        return 1.0 - sum(r[err] for r in rows) / total if total else 0.0

    return {
        "char_accuracy": rate("char_errors", "chars"),
        "word_accuracy": rate("word_errors", "words"),
        "raw_char_accuracy": rate("raw_char_errors", "raw_chars"),
        "lines": len(rows),
    }
