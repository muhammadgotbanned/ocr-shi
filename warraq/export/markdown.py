"""Markdown export."""

from __future__ import annotations

from pathlib import Path

from ..core.document import TITLE
from . import SIDE_WORDS


def render(views, settings, doc, translations: list[dict] | None = None) -> str:
    fs, ms = settings.footnotes, settings.margins
    out = [f"# {Path(doc.name).stem}", ""]
    for i, v in enumerate(views):
        tr = translations[i] if translations else None
        title = f"Page {v.number}" + (f" · {v.printed_number}" if v.printed_number else "")
        out += [f"## {title}", ""]
        for h in v.headers:
            out += [f"*{h}*", ""]
        for b in v.blocks:
            text = b.text
            if b.kind == TITLE:
                out += [f"### {text.replace(chr(10), ' ')}", ""]
            else:
                out += [text.replace("\n", "  \n"), ""]
        if tr and tr.get("body"):
            out += ["> " + tr["body"].replace("\n", "\n> "), ""]
        if v.margins:
            out += [f"**{ms.section_header.strip('- ')}**", ""]
            for m in v.margins:
                pos = f" ({SIDE_WORDS.get(m.side or '', m.side)})" if ms.include_position and m.side else ""
                out.append(f"- **{m.label}**{pos} {' '.join(m.text.split())}")
            out.append("")
        if v.footnotes:
            out += ["---", "", f"**{fs.section_header.strip('- ')}**", ""]
            for f in v.footnotes:
                out.append(f"- **{f.label}** {' '.join(f.text.split())}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def write(path: Path, views, settings, doc, translations=None) -> Path:
    Path(path).write_text(render(views, settings, doc, translations), encoding="utf-8")
    return Path(path)
