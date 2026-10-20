"""A self-contained, beautiful reading view (.html)."""

from __future__ import annotations

import html
from pathlib import Path

from ..core.document import TITLE
from . import SIDE_WORDS

CSS = """
:root{--bg:#f7f4ee;--paper:#fffdf9;--ink:#1d1a16;--muted:#7a7166;--rule:#e6dfd3;--accent:#9a7b3f;--note:#f3eee4}
@media (prefers-color-scheme:dark){:root{--bg:#0e0e11;--paper:#16161b;--ink:#ece7de;--muted:#9a948a;--rule:#2a2a31;--accent:#d8b46a;--note:#1d1d23}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Inter","Segoe UI",system-ui,sans-serif}
header.top{position:sticky;top:0;backdrop-filter:blur(12px);background:color-mix(in srgb,var(--bg) 80%,transparent);border-bottom:1px solid var(--rule);padding:14px 24px;display:flex;gap:16px;align-items:baseline;z-index:2}
header.top h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.02em}
header.top span{color:var(--muted);font-size:13px}
main{max-width:1180px;margin:0 auto;padding:32px 20px 80px}
.page{background:var(--paper);border:1px solid var(--rule);border-radius:18px;padding:36px 44px;margin:0 0 28px;box-shadow:0 1px 0 rgba(0,0,0,.02),0 20px 40px -30px rgba(0,0,0,.25)}
.page .no{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:18px;display:flex;justify-content:space-between}
.cols{display:grid;gap:36px}
.cols.two{grid-template-columns:1fr 1fr}
.ar{direction:rtl;text-align:justify;font-family:"Amiri","Noto Naskh Arabic","Traditional Arabic","Scheherazade New","Geeza Pro",serif;font-size:22px;line-height:2.05}
.tr{font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-size:17px;line-height:1.75}
.ar p,.tr p{margin:0 0 .9em}
.ar h3{font-size:25px;margin:.2em 0 .6em;color:var(--accent);font-weight:700}
.hdr{color:var(--muted);font-size:16px;text-align:center;margin-bottom:14px}
.notes{margin-top:22px;padding-top:14px;border-top:1px solid var(--rule)}
.notes h4{font:600 11px/1 "Inter",system-ui,sans-serif;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
.notes .item{font-size:.82em;line-height:1.9;margin:0 0 6px}
.lbl{color:var(--accent);font-weight:700;margin-inline-end:.35em;font-family:"Inter",system-ui,sans-serif;font-size:.8em}
.side{color:var(--muted);font-size:.75em}
.margin-box{background:var(--note);border-radius:12px;padding:12px 16px}
footer{color:var(--muted);text-align:center;font-size:12px;padding:20px}
@media (max-width:820px){.cols.two{grid-template-columns:1fr}.page{padding:24px 20px}}
@media print{header.top{display:none}.page{box-shadow:none;break-after:page}}
"""


def _p(text: str) -> str:
    return "".join(f"<p>{html.escape(line)}</p>" for line in text.split("\n") if line.strip())


def render(views, settings, doc, translations: list[dict] | None = None) -> str:
    ms, fs = settings.margins, settings.footnotes
    parts = []
    for i, v in enumerate(views):
        tr = translations[i] if translations else None
        ar = []
        for h in v.headers:
            ar.append(f'<div class="hdr">{html.escape(h)}</div>')
        for b in v.blocks:
            ar.append(f"<h3>{html.escape(b.text)}</h3>" if b.kind == TITLE else _p(b.text))
        if v.margins:
            items = "".join(
                f'<div class="item"><span class="lbl">{html.escape(m.label)}</span>'
                + (f'<span class="side">({SIDE_WORDS.get(m.side or "", m.side)})</span> ' if ms.include_position and m.side else "")
                + f"{html.escape(' '.join(m.text.split()))}</div>"
                for m in v.margins
            )
            ar.append(f'<div class="notes margin-box"><h4>{html.escape(ms.section_header.strip("- "))}</h4>{items}</div>')
        if v.footnotes:
            items = "".join(f'<div class="item"><span class="lbl">{html.escape(f.label)}</span>{html.escape(f.text)}</div>' for f in v.footnotes)
            ar.append(f'<div class="notes"><h4>{html.escape(fs.section_header.strip("- "))}</h4>{items}</div>')
        arabic = f'<div class="ar" lang="ar" dir="rtl">{"".join(ar)}</div>'
        cols = arabic
        cls = "cols"
        if tr:
            tparts = [_p(tr.get("body", ""))]
            margin_tr = [(m.label, tr.get(("margin", m.region_id))) for m in v.margins if tr.get(("margin", m.region_id))]
            if margin_tr:
                tparts.append('<div class="notes margin-box">' + "".join(f'<div class="item"><span class="lbl">{html.escape(l)}</span>{html.escape(t)}</div>' for l, t in margin_tr) + "</div>")
            foot_tr = [(f.label, tr.get(("footnote", f.label))) for f in v.footnotes if tr.get(("footnote", f.label))]
            if foot_tr:
                tparts.append('<div class="notes">' + "".join(f'<div class="item"><span class="lbl">{html.escape(l)}</span>{html.escape(t)}</div>' for l, t in foot_tr) + "</div>")
            cols = f'<div class="tr" dir="auto">{"".join(tparts)}</div>{arabic}'
            cls = "cols two"
        printed = f"<span>{html.escape(v.printed_number)}</span>" if v.printed_number else "<span></span>"
        parts.append(f'<section class="page" id="p{v.number}"><div class="no"><span>Page {v.number}</span>{printed}</div><div class="{cls}">{cols}</div></section>')
    title = html.escape(Path(doc.name).stem)
    return (
        f'<!doctype html><html lang="ar"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{CSS}</style></head><body>"
        f'<header class="top"><h1>{title}</h1><span>{len(views)} pages · Warraq</span></header><main>{"".join(parts)}</main>'
        f"<footer>Generated locally by Warraq</footer></body></html>"
    )


def write(path: Path, views, settings, doc, translations=None) -> Path:
    Path(path).write_text(render(views, settings, doc, translations), encoding="utf-8")
    return Path(path)
