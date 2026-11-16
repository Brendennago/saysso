"""Agreement between saysso verdicts and human labels (no numpy).

labels.csv: columns id,label. `id` is a claim id (c1, c2, ...) from a single-report JSON, or
"<input>#<claim id>" when the JSON holds several reports. Labels are Verdict values.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .models import Verdict


def load_predictions(path: str) -> dict[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    reports = data if isinstance(data, list) else [data]
    multi = len(reports) > 1
    out: dict[str, str] = {}
    for rep in reports:
        for c in rep["claims"]:
            key = f"{rep['input']}#{c['claim']['id']}" if multi else c["claim"]["id"]
            out[key] = Verdict.parse(c["verdict"]).value
    return out


def load_labels(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = csv.DictReader(f)
        if not rows.fieldnames or not {"id", "label"} <= set(rows.fieldnames):
            raise ValueError("labels file needs columns: id,label")
        for row in rows:
            if row["id"].strip():
                out[row["id"].strip()] = Verdict.parse(row["label"]).value
    return out


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    if pe == 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def agreement(pred: dict[str, str], gold: dict[str, str]) -> dict[str, Any]:
    ids = sorted(set(pred) & set(gold))
    pairs = [(gold[i], pred[i]) for i in ids]  # (human, saysso)
    labels = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    matrix = {h: {p: 0 for p in labels} for h in labels}
    for h, p in pairs:
        matrix[h][p] += 1
    per = {}
    for lab in labels:
        tp = matrix[lab][lab]
        pred_n = sum(matrix[h][lab] for h in labels)
        gold_n = sum(matrix[lab].values())
        per[lab] = {
            "precision": tp / pred_n if pred_n else None,
            "recall": tp / gold_n if gold_n else None,
            "support": gold_n,
        }
    raw = sum(1 for a, b in pairs if a == b) / len(pairs) if pairs else None
    return {
        "n": len(pairs), "unmatched_predictions": len(set(pred) - set(gold)),
        "unmatched_labels": len(set(gold) - set(pred)),
        "raw_agreement": raw, "kappa": cohens_kappa(pairs), "labels": labels, "matrix": matrix, "per_label": per,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def render(res: dict[str, Any]) -> str:
    lines = [f"n = {res['n']} claims labeled by both"]
    if res["unmatched_labels"] or res["unmatched_predictions"]:
        lines.append(f"({res['unmatched_labels']} labels had no prediction, "
                     f"{res['unmatched_predictions']} predictions had no label)")
    k = res["kappa"]
    lines.append(f"raw agreement {_pct(res['raw_agreement'])}   Cohen's kappa {'n/a' if k is None else f'{k:.3f}'}")
    labs = res["labels"]
    width = max([len(x) for x in labs] + [8])
    lines.append("")
    lines.append("rows = human, columns = saysso")
    lines.append(" " * (width + 2) + "  ".join(f"{x[:10]:>10}" for x in labs))
    for h in labs:
        lines.append(f"{h:<{width}}  " + "  ".join(f"{res['matrix'][h][p]:>10}" for p in labs))
    lines.append("")
    lines.append(f"{'label':<{width}}  precision  recall  support")
    for lab in labs:
        s = res["per_label"][lab]
        lines.append(f"{lab:<{width}}  {_pct(s['precision']):>9}  {_pct(s['recall']):>6}  {s['support']:>7}")
    return "\n".join(lines)
