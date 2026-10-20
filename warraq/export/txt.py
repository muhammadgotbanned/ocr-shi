"""Plain-text export with labelled marginal notes and footnotes.

Layout of every page::

    ========== Page 1 ==========
    [Header] …running header…

    …main text…

    ------marginal notes-------
    [M1] (right margin) …

    ------footnotes-------
    [1] …
    [2] …
"""

from __future__ import annotations

import os
from pathlib import Path

from . import SIDE_WORDS


def render_page(view, settings, translated: dict | None = None) -> str:
    out = settings.output
    fs, ms = settings.footnotes, settings.margins
    lines: list[str] = []
    sep = out.page_separator.replace("{n}", str(view.number)).replace("{label}", view.printed_number or str(view.number))
    if sep.strip():
        lines.append(sep)
    if out.include_headers_in_txt:
        for h in view.headers:
            lines.append(f"[Header] {h}")
        if view.headers:
            lines.append("")
    body = _t(translated, "body", view.body)
    if body:
        lines.append(body)
    margins_block: list[str] = []
    if view.margins:
        margins_block.append("")
        margins_block.append(ms.section_header)
        for m in view.margins:
            pos = f"({SIDE_WORDS.get(m.side or '', m.side)}) " if ms.include_position and m.side else ""
            text = _t(translated, ("margin", m.region_id), m.text)
            margins_block.append(f"{m.label} {pos}{_one_paragraph(text)}")
    foot_block: list[str] = []
    if view.footnotes:
        foot_block.append("")
        foot_block.append(fs.section_header)
        for f in view.footnotes:
            text = _t(translated, ("footnote", f.label), f.text)
            foot_block.append(f"{f.label} {_one_paragraph(text)}")
    if ms.placement == "after_footnotes":
        lines.extend(foot_block)
        lines.extend(margins_block)
    else:
        lines.extend(margins_block)
        lines.extend(foot_block)
    if out.include_headers_in_txt and view.footers:
        lines.append("")
        for f in view.footers:
            lines.append(f"[Footer] {f}")
    return "\n".join(lines).rstrip()


def _one_paragraph(text: str) -> str:
    return " ".join(part.strip() for part in text.split("\n") if part.strip())


def _t(translated, key, default):
    if translated is None:
        return default
    return translated.get(key, default)


def render(views, settings, translations: list[dict] | None = None) -> str:
    pages = []
    for i, v in enumerate(views):
        tr = translations[i] if translations else None
        pages.append(render_page(v, settings, tr))
    return "\n\n\n".join(pages).strip() + "\n"


def write(path: Path, views, settings, translations: list[dict] | None = None) -> Path:
    text = render(views, settings, translations)
    write_text(path, text, settings)
    return path


def write_text(path: Path, text: str, settings) -> None:
    out = settings.output
    eol = {"lf": "\n", "crlf": "\r\n"}.get(out.line_endings, os.linesep)
    data = text.replace("\r\n", "\n")
    if eol != "\n":
        data = data.replace("\n", eol)
    Path(path).write_text(data, encoding=out.txt_encoding, newline="")
