from warraq.core.assemble import assemble, parse_footnote_area
from warraq.core.document import BODY, FOOTNOTE, HEADER, MARGIN, Document, Page, Region
from warraq.export import txt
from warraq.settings import Settings


def region(rid, kind, text, y, notes=None, side=None):
    return Region(id=rid, kind=kind, label="text", bbox=(100, y, 900, y + 50), text=text, notes=notes or [], side=side)


def make_doc():
    p1 = Page(index=0, source_index=0, source_path="book.pdf", width=1000, height=1400)
    p1.regions = [
        region("p1-h", HEADER, "مقدمة في علم الحديث — ٥", 20),
        region("p1-b", BODY, "الحمد لله رب العالمين (١)\nوالصلاة والسلام (٢)", 100),
        region("p1-m", MARGIN, "حاشية: هذا القول\nمنقول عن جماعة", 300, side="right"),
        region(
            "p1-f",
            FOOTNOTE,
            "(١) انظر: مقدمة ابن الصلاح.\n(٢) قال الحافظ ابن حجر في هدي الساري:\nاتفق العلماء على ذلك.\n(٣) وقد سار المصنف على طريقة المتقدمين، =",
            1100,
            notes=["ends_with_equals"],
        ),
    ]
    p2 = Page(index=1, source_index=1, source_path="book.pdf", width=1000, height=1400)
    p2.regions = [
        region("p2-b", BODY, "وأول ما ينبغي للطالب (١)", 100),
        region("p2-f", FOOTNOTE, "= ثم ختم بذكر الموضوع وبيان علاماته.\n(١) رواه مسلم في مقدمة صحيحه.", 1100, notes=["starts_with_equals"]),
    ]
    return Document(name="book.pdf", source_path="book.pdf", pages=[p1, p2])


def test_parse_basic_and_continuation():
    s = Settings()
    area = parse_footnote_area([("= تتمة الحاشية.", "r"), ("(١) أولى.", "r"), ("(٢) ثانية", "r"), ("تكملة الثانية.", "r")], s)
    assert area.continuation == "تتمة الحاشية."
    assert [n.number for n in area.notes] == [1, 2]
    assert area.notes[1].text == "ثانية تكملة الثانية."


def test_visual_equals_when_model_dropped_sign():
    s = Settings()
    area = parse_footnote_area([("ثم ختم بذكر الموضوع.", "r"), ("(١) رواه مسلم.", "r")], s, starts_with_equals=True)
    assert area.continuation == "ثم ختم بذكر الموضوع."
    assert len(area.notes) == 1


def test_non_sequential_numbers_do_not_split():
    s = Settings()
    area = parse_footnote_area([("(١) قال تعالى في سورة البقرة", "r"), ("(٢٥٥) وهي آية الكرسي.", "r")], s)
    assert len(area.notes) == 1


def test_inline_notes_split():
    s = Settings()
    area = parse_footnote_area([("(١) انظر الكفاية. (٢) انظر التدريب.", "r")], s)
    assert [n.text for n in area.notes] == ["انظر الكفاية.", "انظر التدريب."]


def test_merge_continuation_into_previous_page():
    s = Settings()
    doc = make_doc()
    views = assemble(doc, s)
    assert [f.label for f in views[0].footnotes] == ["[1]", "[2]", "[3]"]
    assert views[0].footnotes[2].text == "وقد سار المصنف على طريقة المتقدمين، ثم ختم بذكر الموضوع وبيان علاماته."
    assert [f.label for f in views[1].footnotes] == ["[1]"]
    assert doc.pages[0].footnotes[-1].continues_on_next
    out = txt.render(views, s)
    assert "------footnotes-------\n[1] انظر: مقدمة ابن الصلاح." in out
    assert "[2] قال الحافظ ابن حجر في هدي الساري: اتفق العلماء على ذلك." in out
    assert "------marginal notes-------\n[M1] (right margin) حاشية: هذا القول منقول عن جماعة" in out
    assert "= ثم ختم" not in out
    assert "[Header] مقدمة في علم الحديث — ٥" in out


def test_mark_mode_keeps_continuation_on_its_page():
    s = Settings()
    s = s.model_copy(update={"footnotes": s.footnotes.model_copy(update={"continuation_mode": "mark"})})
    views = assemble(make_doc(), s)
    assert views[1].footnotes[0].label == "[3 continued]"
    assert views[1].footnotes[0].text == "ثم ختم بذكر الموضوع وبيان علاماته."
    assert views[0].footnotes[2].text == "وقد سار المصنف على طريقة المتقدمين،"


def test_document_numbering_and_link_markers():
    s = Settings()
    s = s.model_copy(update={"footnotes": s.footnotes.model_copy(update={"numbering": "document", "link_body_markers": True})})
    views = assemble(make_doc(), s)
    assert [f.label for f in views[1].footnotes] == ["[4]"]
    assert "(١)" not in views[1].body and "[4]" in views[1].body
    assert views[0].body.startswith("الحمد لله رب العالمين [1]")


def test_digits_made_consistent():
    s = Settings()
    doc = make_doc()
    doc.pages[1].regions[1].text = "= تتمة.\n(1) رواه مسلم ص 12."
    views = assemble(doc, s)
    assert views[1].footnotes[0].text == "رواه مسلم ص ١٢."
