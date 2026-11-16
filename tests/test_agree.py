import json

import pytest

from saysso.agree import agreement, cohens_kappa, load_labels, load_predictions


def test_kappa_hand_computed():
    # human: 5 SUPPORTED, 5 UNSUPPORTED; tool agrees on 8/10 (4+4). po=.8, pe=.5 -> kappa=.6
    pairs = [("SUPPORTED", "SUPPORTED")] * 4 + [("SUPPORTED", "UNSUPPORTED")] + \
            [("UNSUPPORTED", "UNSUPPORTED")] * 4 + [("UNSUPPORTED", "SUPPORTED")]
    assert cohens_kappa(pairs) == pytest.approx(0.6)
    assert cohens_kappa([("A", "A")] * 3) == 1.0
    assert cohens_kappa([]) is None


def test_agreement_from_files(tmp_path):
    pred = {"input": "r.md", "claims": [{"claim": {"id": f"c{i}"}, "verdict": v} for i, v in
            enumerate(["SUPPORTED", "SUPPORTED", "UNSUPPORTED", "CONTRADICTED"], 1)]}
    (tmp_path / "p.json").write_text(json.dumps(pred))
    (tmp_path / "l.csv").write_text("id,label\nc1,SUPPORTED\nc2,UNSUPPORTED\nc3,UNSUPPORTED\nc4,CONTRADICTED\nc9,PARTIAL\n")
    res = agreement(load_predictions(str(tmp_path / "p.json")), load_labels(str(tmp_path / "l.csv")))
    assert res["n"] == 4 and res["raw_agreement"] == 0.75 and res["unmatched_labels"] == 1
    assert res["per_label"]["SUPPORTED"]["precision"] == 0.5 and res["per_label"]["SUPPORTED"]["recall"] == 1.0
    assert res["matrix"]["UNSUPPORTED"]["SUPPORTED"] == 1
