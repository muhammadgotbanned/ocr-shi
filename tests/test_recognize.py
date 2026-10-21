import math

from warraq.core import recognize as R
from warraq.core.document import BODY, Region
from warraq.core.imaging import LineBand
from warraq.core.text import clean_model_output, drop_foreign_scripts, foreign_char_count
from warraq.models.ocr_model import Recognition, line_stats
from warraq.settings import Settings

lp = math.log


def test_line_stats_split_lines_and_ignore_markup():
    stats = line_stats(["<", "table", "><", "td", ">", "بلغ", " مقابلة", "</", "td", ">"], [lp(0.05)] * 5 + [lp(0.9), lp(0.8)] + [lp(0.1)] * 3)
    assert len(stats) == 1 and abs(stats[0][0] - 0.8) < 1e-6  # markup tokens never make a line doubtful
    stats = line_stats(["ي", "ومن", " يعمل", "\n", "وقد", " كان", "\n\n", "x"], [lp(0.04), lp(0.4), lp(0.9), lp(0.99), lp(0.6), lp(0.9), lp(0.9), lp(0.3)])
    assert [round(p, 2) for p, _ in stats] == [0.04, 0.6, 0.3]


def _chunk(text, stats, bands=1, ok=True):
    region = Region(id="r", kind=BODY, label="text", bbox=(0, 0, 100, 100))
    c = R.Chunk(region, 0, [LineBand(i * 10, i * 10 + 8, 0, 100) for i in range(bands)], (0, 0, 100, 100), ok=ok)
    c.result = Recognition(text=text, confidence=math.exp(sum(m for _, m in stats) / len(stats)), tokens=10, hit_limit=False, looped=False, line_stats=stats)
    return c


def test_patches_replace_only_more_confident_lines():
    block = _chunk("السطر الأول\nيومن يعمل بما علم\n\nالسطر الثالث", [(0.9, -0.02), (0.04, -0.15), (0.8, -0.03)], bands=3)
    good = _chunk("وأن يعمل بما علم", [(0.3, -0.05)])
    worse = _chunk("السطر", [(0.5, -0.4)])
    text, changed = R._apply_patches(block, {1: good, 2: worse})
    assert changed
    assert text == "السطر الأول\nوأن يعمل بما علم\n\nالسطر الثالث"  # blank line kept, line 3 untouched
    # a re-read that lost half the line is never taken, however confident
    short = _chunk("وأن", [(0.99, -0.001)])
    assert R._apply_patches(block, {1: short}) == (block.result.text, {})


def test_weak_words_by_line():
    from warraq.models.ocr_model import weak_words

    pieces = ["<", "td", ">", "ي", "ومن", " يعمل", "\n", "وقد", " كان", "</", "td", ">"]
    probs = [0.01, 0.01, 0.01, 0.04, 0.4, 0.9, 0.99, 0.95, 0.2, 0.01, 0.01, 0.01]
    assert weak_words(pieces, probs, 0.3) == [(0, "يومن"), (1, "كان")]


def test_review_words_match_final_text():
    assert R._review_words(["«يومن،", "كان", "كان", "غائب"], "يومن يعمل، وكان") == ["يومن", "كان"]


def test_orientation_uses_the_best_attempt():
    first = _chunk("بلغ مقابلة", [(0.9, -0.01)])
    retry = _chunk("黑白三色", [(0.2, -1.5)], ok=False)
    retry.previous = first
    job = R.RegionJob(first.region, None, (0, 0), (10, 10), -90, 30, first.bands, chunks=[retry])
    assert R._best_attempt(retry) is first
    assert R._job_score(job) == R._chunk_score(first)


def test_foreign_script_cleanup():
    assert foreign_char_count("沌 يتكلمون") == 1
    assert drop_foreign_scripts("沌 يتكلمون") == ("يتكلمون", True)
    assert drop_foreign_scripts("قال：نعم，ولا？")[0] == "قال:نعم،ولا؟"
    assert drop_foreign_scripts("Ελληνικά и русский") == ("Ελληνικά и русский", False)
    text, notes = clean_model_output("沌 يتكلمون إلا بما ينفع", Settings())
    assert text == "يتكلمون إلا بما ينفع" and "removed misread characters" in notes


def test_quotes_restored_as_guillemets():
    from warraq.core.text import convert_quotes

    assert convert_quotes('قال: "إن الماء طهور"، و"نعم"', "guillemets") == "قال: «إن الماء طهور»، و«نعم»"
    assert convert_quotes('سطر "مفتوح\nيستمر" هنا', "guillemets") == "سطر «مفتوح\nيستمر» هنا"
    assert convert_quotes('"أ"', "keep") == '"أ"'


def test_looped_output_is_trimmed_to_one_copy():
    from warraq.models.ocr_model import trim_loop

    line = list(range(100, 150))  # a 50-token line read twice, then restarted
    assert trim_loop(line + line + line[:3]) == line
    assert trim_loop([1, 2, 3] + [7] * 14) == [1, 2, 3, 7]
    assert trim_loop(list(range(60))) == list(range(60))  # no loop: untouched


def _block(lines, widths, stats=None):
    region = Region(id="r", kind=BODY, label="text", bbox=(0, 0, 1000, 100))
    bands = [LineBand(i * 40, i * 40 + 30, 1000 - w, 1000) for i, w in enumerate(widths)]
    c = R.Chunk(region, 0, bands, (0, 0, 1000, 40 * len(widths)))
    stats = stats or [(0.9, -0.02)] * len(lines)
    c.result = Recognition(text="\n".join(lines), confidence=0.97, tokens=50, hit_limit=False, looped=False, line_stats=stats)
    return c


def test_line_that_lost_words_is_flagged_and_patched():
    full = "ولا أكثر أعجوبة وتصرفا، ولا أقل تصلفا وتكلفا، ولا أبعد من مراء"
    skipped = "ولا أكثر أعجوبة وتصرفا، ولا أبعد من مراء"
    lines = [full, full, skipped, full]
    rec = R.Recognizer(None, Settings())
    block = _block(lines, [900, 900, 900, 900])
    rec.density[("r", "")] = (len(full) / 900, 900)
    rec._check_length(block)
    assert block.off_length == {2} and 2 in block.doubt
    patch = _chunk(full, [(0.8, -0.05)])
    text, used = R._apply_patches(block, {2: patch})
    assert text.split("\n")[2] == full and 2 in used


def test_short_last_line_is_not_off_length():
    full = "ولا أكثر أعجوبة وتصرفا، ولا أقل تصلفا وتكلفا، ولا أبعد من مراء"
    rec = R.Recognizer(None, Settings())
    block = _block([full, full, "حتى لا تضيع."], [900, 900, 900 * 12 / len(full)])
    rec.density[("r", "")] = (len(full) / 900, 900)
    rec._check_length(block)
    assert not block.off_length


def test_line_end_echo_dropped_only_when_the_width_says_so():
    line = "بشيء طاهر كالزعفران والصابون حتى غلب عليه، فلا يرفع الحدث عند جمهور الفقهاء، وإن جاز استعماله"
    echo = line + " جاز استعماله"
    density = len(line) / 900
    c = _block([echo], [900])
    assert R._trim_line_ends(c, echo, density) == (line, True)
    # the same repeat where the line really is that long (a repeated takbir): kept
    takbir = "حي على الصلاة حي على الفلاح قد قامت الصلاة الله أكبر الله أكبر"
    c = _block([takbir], [900])
    assert R._trim_line_ends(c, takbir, len(takbir) / 900) == (takbir, False)


def test_cut_off_restart_is_dropped():
    line = "باب الطهارة: الماء على ثلاثة أقسام، طهور وطاهر ونجس. خلق عليها،"
    c = _block([line + " باب الطه"], [900])
    assert R._trim_line_ends(c, line + " باب الطه", 0.0) == (line + " باب الطه", False)  # finished reading: kept
    c.result.hit_limit = True
    assert R._trim_line_ends(c, line + " باب الطه", 0.0) == (line, True)


def test_short_line_is_widened_right_aligned():
    from PIL import Image

    img = Image.new("RGB", (200, 100), (0, 0, 0))
    wide = R._widen_short_line(img, 1500)
    assert wide.size == (1500, 100)
    assert wide.getpixel((1499, 50)) == (0, 0, 0) and wide.getpixel((10, 50)) == (255, 255, 255)
    assert R._widen_short_line(img, 220) is img  # already about a line's width


def test_block_with_lines_mixed_up_fails_verification():
    mixed = [
        "وفي سنة إحدى وستين ومائتين، ثم سار إلى حلب فاستقبله أهلها بالترحيب، وكان معه",
        "وأقام بها شهرين، ثم سار إلى حلب فاستقبله أهلها بالترحيب، وكان معه من القواد",
    ]
    assert R._shared_run(mixed, 6)
    assert not R._shared_run(["الله أكبر الله أكبر أشهد أن لا إله إلا الله", "أشهد أن محمدا رسول الله حي على الصلاة"], 6)


def test_auto_batch_follows_memory(monkeypatch):
    from warraq.models import hardware

    ocr = type("M", (), {"device": "mps"})()
    for gb, expected in ((8, 2), (16, 4), (24, 6)):
        monkeypatch.setattr(hardware, "device_memory_gb", lambda device, gb=gb: gb)
        assert R._auto_batch(ocr) == expected
    ocr.device = "cpu"
    assert R._auto_batch(ocr) == 1
