import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))

import metrics as M  # noqa: E402


def test_conventions_do_not_count_as_errors():
    gold = "الولد كذلك : إما ألا يتم خلقه ، وإما ألا يعيش (2) ] ؛ وكذلك الكبش"
    pred = "الوَلَدُ كذلك: إما ألّا يتم خلقه، وإما ألا يعيش (٢)]؛ وكذلـك الكبش"
    s = M.score(pred, gold)
    assert s["char_errors"] == 0 and s["word_errors"] == 0
    assert s["raw_char_errors"] > 0


def test_real_errors_count():
    s = M.score("وقد تجد الرجل يطلب الاثار", "وقد تجد الرجل يطلب الآثار")
    assert s["char_errors"] == 1 and s["word_errors"] == 1
    acc = M.accuracy([s, M.score("نعم", "نعم")])
    assert 0.9 < acc["char_accuracy"] < 1.0 and acc["lines"] == 2
    assert M.edit_distance("كتاب", "") == 4
