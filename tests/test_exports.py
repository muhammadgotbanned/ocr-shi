import json
import zipfile

from lxml import etree

from warraq.core.assemble import assemble
from warraq.export import docx_export, html_export, json_export, markdown
from warraq.export.docx_export import _PPR_ORDER, _RPR_ORDER
from warraq.settings import Settings

from test_footnotes import make_doc

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _ordered(children, order):
    idx = [order.index(c.tag.replace(W, "")) for c in children if c.tag.replace(W, "") in order]
    return idx == sorted(idx)


def test_docx_is_schema_ordered_and_rtl(tmp_path):
    s = Settings()
    doc = make_doc()
    path = tmp_path / "out.docx"
    docx_export.write(path, assemble(doc, s), s, doc)
    xml = zipfile.ZipFile(path).read("word/document.xml")
    root = etree.fromstring(xml)
    for ppr in root.iter(W + "pPr"):
        assert _ordered(list(ppr), _PPR_ORDER), [c.tag for c in ppr]
    for rpr in root.iter(W + "rPr"):
        assert _ordered(list(rpr), _RPR_ORDER), [c.tag for c in rpr]
    assert root.find(f".//{W}bidi") is not None
    assert "الحمد لله رب العالمين" in xml.decode("utf-8")


def test_markdown_html_json(tmp_path):
    s = Settings()
    doc = make_doc()
    views = assemble(doc, s)
    md = markdown.render(views, s, doc)
    assert "## Page 1" in md and "**[1]** انظر: مقدمة ابن الصلاح." in md
    page = html_export.render(views, s, doc)
    assert 'dir="rtl"' in page and "حاشية" in page
    jpath = tmp_path / "o.json"
    json_export.write(jpath, doc, views, s)
    data = json.loads(jpath.read_text(encoding="utf-8"))
    assert data["pages"][0]["assembled"]["footnotes"][2]["merged_continuation_from_page"] == 2
