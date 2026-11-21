from srotaai.signals import _compute


def rep(drug, event):
    """One report that mentions exactly one drug and one event."""
    return ({drug}, {event}, {(drug, event)})


def test_strong_association_is_flagged():
    # 10 reports of drugA->rash, plus background noise on other pairs.
    rows = [rep("drugA", "rash")] * 10 + [rep("drugB", "nausea")] * 30 + [rep("drugB", "rash")] * 2
    out = {(s["drug"], s["event"]): s for s in _compute(rows, min_n=3)}
    sig = out[("drugA", "rash")]
    assert sig["is_signal"] is True
    assert sig["n"] == 10
    assert sig["prr"] >= 2.0


def test_min_n_filters_sparse_cells():
    rows = [rep("drugA", "rash")] * 2 + [rep("drugB", "nausea")] * 20
    cells = {(s["drug"], s["event"]) for s in _compute(rows, min_n=3)}
    assert ("drugA", "rash") not in cells      # n=2 < min_n
    assert ("drugB", "nausea") in cells


def test_prr_matches_hand_calculation():
    # a=6 b=4 c=4 d=36 (N=50)  ->  PRR = (6/10) / (4/40) = 6.0
    rows = ([rep("X", "E")] * 6 + [rep("X", "other")] * 4 +
            [rep("Y", "E")] * 4 + [rep("Y", "other")] * 36)
    sig = next(s for s in _compute(rows, min_n=3) if (s["drug"], s["event"]) == ("X", "E"))
    assert sig["prr"] == 6.0


def test_reports_without_pairs_count_toward_the_population():
    # Same 10 drugA->rash reports, but now 100 unrelated reports exist.
    # Reporting-unit denominators must grow N, making drugA->rash MORE
    # disproportionate than when only co-occurring pairs are counted.
    small = [rep("drugA", "rash")] * 10 + [rep("drugB", "nausea")] * 10
    big = small + [(set(), set(), set())] * 100
    prr_small = next(s["prr"] for s in _compute(small, min_n=3) if s["drug"] == "drugA")
    prr_big = next(s["prr"] for s in _compute(big, min_n=3) if s["drug"] == "drugA")
    assert prr_big >= prr_small
