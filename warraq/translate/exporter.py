"""Write translated and bilingual files."""

from __future__ import annotations

import re
from pathlib import Path

from ..export import SIDE_WORDS
from ..export import txt as txt_export


def _lang_tag(settings) -> str:
    name = settings.translation.target_language.strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "translation"


def render_translated_txt(views, settings, translations) -> str:
    """Same layout as the Arabic text file, but translated."""
    fs, ms, out = settings.footnotes, settings.margins, settings.output
    pages = []
    for v, tr in zip(views, translations):
        lines = []
        sep = out.page_separator.replace("{n}", str(v.number)).replace("{label}", v.printed_number or str(v.number))
        lines.append(sep)
        body = tr.get("body", "")
        if body:
            lines.append(body)
        if v.margins:
            lines += ["", ms.section_header]
            for m in v.margins:
                pos = f"({SIDE_WORDS.get(m.side or '', m.side)}) " if ms.include_position and m.side else ""
                lines.append(f"{m.label} {pos}{tr.get(('margin', m.region_id), '')}".rstrip())
        if v.footnotes:
            lines += ["", fs.section_header]
            for f in v.footnotes:
                lines.append(f"{f.label} {tr.get(('footnote', f.label), '')}".rstrip())
        pages.append("\n".join(lines))
    return "\n\n\n".join(pages).strip() + "\n"


def render_bilingual_txt(views, settings, translations) -> str:
    fs, ms, out = settings.footnotes, settings.margins, settings.output
    pages = []
    for v, tr in zip(views, translations):
        lines = [out.page_separator.replace("{n}", str(v.number)).replace("{label}", v.printed_number or str(v.number))]
        if v.body:
            lines += [v.body, "", "— " + settings.translation.target_language + " —", tr.get("body", "")]
        if v.margins:
            lines += ["", ms.section_header]
            for m in v.margins:
                lines.append(f"{m.label} {' '.join(m.text.split())}")
                if tr.get(("margin", m.region_id)):
                    lines.append(f"     {tr[('margin', m.region_id)]}")
        if v.footnotes:
            lines += ["", fs.section_header]
            for f in v.footnotes:
                lines.append(f"{f.label} {' '.join(f.text.split())}")
                if tr.get(("footnote", f.label)):
                    lines.append(f"     {tr[('footnote', f.label)]}")
        pages.append("\n".join(lines))
    return "\n\n\n".join(pages).strip() + "\n"


def write_translations(out_dir: Path, base: str, views, settings, translations, emit=None) -> dict[str, str]:
    emit = emit or (lambda e: None)
    tag = _lang_tag(settings)
    results: dict[str, str] = {}
    path = Path(out_dir) / f"{base}.{tag}.txt"
    txt_export.write_text(path, render_translated_txt(views, settings, translations), settings)
    results["translation_txt"] = str(path)
    emit({"type": "export", "format": "translation_txt", "path": str(path)})
    if settings.translation.bilingual:
        bpath = Path(out_dir) / f"{base}.bilingual.txt"
        txt_export.write_text(bpath, render_bilingual_txt(views, settings, translations), settings)
        results["bilingual_txt"] = str(bpath)
        from ..export import html_export

        hpath = Path(out_dir) / f"{base}.bilingual.html"

        class _Doc:  # minimal stand-in carrying the name
            name = base

        html_export.write(hpath, views, settings, _Doc, translations)
        results["bilingual_html"] = str(hpath)
        emit({"type": "export", "format": "bilingual_html", "path": str(hpath)})
        if "docx" in settings.output.formats:
            from ..export import docx_export

            dpath = Path(out_dir) / f"{base}.bilingual.docx"
            docx_export.write(dpath, views, settings, None, translations)
            results["bilingual_docx"] = str(dpath)
    return results
