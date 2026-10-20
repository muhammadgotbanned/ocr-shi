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


def _two_pages(first_foot, second_foot, second_notes=None):
    p1 = Page(index=0, source_index=0, source_path="b", width=1000, height=1400)
    p1.regions = [region("p1-b", BODY, "متن", 100), region("p1-f", FOOTNOTE, first_foot, 1100)]
    p2 = Page(index=1, source_index=1, source_path="b", width=1000, height=1400)
    p2.regions = [region("p2-b", BODY, "متن", 100), region("p2-f", FOOTNOTE, second_foot, 1100, notes=second_notes)]
    return Document(name="b", source_path="b", pages=[p1, p2])


def test_seen_equals_after_a_finished_note_is_not_a_continuation():
    # the image detector saw an "=" but the previous page's last note ended properly:
    # this page's first note must not be glued onto it
    doc = _two_pages("(١) انظر: الكامل ٢/٣٤.", "قال الذهبي في السير.\n(٢) رواه مسلم.", ["starts_with_equals"])
    views = assemble(doc, Settings())
    assert views[0].footnotes[0].text == "انظر: الكامل ٢/٣٤."
    assert [f.label for f in views[1].footnotes] == ["[1]", "[2]"]
    assert views[1].footnotes[0].text == "قال الذهبي في السير."


def test_seen_equals_after_an_open_note_is_merged():
    doc = _two_pages("(١) وقد سار المصنف على طريقة", "المتقدمين في التصنيف.\n(١) رواه مسلم.", ["starts_with_equals"])
    views = assemble(doc, Settings())
    assert views[0].footnotes[0].text == "وقد سار المصنف على طريقة المتقدمين في التصنيف."
    assert [f.text for f in views[1].footnotes] == ["رواه مسلم."]


def test_unnumbered_text_before_note_one_continues_previous_page():
    # neither the model nor the image showed the "=", but nothing else can come before note 1
    doc = _two_pages("(٣) وقد سار المصنف على طريقة المتقدمين.", "ثم ختم بذكر الموضوع.\n(١) رواه مسلم.")
    views = assemble(doc, Settings())
    assert views[0].footnotes[0].text.endswith("ثم ختم بذكر الموضوع.")
    assert [f.label for f in views[1].footnotes] == ["[1]"]


def test_unnumbered_text_before_note_two_is_note_one():
    # the marker (١) was misread: the note keeps its place and the numbers stay right
    doc = _two_pages("(٣) انظر الكامل.", "ا) انظر السير.\n(٢) رواه مسلم.")
    views = assemble(doc, Settings())
    assert [f.label for f in views[1].footnotes] == ["[1]", "[2]"]
    assert views[1].footnotes[1].text == "رواه مسلم."
    assert views[0].footnotes[0].text == "انظر الكامل."


def test_line_counters_and_catchwords_stay_out_of_the_text():
    from warraq.core import layout as L
    from warraq.core.document import FOOTER

    page = Page(index=0, source_index=0, source_path="b", width=1000, height=1400)
    counter = region("c", MARGIN, "١٠", 400, side="left")
    note = region("n", MARGIN, "بلغ", 500, side="left")
    catch = Region(id="w", kind=FOOTER, label="catchword", bbox=(100, 1300, 200, 1340), text="فقال")
    page.regions = [region("b", BODY, "متن الصفحة", 100), counter, note, catch]
    L.refine_after_ocr(page.regions, None, page)
    assert counter.kind == FOOTER and note.kind == MARGIN
    s = Settings()
    s.layout.headers_footers = "inline"
    view = assemble(Document(name="b", source_path="b", pages=[page]), s)[0]
    assert view.body == "متن الصفحة" and [m.text for m in view.margins] == ["بلغ"]
