from srotaai.signals import _compute


def test_strong_association_is_flagged():
    # 10 reports of drugA->rash, plus background noise on other pairs.
    rows = [("drugA", "rash")] * 10 + [("drugB", "nausea")] * 30 + [("drugB", "rash")] * 2
    out = {(s["drug"], s["event"]): s for s in _compute(rows, min_n=3)}
    sig = out[("drugA", "rash")]
    assert sig["is_signal"] is True
    assert sig["n"] == 10
    assert sig["prr"] >= 2.0


def test_min_n_filters_sparse_cells():
    rows = [("drugA", "rash")] * 2 + [("drugB", "nausea")] * 20
    cells = {(s["drug"], s["event"]) for s in _compute(rows, min_n=3)}
    assert ("drugA", "rash") not in cells      # n=2 < min_n
    assert ("drugB", "nausea") in cells


def test_prr_matches_hand_calculation():
    # a=6 b=4 c=4 d=36 (N=50)  ->  PRR = (6/10) / (4/40) = 6.0
    rows = ([("X", "E")] * 6 + [("X", "other")] * 4 +
            [("Y", "E")] * 4 + [("Y", "other")] * 36)
    sig = next(s for s in _compute(rows, min_n=3) if (s["drug"], s["event"]) == ("X", "E"))
    assert sig["prr"] == 6.0
